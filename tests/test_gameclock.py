from app.gameclock import game_progress


def _frac(sport, period, clock):
    return game_progress(sport, period, clock)[1]


def test_regulation_fraction():
    assert _frac("basketball", "Q1", "12:00") == 1.0
    assert _frac("basketball", "Q4", "06:00") == 0.125
    assert _frac("basketball", "Q4", "00:00") == 0.0


def test_overtime_blank_clock_is_near_zero():
    # OT with an unparseable clock must NOT report a full period remaining.
    assert _frac("basketball", "OT", "") == 0.0
    assert _frac("hockey", "2OT", "") == 0.0


def test_not_started_is_not_overtime():
    # "Not started" contains the substring "ot" but is not overtime.
    assert _frac("basketball", "Not started", "") == 1.0


def test_unknown_or_upcount_sport_returns_none():
    assert game_progress("soccer", "1H", "30:00") == (None, None)
    assert game_progress("cricket", "1", "10:00") == (None, None)


def test_garbage_clock_falls_back_without_crashing():
    seconds, fraction = game_progress("basketball", "Q2", "not-a-clock")
    assert 0.0 <= fraction <= 1.0
