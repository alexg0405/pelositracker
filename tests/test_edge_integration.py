from datetime import datetime, timezone

from app import actionnetwork
from app.advice import market_views
from app.engine import SignalEngine
from app.models import Event
from app.pinnacle import _parse_pinnacle_quotes
from app.sources import _polymarket_token_meta, _quote_from_book, odds_api_quotes


NOW = datetime.now(timezone.utc)


_SignalEngine = SignalEngine


class SignalEngine(_SignalEngine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_fixture_policies = True
        self.calibrated_markets = {
            "moneyline", "spread", "total",
            "jalen brunson — points", "karl-anthony towns — rebounds",
        }


def event():
    return Event("Dodgers vs Yankees", "baseball", "New York Yankees", "Los Angeles Dodgers")


def book(bid, ask):
    return {
        "timestamp": str(int(NOW.timestamp() * 1000)),
        "bids": [{"price": str(bid), "size": "200"}],
        "asks": [{"price": str(ask), "size": "1000"}],
    }


def reference_quotes(ev):
    actionnetwork._book_map.clear()
    actionnetwork._book_map[1] = "Bookmaker"
    action = actionnetwork.parse_action_quotes(ev, {"last_updated": NOW.isoformat(), "odds": [{
        "type": "game", "book_id": 1,
        "ml_home": -140, "ml_away": 120,
        "spread_home": -1.5, "spread_home_line": -140,
        "spread_away": 1.5, "spread_away_line": 120,
        "total": 8.5, "over": -135, "under": 115,
    }]})
    pinnacle = _parse_pinnacle_quotes(ev, {"updatedAt": NOW.isoformat()}, [
        {"type": "spread", "key": "s;0;m", "prices": [
            {"designation": "home", "price": -145, "points": -1.5},
            {"designation": "away", "price": 125, "points": 1.5},
        ]},
        {"type": "total", "key": "s;0;m", "prices": [
            {"designation": "over", "price": -140, "points": 8.5},
            {"designation": "under", "price": 120, "points": 8.5},
        ]},
    ])
    return action + pinnacle


def polymarket_quotes(ev):
    metadata = _polymarket_token_meta({"markets": [
        {
            "active": True, "closed": False, "enableOrderBook": True, "feesEnabled": False,
            "acceptingOrders": True, "sportsMarketType": "spreads", "line": -1.5,
            "question": "Spread: New York Yankees (-1.5)",
            "outcomes": '["New York Yankees", "Los Angeles Dodgers"]',
            "clobTokenIds": '["poly-spread-home", "poly-spread-away"]',
        },
        {
            "active": True, "closed": False, "enableOrderBook": True, "feesEnabled": False,
            "acceptingOrders": True, "sportsMarketType": "totals", "line": 8.5,
            "question": "Los Angeles Dodgers vs. New York Yankees: O/U 8.5",
            "outcomes": '["Over", "Under"]',
            "clobTokenIds": '["poly-over", "poly-under"]',
        },
    ]})
    prices = {
        "poly-spread-home": (.48, .50), "poly-spread-away": (.49, .51),
        "poly-over": (.47, .49), "poly-under": (.50, .52),
    }
    return [_quote_from_book(ev, token, meta, book(*prices[token]))
            for token, meta in metadata.items()]


def test_cross_provider_spread_and_total_edges_reach_actionable_cards():
    ev = event()
    poly = polymarket_quotes(ev)
    quotes = poly + reference_quotes(ev)
    signals = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0).evaluate(
        ev.id, quotes, [], away_outcome=ev.away, home_outcome=ev.home, sport=ev.sport,
        as_of=NOW
    )

    spread = next(signal for signal in signals if signal.outcome == "New York Yankees -1.5")
    total = next(signal for signal in signals if signal.outcome == "Over 8.5")
    assert spread.quote_source == total.quote_source == "Polymarket"
    assert spread.n_reference_sources == total.n_reference_sources == 2
    assert spread.edge > .02 and total.edge > .02
    assert spread.action == total.action == "PAPER_BET"

    views = {view["token_id"]: view for view in market_views(poly, signals, 0)}
    assert views["poly-spread-home"]["entry_action"] == "ENTRY WINDOW"
    assert views["poly-over"]["entry_action"] == "ENTRY WINDOW"


def test_polymarket_alternate_lines_keep_distinct_selection_identities():
    meta = _polymarket_token_meta({"markets": [
        {"active": True, "closed": False, "enableOrderBook": True, "acceptingOrders": True, "feesEnabled": False,
         "sportsMarketType": "totals", "line": 7.5, "question": "O/U 7.5",
         "outcomes": '["Over", "Under"]', "clobTokenIds": '["o75", "u75"]'},
        {"active": True, "closed": False, "enableOrderBook": True, "acceptingOrders": True, "feesEnabled": False,
         "sportsMarketType": "totals", "line": 8.5, "question": "O/U 8.5",
         "outcomes": '["Over", "Under"]', "clobTokenIds": '["o85", "u85"]'},
    ]})
    assert {item["outcome"] for item in meta.values()} == {
        "Over 7.5", "Under 7.5", "Over 8.5", "Under 8.5"
    }


