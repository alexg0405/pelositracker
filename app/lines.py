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
from functools import lru_cache

# Market identity normalization is pure (string in, string out) and was measured
# as the single largest cost in both quote ingestion and request preparation:
# `base_market_type` ran ~13 times per quote, each time re-running the same
# regex over the same handful of market names. Memoizing the single-argument
# normalizers removes that repetition without changing any result, and it also
# makes the multi-argument callers below cheap, because almost all of their cost
# was these calls rather than their own work.
#
# The caches are bounded by `maxsize`, so a provider that emits unbounded distinct
# market strings degrades to today's behavior (misses) instead of growing memory.
# The size is generous relative to the real market vocabulary and cheap in bytes:
# keys are strings the caller already holds, and most values are either one of
# the small set of family literals below or a `_market_key` result that is itself
# already cached. `lru_cache` is thread-safe, which matters because these are
# reached from the store-writer threads as well as the event loop.
_MARKET_CACHE_SIZE = 4096

FULL_GAME_SCOPE = "full_game"
MLB_FIRST_INNING_SCOPE = "first_inning"
MLB_FIRST_FIVE_SCOPE = "first_five_innings"
SUPPORTED_MARKET_SCOPES = (
    FULL_GAME_SCOPE,
    MLB_FIRST_INNING_SCOPE,
    MLB_FIRST_FIVE_SCOPE,
)

_SPREAD_MARKETS = {
    "spread",
    "spreads",
    "handicap",
    "point_spread",
    "first_five_spread",
}
_TOTAL_MARKETS = {
    "total",
    "totals",
    "over_under",
    "over/under",
    "ou",
    "game_total",
    "first_five_total",
    "first_inning_total",
}
_MONEYLINE_MARKETS = {
    "moneyline",
    "h2h",
    "winner",
    "match_winner",
    "drawable_outcome",
    "first_five_moneyline",
}


@lru_cache(maxsize=_MARKET_CACHE_SIZE)
def _market_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (value or "").strip().casefold()).strip("_")


@lru_cache(maxsize=_MARKET_CACHE_SIZE)
def market_scope(market: str) -> str:
    """Return the exact regulation segment encoded by a market identity.

    Period identity is part of the market key, never an execution hint. This
    prevents a first-five quote from being compared with a full-game quote.
    """
    key = _market_key(market)
    if key in {"nrfi", "yrfi"}:
        return MLB_FIRST_INNING_SCOPE
    if (
        "first_five" in key
        or "first_5" in key
        or "1st_5" in key
        or "1st_five" in key
    ):
        return MLB_FIRST_FIVE_SCOPE
    if (
        "first_inning" in key
        or "1st_inning" in key
        or "1st_1_inning" in key
        or "1st_1_innings" in key
    ):
        return MLB_FIRST_INNING_SCOPE
    return FULL_GAME_SCOPE


@lru_cache(maxsize=_MARKET_CACHE_SIZE)
def base_market_type(market: str) -> str:
    """Collapse a scoped identity only to its line family.

    The caller must keep :func:`market_scope` alongside this value whenever
    comparing contracts.
    """
    key = _market_key(market).removeprefix("sports_market_type_")
    if key in {"nrfi", "yrfi"}:
        return "total"
    if key in _MONEYLINE_MARKETS:
        return "moneyline"
    if key in _SPREAD_MARKETS:
        return "spread"
    if key in _TOTAL_MARKETS:
        return "total"
    if key.startswith(("h2h_1st_5_innings", "h2h_3_way_1st_5_innings")):
        return "moneyline"
    if "first_five_winner" in key:
        return "moneyline"
    if key.startswith("spreads_1st_5_innings") or "first_five_spread" in key:
        return "spread"
    if key.startswith("totals_1st_5_innings") or "first_five_total" in key:
        return "total"
    if key.startswith("totals_1st_1_innings") or "first_inning_run" in key:
        return "total"
    if key.endswith(("_team_full_time_winner", "_team_full_game_winner")):
        return "moneyline"
    if key.endswith("_fight_winner"):
        return "moneyline"
    if key.endswith("_team_full_game_spread"):
        return "spread"
    if key.endswith("_team_full_game_total"):
        return "total"
    return key or "market"


