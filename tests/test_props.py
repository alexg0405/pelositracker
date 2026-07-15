from app.models import Event
from app.sources import is_player_prop, odds_api_quotes, odds_api_request


def _event(**overrides):
    values = {"name": "Foxes at Hawks", "sport": "basketball_nba",
              "home": "Hawks", "away": "Foxes", "odds_api_sport": "basketball_nba"}
    values.update(overrides)
    return Event(**values)


def test_is_player_prop_excludes_alternates_and_non_props():
    assert is_player_prop("player_points")
    assert is_player_prop("batter_home_runs")
    assert not is_player_prop("player_points_alternate")
    assert not is_player_prop("h2h")
    assert not is_player_prop("totals")


def test_player_props_group_per_player_and_line():
    ev = _event(odds_api_event_id="g1")
    payload = {
        "id": "g1", "home_team": "Hawks", "away_team": "Foxes",
        "bookmakers": [{"title": "BookOne", "markets": [{"key": "player_points", "outcomes": [
            {"name": "Over", "description": "LeBron James", "point": 24.5, "price": -110},
            {"name": "Under", "description": "LeBron James", "point": 24.5, "price": -110},
            {"name": "Over", "description": "Anthony Davis", "point": 19.5, "price": 105},
            {"name": "Under", "description": "Anthony Davis", "point": 19.5, "price": -125},
        ]}]}],
    }
    quotes = odds_api_quotes(ev, payload)
    markets = {q.market for q in quotes}
    assert markets == {"LeBron James — points", "Anthony Davis — points"}
    lebron = {q.outcome for q in quotes if q.market == "LeBron James — points"}
    assert lebron == {"Over 24.5", "Under 24.5"}  # a clean 2-way market to de-vig


def test_prop_markets_only_requested_with_event_id(monkeypatch):
    monkeypatch.setenv("ODDS_PLAYER_MARKETS", "player_points,player_rebounds")
    _, with_id = odds_api_request(_event(odds_api_event_id="g1"), "key")
    assert "player_points" in with_id["markets"]
    _, without_id = odds_api_request(_event(), "key")  # bulk endpoint: no props
    assert "player_points" not in without_id["markets"]
