from datetime import datetime, timedelta, timezone

from app.models import Event, GameState, Quote
from app.store import Store


def test_live_store_keeps_latest_quote_per_engine_selection():
    store = Store()
    event = Event("Away at Home", "basketball", "Home", "Away", id="event")
    store.add_event(event)
    start = datetime.now(timezone.utc)

    for index in range(1_000):
        observed = start + timedelta(milliseconds=index)
        store.add_quotes([Quote(
            event.id,
            "moneyline",
            "Home",
            .50,
            "Polymarket",
            observed_at=observed,
            bid=.49,
            ask=.50 + index / 1_000_000,
            token_id="home-token",
            depth_complete=True,
            bid_levels=tuple((.49 - level / 10_000, 10.0) for level in range(50)),
            ask_levels=tuple((.50 + level / 10_000, 10.0) for level in range(50)),
        )])

    live = store.quote_values(event.id)
    assert len(live) == 1
    assert live[0].observed_at == start + timedelta(milliseconds=999)
    assert store.quote_updates[event.id] == 1_000


def test_live_store_ignores_invalid_or_late_updates_and_removed_event_writes():
    store = Store()
    event = Event("Away at Home", "basketball", "Home", "Away", id="event")
    store.add_event(event)
    now = datetime.now(timezone.utc)
    good = Quote(
        event.id, "moneyline", "Home", .50, "Polymarket",
        observed_at=now, bid=.49, ask=.50, token_id="home-token",
    )
    invalid = Quote(
        event.id, "moneyline", "Home", .50, "Polymarket",
        observed_at=now + timedelta(seconds=1), bid=.49, ask=1.0,
        token_id="home-token",
    )
    late = Quote(
        event.id, "moneyline", "Home", .40, "Polymarket",
        observed_at=now - timedelta(seconds=1), bid=.39, ask=.40,
        token_id="home-token",
    )
    store.add_quotes([good, invalid, late])

    assert store.quote_values(event.id) == [good]
    store.remove_event(event.id)
    store.add_quotes([good])
    store.add_state(GameState(event.id, 1, 0, "1", "10:00", "fixture"))

    assert event.id not in store.quotes
    assert event.id not in store.states
    assert event.id not in store.quote_updates
    assert event.id not in store.state_updates


def test_live_state_history_is_bounded_but_update_count_is_preserved():
    store = Store()
    event = Event("Away at Home", "basketball", "Home", "Away", id="event")
    store.add_event(event)
    for index in range(200):
        store.add_state(GameState(
            event.id, index, 0, "1", "10:00", "fixture",
        ))

    assert len(store.states[event.id]) == 64
    assert store.state_updates[event.id] == 200
