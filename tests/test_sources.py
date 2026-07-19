import asyncio

from app import sources
from app.models import Event
from app.sources import (_polymarket_token_meta, _quote_from_book, _quote_from_ws_change,
                         canonical_market, extract_polymarket_slug, infer_polymarket_event,
                         odds_api_quotes, odds_api_request)


def event(**overrides):
    values = {
        "name": "Celtics at Knicks",
        "sport": "basketball",
        "home": "New York Knicks",
        "away": "Boston Celtics",
        "odds_api_sport": "basketball_nba",
    }
    values.update(overrides)
    return Event(**values)


def test_v4_request_uses_sport_path_and_query_key(monkeypatch):
    monkeypatch.setenv("ODDS_REGIONS", "us")
    url, params = odds_api_request(event(), "secret")
    assert url == "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
    assert params["apiKey"] == "secret"
    assert params["regions"] == "us"
    assert params["oddsFormat"] == "american"


def test_event_request_uses_event_odds_endpoint():
    url, _ = odds_api_request(event(odds_api_event_id="game-123"), "secret")
    assert url.endswith("/events/game-123/odds")


def test_quotes_filter_matchup_and_keep_line_points():
    target = event()
    payload = [
        {
            "id": "other",
            "home_team": "Other Home",
            "away_team": "Other Away",
            "bookmakers": [{"title": "Wrong Book", "markets": []}],
        },
        {
            "id": "target",
            "home_team": "New York Knicks",
            "away_team": "Boston Celtics",
            "bookmakers": [{
                "title": "Example Book",
                "markets": [
                    {"key": "h2h", "outcomes": [{"name": "New York Knicks", "price": -120}]},
                    {"key": "spreads", "outcomes": [{"name": "Boston Celtics", "price": -110,
                                                       "point": 2.5}]},
                    {"key": "totals", "outcomes": [{"name": "Over", "price": 105,
                                                      "point": 221.5}]},
                ],
            }],
        },
    ]
    quotes = odds_api_quotes(target, payload)
    assert [quote.outcome for quote in quotes] == [
        "New York Knicks", "Boston Celtics +2.5", "Over 221.5"
    ]
    assert all(quote.source == "Example Book" for quote in quotes)
    assert [quote.market for quote in quotes] == ["moneyline", "spread", "total"]


def test_full_mobile_polymarket_link_resolves_to_event_slug():
    assert extract_polymarket_slug(
        "https://polymarket.com/event/nba-nyk-bos-2026?tid=mobile-share"
    ) == "nba-nyk-bos-2026"
    assert extract_polymarket_slug("nba-nyk-bos-2026") == "nba-nyk-bos-2026"


def test_polymarket_metadata_infers_nba_and_matchup():
    inferred = infer_polymarket_event({"title": "Boston Celtics vs. New York Knicks",
                                      "seriesSlug": "nba"})
    assert inferred["sport"] == "basketball"
    assert inferred["odds_api_sport"] == "basketball_nba"
    assert inferred["game_start"] is None
    assert inferred["away"] == "Boston Celtics"
    assert inferred["home"] == "New York Knicks"
    assert canonical_market("h2h") == "moneyline"


def test_polymarket_line_metadata_matches_sportsbook_selection_labels():
    payload = {"markets": [
        {
            "active": True, "closed": False, "enableOrderBook": True,
            "acceptingOrders": True, "sportsMarketType": "spreads", "line": -1.5,
            "question": "Spread: New York Knicks (-1.5)",
            "outcomes": '["New York Knicks", "Boston Celtics"]',
            "clobTokenIds": '["spread-home", "spread-away"]',
        },
        {
            "active": True, "closed": False, "enableOrderBook": True,
            "acceptingOrders": True, "sportsMarketType": "totals", "line": 221.5,
            "question": "Boston Celtics vs. New York Knicks: O/U 221.5",
            "outcomes": '["Over", "Under"]',
            "clobTokenIds": '["total-over", "total-under"]',
        },
    ]}
    meta = _polymarket_token_meta(payload)
    assert meta["spread-home"]["outcome"] == "New York Knicks -1.5"
    assert meta["spread-away"]["outcome"] == "Boston Celtics +1.5"
    assert meta["total-over"]["outcome"] == "Over 221.5"
    assert meta["total-under"]["outcome"] == "Under 221.5"


def test_polymarket_metadata_keeps_provider_game_start():
    inferred = infer_polymarket_event({
        "title": "Boston Celtics vs. New York Knicks", "seriesSlug": "nba",
        "markets": [{"gameStartTime": "2026-07-19T23:20:00Z"}],
    })
    assert inferred["game_start"] == "2026-07-19T23:20:00Z"


