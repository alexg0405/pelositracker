from __future__ import annotations

import asyncio
import gc
import math
import os
import sys
import time
import tracemalloc
from collections import Counter, deque
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock


def start_memory_trace() -> None:
    """Begin per-allocation tracing. Off by default (tracemalloc adds per-alloc
    overhead); the caller enables it behind a flag so allocation attribution is
    available without paying for it in steady state."""
    if not tracemalloc.is_tracing():
        tracemalloc.start()


def _windows_memory_counters():
    """The Win32 PROCESS_MEMORY_COUNTERS for this process, or None. Exposes both
    the current (WorkingSetSize) and peak (PeakWorkingSetSize) working set."""
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        get_info = ctypes.windll.psapi.GetProcessMemoryInfo
        # Without explicit types the 64-bit HANDLE is truncated to a 32-bit int
        # and the call fails; pin the signature so it succeeds.
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        get_info.restype = wintypes.BOOL
        counters = _PMC()
        counters.cb = ctypes.sizeof(_PMC)
        if get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters
    except Exception:
        return None
    return None


def process_rss_bytes() -> int | None:
    """CURRENT resident set size of this process in bytes, or None when it can't
    be read cheaply here. Dependency-free: /proc on Linux (the Render deploy
    target), the Win32 working-set counter on Windows. This is the live working
    set, NOT the lifetime peak -- see :func:`process_peak_rss_bytes`. (POSIX
    ``ru_maxrss`` is deliberately not used here: it is the peak, not the current.)"""
    # `os.sysconf` only exists on POSIX. Looked up dynamically so this module
    # type-checks identically on both the Linux deploy target and Windows dev
    # boxes instead of needing a platform-conditional ignore.
    sysconf = getattr(os, "sysconf", None)
    try:
        if sysconf is not None:
            with open("/proc/self/statm", encoding="ascii") as handle:
                resident_pages = int(handle.read().split()[1])
            return resident_pages * int(sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, IndexError):
        pass
    if sys.platform == "win32":
        counters = _windows_memory_counters()
        if counters is not None:
            return int(counters.WorkingSetSize)
    return None


def process_peak_rss_bytes() -> int | None:
    """PEAK (lifetime maximum) resident set size in bytes, or None. POSIX
    ``ru_maxrss`` (KiB on Linux, bytes on macOS) or the Win32 peak working set."""
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows has no resource module
        resource = None  # type: ignore[assignment]
    # Looked up dynamically for the same reason as ``os.sysconf`` above: these
    # names are POSIX-only in the type stubs, and a platform-conditional ignore
    # would itself be flagged as unused on the other platform.
    getrusage = getattr(resource, "getrusage", None)
    rusage_self = getattr(resource, "RUSAGE_SELF", None)
    if getrusage is not None and rusage_self is not None:
        ru = int(getrusage(rusage_self).ru_maxrss)
        # Linux reports ru_maxrss in KiB, macOS in bytes.
        return ru if sys.platform == "darwin" else ru * 1024
    if sys.platform == "win32":
        counters = _windows_memory_counters()
        if counters is not None:
            return int(counters.PeakWorkingSetSize)
    return None


def memory_snapshot() -> dict:
    """Lightweight process-memory readout for ``/api/runtime``. Cheap enough to
    call per request: current RSS (``rss_mib``) and lifetime peak RSS
    (``rss_peak_mib``) reported separately, GC generation counts, and (only when
    tracing is on) the tracked Python-heap current/peak."""
    snapshot: dict[str, object] = {
        "gc_counts": list(gc.get_count()),
        "gc_collections": [stat.get("collections", 0) for stat in gc.get_stats()],
        "tracing": tracemalloc.is_tracing(),
    }
    rss = process_rss_bytes()
    if rss is not None:
        snapshot["rss_mib"] = round(rss / (1024 * 1024), 1)
    peak_rss = process_peak_rss_bytes()
    if peak_rss is not None:
        snapshot["rss_peak_mib"] = round(peak_rss / (1024 * 1024), 1)
    if tracemalloc.is_tracing():
        current, peak = tracemalloc.get_traced_memory()
        snapshot["python_heap_current_mib"] = round(current / (1024 * 1024), 1)
        snapshot["python_heap_peak_mib"] = round(peak / (1024 * 1024), 1)
    return snapshot


