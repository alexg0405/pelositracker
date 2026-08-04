"""Venn-Abers intervals: validity, monotonicity, and the fee refusal rule."""
import itertools
import random

import pytest

from app.venn_abers import (
    VennAbersInterval,
    VennAbersPredictor,
    actionability,
    shadow_report,
)


def _calibrated_sample(count_per_level: int) -> tuple[list[float], list[float]]:
    """Deterministic sample whose empirical rates equal their scores."""
    scores: list[float] = []
    labels: list[float] = []
    for level, rate in ((0.2, 0.2), (0.5, 0.5), (0.8, 0.8)):
        positives = round(count_per_level * rate)
        for index in range(count_per_level):
            scores.append(level)
            labels.append(1.0 if index < positives else 0.0)
    return scores, labels


def test_interval_brackets_the_empirical_rate_when_calibrated():
    scores, labels = _calibrated_sample(20)
    predictor = VennAbersPredictor(scores, labels)
    for level in (0.2, 0.5, 0.8):
        interval = predictor.interval(level)
        assert interval.lower <= level <= interval.upper
        assert interval.width < 0.2


def test_interval_orders_lower_below_upper_and_point_between():
    scores, labels = _calibrated_sample(10)
    predictor = VennAbersPredictor(scores, labels)
    for query in (0.1, 0.35, 0.62, 0.9):
        interval = predictor.interval(query)
        assert interval.lower <= interval.upper
        assert interval.lower <= interval.point <= interval.upper


def test_interval_bounds_are_monotone_in_the_score():
    rng = random.Random(5)
    scores = [rng.random() for _ in range(60)]
    labels = [1.0 if rng.random() < score else 0.0 for score in scores]
    predictor = VennAbersPredictor(scores, labels)
    queries = [round(0.05 + 0.09 * step, 2) for step in range(10)]
    intervals = [predictor.interval(query) for query in queries]
    for previous, current in itertools.pairwise(intervals):
        assert current.lower >= previous.lower - 1e-9
        assert current.upper >= previous.upper - 1e-9


def test_interval_width_shrinks_with_more_evidence():
    small = VennAbersPredictor(*_calibrated_sample(5))
    large = VennAbersPredictor(*_calibrated_sample(50))
    assert large.interval(0.5).width < small.interval(0.5).width


def test_empty_fit_returns_full_uncertainty():
    predictor = VennAbersPredictor([], [])
    interval = predictor.interval(0.4)
    assert (interval.lower, interval.upper) == (0.0, 1.0)


def test_rejects_labels_outside_binary():
    with pytest.raises(ValueError, match="labels must be 0 or 1"):
        VennAbersPredictor([0.5], [0.4])
    with pytest.raises(ValueError, match="same length"):
        VennAbersPredictor([0.5], [])


def test_actionability_applies_the_fee_implied_floor():
    # 45c contract: round-trip floor = 0.10 * 0.45 * 0.55 = 0.02475.
    clear = actionability(VennAbersInterval(0.50, 0.52), 0.45)
    assert clear["status"] == "actionable"
    hopeless = actionability(VennAbersInterval(0.44, 0.46), 0.45)
    assert hopeless["status"] == "refused"
    straddle = actionability(VennAbersInterval(0.46, 0.49), 0.45)
    assert straddle["status"] == "indistinguishable_from_break_even"
    assert straddle["fee_floor"] == pytest.approx(0.02475)
    unpriceable = actionability(VennAbersInterval(0.4, 0.6), 0.0)
    assert unpriceable["status"] == "unpriceable"


def _pair(
    market_type: str,
    stage: str,
    price: float,
    edge: float,
    label: int,
    event_id: str,
) -> dict:
    return {
        "market_type": market_type,
        "stage": stage,
        "entry_price": price,
        "entry_signal_edge": edge,
        "label": label,
        "event_id": event_id,
    }


def test_shadow_report_groups_by_line_and_stage():
    rng = random.Random(11)
    pairs = []
    # Moneyline early: a genuinely informative edge.
    for index in range(24):
        price = 0.30 + 0.02 * (index % 5)
        edge = 0.10
        label = 1 if rng.random() < price + edge else 0
        pairs.append(
            _pair("moneyline", "early", price, edge, label, f"event-{index % 8}")
        )
    # Spread late: only three pairs, below the evidence bar.
    pairs.extend(
        _pair("spread", "late", 0.4, 0.05, 1, f"spread-{index}")
        for index in range(3)
    )
    # A pair with no recorded edge cannot be scored.
    pairs.append(
        {
            "market_type": "total",
            "stage": "early",
            "entry_price": 0.4,
            "entry_signal_edge": None,
            "label": 1,
            "event_id": "x",
        }
    )

    report = shadow_report(pairs)
    assert report["pairs"] == 27
    assert report["skipped_missing_score"] == 1
    assert [
        (group["market_type"], group["stage"]) for group in report["groups"]
    ] == [("moneyline", "early")]
    assert report["insufficient_groups"] == [
        {"market_type": "spread", "stage": "late", "pairs": 3, "required": 8}
    ]
    moneyline = report["groups"][0]
    assert moneyline["pairs"] == 24
    assert moneyline["events"] == 8
    assert 0.0 <= moneyline["mean_interval_width"] <= 1.0
    assert (
        moneyline["actionable_fraction"]
        + moneyline["refused_fraction"]
        + moneyline["indistinguishable_fraction"]
    ) == pytest.approx(1.0)
    assert report["caveats"]


def test_shadow_report_calibrated_point_beats_a_miscalibrated_price():
    # The market price is systematically 15 points below the true rate, and
    # the recorded edge captures that gap: calibration should beat price.
    # Labels are exact (30 of 60) so the comparison is deterministic.
    pairs = [
        _pair(
            "moneyline", "early", 0.35, 0.15,
            1 if index % 2 == 0 else 0, f"event-{index % 15}",
        )
        for index in range(60)
    ]
    report = shadow_report(pairs)
    group = report["groups"][0]
    assert group["calibrated_beats_price"] is True
    assert group["loo_brier_calibrated_point"] < group["loo_brier_market_price"]
