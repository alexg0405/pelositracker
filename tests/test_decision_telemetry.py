"""Stage telemetry and the split preparation / native / materialization path.

These are the guardrails for the optimization work: the split must be provably
equivalent to the old single-shot ``evaluate`` (same decision hash, same signal
ids, same lineage), the offloaded path must return the same answer as the inline
one, and the measurement layer must stay bounded and honest about tails.
"""
import asyncio
import dataclasses
import math
from datetime import datetime, timedelta, timezone

import pytest

from app import engine as engine_module
from app.engine import (
    NativeEngineUnavailable,
    SignalEngine,
    evaluate_prepared,
    evaluate_prepared_async,
    native_engine_available,
    require_native_engine,
)
from app.models import Quote
from app.telemetry import (
    DistributionRegistry,
    EventLoopMonitor,
    EVENT_LOOP_LAG_STAGE,
    performance_snapshot,
    stage_latency,
)


NOW = datetime(2026, 6, 1, 20, 30, tzinfo=timezone.utc)


def _quotes():
    return [
        Quote("e", "moneyline", "home", 0.55, "Pinnacle", NOW,
              bid=0.54, ask=0.56, received_at=NOW),
        Quote("e", "moneyline", "away", 0.45, "Pinnacle", NOW,
              bid=0.44, ask=0.46, received_at=NOW),
        Quote("e", "moneyline", "home", 0.52, "DraftKings", NOW,
              bid=0.51, ask=0.53, received_at=NOW),
        Quote("e", "moneyline", "away", 0.48, "DraftKings", NOW,
              bid=0.47, ask=0.49, received_at=NOW),
        Quote("e", "moneyline", "home", 0.50, "Polymarket", NOW,
              bid=0.49, ask=0.51, ask_size=1_000, depth_complete=True,
              ask_levels=((0.51, 1_000.0),), fee_rate=0.0, tick_size=0.01,
              min_order_size=5.0, received_at=NOW, token_id="tok-home"),
    ]


def _identity(signals):
    """The fields a replay must reproduce exactly."""
    return [
        (s.market, s.outcome, s.action, s.decision_hash, s.decision_id,
         s.edge, s.model_version, s.calibration_version,
         s.configuration_hash, s.input_snapshot_json)
        for s in signals
    ]


# --- the split is equivalent to the original single-shot evaluation -----------

def test_split_phases_reproduce_evaluate_exactly():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0)
    quotes = _quotes()

    one_shot = engine.evaluate("e", quotes, [], as_of=NOW, home_outcome="home")

    prepared = engine.prepare_request("e", quotes, [], as_of=NOW, home_outcome="home")
    staged = engine.materialize_signals(prepared, evaluate_prepared(prepared))

    assert _identity(staged) == _identity(one_shot)
    assert prepared.decision_hash == one_shot[0].decision_hash


def test_offloaded_evaluation_matches_the_inline_one():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0)
    quotes = _quotes()

    inline = engine.evaluate("e", quotes, [], as_of=NOW, home_outcome="home")
    offloaded = asyncio.run(
        engine.evaluate_async("e", quotes, [], as_of=NOW, home_outcome="home"))

    assert _identity(offloaded) == _identity(inline)


def test_prepared_request_carries_only_an_immutable_string_into_the_worker():
    """The worker must not be handed anything the loop thread can still mutate."""
    engine = SignalEngine()
    prepared = engine.prepare_request("e", _quotes(), [], as_of=NOW,
                                      home_outcome="home")
    assert isinstance(prepared.canonical_request, str)
    assert prepared.request_bytes == len(prepared.canonical_request.encode("utf-8"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        prepared.canonical_request = "{}"


def test_lineage_is_snapshotted_at_preparation_not_at_materialization():
    """A calibration installed mid-evaluation must not restamp the decision.

    The canonical request -- and therefore the decision hash -- already embeds
    the lineage that was live when the request was built. Reading the engine's
    current versions again during materialization would produce a signal whose
    stated model version its own hash never covered.
    """
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0)
    prepared = engine.prepare_request("e", _quotes(), [], as_of=NOW,
                                      home_outcome="home")
    results = evaluate_prepared(prepared)

    engine.model_version = "installed-mid-evaluation"
    engine.calibration_version = "installed-mid-evaluation"
    signals = engine.materialize_signals(prepared, results)

    assert all(s.model_version != "installed-mid-evaluation" for s in signals)
    assert all(s.model_version == prepared.model_version for s in signals)
    assert all(s.calibration_version == prepared.calibration_version
               for s in signals)


def test_async_evaluation_does_not_hold_the_event_loop_thread():
    """A concurrently scheduled coroutine must make progress during scoring.

    This is the point of the whole change: PyO3 detaches around the scoring
    kernel, so the worker running it is not holding the interpreter and other
    loop work continues.
    """
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0)
    prepared = engine.prepare_request("e", _quotes(), [], as_of=NOW,
                                      home_outcome="home")

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        for _ in range(50):
            await evaluate_prepared_async(prepared)
        beat.cancel()
        return ticks

    assert asyncio.run(scenario()) > 0


