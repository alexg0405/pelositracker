"""Event-balanced execution-policy research for managed trade history.

This module only fits execution filters around the established engine. It never
replaces or changes probability, edge, signal-quality, calibration, or engine
gate calculations. Recommendations are chronological, grouped by event, and
fail closed when later-event evidence is too small or uncertain.
"""
from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import json
import math
import random
import statistics
from typing import Any, Iterable, Mapping, Sequence


ADVISOR_VERSION = "execution-policy-advisor-v5-candidate-population"
ADVISOR_OBJECTIVES = {
    "protect_profit": {
        "label": "Protect profit",
        "description": "Prefer after-cost return, event consistency, and lower drawdown.",
    },
    "balanced": {
        "label": "Balanced",
        "description": "Balance after-cost return, event consistency, and qualified frequency.",
    },
    "more_trades": {
        "label": "Seek more trades",
        "description": "Increase qualified frequency without removing bounded risk controls.",
    },
}
ADVISOR_TUNABLE_FIELDS = (
    "allowed_market_types",
    "min_edge",
    "max_edge",
    "min_signal_quality",
    "min_reference_sources",
    "min_entry_price",
    "max_entry_price",
    "max_entries_per_event_per_hour",
    "min_mlb_fraction_remaining",
    "max_orders_per_hour",
    "candidate_cooldown_seconds",
)
MIN_CLOSED_TRADES = 40
MIN_INDEPENDENT_EVENTS = 20
MIN_TEST_TRADES = 8
MIN_TEST_EVENTS = 5
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_CONFIDENCE = 0.90

# Support thresholds for a single field's marginal evidence. These are looser
# than the joint-grid gates above because a marginal sweep compares far fewer
# hypotheses, but they are still stated as whole events rather than trades.
FIELD_DIRECTIONAL_EVENTS = 10
FIELD_DIRECTIONAL_TRADES = 20

BASELINE_CHEAT_SHEET_VERSION = "baseline-cheat-sheet-v1-2026-07"

# How each policy field can honestly be estimated from retained evidence.
#
#   grid_search     the joint optimizer already scores it against realized P/L
#   marginal        sweep the field alone, holding the other filters fixed
#   excursion       identifiable only from retained peak-excursion evidence,
#                   and only in the tightening direction
#   not_identifiable  changing it changes the price path that produced the
#                   realized P/L, so retained outcomes cannot score it
#
# Whether a marginal field is *measurable* is a property of the retained data,
# not a fixed attribute of the field: a column added by a later migration leaves
# older trades unmeasurable, and the sweep reports that count and falls back to
# the versioned baseline rather than scoring a biased subset.
ENTRY_FIELD = "entry"
EXIT_FIELD = "exit"
PACING_FIELD = "pacing"
ADAPTIVE_FIELD = "adaptive"

POLICY_FIELD_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "field": "min_edge", "group": ENTRY_FIELD, "mode": "grid_search",
        "label": "Minimum edge", "unit": "fraction",
    },
    {
        "field": "max_edge", "group": ENTRY_FIELD, "mode": "grid_search",
        "label": "Maximum edge (anomaly ceiling)", "unit": "fraction",
    },
    {
        "field": "min_signal_quality", "group": ENTRY_FIELD,
        "mode": "grid_search", "label": "Minimum signal quality", "unit": "score",
    },
    {
        "field": "min_reference_sources", "group": ENTRY_FIELD,
        "mode": "grid_search", "label": "Minimum independent references",
        "unit": "count",
    },
    {
        "field": "min_entry_price", "group": ENTRY_FIELD, "mode": "grid_search",
        "label": "Entry price floor", "unit": "fraction",
    },
    {
        "field": "max_entry_price", "group": ENTRY_FIELD, "mode": "grid_search",
        "label": "Entry price ceiling", "unit": "fraction",
    },
    {
        "field": "max_signal_quality", "group": ENTRY_FIELD, "mode": "marginal",
        "label": "Maximum signal quality", "unit": "score",
        "row_key": "signal_quality", "direction": "upper",
        "values": (85.0, 90.0, 95.0, 100.0),
        "baseline": 100.0,
        "baseline_reason": (
            "Signal quality is a reliability score, not a win probability, and "
            "the July 2026 audit found it was not monotonic with realized "
            "return. Leave the ceiling open unless local evidence supports it."
        ),
    },
    {
        "field": "min_source_agreement", "group": ENTRY_FIELD, "mode": "marginal",
        "label": "Minimum source agreement", "unit": "score",
        "row_key": "source_agreement", "direction": "lower",
        "values": (0.0, 40.0, 55.0, 70.0),
        "baseline": 0.0,
        "baseline_reason": (
            "Source agreement was only retained from policy v11 onward, so most "
            "closed trades cannot score it. Zero preserves existing behaviour."
        ),
    },
    {
        "field": "max_signal_age_seconds", "group": ENTRY_FIELD,
        "mode": "marginal", "label": "Maximum retained signal age",
        "unit": "seconds", "row_key": "signal_age_seconds", "direction": "upper",
        "values": (15.0, 30.0, 60.0, 120.0),
        "baseline": 120.0,
        "baseline_reason": (
            "120s matches the previous hard-coded staleness bound, so it is the "
            "behaviour-preserving default until local evidence separates the "
            "age bands."
        ),
    },
    {
        "field": "entry_confirmation_readings", "group": ENTRY_FIELD,
        "mode": "marginal", "label": "Distinct qualifying readings",
        "unit": "count", "row_key": "entry_confirmation_readings",
        "direction": "lower", "values": (1, 2, 3),
        "baseline": 2,
        "baseline_reason": (
            "Two readings require the signal to persist across a refresh "
            "without materially raising latency. One reproduces the immediate "
            "entry behaviour that produced the existing sample."
        ),
    },
    {
        "field": "max_confirmation_price_drift", "group": ENTRY_FIELD,
        "mode": "marginal", "label": "Maximum ask drift while confirming",
        "unit": "fraction", "row_key": "confirmation_price_drift",
        "direction": "upper", "values": (0.01, 0.02, 0.03, 1.0),
        "baseline": 0.02,
        "baseline_reason": (
            "Two cents bounds chasing a worsening ask while leaving room for "
            "ordinary one-tick movement between readings."
        ),
    },
    {
        # Entry spread and depth are retained on the position from policy v12
        # onward. Positions opened before that migration carry no value and are
        # counted as unmeasurable, so this stays on the versioned baseline until
        # enough newer trades exist to score it.
        "field": "max_spread", "group": ENTRY_FIELD, "mode": "marginal",
        "label": "Maximum bid/ask spread", "unit": "fraction",
        "row_key": "spread", "direction": "upper",
        "values": (0.02, 0.03, 0.05, 0.08),
        "baseline": 0.05,
        "baseline_reason": (
            "Five cents bounds round-trip cost without excluding ordinary "
            "in-play books. Entry spread is only retained on positions opened "
            "after the v12 migration, so older trades cannot score it."
        ),
    },
    {
        "field": "min_book_shares", "group": ENTRY_FIELD, "mode": "marginal",
        "label": "Minimum book depth", "unit": "shares",
        "row_key": "book_shares", "direction": "lower",
        "values": (5.0, 10.0, 25.0, 50.0),
        "baseline": 10.0,
        "baseline_reason": (
            "Ten shares keeps a full exit plausible at workstation position "
            "sizes. Entry depth is only retained on positions opened after the "
            "v12 migration, so older trades cannot score it."
        ),
    },
    {
        "field": "max_entries_per_event_per_hour", "group": PACING_FIELD,
        "mode": "grid_search", "label": "Maximum entries per event / hour",
        "unit": "count",
    },
    {
        "field": "max_orders_per_hour", "group": PACING_FIELD,
        "mode": "grid_search", "label": "Maximum entries / hour", "unit": "count",
    },
    {
        "field": "candidate_cooldown_seconds", "group": PACING_FIELD,
        "mode": "grid_search", "label": "Candidate retry cooldown",
        "unit": "seconds",
    },
    {
        "field": "min_mlb_fraction_remaining", "group": PACING_FIELD,
        "mode": "grid_search", "label": "Minimum MLB regulation remaining",
        "unit": "fraction",
    },
    {
        "field": "profit_target", "group": EXIT_FIELD, "mode": "excursion",
        "label": "Meaningful profit target", "unit": "fraction",
        "row_key": "highest_exit_value", "direction": "upper",
        "values": (0.04, 0.06, 0.08, 0.10, 0.15),
        "exit_families": (
            "profit_target", "profit_lock", "trailing_profit_lock",
            "meaningful_profit", "profit_floor_missed_holding",
        ),
        "baseline": 0.08,
        "baseline_reason": (
            "Profit locks were the best-performing exit family in the July 2026 "
            "sample. Eight percent is inside the band that was reached often "
            "enough to matter without demanding a rare move."
        ),
    },
    {
        "field": "minimum_locked_profit", "group": EXIT_FIELD,
        "mode": "not_identifiable", "label": "Minimum retained profit",
        "unit": "fraction", "baseline": 0.02,
        "baseline_reason": (
            "47 losing or push trades had first shown a positive mark. A small "
            "retained floor bounds that give-back without disabling the stop."
        ),
    },
    {
        "field": "trailing_drawdown", "group": EXIT_FIELD,
        "mode": "not_identifiable", "label": "Trailing pullback",
        "unit": "fraction", "baseline": 0.04,
        "baseline_reason": (
            "Trailing locks were positive after cost in the observed sample, "
            "but the observed pullback path depends on the trail that was "
            "actually running."
        ),
    },
    {
        # Retained adverse excursion makes a *tighter* stop identifiable in the
        # same way peak excursion does for a profit target. Widening a stop
        # stays unidentifiable: a position closed at the old stop has no
        # observed continuation.
        "field": "stop_loss", "group": EXIT_FIELD, "mode": "excursion",
        "label": "Stop loss", "unit": "fraction",
        "row_key": "lowest_exit_value", "direction": "lower",
        "values": (0.10, 0.15, 0.20, 0.25),
        "exit_families": (
            "stop_loss", "hard_stop", "catastrophic_stop",
            "confirmed_stop", "volatility_stop",
        ),
        "baseline": 0.20,
        "baseline_reason": (
            "Hard stops were the dominant realized-loss source, but removing or "
            "widening them is not supported: positions that could not recover "
            "leave the comparison at different times. Keep the tail bounded."
        ),
    },
    {
        "field": "exit_edge", "group": EXIT_FIELD, "mode": "not_identifiable",
        "label": "Model-reversal edge", "unit": "fraction", "baseline": 0.0,
        "baseline_reason": (
            "Model-reversal exits were roughly flat overall and their result "
            "depended on line and context rather than on one threshold."
        ),
    },
    {
        "field": "min_hold_minutes", "group": EXIT_FIELD,
        "mode": "not_identifiable", "label": "Minimum hold / fallback confirmation",
        "unit": "minutes", "baseline": 2.0,
        "baseline_reason": (
            "A short fallback hold avoids reacting to a single quote without "
            "delaying a confirmed two-reading profit lock."
        ),
    },
    {
        "field": "adaptive_exit_profile", "group": ADAPTIVE_FIELD,
        "mode": "not_identifiable", "label": "Adaptive exit response",
        "unit": "choice", "baseline": "observe",
        "baseline_reason": (
            "Guarded, Balanced, and Responsive tighten exits from a cold-start "
            "confidence floor before any labeled evidence exists. Observe-only "
            "scores the overlay without letting it act."
        ),
    },
    {
        "field": "stop_confirmation_readings", "group": ADAPTIVE_FIELD,
        "mode": "not_identifiable", "label": "Stop confirmation readings",
        "unit": "count", "baseline": 2,
        "baseline_reason": (
            "Requiring a second reading filters a single bad quote. Scoring it "
            "needs the price path of trades that were stopped under a different "
            "rule, which retained outcomes do not contain."
        ),
    },
)

