from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, pstdev

from .models import GameState, Quote, Signal


class SignalEngine:
    """Conservative, explainable signal gate; not a calibrated prediction model."""

    def __init__(self, confidence_threshold: float = 72, edge_threshold: float = 0.035,
                 max_age_seconds: float = 20):
        self.confidence_threshold = confidence_threshold
        self.edge_threshold = edge_threshold
        self.max_age_seconds = max_age_seconds

    @staticmethod
    def _age_seconds(observed_at: datetime) -> float:
        now = datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        return max(0.0, (now - observed_at).total_seconds())

    @staticmethod
    def _momentum(states: list[GameState], market: str, outcome: str,
                  away_outcome: str = "away") -> tuple[float, str]:
        if market.lower() not in {"h2h", "moneyline", "winner"}:
            return 0.0, "momentum adjustment not used for this market type"
        if len(states) < 2:
            return 0.0, "insufficient score history"
        recent = sorted(states, key=lambda x: x.observed_at)[-6:]
        first, last = recent[0], recent[-1]
        home_run = last.home_score - first.home_score
        away_run = last.away_score - first.away_score
        direction = home_run - away_run
        if outcome.casefold() in {"away", away_outcome.casefold()}:
            direction *= -1
        # Momentum is deliberately capped: markets usually incorporate score changes first.
        adjustment = max(-0.06, min(0.06, direction * 0.008))
        return adjustment, f"recent scoring differential {direction:+.0f}"

    @staticmethod
    def _fair_consensus(quotes: list[Quote]) -> tuple[float, float, int]:
        """Average source-level probabilities after binary-market normalization."""
        by_source: dict[str, list[Quote]] = defaultdict(list)
        for quote in quotes:
            by_source[quote.source].append(quote)
        fair_values: list[float] = []
        target = quotes[0].outcome
        for source_quotes in by_source.values():
            target_quotes = [q for q in source_quotes if q.outcome == target]
            if not target_quotes:
                continue
            target_p = target_quotes[-1].probability
            total = sum(max(0.001, q.probability) for q in source_quotes)
            fair_values.append(target_p / total if total > 1.01 else target_p)
        if not fair_values:
            return 0.5, 1.0, 0
        dispersion = pstdev(fair_values) if len(fair_values) > 1 else 0.08
        return mean(fair_values), dispersion, len(fair_values)

    def evaluate(self, event_id: str, quotes: list[Quote], states: list[GameState],
                 away_outcome: str = "away") -> list[Signal]:
        if not quotes:
            return []
        freshest_by_key: dict[tuple[str, str, str], Quote] = {}
        for q in quotes:
            key = (q.market, q.outcome, q.source)
            if key not in freshest_by_key or q.observed_at > freshest_by_key[key].observed_at:
                freshest_by_key[key] = q
        current = list(freshest_by_key.values())
        signals: list[Signal] = []
        for market, outcome in sorted({(q.market, q.outcome) for q in current}):
            same_market = [q for q in current if q.market == market]
            target_quotes = [q for q in same_market if q.outcome == outcome]
            fair, dispersion, sources = self._fair_consensus(
                [q for q in same_market if q.outcome == outcome or q.outcome != outcome]
            )
            # Compute target fair probability directly per source to avoid target ambiguity.
            source_fairs = []
            for source in {q.source for q in same_market}:
                source_q = [q for q in same_market if q.source == source]
                target_q = next((q for q in source_q if q.outcome == outcome), None)
                if target_q:
                    total = sum(q.probability for q in source_q)
                    source_fairs.append(target_q.probability / total if total > 1.01 else target_q.probability)
            if source_fairs:
                fair = mean(source_fairs)
                dispersion = pstdev(source_fairs) if len(source_fairs) > 1 else 0.08
                sources = len(source_fairs)
            momentum, momentum_reason = self._momentum(states, market, outcome, away_outcome)
            model_p = max(0.01, min(0.99, fair + momentum))
            best = min(target_quotes, key=lambda q: q.executable_probability)
            executable = best.executable_probability
            edge = model_p - executable
            age = self._age_seconds(best.observed_at)
            spread = (best.ask - best.bid) if best.ask is not None and best.bid is not None else 0.04

            freshness_score = max(0.0, 1 - age / self.max_age_seconds)
            agreement_score = max(0.0, 1 - dispersion / 0.12)
            source_score = min(1.0, sources / 3)
            spread_score = max(0.0, 1 - spread / 0.12)
            edge_stability = min(1.0, max(0.0, edge) / max(self.edge_threshold * 2, 0.001))
            confidence = 100 * (0.28 * freshness_score + 0.24 * agreement_score +
                                0.18 * source_score + 0.15 * spread_score + 0.15 * edge_stability)

            blockers = []
            if age > self.max_age_seconds:
                blockers.append(f"quote stale ({age:.0f}s)")
            if sources < 2:
                blockers.append("fewer than 2 independent price sources")
            if spread > 0.08:
                blockers.append(f"wide executable spread ({spread:.1%})")
            if edge < self.edge_threshold:
                blockers.append(f"edge {edge:.1%} below {self.edge_threshold:.1%} threshold")
            if confidence < self.confidence_threshold:
                blockers.append(f"signal quality {confidence:.0f} below {self.confidence_threshold:.0f}")
            action = "WATCH" if blockers else "PAPER_BET"
            reasons = [momentum_reason, f"{sources} price source(s), dispersion {dispersion:.1%}",
                       f"best executable probability {executable:.1%} via {best.source}"] + blockers
            signals.append(Signal(event_id, market, outcome, model_p, executable, edge,
                                  round(confidence, 1), action, reasons, quote_source=best.source))
        return sorted(signals, key=lambda s: (s.action == "PAPER_BET", s.edge), reverse=True)
