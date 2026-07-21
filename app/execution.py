from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, getcontext
from enum import Enum
from typing import Iterable

getcontext().prec = 28


def D(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    size: Decimal

    @classmethod
    def create(cls, price: object, size: object) -> "BookLevel":
        level = cls(D(price), D(size))
        if not (Decimal("0") < level.price < Decimal("1")) or level.size < 0:
            raise ValueError("invalid order-book level")
        return level


class PartialFillPolicy(str, Enum):
    REJECT = "reject"
    ALLOW = "allow"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    requested_cash: Decimal
    filled_cash: Decimal
    filled_shares: Decimal
    vwap: Decimal | None
    fee: Decimal
    effective_probability: Decimal | None
    complete: bool
    reason: str
    levels_consumed: int


def polymarket_fee(shares: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    """Documented CLOB fee curve, rounded deterministically to 1e-8 USDC."""
    if fee_rate < 0:
        raise ValueError("fee rate cannot be negative")
    return (shares * fee_rate * price * (Decimal("1") - price)).quantize(
        Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)


def simulate_buy(
    asks: Iterable[BookLevel],
    *,
    cash: object,
    fee_rate: object | None,
    partial_policy: PartialFillPolicy = PartialFillPolicy.REJECT,
    tick_size: object | None = None,
    min_order_size: object | None = None,
    active: bool = True,
    resolved: bool = False,
    restricted: bool = False,
    accepting_orders: bool = True,
    depth_complete: bool = True,
    identity_ambiguous: bool = False,
) -> ExecutionResult:
    requested = D(cash)
    if requested <= 0:
        raise ValueError("cash must be positive")
    if identity_ambiguous:
        return ExecutionResult(requested, D(0), D(0), None, D(0), None, False,
                               "market identity is ambiguous", 0)
    if not active or resolved or restricted or not accepting_orders:
        return ExecutionResult(requested, D(0), D(0), None, D(0), None, False,
                               "market is not open for paper execution", 0)
    if not depth_complete:
        return ExecutionResult(requested, D(0), D(0), None, D(0), None, False,
                               "complete order-book depth unavailable", 0)
    if fee_rate is None:
        return ExecutionResult(requested, D(0), D(0), None, D(0), None, False,
                               "fee metadata unavailable", 0)
    rate = D(fee_rate)
    tick = D(tick_size) if tick_size is not None else None
    if tick is not None and (tick <= 0 or tick >= 1):
        return ExecutionResult(requested, D(0), D(0), None, D(0), None, False,
                               "invalid tick size", 0)
    minimum = D(min_order_size) if min_order_size is not None else None
    if minimum is not None and minimum <= 0:
        return ExecutionResult(requested, D(0), D(0), None, D(0), None, False,
                               "invalid minimum order size", 0)
    remaining = requested
    cost = D(0)
    shares = D(0)
    fee = D(0)
    consumed = 0
    ordered = sorted((level for level in asks if level.size > 0), key=lambda level: level.price)
    if tick is not None and any(level.price % tick != 0 for level in ordered):
        return ExecutionResult(requested, D(0), D(0), None, D(0), None, False,
                               "ask price is not aligned to tick size", 0)
    for level in ordered:
        # Fee depends on shares at this level. Solve cash = shares*(p + fee/unit).
        fee_per_share = rate * level.price * (D(1) - level.price)
        all_in_per_share = level.price + fee_per_share
        take = min(level.size, remaining / all_in_per_share)
        if take <= 0:
            continue
        level_cost = take * level.price
        level_fee = polymarket_fee(take, level.price, rate)
        total = level_cost + level_fee
        if total > remaining:  # rounding guard
            take = take * remaining / total
            level_cost = take * level.price
            level_fee = polymarket_fee(take, level.price, rate)
            total = level_cost + level_fee
        shares += take
        cost += level_cost
        fee += level_fee
        remaining -= total
        consumed += 1
        if remaining <= Decimal("0.00000001"):
            remaining = D(0)
            break

    complete = remaining == 0
    if not complete and partial_policy is PartialFillPolicy.REJECT:
        return ExecutionResult(requested, D(0), D(0), None, D(0), None, False,
                               "insufficient depth for full-fill policy", consumed)
    if shares == 0:
        return ExecutionResult(requested, D(0), D(0), None, D(0), None, False,
                               "no executable ask depth", consumed)
    if minimum is not None and shares < minimum:
        return ExecutionResult(requested, D(0), D(0), None, D(0), None, False,
                               "minimum order size not satisfied", consumed)
    vwap = cost / shares
    effective = (cost + fee) / shares
    return ExecutionResult(requested, cost + fee, shares, vwap, fee, effective,
                           complete, "full fill" if complete else "partial fill", consumed)
