import asyncio
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import main
from app.accounts import AccountBook, Strategy
from app.history import HistoryDB
from app.ledger import Ledger
from app.models import Event, GameState, Quote
from app.monitor_state import MonitorState
from app.store import Store


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    accounts = AccountBook(str(tmp_path / "accounts.db"))
    history = HistoryDB(str(tmp_path / "history.db"))
    monitor = MonitorState(str(tmp_path / "state.db"))
    accounts.seed([
        Strategy("Active", edge_threshold=0.0, sizing="flat", flat_stake=100,
                 start_bankroll=1_000)
    ])

    monkeypatch.setattr(main, "store", Store())
    monkeypatch.setattr(main, "ledger", ledger)
    monkeypatch.setattr(main, "account_book", accounts)
    monkeypatch.setattr(main, "history_db", history)
    monkeypatch.setattr(main, "monitor_state", monitor)
    monkeypatch.setattr(main, "tasks", {})
    monkeypatch.setattr(main, "_finalized", set())
    monkeypatch.setattr(main, "_terminal_events", {})
    monkeypatch.setattr(main, "_event_locks", {})
    monkeypatch.setattr(main, "_pregame", {})
    monkeypatch.setattr(main, "_subscribers", set())

    try:
        yield {
            "ledger": ledger,
            "accounts": accounts,
            "history": history,
            "monitor": monitor,
        }
    finally:
        ledger.close()
        accounts.close()
        history.close()
        monitor.close()


def _event() -> Event:
    return Event(
        id="runtime-event",
        name="Celtics at Knicks",
        sport="basketball",
        home="Knicks",
        away="Celtics",
        league="NBA",
        polymarket_slug="nba-runtime-event",
    )


def _quotes(event: Event) -> list[Quote]:
    return [
        Quote(event.id, "moneyline", event.home, .445, "Polymarket",
              bid=.44, ask=.45, ask_size=1_000),
        Quote(event.id, "moneyline", event.away, .555, "Polymarket",
              bid=.55, ask=.56, ask_size=1_000),
        Quote(event.id, "moneyline", event.home, .62, "Pinnacle"),
        Quote(event.id, "moneyline", event.away, .38, "Pinnacle"),
        Quote(event.id, "moneyline", event.home, .62, "Circa"),
        Quote(event.id, "moneyline", event.away, .38, "Circa"),
    ]


def _state(event: Event, status: str, home_score: float = 112,
           away_score: float = 104) -> GameState:
    return GameState(
        event.id, home_score, away_score, "4", "00:00", "fixture", status=status
    )


def _outcome_count(history: HistoryDB) -> int:
    with sqlite3.connect(history.path) as connection:
        return connection.execute("SELECT COUNT(*) FROM event_outcomes").fetchone()[0]


def _register(runtime, event: Event) -> None:
    main.store.add_event(event)
    runtime["monitor"].save_event(event)


def test_terminal_state_blocks_first_entry_and_all_late_accounts(runtime):
    event = _event()
    _register(runtime, event)
    main.store.add_quotes(_quotes(event))

    async def scenario():
        # "completed" used to fall through the narrower final-status set.
        await main.on_state(_state(event, "completed"))
        runtime["accounts"].seed([
            Strategy("Late", edge_threshold=0.0, sizing="flat", flat_stake=100,
                     start_bankroll=1_000)
        ])
        await main.on_quotes(_quotes(event))

    asyncio.run(scenario())

    assert runtime["accounts"].account_bets("Active") == []
    assert runtime["accounts"].account_bets("Late") == []
    assert runtime["ledger"].event_bets(event.id) == []
    assert event.id in main._finalized


def test_finalization_failure_keeps_restore_state_and_retries_without_double_credit(
        runtime, monkeypatch):
    event = _event()
    _register(runtime, event)
    real_log_outcome = runtime["history"].log_outcome
    attempts = 0

    def flaky_log_outcome(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("one-time history failure")
        return real_log_outcome(*args, **kwargs)

    monkeypatch.setattr(runtime["history"], "log_outcome", flaky_log_outcome)
    credited_once = {}

    async def scenario():
        await main.on_quotes(_quotes(event))
        with pytest.raises(RuntimeError, match="one-time history failure"):
            await main.on_state(_state(event, "final"))
        assert event.id not in main._finalized
        assert [restored.id for restored in runtime["monitor"].events()] == [event.id]
        credited_once["bankroll"] = runtime["accounts"].leaderboard()[0]["bankroll"]
        await main.finalize_event(event.id)

    asyncio.run(scenario())

    bet = runtime["accounts"].account_bets("Active")[0]
    assert bet["status"] == "win"
    assert runtime["accounts"].leaderboard()[0]["bankroll"] == pytest.approx(
        credited_once["bankroll"]
    )
    assert runtime["monitor"].events() == []
    assert event.id in main._finalized
    assert attempts == 2


def test_manual_delete_voids_open_bets_without_writing_completed_outcome(runtime):
    event = _event()
    _register(runtime, event)

    async def scenario():
        await main.on_quotes(_quotes(event))
        main.store.add_state(_state(event, "in_progress", 70, 60))
        await main.delete_event(event.id)

    asyncio.run(scenario())

    bet = runtime["accounts"].account_bets("Active")[0]
    assert bet["status"] == "void"
    assert bet["pnl"] == pytest.approx(0)
    assert runtime["accounts"].leaderboard()[0]["bankroll"] == pytest.approx(1_000)
    assert runtime["ledger"].event_bets(event.id)[0]["settled_result"] is None
    assert _outcome_count(runtime["history"]) == 0
    assert runtime["monitor"].events() == []
    assert event.id not in main.store.events


def test_provider_cancellation_voids_instead_of_grading_score(runtime):
    event = _event()
    _register(runtime, event)

    async def scenario():
        await main.on_quotes(_quotes(event))
        await main.on_state(_state(event, "cancelled", 70, 60))

    asyncio.run(scenario())

    bet = runtime["accounts"].account_bets("Active")[0]
    assert bet["status"] == "void"
    assert runtime["accounts"].leaderboard()[0]["bankroll"] == pytest.approx(1_000)
    assert runtime["ledger"].event_bets(event.id)[0]["settled_result"] is None
    assert _outcome_count(runtime["history"]) == 0
    assert event.id in main._finalized


def test_lifespan_awaits_background_task_cancellation(monkeypatch):
    sports_started = threading.Event()
    sports_stopped = threading.Event()
    auto_started = threading.Event()
    auto_stopped = threading.Event()

    async def sports_worker(*_args, **_kwargs):
        sports_started.set()
        try:
            await asyncio.Future()
        finally:
            sports_stopped.set()

    async def auto_worker():
        auto_started.set()
        try:
            await asyncio.Future()
        finally:
            auto_stopped.set()

    monkeypatch.setattr(main, "polymarket_sports_stream", sports_worker)
    monkeypatch.setattr(main, "auto_monitor_loop", auto_worker)

    with TestClient(main.app):
        assert sports_started.wait(2)
        assert auto_started.wait(2)

    assert sports_stopped.wait(2)
    assert auto_stopped.wait(2)
    assert main.ledger is None
    assert main.account_book is None
    assert main.history_db is None
    assert main.monitor_state is None
