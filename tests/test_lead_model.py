"""Tests for the lead/clock in-play win-probability model."""
from __future__ import annotations

import pytest

from app.lead_model import (
    LEAD_SPORT_PARAMS,
    live_win_probability,
    pregame_margin_from_price,
    score_swing,
    win_probability_band,
)

NBA_SIGMA = LEAD_SPORT_PARAMS["nba"].sigma


def test_even_game_at_tipoff_is_a_coin_flip():
    assert live_win_probability(0, 0.0, 1.0, NBA_SIGMA) == pytest.approx(0.5)


def test_end_of_game_is_decided_by_the_lead():
    assert live_win_probability(1, 0.0, 0.0, NBA_SIGMA) == 1.0
    assert live_win_probability(-1, 0.0, 0.0, NBA_SIGMA) == 0.0
    assert live_win_probability(0, 0.0, 0.0, NBA_SIGMA) == 0.5


def test_leading_and_being_favored_raise_win_probability():
    base = live_win_probability(0, 0.0, 0.5, NBA_SIGMA)
    leading = live_win_probability(6, 0.0, 0.5, NBA_SIGMA)
    favored = live_win_probability(0, 6.0, 0.5, NBA_SIGMA)
    assert leading > base == pytest.approx(0.5)
    assert favored > base


@pytest.mark.parametrize("p0", [0.35, 0.5, 0.62, 0.8])
def test_pregame_price_anchor_round_trips(p0):
    margin = pregame_margin_from_price(p0, NBA_SIGMA)
    assert live_win_probability(0, margin, 1.0, NBA_SIGMA) == pytest.approx(p0, abs=1e-6)


def test_band_is_ordered_and_collapses_at_the_final_whistle():
    low, mid, high = win_probability_band(0.6, 4, 0.5, NBA_SIGMA)
    assert 0.0 < low <= mid <= high < 1.0
    # With no time left a positive lead is a certain win, so the band collapses.
    assert win_probability_band(0.6, 4, 0.0, NBA_SIGMA) == (1.0, 1.0, 1.0)


def test_score_swing_is_larger_late_in_a_close_game():
    early = score_swing(0.5, 0, 0.5, NBA_SIGMA, 3.0)
    late = score_swing(0.5, 0, 0.02, NBA_SIGMA, 3.0)
    assert 0.0 < early < late


def test_score_swing_vanishes_in_a_late_blowout():
    assert score_swing(0.5, 40, 0.02, NBA_SIGMA, 3.0) == pytest.approx(0.0, abs=1e-9)


def test_hockey_is_far_more_lead_sensitive_than_basketball():
    # A 2-goal hockey lead at the half is worth much more than a 2-point NBA lead.
    nhl = LEAD_SPORT_PARAMS["nhl"].sigma
    hockey = live_win_probability(2, 0.0, 0.5, nhl)
    hoops = live_win_probability(2, 0.0, 0.5, NBA_SIGMA)
    assert hockey > hoops


def test_supported_leagues_present():
    assert {"nba", "wnba", "ncaab", "nfl", "ncaaf", "nhl"} <= set(LEAD_SPORT_PARAMS)