# --- the native engine fails closed, but only when scoring is attempted ------

def test_native_engine_is_available_in_a_built_checkout():
    assert native_engine_available() is True
    assert callable(require_native_engine())


def test_missing_native_engine_fails_closed_at_call_time(monkeypatch):
    """Import stays tolerant; scoring does not.

    Raising at import made a missing native build block collection of storage,
    security and identity tests that never score anything. The failure has to
    survive that move, so it is asserted here.
    """
    monkeypatch.setattr(engine_module, "_native_evaluate_json", None)
    assert engine_module.native_engine_available() is False
    with pytest.raises(NativeEngineUnavailable, match="maturin develop"):
        require_native_engine()

    engine = SignalEngine()
    prepared = engine.prepare_request("e", _quotes(), [], as_of=NOW,
                                      home_outcome="home")
    with pytest.raises(NativeEngineUnavailable):
        evaluate_prepared(prepared)


# --- the measurement layer ---------------------------------------------------

def test_percentiles_track_the_tail_not_the_average():
    registry = DistributionRegistry()
    for value in range(1, 101):
        registry.observe("stage", value / 100)
    stats = registry.snapshot()["stage"]
    assert stats["count"] == 100
    assert stats["samples"] == 100
    # Linear interpolation on 0.01..1.00: rank = fraction * (n - 1).
    assert stats["p50"] == pytest.approx(0.505, abs=1e-6)
    assert stats["p95"] == pytest.approx(0.9505, abs=1e-6)
    assert stats["p99"] == pytest.approx(0.9901, abs=1e-6)
    assert stats["max"] == pytest.approx(1.0)
    # A mean over this sample would report 0.505 and hide the 1.0 tail entirely.
    assert stats["p99"] > stats["mean"]


def test_sample_window_is_bounded_while_the_count_keeps_rising():
    registry = DistributionRegistry(capacity=10)
    for value in range(1_000):
        registry.observe("stage", float(value))
    stats = registry.snapshot()["stage"]
    assert stats["samples"] == 10  # bounded memory
    assert stats["count"] == 1_000  # unbounded throughput visibility
    assert stats["max"] == 999.0  # the retained window is the recent one


def test_unbounded_metric_names_are_rejected_rather_than_accumulated():
    registry = DistributionRegistry(max_names=2)
    for index in range(50):
        registry.observe(f"event-{index}", 1.0)  # the mistake this guards against
    report = registry.snapshot()
    assert len([key for key in report if not key.startswith("_")]) == 2
    assert report["_rejected_names"]["count"] == 48


def test_non_finite_observations_cannot_poison_later_percentiles():
    registry = DistributionRegistry()
    registry.observe("stage", 0.5)
    registry.observe("stage", float("nan"))
    registry.observe("stage", float("inf"))
    stats = registry.snapshot()["stage"]
    assert stats["samples"] == 1
    assert math.isfinite(stats["p99"])


def test_timer_records_a_failing_stage_instead_of_losing_it():
    registry = DistributionRegistry()
    with pytest.raises(ValueError):
        with registry.timer("stage"):
            raise ValueError("boom")
    assert registry.snapshot()["stage"]["count"] == 1


def test_reset_clears_samples_and_counts():
    registry = DistributionRegistry()
    registry.observe("stage", 1.0)
    registry.reset()
    assert registry.snapshot() == {}


def test_event_loop_monitor_samples_lag_and_stops_cleanly():
    monitor = EventLoopMonitor(interval=0.001)

    async def scenario():
        monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()
        # Stopping is idempotent -- shutdown may run it after a failed startup.
        await monitor.stop()

    stage_latency.reset()
    try:
        asyncio.run(scenario())
        stats = stage_latency.snapshot().get(EVENT_LOOP_LAG_STAGE)
        assert stats is not None and stats["count"] > 0
        assert stats["p50"] >= 0.0
    finally:
        stage_latency.reset()


def test_evaluation_populates_the_stage_breakdown():
    stage_latency.reset()
    try:
        engine = SignalEngine(confidence_threshold=0, edge_threshold=0)
        engine.evaluate("e", _quotes(), [], as_of=NOW, home_outcome="home")
        report = performance_snapshot()
        assert {"engine.prepare", "engine.canonicalize", "engine.native",
                "engine.materialize"} <= set(report["stages"])
        assert report["sizes"]["decision_request_bytes"]["max"] > 0
        assert report["sizes"]["decision_output_count"]["max"] > 0
    finally:
        stage_latency.reset()


def test_repeated_evaluations_of_the_same_input_are_still_deterministic():
    """Determinism is what makes a benchmark comparable across runs."""
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0)
    quotes = _quotes()
    first = engine.evaluate("e", quotes, [], as_of=NOW, home_outcome="home")
    second = engine.evaluate("e", quotes, [], as_of=NOW, home_outcome="home")
    later = engine.evaluate("e", quotes, [], as_of=NOW + timedelta(seconds=1),
                            home_outcome="home")
    assert _identity(first) == _identity(second)
    assert first[0].decision_hash != later[0].decision_hash