def canonical_scoped_market(market: str) -> str:
    """Return one engine-safe identity for supported base and MLB segments."""
    kind = base_market_type(market)
    scope = market_scope(market)
    if scope == MLB_FIRST_FIVE_SCOPE and kind in {"moneyline", "spread", "total"}:
        return f"first_five_{kind}"
    if scope == MLB_FIRST_INNING_SCOPE and kind == "total":
        return "first_inning_total"
    if scope == FULL_GAME_SCOPE and kind in {"moneyline", "spread", "total"}:
        return kind
    return market or "market"

_TRAILING_SIGNED = re.compile(r"^(.*?)\s*([+-]\d+(?:\.\d+)?)$")
_TOTAL_LABEL = re.compile(r"^(over|under)\s+(\d+(?:\.\d+)?)$", re.IGNORECASE)


def is_spread_market(market: str) -> bool:
    return base_market_type(market) == "spread"


def is_total_market(market: str) -> bool:
    return base_market_type(market) == "total"


def quote_line_side(market: str, outcome: str, home: str, away: str) -> tuple[float | None, str | None]:
    """Return (point, side) for a spread/total/prop outcome, else (None, None).

    An "Over N" / "Under N" label is unambiguous and covers both game totals and
    player props; spreads carry the point on the team label.
    """
    label = (outcome or "").strip()
    total = _TOTAL_LABEL.match(label)
    if total:
        return float(total.group(2)), total.group(1).casefold()
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


def comparison_keys(
    market: str,
    outcome: str,
    home: str,
    away: str,
    point: float | None,
    side: str | None,
) -> tuple[str, str]:
    """Canonical selection identity shared by the engine and live quote store.

    Keeping this in one place lets the in-memory store retain only the freshest
    valid quote the engine could use for each source/selection. Full quote
    history remains in ``HistoryDB``; the live process no longer holds thousands
    of obsolete order-book snapshots per event.
    """
    market_key = (market or "market").strip().casefold()
    outcome_key = (outcome or "").strip().casefold()
    if is_spread_market(market_key) and point is not None and side in {"home", "away"}:
        home_line = point if side == "home" else -point
        if abs(home_line) < 1e-9:
            home_line = 0.0
        return f"spread:{home_line:g}", side
    if is_total_market(market_key) and point is not None and side in {"over", "under"}:
        return f"total:{point:g}", side
    if point is not None and side in {"over", "under"}:
        return f"{market_key}:{point:g}", side
    if outcome_key in {"home", (home or "").strip().casefold()}:
        outcome_key = "home"
    elif outcome_key in {"away", (away or "").strip().casefold()}:
        outcome_key = "away"
    return market_key, outcome_key


def market_cache_stats() -> dict[str, dict[str, int]]:
    """Hit/miss counters for the identity caches, for ``/api/runtime``.

    Exposed because a memoization win is only real if the hit rate is high on
    production market vocabulary; a low rate would mean the cache is being
    thrashed by high-cardinality market strings and should be resized or dropped.
    """
    return {
        name: {
            "hits": info.hits,
            "misses": info.misses,
            "entries": info.currsize,
            "maxsize": info.maxsize or 0,
        }
        for name, info in (
            ("market_key", _market_key.cache_info()),
            ("market_scope", market_scope.cache_info()),
            ("base_market_type", base_market_type.cache_info()),
        )
    }


def clear_market_caches() -> None:
    """Drop every memoized identity result. For tests and benchmarks."""
    _market_key.cache_clear()
    market_scope.cache_clear()
    base_market_type.cache_clear()


def pregame_priors(quotes, home: str, away: str) -> tuple[float | None, float | None]:
    """Best-effort pregame spread (home point) and game total line from quotes.

    Captured near tip and held as a prior; the home spread point becomes the
    expected home margin (mu = -point) for the live win-probability model. Gated
    to real spread/total markets so a player-prop "Over 24.5" is never mistaken
    for the game total.
    """
    spread_home = None
    total_line = None
    for quote in quotes:
        if spread_home is None and is_spread_market(quote.market):
            point, side = quote_line_side(quote.market, quote.outcome, home, away)
            if point is not None and side == "home":
                spread_home = point
        elif total_line is None and is_total_market(quote.market):
            point, side = quote_line_side(quote.market, quote.outcome, home, away)
            if point is not None and side in ("over", "under"):
                total_line = point
        if spread_home is not None and total_line is not None:
            break
    return spread_home, total_line
