from datetime import datetime, timezone

from app.mlb_live import game_state_from_linescore, is_mlb_event, match_mlb_game
from app.models import Event


def _event() -> Event:
    return Event(
        id="tracked",
        name="Boston Red Sox at New York Yankees",
        sport="baseball",
        league="MLB",
        home="New York Yankees",
        away="Boston Red Sox",
        game_start="2027-07-28T23:05:00Z",
    )


def _game(game_pk: int, start: str) -> dict:
    return {
        "gamePk": game_pk,
        "gameDate": start,
        "teams": {
            "home": {"team": {"id": 147, "name": "New York Yankees"}},
            "away": {"team": {"id": 111, "name": "Boston Red Sox"}},
        },
    }


def test_mlb_schedule_mapping_uses_start_to_disambiguate_doubleheader():
    event = _event()
    morning = _game(1, "2027-07-28T17:05:00Z")
    evening = _game(2, "2027-07-28T23:05:00Z")

    assert is_mlb_event(event) is True
    assert match_mlb_game(event, [morning, evening])["gamePk"] == 2


def test_linescore_parser_retains_base_out_count_and_personnel_identity():
    event = _event()
    game = _game(824135, "2027-07-28T23:05:00Z")
    state = game_state_from_linescore(
        event,
        game,
        {
            "currentInning": 7,
            "inningState": "Bottom",
            "scheduledInnings": 9,
            "balls": 3,
            "strikes": 2,
            "outs": 1,
            "teams": {"home": {"runs": 4}, "away": {"runs": 3}},
            "offense": {
                "first": {"id": 10, "fullName": "Runner One"},
                "third": {"id": 30, "fullName": "Runner Three"},
                "batter": {"id": 99, "fullName": "Current Batter"},
            },
            "defense": {
                "pitcher": {"id": 55, "fullName": "Current Pitcher"},
            },
        },
        received_at=datetime(2027, 7, 28, 23, 30, tzinfo=timezone.utc),
    )

    assert state is not None
    assert state.period == "Bottom 7"
    assert state.possession == event.home
    assert state.provider_event_id == "824135"
    assert state.sport_state["base_mask"] == 5
    assert state.sport_state["outs"] == 1
    assert state.sport_state["balls"] == 3
    assert state.sport_state["strikes"] == 2
    assert state.sport_state["batter"]["id"] == 99
    assert state.sport_state["pitcher"]["id"] == 55
    assert state.state_schema_version == "mlb-linescore-v1"


def test_linescore_parser_fails_closed_when_half_or_score_is_missing():
    event = _event()
    game = _game(1, "2027-07-28T23:05:00Z")
    assert game_state_from_linescore(
        event,
        game,
        {
            "currentInning": 4,
            "teams": {"home": {"runs": 1}, "away": {"runs": 0}},
        },
    ) is None


def test_linescore_parser_marks_an_official_final_as_terminal():
    event = _event()
    game = _game(824135, "2027-07-28T23:05:00Z")
    game["status"] = {
        "abstractGameState": "Final",
        "detailedState": "Final",
    }

    state = game_state_from_linescore(
        event,
        game,
        {
            "currentInning": 9,
            "inningState": "End",
            "scheduledInnings": 9,
            "teams": {"home": {"runs": 5}, "away": {"runs": 2}},
        },
    )

    assert state is not None
    assert state.status == "final"
    assert state.ended is True
    assert state.live is False
    assert state.sport_state["ended"] is True
    assert state.sport_state["official_game_status"] == "final"
