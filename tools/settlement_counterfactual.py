"""Repeatable hold-to-settlement counterfactual for closed managed positions.

Grades every closed full-game position against the settled final score using
the application's own settlement function, and compares realized exits with
the counterfactual of holding to settlement. This is the measurement behind
docs/predictive-direction-2026-07-31.md ("the hard stop is the leak") made
repeatable, so any candidate exit-policy change can be re-measured without
assistance.

Read-only by construction: both databases are opened in query-only mode. The
counterfactual assumes unlimited capital and prices no drawdown; treat the
output as evidence about exit rules, never as licence to disable stops.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.models import Event
from app.polymarket_us_trading import (
    _dry_segment_settlement_value,
    _estimated_taker_fee,
    _execution_game_stage,
)

FULL_GAME_SCOPE = "full_game"
DEFAULT_RESAMPLES = 2000
DEFAULT_SEED = 7


@dataclass(slots=True)
class GradedPosition:
    position_id: str
    event_id: str
    market_type: str
    exit_reason: str
    entry_price: float
    quantity: float
    stake: float
    actual_pnl: float
    settlement_value: float
    settlement_pnl: float
    estimated_entry_fee: float
    estimated_exit_fee: float
    entry_signal_edge: float | None
    entry_execution_edge: float | None
    entry_signal_quality: float | None
    entry_game_fraction_remaining: float | None

    @property
    def giveback(self) -> float:
        return self.settlement_pnl - self.actual_pnl


def _read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_outcomes(history_path: Path) -> dict[str, sqlite3.Row]:
    connection = _read_only(history_path)
    try:
        rows = connection.execute(
            """SELECT event_id,name,sport,home,away,
                      final_home_score,final_away_score
               FROM event_outcomes
               WHERE final_home_score IS NOT NULL
                 AND final_away_score IS NOT NULL""",
        ).fetchall()
    finally:
        connection.close()
    return {str(row["event_id"]): row for row in rows}


def load_closed_positions(trading_path: Path) -> list[dict[str, object]]:
    connection = _read_only(trading_path)
    try:
        rows = connection.execute(
            """SELECT id,event_id,event_name,market_type,market_scope,
                      selection,quantity,entry_cost,current_exit_value,
                      exit_reason,realized_pnl,entry_signal_edge,
                      entry_execution_edge,entry_signal_quality,
                      entry_game_fraction_remaining
               FROM live_managed_positions
               WHERE status='closed'
               ORDER BY closed_ts""",
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def grade_positions(
    positions: list[dict[str, object]],
    outcomes: dict[str, sqlite3.Row],
) -> tuple[list[GradedPosition], dict[str, int]]:
    graded: list[GradedPosition] = []
    skipped = {
        "not_full_game_scope": 0,
        "no_settled_outcome": 0,
        "unpriced_close": 0,
        "unmapped_selection": 0,
    }
    for row in positions:
        scope = str(row["market_scope"] or FULL_GAME_SCOPE)
        if scope != FULL_GAME_SCOPE:
            skipped["not_full_game_scope"] += 1
            continue
        outcome = outcomes.get(str(row["event_id"]))
        if outcome is None:
            skipped["no_settled_outcome"] += 1
            continue
        if row["realized_pnl"] is None:
            skipped["unpriced_close"] += 1
            continue
        event = Event(
            name=str(outcome["name"] or ""),
            sport=str(outcome["sport"] or ""),
            home=str(outcome["home"] or ""),
            away=str(outcome["away"] or ""),
        )
        scores = (
            float(outcome["final_home_score"]),
            float(outcome["final_away_score"]),
        )
        settlement_value = _dry_segment_settlement_value(row, event, scores)
        if settlement_value is None:
            skipped["unmapped_selection"] += 1
            continue
        entry_price = float(row["entry_cost"])
        quantity = float(row["quantity"])
        exit_value = row["current_exit_value"]
        exit_price = float(exit_value) if exit_value is not None else None
        graded.append(
            GradedPosition(
                position_id=str(row["id"]),
                event_id=str(row["event_id"]),
                market_type=str(row["market_type"]),
                exit_reason=str(row["exit_reason"] or "unknown"),
                entry_price=entry_price,
                quantity=quantity,
                stake=entry_price * quantity,
                actual_pnl=float(row["realized_pnl"]),
                settlement_value=settlement_value,
                settlement_pnl=(settlement_value - entry_price) * quantity,
                estimated_entry_fee=_estimated_taker_fee(
                    quantity=quantity,
                    contract_price=entry_price,
                ),
                # Settlement pays no exit fee, so only the realized exit is
                # charged; a 0/1 exit price prices to zero fee by the formula.
                estimated_exit_fee=(
                    _estimated_taker_fee(
                        quantity=quantity,
                        contract_price=exit_price,
                    )
                    if exit_price is not None
                    else 0.0
                ),
                entry_signal_edge=_optional_float(row["entry_signal_edge"]),
                entry_execution_edge=_optional_float(
                    row["entry_execution_edge"]
                ),
                entry_signal_quality=_optional_float(
                    row["entry_signal_quality"]
                ),
                entry_game_fraction_remaining=_optional_float(
                    row["entry_game_fraction_remaining"]
                ),
            )
        )
    return graded, skipped


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _round(value: float, digits: int = 4) -> float:
    return round(value, digits)


def _group_summary(rows: list[GradedPosition]) -> dict[str, object]:
    stake = sum(r.stake for r in rows)
    actual = sum(r.actual_pnl for r in rows)
    settlement = sum(r.settlement_pnl for r in rows)
    wins = sum(1 for r in rows if r.settlement_value >= 0.5)
    average_price = (
        sum(r.entry_price * r.stake for r in rows) / stake if stake else 0.0
    )
    return {
        "positions": len(rows),
        "events": len({r.event_id for r in rows}),
        "stake_usd": _round(stake),
        "actual_pnl_usd": _round(actual),
        "settlement_pnl_usd": _round(settlement),
        "giveback_usd": _round(settlement - actual),
        "actual_return_pct": _round(100.0 * actual / stake if stake else 0.0, 2),
        "settlement_return_pct": _round(
            100.0 * settlement / stake if stake else 0.0, 2
        ),
        "settlement_win_rate_pct": _round(
            100.0 * wins / len(rows) if rows else 0.0, 2
        ),
        "stake_weighted_entry_price": _round(average_price),
        "win_rate_minus_price_pp": _round(
            (wins / len(rows) - average_price) * 100.0 if rows else 0.0, 2
        ),
    }


def _grouped(
    rows: list[GradedPosition],
    key: str,
) -> list[dict[str, object]]:
    buckets: dict[str, list[GradedPosition]] = {}
    for row in rows:
        buckets.setdefault(getattr(row, key), []).append(row)
    summaries = []
    for value in sorted(buckets):
        summary = _group_summary(buckets[value])
        summary[key] = value
        summaries.append(summary)
    summaries.sort(key=lambda s: -int(s["positions"]))
    return summaries


def bootstrap_giveback(
    rows: list[GradedPosition],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> dict[str, object]:
    """Event-block bootstrap of the stake-weighted settlement-minus-actual
    return, in percent of stake. Resampling whole events preserves the
    within-event correlation that per-position resampling would destroy."""
    by_event: dict[str, list[GradedPosition]] = {}
    for row in rows:
        by_event.setdefault(row.event_id, []).append(row)
    events = sorted(by_event)
    if len(events) < 2:
        return {
            "resamples": 0,
            "note": "fewer than two independent events; no interval computed",
        }
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(resamples):
        stake = 0.0
        difference = 0.0
        for _ in range(len(events)):
            event_rows = by_event[events[rng.randrange(len(events))]]
            stake += sum(r.stake for r in event_rows)
            difference += sum(r.giveback for r in event_rows)
        if stake > 0:
            draws.append(100.0 * difference / stake)
    draws.sort()

    def percentile(fraction: float) -> float:
        index = fraction * (len(draws) - 1)
        low = math.floor(index)
        high = math.ceil(index)
        weight = index - low
        return draws[low] * (1 - weight) + draws[high] * weight

    return {
        "resamples": len(draws),
        "seed": seed,
        "events": len(events),
        "mean_pct": _round(sum(draws) / len(draws), 2),
        "ci95_low_pct": _round(percentile(0.025), 2),
        "ci95_high_pct": _round(percentile(0.975), 2),
        "positive_fraction": _round(
            sum(1 for d in draws if d > 0) / len(draws), 4
        ),
    }


def build_report(
    graded: list[GradedPosition],
    skipped: dict[str, int],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> dict[str, object]:
    overall = _group_summary(graded)
    overall["bootstrap_giveback"] = bootstrap_giveback(
        graded, resamples=resamples, seed=seed
    )
    return {
        "graded_positions": len(graded),
        "skipped": skipped,
        "overall": overall,
        "by_market_type": _grouped(graded, "market_type"),
        "by_exit_reason": _grouped(graded, "exit_reason"),
        "estimated_fees_usd": {
            "entry": _round(sum(r.estimated_entry_fee for r in graded)),
            "actual_exit": _round(sum(r.estimated_exit_fee for r in graded)),
        },
        "notes": [
            "Settlement counterfactual assumes unlimited capital and prices "
            "no drawdown; it is evidence about exit rules, not licence to "
            "remove stops.",
            "Dry-lane realized_pnl excludes fees while live exit marks are "
            "fee-adjusted; estimated fees are reported separately so the "
            "lanes can be read consistently.",
            "Only full-game scopes are graded: event_outcomes carries final "
            "scores, so segment positions cannot be settled here.",
        ],
    }


def calibration_pairs(graded: list[GradedPosition]) -> list[dict[str, object]]:
    """Per-position (entry, settled-label) pairs for calibration research."""
    pairs = []
    for row in graded:
        fraction = row.entry_game_fraction_remaining
        pairs.append(
            {
                "position_id": row.position_id,
                "event_id": row.event_id,
                "market_type": row.market_type,
                "stage": (
                    _execution_game_stage(fraction)
                    if fraction is not None
                    else None
                ),
                "entry_price": row.entry_price,
                "entry_signal_edge": row.entry_signal_edge,
                "entry_execution_edge": row.entry_execution_edge,
                "entry_signal_quality": row.entry_signal_quality,
                "entry_game_fraction_remaining": fraction,
                "label": int(row.settlement_value >= 0.5),
            }
        )
    return pairs


def run(
    trading_db: Path,
    history_db: Path,
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    pairs_path: Path | None = None,
) -> dict[str, object]:
    outcomes = load_outcomes(history_db)
    positions = load_closed_positions(trading_db)
    graded, skipped = grade_positions(positions, outcomes)
    report = build_report(graded, skipped, resamples=resamples, seed=seed)
    report["trading_database"] = str(trading_db)
    report["history_database"] = str(history_db)
    report["closed_positions"] = len(positions)
    report["settled_outcomes"] = len(outcomes)
    if pairs_path is not None:
        with pairs_path.open("w", encoding="utf-8") as handle:
            for pair in calibration_pairs(graded):
                handle.write(json.dumps(pair, sort_keys=True) + "\n")
        report["calibration_pairs_path"] = str(pairs_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "trading_db",
        nargs="?",
        type=Path,
        default=Path("polymarket-us-trading.db"),
        help="Lane trading database (live default; pass "
        "workstation-data/polymarket-us-dry-run.db for the dry lane)",
    )
    parser.add_argument(
        "--history-db",
        type=Path,
        default=Path("workstation-data/history.db"),
        help="History database carrying event_outcomes",
    )
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="Bootstrap seed; fixed by default so runs are reproducible",
    )
    parser.add_argument(
        "--emit-pairs",
        type=Path,
        default=None,
        help="Optional JSONL path for per-position calibration pairs",
    )
    args = parser.parse_args()
    report = run(
        args.trading_db,
        args.history_db,
        resamples=args.resamples,
        seed=args.seed,
        pairs_path=args.emit_pairs,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
