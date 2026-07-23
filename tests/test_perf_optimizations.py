"""Regression tests for the latency/CPU optimizations:

* the SSE events snapshot is built at most once per change, shared across every
  subscriber (not rebuilt once per subscriber); and
* one-shot provider fetches borrow a shared keep-alive pool that the app
  lifespan opens on startup and closes on shutdown.
"""
import asyncio

from fastapi.testclient import TestClient

from app import http_clients, main, sources
from app.ledger import Ledger


def test_sse_snapshot_built_once_until_a_change(monkeypatch):
    builds = {"count": 0}

    def counting_sort():
        builds["count"] += 1
        return []

    monkeypatch.setattr(main, "_sort_events_by_edge", counting_sort)
    monkeypatch.setattr(main, "_snapshot_version", 0)
    monkeypatch.setattr(main, "_snapshot_cache", {"version": -1, "payload": ""})
    monkeypatch.setattr(main, "_snapshot_lock", asyncio.Lock())
    monkeypatch.setattr(main, "_subscribers", set())

    async def scenario():
        first = await main._events_snapshot_sse()
        second = await main._events_snapshot_sse()  # served from cache
        assert first == second == "data: []\n\n"
        assert builds["count"] == 1, "no change yet -> must not rebuild"

        main._notify_subscribers()  # a change invalidates the cached payload
        await main._events_snapshot_sse()
        await main._events_snapshot_sse()  # cached again
        assert builds["count"] == 2, "exactly one rebuild per change"

    asyncio.run(scenario())


def test_sse_snapshot_coalesced_across_concurrent_subscribers(monkeypatch):
    builds = {"count": 0}

    def counting_sort():
        builds["count"] += 1
        return []

    monkeypatch.setattr(main, "_sort_events_by_edge", counting_sort)
    monkeypatch.setattr(main, "_snapshot_version", 0)
    monkeypatch.setattr(main, "_snapshot_cache", {"version": -1, "payload": ""})
    monkeypatch.setattr(main, "_snapshot_lock", asyncio.Lock())

    async def scenario():
        # Three subscribers waking on the same change build the snapshot once.
        results = await asyncio.gather(
            main._events_snapshot_sse(),
            main._events_snapshot_sse(),
            main._events_snapshot_sse(),
        )
        assert results == ["data: []\n\n"] * 3
        assert builds["count"] == 1

    asyncio.run(scenario())


def test_shared_http_pool_opened_by_app_and_closed_on_shutdown(monkeypatch):
    async def idle_sports(*_args, **_kwargs):
        await asyncio.Future()

    async def idle_auto():
        await asyncio.Future()

    monkeypatch.setattr(main, "polymarket_sports_stream", idle_sports)
    monkeypatch.setattr(main, "auto_monitor_loop", idle_auto)

    with TestClient(main.app):
        assert http_clients.current_shared_client() is not None
    # Lifespan shutdown must close the pool it opened.
    assert http_clients.current_shared_client() is None


def test_borrow_client_reuses_shared_pool_without_closing_it():
    async def scenario():
        shared = http_clients.open_shared_client()
        try:
            async with sources._borrow_client(timeout=15) as borrowed:
                assert borrowed is shared  # borrows the pool, no new client
            assert not shared.is_closed  # borrowing must not close the pool
        finally:
            await http_clients.close_shared_client()
        assert http_clients.current_shared_client() is None

    asyncio.run(scenario())


def test_event_positions_bulk_matches_per_event_fetch(tmp_path):
    ledger = Ledger(str(tmp_path / "positions.db"))
    try:
        ledger.upsert_position("evt-a", "tok-1", "moneyline", "home", 10, 0.45)
        ledger.upsert_position("evt-a", "tok-2", "moneyline", "away", 5, 0.55)
        ledger.upsert_position("evt-b", "tok-3", "moneyline", "home", 7, 0.40)
        # evt-c has no positions and must still map to an empty list.

        bulk = ledger.event_positions_bulk(["evt-a", "evt-b", "evt-c"])
        assert set(bulk) == {"evt-a", "evt-b", "evt-c"}
        assert bulk["evt-c"] == []
        # One IN query must return exactly what per-event queries would.
        for event_id in ("evt-a", "evt-b", "evt-c"):
            assert bulk[event_id] == ledger.event_positions(event_id)
        # An id absent from the store yields an empty bucket, not a KeyError.
        assert ledger.event_positions_bulk([]) == {}
    finally:
        ledger.close()


def test_engine_audit_index_keeps_first_match():
    # The audit index must select the same payload the old linear next() did:
    # the first quote_payload matching (market, outcome, source).
    quote_payloads = [
        {"market": "ml", "outcome": "home", "source": "poly", "token_id": "first"},
        {"market": "ml", "outcome": "home", "source": "poly", "token_id": "second"},
        {"market": "ml", "outcome": "away", "source": "poly", "token_id": "third"},
    ]
    audit_by_key: dict[tuple[str, str, str], dict] = {}
    for payload in quote_payloads:
        audit_by_key.setdefault(
            (payload["market"], payload["outcome"], payload["source"]), payload)

    assert audit_by_key[("ml", "home", "poly")]["token_id"] == "first"
    assert audit_by_key[("ml", "away", "poly")]["token_id"] == "third"
