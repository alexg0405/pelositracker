from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def ensure_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC timestamp or reject an ambiguous value."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def parse_provider_timestamp(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric, timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return parse_provider_timestamp(int(text))
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return ensure_utc(parsed)
    raise ValueError("unsupported timestamp type")


@dataclass(frozen=True, slots=True)
class TimestampAssessment:
    trusted: bool
    age_seconds: float | None
    reason: str | None = None


def assess_provider_timestamp(
    provider_timestamp: datetime | None,
    *,
    as_of: datetime,
    max_age_seconds: float,
    max_future_skew_seconds: float = 5.0,
) -> TimestampAssessment:
    """Assess provider freshness without substituting a local receipt timestamp."""
    as_of = ensure_utc(as_of)
    if provider_timestamp is None:
        return TimestampAssessment(False, None, "provider timestamp unavailable")
    try:
        provider_timestamp = ensure_utc(provider_timestamp)
    except ValueError:
        return TimestampAssessment(False, None, "provider timestamp is timezone-ambiguous")
    age = (as_of - provider_timestamp).total_seconds()
    if age < -max_future_skew_seconds:
        return TimestampAssessment(False, age, "provider timestamp is in the future")
    if age > max_age_seconds:
        return TimestampAssessment(False, age, "provider observation is stale")
    return TimestampAssessment(True, max(0.0, age))
