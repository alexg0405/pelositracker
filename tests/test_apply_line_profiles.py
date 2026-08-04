"""Per-line reversal confirmation + the offline profile apply tool.

The 2026-08-02 settlement grading split the reversal exit's value by line
(moneyline giveback, spread/totals savings), which only a per-line
``reversal_confirmation_readings`` can express. These tests pin the field's
whole path — profile normalization, effective-policy resolution, entry
snapshot — and the apply tool's save shape, session boundary, and
old-runtime guard.
"""
import json
import sqlite3

import pytest

from app.polymarket_us_trading import (
    TradingPolicy,
    TradingPolicyError,
)
from tools.apply_line_profiles import (
    RECOMMENDED_PROFILES,
    apply_profiles,
    load_saved_payload,
    merged_policy,
    new_field_overrides,
    strip_new_fields,
)

from tests.test_polymarket_us_trading import Clock, make_trader


def _policy_with_reversal_profile(readings=5):
    return TradingPolicy.from_mapping({
        "line_execution_profiles": [{
            "market_type": "moneyline",
            "game_stage": "all",
            "enabled": True,
            "overrides": {"reversal_confirmation_readings": readings},
        }],
    })


def test_profile_reversal_readings_resolve_per_line():
    policy = _policy_with_reversal_profile(readings=5)
    moneyline, key = policy.execution_policy_for("moneyline", 0.75)
    assert key == "moneyline/all"
    assert moneyline.reversal_confirmation_readings == 5
    # Lines without the profile keep the lane-wide window.
    spread, key = policy.execution_policy_for("spread", 0.75)
    assert key == "global"
    assert spread.reversal_confirmation_readings == (
        policy.reversal_confirmation_readings
    )


def test_profile_reversal_readings_coerce_and_validate():
    coerced = _policy_with_reversal_profile(readings="4")
    effective, _ = coerced.execution_policy_for("moneyline", None)
    assert effective.reversal_confirmation_readings == 4
    # The effective overlay runs the same 1..10 bounds as the lane field.
    with pytest.raises(TradingPolicyError, match="reversal_confirmation"):
        _policy_with_reversal_profile(readings=0)
    with pytest.raises(TradingPolicyError, match="reversal_confirmation"):
        _policy_with_reversal_profile(readings=11)


def test_profile_reversal_readings_govern_the_confirmation_window(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "reversal_confirmation_readings": 1,
        "min_edge": 0.02,
        "cycle_seconds": 30,
        "line_execution_profiles": [{
            "market_type": "moneyline",
            "game_stage": "all",
            "enabled": True,
            "overrides": {"reversal_confirmation_readings": 3},
        }],
    })
    effective, _ = trader.policy.execution_policy_for("moneyline", 0.8)
    position = {"reversal_triggered_ts": None, "reversal_observation_count": 0}
    # Under the per-line window a single adverse quote no longer confirms.
    confirmed, _, count = trader._reversal_confirmation(
        position, effective, -0.05
    )
    assert (confirmed, count) == (False, 1)
    # The lane-wide window still confirms immediately.
    confirmed, _, count = trader._reversal_confirmation(
        position, trader.policy, -0.05
    )
    assert (confirmed, count) == (True, 1)
    trader.close()


