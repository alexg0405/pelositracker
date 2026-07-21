from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .time import ensure_utc


class GateStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    status: GateStatus
    reason: str

    @property
    def passed(self) -> bool:
        return self.status is GateStatus.PASS


@dataclass(frozen=True, slots=True)
class DataTimestamps:
    provider_timestamp: datetime | None
    received_at: datetime
    processed_at: datetime

    def __post_init__(self) -> None:
        if self.provider_timestamp is not None:
            object.__setattr__(self, "provider_timestamp", ensure_utc(self.provider_timestamp))
        object.__setattr__(self, "received_at", ensure_utc(self.received_at))
        object.__setattr__(self, "processed_at", ensure_utc(self.processed_at))
        if self.processed_at < self.received_at:
            raise ValueError("processed_at cannot precede received_at")


@dataclass(frozen=True, slots=True)
class SignalQuality:
    freshness: float
    identity: float
    state: float
    source_independence: float
    liquidity: float
    calibration: float

    def minimum(self) -> float:
        return min(
            self.freshness,
            self.identity,
            self.state,
            self.source_independence,
            self.liquidity,
            self.calibration,
        )
