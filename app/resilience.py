from __future__ import annotations

from dataclasses import dataclass
import random


@dataclass(slots=True)
class RetryBackoff:
    base_seconds: float = 1.0
    cap_seconds: float = 60.0
    jitter_fraction: float = 0.20
    attempts: int = 0

    def next_delay(self) -> float:
        raw = min(self.cap_seconds, self.base_seconds * (2 ** self.attempts))
        self.attempts += 1
        jitter = raw * self.jitter_fraction
        return min(self.cap_seconds, max(0.0, raw + random.uniform(-jitter, jitter)))

    def reset(self) -> None:
        self.attempts = 0