POLICY_FIELD_CATALOG_BY_NAME = {
    item["field"]: item for item in POLICY_FIELD_CATALOG
}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _market_type(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("_", " ")
    if any(token in text for token in ("moneyline", "money line", "winner", "h2h")):
        return "moneyline"
    if "spread" in text or "run line" in text:
        return "spread"
    if "total" in text or "over/under" in text:
        return "total"
    return text or "unknown"


def _active_hours(timestamps: Iterable[float]) -> float:
    """Estimate active observation time without treating overnight gaps as work."""
    values = sorted(set(float(value) for value in timestamps))
    if not values:
        return 0.0
    if len(values) == 1:
        return 0.25
    seconds = 15 * 60
    for before, after in zip(values, values[1:]):
        seconds += min(max(0.0, after - before), 60 * 60)
    return max(0.25, seconds / 3600.0)


def _enrich_rows(
    source: Iterable[Mapping[str, Any]],
    *,
    timestamp_field: str,
    decision_contexts: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = [dict(row) for row in source]
    rows.sort(key=lambda row: float(row.get(timestamp_field) or 0.0))
    event_windows: dict[str, deque[float]] = defaultdict(deque)
    contract_last: dict[tuple[str, str, str], float] = {}
    for row in rows:
        timestamp = float(row.get(timestamp_field) or 0.0)
        event_id = str(row.get("event_id") or "")
        market_slug = str(row.get("market_slug") or "")
        selection = str(row.get("selection") or row.get("position_side") or "")
        window = event_windows[event_id]
        while window and timestamp - window[0] >= 3600:
            window.popleft()
        retained_count = _finite(row.get("event_entries_60m"))
        row["event_entries_60m"] = (
            max(1, int(retained_count))
            if retained_count is not None
            else len(window) + 1
        )
        window.append(timestamp)
        contract_key = (event_id, market_slug, selection)
        previous = contract_last.get(contract_key)
        row["seconds_since_same_contract"] = (
            timestamp - previous if previous is not None else None
        )
        contract_last[contract_key] = timestamp
        row["market_type"] = _market_type(row.get("market_type"))
        fraction = _finite(row.get("game_fraction_remaining"))
        if fraction is None:
            context = decision_contexts.get(str(row.get("decision_id") or ""))
            fraction = _finite((context or {}).get("fraction_remaining"))
        row["game_fraction_remaining"] = fraction
    return rows


def _matches(row: Mapping[str, Any], settings: Mapping[str, Any]) -> bool:
    edge = _finite(row.get("signal_edge"))
    quality = _finite(row.get("signal_quality"))
    price = _finite(row.get("entry_cost"))
    references = _finite(row.get("reference_sources"))
    if edge is None or quality is None or price is None or references is None:
        return False
    allowed = {
        _market_type(value)
        for value in settings.get("allowed_market_types", ())
    }
    row_market = _market_type(row.get("market_type"))
    if allowed and row_market not in allowed:
        return False
    if not (
        float(settings["min_edge"]) <= edge <= float(settings["max_edge"])
        and quality >= float(settings["min_signal_quality"])
        and references >= int(settings["min_reference_sources"])
        and float(settings["min_entry_price"])
        <= price
        <= float(settings["max_entry_price"])
    ):
        return False
    if int(row.get("event_entries_60m") or 1) > int(
        settings["max_entries_per_event_per_hour"]
    ):
        return False
    cooldown = int(settings["candidate_cooldown_seconds"])
    seconds_since = _finite(row.get("seconds_since_same_contract"))
    if seconds_since is not None and seconds_since < cooldown:
        return False
    min_fraction = float(settings["min_mlb_fraction_remaining"])
    if min_fraction > 0:
        fraction = _finite(row.get("game_fraction_remaining"))
        if fraction is None or fraction < min_fraction:
            return False
    return True


def _performance(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    stake = sum(float(row["cost_basis"]) for row in rows)
    net = sum(float(row["realized_pnl"]) for row in rows)
    wins = sum(float(row["realized_pnl"]) > 1e-9 for row in rows)
    losses = sum(float(row["realized_pnl"]) < -1e-9 for row in rows)
    by_event: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_event[str(row["event_id"])].append(row)
    gross_wins = sum(max(0.0, float(row["realized_pnl"])) for row in rows)
    gross_losses = sum(max(0.0, -float(row["realized_pnl"])) for row in rows)
    event_rois = []
    event_stakes = []
    for event_rows in by_event.values():
        event_stake = sum(float(row["cost_basis"]) for row in event_rows)
        event_net = sum(float(row["realized_pnl"]) for row in event_rows)
        event_stakes.append(event_stake)
        if event_stake > 0:
            event_rois.append(event_net / event_stake)
    equity = 0.0
    peak = 0.0
    maximum_drawdown = 0.0
    for row in sorted(
        rows,
        key=lambda value: float(
            value.get("closed_ts") or value.get("opened_ts") or 0.0
        ),
    ):
        equity += float(row["realized_pnl"])
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, peak - equity)
    return {
        "trades": len(rows),
        "events": len(by_event),
        "stake_usd": round(stake, 4),
        "net_usd": round(net, 4),
        "turnover_roi": net / stake if stake > 0 else None,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / (wins + losses) if wins + losses else None,
        "profit_factor": gross_wins / gross_losses if gross_losses > 0 else None,
        "average_pnl_usd": net / len(rows) if rows else None,
        "median_event_roi": statistics.median(event_rois) if event_rois else None,
        "positive_event_rate": (
            sum(value > 0 for value in event_rois) / len(event_rois)
            if event_rois else None
        ),
        "maximum_event_stake_share": (
            max(event_stakes) / stake if stake > 0 and event_stakes else None
        ),
        "maximum_drawdown_usd": round(maximum_drawdown, 4),
        "maximum_drawdown_per_staked_dollar": (
            maximum_drawdown / stake if stake > 0 else None
        ),
    }


def _event_roi_values(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    by_event: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_event[str(row["event_id"])].append(row)
    values = []
    for event_rows in by_event.values():
        stake = sum(float(row["cost_basis"]) for row in event_rows)
        net = sum(float(row["realized_pnl"]) for row in event_rows)
        if stake > 0:
            values.append(net / stake)
    return values


def _bootstrap(values: list[float]) -> dict[str, Any]:
    if len(values) < 2:
        return {
            "events": len(values),
            "draws": 0,
            "mean_event_roi": values[0] if values else None,
            "lower_95": None,
            "upper_95": None,
            "probability_positive": None,
            "resampling_unit": "event",
        }
    seed_text = json.dumps(
        [round(value, 12) for value in values],
        separators=(",", ":"),
    )
    seed = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:16], 16)
    generator = random.Random(seed)
    estimates = []
    for _ in range(BOOTSTRAP_DRAWS):
        sample = [generator.choice(values) for _ in values]
        estimates.append(sum(sample) / len(sample))
    estimates.sort()
    return {
        "events": len(values),
        "draws": BOOTSTRAP_DRAWS,
        "mean_event_roi": sum(values) / len(values),
        "lower_95": estimates[int(0.025 * (len(estimates) - 1))],
        "upper_95": estimates[int(0.975 * (len(estimates) - 1))],
        "probability_positive": (
            sum(value > 0.0 for value in estimates) / len(estimates)
        ),
        "resampling_unit": "event",
    }


def _chronological_events(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[set[str], set[str]]:
    event_times: dict[str, float] = {}
    for row in rows:
        event_id = str(row["event_id"])
        event_times[event_id] = min(
            event_times.get(event_id, float("inf")),
            float(row["opened_ts"]),
        )
    ordered = sorted(event_times, key=lambda event_id: event_times[event_id])
    if len(ordered) < 4:
        return set(ordered), set()
    split = max(1, min(len(ordered) - 1, int(len(ordered) * 0.7)))
    return set(ordered[:split]), set(ordered[split:])


def _base_candidate_grid(
    current: Mapping[str, Any],
    allowed_scope: Sequence[str],
) -> list[dict[str, Any]]:
    edge_values = sorted({
        round(float(current["min_edge"]), 4),
        0.03, 0.04, 0.06, 0.08, 0.10,
    })
    max_edge_values = sorted({
        round(float(current.get("max_edge", 1.0)), 4),
        0.12, 0.15, 0.20, 0.30,
    })
    quality_values = sorted({
        round(float(current["min_signal_quality"]), 2),
        55.0, 65.0, 75.0, 80.0,
    })
    reference_values = sorted({
        int(current["min_reference_sources"]),
        1,
        2,
    })
    brackets = {
        (
            round(float(current["min_entry_price"]), 4),
            round(float(current["max_entry_price"]), 4),
        ),
        (0.15, 0.85),
        (0.20, 0.80),
        (0.25, 0.75),
    }
    market_sets = {tuple(sorted(allowed_scope))}
    market_sets.update((market,) for market in allowed_scope)
    base = {
        "max_entries_per_event_per_hour": int(
            current.get("max_entries_per_event_per_hour", 3)
        ),
        "min_mlb_fraction_remaining": float(
            current.get("min_mlb_fraction_remaining", 0.0)
        ),
        "candidate_cooldown_seconds": int(
            current.get("candidate_cooldown_seconds", 300)
        ),
    }
    candidates = []
    for minimum in edge_values:
        for maximum in max_edge_values:
            if maximum <= minimum:
                continue
            for quality in quality_values:
                for references in reference_values:
                    for floor, ceiling in sorted(brackets):
                        for markets in sorted(market_sets):
                            candidates.append({
                                **base,
                                "allowed_market_types": list(markets),
                                "min_edge": minimum,
                                "max_edge": maximum,
                                "min_signal_quality": quality,
                                "min_reference_sources": references,
                                "min_entry_price": floor,
                                "max_entry_price": ceiling,
                            })
    return candidates


def _score_candidate(
    objective: str,
    target_trades_per_hour: float,
    train: Mapping[str, Any],
    opportunity_rate: float,
) -> float:
    roi = float(train.get("turnover_roi") or -1.0)
    median_event_roi = float(train.get("median_event_roi") or -1.0)
    positive_event_rate = float(train.get("positive_event_rate") or 0.0)
    drawdown = float(train.get("maximum_drawdown_per_staked_dollar") or 0.0)
    concentration = float(train.get("maximum_event_stake_share") or 1.0)
    trades = int(train.get("trades") or 0)
    frequency_ratio = min(1.5, opportunity_rate / max(0.25, target_trades_per_hour))
    support = min(1.0, trades / 40.0)
    robustness = (
        1.2 * roi
        + 0.7 * median_event_roi
        + 0.35 * positive_event_rate
        + 0.15 * support
        - 0.7 * drawdown
        - 0.25 * concentration
    )
    if objective == "protect_profit":
        return robustness - 0.10 * max(0.0, frequency_ratio - 1.0)
    if objective == "more_trades":
        negative_penalty = min(1.5, abs(min(0.0, roi)) * 5.0)
        return robustness + 0.9 * frequency_ratio - negative_penalty
    return robustness - 0.20 * abs(1.0 - min(1.0, frequency_ratio))


def _evaluate_candidate(
    settings: Mapping[str, Any],
    *,
    rows: Sequence[dict[str, Any]],
    opportunities: Sequence[dict[str, Any]],
    train_events: set[str],
    test_events: set[str],
    opportunity_hours: float,
    objective: str,
    target_trades_per_hour: float,
) -> dict[str, Any] | None:
    train_rows = [
        row for row in rows
        if str(row["event_id"]) in train_events and _matches(row, settings)
    ]
    if len(train_rows) < min(8, max(1, len(rows))):
        return None
    test_rows = [
        row for row in rows
        if str(row["event_id"]) in test_events and _matches(row, settings)
    ]
    qualifying = [row for row in opportunities if _matches(row, settings)]
    rate = len(qualifying) / opportunity_hours if opportunity_hours > 0 else 0.0
    train = _performance(train_rows)
    return {
        "settings": dict(settings),
        "train": train,
        "test": _performance(test_rows),
        "opportunity_rate_per_hour": rate,
        "score": _score_candidate(
            objective, target_trades_per_hour, train, rate
        ),
    }


def _expand_churn_and_stage(
    candidates: Sequence[dict[str, Any]],
    *,
    current: Mapping[str, Any],
    rows: Sequence[dict[str, Any]],
    opportunities: Sequence[dict[str, Any]],
    train_events: set[str],
    test_events: set[str],
    opportunity_hours: float,
    objective: str,
    target_trades_per_hour: float,
) -> list[dict[str, Any]]:
    top = sorted(candidates, key=lambda value: -float(value["score"]))[:20]
    caps = sorted({
        1, 2, 3, int(current.get("max_entries_per_event_per_hour", 3))
    })
    cooldowns = sorted({
        120, 300, 600, int(current.get("candidate_cooldown_seconds", 300))
    })
    stage_values = sorted({
        0.0, 0.25, 0.33,
        round(float(current.get("min_mlb_fraction_remaining", 0.0)), 4),
    })
    expanded = list(candidates)
    for candidate in top:
        for cap in caps:
            for cooldown in cooldowns:
                for stage in stage_values:
                    settings = {
                        **candidate["settings"],
                        "max_entries_per_event_per_hour": cap,
                        "candidate_cooldown_seconds": max(
                            int(current.get("cycle_seconds", 10)), cooldown
                        ),
                        "min_mlb_fraction_remaining": stage,
                    }
                    evaluated = _evaluate_candidate(
                        settings,
                        rows=rows,
                        opportunities=opportunities,
                        train_events=train_events,
                        test_events=test_events,
                        opportunity_hours=opportunity_hours,
                        objective=objective,
                        target_trades_per_hour=target_trades_per_hour,
                    )
                    if evaluated is not None:
                        expanded.append(evaluated)
    return expanded


def _bucket_diagnostics(
    rows: Sequence[dict[str, Any]],
    *,
    key: str,
    buckets: Sequence[tuple[str, float, float]],
) -> list[dict[str, Any]]:
    result = []
    for label, lower, upper in buckets:
        selected = []
        for row in rows:
            value = _finite(row.get(key))
            if value is not None and lower <= value < upper:
                selected.append(row)
        if selected:
            result.append({"label": label, **_performance(selected)})
    return result


def _category_diagnostics(
    rows: Sequence[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "unknown")].append(row)
    result = []
    for label, group in sorted(grouped.items()):
        performance = _performance(group)
        bootstrap = _bootstrap(_event_roi_values(group))
        result.append({
            "label": label,
            **performance,
            "event_block_bootstrap": bootstrap,
            "support": (
                "directional"
                if int(performance["events"]) >= 10
                and int(performance["trades"]) >= 20
                else "sparse"
            ),
        })
    return result


def _opportunity_provenance(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Describe what the qualified-frequency estimate is actually counting.

    Before candidate logging existed, "opportunities" were the fills themselves,
    so a looser candidate policy could only ever re-filter trades that were
    already taken and its estimated frequency was bounded by the frequency that
    produced the data. Rows from the candidate log remove that ceiling; rows
    from the legacy entry journal do not.
    """
    logged = [
        row for row in rows
        if str(row.get("evidence_source") or "") == "candidate_log"
    ]
    unentered = [row for row in logged if not row.get("entered")]
    legacy = len(rows) - len(logged)
    propensity_sources = sorted({
        str(row.get("propensity_source"))
        for row in logged
        if row.get("propensity_source")
    })
    return {
        "total_observations": len(rows),
        "candidate_log_observations": len(logged),
        "unentered_candidate_observations": len(unentered),
        "legacy_entry_only_observations": legacy,
        "propensity_sources": propensity_sources,
        "randomized_exploration": False,
        "off_policy_identified": False,
        "frequency_estimate_basis": (
            "candidate population including rejected contracts"
            if unentered
            else "entered contracts only"
        ),
        "note": (
            "Estimated qualified-per-hour counts contracts the policy declined, "
            "so a looser filter can now show a higher rate than the policy that "
            "produced the data. Rejected contracts still have no observed "
            "return, so no profit estimate is available for them."
            if unentered
            else "Only entered contracts are retained for this range, so the "
            "estimated qualified-per-hour rate cannot exceed the rate of the "
            "policy that produced the data. Collect candidate-log history "
            "before comparing looser filters on frequency."
        ),
    }


def _date_range(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    stamps = [
        value for value in (_finite(row.get(field)) for row in rows)
        if value is not None
    ]
    return {
        "first_ts": min(stamps) if stamps else None,
        "last_ts": max(stamps) if stamps else None,
    }


def _support_level(performance: Mapping[str, Any]) -> str:
    if (
        int(performance.get("events") or 0) >= FIELD_DIRECTIONAL_EVENTS
        and int(performance.get("trades") or 0) >= FIELD_DIRECTIONAL_TRADES
    ):
        return "directional"
    if int(performance.get("trades") or 0) > 0:
        return "sparse"
    return "none"


def _threshold_keeps(
    row: Mapping[str, Any],
    *,
    row_key: str,
    direction: str,
    value: float,
) -> bool | None:
    """Would this candidate still qualify under one threshold value?

    Returns None when the row does not carry the field at all, so a missing
    value is never silently counted as a pass or a fail.
    """
    observed = _finite(row.get(row_key))
    if observed is None:
        return None
    return observed <= value if direction == "upper" else observed >= value


def _marginal_field_evidence(
    spec: Mapping[str, Any],
    *,
    closed_rows: Sequence[dict[str, Any]],
    opportunity_rows: Sequence[dict[str, Any]],
    opportunity_hours: float,
    test_events: set[str],
    current_value: Any,
) -> dict[str, Any]:
    """Sweep one field alone, holding every other selected filter fixed.

    A joint grid over every field would multiply the hypothesis count into the
    tens of thousands and make the later-event check meaningless. A marginal
    sweep answers the narrower, honest question: conditional on the rest of the
    policy, what does this one threshold do to the retained sample?
    """
    row_key = str(spec["row_key"])
    direction = str(spec["direction"])
    options: list[dict[str, Any]] = []
    for value in spec["values"]:
        kept_opportunities = 0
        unknown_opportunities = 0
        for row in opportunity_rows:
            keeps = _threshold_keeps(
                row, row_key=row_key, direction=direction, value=float(value)
            )
            if keeps is None:
                unknown_opportunities += 1
            elif keeps:
                kept_opportunities += 1
        option: dict[str, Any] = {
            "value": value,
            "qualified_per_hour": (
                kept_opportunities / opportunity_hours
                if opportunity_hours > 0 else None
            ),
            "qualified_observations": kept_opportunities,
            "unmeasurable_observations": unknown_opportunities,
        }
        kept_rows = []
        unknown_trades = 0
        for row in closed_rows:
            keeps = _threshold_keeps(
                row, row_key=row_key, direction=direction, value=float(value)
            )
            if keeps is None:
                unknown_trades += 1
            elif keeps:
                kept_rows.append(row)
        later = [
            row for row in kept_rows
            if str(row["event_id"]) in test_events
        ]
        performance = _performance(kept_rows)
        option.update({
            "all_history": performance,
            "later_events": _performance(later),
            "event_block_bootstrap": _bootstrap(_event_roi_values(kept_rows)),
            "date_range": _date_range(kept_rows, "opened_ts"),
            "support": _support_level(performance),
            "unmeasurable_trades": unknown_trades,
        })
        options.append(option)

    measurable = [
        option for option in options
        if option.get("support") == "directional"
    ]
    best = max(
        measurable,
        key=lambda option: float(
            option["all_history"].get("turnover_roi") or -1.0
        ),
        default=None,
    )
    if best is not None:
        basis = "observational"
        suggested = best["value"]
        rationale = (
            f"Best after-cost turnover ROI among swept values on "
            f"{best['all_history']['trades']} trades across "
            f"{best['all_history']['events']} independent events, conditional "
            "on the rest of the selected policy."
        )
    else:
        basis = "baseline_fallback"
        suggested = spec["baseline"]
        rationale = spec["baseline_reason"]
    return {
        "basis": basis,
        "suggested": suggested,
        "current": current_value,
        "options": options,
        "rationale": rationale,
        "scores_realized_pnl": True,
        "measurement_note": (
            None if best is not None else
            "No swept value reached directional support on trades that retain "
            "this field, so the versioned baseline is shown instead of a fitted "
            "value. Check each option's unmeasurable_trades count: a column "
            "added by a later migration leaves older trades unscoreable."
        ),
    }


# Polymarket US publishes a symmetric taker fee of 0.05 * contracts * p * (1-p).
# A counterfactual exit crosses the book, so it pays that fee. Restated here
# rather than imported because the execution module imports this one.
_TAKER_FEE_COEFFICIENT = 0.05


def _counterfactual_exit_roi(entry_cost: float, exit_price: float) -> float:
    """After-fee return on cost basis for a full exit at one known price."""
    if entry_cost <= 0 or not 0 < exit_price < 1:
        return exit_price / entry_cost - 1.0
    fee_per_share = (
        _TAKER_FEE_COEFFICIENT * exit_price * (1.0 - exit_price)
    )
    return (exit_price - fee_per_share) / entry_cost - 1.0


def _excursion_evidence(
    spec: Mapping[str, Any],
    *,
    closed_rows: Sequence[dict[str, Any]],
    current_value: Any,
) -> dict[str, Any]:
    """Score an exit threshold from retained extreme-excursion evidence.

    `highest_exit_value` and `lowest_exit_value` record the best and worst
    executable marks a position reached before it actually closed. If the
    observed path crossed a candidate threshold, an exit there was reachable at
    a known price, so its after-fee return is identifiable.

    Only the tightening direction is identifiable. A threshold looser than the
    rule that actually ran has no observed continuation, because the position
    was already closed. Trades whose path never crossed the candidate keep
    their observed outcome, and trades whose actual exit was caused by a
    *tighter* rule of the same family are excluded rather than assumed.
    """
    direction = str(spec["direction"])
    exit_family = set(spec.get("exit_families") or ())
    rows: list[dict[str, Any]] = []
    for row in closed_rows:
        entry_cost = _finite(row.get("entry_cost"))
        realized = _finite(row.get("realized_pnl"))
        basis_usd = _finite(row.get("cost_basis"))
        extreme = _finite(row.get(str(spec["row_key"])))
        if (
            entry_cost is None or realized is None or basis_usd is None
            or extreme is None or entry_cost <= 0 or basis_usd <= 0
        ):
            continue
        rows.append({
            "excursion": extreme / entry_cost - 1.0,
            "entry_cost": entry_cost,
            "observed_roi": realized / basis_usd,
            "exit_reason": str(row.get("exit_reason") or ""),
            "event_id": str(row.get("event_id") or ""),
        })

    options = []
    for value in spec["values"]:
        threshold = float(value)
        crossed, untouched, ambiguous = [], [], []
        for item in rows:
            reaches = (
                item["excursion"] >= threshold
                if direction == "upper"
                else item["excursion"] <= -threshold
            )
            if reaches:
                crossed.append(item)
            elif exit_family and item["exit_reason"] in exit_family:
                # The actual rule of this family fired before the candidate
                # threshold was reached, so the continuation is unobserved.
                ambiguous.append(item)
            else:
                untouched.append(item)
        exit_price = [
            item["entry_cost"] * (
                1.0 + threshold if direction == "upper" else 1.0 - threshold
            )
            for item in crossed
        ]
        counterfactual = [
            _counterfactual_exit_roi(item["entry_cost"], price)
            for item, price in zip(crossed, exit_price)
        ]
        identifiable = counterfactual + [
            item["observed_roi"] for item in untouched
        ]
        options.append({
            "value": value,
            "trades_with_excursion_evidence": len(rows),
            "crossed_threshold": len(crossed),
            "crossed_share": len(crossed) / len(rows) if rows else None,
            "kept_observed_outcome": len(untouched),
            "unidentifiable_trades": len(ambiguous),
            "counterfactual_roi_of_crossing_trades": (
                sum(counterfactual) / len(counterfactual)
                if counterfactual else None
            ),
            "observed_roi_of_crossing_trades": (
                sum(item["observed_roi"] for item in crossed) / len(crossed)
                if crossed else None
            ),
            "identifiable_mean_roi": (
                sum(identifiable) / len(identifiable) if identifiable else None
            ),
            "identifiable_events": len({
                item["event_id"] for item in crossed + untouched
            }),
        })

    usable = [
        option for option in options
        if option["identifiable_mean_roi"] is not None
        and option["unidentifiable_trades"] == 0
        and option["identifiable_events"] >= FIELD_DIRECTIONAL_EVENTS
        and option["trades_with_excursion_evidence"] >= FIELD_DIRECTIONAL_TRADES
    ]
    best = max(
        usable,
        key=lambda option: float(option["identifiable_mean_roi"]),
        default=None,
    )
    if best is not None:
        basis = "observational"
        suggested = best["value"]
        rationale = (
            f"Best identifiable mean after-fee return across "
            f"{best['trades_with_excursion_evidence']} trades with retained "
            f"excursion evidence over {best['identifiable_events']} events. "
            "Every trade either crossed this threshold at a known price or "
            "kept its observed outcome."
        )
    else:
        basis = "baseline_fallback"
        suggested = spec["baseline"]
        rationale = spec["baseline_reason"]
    return {
        "basis": basis,
        "suggested": suggested,
        "current": current_value,
        "options": options,
        "rationale": rationale,
        "scores_realized_pnl": True,
        "identifiable_direction": "tightening_only",
        "measurement_note": (
            "Marks are sampled once per cycle, so a retained extreme is a lower "
            "bound on the true excursion and a counterfactual exit assumes a "
            "full fill at the threshold price plus the standard taker fee. "
            "`unidentifiable_trades` counts trades whose own exit rule of this "
            "family fired first; when that is above zero the option has no "
            "identifiable comparison and is not eligible to be suggested."
        ),
    }


def _field_recommendations(
    *,
    closed_rows: Sequence[dict[str, Any]],
    opportunity_rows: Sequence[dict[str, Any]],
    opportunity_hours: float,
    test_events: set[str],
    current_policy: Mapping[str, Any],
    grid_settings: Mapping[str, Any],
    grid_status: str,
    grid_train: Mapping[str, Any],
    grid_test: Mapping[str, Any],
    grid_bootstrap: Mapping[str, Any],
    analysis_mode: str,
    market_types: Sequence[str],
) -> list[dict[str, Any]]:
    """Report every meaningful policy field with its actual evidence basis."""
    date_range = _date_range(closed_rows, "opened_ts")
    shared = {
        "lane": analysis_mode,
        "line_types": list(market_types),
        # The retained sample is pooled across stages unless a line/stage
        # profile is being fitted, so this is stated rather than implied.
        "game_stage": "all",
        "date_range": date_range,
        "independent_events": len({
            str(row["event_id"]) for row in closed_rows
        }),
        "trades": len(closed_rows),
    }
    records: list[dict[str, Any]] = []
    for spec in POLICY_FIELD_CATALOG:
        field = str(spec["field"])
        current = current_policy.get(field)
        mode = str(spec["mode"])
        record: dict[str, Any] = {
            **shared,
            "field": field,
            "label": spec["label"],
            "group": spec["group"],
            "unit": spec["unit"],
            "evidence_mode": mode,
            "current": current,
        }
        if mode == "grid_search":
            suggested = grid_settings.get(field, current)
            validated = grid_status == "evidence_backed_research"
            record.update({
                "suggested": suggested,
                "basis": "validated" if validated else "observational",
                "rationale": (
                    "Selected by the joint optimizer and cleared the "
                    "later-event, concentration, and whole-event bootstrap "
                    "checks."
                    if validated else
                    "Selected by the joint optimizer but the later-event or "
                    "bootstrap check did not pass, so it is diagnostic only."
                ),
                "training_performance": dict(grid_train),
                "later_event_performance": dict(grid_test),
                "event_block_bootstrap": dict(grid_bootstrap),
                "support": _support_level(grid_test),
                # The optimizer tunes this field, but Apply is itself gated on
                # the later-event validation. Reporting it as "written by
                # Apply" while Apply is locked would overstate what will
                # actually happen, so the effective flag tracks the gate and
                # `in_apply_set` records the static membership.
                "in_apply_set": True,
                "applyable": validated,
                "scores_realized_pnl": True,
            })
        elif mode == "marginal":
            evidence = _marginal_field_evidence(
                spec,
                closed_rows=closed_rows,
                opportunity_rows=opportunity_rows,
                opportunity_hours=opportunity_hours,
                test_events=test_events,
                current_value=current,
            )
            record.update(evidence)
            record["support"] = max(
                (
                    str(option.get("support") or "none")
                    for option in evidence["options"]
                ),
                key=lambda level: {
                    "none": 0, "sparse": 1, "directional": 2
                }.get(level, 0),
                default="none",
            )
            record["in_apply_set"] = False
            record["applyable"] = False
        elif mode == "excursion":
            evidence = _excursion_evidence(
                spec, closed_rows=closed_rows, current_value=current
            )
            record.update(evidence)
            record["support"] = (
                "directional"
                if evidence["basis"] == "observational"
                else "sparse"
            )
            # Excursion evidence is identifiable only in the tightening
            # direction and assumes a clean fill at the threshold, so it is
            # reported for a deliberate decision rather than auto-applied.
            record["in_apply_set"] = False
            record["applyable"] = False
        else:
            record.update({
                "suggested": spec["baseline"],
                "basis": "not_identifiable",
                "rationale": spec["baseline_reason"],
                "support": "none",
                "in_apply_set": False,
                "applyable": False,
                "scores_realized_pnl": False,
                "measurement_note": (
                    "Changing this value changes the price path that produced "
                    "every retained realized P/L, so past outcomes cannot score "
                    "it. The value shown is a versioned baseline, not a fitted "
                    "or optimized setting."
                ),
            })
        if record.get("basis") == "baseline_fallback" or (
            record.get("basis") == "not_identifiable"
        ):
            record["baseline_version"] = BASELINE_CHEAT_SHEET_VERSION
        records.append(record)
    return records


def _diagnostics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    known_stage = [
        row for row in rows
        if _finite(row.get("game_fraction_remaining")) is not None
    ]
    first_entries = [
        row for row in rows if int(row.get("event_entries_60m") or 1) == 1
    ]
    repeat_entries = [
        row for row in rows if int(row.get("event_entries_60m") or 1) > 1
    ]
    line_stage_rows = []
    for source in known_stage:
        row = dict(source)
        fraction = float(row["game_fraction_remaining"])
        stage = (
            "early"
            if fraction >= 0.50
            else "middle"
            if fraction >= 0.25
            else "late"
        )
        row["line_stage"] = f"{_market_type(row.get('market_type'))} / {stage}"
        line_stage_rows.append(row)
    return {
        "execution_modes": _category_diagnostics(rows, "mode"),
        "line_types": _category_diagnostics(rows, "market_type"),
        "edge_bands": _bucket_diagnostics(
            rows,
            key="signal_edge",
            buckets=(
                ("under 4%", -1.0, 0.04),
                ("4-6%", 0.04, 0.06),
                ("6-8%", 0.06, 0.08),
                ("8-12%", 0.08, 0.12),
                ("12%+", 0.12, 2.0),
            ),
        ),
        "quality_bands": _bucket_diagnostics(
            rows,
            key="signal_quality",
            buckets=(
                ("under 60", 0.0, 60.0),
                ("60-70", 60.0, 70.0),
                ("70-80", 70.0, 80.0),
                ("80+", 80.0, 101.0),
            ),
        ),
        "price_bands": _bucket_diagnostics(
            rows,
            key="entry_cost",
            buckets=(
                ("5-15c", 0.05, 0.15),
                ("15-25c", 0.15, 0.25),
                ("25-50c", 0.25, 0.50),
                ("50-75c", 0.50, 0.75),
                ("75-95c", 0.75, 0.95),
            ),
        ),
        "entry_repetition": [
            {"label": "first event entry / hour", **_performance(first_entries)},
            {"label": "additional event entries / hour", **_performance(repeat_entries)},
        ],
        "game_stage": _bucket_diagnostics(
            known_stage,
            key="game_fraction_remaining",
            buckets=(
                ("late: under 25% remaining", 0.0, 0.25),
                ("middle-late: 25-50%", 0.25, 0.50),
                ("early: 50%+", 0.50, 1.01),
            ),
        ),
        "game_stage_coverage": {
            "known": len(known_stage),
            "total": len(rows),
            "fraction": len(known_stage) / len(rows) if rows else 0.0,
        },
        "line_by_game_stage": _category_diagnostics(
            line_stage_rows,
            "line_stage",
        ),
        "exit_reasons": _category_diagnostics(rows, "exit_reason"),
    }


def recommend_policy(
    *,
    closed_trades: Iterable[Mapping[str, Any]],
    opportunities: Iterable[Mapping[str, Any]],
    current_policy: Mapping[str, Any],
    objective: str,
    target_trades_per_hour: float,
    model_evidence: Mapping[str, Any] | None = None,
    analysis_mode: str | None = None,
    lookback_days: int = 0,
    market_types: Sequence[str] | None = None,
) -> dict[str, Any]:
    if objective not in ADVISOR_OBJECTIVES:
        raise ValueError(f"unknown policy-advisor objective: {objective}")
    if not math.isfinite(target_trades_per_hour) or not 0.25 <= target_trades_per_hour <= 30:
        raise ValueError("target_trades_per_hour must be between 0.25 and 30")
    if lookback_days not in {0, 7, 30, 90, 180, 365}:
        raise ValueError("lookback_days must be 0, 7, 30, 90, 180, or 365")
    selected_mode = str(
        analysis_mode or current_policy.get("execution_mode") or "dry_run"
    )
    if selected_mode not in {"dry_run", "live", "combined"}:
        raise ValueError("analysis_mode must be dry_run, live, or combined")
    selected_execution_modes = (
        {"dry_run", "live"}
        if selected_mode == "combined"
        else {selected_mode}
    )
    current_allowed = tuple(
        _market_type(value)
        for value in current_policy.get(
            "allowed_market_types", ("moneyline", "spread", "total")
        )
    )
    selected_markets = tuple(dict.fromkeys(
        _market_type(value)
        for value in (market_types or current_allowed)
    ))
    if not selected_markets or not set(selected_markets).issubset(
        {"moneyline", "spread", "total"}
    ):
        raise ValueError("select at least one supported line type")
    model = dict(model_evidence or {})
    contexts = {
        str(item.get("decision_id") or ""): item
        for item in model.pop("decision_contexts", [])
        if isinstance(item, Mapping) and item.get("decision_id")
    }
    raw_closed = [
        dict(row)
        for row in closed_trades
        if str(row.get("mode") or selected_mode) in selected_execution_modes
        and row.get("realized_pnl") is not None
        and _finite(row.get("cost_basis")) is not None
        and float(row.get("cost_basis") or 0.0) > 0
        and all(
            _finite(row.get(field)) is not None
            for field in (
                "signal_edge",
                "signal_quality",
                "reference_sources",
                "entry_cost",
                "opened_ts",
            )
        )
    ]
    newest_ts = max(
        (float(row.get("opened_ts") or 0.0) for row in raw_closed),
        default=0.0,
    )
    cutoff = newest_ts - lookback_days * 86400 if lookback_days else 0.0
    raw_closed = [
        row for row in raw_closed
        if float(row.get("opened_ts") or 0.0) >= cutoff
    ]
    rows = [
        row for row in _enrich_rows(
        raw_closed,
        timestamp_field="opened_ts",
        decision_contexts=contexts,
        )
        if _market_type(row.get("market_type")) in selected_markets
    ]
    raw_opportunities = [
        dict(row)
        for row in opportunities
        if str(row.get("mode") or selected_mode) in selected_execution_modes
        and all(
            _finite(row.get(field)) is not None
            for field in (
                "signal_edge",
                "signal_quality",
                "reference_sources",
                "entry_cost",
                "observed_ts",
            )
        )
        and float(row.get("observed_ts") or 0.0) >= cutoff
    ]
    opportunity_rows = [
        row for row in _enrich_rows(
        raw_opportunities,
        timestamp_field="observed_ts",
        decision_contexts=contexts,
        )
        if _market_type(row.get("market_type")) in selected_markets
    ]
    train_events, test_events = _chronological_events(rows)
    current_settings = {
        "allowed_market_types": list(selected_markets),
        "min_edge": float(current_policy["min_edge"]),
        "max_edge": float(current_policy.get("max_edge", 1.0)),
        "min_signal_quality": float(current_policy["min_signal_quality"]),
        "min_reference_sources": int(current_policy["min_reference_sources"]),
        "min_entry_price": float(current_policy["min_entry_price"]),
        "max_entry_price": float(current_policy["max_entry_price"]),
        "max_entries_per_event_per_hour": int(
            current_policy.get("max_entries_per_event_per_hour", 3)
        ),
        "min_mlb_fraction_remaining": float(
            current_policy.get("min_mlb_fraction_remaining", 0.0)
        ),
        "candidate_cooldown_seconds": int(
            current_policy.get("candidate_cooldown_seconds", 300)
        ),
    }
    opportunity_hours = _active_hours(
        float(row["observed_ts"]) for row in opportunity_rows
    )
    candidates = []
    for settings in _base_candidate_grid(current_policy, selected_markets):
        evaluated = _evaluate_candidate(
            settings,
            rows=rows,
            opportunities=opportunity_rows,
            train_events=train_events,
            test_events=test_events,
            opportunity_hours=opportunity_hours,
            objective=objective,
            target_trades_per_hour=target_trades_per_hour,
        )
        if evaluated is not None:
            candidates.append(evaluated)
    candidates = _expand_churn_and_stage(
        candidates,
        current=current_policy,
        rows=rows,
        opportunities=opportunity_rows,
        train_events=train_events,
        test_events=test_events,
        opportunity_hours=opportunity_hours,
        objective=objective,
        target_trades_per_hour=target_trades_per_hour,
    )
    candidates.sort(
        key=lambda value: (
            -float(value["score"]),
            -int(value["test"]["events"]),
            -int(value["test"]["trades"]),
            json.dumps(value["settings"], sort_keys=True),
        )
    )
    selected = candidates[0] if candidates else {
        "settings": current_settings,
        "train": _performance([]),
        "test": _performance([]),
        "opportunity_rate_per_hour": 0.0,
        "score": 0.0,
    }
    suggestion = {
        **selected["settings"],
        "max_orders_per_hour": (
            max(
                int(current_policy["max_orders_per_hour"]),
                math.ceil(target_trades_per_hour),
            )
            if objective == "more_trades"
            else int(current_policy["max_orders_per_hour"])
        ),
    }
    if objective == "more_trades":
        suggestion["candidate_cooldown_seconds"] = max(
            int(current_policy.get("cycle_seconds", 10)),
            min(int(suggestion["candidate_cooldown_seconds"]), 120),
        )
    current_rows = [row for row in rows if _matches(row, current_settings)]
    selected_rows = [row for row in rows if _matches(row, suggestion)]
    selected_test_rows = [
        row for row in selected_rows if str(row["event_id"]) in test_events
    ]
    current_opportunities = [
        row for row in opportunity_rows if _matches(row, current_settings)
    ]
    current_rate = (
        len(current_opportunities) / opportunity_hours
        if opportunity_hours > 0 else 0.0
    )
    bootstrap = _bootstrap(_event_roi_values(selected_test_rows))
    independent_events = len({str(row["event_id"]) for row in rows})
    evidence_ready = (
        len(rows) >= MIN_CLOSED_TRADES
        and independent_events >= MIN_INDEPENDENT_EVENTS
    )
    selected_test = _performance(selected_test_rows)
    test_supported = (
        int(selected_test["trades"]) >= MIN_TEST_TRADES
        and int(selected_test["events"]) >= MIN_TEST_EVENTS
    )
    test_roi = selected_test.get("turnover_roi")
    bootstrap_probability = bootstrap.get("probability_positive")
    lower_bound = bootstrap.get("lower_95")
    concentration = selected_test.get("maximum_event_stake_share")
    statistical_validation_passed = (
        evidence_ready
        and test_supported
        and test_roi is not None
        and float(test_roi) > 0.0
        and bootstrap_probability is not None
        and float(bootstrap_probability) >= BOOTSTRAP_CONFIDENCE
        and lower_bound is not None
        and float(lower_bound) > 0.0
        and concentration is not None
        and float(concentration) <= 0.35
    )
    # Simulated and live fills have different execution domains. Pooled history
    # is useful for discovering candidate filters and comparing regimes, but it
    # must never become one-click live authorization merely because simulated
    # fills dominate a favorable aggregate.
    validation_passed = (
        statistical_validation_passed and selected_mode != "combined"
    )
    if not evidence_ready:
        status = "exploratory"
    elif not test_supported:
        status = "held_back_insufficient_later_event_test"
    elif selected_mode == "combined" and statistical_validation_passed:
        status = "exploratory_cross_lane"
    elif validation_passed:
        status = "evidence_backed_research"
    else:
        status = "held_back_negative_or_uncertain_test"
    changes = {
        field: {
            "current": (
                list(current_policy[field])
                if field == "allowed_market_types"
                else current_policy.get(field)
            ),
            "suggested": suggestion[field],
        }
        for field in ADVISOR_TUNABLE_FIELDS
        if field in suggestion
        and suggestion[field]
        != (
            list(current_policy[field])
            if field == "allowed_market_types"
            else current_policy.get(field)
        )
    }
    frontier = []
    seen: set[str] = set()
    for candidate in candidates:
        signature = json.dumps(candidate["settings"], sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        frontier.append({
            "settings": candidate["settings"],
            "train": candidate["train"],
            "test": candidate["test"],
            "opportunity_rate_per_hour": candidate["opportunity_rate_per_hour"],
        })
        if len(frontier) >= 5:
            break
    metadata_complete = sum(
        row.get("game_fraction_remaining") is not None for row in rows
    )
    return {
        "version": ADVISOR_VERSION,
        "objective": objective,
        "objective_label": ADVISOR_OBJECTIVES[objective]["label"],
        "target_trades_per_hour": target_trades_per_hour,
        "status": status,
        "scope": {
            "analysis_mode": selected_mode,
            "lookback_days": lookback_days,
            "market_types": list(selected_markets),
        },
        "suggested_policy": suggestion,
        "changes": changes,
        "candidate_frontier": frontier,
        "field_recommendations": _field_recommendations(
            closed_rows=rows,
            opportunity_rows=opportunity_rows,
            opportunity_hours=opportunity_hours,
            test_events=test_events,
            current_policy=current_policy,
            grid_settings=suggestion,
            grid_status=status,
            grid_train=selected["train"],
            grid_test=selected_test,
            grid_bootstrap=bootstrap,
            analysis_mode=selected_mode,
            market_types=selected_markets,
        ),
        "baseline_cheat_sheet_version": BASELINE_CHEAT_SHEET_VERSION,
        "field_coverage_note": (
            "Only fields marked applyable are written by Apply. Everything else "
            "is reported with its evidence so it can be set deliberately: a "
            "marginal sweep is conditional on the rest of the policy, a "
            "frequency-only field has no retained P/L at all, and an exit "
            "control cannot be scored from outcomes its own rule produced."
        ),
        "diagnostics": _diagnostics(rows),
        "evidence": {
            "eligible_closed_trades": len(rows),
            "analysis_mode": selected_mode,
            "lookback_days": lookback_days,
            "allowed_market_types": list(selected_markets),
            "independent_events": independent_events,
            "minimum_closed_trades": MIN_CLOSED_TRADES,
            "minimum_independent_events": MIN_INDEPENDENT_EVENTS,
            "minimum_test_trades": MIN_TEST_TRADES,
            "minimum_test_events": MIN_TEST_EVENTS,
            "train_events": len(train_events),
            "test_events": len(test_events),
            "opportunity_observations": len(opportunity_rows),
            "opportunity_provenance": _opportunity_provenance(opportunity_rows),
            "opportunity_active_hours": opportunity_hours,
            "current_estimated_qualified_per_hour": current_rate,
            "suggested_estimated_qualified_per_hour": selected[
                "opportunity_rate_per_hour"
            ],
            "current_all_history": _performance(current_rows),
            "suggested_all_history": _performance(selected_rows),
            "suggested_train": selected["train"],
            "suggested_test": selected_test,
            "event_block_bootstrap": bootstrap,
            "game_stage_metadata": {
                "known": metadata_complete,
                "total": len(rows),
                "fraction": metadata_complete / len(rows) if rows else 0.0,
            },
            "validation_passed": validation_passed,
            "statistical_validation_passed": statistical_validation_passed,
            "execution_domain_warning": (
                "Combined research pools simulated and live execution outcomes. "
                "It may suggest candidate filters, but it cannot validate a "
                "one-click live policy; re-run the candidate on live fills only "
                "before treating it as live evidence."
                if selected_mode == "combined" else ""
            ),
            "selection_bias_warning": (
                "Historical fills come from policies that were actually used. "
                "Rejected candidates are logged for frequency and context, but "
                "they carry no observed return, so a looser policy still cannot "
                "reveal the P/L of trades never placed. Selection propensities "
                "are deterministic (0 or 1), so inverse-propensity and doubly "
                "robust return estimates are not identified."
            ),
            "multiple_testing_warning": (
                "Many candidate filters were compared. The later-event split, "
                "whole-event bootstrap, concentration cap, and positive lower "
                "confidence bound reduce—but do not eliminate—overfitting."
            ),
        },
        "model_evidence": model,
        "model_used_to_change_settings": False,
        "model_role": (
            "Shadow readiness and game-stage metadata only; the optimizer never "
            "substitutes a fitted probability for the established live engine."
        ),
        "live_model_eligible": bool(model.get("live_eligible")),
        "apply_allowed": bool(changes) and validation_passed,
        "validation_passed": validation_passed,
        "validation_note": (
            "Suggested filters cleared positive later-event after-cost ROI, "
            "event concentration, and 90% whole-event bootstrap checks."
            if validation_passed
            else "Cross-lane analysis is exploratory: dry-run and live fills "
            "were aggregated for candidate discovery, but one-click application "
            "is locked until the same filters validate on one execution mode."
            if selected_mode == "combined"
            else "Diagnostic suggestion only: applying is locked until at least "
            f"{MIN_CLOSED_TRADES} complete trades across "
            f"{MIN_INDEPENDENT_EVENTS} events produce a supported positive "
            "later-event result whose whole-event 95% interval stays above zero."
        ),
        "apply_effect": (
            "Applying saves only execution filters, switches risk style to "
            "Custom, and disarms live orders for review."
        ),
        "guarantee": (
            "No setting can guarantee profit. This is retrospective, "
            "event-balanced research—not a forecast of certain returns."
        ),
    }
