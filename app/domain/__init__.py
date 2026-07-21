"""Canonical domain contracts shared by live evaluation and replay."""

from .records import DataTimestamps, GateResult, GateStatus, SignalQuality
from .time import TimestampAssessment, assess_provider_timestamp, ensure_utc

__all__ = [
    "DataTimestamps",
    "GateResult",
    "GateStatus",
    "SignalQuality",
    "TimestampAssessment",
    "assess_provider_timestamp",
    "ensure_utc",
]