def test_gamma_three_condition_soccer_moneyline_joins_complete_three_way_books():
    ev = Event("AFC Bournemouth vs Arsenal FC", "soccer", "Arsenal FC", "AFC Bournemouth")
    metadata = _polymarket_token_meta({"markets": [
        {"active": True, "closed": False, "enableOrderBook": True, "feesEnabled": False,
         "acceptingOrders": True, "sportsMarketType": "moneyline",
         "question": "Will AFC Bournemouth win?", "groupItemTitle": "AFC Bournemouth",
         "outcomes": '["Yes", "No"]', "clobTokenIds": '["bou-yes", "bou-no"]'},
        {"active": True, "closed": False, "enableOrderBook": True, "feesEnabled": False,
         "acceptingOrders": True, "sportsMarketType": "moneyline",
         "question": "Will AFC Bournemouth vs. Arsenal FC end in a draw?",
         "groupItemTitle": "Draw (AFC Bournemouth vs. Arsenal FC)",
         "outcomes": '["Yes", "No"]', "clobTokenIds": '["draw-yes", "draw-no"]'},
        {"active": True, "closed": False, "enableOrderBook": True, "feesEnabled": False,
         "acceptingOrders": True, "sportsMarketType": "moneyline",
         "question": "Will Arsenal FC win?", "groupItemTitle": "Arsenal FC",
         "outcomes": '["Yes", "No"]', "clobTokenIds": '["ars-yes", "ars-no"]'},
    ]})
    prices = {
        "bou-yes": (.18, .20), "bou-no": (.78, .80),
        "draw-yes": (.18, .20), "draw-no": (.78, .80),
        "ars-yes": (.43, .45), "ars-no": (.53, .55),
    }
    poly = [_quote_from_book(ev, token, meta, book(*prices[token]))
            for token, meta in metadata.items()]

    actionnetwork._book_map.clear()
    actionnetwork._book_map[1] = "Bookmaker"
    action = actionnetwork.parse_action_quotes(ev, {"last_updated": NOW.isoformat(), "odds": [{
        "type": "game", "book_id": 1,
        "ml_home": -125, "ml_away": 300, "ml_draw": 300,
    }]})
    pinnacle = _parse_pinnacle_quotes(ev, {"updatedAt": NOW.isoformat()}, [{
        "type": "moneyline", "key": "s;0;m", "prices": [
            {"designation": "home", "price": -120},
            {"designation": "away", "price": 310},
            {"designation": "draw", "price": 310},
        ],
    }])
    signals = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0).evaluate(
        ev.id, poly + action + pinnacle, [], away_outcome=ev.away, home_outcome=ev.home,
        as_of=NOW
    )

    affirmative = [signal for signal in signals
                   if signal.market == "moneyline" and signal.quote_source == "Polymarket"]
    assert {signal.outcome for signal in affirmative} == {ev.home, ev.away, "Draw"}
    assert all(signal.n_reference_sources == 2 for signal in affirmative)
    assert all(signal.edge > 0 and signal.action == "PAPER_BET" for signal in affirmative)
    negative = [signal for signal in signals if signal.market == "moneyline condition"]
    assert {signal.outcome for signal in negative} == {
        f"Not {ev.home}", f"Not {ev.away}", "Not Draw"
    }
    assert all(signal.n_reference_sources == 0 and signal.action == "WATCH"
               for signal in negative)


def test_gamma_player_props_join_odds_api_without_cross_player_or_line_collapse():
    ev = Event("Celtics vs Knicks", "basketball", "New York Knicks", "Boston Celtics",
               odds_api_event_id="game")
    metadata = _polymarket_token_meta({"markets": [
        {"active": True, "closed": False, "enableOrderBook": True, "feesEnabled": False,
         "acceptingOrders": True, "sportsMarketType": "points", "line": 25.5,
         "question": "Jalen Brunson: Points O/U 25.5",
         "groupItemTitle": "Jalen Brunson: Points O/U 25.5",
         "outcomes": '["Yes", "No"]', "clobTokenIds": '["jb-o", "jb-u"]'},
        {"active": True, "closed": False, "enableOrderBook": True, "feesEnabled": False,
         "acceptingOrders": True, "sportsMarketType": "rebounds", "line": 11.5,
         "question": "Karl-Anthony Towns: Rebounds O/U 11.5",
         "groupItemTitle": "Karl-Anthony Towns: Rebounds O/U 11.5",
         "outcomes": '["Yes", "No"]', "clobTokenIds": '["kat-o", "kat-u"]'},
    ]})
    prices = {"jb-o": (.43, .45), "jb-u": (.53, .55),
              "kat-o": (.42, .44), "kat-u": (.54, .56)}
    poly = [_quote_from_book(ev, token, meta, book(*prices[token]))
            for token, meta in metadata.items()]
    markets = [
        {"key": "player_points", "outcomes": [
            {"name": "Over", "description": "Jalen Brunson", "point": 25.5, "price": -120},
            {"name": "Under", "description": "Jalen Brunson", "point": 25.5, "price": 100},
        ]},
        {"key": "player_rebounds", "outcomes": [
            {"name": "Over", "description": "Karl-Anthony Towns", "point": 11.5,
             "price": -125},
            {"name": "Under", "description": "Karl-Anthony Towns", "point": 11.5,
             "price": 105},
        ]},
    ]
    references = odds_api_quotes(ev, {
        "id": "game", "home_team": ev.home, "away_team": ev.away,
        "bookmakers": [
            {"key": "pinnacle", "title": "Pinnacle", "last_update": NOW.isoformat(),
             "markets": markets},
            {"key": "bookmaker", "title": "Bookmaker", "last_update": NOW.isoformat(),
             "markets": markets},
        ],
    })
    signals = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0).evaluate(
        ev.id, poly + references, [], away_outcome=ev.away, home_outcome=ev.home,
        as_of=NOW
    )
    matched = [signal for signal in signals if signal.quote_source == "Polymarket"
               and signal.n_reference_sources]
    assert {(signal.market, signal.outcome) for signal in matched} == {
        ("Jalen Brunson — points", "Over 25.5"),
        ("Jalen Brunson — points", "Under 25.5"),
        ("Karl-Anthony Towns — rebounds", "Over 11.5"),
        ("Karl-Anthony Towns — rebounds", "Under 11.5"),
    }
    assert all(signal.n_reference_sources == 2 for signal in matched)
