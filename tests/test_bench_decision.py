"""The benchmark harness has to keep working, or the perf gate is decorative.

These are correctness tests for the measurement tooling, not performance
assertions: no timing threshold is asserted, because a shared CI runner cannot
support one. What is asserted is that every checked-in fixture still builds a
scoreable workload against the current request schema, that the workload matches
what the live decision path actually evaluates, and that a full run produces the
report shape a regression comparison would read.
"""
import json

import pytest

from tools import bench_decision, bench_workload


def test_every_fixture_is_discoverable_and_documented():
    names = bench_workload.fixture_names()
    assert {"small", "normal", "heavy", "adversarial"} <= set(names)
    for name in names:
        spec = bench_workload.load_fixture(name)
        assert spec["name"] == name
        # A size knob without an explanation is a number nobody can justify
        # changing later.
        assert spec["description"].strip()


def test_unknown_fixture_fails_loudly():
    with pytest.raises(FileNotFoundError, match="unknown fixture"):
        bench_workload.load_fixture("does-not-exist")


def test_fixtures_are_ordered_by_the_work_they_impose():
    scored = {}
    for name in ("small", "normal", "heavy"):
        workload = bench_workload.build_workload(name)
        scored[name] = len(workload["prepared"].audit_by_key)
    assert scored["small"] < scored["normal"] < scored["heavy"]


def test_workload_matches_what_the_decision_path_would_score():
    """The benchmark must not score quotes the store would already have dropped.

    ``recompute`` evaluates ``store.quote_values()``, which keeps one quote per
    (comparison market, outcome, canonical source) and rejects invalid ones. A
    harness that fed the raw provider stream instead would overstate the scoring
    workload by the duplicate factor and make the wrong stage look expensive.
    """
    workload = bench_workload.build_workload("adversarial")
    supplied = workload["supplied_quotes"]
    stored = workload["quotes"]
    assert len(stored) < len(supplied)  # duplicates and invalid rows collapsed
    assert all(quote.probability < 1.0 for quote in stored)

    store = bench_workload.build_store(
        workload["spec"], supplied, workload["states"])
    assert len(store.quote_values(bench_workload.EVENT_ID)) == len(stored)


def test_adversarial_fixture_actually_exercises_rejection():
    spec = bench_workload.load_fixture("adversarial")
    quotes = bench_workload.build_quotes(spec)
    assert sum(1 for quote in quotes if quote.probability >= 1.0) == spec[
        "invalid_quotes"]
    assert sum(1 for quote in quotes if quote.quarantined) == spec[
        "quarantined_quotes"]


def test_a_full_run_reports_every_lane(tmp_path, capsys):
    report_path = tmp_path / "bench.json"
    exit_code = bench_decision.main([
        "--fixture", "small",
        "--repeat", "2",
        "--warmup", "1",
        "--concurrent-events", "2",
        "--lag-fixture", "small",
        "--ledger-batches", "2",
        "--json", str(report_path),
    ])
    assert exit_code == 0

    report = json.loads(report_path.read_text(encoding="utf-8"))
    lane = report["fixtures"][0]
    assert lane["fixture"] == "small"
    assert set(lane["seconds"]) == {"ingest", "prepare", "native", "materialize"}
    for stats in lane["seconds"].values():
        assert stats["count"] == 2
        assert stats["p99"] >= stats["p50"] >= 0.0
    assert lane["input"]["request_bytes"] > 0
    assert lane["input"]["output_count"] > 0

    # Both loop-lag modes must be present: the comparison between them is the
    # acceptance test for moving the native call off the loop thread.
    modes = {run["mode"] for run in report["loop_lag"]["runs"]}
    assert modes == {"event_loop_thread", "worker_thread"}

    ledger = report["ledger"]
    assert ledger["decision_rows_written"] > 0
    assert ledger["rows_per_second"] > 0

    # The human-readable summary is what a reviewer actually reads.
    printed = capsys.readouterr().out
    assert "[small]" in printed
    assert "event-loop lag" in printed


def test_ledger_lane_writes_distinct_decisions_per_batch():
    """Re-recording one prepared request would time an ON CONFLICT no-op."""
    workload = bench_workload.build_workload("small")
    result = bench_decision._ledger_lane(workload, batches=3)
    assert result["decision_rows_written"] == (
        3 * result["signals_per_batch"]
    )
