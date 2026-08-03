"""The outcome backfill only writes verified official finals."""
from tools.backfill_event_outcomes import (
    MissingEvent,
    judge_match,
    parse_matchup,
)


def _item(**overrides):
    values = {
        "event_id": "e1",
        "event_name": "Boston Red Sox vs. Athletics",
        "away": "Boston Red Sox",
        "home": "Athletics",
        "last_state_ts": 1785400000.0,
        "last_home_score": 2.0,
        "last_away_score": 2.0,
    }
    values.update(overrides)
    return MissingEvent(**values)


def _game(state="Final", home_score=4.0, away_score=2.0):
    return {
        "status": {"abstractGameState": state},
        "teams": {
            "home": {"score": home_score, "team": {"name": "Athletics"}},
            "away": {"score": away_score, "team": {"name": "Boston Red Sox"}},
        },
    }


def test_parses_the_away_vs_home_convention():
    assert parse_matchup("Boston Red Sox vs. Athletics") == (
        "Boston Red Sox",
        "Athletics",
    )
    assert parse_matchup("malformed") is None


def test_writes_only_verified_finals():
    decision = judge_match(_item(), _game(home_score=2.0, away_score=4.0))
    assert decision["action"] == "write"
    assert decision["final_home_score"] == 2.0
    assert decision["final_away_score"] == 4.0
    assert decision["home"] == "Athletics"


def test_skips_unmatched_live_and_regressing_scores():
    assert judge_match(_item(), None)["action"] == "skip"
    assert judge_match(_item(), _game(state="Live"))["action"] == "skip"
    # An official final below the last observed score means the schedule
    # match resolved to the wrong game; never write it.
    regressed = judge_match(
        _item(last_home_score=5.0), _game(home_score=4.0, away_score=4.0)
    )
    assert regressed["action"] == "skip"
    assert "identity is suspect" in regressed["reason"]