def test_recommended_payloads_validate_for_both_lanes():
    for lane, profiles in RECOMMENDED_PROFILES.items():
        policy = TradingPolicy.from_mapping({
            "line_execution_profiles": profiles,
        })
        # Every totals stage runs the disciplined pocket — late included
        # (late Unders graded +78% settled once the side gate landed) —
        # and never the bare lane gates.
        for fraction in (0.10, 0.375, 0.75):
            stage_policy, _ = policy.execution_policy_for("total", fraction)
            assert stage_policy is not None, (lane, fraction)
            # Dry probes the widened-Unders bands (ceiling 0.45, floor
            # 0.06); live holds the graded pocket until dry reads out.
            assert stage_policy.max_entry_price == pytest.approx(
                0.45 if lane == "dry" else 0.38
            ), lane
            assert stage_policy.min_edge == pytest.approx(
                0.03 if lane == "dry" else 0.10
            ), (lane, fraction)
        middle, _ = policy.execution_policy_for("total", 0.375)
        moneyline, _ = policy.execution_policy_for("moneyline", None)
        assert moneyline.max_edge == pytest.approx(0.10), lane
        # Guards-off on ML/spread: vestigial stop and maximum reversal
        # window — full-path grading showed every price-triggered exit
        # losing to settlement on these lines. Totals keep their guards.
        assert moneyline.stop_loss == pytest.approx(0.95), lane
        assert moneyline.reversal_confirmation_readings == 10, lane
        # The wall-clock floor keeps the reversal window at ~5 minutes
        # regardless of how fast the analysis cycle runs.
        assert moneyline.reversal_confirmation_seconds == pytest.approx(
            300.0
        ), lane
        assert middle.reversal_confirmation_seconds == pytest.approx(
            300.0
        ), lane
        spread, _ = policy.execution_policy_for("spread", None)
        assert spread.stop_loss == pytest.approx(0.95), lane
        assert spread.reversal_confirmation_readings == 10, lane
        if lane == "live":
            # Live spread runs concentrated: the profile-only caps bound
            # the thinnest-edge book, and it adopts the validated
            # agreement gate at 55 (sub-55 dogs' CI includes zero;
            # 55-70 is proven volume, so 70 would over-gate).
            assert spread.max_position_usd == pytest.approx(1.0), lane
            assert spread.max_profile_open_positions == 2, lane
            assert spread.max_profile_exposure_usd == pytest.approx(5.0), lane
            assert spread.min_source_agreement == pytest.approx(55.0), lane
            # ML fires on first sight (the missed reading-1 pocket graded
            # +142.7% per $1); spread keeps the lane's glitch guard.
            assert moneyline.entry_confirmation_readings == 1
            assert spread.entry_confirmation_readings == (
                policy.entry_confirmation_readings
            )
            # Validated floor: 10-15c MLs graded +403.8%/$1, CI > 0.
            assert moneyline.min_entry_price == pytest.approx(0.10), lane
            # Untested-but-directional decay values hold on the money lane.
            assert moneyline.exit_edge == pytest.approx(-0.30), lane
            assert spread.exit_edge == pytest.approx(-0.15), lane
        else:
            # Dry reopens the agreement control band and probes one regime
            # step past live everywhere.
            assert spread.min_source_agreement == pytest.approx(
                policy.min_source_agreement
            ), lane
            assert spread.min_entry_price == pytest.approx(0.15), lane
            assert moneyline.min_entry_price == pytest.approx(0.06), lane
            assert moneyline.min_edge == pytest.approx(0.01), lane
            assert moneyline.exit_edge == pytest.approx(-0.50), lane
            assert spread.exit_edge == pytest.approx(-0.30), lane
        # Unders joined guards-off: the totals guards' protective record
        # was Over contamination (reversal-sold Unders won 76%, stopped
        # Unders 50%). The side gate is the totals protection.
        assert middle.reversal_confirmation_readings == 10, lane
        assert middle.stop_loss == pytest.approx(0.95), lane


def test_strip_new_fields_degrades_to_a_legacy_safe_payload():
    for profiles in RECOMMENDED_PROFILES.values():
        assert new_field_overrides(profiles)
        stripped = strip_new_fields(profiles)
        assert not new_field_overrides(stripped)
        # Nothing else may change: same profiles, same other overrides.
        from tools.apply_line_profiles import RUNTIME_NEW_PROFILE_FIELDS
        assert [
            {**p, "overrides": {
                k: v for k, v in p["overrides"].items()
                if k not in RUNTIME_NEW_PROFILE_FIELDS
            }}
            for p in profiles
        ] == stripped


