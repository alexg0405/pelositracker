from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from app.models import Event, GameState, Quote, Signal
from app.sport_model_lab import SportModelLab, _baseball_live_state


def _event(number: int, *, sport: str = "basketball", league: str = "NBA") -> Event:
    return Event(
        id=f"event-{number}",
        name=f"Away {number} at Home {number}",
        sport=sport,
        league=league,
        home=f"Home {number}",
        away=f"Away {number}",
    )


def _state(event: Event, when: datetime, *, home_score: float, away_score: float):
    return GameState(
        event_id=event.id,
        home_score=home_score,
        away_score=away_score,
        period="Q2",
        clock="06:00",
        source="test-state",
        provider_timestamp=when,
        possession=event.home,
        status="in_progress",
    )


def _signal(event: Event, when: datetime, probability: float = 0.62) -> Signal:
    return Signal(
        event_id=event.id,
        market="moneyline",
        outcome=event.home,
        model_probability=probability,
        market_probability=0.55,
        edge=probability - 0.55,
        confidence=78.0,
        action="WATCH",
        reasons=[],
        observed_at=when,
        n_reference_sources=2,
        decision_id=f"decision-{event.id}-{when.timestamp()}",
        engine_version="unchanged-engine",
        configuration_hash="unchanged-config",
    )


def _baseball_state(
    event: Event,
    when: datetime,
    *,
    inning: int,
    half: str,
    home_score: float,
    away_score: float,
) -> GameState:
    return GameState(
        event_id=event.id,
        home_score=home_score,
        away_score=away_score,
        period=f"{half} {inning}",
        clock="",
        source="test-mlb-state",
        provider_timestamp=when,
        status="in_progress",
        quarantined=True,
        quarantine_reason="unknown or invalid game clock",
    )


def test_baseball_state_parser_requires_an_explicit_inning_half():
    assert _baseball_live_state("Top 5") == {
        "inning": 5,
        "half": "top",
        "batting_side": "away",
        "fraction_remaining": pytest.approx(10 / 18),
        "extra_innings_indicator": 0.0,
    }
    assert _baseball_live_state("B9")["batting_side"] == "home"
    assert _baseball_live_state("10th inning bottom")["extra_innings_indicator"] == 1.0
    assert _baseball_live_state("End 5") == {
        "inning": 5,
        "half": "end",
        "batting_side": None,
        "fraction_remaining": pytest.approx(8 / 18),
        "extra_innings_indicator": 0.0,
    }
    assert _baseball_live_state("in progress") is None
    assert _baseball_live_state("", "") is None


def test_model_lab_buckets_momentum_and_labels_without_installing_a_model(tmp_path):
    lab = SportModelLab(str(tmp_path / "model-lab.db"))
    event = _event(1)
    start = datetime(2027, 1, 1, tzinfo=timezone.utc)
    try:
        assert lab.record(
            event,
            [_signal(event, start)],
            [],
            [_state(event, start, home_score=20, away_score=18)],
            as_of=start,
        ) == 1
        # The same selection/time bucket is idempotent.
        assert lab.record(
            event,
            [_signal(event, start + timedelta(seconds=3))],
            [],
            [_state(event, start, home_score=20, away_score=18)],
            as_of=start + timedelta(seconds=3),
        ) == 0
        assert lab.record(
            event,
            [_signal(event, start + timedelta(seconds=30), 0.65)],
            [],
            [_state(event, start + timedelta(seconds=30), home_score=24, away_score=18)],
            as_of=start + timedelta(seconds=30),
        ) == 1
        assert lab.settle_event(event, 100, 90) == 2

        summary = lab.summary()
        segment = summary["segments"][0]
        assert summary["engine_impact"] == "none"
        assert segment["observations"] == 2
        assert segment["observed_events"] == 1
        assert segment["state_observations"] == 2
        assert segment["state_events"] == 1
        assert segment["settled_observations"] == 2
        assert segment["settled_events"] == 1
        assert summary["recent_momentum"][0]["score_swing"] == 4.0
    finally:
        lab.close()


def test_model_lab_fit_is_event_blocked_research_only_and_never_promoted(tmp_path):
    lab = SportModelLab(str(tmp_path / "fit-lab.db"))
    start = datetime(2027, 1, 1, tzinfo=timezone.utc)
    try:
        for event_number in range(10):
            event = _event(event_number)
            for sample in range(5):
                when = start + timedelta(days=event_number, seconds=sample * 30)
                probability = 0.58 + (0.01 * (sample % 2))
                lab.record(
                    event,
                    [_signal(event, when, probability)],
                    [],
                    [
                        _state(
                            event,
                            when,
                            home_score=30 + sample + (event_number % 2),
                            away_score=28 + sample,
                        )
                    ],
                    as_of=when,
                )
            if event_number % 2:
                lab.settle_event(event, 95, 100)
            else:
                lab.settle_event(event, 105, 100)

        result = lab.fit_candidate(sport="basketball", league="nba")
        assert result["status"] in {
            "candidate_beats_baseline",
            "candidate_does_not_beat_baseline",
        }
        assert result["details"]["research_only"] is True
        assert result["details"]["promoted"] is False
        assert result["details"]["split_policy"] == "chronological_event_block_80_20"
        assert result["details"]["train_events"] == 8
        assert result["details"]["test_events"] == 2
        walk_forward = result["details"]["walk_forward_evaluation"]
        assert walk_forward["policy"] == (
            "expanding_window_chronological_event_blocks"
        )
        assert walk_forward["test_events"] == 5
        assert walk_forward["event_block_bootstrap"]["draws"] == 1000
        assert walk_forward["candidate_calibration"]

        blocked = lab.fit_candidate(sport="baseball", league="mlb")
        assert blocked["status"] == "insufficient_data"
        assert blocked["details"]["promoted"] is False
    finally:
        lab.close()


