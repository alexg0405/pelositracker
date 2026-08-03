"""Inductive Venn-Abers intervals over the existing consensus scores.

The confidence question this answers is the one in
docs/predictive-direction-2026-07-31.md: *be more confident on some lines and
less on others* is an interval problem, not a point-estimate problem. A
Venn-Abers predictor wraps any scoring rule as a post-hoc layer — the
consensus stays the fixed baseline — and returns a probability interval whose
width is the per-contract confidence measure. Per-subpopulation fits (line
type × game stage) turn "trust moneyline more than spreads" into a measured
statement.

Strictly shadow: nothing here touches probability, edge, entry gates, or the
execution lanes. The operational rule it enables is a refusal — a contract
whose interval cannot be distinguished from its fee-implied break-even is not
a trade, regardless of the point estimate.

Implementation notes: this is the inductive variant (IVAP). For each query
score the isotonic (PAV) fit is recomputed twice with the query appended under
each hypothetical label; the two fitted values at the query are the interval.
That is O(n log n) per query, which is the right trade at this codebase's
sample sizes (tens to low hundreds of settled positions) and keeps the code
verifiable against the PAV already tested in app/backtest.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .polymarket_us_trading import _fee_implied_edge_floor

MIN_GROUP_PAIRS = 8


@dataclass(frozen=True, slots=True)
class VennAbersInterval:
    lower: float
    upper: float

    @property
    def width(self) -> float:
        return self.upper - self.lower

    @property
    def point(self) -> float:
        """Log-loss-optimal merge of the two Venn probabilities."""
        denominator = 1.0 - self.lower + self.upper
        if denominator <= 0:
            return 0.5
        return self.upper / denominator


class VennAbersPredictor:
    """Interval predictor fitted on (score, settled 0/1 label) pairs."""

    def __init__(
        self,
        scores: Iterable[float],
        labels: Iterable[float],
    ) -> None:
        scores = [float(score) for score in scores]
        labels = [float(label) for label in labels]
        if len(scores) != len(labels):
            raise ValueError("scores and labels must have the same length")
        for label in labels:
            if label not in (0.0, 1.0):
                raise ValueError("labels must be 0 or 1")
        self._pairs = sorted(zip(scores, labels))

    def __len__(self) -> int:
        return len(self._pairs)

    def interval(self, score: float) -> VennAbersInterval:
        """Return the (p0, p1) Venn-Abers interval for ``score``."""
        if not self._pairs:
            return VennAbersInterval(0.0, 1.0)
        score = float(score)
        fitted = [
            self._fitted_at_query(score, hypothetical)
            for hypothetical in (0.0, 1.0)
        ]
        return VennAbersInterval(min(fitted), max(fitted))

    def _fitted_at_query(self, score: float, hypothetical: float) -> float:
        # Isotonic regression requires tied scores to be pooled before PAV;
        # otherwise the within-tie label order fabricates block boundaries
        # inside a group that must share one fitted value.
        totals: dict[float, list[float]] = {}
        for pair_score, label in [*self._pairs, (score, hypothetical)]:
            entry = totals.setdefault(pair_score, [0.0, 0.0])
            entry[0] += label
            entry[1] += 1.0
        ordered_scores = sorted(totals)
        blocks: list[list[float]] = []  # [label_sum, count, last_group_index]
        for group_index, group_score in enumerate(ordered_scores):
            label_sum, count = totals[group_score]
            block = [label_sum, count, float(group_index)]
            while blocks and (
                blocks[-1][0] / blocks[-1][1] >= block[0] / block[1]
            ):
                previous = blocks.pop()
                block[0] += previous[0]
                block[1] += previous[1]
            blocks.append(block)
        query_group = ordered_scores.index(score)
        for label_sum, count, last_group_index in blocks:
            if query_group <= last_group_index:
                return label_sum / count
        return blocks[-1][0] / blocks[-1][1]


def actionability(
    interval: VennAbersInterval,
    entry_price: float,
    *,
    fee_margin: float = 1.0,
) -> dict[str, Any]:
    """Judge an interval against the fee-implied break-even at this price.

    ``actionable`` requires the *entire* interval to clear the round-trip fee
    floor; ``refused`` means even the optimistic end cannot; anything between
    is ``indistinguishable_from_break_even`` — not a trade either, per the
    refusal rule this layer exists to enable.
    """
    floor = _fee_implied_edge_floor(entry_price, fee_margin)
    if floor is None:
        return {
            "status": "unpriceable",
            "reason": "entry price outside the open interval (0, 1)",
        }
    lower_edge = interval.lower - entry_price
    upper_edge = interval.upper - entry_price
    if lower_edge > floor:
        status = "actionable"
    elif upper_edge <= floor:
        status = "refused"
    else:
        status = "indistinguishable_from_break_even"
    return {
        "status": status,
        "fee_floor": round(floor, 6),
        "lower_edge": round(lower_edge, 6),
        "upper_edge": round(upper_edge, 6),
        "interval_width": round(interval.width, 6),
    }


def _pair_score(pair: Mapping[str, Any]) -> float | None:
    """Model-implied probability at entry: price plus the recorded edge."""
    price = pair.get("entry_price")
    edge = pair.get("entry_signal_edge")
    if price is None or edge is None:
        return None
    score = float(price) + float(edge)
    return min(1.0, max(0.0, score))


def _group_key(pair: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(pair.get("market_type") or "unknown"),
        str(pair.get("stage") or "unknown"),
    )


def _brier(probabilities: list[float], labels: list[float]) -> float:
    return sum(
        (probability - label) ** 2
        for probability, label in zip(probabilities, labels)
    ) / len(labels)


def _group_report(pairs: list[Mapping[str, Any]]) -> dict[str, Any]:
    scores = [_pair_score(pair) for pair in pairs]
    labels = [float(pair["label"]) for pair in pairs]
    prices = [float(pair["entry_price"]) for pair in pairs]

    # Leave-one-out: each position is judged by a predictor fitted on the
    # rest, so the report never grades a pair with itself in the fit.
    intervals: list[VennAbersInterval] = []
    points: list[float] = []
    statuses: list[str] = []
    for index in range(len(pairs)):
        rest_scores = scores[:index] + scores[index + 1:]
        rest_labels = labels[:index] + labels[index + 1:]
        predictor = VennAbersPredictor(rest_scores, rest_labels)
        interval = predictor.interval(scores[index])
        intervals.append(interval)
        points.append(interval.point)
        statuses.append(actionability(interval, prices[index])["status"])

    widths = sorted(interval.width for interval in intervals)
    count = len(widths)
    return {
        "pairs": count,
        "events": len({pair.get("event_id") for pair in pairs}),
        "settled_rate": round(sum(labels) / count, 4),
        "mean_interval_width": round(sum(widths) / count, 4),
        "median_interval_width": round(widths[count // 2], 4),
        "actionable_fraction": round(
            statuses.count("actionable") / count, 4
        ),
        "refused_fraction": round(statuses.count("refused") / count, 4),
        "indistinguishable_fraction": round(
            statuses.count("indistinguishable_from_break_even") / count, 4
        ),
        "loo_brier_calibrated_point": round(_brier(points, labels), 4),
        "loo_brier_market_price": round(_brier(prices, labels), 4),
        "calibrated_beats_price": _brier(points, labels)
        < _brier(prices, labels),
    }


def shadow_report(
    pairs: Iterable[Mapping[str, Any]],
    *,
    min_group_pairs: int = MIN_GROUP_PAIRS,
) -> dict[str, Any]:
    """Per-(line, stage) interval report over settled calibration pairs.

    Input rows are the counterfactual tool's ``--emit-pairs`` records. Groups
    smaller than ``min_group_pairs`` are reported as insufficient rather than
    fitted: a two-point isotonic fit is not evidence.
    """
    usable: list[Mapping[str, Any]] = []
    skipped_missing_score = 0
    for pair in pairs:
        if _pair_score(pair) is None or pair.get("label") is None:
            skipped_missing_score += 1
            continue
        usable.append(pair)

    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for pair in usable:
        groups.setdefault(_group_key(pair), []).append(pair)

    reported = []
    insufficient = []
    for (market_type, stage), group_pairs in sorted(groups.items()):
        if len(group_pairs) < min_group_pairs:
            insufficient.append(
                {
                    "market_type": market_type,
                    "stage": stage,
                    "pairs": len(group_pairs),
                    "required": min_group_pairs,
                }
            )
            continue
        entry = _group_report(group_pairs)
        entry["market_type"] = market_type
        entry["stage"] = stage
        reported.append(entry)
    reported.sort(key=lambda item: -int(item["pairs"]))

    overall = (
        _group_report(usable) if len(usable) >= min_group_pairs else None
    )
    return {
        "pairs": len(usable),
        "skipped_missing_score": skipped_missing_score,
        "overall": overall,
        "groups": reported,
        "insufficient_groups": insufficient,
        "caveats": [
            "Shadow measurement only: nothing here alters consensus, edge, "
            "entry gates, or execution.",
            "Pairs come from positions the policy chose to enter; selection "
            "bias is unmeasured.",
            "Event outcomes within a game are correlated; per-pair counts "
            "overstate independent evidence. Judge groups by events.",
            "Interval validity is finite-sample but the leave-one-out Brier "
            "comparison is descriptive, not a significance claim.",
        ],
    }
