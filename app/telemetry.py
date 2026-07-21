from __future__ import annotations

from collections import Counter
from threading import Lock


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
