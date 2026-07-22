"""Joint-outcome (portfolio) Kelly sizing for correlated paper bets.

Sizing each position with its own Kelly fraction over-stakes a book of bets that
move together: two bets on the same team side both win or both lose, so betting
each at full Kelly doubles the risk of one underlying outcome. This module sizes
the whole set at once -- it maximizes expected log wealth over Monte-Carlo joint
outcomes, so positively correlated bets share one risk budget while genuinely
independent bets are allowed a little more total exposure (diversification).

Correlation is modeled conservatively from the caller's correlation group: bets
in the same group are perfectly positively correlated (a common latent draw, so
each still honors its own marginal win probability); bets in different groups are
independent. This needs no fitted covariance matrix and never overstates
diversification. Output is display-grade paper sizing, not investment advice.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Candidate:
    prob: float    # model win probability of the backed selection, in (0,1)
    price: float   # executable entry price, in (0,1)
    group: str     # correlation group; same group => perfectly correlated
    cap: float     # maximum stake in dollars (independent exposure cap)


def joint_kelly_stakes(
    candidates: list[Candidate], bankroll: float, *,
    kelly_multiplier: float = 1.0, max_total_fraction: float = 1.0,
    draws: int = 1500, seed: int = 0, iterations: int = 400, learning_rate: float = 0.5,
) -> list[float]:
    """Return the per-candidate stake in dollars that (approximately) maximizes
    fractional expected log wealth over simulated joint outcomes.

    Each stake is non-negative, at most its ``cap``, and the total is at most
    ``max_total_fraction`` of ``bankroll``. ``kelly_multiplier`` shrinks the full
    Kelly solution (e.g. 0.5 for half-Kelly). The objective is concave, so a
    projected gradient ascent converges to the constrained optimum.
    """
    n = len(candidates)
    if n == 0 or bankroll <= 0:
        return [0.0] * n

    returns_if_win = [1.0 / c.price - 1.0 for c in candidates]
    multiplier = max(0.0, kelly_multiplier)

    # Fast, exact path for a lone bet (the common case: models are moneyline-only,
    # one side per event). Full Kelly is f* = (p*(b+1) - 1) / b, b = payoff-if-win.
    if n == 1:
        candidate, payoff = candidates[0], returns_if_win[0]
        fraction = (candidate.prob * (payoff + 1.0) - 1.0) / payoff if payoff > 0 else 0.0
        fraction = min(max(0.0, fraction) * multiplier,
                       max(0.0, candidate.cap) / bankroll, max_total_fraction)
        return [fraction * bankroll]
    group_index: dict[str, int] = {}
    for c in candidates:
        group_index.setdefault(c.group, len(group_index))

    # Monte-Carlo per-dollar return rows. A single uniform latent per group makes
    # same-group bets comonotonic (perfectly correlated) while honoring marginals.
    rng = random.Random(seed)
    rows: list[list[float]] = []
    for _ in range(draws):
        latent = [rng.random() for _ in range(len(group_index))]
        rows.append([
            returns_if_win[i] if latent[group_index[c.group]] < c.prob else -1.0
            for i, c in enumerate(candidates)
        ])

    caps_fraction = [max(0.0, c.cap) / bankroll for c in candidates]
    x = [0.0] * n  # fractions of bankroll
    for _ in range(iterations):
        gradient = [0.0] * n
        for row in rows:
            wealth = 1.0 + sum(x[i] * row[i] for i in range(n))
            wealth = wealth if wealth > 1e-9 else 1e-9
            for i in range(n):
                gradient[i] += row[i] / wealth
        x = [x[i] + learning_rate * gradient[i] / len(rows) for i in range(n)]
        x = [min(max(0.0, x[i]), caps_fraction[i]) for i in range(n)]
        total = sum(x)
        if total > max_total_fraction and total > 0:
            scale = max_total_fraction / total
            x = [value * scale for value in x]

    x = [min(caps_fraction[i], multiplier * x[i]) for i in range(n)]
    return [value * bankroll for value in x]
