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


ADVISOR_VERSION = "execution-policy-advisor-v3-event-balanced"
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
    return [
        {"label": label, **_performance(group)}
        for label, group in sorted(grouped.items())
    ]


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
    return {
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
    if selected_mode not in {"dry_run", "live"}:
        raise ValueError("analysis_mode must be dry_run or live")
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
        if str(row.get("mode") or selected_mode) == selected_mode
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
        if str(row.get("mode") or selected_mode) == selected_mode
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
    validation_passed = (
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
    if not evidence_ready:
        status = "exploratory"
    elif not test_supported:
        status = "held_back_insufficient_later_event_test"
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
            "selection_bias_warning": (
                "Historical fills come from policies that were actually used. "
                "A looser policy cannot reveal the P/L of trades never placed."
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
