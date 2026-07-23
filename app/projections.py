"""Player-prop distribution & blend engine (Phase 3).

This module turns a projected mean (mu) for a player's stat into the probability
partition of a betting line -- P(under), P(push), P(over) -- using the
distribution family the stat actually follows, and blends a model probability
with the vig-free market prior in logit space.

IMPORTANT -- honesty note: the *projection* (mu) is the hard, data-hungry part
(minutes, usage, pace, opponent defense-vs-position, injuries). It requires a
real player-data feed that this system does not yet ingest, so `project()`
returns None by default and the app falls back to the market-relative edge. The
functions below are the (unit-tested) math that a projection source plugs into;
they do not invent data.

Numerical policy: the discrete tails are computed in log space (via `lgamma`) so
a large mean can never underflow `exp(-mu)` and zero the whole distribution, and
probabilities are NOT clipped inside the computation -- the real value is
returned with a `numerical_error_bound`. A display layer may format tiny values;
proper scores need the true one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

_EPS = 1e-9
# Max error of the Abramowitz-Stegun 7.1.26 erf approximation used by normal_cdf.
_NORMAL_CDF_ERROR = 1.5e-7
# Above this mean the exact log-space sum is replaced by a continuity-corrected
# normal approximation (Knuesel's asymptotic regime), keeping work bounded.
_EXACT_COUNT_MAX_MEAN = 400.0

# Stats modeled as continuous (Normal); the rest are treated as counts.
_CONTINUOUS = {
    "points", "pts", "yards", "pass_yds", "pass_yards", "rush_yds", "rush_yards",
    "reception_yds", "receiving_yards", "receptions_yards",
}


@dataclass(frozen=True, slots=True)
class DiscreteLineProbability:
    """The full probability partition of a betting line.

    ``under + push + over == 1`` within ``numerical_error_bound``. ``push`` is
    zero for a half-integer line and the point mass ``P(X == line)`` for an
    integer line on a count stat.
    """
    under: float
    push: float
    over: float
    distribution: str
    parameters: dict[str, float]
    numerical_error_bound: float


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


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


def _is_integer(value: float) -> bool:
    return abs(value - round(value)) <= 1e-9


def _normal_partition(mu: float, sigma: float, line: float, distribution: str,
                      parameters: dict[str, float]) -> DiscreteLineProbability:
    """Continuous (Normal) line partition; push is always zero. Both tails are
    evaluated directly (never as ``1 - cdf``) to keep small tails accurate."""
    z = (line - mu) / sigma
    under = normal_cdf(z)
    over = normal_cdf(-z)
    return DiscreteLineProbability(under, 0.0, over, distribution, parameters,
                                   _NORMAL_CDF_ERROR)


def _split_pmf(pmf: list[float], line: float) -> tuple[float, float, float]:
    """Partition a discrete pmf over 0..len-1 into (under, push, over) for ``line``."""
    n = len(pmf)

    def total(lo: int, hi: int) -> float:
        lo, hi = max(lo, 0), min(hi, n - 1)
        return math.fsum(pmf[lo:hi + 1]) if lo <= hi else 0.0

    if _is_integer(line):
        k = round(line)
        push = pmf[k] if 0 <= k < n else 0.0
        return total(0, k - 1), push, total(k + 1, n - 1)
    floor = math.floor(line)
    return total(0, floor), 0.0, total(floor + 1, n - 1)


def _continuity_corrected(mu: float, sd: float, line: float, distribution: str,
                          parameters: dict[str, float]) -> DiscreteLineProbability:
    """Normal approximation with a continuity correction for a large-mean count."""
    if _is_integer(line):
        k = round(line)
        under = normal_cdf((k - 0.5 - mu) / sd)
        over = normal_cdf((mu - (k + 0.5)) / sd)
        push = max(0.0, 1.0 - under - over)
    else:
        under = normal_cdf((line - mu) / sd)
        over = normal_cdf((mu - line) / sd)
        push = 0.0
    # Continuity-corrected CLT error is roughly O(1/sd) on top of the erf error.
    bound = _NORMAL_CDF_ERROR + 0.5 / sd
    return DiscreteLineProbability(under, push, over, distribution, parameters, bound)


def _point_mass(value: int, line: float, distribution: str,
                parameters: dict[str, float]) -> DiscreteLineProbability:
    return DiscreteLineProbability(
        1.0 if value < line else 0.0,
        1.0 if value == line else 0.0,
        1.0 if value > line else 0.0,
        distribution, parameters, 0.0,
    )


def _poisson_partition(mu: float, line: float) -> DiscreteLineProbability:
    if mu <= 0.0:
        return _point_mass(0, line, "poisson", {"mu": mu})
    sd = math.sqrt(mu)
    if mu > _EXACT_COUNT_MAX_MEAN:
        return _continuity_corrected(mu, sd, line, "poisson", {"mu": mu})
    log_mu = math.log(mu)
    upper = max(int(math.ceil(mu + 12.0 * sd + 20.0)), math.floor(line) + 2)
    # Each pmf is computed independently in log space, so a tiny P(X=0) never
    # zeros the recurrence the way a forward `exp(-mu)` product would.
    pmf = [math.exp(-mu + k * log_mu - math.lgamma(k + 1)) for k in range(upper + 1)]
    under, push, over = _split_pmf(pmf, line)
    ratio = mu / (upper + 1)
    tail = pmf[upper] * ratio / (1.0 - ratio) if ratio < 1.0 else float("inf")
    return DiscreteLineProbability(under, push, over, "poisson", {"mu": mu}, tail + _EPS)


def _negbin_partition(mu: float, variance: float, line: float) -> DiscreteLineProbability:
    if variance <= mu:  # not overdispersed -> Poisson
        return _poisson_partition(mu, line)
    r = mu * mu / (variance - mu)
    p = r / (r + mu)
    sd = math.sqrt(variance)
    params = {"mu": mu, "variance": variance, "r": r, "p": p}
    if mu > _EXACT_COUNT_MAX_MEAN:
        return _continuity_corrected(mu, sd, line, "negative_binomial", params)
    log_p, log_1mp = math.log(p), math.log1p(-p)
    upper = max(int(math.ceil(mu + 12.0 * sd + 20.0)), math.floor(line) + 2)
    pmf = [
        math.exp(math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
                 + r * log_p + k * log_1mp)
        for k in range(upper + 1)
    ]
    under, push, over = _split_pmf(pmf, line)
    ratio = (upper + r) / (upper + 1) * (1.0 - p)
    tail = pmf[upper] * ratio / (1.0 - ratio) if ratio < 1.0 else float("inf")
    return DiscreteLineProbability(under, push, over, "negative_binomial", params,
                                   tail + _EPS)


def _default_sigma(mu: float, stat_type: str) -> float:
    """Rough SD for a continuous stat when the caller has no empirical value.

    Placeholder only -- real sigma should be fit from history. Points are ~2.5-4x
    Poisson variance; yards are wide. Documented as approximate."""
    st = (stat_type or "").casefold()
    if "yd" in st or "yard" in st:
        return 60.0
    return math.sqrt(max(mu, 1.0) * 3.0)  # points-like overdispersion


def line_probability(mu: float, line: float, stat_type: str = "points",
                     sigma: float | None = None,
                     dispersion: float | None = None) -> DiscreteLineProbability:
    """Full under/push/over partition of a stat line.

    Count stats use a Poisson (or negative-binomial when ``dispersion > 1``)
    partition computed in log space; continuous stats use a Normal partition.
    Raises ``ValueError`` on a non-finite/malformed mean, line, sigma or
    dispersion, or a negative count mean.
    """
    if not _finite(mu, line):
        raise ValueError("mu and line must be finite")
    if sigma is not None and (not math.isfinite(sigma) or sigma <= 0.0):
        raise ValueError("sigma must be a positive, finite standard deviation")
    if dispersion is not None and not math.isfinite(dispersion):
        raise ValueError("dispersion must be finite")

    st = (stat_type or "").casefold()
    if st in _CONTINUOUS:
        s = sigma if sigma and sigma > 0 else _default_sigma(mu, st)
        return _normal_partition(mu, s, line, "normal", {"mu": mu, "sigma": s})
    if mu < 0.0:
        raise ValueError("a count-stat mean cannot be negative")
    if dispersion is not None and dispersion > 1.0:
        return _negbin_partition(mu, dispersion * mu, line)
    return _poisson_partition(mu, line)


def over_probability(mu: float, line: float, stat_type: str = "points",
                     sigma: float | None = None, dispersion: float | None = None) -> float:
    """P(stat > line), the ``over`` leg of :func:`line_probability`.

    Not clipped: an extreme line returns its true (possibly tiny) probability.
    """
    return line_probability(mu, line, stat_type, sigma, dispersion).over


def blend_logit(p_model: float, p_market: float, alpha: float = 0.3) -> float:
    """Shrink the model toward the (sharp) market prior in logit space.

    alpha is the model's weight; prop closing lines are sharp, so alpha is small
    (0.2-0.4) and should shrink further when the projection is uncertain."""
    a = min(max(alpha, 0.0), 1.0)
    return _clip(_inv_logit(a * _logit(p_model) + (1 - a) * _logit(p_market)))


def equicorrelation_bounds(dimension: int) -> tuple[float, float]:
    """Admissible range of a shared correlation for a ``dimension``-vector: a
    common ``rho`` keeps the covariance PSD only for ``-1/(d-1) <= rho <= 1``."""
    if dimension <= 1:
        return -1.0, 1.0
    return -1.0 / (dimension - 1), 1.0


def pra_line_probability(means: list[float], sigmas: list[float], rho: float,
                         line: float) -> DiscreteLineProbability:
    """Partition for a combined prop (e.g. points+rebounds+assists) modeled as a
    correlated SUM under an equicorrelation ``rho``.

    ``Var = sum(sigma_i^2) + 2*rho*sum_{i<j} sigma_i*sigma_j``. Raises
    ``ValueError`` when the inputs are malformed or ``rho`` is outside the
    admissible equicorrelation range (which would imply a non-PSD covariance)."""
    if len(means) != len(sigmas):
        raise ValueError("means and sigmas must have the same length")
    if not means:
        raise ValueError("at least one leg is required")
    if not _finite(*means, *sigmas, rho, line) or any(s < 0 for s in sigmas):
        raise ValueError("means, sigmas, rho and line must be finite; sigmas >= 0")
    low, high = equicorrelation_bounds(len(sigmas))
    if not (low - 1e-12 <= rho <= high + 1e-12):
        raise ValueError(
            f"rho {rho} outside admissible equicorrelation range [{low:.6g}, {high:.6g}]")

    mean = math.fsum(means)
    var = math.fsum(s * s for s in sigmas)
    for i in range(len(sigmas)):
        for j in range(i + 1, len(sigmas)):
            var += 2.0 * rho * sigmas[i] * sigmas[j]
    params = {"mean": mean, "variance": var, "rho": rho}
    if var <= _EPS:  # degenerate: the sum is effectively a point at its mean
        rounded = round(mean)
        if _is_integer(mean):
            return _point_mass(rounded, line, "normal", params)
        return DiscreteLineProbability(
            1.0 if mean < line else 0.0, 0.0, 1.0 if mean > line else 0.0,
            "normal", params, 0.0)
    return _normal_partition(mean, math.sqrt(var), line, "normal", params)


def pra_over_probability(means: list[float], sigmas: list[float], rho: float,
                         line: float) -> float:
    """P(sum of correlated stats > line); the ``over`` leg of
    :func:`pra_line_probability`. Treating the legs as independent understates
    variance and manufactures spurious edge."""
    return pra_line_probability(means, sigmas, rho, line).over


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
    wired in -- the app then falls back to the market-relative edge. This is the
    single integration point for that future feed; it deliberately does not
    guess a mean from thin air.
    """
    return None
