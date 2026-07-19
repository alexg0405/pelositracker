"""Thin Python adapter around the Rust signal engine."""

from __future__ import annotations

import json

from .gameclock import game_progress
from .lines import is_spread_market, is_total_market, quote_line_side
from .models import GameState, Quote, Signal, canonical_source, classify_source

try:
    from ._native_engine import evaluate_json
except ImportError as exc:  # pragma: no cover - exercised only before native build
    raise ImportError(
        "The Rust engine is not built. Run: .\\.venv\\Scripts\\python.exe -m "
        "maturin develop --release"
    ) from exc


class SignalEngine:
    """Python-facing configuration wrapper for the Rust recommendation engine."""

    def __init__(self, confidence_threshold: float = 72, edge_threshold: float = 0.035,
                 max_age_seconds: float = 20, kelly_fraction: float = 0.25,
                 edge_z: float = 1.0):
        self.confidence_threshold = confidence_threshold
        self.edge_threshold = edge_threshold
        self.max_age_seconds = max_age_seconds
        self.kelly_fraction = kelly_fraction
        self.edge_z = edge_z

    def evaluate(self, event_id: str, quotes: list[Quote], states: list[GameState],
                 away_outcome: str = "away", sport: str = "", home_outcome: str = "",
                 pregame_spread: float | None = None,
                 pregame_total: float | None = None) -> list[Signal]:
        request = {
            "event_id": event_id,
            "confidence_threshold": self.confidence_threshold,
            "edge_threshold": self.edge_threshold,
            "max_age_seconds": self.max_age_seconds,
            "away_outcome": away_outcome,
            "sport": sport or None,
            "pregame_spread": pregame_spread,
            "pregame_total": pregame_total,
            "edge_z": self.edge_z,
            "kelly_fraction": self.kelly_fraction,
            "quotes": [
                self._quote_payload(q, home_outcome, away_outcome)
                for q in quotes
            ],
            "states": [
                self._state_payload(s, sport)
                for s in states
            ],
        }
        results = json.loads(evaluate_json(json.dumps(request, separators=(",", ":"))))
        return [Signal(**result) for result in results]

    @staticmethod
    def _state_payload(s: GameState, sport: str) -> dict:
        _, fraction_remaining = game_progress(sport, s.period, s.clock)
        return {
            "home_score": s.home_score,
            "away_score": s.away_score,
            "observed_at": s.observed_at.timestamp(),
            "fraction_remaining": fraction_remaining,
        }

    @staticmethod
    def _quote_payload(q: Quote, home_outcome: str = "", away_outcome: str = "") -> dict:
        weight, is_exchange = classify_source(q.source)
        point, side = quote_line_side(q.market, q.outcome, home_outcome, away_outcome)
        market_key, outcome_key = SignalEngine._comparison_keys(
            q.market, q.outcome, home_outcome, away_outcome, point, side
        )
        return {
            "market": q.market,
            "outcome": q.outcome,
            "comparison_market": market_key,
            "comparison_outcome": outcome_key,
            "comparison_source": SignalEngine._source_key(q.source),
            "probability": q.probability,
            "source": q.source,
            "observed_at": q.observed_at.timestamp(),
            "bid": q.bid,
            "ask": q.ask,
            "source_weight": weight,
            "is_exchange": is_exchange,
            "decimal_odds": q.decimal_odds,
            "liquidity": q.liquidity,
            "ask_size": q.ask_size,
            "point": point,
            "side": side,
        }

    @staticmethod
    def _source_key(source: str) -> str:
        """Canonicalize one underlying book across direct and aggregator feeds."""
        return canonical_source(source)

    @staticmethod
    def _comparison_keys(market: str, outcome: str, home: str, away: str,
                         point: float | None, side: str | None) -> tuple[str, str]:
        """Return stable cross-provider keys without changing display labels.

        The line is part of the market identity. Grouping every alternate
        spread/total together makes de-vigging treat many unrelated two-way
        books as one giant market and can inflate overround above 500%.
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

