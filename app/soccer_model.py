"""Independent in-play win/draw/away model for soccer (Poisson goals).

Soccer is low-scoring and three-way, so a lead/clock diffusion is a poor fit. The
standard approach models each side's remaining goals as independent Poisson
arrivals and derives the result probabilities from the joint score distribution:

    remaining_home ~ Poisson(lam_home * f),  remaining_away ~ Poisson(lam_away * f)
    final_home = home_now + remaining_home,   final_away = away_now + remaining_away

where ``f`` is the fraction of regulation time remaining. The full-match rates
``(lam_home, lam_away)`` are inverted from the market's **pre-match** 1X2 prices
(home-win and draw), so the model reproduces the market at kick-off and diverges
only as the live score and clock move -- the same pre-match-anchor discipline as
the tennis and lead models. The edge is the model's live result probability minus
the executable price.

Simplifications: independent Poisson (no goal-time correlation or Dixon-Coles
low-score adjustment), constant rates across the match, regulation only (added
time / extra time is skipped upstream via ``game_progress``). Display-grade paper
output, not validated calibration.
"""
from __future__ import annotations

from math import exp

_MAX_GOALS = 12  # remaining-goal cap; soccer tails beyond this are negligible


def _poisson_pmf(rate: float) -> list[float]:
    """Normalized Poisson pmf over 0.._MAX_GOALS for a non-negative ``rate``."""
    rate = max(0.0, rate)
    pmf = [exp(-rate)]
    for k in range(1, _MAX_GOALS + 1):
        pmf.append(pmf[-1] * rate / k)
    total = sum(pmf)
    return [value / total for value in pmf] if total > 0 else pmf


def result_probabilities(lam_home: float, lam_away: float, home_now: int,
                         away_now: int, fraction_remaining: float) -> tuple[float, float, float]:
    """``(p_home, p_draw, p_away)`` for the final result from the live score and
    remaining scoring rates scaled by ``fraction_remaining``."""
    f = min(max(fraction_remaining, 0.0), 1.0)
    home_pmf = _poisson_pmf(lam_home * f)
    away_pmf = _poisson_pmf(lam_away * f)
    p_home = p_draw = p_away = 0.0
    for i, ph in enumerate(home_pmf):
        for j, pa in enumerate(away_pmf):
            joint = ph * pa
            final_margin = (home_now + i) - (away_now + j)
            if final_margin > 0:
                p_home += joint
            elif final_margin == 0:
                p_draw += joint
            else:
                p_away += joint
    return p_home, p_draw, p_away


def prematch_rates(p_home: float, p_draw: float) -> tuple[float, float] | None:
    """Full-match Poisson rates from the pre-match 1X2 price (the 0-0 case of
    :func:`rates_from_state`)."""
    return rates_from_state(p_home, p_draw, 0, 0, 1.0)


def rates_from_state(p_home: float, p_draw: float, home_now: int, away_now: int,
                     fraction_remaining: float) -> tuple[float, float] | None:
    """Invert live home-win and draw probabilities into full-match Poisson rates
    ``(lam_home, lam_away)`` given the current score and remaining fraction, so
    the model can anchor to the market at any point in the match. Returns None if
    the inputs are not a usable 1X2 prior or there is no time left. A coarse grid
    then a local refine keeps it robust without an external solver; it runs once
    per match and is then cached."""
    if not (0.0 < p_home < 1.0 and 0.0 < p_draw < 1.0):
        return None
    f = min(max(fraction_remaining, 0.0), 1.0)
    if f <= 1e-9:
        return None
    # Polymarket 1X2 legs are independent binaries and need not sum to 1. If the
    # implied away probability is non-positive, renormalize home+draw so it has
    # mass and the fit is well-posed (rather than refusing to price the match).
    if p_home + p_draw >= 0.999:
        scale = 0.98 / (p_home + p_draw)
        p_home, p_draw = p_home * scale, p_draw * scale

    def error(lam_h: float, lam_a: float) -> float:
        home, draw, _ = result_probabilities(lam_h, lam_a, home_now, away_now, f)
        return (home - p_home) ** 2 + (draw - p_draw) ** 2

    def search(h_lo: float, h_hi: float, a_lo: float, a_hi: float, steps: int):
        best, best_err = (1.3, 1.1), float("inf")
        for hi in range(steps + 1):
            lam_h = h_lo + (h_hi - h_lo) * hi / steps
            for ai in range(steps + 1):
                lam_a = a_lo + (a_hi - a_lo) * ai / steps
                err = error(lam_h, lam_a)
                if err < best_err:
                    best, best_err = (lam_h, lam_a), err
        return best

    lam_h, lam_a = search(0.1, 6.0, 0.1, 6.0, 32)
    lam_h, lam_a = search(max(0.05, lam_h - 0.25), lam_h + 0.25,
                          max(0.05, lam_a - 0.25), lam_a + 0.25, 20)
    return lam_h, lam_a


_SIDE_INDEX = {"home": 0, "draw": 1, "away": 2}


def _side_probability(probs: tuple[float, float, float], side: str) -> float:
    return probs[_SIDE_INDEX[side]]


def result_band(lam_home: float, lam_away: float, home_now: int, away_now: int,
                fraction_remaining: float, side: str, *,
                rate_sd: float = 0.15) -> tuple[float, float, float]:
    """``(low, mid, high)`` for one result side, from scoring-rate uncertainty
    ``rate_sd`` (fractional). Collapses as the clock runs out."""
    mid = _side_probability(
        result_probabilities(lam_home, lam_away, home_now, away_now, fraction_remaining), side)
    scaled = [
        _side_probability(
            result_probabilities(lam_home * s, lam_away * s, home_now, away_now,
                                 fraction_remaining), side)
        for s in (1.0 - rate_sd, 1.0 + rate_sd)
    ]
    return min(scaled + [mid]), mid, max(scaled + [mid])


def result_swing(lam_home: float, lam_away: float, home_now: int, away_now: int,
                 fraction_remaining: float, side: str) -> float:
    """Half the |probability change| for ``side`` from the next goal going to the
    home vs the away team -- the local price-move scale for the latency gate."""
    if fraction_remaining <= 0.0:
        return 0.0
    home_scores = _side_probability(
        result_probabilities(lam_home, lam_away, home_now + 1, away_now, fraction_remaining), side)
    away_scores = _side_probability(
        result_probabilities(lam_home, lam_away, home_now, away_now + 1, fraction_remaining), side)
    return abs(home_scores - away_scores) / 2.0