def test_model_lab_separates_targets_and_writes_hashed_research_export(tmp_path):
    lab = SportModelLab(
        str(tmp_path / "archive-lab.db"),
        export_root=str(tmp_path / "exports"),
    )
    event = _event(44, sport="baseball", league="MLB")
    start = datetime(2027, 4, 1, tzinfo=timezone.utc)
    first_signal = _signal(event, start, 0.62)
    first_signal.quote_source = "polymarket-us"
    first_signal.token_id = "token-home"
    quote = Quote(
        event_id=event.id,
        market="moneyline",
        outcome=event.home,
        probability=0.55,
        source="polymarket-us",
        bid=0.54,
        ask=0.56,
        bid_size=120.0,
        ask_size=90.0,
        fee_rate=0.01,
        depth_complete=True,
        token_id="token-home",
        book_hash="book-1",
    )
    try:
        assert lab.record(
            event,
            [first_signal],
            [quote],
            [_baseball_state(
                event,
                start,
                inning=2,
                half="Top",
                home_score=0,
                away_score=0,
            )],
            as_of=start,
        ) == 1
        later = start + timedelta(seconds=190)
        assert lab.record(
            event,
            [_signal(event, later, 0.64)],
            [],
            [_baseball_state(
                event,
                later,
                inning=2,
                half="Bottom",
                home_score=1,
                away_score=0,
            )],
            as_of=later,
        ) == 1
        assert lab.settle_event(event, 4, 2) == 2

        summary = lab.summary()
        counts = {
            row["target_name"]: row["target_count"]
            for row in summary["target_counts"]
        }
        assert counts["event_outcome"] == 2
        assert counts["market_probability_change_3m"] == 1
        assert summary["target_definitions"]["after_cost_strategy_pnl"][
            "version"
        ] == "after-cost-return-v1"

        exported = lab.create_export()
        destination = Path(exported["directory"])
        manifest = json.loads((destination / "manifest.json").read_text("utf-8"))
        assert exported["counts"] == {
            "observations": 2,
            "targets": 3,
            "candidates": 0,
        }
        assert manifest["manifest_sha256"] == exported["manifest_hash"]
        for filename, metadata in manifest["files"].items():
            assert hashlib.sha256(
                (destination / filename).read_bytes()
            ).hexdigest() == metadata["sha256"]
        observation = json.loads(
            (destination / "observations.jsonl")
            .read_text("utf-8")
            .splitlines()[0]
        )
        assert observation["features"]["execution_ask"] == pytest.approx(0.56)
        assert observation["features"]["execution_spread"] == pytest.approx(0.02)
        assert lab.summary()["archive"]["latest_export"]["manifest_hash"] == (
            exported["manifest_hash"]
        )
    finally:
        lab.close()


def test_model_lab_links_only_exact_closed_execution_results(tmp_path):
    lab = SportModelLab(str(tmp_path / "linked-lab.db"))
    event = _event(45, sport="baseball", league="MLB")
    when = datetime(2027, 4, 1, tzinfo=timezone.utc)
    signal = _signal(event, when, 0.61)
    try:
        assert lab.record(
            event,
            [signal],
            [],
            [_baseball_state(
                event,
                when,
                inning=4,
                half="Top",
                home_score=1,
                away_score=1,
            )],
            as_of=when,
        ) == 1
        assert lab.link_execution_results([
            {
                "id": "position-1",
                "status": "closed",
                "mode": "dry_run",
                "entry_decision_id": signal.decision_id,
                "cost_basis": 2.0,
                "realized_pnl": 0.5,
                "opened_ts": when.timestamp(),
                "closed_ts": when.timestamp() + 300,
                "exit_reason": "test",
            },
            {
                "id": "position-unlinked",
                "status": "closed",
                "entry_decision_id": "unknown-decision",
                "cost_basis": 1.0,
                "realized_pnl": -1.0,
            },
        ]) == 1
        assert lab.link_execution_results([
            {
                "id": "position-1",
                "status": "closed",
                "entry_decision_id": signal.decision_id,
                "cost_basis": 2.0,
                "realized_pnl": 0.5,
            },
        ]) == 0
        targets = {
            row["target_name"]: row
            for row in lab.summary()["target_counts"]
        }
        assert targets["after_cost_strategy_pnl"]["target_count"] == 1
    finally:
        lab.close()


