from app.sources import filter_sports_games


def _ev(title, tags, orderbook=True, slug=None):
    return {"title": title, "slug": slug or title.lower().replace(" ", "-"),
            "enableOrderBook": orderbook, "tags": [{"label": t} for t in tags],
            "startDate": "2026-07-15T00:00:00Z", "volume24hr": 100.0}


def test_keeps_tradeable_sports_matchups():
    events = [
        _ev("England vs. Argentina", ["Soccer", "World Cup"]),
        _ev("Lakers vs. Celtics", ["NBA"]),
    ]
    games = filter_sports_games(events)
    titles = {g["title"] for g in games}
    assert titles == {"England vs. Argentina", "Lakers vs. Celtics"}
    assert all(g["slug"] for g in games)


def test_drops_futures_submarkets_untradeable_and_nonsports():
    events = [
        _ev("World Cup Winner", ["Soccer"]),                       # future, no "vs"
        _ev("England vs. Argentina - Player Props", ["Soccer"]),   # sub-market
        _ev("Lakers vs. Celtics", ["NBA"], orderbook=False),       # not tradeable
        _ev("Trump vs. Biden debate winner", ["Politics"]),        # not sports
    ]
    assert filter_sports_games(events) == []
