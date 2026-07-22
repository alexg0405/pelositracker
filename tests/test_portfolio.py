"""Tests for joint-outcome (portfolio) Kelly sizing."""
from __future__ import annotations

import pytest

from app.portfolio import Candidate, joint_kelly_stakes

BANKROLL = 1000.0


def _c(prob=0.6, price=0.5, group="g", cap=BANKROLL):
    return Candidate(prob=prob, price=price, group=group, cap=cap)


def test_empty_book():
    assert joint_kelly_stakes([], BANKROLL) == []


def test_single_bet_matches_full_kelly():
    # p=.6 at even odds -> Kelly fraction 0.2 -> ~$200.
    [stake] = joint_kelly_stakes([_c()], BANKROLL)
    assert stake == pytest.approx(200.0, abs=25.0)


def test_negative_edge_gets_no_stake():
    [stake] = joint_kelly_stakes([_c(prob=0.45)], BANKROLL)
    assert stake == pytest.approx(0.0, abs=1.0)


def test_perfectly_correlated_bets_share_one_budget():
    both = joint_kelly_stakes([_c(group="same"), _c(group="same")], BANKROLL)
    [single] = joint_kelly_stakes([_c()], BANKROLL)
    # Two identical, perfectly correlated bets ~= one bet's budget, not double.
    assert sum(both) == pytest.approx(single, rel=0.2)
    assert sum(both) < 1.7 * single


def test_independent_bets_diversify_more_total_less_each():
    indep = joint_kelly_stakes([_c(group="a"), _c(group="b")], BANKROLL)
    [single] = joint_kelly_stakes([_c()], BANKROLL)
    assert all(stake < single for stake in indep)      # each shrinks
    assert single < sum(indep) < 2.0 * single          # but total grows


def test_respects_per_bet_cap():
    [stake] = joint_kelly_stakes([_c(cap=30.0)], BANKROLL)
    assert stake == pytest.approx(30.0, abs=1.0)


def test_respects_total_fraction_budget():
    book = [_c(group="a"), _c(group="b"), _c(group="c")]
    stakes = joint_kelly_stakes(book, BANKROLL, max_total_fraction=0.1)
    assert sum(stakes) <= 0.1 * BANKROLL + 1e-6


def test_half_kelly_is_half_of_full():
    [full] = joint_kelly_stakes([_c()], BANKROLL, kelly_multiplier=1.0)
    [half] = joint_kelly_stakes([_c()], BANKROLL, kelly_multiplier=0.5)
    assert half == pytest.approx(0.5 * full, rel=0.1)
