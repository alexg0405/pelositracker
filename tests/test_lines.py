from datetime import datetime, timezone

from app.lines import pregame_priors, quote_line_side
from app.models import Quote

NOW = datetime.now(timezone.utc)


def test_spread_side_and_point():
    # Away team favored by the label sign; side resolved via team names.
    assert quote_line_side("spreads", "Boston Celtics +2.5",
                           "New York Knicks", "Boston Celtics") == (2.5, "away")
    assert quote_line_side("spreads", "New York Knicks -6",
                           "New York Knicks", "Boston Celtics") == (-6.0, "home")
    # Generic home/away labels also resolve.
    assert quote_line_side("spreads", "home -3.5", "H", "A") == (-3.5, "home")


def test_total_side_and_point():
    assert quote_line_side("totals", "Over 221.5", "H", "A") == (221.5, "over")
    assert quote_line_side("totals", "Under 221.5", "H", "A") == (221.5, "under")


def test_moneyline_and_unknown_have_no_line():
    assert quote_line_side("h2h", "Boston Celtics", "NYK", "Boston Celtics") == (None, None)
    assert quote_line_side("spreads", "Mystery Team +2.5", "H", "A") == (2.5, None)


def test_pregame_priors_extracts_home_spread_and_total():
    quotes = [
        Quote("e", "spreads", "Knicks -6", 0.5, "Pinnacle", NOW),
        Quote("e", "spreads", "Celtics +6", 0.5, "Pinnacle", NOW),
        Quote("e", "totals", "Over 221.5", 0.5, "Pinnacle", NOW),
        Quote("e", "totals", "Under 221.5", 0.5, "Pinnacle", NOW),
    ]
    spread, total = pregame_priors(quotes, "Knicks", "Celtics")
    assert spread == -6.0      # home point -> expected home margin = +6
    assert total == 221.5
