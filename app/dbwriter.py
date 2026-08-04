"""Bounded, observable write lanes -- one per store.

Every store call used to be handed to ``asyncio.to_thread``, which means the
default executor: dozens of call sites, one shared and effectively unbounded
work queue, no priority, and no way to see how deep the backlog was. Two
consequences showed up in the decision path. A research write and a
safety-critical write competed for the same threads with no ordering guarantee,
and ``record()`` awaited both in series inside the per-event lock even though
they target different databases.

A lane gives each store one dedicated worker thread and one bounded queue:

* work for different stores runs in parallel instead of contending;
* work for the same store is serialized, which it already was (each store holds
  its own lock and its SQLite file has one writer anyway), so nothing is lost;
* the backlog has a size, a wait time, and a name, all reported to
  ``/api/runtime``.

Two submission modes, chosen by what the caller can honestly tolerate:

``submit``    the caller awaits durability. For money and audit writes, where
              continuing before the row exists would break the evidence chain.
``schedule``  the caller does not await. For research and telemetry rows that
              must still be written, just not inside the decision's critical
              section.

DELIBERATE DEVIATION from the optimization report, which suggests sampling or
rejecting low-priority writes when the queue is full. That is right for pure
telemetry and wrong here: the model-lab rows are the training sample for
calibration, so dropping them under load would silently bias the fit toward
quiet periods -- a research-validity bug that no latency number justifies. A
full queue therefore applies backpressure to the submitter instead. Saturation
shows up as latency and queue depth, which are visible, rather than as missing
evidence, which is not.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .telemetry import runtime_telemetry, stage_latency

logger = logging.getLogger(__name__)

# Durable work is ordered ahead of research work that is already queued. It
# cannot preempt an operation that is mid-flight; store writes here are single
# -digit-to-tens of milliseconds, so head-of-line delay is bounded and small.
PRIORITY_DURABLE = 0
PRIORITY_RESEARCH = 10


@dataclass(slots=True)
class _Command:
    priority: int
    operation: Callable[[], Any]
    name: str
    queued_at: float
    future: asyncio.Future | None = field(default=None)


class DatabaseWriter:
    """One serialized, bounded write lane for a single store."""

    def __init__(self, name: str, *, max_queue: int = 1000) -> None:
        self.name = name
        self._max_queue = max_queue
        self._queue: asyncio.PriorityQueue[tuple[int, int, _Command]] | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._task: asyncio.Task | None = None
        self._accepting = False
        self._sequence = itertools.count()
        self._completed = 0
        self._failed = 0
        self._peak_depth = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self.running:
            return
        queue: asyncio.PriorityQueue[tuple[int, int, _Command]] = asyncio.PriorityQueue(
            maxsize=self._max_queue
        )
        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"db-{self.name}"
        )
        self._queue = queue
        self._executor = executor
        self._accepting = True
        # The worker is handed its queue and executor rather than reading them
        # back off the instance, so stopping the lane cannot make a running
        # worker lose track of the queue it still has to drain.
        self._task = asyncio.create_task(
            self._run(queue, executor), name=f"db-writer-{self.name}"
        )

    async def stop(self, *, drain: bool = True) -> None:
        """Finish queued work, then release the worker thread.

        Draining is the default and matters: scheduled research writes are not
        awaited by their submitter, so shutting down without draining would
        discard exactly the evidence this class refuses to drop under load.

        Order is load-bearing. New work stops being accepted first (it falls
        back to a plain thread rather than joining a queue nobody will drain),
        then the backlog is drained, and only then is the worker torn down.
        """
        self._accepting = False
        queue, task, executor = self._queue, self._task, self._executor
        if drain and queue is not None and task is not None and not task.done():
            try:
                await asyncio.wait_for(queue.join(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "db writer %s did not drain in 30s; %d items abandoned",
                    self.name, queue.qsize(),
                )
        self._queue = self._task = self._executor = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if executor is not None:
            executor.shutdown(wait=True)

    @property
    def running(self) -> bool:
        return (self._accepting and self._task is not None
                and not self._task.done())

    # -- submission --------------------------------------------------------

    async def submit(self, name: str, operation: Callable[[], Any]) -> Any:
        """Run ``operation`` on this lane and await its result."""
        if not self.running:
            # No lane (tests, or a store used before startup): preserve the old
            # behavior rather than silently skipping a durable write.
            return await asyncio.to_thread(operation)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._put(PRIORITY_DURABLE, name, operation, future)
        return await future

    async def schedule(self, name: str, operation: Callable[[], Any]) -> None:
        """Queue ``operation`` without awaiting it.

        Returns once the work is accepted, not once it is durable. A full queue
        blocks here -- that is the backpressure, and it is deliberate.
        """
        if not self.running:
            await asyncio.to_thread(operation)
            return
        await self._put(PRIORITY_RESEARCH, name, operation, None)

    async def _put(self, priority: int, name: str, operation: Callable[[], Any],
                   future: asyncio.Future | None) -> None:
        queue = self._queue
        if queue is None:  # stopped between the running check and here
            await asyncio.to_thread(operation)
            return
        command = _Command(priority, operation, name, time.perf_counter(), future)
        # The sequence tiebreaker keeps FIFO order inside a priority and stops
        # the heap from ever comparing two _Command objects.
        await queue.put((priority, next(self._sequence), command))
        depth = queue.qsize()
        self._peak_depth = max(self._peak_depth, depth)
        stage_latency.observe(f"db.{self.name}.queue_depth", depth)

    # -- worker ------------------------------------------------------------

    async def _run(self, queue: asyncio.PriorityQueue,
                   executor: ThreadPoolExecutor) -> None:
        loop = asyncio.get_running_loop()
        while True:
            _, _, command = await queue.get()
            try:
                stage_latency.observe(
                    f"db.{self.name}.queue_wait",
                    time.perf_counter() - command.queued_at,
                )
                started = time.perf_counter()
                try:
                    result = await loop.run_in_executor(
                        executor, command.operation)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # surfaced to the awaiter or logged
                    self._failed += 1
                    runtime_telemetry.increment(f"db_write_failed_{self.name}")
                    if command.future is not None and not command.future.done():
                        command.future.set_exception(exc)
                    else:
                        # Nobody is awaiting a scheduled write, so a failure that
                        # is not logged here is a failure nobody ever sees.
                        logger.exception(
                            "%s write %s failed", self.name, command.name)
                else:
                    self._completed += 1
                    if command.future is not None and not command.future.done():
                        command.future.set_result(result)
                finally:
                    stage_latency.observe(
                        f"db.{self.name}.{command.name}",
                        time.perf_counter() - started,
                    )
            finally:
                queue.task_done()

    # -- observability -----------------------------------------------------

    def stats(self) -> dict[str, Any]:
        queue = self._queue
        return {
            "running": self.running,
            "depth": queue.qsize() if queue is not None else 0,
            "peak_depth": self._peak_depth,
            "max_queue": self._max_queue,
            "completed": self._completed,
            "failed": self._failed,
        }


class WriterRegistry:
    """The set of lanes owned by the application lifespan."""

    def __init__(self) -> None:
        self._lanes: dict[str, DatabaseWriter] = {}

    def lane(self, name: str, *, max_queue: int = 1000) -> DatabaseWriter:
        writer = self._lanes.get(name)
        if writer is None:
            writer = DatabaseWriter(name, max_queue=max_queue)
            self._lanes[name] = writer
        return writer

    def start_all(self) -> None:
        for writer in self._lanes.values():
            writer.start()

    async def stop_all(self) -> None:
        for writer in self._lanes.values():
            await writer.stop()

    def stats(self) -> dict[str, dict[str, Any]]:
        return {name: writer.stats() for name, writer in sorted(self._lanes.items())}


writers = WriterRegistry()
