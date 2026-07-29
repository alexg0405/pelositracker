from datetime import datetime, timezone
import sqlite3

import pytest

from app.adaptive_exit_model import (
    ADAPTIVE_EXIT_CLEAR_PHRASE,
    AdaptiveExitModel,
    _SCHEMA_V1,
)
from app.database import Database
from app.models import Event, GameState


class Clock:
    def __init__(self, value: float = 1_800_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_existing_v1_database_upgrades_without_changing_v1_checksum(tmp_path):
    path = tmp_path / "legacy-adaptive.db"
    legacy = Database.open(
        str(path),
        sqlite_envs=(),
        sqlite_default=str(path),
    )
    legacy.initialize(
        _SCHEMA_V1,
        component="adaptive_exit_learning",
        version=1,
    )
    legacy.close()

    model = AdaptiveExitModel(str(path), clock=Clock())
    model.close()

    connection = sqlite3.connect(path)
    try:
        versions = connection.execute(
            """SELECT version FROM schema_migrations
               WHERE component='adaptive_exit_learning'
               ORDER BY version"""
        ).fetchall()
        table = connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name='exit_recovery_observations'"""
        ).fetchone()
    finally:
        connection.close()
    assert versions == [(1,), (2,)]
    assert table == ("exit_recovery_observations",)


def mlb_event(number: int) -> Event:
    return Event(
        id=f"mlb-{number}",
        name=f"Away {number} at Home {number}",
        sport="baseball",
        league="MLB",
        home=f"Home {number}",
        away=f"Away {number}",
    )


def state(clock: Clock, event: Event) -> GameState:
    observed = datetime.fromtimestamp(clock(), timezone.utc)
    return GameState(
        event_id=event.id,
        home_score=3,
        away_score=3,
        period="Top 9",
        clock="",
        source="test-live-feed",
        provider_timestamp=observed,
        received_at=observed,
        processed_at=observed,
        status="in_progress",
        live=True,
        ended=False,
    )


def observe(
    model: AdaptiveExitModel,
    clock: Clock,
    event: Event,
    *,
    position_id: str,
    exit_value: float,
    profile: str = "balanced",
):
    return model.observe(
        position={
            "id": position_id,
            "event_id": event.id,
            "selection": event.away,
            "market_type": "moneyline",
            "mode": "dry_run",
        },
        event=event,
        state=state(clock, event),
        exit_value=exit_value,
        highest_exit_value=max(1.0, exit_value),
        return_fraction=exit_value - 1.0,
        current_edge=0.01,
        profile=profile,
        horizon_seconds=60,
        minimum_samples=5,
        maximum_tightening=0.35,
        profit_target=0.10,
        trailing_drawdown=0.04,
        exit_edge=0.0,
        stop_loss=0.20,
    )


def test_event_balanced_learning_persists_and_only_tightens_profit_exits(
    tmp_path,
):
    path = str(tmp_path / "adaptive.db")
    clock = Clock()
    model = AdaptiveExitModel(path, clock=clock)

    for number in range(6):
        event = mlb_event(number)
        position_id = f"position-{number}"
        observe(
            model,
            clock,
            event,
            position_id=position_id,
            exit_value=1.0,
        )
        clock.value += 180
        observe(
            model,
            clock,
            event,
            position_id=position_id,
            exit_value=0.94,
        )
        clock.value += 30

    next_event = mlb_event(99)
    decision = observe(
        model,
        clock,
        next_event,
        position_id="position-99",
        exit_value=1.0,
    )

    assert decision.active is True
    assert decision.labeled_events >= 5
    assert decision.predicted_adverse_probability > 0.50
    assert decision.effective_profit_target < decision.base_profit_target
    assert (
        decision.effective_trailing_drawdown
        < decision.base_trailing_drawdown
    )
    assert decision.effective_exit_edge >= decision.base_exit_edge
    assert decision.hard_stop_unchanged == pytest.approx(0.20)

    before_close = model.summary()
    assert before_close["labeled_events"] >= 5
    model.close()

    reopened = AdaptiveExitModel(path, clock=clock)
    after_reopen = reopened.summary()
    assert after_reopen["observations"] == before_close["observations"]
    assert after_reopen["labeled_events"] == before_close["labeled_events"]
    reopened.close()


def test_observe_mode_learns_without_changing_any_exit_threshold(tmp_path):
    clock = Clock()
    model = AdaptiveExitModel(str(tmp_path / "observe.db"), clock=clock)
    event = mlb_event(1)

    decision = observe(
        model,
        clock,
        event,
        position_id="observe-position",
        exit_value=1.0,
        profile="observe",
    )

    assert decision.applicable is True
    assert decision.active is False
    assert decision.effective_profit_target == decision.base_profit_target
    assert (
        decision.effective_trailing_drawdown
        == decision.base_trailing_drawdown
    )
    assert decision.effective_exit_edge == decision.base_exit_edge
    assert model.summary()["observations"] == 1
    model.close()


def test_mlb_total_is_collected_with_line_and_game_state_without_new_probability(
    tmp_path,
):
    clock = Clock()
    model = AdaptiveExitModel(str(tmp_path / "total.db"), clock=clock)
    event = mlb_event(1)
    live_state = state(clock, event)
    live_state.home_score = 1
    live_state.away_score = 1
    live_state.period = "Top 5"

    decision = model.observe(
        position={
            "id": "total-position",
            "event_id": event.id,
            "selection": "Over 7.5",
            "market_type": "total",
            "mode": "dry_run",
        },
        event=event,
        state=live_state,
        exit_value=0.30,
        highest_exit_value=0.40,
        return_fraction=-0.25,
        current_edge=0.05,
        profile="observe",
        horizon_seconds=60,
        minimum_samples=5,
        maximum_tightening=0.35,
        profit_target=0.10,
        trailing_drawdown=0.04,
        exit_edge=0.0,
        stop_loss=0.20,
    )

    assert decision.applicable is True
    assert decision.active is False
    assert decision.state["market_type"] == "total"
    assert decision.state["selection_direction"] == "over"
    assert decision.state["line"] == pytest.approx(7.5)
    assert decision.state["current_total_runs"] == pytest.approx(2.0)
    assert decision.state["selection_margin"] == pytest.approx(-5.5)
    assert model.summary()["market_segments"][0]["market_type"] == "total"
    model.close()


def test_post_exit_shadow_records_recovery_without_trading(tmp_path):
    clock = Clock()
    model = AdaptiveExitModel(str(tmp_path / "recovery.db"), clock=clock)
    position = {
        "id": "closed-position",
        "event_id": "mlb-1",
        "event_name": "Away at Home",
        "market_slug": "market-1",
        "market_type": "total",
        "selection": "Over 7.5",
        "position_side": "long",
        "mode": "dry_run",
        "quantity": 2.0,
        "entry_cost": 0.40,
    }
    model.track_exit(
        position=position,
        exit_value=0.30,
        reason="confirmed_stop_loss",
        horizon_seconds=120,
    )

    clock.value += 60
    observation = model.observe_exit_recovery(
        "closed-position",
        exit_value=0.41,
    )

    assert observation["recovered_entry"] is True
    summary = model.exit_recovery_summary()
    assert summary["exits"] == 1
    assert summary["recovered_entry"] == 1
    assert summary["average_rebound"] == pytest.approx(0.11)
    model.close()


def test_unsupported_sport_is_a_no_op_and_clear_requires_exact_phrase(
    tmp_path,
):
    clock = Clock()
    model = AdaptiveExitModel(str(tmp_path / "clear.db"), clock=clock)
    event = mlb_event(1)
    observe(
        model,
        clock,
        event,
        position_id="clear-position",
        exit_value=1.0,
    )
    basketball = Event(
        id="nba-1",
        name="Away at Home",
        sport="basketball",
        league="NBA",
        home="Home",
        away="Away",
    )
    decision = model.observe(
        position={
            "id": "nba-position",
            "selection": "Away",
            "market_type": "moneyline",
            "mode": "dry_run",
        },
        event=basketball,
        state=None,
        exit_value=1.0,
        highest_exit_value=1.0,
        return_fraction=0.0,
        current_edge=0.01,
        profile="responsive",
        horizon_seconds=60,
        minimum_samples=5,
        maximum_tightening=0.35,
        profit_target=0.10,
        trailing_drawdown=0.04,
        exit_edge=0.0,
        stop_loss=0.20,
    )
    assert decision.applicable is False
    assert decision.effective_profit_target == 0.10

    with pytest.raises(ValueError, match=ADAPTIVE_EXIT_CLEAR_PHRASE):
        model.clear("clear")

    result = model.clear(ADAPTIVE_EXIT_CLEAR_PHRASE)
    assert result["deleted_observations"] == 1
    assert result["retained_positions"] is True
    assert result["retained_journal"] is True
    assert model.summary()["observations"] == 0
    model.close()
