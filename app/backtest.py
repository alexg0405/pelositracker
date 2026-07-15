"""Offline evaluation metrics over the paper-bet ledger.

Pure functions on lists of bet dicts (as returned by Ledger.all_bets). The
headline metric is CLV: beating the closing line is the strongest available
evidence of a real edge, and it needs no settlement. Brier / log-loss /
calibration are reported over settled bets and always alongside a market
baseline (using the entry executable price as the prediction), because a
model is only interesting if it beats the price you could have taken.
"""
from __future__ import annotations

import math

_EPS = 1e-6


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def clv_summary(bets: list[dict]) -> dict:
    """CLV = closing_fair_prob - entry_executable, over bets with a close."""
    clvs = [b["clv"] for b in bets if b.get("clv") is not None]
    if not clvs:
        return {"n": 0, "mean_clv": None, "median_clv": None, "beat_close_rate": None}
    ordered = sorted(clvs)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "n": len(clvs),
        "mean_clv": _mean(clvs),
        "median_clv": median,
        "beat_close_rate": sum(1 for c in clvs if c > 0) / len(clvs),
    }


def _settled(bets: list[dict]) -> list[dict]:
    return [b for b in bets if b.get("settled_result") is not None]


def brier_score(bets: list[dict], prob_key: str = "entry_fair_prob") -> float | None:
    rows = _settled(bets)
    if not rows:
        return None
    return _mean([(b[prob_key] - b["settled_result"]) ** 2 for b in rows])


def log_loss(bets: list[dict], prob_key: str = "entry_fair_prob") -> float | None:
    rows = _settled(bets)
    if not rows:
        return None
    total = 0.0
    for b in rows:
        p = min(max(b[prob_key], _EPS), 1 - _EPS)
        y = b["settled_result"]
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(rows)


def reliability_bins(bets: list[dict], prob_key: str = "entry_fair_prob", n_bins: int = 10) -> list[dict]:
    """Group settled bets by predicted probability for a reliability diagram."""
    rows = _settled(bets)
    bins: list[dict] = []
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        # include the right edge in the final bin
        members = [
            b for b in rows
            if lo <= b[prob_key] < hi or (i == n_bins - 1 and b[prob_key] == hi)
        ]
        if not members:
            continue
        bins.append({
            "lo": lo,
            "hi": hi,
            "count": len(members),
            "mean_predicted": _mean([b[prob_key] for b in members]),
            "empirical_rate": _mean([b["settled_result"] for b in members]),
        })
    return bins


def expected_calibration_error(bins: list[dict]) -> float | None:
    """Count-weighted mean gap between predicted probability and outcome rate."""
    total = sum(b["count"] for b in bins)
    if not total:
        return None
    return sum(b["count"] * abs(b["mean_predicted"] - b["empirical_rate"]) for b in bins) / total


def summary(bets: list[dict]) -> dict:
    """Full report: CLV, model vs market calibration, reliability, ECE."""
    settled = _settled(bets)
    bins = reliability_bins(bets)
    return {
        "n_bets": len(bets),
        "n_settled": len(settled),
        "clv": clv_summary(bets),
        "model": {
            "brier": brier_score(bets, "entry_fair_prob"),
            "log_loss": log_loss(bets, "entry_fair_prob"),
            "ece": expected_calibration_error(bins),
        },
        "market_baseline": {
            # The price you could have taken, as a prediction — the bar to beat.
            "brier": brier_score(bets, "entry_executable"),
            "log_loss": log_loss(bets, "entry_executable"),
        },
        "reliability": bins,
    }
