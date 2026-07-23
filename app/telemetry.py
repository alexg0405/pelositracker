from __future__ import annotations

import gc
import sys
import tracemalloc
from collections import Counter
from threading import Lock


def start_memory_trace() -> None:
    """Begin per-allocation tracing. Off by default (tracemalloc adds per-alloc
    overhead); the caller enables it behind a flag so allocation attribution is
    available without paying for it in steady state."""
    if not tracemalloc.is_tracing():
        tracemalloc.start()


def process_rss_bytes() -> int | None:
    """Resident set size of this process, or None when it can't be read on this
    platform. Best-effort and dependency-free: ``resource`` on POSIX (Render
    deploys), the Win32 working-set counter on Windows dev boxes."""
    try:
        import resource
    except ImportError:
        resource = None
    if resource is not None:
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports ru_maxrss in KiB, macOS in bytes.
        return ru if sys.platform == "darwin" else ru * 1024
    if sys.platform == "win32":
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
            # Without explicit types the 64-bit HANDLE is truncated to a 32-bit
            # int and the call fails; pin the signature so it succeeds.
            get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
            get_info.restype = wintypes.BOOL
            counters = _PMC()
            counters.cb = ctypes.sizeof(_PMC)
            if get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
        except Exception:
            return None
    return None


def memory_snapshot() -> dict:
    """Lightweight process-memory readout for ``/api/runtime``. Cheap enough to
    call per request: RSS, GC generation counts, and (only when tracing is on)
    the tracked Python-heap current/peak."""
    snapshot: dict[str, object] = {
        "gc_counts": list(gc.get_count()),
        "gc_collections": [stat.get("collections", 0) for stat in gc.get_stats()],
        "tracing": tracemalloc.is_tracing(),
    }
    rss = process_rss_bytes()
    if rss is not None:
        snapshot["rss_mib"] = round(rss / (1024 * 1024), 1)
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
