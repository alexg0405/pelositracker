"""Bounded per-store write lanes.

The lanes exist to take research writes out of the decision's critical section
without ever losing them. That makes three properties load-bearing, and each is
pinned below: durable work is durable before the caller proceeds, scheduled work
survives shutdown, and a saturated queue applies backpressure instead of
discarding evidence.
"""
import asyncio
import threading

import pytest

from app.dbwriter import DatabaseWriter, WriterRegistry


def _run(coroutine):
    return asyncio.run(coroutine)


def test_submit_returns_the_operation_result_and_runs_off_the_loop_thread():
    async def scenario():
        writer = DatabaseWriter("test")
        writer.start()
        loop_thread = threading.get_ident()
        threads = []

        def operation():
            threads.append(threading.get_ident())
            return "written"

        try:
            assert await writer.submit("op", operation) == "written"
        finally:
            await writer.stop()
        return loop_thread, threads

    loop_thread, threads = _run(scenario())
    assert threads and threads[0] != loop_thread


def test_scheduled_work_is_not_awaited_by_the_submitter():
    """The point of the lane: research writes leave the critical section."""
    async def scenario():
        writer = DatabaseWriter("test")
        writer.start()
        release = threading.Event()
        finished = []

        def slow_operation():
            release.wait(5.0)
            finished.append(True)

        try:
            await writer.schedule("slow", slow_operation)
            # Submitter has already returned while the write is still blocked.
            still_running = not finished
            release.set()
            await writer.stop()  # drains
        finally:
            release.set()
        return still_running, finished

    still_running, finished = _run(scenario())
    assert still_running is True
    assert finished == [True]


def test_stop_drains_queued_research_writes_before_returning():
    """Shutdown must not discard what the lane refused to drop under load."""
    async def scenario():
        writer = DatabaseWriter("test")
        writer.start()
        done = []
        for index in range(25):
            await writer.schedule("op", lambda index=index: done.append(index))
        await writer.stop()
        return done

    assert _run(scenario()) == list(range(25))


def test_operations_on_one_lane_keep_submission_order():
    """Model-lab rows are a time series; reordering them would corrupt it."""
    async def scenario():
        writer = DatabaseWriter("test")
        writer.start()
        order = []
        for index in range(50):
            await writer.schedule("op", lambda index=index: order.append(index))
        await writer.stop()
        return order

    assert _run(scenario()) == list(range(50))


def test_durable_work_is_ordered_ahead_of_queued_research_work():
    async def scenario():
        writer = DatabaseWriter("test")
        writer.start()
        gate = threading.Event()
        order = []

        # Occupy the worker so the rest of the work has to queue behind it.
        await writer.schedule("blocker", lambda: gate.wait(5.0))
        for index in range(5):
            await writer.schedule("research", lambda i=index: order.append(f"r{i}"))
        durable = asyncio.create_task(
            writer.submit("durable", lambda: order.append("durable")))
        await asyncio.sleep(0)
        gate.set()
        await durable
        await writer.stop()
        return order

    order = _run(scenario())
    assert order[0] == "durable"  # jumped the queued research work
    assert order[1:] == [f"r{index}" for index in range(5)]


def test_a_failing_durable_write_raises_in_the_caller():
    async def scenario():
        writer = DatabaseWriter("test")
        writer.start()

        def explode():
            raise RuntimeError("disk full")

        try:
            with pytest.raises(RuntimeError, match="disk full"):
                await writer.submit("op", explode)
            # The lane survives a failed write and keeps serving.
            assert await writer.submit("ok", lambda: 1) == 1
        finally:
            await writer.stop()
        return writer.stats()

    stats = _run(scenario())
    assert stats["failed"] == 1
    assert stats["completed"] == 1


def test_a_failing_scheduled_write_is_logged_and_does_not_stall_the_lane(caplog):
    async def scenario():
        writer = DatabaseWriter("test")
        writer.start()
        done = []

        def explode():
            raise RuntimeError("research write failed")

        await writer.schedule("bad", explode)
        await writer.schedule("good", lambda: done.append(True))
        await writer.stop()
        return done, writer.stats()

    done, stats = _run(scenario())
    assert done == [True]  # a failure does not block later work
    assert stats["failed"] == 1
    # Nobody awaits a scheduled write, so an unlogged failure is invisible.
    assert "research write failed" in caplog.text


def test_a_full_queue_applies_backpressure_instead_of_dropping():
    """Dropping model-lab rows would bias the calibration training sample.

    The report this came from suggests sampling or rejecting low-priority writes
    when saturated. That is deliberately not done here: saturation has to show
    up as latency, which is measurable, not as missing evidence, which is not.
    """
    async def scenario():
        writer = DatabaseWriter("test", max_queue=4)
        writer.start()
        gate = threading.Event()
        done = []

        await writer.schedule("blocker", lambda: gate.wait(5.0))
        for index in range(4):
            await writer.schedule("op", lambda i=index: done.append(i))

        # The queue is now full; this submitter must wait for a slot.
        pending = asyncio.create_task(
            writer.schedule("overflow", lambda: done.append(99)))
        await asyncio.sleep(0)
        blocked = not pending.done()

        gate.set()
        await pending
        await writer.stop()
        return blocked, done

    blocked, done = _run(scenario())
    assert blocked is True  # backpressure, not a silent drop
    assert done == [0, 1, 2, 3, 99]  # nothing lost


def test_lane_falls_back_to_a_thread_when_not_started():
    """Stores used outside the application lifespan must still work."""
    async def scenario():
        writer = DatabaseWriter("test")  # never started
        assert writer.running is False
        result = await writer.submit("op", lambda: "ok")
        done = []
        await writer.schedule("op", lambda: done.append(True))
        return result, done

    result, done = _run(scenario())
    assert result == "ok"
    assert done == [True]  # a scheduled write is still performed, not skipped


def test_registry_reuses_lanes_and_reports_their_depth():
    async def scenario():
        registry = WriterRegistry()
        assert registry.lane("ledger") is registry.lane("ledger")
        registry.start_all()
        try:
            await registry.lane("ledger").submit("op", lambda: None)
        finally:
            await registry.stop_all()
        return registry.stats()

    stats = _run(scenario())
    assert stats["ledger"]["completed"] == 1
    assert stats["ledger"]["depth"] == 0
    assert stats["ledger"]["max_queue"] > 0


def test_stop_is_safe_to_call_twice_and_without_a_start():
    async def scenario():
        writer = DatabaseWriter("test")
        await writer.stop()  # never started
        writer.start()
        await writer.stop()
        await writer.stop()

    _run(scenario())