class RuntimeTelemetry:
    def __init__(self):
        self._counters: Counter[str] = Counter()
        self._lock = Lock()

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self._counters.items()))


runtime_telemetry = RuntimeTelemetry()


def _percentile(ordered: list[float], fraction: float) -> float:
    """Linearly interpolated percentile of an already-sorted, non-empty list."""
    if len(ordered) == 1:
        return ordered[0]
    rank = fraction * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


class DistributionRegistry:
    """Bounded recent-sample distributions keyed by a low-cardinality name.

    Optimization work needs tail latency, not an average: a mean hides exactly
    the queueing and event-loop stalls that matter. Each name keeps a
    fixed-size ``deque`` of its most recent observations (a sliding reservoir,
    so memory is O(names x capacity) regardless of uptime) and percentiles are
    computed on demand when ``/api/runtime`` is read. ``count`` is the lifetime
    number of observations, so throughput stays visible even though only the
    recent window is retained.

    Names must be code-level literals such as ``engine.native``. The
    ``max_names`` cap is a fail-safe: a caller that ever interpolates an event
    id or market slug into a name is rejected and counted rather than allowed
    to grow the registry without bound.
    """

    def __init__(self, capacity: int = 512, max_names: int = 64, digits: int = 6):
        self._capacity = capacity
        self._max_names = max_names
        self._digits = digits
        self._samples: dict[str, deque[float]] = {}
        self._counts: Counter[str] = Counter()
        self._rejected = 0
        self._lock = Lock()

    def observe(self, name: str, value: float) -> None:
        """Record one observation. Non-finite values are ignored (a failed
        measurement must not poison every later percentile)."""
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            return
        with self._lock:
            window = self._samples.get(name)
            if window is None:
                if len(self._samples) >= self._max_names:
                    self._rejected += 1
                    return
                window = deque(maxlen=self._capacity)
                self._samples[name] = window
            window.append(float(value))
            self._counts[name] += 1

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        """Time the enclosed block and record its duration in seconds.

        Records on the way out even when the block raises, so a failing stage
        still shows up in the latency breakdown instead of silently vanishing.
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, time.perf_counter() - started)

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            windows = {name: list(window) for name, window in self._samples.items()}
            counts = dict(self._counts)
            rejected = self._rejected
        report: dict[str, dict[str, float]] = {}
        for name in sorted(windows):
            values = sorted(windows[name])
            if not values:
                continue
            digits = self._digits
            report[name] = {
                "count": counts.get(name, len(values)),
                "samples": len(values),
                "p50": round(_percentile(values, 0.50), digits),
                "p95": round(_percentile(values, 0.95), digits),
                "p99": round(_percentile(values, 0.99), digits),
                "max": round(values[-1], digits),
                "mean": round(sum(values) / len(values), digits),
            }
        if rejected:
            report["_rejected_names"] = {"count": rejected}
        return report

    def reset(self) -> None:
        """Drop every retained sample and count. For tests and benchmarks."""
        with self._lock:
            self._samples.clear()
            self._counts.clear()
            self._rejected = 0


# Seconds-valued stage durations along the decision path (see record() in
# app/main.py and SignalEngine in app/engine.py for the stage names).
stage_latency = DistributionRegistry()
# Byte sizes and output counts. Whole units, so no sub-integer rounding.
decision_sizes = DistributionRegistry(digits=1)

EVENT_LOOP_LAG_STAGE = "event_loop.lag"


class EventLoopMonitor:
    """Sample how late the event loop runs a timer that should be on time.

    Lag is the difference between the requested sleep and the observed sleep:
    the loop can only be late because something else held its thread. That
    makes this the single most direct measurement of whether synchronous work
    (native scoring, canonical JSON, a store call that never reached a worker)
    is starving the loop, and it is the acceptance signal for moving that work
    off it.
    """

    def __init__(self, interval: float = 0.25) -> None:
        self.interval = interval
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            await asyncio.sleep(self.interval)
            stage_latency.observe(
                EVENT_LOOP_LAG_STAGE, max(0.0, loop.time() - started - self.interval)
            )

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


event_loop_monitor = EventLoopMonitor()


def performance_snapshot() -> dict:
    """Latency and size distributions for ``/api/runtime``."""
    return {"stages": stage_latency.snapshot(), "sizes": decision_sizes.snapshot()}
