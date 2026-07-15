"""Player-prop distribution & blend engine (Phase 3).

This module turns a projected mean (mu) for a player's stat into P(over line),
using the distribution family the stat actually follows, and blends that model
probability with the vig-free market prior in logit space.

IMPORTANT — honesty note: the *projection* (mu) is the hard, data-hungry part
(minutes, usage, pace, opponent defense-vs-position, injuries). It requires a
real player-data feed that this system does not yet ingest, so `project()`
returns None by default and the app falls back to the market-relative edge. The
functions below are the (unit-tested) math that a projection source plugs into;
they do not invent data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

_EPS = 1e-9

# Stats modeled as continuous (Normal); the rest are treated as counts.
_CONTINUOUS = {
    "points", "pts", "yards", "pass_yds", "pass_yards", "rush_yds", "rush_yards",
    "reception_yds", "receiving_yards", "receptions_yards",
}


def _erf(x: float) -> float:
    sign = -1.0 if x < 0 else 1.0
    x = abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
               - 0.284496736) * t + 0.254829592) * t * math.exp(-x * x)
    return sign * y


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + _erf(x / math.sqrt(2.0)))


def _clip(p: float) -> float:
    return min(max(p, 0.001), 0.999)


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _inv_logit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _poisson_cdf(k: int, mu: float) -> float:
    """P(X <= k) for X ~ Poisson(mu)."""
    if mu <= 0:
        return 1.0
    term = math.exp(-mu)
    total = term
    for i in range(1, k + 1):
        term *= mu / i
        total += term
    return min(total, 1.0)


def _negbin_cdf(k: int, mu: float, variance: float) -> float:
    """P(X <= k) for a negative-binomial with the given mean and variance>mu."""
    if variance <= mu:  # not overdispersed -> fall back to Poisson
        return _poisson_cdf(k, mu)
    r = mu * mu / (variance - mu)
    p = r / (r + mu)
    pmf = p ** r  # P(X = 0)
    total = pmf
    for i in range(1, k + 1):
        pmf *= (r + i - 1) / i * (1 - p)
        total += pmf
    return min(total, 1.0)


def _default_sigma(mu: float, stat_type: str) -> float:
    """Rough SD for a continuous stat when the caller has no empirical value.

    Placeholder only — real sigma should be fit from history. Points are ~2.5-4x
    Poisson variance; yards are wide. Documented as approximate."""
    st = (stat_type or "").casefold()
    if "yd" in st or "yard" in st:
        return 60.0
    return math.sqrt(max(mu, 1.0) * 3.0)  # points-like overdispersion


def over_probability(mu: float, line: float, stat_type: str = "points",
                     sigma: float | None = None, dispersion: float | None = None) -> float:
    """P(stat > line). Half-integer lines have no push; for an integer line this
    is P(X > line) (strictly), with the push mass handled by the caller."""
    st = (stat_type or "").casefold()
    if st in _CONTINUOUS:
        s = sigma if sigma and sigma > 0 else _default_sigma(mu, st)
        return _clip(1.0 - normal_cdf((line - mu) / s))
    # count stat: Poisson, or negative-binomial when overdispersion is known
    k = math.floor(line)
    if dispersion is not None and dispersion > 1.0:
        cdf = _negbin_cdf(k, mu, dispersion * mu)
    else:
        cdf = _poisson_cdf(k, mu)
    return _clip(1.0 - cdf)


def blend_logit(p_model: float, p_market: float, alpha: float = 0.3) -> float:
    """Shrink the model toward the (sharp) market prior in logit space.

    alpha is the model's weight; prop closing lines are sharp, so alpha is small
    (0.2-0.4) and should shrink further when the projection is uncertain."""
    a = min(max(alpha, 0.0), 1.0)
    return _clip(_inv_logit(a * _logit(p_model) + (1 - a) * _logit(p_market)))


def pra_over_probability(means: list[float], sigmas: list[float], rho: float,
                         line: float) -> float:
    """P(sum of correlated stats > line) via a Normal approximation.

    Models a combined prop (e.g. points+rebounds+assists) as a correlated SUM:
    Var = sum(sigma_i^2) + 2*rho*sum_{i<j} sigma_i*sigma_j. Treating the legs as
    independent understates variance and manufactures spurious edge."""
    mean = sum(means)
    var = sum(s * s for s in sigmas)
    for i in range(len(sigmas)):
        for j in range(i + 1, len(sigmas)):
            var += 2.0 * rho * sigmas[i] * sigmas[j]
    if var <= 0:
        return _clip(1.0 if mean > line else 0.0)
    return _clip(1.0 - normal_cdf((line - mean) / math.sqrt(var)))


@dataclass(slots=True)
class Projection:
    """A player-stat projection: mean plus distribution parameters."""
    mu: float
    stat_type: str = "points"
    sigma: float | None = None
    dispersion: float | None = None


def project(player: str, stat_type: str, context: dict | None = None) -> Projection | None:
    """Return an independent projection for a player's stat, or None.

    Returns None until a real player-data source (minutes/usage/pace/matchup) is
    wired in — the app then falls back to the market-relative edge. This is the
    single integration point for that future feed; it deliberately does not
    guess a mean from thin air.
    """
    return None
