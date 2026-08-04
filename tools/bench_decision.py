r"""Reproducible latency benchmark for the decision path.

Run: ``.\.venv\Scripts\python.exe -m tools.bench_decision``
Machine-readable: ``... -m tools.bench_decision --json bench.json``

The functional suite proves the decision path is *correct*. Nothing proved it
was still *fast*, so a change that doubled serialization cost or put CPU work
back on the event loop could pass CI unnoticed. These lanes measure the layers
the report calls out, in the order the cost actually accumulates:

``native``      the Python/Rust boundary -- parse, score, encode, parse back --
                with the request and output sizes that drive it.
``prepare``     building payloads, the freshest-valid reduction, canonical JSON
                and the decision hash.
``materialize`` turning scorer output into ``Signal`` objects.
``loop_lag``    the one that matters for responsiveness: many events scoring
                concurrently while a timer samples how late the loop runs it.
                Run twice -- inline on the loop thread (the pre-detach shape)
                and awaited on a worker -- because the claim being tested is
                that scoring no longer starves the loop, not that the scorer
                itself got faster.
``ledger``      durable signal writes and a bounded history read, against a
                throwaway SQLite file.

Percentiles come from the same ``DistributionRegistry`` the service uses, so a
number here and a number on ``/api/runtime`` mean the same thing.

Absolute values are machine-specific. Compare a run against a run on the same
box, and treat hosted-runner numbers as trend data with wide error bars.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from app import __version__
from app.telemetry import DistributionRegistry
from tools import bench_workload
from tools.bench_workload import AS_OF, EVENT_ID, build_workload, fixture_names

DEFAULT_REPEAT = 200
DEFAULT_WARMUP = 20
DEFAULT_CONCURRENT_EVENTS = 8
LAG_SAMPLE_INTERVAL = 0.005


def _measure(name: str, call: Callable[[], Any], *, repeat: int,
             warmup: int) -> dict[str, float]:
    """Time ``call`` ``repeat`` times after ``warmup`` untimed iterations.

    The warmup exists because the first call through a lane pays one-off costs
    -- import, lazily built caches, first-touch pages -- that would otherwise
    dominate a small sample and make an unchanged lane look like a regression.
    """
    registry = DistributionRegistry(capacity=max(repeat, 1))
    for _ in range(warmup):
        call()
    for _ in range(repeat):
        started = time.perf_counter()
        call()
        registry.observe(name, time.perf_counter() - started)
    return registry.snapshot()[name]


def _lane(fixture: str, workload: dict, *, repeat: int,
          warmup: int) -> dict[str, Any]:
    engine = workload["engine"]
    prepared = workload["prepared"]
    quotes = workload["quotes"]
    supplied = workload["supplied_quotes"]
    states = workload["states"]
    spec = workload["spec"]

    from app.engine import evaluate_prepared

    results = evaluate_prepared(prepared)
    output_json = json.dumps(results, separators=(",", ":"))

    def ingest() -> None:
        bench_workload.build_store(spec, supplied, states)

    return {
        "fixture": fixture,
        "description": spec.get("description", ""),
        "input": {
            "quotes_supplied": len(supplied),
            "quotes_after_store": len(quotes),
            "quote_payloads_scored": len(prepared.audit_by_key),
            "states": len(states),
            "request_bytes": prepared.request_bytes,
            "output_bytes": len(output_json.encode("utf-8")),
            "output_count": len(results),
        },
        "seconds": {
            "ingest": _measure("ingest", ingest, repeat=repeat, warmup=warmup),
            "prepare": _measure(
                "prepare",
                lambda: engine.prepare_request(
                    EVENT_ID, quotes, states, bench_workload.AWAY,
                    sport=spec.get("sport", ""), league=spec.get("league", ""),
                    home_outcome=bench_workload.HOME, as_of=AS_OF,
                    canonical_event_id=f"canonical-{fixture}",
                ),
                repeat=repeat, warmup=warmup),
            "native": _measure(
                "native", lambda: evaluate_prepared(prepared),
                repeat=repeat, warmup=warmup),
            "materialize": _measure(
                "materialize",
                lambda: engine.materialize_signals(prepared, results),
                repeat=repeat, warmup=warmup),
        },
    }


async def _loop_lag(workload: dict, *, offload: bool, events: int,
                    rounds: int) -> dict[str, Any]:
    """Score ``events`` events ``rounds`` times each while sampling loop lag.

    ``offload=False`` reproduces the pre-change shape: the native round trip runs
    inline on the loop thread. ``offload=True`` awaits it on a worker. The lag
    sampler is an ordinary timer task, so any stall it records is a stall a
    provider callback, health check or SSE heartbeat would also have taken.
    """
    from app.engine import evaluate_prepared, evaluate_prepared_async

    prepared = workload["prepared"]
    registry = DistributionRegistry(capacity=4096)
    stop = asyncio.Event()

    async def sampler() -> None:
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            started = loop.time()
            await asyncio.sleep(LAG_SAMPLE_INTERVAL)
            registry.observe(
                "lag", max(0.0, loop.time() - started - LAG_SAMPLE_INTERVAL))

    async def worker() -> None:
        for _ in range(rounds):
            if offload:
                await evaluate_prepared_async(prepared)
            else:
                evaluate_prepared(prepared)
                await asyncio.sleep(0)  # the yield an await point would give

    sampler_task = asyncio.create_task(sampler())
    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(events)))
    wall = time.perf_counter() - started
    stop.set()
    await sampler_task

    lag = registry.snapshot().get("lag", {})
    return {
        "mode": "worker_thread" if offload else "event_loop_thread",
        "events": events,
        "rounds_per_event": rounds,
        "wall_seconds": round(wall, 6),
        "evaluations_per_second": round(events * rounds / wall, 1) if wall else None,
        "lag_seconds": lag,
    }


def _ledger_lane(workload: dict, *, batches: int) -> dict[str, Any]:
    """Durable decision writes plus a bounded history read on a scratch database.

    Each batch must be a genuinely new decision: ``decision_marks`` is keyed by
    ``(decision_hash, market, outcome)`` and inserts ``ON CONFLICT DO NOTHING``,
    so re-recording the same prepared request would time a no-op. Advancing
    ``as_of`` per batch changes the canonical request and therefore the hash, so
    every batch inserts. The batches are built up front and only the writes are
    timed.
    """
    from app.engine import evaluate_prepared
    from app.ledger import Ledger
    from app.sport_model_lab import SportModelLab

    engine = workload["engine"]
    event = workload["event"]
    quotes = workload["quotes"]
    states = workload["states"]
    spec = workload["spec"]

    prepared_batches = []
    for batch in range(batches):
        prepared = engine.prepare_request(
            EVENT_ID, quotes, states, bench_workload.AWAY,
            sport=spec.get("sport", ""), league=spec.get("league", ""),
            home_outcome=bench_workload.HOME,
            as_of=AS_OF + timedelta(seconds=batch),
            canonical_event_id="canonical-ledger",
        )
        prepared_batches.append(
            engine.materialize_signals(prepared, evaluate_prepared(prepared)))
    if not prepared_batches or not prepared_batches[0]:
        return {"skipped": "fixture produced no signals"}

    write = DistributionRegistry(capacity=max(batches, 1))
    research = DistributionRegistry(capacity=max(batches, 1))
    paper_bets = 0
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(str(Path(tmp) / "bench-ledger.db"))
        lab = SportModelLab(str(Path(tmp) / "bench-model-lab.db"))
        try:
            for signals in prepared_batches:
                started = time.perf_counter()
                paper_bets += ledger.record_signals(event, signals)
                write.observe("batch", time.perf_counter() - started)
                # Timed separately because it is no longer awaited inside the
                # decision: `record()` hands this to the model-lab write lane.
                # This is the cost that left the critical section.
                started = time.perf_counter()
                lab.record(event, signals, quotes, states,
                           as_of=signals[0].observed_at)
                research.observe("batch", time.perf_counter() - started)
            stored = len(ledger.all_decisions())
            read = _measure(
                "read", lambda: ledger.all_decisions(limit=500),
                repeat=20, warmup=2)
        finally:
            ledger.close()
            lab.close()

    batch_stats = write.snapshot()["batch"]
    research_stats = research.snapshot()["batch"]
    total = batch_stats["mean"] * batch_stats["samples"]
    return {
        "batches": batches,
        "signals_per_batch": len(prepared_batches[0]),
        "decision_rows_written": stored,
        # Only placed entries land in paper_bets; WATCH decisions are still
        # journalled as decision marks, which is what the row count above shows.
        "paper_bet_rows_written": paper_bets,
        "rows_per_second": round(stored / total, 1) if total else None,
        "write_batch_seconds": batch_stats,
        "model_lab_write_seconds": research_stats,
        "recent_decisions_read_seconds": read,
    }


def _format_stats(label: str, stats: dict[str, float]) -> str:
    return (f"    {label:<14}"
            f" p50 {stats['p50'] * 1000:8.3f} ms"
            f"  p95 {stats['p95'] * 1000:8.3f} ms"
            f"  p99 {stats['p99'] * 1000:8.3f} ms"
            f"  max {stats['max'] * 1000:8.3f} ms")


def _print_report(report: dict) -> None:
    print(f"Decision-path benchmark - live-edge-monitor {report['version']}")
    print(f"  python {report['python']}  repeat={report['repeat']} "
          f"warmup={report['warmup']}\n")
    for lane in report["fixtures"]:
        info = lane["input"]
        print(f"  [{lane['fixture']}] {info['quotes_supplied']:,} quotes supplied"
              f" -> {info['quotes_after_store']:,} in store"
              f" -> {info['quote_payloads_scored']:,} scored,"
              f" {info['states']} states")
        print(f"    request {info['request_bytes'] / 1024:.1f} KiB"
              f"  output {info['output_bytes'] / 1024:.1f} KiB"
              f"  signals {info['output_count']}")
        for name, stats in lane["seconds"].items():
            print(_format_stats(name, stats))
        print()

    lag = report.get("loop_lag")
    if lag:
        print(f"  [event-loop lag] {lag['concurrent_events']} concurrent events"
              f" x {lag['rounds_per_event']} evaluations"
              f" ({lag['fixture']} fixture)")
        for run in lag["runs"]:
            stats = run["lag_seconds"]
            if not stats:
                continue
            print(f"    {run['mode']:<18}"
                  f" lag p50 {stats['p50'] * 1000:7.2f} ms"
                  f"  p99 {stats['p99'] * 1000:7.2f} ms"
                  f"  max {stats['max'] * 1000:7.2f} ms"
                  f"  |  {run['evaluations_per_second']:>9,.1f} eval/s")
        inline, offloaded = (run["lag_seconds"] for run in lag["runs"])
        if inline and offloaded and offloaded.get("p99"):
            print(f"    -> p99 loop lag {inline['p99'] / offloaded['p99']:.1f}x "
                  "lower when the native call is offloaded")
        print()

    ledger = report.get("ledger")
    if ledger and "skipped" not in ledger:
        print(f"  [ledger] {ledger['decision_rows_written']:,} decision rows"
              f" in {ledger['batches']} batches")
        print(_format_stats("write batch", ledger["write_batch_seconds"]))
        print(_format_stats("read recent", ledger["recent_decisions_read_seconds"]))
        print(f"    {ledger['rows_per_second']:,.1f} rows/s")
        # Not awaited by record() any more; shown so the amount of work moved
        # off the decision's critical section is a number rather than a claim.
        print(_format_stats("model lab*", ledger["model_lab_write_seconds"]))
        print("    * scheduled on its own write lane, not awaited by record()\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", action="append", choices=fixture_names(),
                        help="fixture to run (repeatable; default: all)")
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT,
                        help=f"timed iterations per lane (default {DEFAULT_REPEAT})")
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP,
                        help=f"untimed iterations per lane (default {DEFAULT_WARMUP})")
    parser.add_argument("--concurrent-events", type=int,
                        default=DEFAULT_CONCURRENT_EVENTS,
                        help="events scoring at once in the loop-lag lane")
    parser.add_argument("--lag-fixture", default="normal", choices=fixture_names(),
                        help="fixture used for the loop-lag lane")
    parser.add_argument("--ledger-batches", type=int, default=25,
                        help="decision batches written in the ledger lane")
    parser.add_argument("--skip-ledger", action="store_true",
                        help="skip the persistence lane")
    parser.add_argument("--json", type=Path,
                        help="also write the full report as JSON to this path")
    args = parser.parse_args(argv)

    fixtures = args.fixture or fixture_names()
    report: dict[str, Any] = {
        "version": __version__,
        "python": sys.version.split()[0],
        "repeat": args.repeat,
        "warmup": args.warmup,
        "fixtures": [],
    }
    workloads = {name: build_workload(name) for name in fixtures}
    for name in fixtures:
        report["fixtures"].append(
            _lane(name, workloads[name], repeat=args.repeat, warmup=args.warmup))

    lag_workload = workloads.get(args.lag_fixture) or build_workload(args.lag_fixture)
    rounds = max(1, args.repeat // 4)
    runs = [
        asyncio.run(_loop_lag(lag_workload, offload=False,
                              events=args.concurrent_events, rounds=rounds)),
        asyncio.run(_loop_lag(lag_workload, offload=True,
                              events=args.concurrent_events, rounds=rounds)),
    ]
    report["loop_lag"] = {
        "fixture": args.lag_fixture,
        "concurrent_events": args.concurrent_events,
        "rounds_per_event": rounds,
        "sample_interval_seconds": LAG_SAMPLE_INTERVAL,
        "runs": runs,
    }

    if not args.skip_ledger:
        report["ledger"] = _ledger_lane(
            workloads[fixtures[0]], batches=args.ledger_batches)

    _print_report(report)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True),
                             encoding="utf-8")
        print(f"  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
