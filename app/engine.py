"""Thin Python adapter around the Rust signal engine."""

from __future__ import annotations

import json

from .gameclock import game_progress
from .lines import quote_line_side
from .models import GameState, Quote, Signal, classify_source

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
                 max_age_seconds: float = 20):
        self.confidence_threshold = confidence_threshold
        self.edge_threshold = edge_threshold
        self.max_age_seconds = max_age_seconds

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
        return {
            "market": q.market,
            "outcome": q.outcome,
            "probability": q.probability,
            "source": q.source,
            "observed_at": q.observed_at.timestamp(),
            "bid": q.bid,
            "ask": q.ask,
            "source_weight": weight,
            "is_exchange": is_exchange,
            "decimal_odds": q.decimal_odds,
            "liquidity": q.liquidity,
            "point": point,
            "side": side,
        }

