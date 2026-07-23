import asyncio

import pytest

from app import actionnetwork
from app.actionnetwork import (_action_network_once, match_game, parse_action_quotes,
                               scoreboard_sport)
from app.matching import closest_start, team_match_score
from app.models import Event
from app.pinnacle import _match_pinnacle_game, _parse_pinnacle_quotes


def event(start="2026-07-19T23:20:00Z"):
    return Event("Dodgers vs Yankees", "baseball", "New York Yankees",
                 "Los Angeles Dodgers", game_start=start)


def test_no_start_time_rejects_an_ambiguous_rematch():
    options = [{"id": "g1", "start": None}, {"id": "g2", "start": None}]
    # Two same-name candidates and no tracked start: cannot disambiguate -> refuse.
    assert closest_start(options, None, lambda o: o["start"]) is None
    # A lone candidate with no start is still safe to accept.
    assert closest_start(options[:1], None, lambda o: o["start"]) == options[0]


def test_action_network_doubleheader_matches_by_start_time():
    games = [
        {"id": "g1", "start_time": "2026-07-19T16:35:00Z",
         "teams": [{"display_name": "Dodgers"}, {"display_name": "Yankees"}]},
        {"id": "g2", "start_time": "2026-07-19T23:20:00Z",
         "teams": [{"display_name": "Dodgers"}, {"display_name": "Yankees"}]},
    ]
    assert match_game(event(), games)["id"] == "g2"


def test_pinnacle_doubleheader_matches_by_start_time():
    matchups = [
        {"id": "g1", "type": "matchup", "startTime": "2026-07-19T16:35:00Z",
         "participants": [{"name": "G1 New York Yankees"}, {"name": "G1 Los Angeles Dodgers"}]},
        {"id": "g2", "type": "matchup", "startTime": "2026-07-19T23:20:00Z",
         "participants": [{"name": "G2 New York Yankees"}, {"name": "G2 Los Angeles Dodgers"}]},
    ]
    assert _match_pinnacle_game(event(), matchups)["id"] == "g2"


def test_ambiguous_match_is_rejected_when_start_is_too_far_away():
    games = [
        {"id": "wrong", "start_time": "2026-07-19T16:35:00Z",
         "teams": [{"display_name": "Dodgers"}, {"display_name": "Yankees"}]},
    ]
    assert match_game(event(), games) is None


def test_action_network_uses_real_ufc_scoreboard_slug():
    assert scoreboard_sport("mma_mixed_martial_arts") == "ufc"


@pytest.mark.parametrize(("full_name", "abbreviation"), [
    ("Los Angeles Dodgers", "LA Dodgers"),
    ("Philadelphia 76ers", "Philly Sixers"),
    ("D.C. United", "DC United"),
    ("Paris Saint Germain", "PSG"),
])
def test_common_team_abbreviations_are_normalized(full_name, abbreviation):
    assert team_match_score(full_name, abbreviation) is not None


def soccer_event():
    return Event("Manchester City vs Manchester United", "soccer",
                 "Manchester United", "Manchester City",
                 odds_api_sport="soccer_epl", game_start="2026-07-19T19:00:00Z")


def test_common_suffix_soccer_collision_is_rejected_and_abbreviation_matches():
    wrong_action = {
        "id": "wrong", "start_time": "2026-07-19T19:00:00Z",
        "teams": [{"display_name": "Leeds United"}, {"display_name": "Leicester City"}],
    }
    right_action = {
        "id": "right", "start_time": "2026-07-19T19:00:00Z",
        "teams": [{"display_name": "Man City"}, {"display_name": "Man Utd"}],
    }
    assert match_game(soccer_event(), [wrong_action]) is None
    assert match_game(soccer_event(), [wrong_action, right_action])["id"] == "right"

    wrong_pinnacle = {
        "id": "wrong", "type": "matchup", "startTime": "2026-07-19T19:00:00Z",
        "participants": [{"name": "Leeds United"}, {"name": "Leicester City"}],
    }
    right_pinnacle = {
        "id": "right", "type": "matchup", "startTime": "2026-07-19T19:00:00Z",
        "participants": [{"name": "Man City"}, {"name": "Man Utd"}],
    }
    assert _match_pinnacle_game(soccer_event(), [wrong_pinnacle]) is None
    assert _match_pinnacle_game(soccer_event(), [wrong_pinnacle, right_pinnacle])["id"] == "right"


def test_three_way_moneyline_adapters_keep_the_draw_leg():
    actionnetwork._book_map.clear()
    actionnetwork._book_map[1] = "Bookmaker"
    try:
        action_quotes = parse_action_quotes(soccer_event(), {"odds": [{
            "type": "game", "book_id": 1,
            "ml_home": -110, "ml_away": 260, "ml_draw": 240,
        }]})
    finally:
        actionnetwork._book_map.clear()
    pinnacle_quotes = _parse_pinnacle_quotes(soccer_event(), {}, [{
        "type": "moneyline", "key": "s;0;m", "prices": [
            {"designation": "home", "price": -110},
            {"designation": "away", "price": 260},
            {"designation": "draw", "price": 240},
        ],
    }])
    assert {quote.outcome for quote in action_quotes} == {
        "Manchester United", "Manchester City", "Draw"
    }
    assert {quote.outcome for quote in pinnacle_quotes} == {
        "Manchester United", "Manchester City", "Draw"
    }
    draw = next(quote for quote in pinnacle_quotes if quote.outcome == "Draw")
    assert draw.probability == pytest.approx(100 / 340)


def test_action_book_metadata_is_retried_after_a_transient_failure():
    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class Client:
        def __init__(self):
            self.book_attempts = 0
            self.scoreboard_attempts = 0

        async def get(self, url, **_kwargs):
            if url.endswith("/books"):
                self.book_attempts += 1
                if self.book_attempts == 1:
                    return Response(503, {})
                return Response(200, {"books": [{"id": 1, "display_name": "Bookmaker"}]})
            self.scoreboard_attempts += 1
            return Response(200, {"games": [{
                "id": "target", "start_time": "2026-07-19T23:20:00Z",
                "teams": [{"display_name": "Dodgers"}, {"display_name": "Yankees"}],
                "odds": [{"type": "game", "book_id": 1,
                          "ml_home": -120, "ml_away": 110}],
            }]})

    async def exercise():
        emitted = []

        async def emit(quotes):
            emitted.extend(quotes)

        client = Client()
        await _action_network_once(event(), client, emit)
        assert emitted == []
        await _action_network_once(event(), client, emit)
        return client, emitted

    actionnetwork._book_map.clear()
    try:
        client, emitted = asyncio.run(exercise())
    finally:
        actionnetwork._book_map.clear()
    assert client.book_attempts == 2
    assert client.scoreboard_attempts == 1
    assert {quote.outcome for quote in emitted} == {"New York Yankees", "Los Angeles Dodgers"}
