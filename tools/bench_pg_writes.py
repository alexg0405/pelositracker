"""Measure batched vs per-statement decision-mark inserts against PostgreSQL.

Why this exists as its own tool. `Ledger.record_signals` journals one decision
mark per signal, and a prop-heavy event produces ~114 of them per tick. Issuing
those as separate `execute` calls costs one network round trip each against a
managed PostgreSQL, which is the production backend -- but *not* against local
SQLite, where the same loop is only ~1.2x slower than the batched form. So the
change that motivated this benchmark cannot be honestly justified by any number
measurable on a development machine.

Rather than assert the PostgreSQL win, CI measures it: the `postgres-migrations`
job already runs a real server, and this tool reports the difference there. It
needs no native engine (it builds `Signal` objects directly), so it runs in a
job that never compiles the Rust extension.

    DATABASE_URL=postgresql://... python -m tools.bench_pg_writes
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

from app.ledger import Ledger
from app.models import Event, Signal

_SIGNAL_COUNT = 114  # the heavy fixture's decision-mark count per tick


def _event() -> Event:
    return Event("bench-pg-event", "Bench Home vs Bench Away", "Home", "Away",
                 sport="basketball", league="nba")


def _signals(count: int, event: Event) -> list[Signal]:
    return [
        Signal(
            event.id,
            f"market_{index // 2}",
            "home" if index % 2 else "away",
            model_probability=0.55,
            market_probability=0.50,
            edge=0.05,
            confidence=80.0,
            action="WATCH",
            reasons=["benchmark"],
            quote_source="Polymarket",
            market_fair_prob=0.55,
            n_reference_sources=3,
            token_id=f"token-{index}",
        )
        for index in range(count)
    ]


_INSERT = """INSERT INTO decision_marks
   (decision_hash, event_id, market, outcome, as_of,
    consensus_probability, executable_probability, gross_edge,
    net_ev_per_stake, policy_action, reasons, decision_id,
    engine_version, configuration_hash, source_mapping_version,
    model_version, calibration_version, execution_policy_version,
    input_snapshot_json, token_id, order_book_snapshot_id,
    requested_cash, execution_vwap, execution_fee,
    calibrated_probability, uncertainty_low, uncertainty_high,
    probability_net_ev_positive, net_ev_per_share, net_ev_total,
    consensus_method, calibration_sample_size, gate_results_json,
    independent_model_probability, independent_model_version,
    independent_model_hash, independent_calibration_version,
    independent_calibration_hash, independent_model_sample_size,
    independent_model_event_count,
    independent_model_registry_version)
   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
   ON CONFLICT(decision_hash, market, outcome) DO NOTHING"""


def _time_write(ledger: Ledger, rows: list[tuple], *, batched: bool) -> float:
    started = time.perf_counter()
    with ledger._db.transaction() as cur:
        if batched:
            ledger._db.execute_many(cur, _INSERT, rows)
        else:
            for row in rows:
                ledger._db.execute(cur, _INSERT, row)
    return time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=15)
    parser.add_argument("--signals", type=int, default=_SIGNAL_COUNT)
    parser.add_argument("--json", metavar="PATH")
    args = parser.parse_args(argv)

    if not os.getenv("DATABASE_URL"):
        print("DATABASE_URL is not set; this benchmark needs a real PostgreSQL.",
              file=sys.stderr)
        return 2

    event = _event()
    signals = _signals(args.signals, event)
    ledger = Ledger()
    if ledger.backend != "postgres":
        print(f"expected a postgres backend, got {ledger.backend}", file=sys.stderr)
        ledger.close()
        return 2

    samples: dict[bool, list[float]] = {True: [], False: []}
    try:
        for index in range(args.rounds):
            # Alternate the two arms round by round so any drift in server load
            # is shared between them rather than attributed to one.
            for batched in (True, False):
                rows = []
                for signal in signals:
                    row = list(Ledger._decision_mark_row(event, signal))
                    row[0] = f"bench-{int(batched)}-{index}"
                    row[3] = f"{row[3]}-{len(rows)}"  # keep the conflict key unique
                    rows.append(tuple(row))
                samples[batched].append(_time_write(ledger, rows, batched=batched))
        with ledger._db.transaction() as cur:
            ledger._db.execute(
                cur, "DELETE FROM decision_marks WHERE event_id=%s", (event.id,))
    finally:
        ledger.close()

    report = {
        "backend": "postgres",
        "rounds": args.rounds,
        "rows_per_round": args.signals,
    }
    for batched in (True, False):
        key = "execute_many" if batched else "per_row_execute"
        values = samples[batched]
        report[key] = {
            "p50_ms": round(statistics.median(values) * 1000, 3),
            "mean_ms": round(statistics.fmean(values) * 1000, 3),
            "min_ms": round(min(values) * 1000, 3),
        }
    report["speedup"] = round(
        report["per_row_execute"]["p50_ms"] / report["execute_many"]["p50_ms"], 2)

    print(f"decision_marks insert, {args.signals} rows x {args.rounds} rounds "
          f"(PostgreSQL)")
    for key in ("execute_many", "per_row_execute"):
        stats = report[key]
        print(f"  {key:16s} p50 {stats['p50_ms']:8.3f} ms  "
              f"mean {stats['mean_ms']:8.3f} ms  min {stats['min_ms']:8.3f} ms")
    print(f"  -> execute_many is {report['speedup']:.2f}x faster")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
