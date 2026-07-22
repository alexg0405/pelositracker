"""Tests for the soccer (Poisson) in-play result model."""
from __future__ import annotations

import pytest

from app.soccer_model import (
    prematch_rates,
    result_band,
    result_probabilities,
    result_swing,
)


def test_probabilities_sum_to_one():
    p_home, p_draw, p_away = result_probabilities(1.4, 1.1, 0, 0, 1.0)
    assert p_home + p_draw + p_away == pytest.approx(1.0, abs=1e-6)


def test_equal_rates_are_symmetric():
    p_home, _, p_away = result_probabilities(1.3, 1.3, 0, 0, 1.0)
    assert p_home == pytest.approx(p_away, abs=1e-9)


def test_a_live_lead_raises_the_win_probability():
    level = result_probabilities(1.3, 1.3, 0, 0, 0.5)[0]
    leading = result_probabilities(1.3, 1.3, 1, 0, 0.5)
    assert leading[0] > level
    assert leading[0] > leading[2]


def test_full_time_result_is_decided_by_the_score():
    assert result_probabilities(1.3, 1.3, 2, 1, 0.0) == (1.0, 0.0, 0.0)
    assert result_probabilities(1.3, 1.3, 1, 1, 0.0) == (0.0, 1.0, 0.0)


def test_prematch_rates_round_trip():
    p_home, p_draw, _ = result_probabilities(1.6, 1.1, 0, 0, 1.0)
    rates = prematch_rates(p_home, p_draw)
    assert rates is not None
    lam_home, lam_away = rates
    assert lam_home == pytest.approx(1.6, abs=0.12)
    assert lam_away == pytest.approx(1.1, abs=0.12)
    # Re-derived pre-match probabilities match the inputs.
    home, draw, _ = result_probabilities(lam_home, lam_away, 0, 0, 1.0)
    assert home == pytest.approx(p_home, abs=0.02)
    assert draw == pytest.approx(p_draw, abs=0.02)


def test_prematch_rates_rejects_impossible_priors():
    assert prematch_rates(0.6, 0.5) is None   # home + draw >= 1
    assert prematch_rates(0.0, 0.3) is None    # degenerate home prob


def test_band_is_ordered_and_collapses_at_full_time():
    low, mid, high = result_band(1.6, 1.1, 1, 1, 0.5, "home")
    assert 0.0 < low <= mid <= high < 1.0
    # Home leads with no time left: certain win, band collapses.
    assert result_band(1.6, 1.1, 2, 0, 0.0, "home") == (1.0, 1.0, 1.0)


def test_swing_is_positive_mid_match_and_zero_at_full_time():
    assert result_swing(1.6, 1.1, 1, 1, 0.4, "home") > 0.0
    assert result_swing(1.6, 1.1, 1, 1, 0.0, "home") == 0.0