def test_apply_writes_the_exact_server_save_shape(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({"min_edge": 0.04, "stop_loss": 0.5})
    trader.close()
    database = tmp_path / "trading.db"

    before = load_saved_payload(database)
    token = apply_profiles(database, RECOMMENDED_PROFILES["dry"])

    after = load_saved_payload(database)
    assert after["_execution_control_token"] == token
    assert after["_execution_control_token"] != before["_execution_control_token"]
    # Only the profiles changed; every other saved field survived.
    assert after["min_edge"] == pytest.approx(0.04)
    assert after["stop_loss"] == pytest.approx(0.5)
    profiles = {
        (p["market_type"], p["game_stage"]): p
        for p in after["line_execution_profiles"]
    }
    assert profiles[("moneyline", "all")]["overrides"][
        "reversal_confirmation_readings"
    ] == 10
    # Totals run one consolidated all-stage profile carrying the pocket.
    assert profiles[("total", "all")]["enabled"] is True
    assert profiles[("total", "all")]["overrides"]["stop_loss"] == 0.95
    assert ("total", "late") not in profiles

    # The save is a policy-session boundary, exactly like configure().
    connection = sqlite3.connect(database)
    try:
        open_rows = connection.execute(
            "SELECT reason FROM trading_policy_sessions WHERE ended_ts IS NULL"
        ).fetchall()
    finally:
        connection.close()
    assert [row[0] for row in open_rows] == ["external_profile_apply"]

    # A reopened runtime adopts the applied profiles wholesale.
    reopened = make_trader(tmp_path, clock, fail_on_orders=True)
    effective, key = reopened.policy.execution_policy_for("moneyline", 0.8)
    assert key == "moneyline/all"
    assert effective.reversal_confirmation_readings == 10
    assert effective.stop_loss == pytest.approx(0.95)
    reopened.close()


def test_apply_refuses_an_invalid_merge(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.close()
    database = tmp_path / "trading.db"
    before = load_saved_payload(database)
    with pytest.raises(TradingPolicyError):
        apply_profiles(database, [{
            "market_type": "moneyline",
            "game_stage": "all",
            "enabled": True,
            "overrides": {"reversal_confirmation_readings": 11},
        }])
    # A refused merge writes nothing.
    assert load_saved_payload(database) == before


def test_merged_policy_preserves_unrelated_saved_state(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({"automation_enabled": True, "cycle_seconds": 15})
    trader.close()
    database = tmp_path / "trading.db"
    policy = merged_policy(
        load_saved_payload(database), RECOMMENDED_PROFILES["dry"]
    )
    assert policy.automation_enabled is True
    assert policy.cycle_seconds == pytest.approx(15)


def test_running_server_inference_is_per_field():
    from tools.apply_line_profiles import proven_runtime_fields

    # A stored payload proves exactly the fields it carries — a runtime
    # that parsed `readings` may still predate `seconds`.
    stored = {
        "line_execution_profiles": [{
            "market_type": "moneyline", "game_stage": "all", "enabled": True,
            "overrides": {"reversal_confirmation_readings": 10},
        }],
    }
    assert proven_runtime_fields(stored) == {"reversal_confirmation_readings"}
    assert proven_runtime_fields({}) == set()

    payload_with_seconds = [{
        "market_type": "moneyline", "game_stage": "all", "enabled": True,
        "overrides": {"reversal_confirmation_seconds": 300.0},
    }]
    unproven = new_field_overrides(payload_with_seconds) - \
        proven_runtime_fields(stored)
    assert unproven == {"reversal_confirmation_seconds"}


def test_reversal_floor_requires_wall_clock(tmp_path):
    """A fast cycle cannot compress the reversal window below the floor."""
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "reversal_confirmation_readings": 3,
        "reversal_confirmation_seconds": 60.0,
        "min_edge": 0.03,
        "cycle_seconds": 7,
        "candidate_cooldown_seconds": 120,
    })
    position = {"reversal_triggered_ts": None, "reversal_observation_count": 0}
    # Three adverse readings at 7s spacing satisfy the count in 14s of
    # elapsed streak — but not the 60s floor.
    for expected_count in (1, 2, 3):
        confirmed, triggered, count = trader._reversal_confirmation(
            position, trader.policy, -0.05
        )
        assert count == expected_count
        assert confirmed is False
        position.update(
            reversal_triggered_ts=triggered, reversal_observation_count=count
        )
        clock.value += 7
    # Keep the streak alive past the floor: now it confirms.
    for _ in range(20):
        confirmed, triggered, count = trader._reversal_confirmation(
            position, trader.policy, -0.05
        )
        position.update(
            reversal_triggered_ts=triggered, reversal_observation_count=count
        )
        if confirmed:
            break
        clock.value += 7
    assert confirmed is True
    assert clock.value - position["reversal_triggered_ts"] >= 60
    # One recovery reading still wipes everything.
    assert trader._reversal_confirmation(position, trader.policy, 0.01) == (
        False, None, 0,
    )
    with pytest.raises(TradingPolicyError, match="reversal_confirmation_seconds"):
        trader.configure({"reversal_confirmation_seconds": 1801})
    trader.close()


def test_spread_side_policy_round_trips_and_validates(tmp_path):
    from app.polymarket_us_trading import _spread_side

    assert _spread_side("Philadelphia Phillies -1.5") == "favorite"
    assert _spread_side("New York Mets +2.5") == "underdog"
    assert _spread_side("Over 11.5") is None
    assert _spread_side("St. Louis Cardinals") is None

    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({"allowed_spread_sides": ["Underdog"]})
    assert trader.policy.allowed_spread_sides == ("underdog",)
    with pytest.raises(TradingPolicyError, match="allowed_spread_sides"):
        trader.configure({"allowed_spread_sides": []})
    with pytest.raises(TradingPolicyError, match="allowed_spread_sides"):
        trader.configure({"allowed_spread_sides": ["underdog", "push"]})
    trader.close()

    reopened = make_trader(tmp_path, clock, fail_on_orders=True)
    assert reopened.policy.allowed_spread_sides == ("underdog",)
    reopened.close()


def test_totals_side_policy_round_trips_and_validates(tmp_path):
    from app.polymarket_us_trading import _totals_side

    assert _totals_side("Over 11.5") == "over"
    assert _totals_side("under 8.5") == "under"
    assert _totals_side("Philadelphia Phillies -1.5") is None

    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({"allowed_total_sides": ["Under"]})
    assert trader.policy.allowed_total_sides == ("under",)
    with pytest.raises(TradingPolicyError, match="allowed_total_sides"):
        trader.configure({"allowed_total_sides": []})
    with pytest.raises(TradingPolicyError, match="allowed_total_sides"):
        trader.configure({"allowed_total_sides": ["under", "push"]})
    trader.close()

    # The saved policy survives a restart with the side gate intact.
    reopened = make_trader(tmp_path, clock, fail_on_orders=True)
    assert reopened.policy.allowed_total_sides == ("under",)
    reopened.close()


def test_apply_tool_carries_the_lane_side_override(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.close()
    database = tmp_path / "trading.db"
    apply_profiles(
        database,
        RECOMMENDED_PROFILES["dry"],
        lane_overrides={"allowed_total_sides": ["under"]},
    )
    after = load_saved_payload(database)
    assert after["allowed_total_sides"] == ["under"]
    reopened = make_trader(tmp_path, clock, fail_on_orders=True)
    assert reopened.policy.allowed_total_sides == ("under",)
    reopened.close()


def test_new_field_detection_only_flags_the_new_fields():
    assert new_field_overrides([{
        "market_type": "spread", "game_stage": "all", "enabled": True,
        "overrides": {"stop_loss": 0.6, "profit_target": 0.3},
    }]) == set()
    assert new_field_overrides(json.loads(json.dumps(
        RECOMMENDED_PROFILES["live"]
    ))) == {
        "reversal_confirmation_readings",
        "reversal_confirmation_seconds",
    }
