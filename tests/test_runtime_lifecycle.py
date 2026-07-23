import asyncio
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timezone

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
    monkeypatch.setattr(main.engine, "allow_fixture_policies", True)
    monkeypatch.setattr(main.engine, "calibrated_markets", {"moneyline"})

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


def _event(suffix: str = "") -> Event:
    tag = f"-{suffix}" if suffix else ""
    return Event(
        id=f"runtime-event{tag}",
        name="Celtics at Knicks",
        sport="basketball",
        home="Knicks",
        away="Celtics",
        league="NBA",
        polymarket_slug=f"nba-runtime-event{tag}",
    )


def _quotes(event: Event) -> list[Quote]:
    observed = datetime.now(timezone.utc)
    return [
        Quote(event.id, "moneyline", event.home, .445, "Polymarket",
              observed, bid=.44, ask=.45, ask_size=1_000, depth_complete=True,
              fee_rate=0.0, token_id="token-home",
              bid_levels=((.44, 1_000.0),), ask_levels=((.45, 1_000.0),)),
        Quote(event.id, "moneyline", event.away, .555, "Polymarket",
              observed, bid=.55, ask=.56, ask_size=1_000, depth_complete=True,
              fee_rate=0.0, token_id="token-away",
              bid_levels=((.55, 1_000.0),), ask_levels=((.56, 1_000.0),)),
        Quote(event.id, "moneyline", event.home, .62, "Pinnacle", observed),
        Quote(event.id, "moneyline", event.away, .38, "Pinnacle", observed),
        Quote(event.id, "moneyline", event.home, .62, "Circa", observed),
        Quote(event.id, "moneyline", event.away, .38, "Circa", observed),
    ]


def _state(event: Event, status: str, home_score: float = 112,
           away_score: float = 104) -> GameState:
    return GameState(
        event.id, home_score, away_score, "4", "00:00", "fixture",
        observed_at=datetime.now(timezone.utc), status=status
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


def test_manual_delete_is_blocked_while_paper_bot_positions_are_open(runtime):
    event = _event()
    _register(runtime, event)

    async def scenario():
        await main.on_quotes(_quotes(event))
        main.store.add_state(_state(event, "in_progress", 70, 60))
        with pytest.raises(main.HTTPException) as error:
            await main.delete_event(event.id)
        assert error.value.status_code == 409

    asyncio.run(scenario())

    bet = runtime["accounts"].account_bets("Active")[0]
    assert bet["status"] == "open"
    assert bet["pnl"] is None
    assert runtime["accounts"].leaderboard()[0]["bankroll"] < 1_000
    assert runtime["ledger"].event_bets(event.id)[0]["settled_result"] is None
    assert _outcome_count(runtime["history"]) == 0
    assert [saved.id for saved in runtime["monitor"].events()] == [event.id]
    assert event.id in main.store.events


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


def test_finalized_events_evicted_beyond_retention(runtime, monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, finalized_event_retention=1))
    monkeypatch.setattr(main, "_finalized_order", main.OrderedDict())
    first = _event("a")
    second = _event("b")
    _register(runtime, first)
    _register(runtime, second)

    async def scenario():
        for event in (first, second):
            await main.on_quotes(_quotes(event))
            await main.on_state(_state(event, "final"))

    asyncio.run(scenario())

    # Retention of 1: the newest settled game stays resident for review, the
    # older one's heavy buffers are evicted once its positions have settled.
    assert second.id in main.store.events
    assert second.id in main._finalized_order
    assert first.id not in main.store.events
    assert first.id not in main.store.quotes
    assert first.id not in main._finalized_order
    # The permanent idempotency marker survives eviction for both.
    assert {first.id, second.id} <= main._finalized


def test_finalized_event_not_evicted_while_positions_open(runtime, monkeypatch):
    # retention=0 would evict on settle, but an open position must block it
    # (mirrors delete_event: a bot could still be marked against the event).
    monkeypatch.setattr(main, "settings", replace(main.settings, finalized_event_retention=0))
    monkeypatch.setattr(main, "_finalized_order", main.OrderedDict())
    monkeypatch.setattr(runtime["accounts"], "open_count", lambda event_id: 1)
    event = _event()
    _register(runtime, event)

    async def scenario():
        await main.on_quotes(_quotes(event))
        await main.on_state(_state(event, "final"))

    asyncio.run(scenario())

    assert event.id in main.store.events  # guarded from eviction
    assert event.id in main._finalized


def test_sports_status_detail_kept_only_for_tracked_events(runtime, monkeypatch):
    monkeypatch.setattr(main, "_sports_status_compact", main.OrderedDict())
    monkeypatch.setattr(main, "_sports_status_detail", {})
    event = _event()
    _register(runtime, event)

    async def scenario():
        await main.on_sports_status(event.polymarket_slug,
                                    {"status": "in_progress", "score": "1-0", "live": True})
        await main.on_sports_status("untracked-slug", {"status": "in_progress", "live": True})

    asyncio.run(scenario())

    # Only a tracked event's rich payload is retained; every slug gets a compact
    # entry, but untracked slugs never carry the score-rich detail.
    assert set(main._sports_status_detail) == {event.polymarket_slug}
    assert main._sports_status_detail[event.polymarket_slug]["score"] == "1-0"
    assert "untracked-slug" in main._sports_status_compact
    assert "score" not in main._sports_status_compact["untracked-slug"]


def test_sports_status_compact_cache_is_lru_bounded(monkeypatch):
    monkeypatch.setattr(main, "store", Store())
    monkeypatch.setattr(main, "_sports_status_compact", main.OrderedDict())
    monkeypatch.setattr(main, "_sports_status_detail", {})
    monkeypatch.setattr(main, "_SPORTS_STATUS_MAX", 3)

    async def scenario():
        for i in range(6):
            await main.on_sports_status(f"slug-{i}", {"status": "in_progress"})

    asyncio.run(scenario())

    # Oldest entries are evicted; the most-recently-seen slugs survive.
    assert set(main._sports_status_compact) == {"slug-3", "slug-4", "slug-5"}


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
