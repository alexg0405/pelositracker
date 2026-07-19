from datetime import datetime, timedelta, timezone

from app.sources import _game_window, filter_sports_games


def _ev(title, tags, orderbook=True, accepting=True, slug=None):
    return {"title": title, "slug": slug or title.lower().replace(" ", "-"),
            "enableOrderBook": orderbook, "tags": [{"label": t} for t in tags],
            "markets": [{"acceptingOrders": accepting, "gameStartTime": "2026-07-15T18:00:00Z",
                         "clobTokenIds": ["x"]}]}


def test_keeps_tradeable_sports_matchups():
    events = [
        _ev("England vs. Argentina", ["Soccer", "World Cup"]),
        _ev("Lakers vs. Celtics", ["NBA"]),
    ]
    games = filter_sports_games(events)
    assert {g["title"] for g in games} == {"England vs. Argentina", "Lakers vs. Celtics"}
    assert all(g["slug"] and g["game_start"] for g in games)


def test_discovery_marks_events_without_reference_adapters_price_only():
    games = filter_sports_games([
        _ev("Lakers vs. Celtics", ["NBA"]),
        _ev("Player One vs. Player Two", ["Tennis"]),
    ])
    support = {game["title"]: game["reference_adapter"] for game in games}
    assert support == {
        "Lakers vs. Celtics": True,
        "Player One vs. Player Two": False,
    }


def test_drops_futures_submarkets_untradeable_and_nonsports():
    events = [
        _ev("World Cup Winner", ["Soccer"]),                       # future, no "vs"
        _ev("England vs. Argentina - Player Props", ["Soccer"]),   # sub-market
        _ev("Lakers vs. Celtics", ["NBA"], orderbook=False),       # not tradeable
        _ev("Reds vs. Cubs", ["MLB"], accepting=False),            # not accepting orders
        _ev("Trump vs. Biden debate winner", ["Politics"]),        # not sports
        _ev("Who will Alexander Volkanovski fight next?", ["UFC"]), # "vs" inside a name
    ]
    assert filter_sports_games(events) == []


def test_game_window_tags_live_upcoming_and_drops_stale():
    now = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)

    def g(name, start):
        return {"slug": name, "title": name, "game_start": start.isoformat()}

    games = [
        g("started", now - timedelta(hours=1)),     # time alone cannot prove LIVE
        g("soon", now + timedelta(hours=3)),        # starts in 3h -> upcoming
        g("done", now - timedelta(hours=10)),       # finished long ago -> dropped
        g("far", now + timedelta(days=9)),          # >7 days out -> dropped
    ]
    result = _game_window(games, now)
    assert [x["slug"] for x in result] == ["started", "soon"]
    assert result[0]["status"] == "started"
    assert result[0]["status_source"] == "schedule-only"
    assert result[1]["status"] == "upcoming"


def test_game_window_uses_fresh_explicit_status_and_drops_finals():
    now = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)
    games = [
        {"slug": "confirmed", "title": "A vs B", "game_start": (now - timedelta(hours=1)).isoformat()},
        {"slug": "finished", "title": "C vs D", "game_start": (now - timedelta(hours=1)).isoformat()},
        {"slug": "stale", "title": "E vs F", "game_start": (now - timedelta(hours=1)).isoformat()},
    ]
    statuses = {
        "confirmed": {"status": "in_progress", "_received_at": now.isoformat()},
        "finished": {"status": "final", "_received_at": now.isoformat()},
        "stale": {"live": True, "_received_at": (now - timedelta(minutes=4)).isoformat()},
    }
    result = _game_window(games, now, statuses)
    assert [game["slug"] for game in result] == ["confirmed", "stale"]
    assert result[0]["status"] == "live"
    assert result[0]["status_source"] == "polymarket-live-feed"
    assert result[1]["status"] == "started"
