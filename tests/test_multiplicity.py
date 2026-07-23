"""Tests for familywise multiplicity control (Reality Check / Romano-Wolf)."""
from __future__ import annotations

import pytest

from app.multiplicity import reality_check_pvalue, romano_wolf_pvalues

EVENTS = [f"e{i}" for i in range(30)]
GOOD = [0.08] * 30                                      # a real, positive edge
NOISE = [0.1 if i % 2 == 0 else -0.1 for i in range(30)]  # mean-zero


def test_romano_wolf_flags_a_true_winner_and_not_noise():
    p = romano_wolf_pvalues({"good": GOOD, "noise": NOISE}, EVENTS, draws=500, seed=1)
    assert p["good"] < 0.05    # a genuine out-of-sample edge survives multiplicity
    assert p["noise"] > 0.2    # a mean-zero candidate does not


def test_reality_check_detects_the_best_candidate():
    p = reality_check_pvalue({"good": GOOD, "noise": NOISE}, EVENTS, draws=500, seed=1)
    assert p < 0.05


def test_multiplicity_is_deterministic_under_a_fixed_seed():
    a = romano_wolf_pvalues({"good": GOOD, "noise": NOISE}, EVENTS, draws=300, seed=7)
    b = romano_wolf_pvalues({"good": GOOD, "noise": NOISE}, EVENTS, draws=300, seed=7)
    assert a == b


def test_single_candidate_romano_wolf_equals_reality_check():
    rw = romano_wolf_pvalues({"good": GOOD}, EVENTS, draws=500, seed=3)
    rc = reality_check_pvalue({"good": GOOD}, EVENTS, draws=500, seed=3)
    assert rw["good"] == pytest.approx(rc)


def test_searching_many_nulls_does_not_manufacture_significance():
    # Twelve mean-zero candidates (phase-shifted): none should be flagged.
    candidates = {
        f"noise{k}": [0.1 if (i + k) % 2 == 0 else -0.1 for i in range(30)]
        for k in range(12)
    }
    p = romano_wolf_pvalues(candidates, EVENTS, draws=500, seed=2)
    assert all(value > 0.05 for value in p.values())
