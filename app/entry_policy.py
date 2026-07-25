"""Shared new-entry policy for recommendations and simulated execution."""

from __future__ import annotations

import math


# Keep these bounds strict: 5c and 95c themselves are blocked alongside prices
# beyond them. Existing-position marking deliberately does not use this policy.
MIN_ENTRY_PRICE = 0.05
MAX_ENTRY_PRICE = 0.95


def entry_price_allowed(price: float | None) -> bool:
    return (
        price is not None
        and math.isfinite(price)
        and MIN_ENTRY_PRICE < price < MAX_ENTRY_PRICE
    )


def entry_price_blocker() -> str:
    return "New entry blocked: executable price must be above 5c and below 95c."
