"""End-to-end cover for the evidence chain the execution lane now produces.

Each piece below is unit-tested elsewhere. This exercises the seam: one lane,
configured the way an operator would, run across several cycles, asserting that
a candidate observed in one cycle becomes a logged observation, a confirmed
entry, a marked position with both excursion extremes, a profile budget
consumption, and finally an input the settings advisor can read.
"""
from dataclasses import asdict

import pytest

from app.policy_advisor import recommend_policy
from tests.test_polymarket_us_trading import (
    Clock,
    event,
    make_trader,
    multi_market_us_payload,
    signal,
)


@pytest.fixture()
def lane(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "candidate_cooldown_seconds": 60,
        "entry_confirmation_readings": 2,
        "max_confirmation_price_drift": 0.05,
        "min_source_agreement": 50.0,
        "auto_cashout": False,
        "stop_loss": 0.95,
        "profit_target": 0.95,
        "line_execution_profiles": [{
            "market_type": "moneyline",
            "game_stage": "all",
            "enabled": True,
            "overrides": {"min_edge": 0.03, "max_profile_open_positions": 1},
        }],
    })
    return trader, clock


def _readings(clock):
    return [
        signal(clock, market=market, outcome=outcome, probability=probability)
        for market, outcome, probability in (
            ("moneyline", "Away", 0.60),
            ("total", "Over 8.5", 0.60),
        )
    ]


def test_candidate_becomes_logged_evidence_before_it_becomes_a_position(lane):
    trader, clock = lane

    # Cycle 1: first qualifying reading. Confirmation requires two, so nothing
    # may enter, but the candidates must already be logged evidence.
    first = trader.run_cycle([(event(), _readings(clock))], multi_market_us_payload())

    assert first["entries"] == 0
    logged = trader._candidate_observation_opportunities()
    assert len(logged) == 2
    assert {row["state"] for row in logged} == {"confirming"}
    assert all(row["entered"] is False for row in logged)
    # Executable context is captured at the moment of the decision, which is
    # the only place US book prices are ever retained.
    for row in logged:
        assert row["entry_cost"] is not None
        assert row["spread"] is not None
        assert row["book_shares"] is not None
        assert row["propensity_source"] == "deterministic_policy"

    # Cycle 2: a newer reading satisfies confirmation and one entry is taken.
    clock.value += 120
    second = trader.run_cycle([(event(), _readings(clock))], multi_market_us_payload())

    assert second["entries"] == 1
    position = trader.positions(open_only=True)[0]
    frozen = position["entry_policy"]
    # The effective policy is frozen into the position, including the profile.
    assert frozen["execution_profile_key"] == "moneyline/all"
    assert frozen["entry_confirmation_readings"] == 2
    assert position["entry_confirmation_count"] >= 2
    assert position["entry_source_agreement"] == pytest.approx(85.0)
    assert position["entry_spread"] is not None
    assert position["entry_book_shares"] is not None


def test_marks_track_both_excursions_and_the_advisor_can_read_the_result(lane):
    trader, clock = lane
    for _ in range(2):
        trader.run_cycle([(event(), _readings(clock))], multi_market_us_payload())
        clock.value += 120
    assert trader.positions(open_only=True)

    # Walk the price down and back up; both extremes must survive.
    for bid, ask in ((0.30, 0.31), (0.24, 0.25), (0.46, 0.47)):
        clock.value += 60
        trader.run_cycle(
            [(event(), _readings(clock))],
            multi_market_us_payload(bid=bid, ask=ask),
        )

    with trader._db.cursor(dict_rows=True) as cur:
        trader._db.execute(
            cur,
            """SELECT entry_cost, highest_exit_value, lowest_exit_value
               FROM live_managed_positions WHERE status='open'""",
        )
        row = dict(cur.fetchone())
    assert row["lowest_exit_value"] < row["entry_cost"]
    assert row["highest_exit_value"] > row["lowest_exit_value"]

    # The advisor consumes the candidate log as its opportunity population.
    dataset = trader.advisor_dataset(source_lane="dry_run")
    contract = dataset["logging_contract"]
    assert contract["candidate_log_observations"] > 0
    assert contract["unentered_candidate_observations"] > 0
    assert contract["off_policy_identified"] is False
    recommendation = recommend_policy(
        closed_trades=dataset["closed_trades"],
        opportunities=dataset["opportunities"],
        current_policy=asdict(trader.policy),
        objective="balanced",
        target_trades_per_hour=2.0,
    )
    provenance = recommendation["evidence"]["opportunity_provenance"]
    assert provenance["candidate_log_observations"] > 0
    # With unentered candidates present the frequency estimate is no longer
    # bounded by the policy that produced the data.
    assert provenance["frequency_estimate_basis"].startswith("candidate population")


def test_profile_budget_and_execution_state_agree_with_each_other(lane):
    trader, clock = lane
    for _ in range(2):
        trader.run_cycle([(event(), _readings(clock))], multi_market_us_payload())
        clock.value += 120

    open_positions = trader.positions(open_only=True)
    assert len(open_positions) == 1
    state = trader.execution_state()

    # Exposure reporting must match the positions actually held.
    assert state["exposure"]["open_positions"] == len(open_positions)
    assert state["exposure"]["managed_exposure_usd"] == pytest.approx(
        round(sum(float(row["cost_basis"]) for row in open_positions), 2)
    )
    # The moneyline profile allows one open position and now holds it, so a
    # further moneyline entry is refused while the unprofiled total is not.
    assert trader._profile_entries_last_hour("moneyline/all", "dry_run") == 1
    for _ in range(2):
        clock.value += 120
        trader.run_cycle(
            [(event(), [signal(clock, market="moneyline", outcome="Home", probability=0.75)])],
            multi_market_us_payload(),
        )
    assert len(trader.positions(open_only=True)) == 1
    assert any(
        "allowed open positions" in str(item.get("reason") or "")
        for item in trader.status()["last_cycle_evaluations"]
    )