def test_mlb_base_out_features_are_oriented_to_the_selected_team(tmp_path):
    lab = SportModelLab(
        str(tmp_path / "orientation-lab.db"),
        export_root=str(tmp_path / "exports"),
    )
    event = _event(46, sport="baseball", league="MLB")
    when = datetime(2027, 4, 1, tzinfo=timezone.utc)
    home_signal = _signal(event, when, 0.61)
    away_signal = _signal(event, when, 0.39)
    away_signal.outcome = event.away
    away_signal.decision_id = "away-decision"
    state = GameState(
        event_id=event.id,
        home_score=2,
        away_score=2,
        period="Bottom 7",
        clock="1 out · 2-1",
        source="MLB Stats API",
        status="in_progress",
        sport_state={
            "schema": "mlb-linescore-v1",
            "inning": 7,
            "half": "bottom",
            "outs": 1,
            "balls": 2,
            "strikes": 1,
            "base_mask": 5,
            "batter": {"id": 10},
            "pitcher": {"id": 20},
        },
    )
    try:
        assert lab.record(
            event,
            [home_signal, away_signal],
            [],
            [state],
            as_of=when,
        ) == 2
        exported = lab.create_export()
        observations = [
            json.loads(line)
            for line in (
                Path(exported["directory"]) / "observations.jsonl"
            ).read_text("utf-8").splitlines()
        ]
        by_side = {row["outcome_side"]: row["features"] for row in observations}
        assert by_side["home"]["oriented_runners_on"] == 2
        assert by_side["away"]["oriented_runners_on"] == -2
        assert by_side["home"]["oriented_outs_remaining"] == 2
        assert by_side["away"]["oriented_outs_remaining"] == -2
        assert by_side["home"]["personnel_state_available"] == 1
    finally:
        lab.close()


def test_mlb_state_candidate_uses_baseball_features_and_stays_research_only(
    tmp_path,
):
    lab = SportModelLab(str(tmp_path / "mlb-fit-lab.db"))
    start = datetime(2027, 4, 1, tzinfo=timezone.utc)
    inning_states = (
        (1, "Top"),
        (2, "Bottom"),
        (4, "Top"),
        (6, "Bottom"),
        (9, "Top"),
    )
    try:
        for event_number in range(10):
            event = _event(event_number, sport="baseball", league="MLB")
            for sample, (inning, half) in enumerate(inning_states):
                when = start + timedelta(days=event_number, seconds=sample * 30)
                home_lead = (event_number % 3) + (1 if sample >= 3 else 0)
                lab.record(
                    event,
                    [_signal(event, when, 0.54 + 0.01 * sample)],
                    [],
                    [
                        _baseball_state(
                            event,
                            when,
                            inning=inning,
                            half=half,
                            home_score=home_lead,
                            away_score=event_number % 2,
                        )
                    ],
                    as_of=when,
                )
            if event_number % 2:
                lab.settle_event(event, 3, 4)
            else:
                lab.settle_event(event, 5, 3)

        summary = lab.summary()
        segment = summary["segments"][0]
        assert segment["fit_supported"] is True
        assert segment["research_fit_ready"] is True
        assert segment["fit_observations"] == 50
        assert summary["recent_momentum"][0]["baseball_inning"] == 9
        assert summary["mlb_research_blueprint"]["status"] == (
            "phase_2_official_base_out_collecting"
        )

        result = lab.fit_candidate(sport="baseball", league="mlb")
        assert result["status"] in {
            "candidate_beats_baseline",
            "candidate_does_not_beat_baseline",
        }
        assert result["details"]["research_phase"] == "mlb_official_base_out_shadow"
        assert result["details"]["production_ready"] is False
        assert result["details"]["promoted"] is False
        assert "batting_side_indicator" in result["details"]["features"]
        assert "late_inning_indicator" in result["details"]["features"]
        assert "tie_game_indicator" in result["details"]["features"]
        assert result["details"]["uses_existing_probability_as_fixed_offset"] is True
        assert "outs" in " ".join(
            result["details"]["mlb_blueprint"]["priority_order"][2]["parameters"]
        )
        last_decision = (
            f"decision-event-9-"
            f"{(start + timedelta(days=9, seconds=4 * 30)).timestamp()}"
        )
        readiness = lab.advisory_evidence([last_decision], sport="baseball")
        assert readiness["stage"] in {
            "fitted_shadow",
            "validated_advisory_shadow",
        }
        assert readiness["engine_impact"] == "none"
        assert readiness["live_eligible"] is False
        assert readiness["decision_score_coverage"] == {
            "requested": 1,
            "scored": 1,
        }
        assert any(
            "base/out" in blocker for blocker in readiness["live_blockers"]
        )
    finally:
        lab.close()
