from app.gameclock import game_progress


def _frac(sport, period, clock, league=""):
    return game_progress(sport, period, clock, league)[1]


def test_regulation_fraction():
    assert _frac("basketball", "Q1", "12:00", "nba") == 1.0
    assert _frac("basketball", "Q4", "06:00", "nba") == 0.125
    assert _frac("basketball", "Q4", "00:00", "nba") == 0.0


def test_overtime_is_not_misrepresented_as_regulation_progress():
    assert _frac("basketball", "OT", "03:00", "nba") is None
    assert _frac("hockey", "2OT", "10:00", "nhl") is None


def test_not_started_without_a_clock_is_unknown():
    assert _frac("basketball", "Not started", "", "nba") is None


def test_unknown_or_unconfigured_upcount_sport_returns_none():
    assert game_progress("soccer", "1H", "30:00") == (None, None)
    assert game_progress("cricket", "1", "10:00") == (None, None)


def test_garbage_clock_is_unknown_without_crashing():
    assert game_progress("basketball", "Q2", "not-a-clock", "nba") == (None, None)


def test_league_identity_selects_the_correct_period_schedule():
    assert _frac("basketball", "Q1", "10:00", "wnba") == 1.0
    assert _frac("basketball", "Q1", "10:00", "nba") < 1.0
    assert _frac("basketball", "H1", "20:00", "ncaab") == 1.0
    assert game_progress("basketball", "Q1", "10:00") == (None, None)


def test_every_known_regulation_fraction_stays_bounded():
    samples = [
        ("basketball", "nba", "Q2", "06:00"),
        ("basketball", "wnba", "Q3", "05:00"),
        ("basketball", "ncaab", "H2", "10:00"),
        ("football", "nfl", "Q4", "02:00"),
        ("hockey", "nhl", "P3", "03:00"),
        ("soccer", "epl", "2H", "75:00"),
    ]
    for sport, league, period, clock in samples:
        fraction = _frac(sport, period, clock, league)
        assert fraction is not None and 0 <= fraction <= 1