def test_gamma_binary_soccer_moneylines_map_only_affirmative_contracts():
    payload = {"markets": [
        {"active": True, "closed": False, "enableOrderBook": True,
         "acceptingOrders": True, "sportsMarketType": "moneyline",
         "question": "Will New York Knicks win?", "groupItemTitle": "New York Knicks",
         "outcomes": '["Yes", "No"]', "clobTokenIds": '["ny-yes", "ny-no"]'},
        {"active": True, "closed": False, "enableOrderBook": True,
         "acceptingOrders": True, "sportsMarketType": "moneyline",
         "question": "Will Boston Celtics vs. New York Knicks end in a draw?",
         "groupItemTitle": "Draw (Boston Celtics vs. New York Knicks)",
         "outcomes": '["Yes", "No"]', "clobTokenIds": '["draw-yes", "draw-no"]'},
    ]}
    meta = _polymarket_token_meta(payload)
    assert (meta["ny-yes"]["market"], meta["ny-yes"]["outcome"]) == (
        "moneyline", "New York Knicks"
    )
    assert (meta["draw-yes"]["market"], meta["draw-yes"]["outcome"]) == (
        "moneyline", "Draw"
    )
    assert meta["ny-no"]["market"] == meta["draw-no"]["market"] == "moneyline condition"
    assert meta["ny-no"]["outcome"] == "Not New York Knicks"
    assert meta["draw-no"]["outcome"] == "Not Draw"


def test_gamma_yes_no_player_props_map_player_stat_line_and_side():
    payload = {"markets": [
        {"active": True, "closed": False, "enableOrderBook": True,
         "acceptingOrders": True, "sportsMarketType": "points", "line": 25.5,
         "question": "Jalen Brunson: Points O/U 25.5",
         "groupItemTitle": "Jalen Brunson: Points O/U 25.5",
         "outcomes": '["Yes", "No"]', "clobTokenIds": '["jb-over", "jb-under"]'},
        {"active": True, "closed": False, "enableOrderBook": True,
         "acceptingOrders": True, "sportsMarketType": "rebounds", "line": 11.5,
         "question": "Karl-Anthony Towns: Rebounds O/U 11.5",
         "groupItemTitle": "Karl-Anthony Towns: Rebounds O/U 11.5",
         "outcomes": '["Yes", "No"]', "clobTokenIds": '["kat-over", "kat-under"]'},
    ]}
    meta = _polymarket_token_meta(payload)
    assert (meta["jb-over"]["market"], meta["jb-over"]["outcome"]) == (
        "Jalen Brunson — points", "Over 25.5"
    )
    assert (meta["jb-under"]["market"], meta["jb-under"]["outcome"]) == (
        "Jalen Brunson — points", "Under 25.5"
    )
    assert (meta["kat-over"]["market"], meta["kat-over"]["outcome"]) == (
        "Karl-Anthony Towns — rebounds", "Over 11.5"
    )


def test_size_less_websocket_delta_never_turns_unknown_depth_into_zero():
    ev = event()
    meta = {"market": "moneyline", "outcome": ev.home, "liquidity": 1000.0,
            "market_slug": "home", "question": "Home win", "accepting_orders": True}
    snapshot = _quote_from_book(ev, "token", meta, {
        "bids": [{"price": "0.48", "size": "20"}],
        "asks": [{"price": "0.52", "size": "30"}],
    })
    delta = _quote_from_ws_change(
        ev, "token", meta, {"event_type": "price_change"},
        {"asset_id": "token", "best_bid": "0.48", "best_ask": "0.52"}, snapshot,
    )
    assert delta.ask_size == 30
    assert delta.liquidity == 50

    unknown = _quote_from_ws_change(
        ev, "other", meta, {"event_type": "best_bid_ask"},
        {"asset_id": "other", "best_bid": "0.48", "best_ask": "0.52"}, None,
    )
    assert unknown.ask_size is None
    assert unknown.liquidity is None


def test_odds_event_resolution_retries_instead_of_disabling_feed(monkeypatch):
    target = event(odds_api_event_id=None)
    attempts = 0
    sleeps = 0
    emitted = []

    async def match(*_args):
        nonlocal attempts
        attempts += 1
        return None if attempts == 1 else {"id": "resolved"}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "resolved", "home_team": target.home, "away_team": target.away,
                "bookmakers": [],
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

    async def sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            raise asyncio.CancelledError

    async def emit(quotes):
        emitted.extend(quotes)

    monkeypatch.setenv("THE_ODDS_API_KEY", "test")
    monkeypatch.setenv("ODDS_POLL_SECONDS", "1")
    monkeypatch.setattr(sources, "match_odds_api_event", match)
    monkeypatch.setattr(sources.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(sources.asyncio, "sleep", sleep)

    try:
        asyncio.run(sources.odds_api_poll(target, emit))
    except asyncio.CancelledError:
        pass
    assert attempts == 2
    assert target.odds_api_event_id == "resolved"
