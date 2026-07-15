"""Parse spread / total outcome labels into a numeric line and a normalized side.

The Odds API encodes the point in the outcome label (see sources._outcome_label):
spreads as "<team> <signed point>" (e.g. "Boston Celtics +2.5") and totals as
"Over <point>" / "Under <point>". The Rust engine needs the numeric line plus a
side it can reason about without knowing team names, so we resolve the side here
(where the Event's home/away names are available) to one of:
home | away | over | under, and hand Rust just (point, side).
"""
from __future__ import annotations

import re

_SPREAD_MARKETS = {"spread", "spreads", "handicap", "point_spread"}
_TOTAL_MARKETS = {"total", "totals", "over_under", "over/under", "ou", "game_total"}

_TRAILING_SIGNED = re.compile(r"^(.*?)\s*([+-]\d+(?:\.\d+)?)$")
_TOTAL_LABEL = re.compile(r"^(over|under)\s+(\d+(?:\.\d+)?)$", re.IGNORECASE)


def is_spread_market(market: str) -> bool:
    return (market or "").strip().casefold() in _SPREAD_MARKETS


def is_total_market(market: str) -> bool:
    return (market or "").strip().casefold() in _TOTAL_MARKETS


def quote_line_side(market: str, outcome: str, home: str, away: str) -> tuple[float | None, str | None]:
    """Return (point, side) for a spread/total outcome, else (None, None)."""
    label = (outcome or "").strip()
    if is_total_market(market):
        match = _TOTAL_LABEL.match(label)
        if match:
            return float(match.group(2)), match.group(1).casefold()
        return None, None
    if is_spread_market(market):
        match = _TRAILING_SIGNED.match(label)
        if not match:
            return None, None
        team, point = match.group(1).strip().casefold(), float(match.group(2))
        if team in ("home", (home or "").strip().casefold()):
            return point, "home"
        if team in ("away", (away or "").strip().casefold()):
            return point, "away"
        return point, None  # team we can't map to home/away
    return None, None


def pregame_priors(quotes, home: str, away: str) -> tuple[float | None, float | None]:
    """Best-effort pregame spread (home point) and total line from current quotes.

    Captured near tip and held as a prior; the home spread point becomes the
    expected home margin (mu = -point) for the live win-probability model.
    """
    spread_home = None
    total_line = None
    for quote in quotes:
        point, side = quote_line_side(quote.market, quote.outcome, home, away)
        if point is None:
            continue
        if spread_home is None and side == "home":
            spread_home = point
        elif total_line is None and side in ("over", "under"):
            total_line = point
        if spread_home is not None and total_line is not None:
            break
    return spread_home, total_line
