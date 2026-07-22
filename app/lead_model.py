"""Independent in-play win-probability model for lead/clock sports.

Basketball, football, and hockey share one structure: the score margin evolves
like a drifting Brownian motion, so the home team's final margin is approximately

    final_margin  ~  Normal(current_lead + pregame_margin * f,  (sigma * sqrt(f))**2)

where ``f`` is the fraction of regulation time remaining, ``pregame_margin`` is the
market's expected full-game home margin, and ``sigma`` is the sport's standard
deviation of final margin. The home win probability is then ``P(final_margin > 0)``
(this is the classic Stern in-play win-probability model).

Like the tennis model, this is anchored to the market's **pre-match price**: the
pre-match home win probability ``p0`` is inverted into ``pregame_margin`` so the
model reproduces the market at tip-off and then diverges only as the live lead
and clock move. That keeps the edge honest -- it is the model's live estimate
minus the executable price, not a fabricated single-source gap. It is a
display-grade paper-harness estimate, not a validated calibration artifact.

Overtime returns unknown upstream (``game_progress``), so this model never
prices an overtime state. Per-sport parameters are documented approximations.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import NormalDist

_N = NormalDist()


@dataclass(frozen=True, slots=True)
class LeadSportParams:
    """Per-league scoring shape. ``sigma`` is the SD of final margin; ``score_unit``
    is a typical single-score change in the lead; ``seconds_per_score`` is a rough
    interval between scoring events, used only to size latency-window movement."""
    sigma: float
    score_unit: float
    seconds_per_score: float


# Documented approximations from public scoring distributions. sigma is the SD of
# the final home-minus-away margin in the sport's scoring units.
LEAD_SPORT_PARAMS: dict[str, LeadSportParams] = {
    "nba": LeadSportParams(12.0, 2.5, 30.0),
    "wnba": LeadSportParams(11.0, 2.4, 32.0),
    "ncaab": LeadSportParams(11.0, 2.4, 35.0),
    "nfl": LeadSportParams(13.5, 5.0, 180.0),
    "ncaaf": LeadSportParams(16.0, 5.5, 170.0),
    "nhl": LeadSportParams(2.2, 1.0, 600.0),
}


def live_win_probability(lead: float, pregame_margin: float,
                         fraction_remaining: float, sigma: float) -> float:
    """P(home wins) from the current lead, expected full-game margin, fraction of
    regulation remaining, and the sport's final-margin standard deviation."""
    f = min(max(fraction_remaining, 0.0), 1.0)
    if sigma <= 0 or f <= 1e-9:
        # No remaining variance: the current lead decides it.
        return 1.0 if lead > 0 else 0.0 if lead < 0 else 0.5
    z = (lead + pregame_margin * f) / (sigma * f ** 0.5)
    return _N.cdf(z)


def pregame_margin_from_price(p0: float, sigma: float) -> float:
    """Invert a pre-match home win probability into an expected full-game margin.

    At tip-off (f=1, lead 0) ``live_win_probability`` returns ``Phi(margin/sigma)``,
    so ``margin = sigma * Phi^-1(p0)`` reproduces the market's pre-match price."""
    p0 = min(max(p0, 1e-6), 1.0 - 1e-6)
    return sigma * _N.inv_cdf(p0)


def implied_prematch_price(p_now: float, lead: float, fraction_remaining: float,
                           sigma: float) -> float | None:
    """The equivalent tip-off home price (lead 0, full time) for a market pricing
    the home side at ``p_now`` given the current lead and fraction remaining.

    This lets the model anchor to the market at ANY point in the game, not only
    pre-match: invert the drift that reproduces ``p_now`` now, then express it as
    the tip-off price the rest of the pipeline already propagates. Returns None
    if there is no remaining time to invert against. Identity at (lead 0, f=1)."""
    f = min(max(fraction_remaining, 0.0), 1.0)
    if sigma <= 0 or f <= 1e-9:
        return None
    p = min(max(p_now, 1e-6), 1.0 - 1e-6)
    margin = (_N.inv_cdf(p) * sigma * f ** 0.5 - lead) / f
    return _N.cdf(margin / sigma)


def win_probability_band(p0: float, lead: float, fraction_remaining: float,
                         sigma: float, *, prematch_sd: float = 0.05) -> tuple[float, float, float]:
    """``(low, mid, high)`` home-win band by propagating a pre-match-price
    uncertainty ``prematch_sd`` through the live state. Collapses toward a point
    as the clock runs out (remaining variance -> 0), so late leads are near-certain
    and carry little model uncertainty. ``low <= mid <= high``."""
    p0 = min(max(p0, 1e-6), 1.0 - 1e-6)

    def at(anchor: float) -> float:
        margin = pregame_margin_from_price(anchor, sigma)
        return live_win_probability(lead, margin, fraction_remaining, sigma)

    return (at(max(1e-6, p0 - prematch_sd)), at(p0), at(min(1.0 - 1e-6, p0 + prematch_sd)))


def score_swing(p0: float, lead: float, fraction_remaining: float,
                sigma: float, score_unit: float) -> float:
    """Half the home-win-probability swing from one typical score landing either
    way. Small early, large late (a bucket near the end can flip a close game),
    which is where latency-driven adverse selection is worst."""
    margin = pregame_margin_from_price(p0, sigma)
    up = live_win_probability(lead + score_unit, margin, fraction_remaining, sigma)
    down = live_win_probability(lead - score_unit, margin, fraction_remaining, sigma)
    return abs(up - down) / 2.0
