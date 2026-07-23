from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, getcontext
from enum import Enum
from typing import Iterable

getcontext().prec = 28

# Versioned fee-unit contract. Polymarket's fee formula (docs.polymarket.com/
# trading/fees) is fee = shares * feeRate * p * (1-p), where feeRate is a DECIMAL
# FRACTION (e.g. 0.05 = 5%) and the fee is rounded to 5 decimal places of USDC
# (smallest fee 0.00001). NOTE: the /fee-rate/{token_id} endpoint returns
# `base_fee` as an INTEGER IN BASIS POINTS (30 = 0.003) -- convert it with
# fee_rate_from_basis_points before passing it here.
FEE_SCHEDULE_VERSION = "polymarket-taker-v1"
FEE_RATE_UNIT = "decimal_fraction"
FEE_ROUNDING = Decimal("0.00001")  # 5 dp USDC, per current Polymarket docs


def D(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def fee_rate_from_basis_points(base_fee: object) -> Decimal:
    """Convert the /fee-rate endpoint's integer basis-point `base_fee` (30 = 30
    bps = 0.003) into the decimal fraction the fee formula expects."""
    return D(base_fee) / Decimal("10000")


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


@dataclass(frozen=True, slots=True)
class SaleResult:
    requested_shares: Decimal
    filled_shares: Decimal
    gross_proceeds: Decimal
    net_proceeds: Decimal
    vwap: Decimal | None
    fee: Decimal
    effective_probability: Decimal | None
    complete: bool
    reason: str
    levels_consumed: int


def polymarket_fee(shares: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    """Documented CLOB fee curve ``shares * feeRate * p * (1-p)``, rounded to 5
    decimal places of USDC per current Polymarket docs.

    ``fee_rate`` is a decimal fraction (see FEE_RATE_UNIT); a value above 1 almost
    certainly means basis points were passed by mistake, so it is rejected rather
    than silently overcharging by 10,000x."""
    if fee_rate < 0:
        raise ValueError("fee rate cannot be negative")
    if fee_rate > 1:
        raise ValueError("fee rate must be a decimal fraction, not basis points")
    return (shares * fee_rate * price * (Decimal("1") - price)).quantize(
        FEE_ROUNDING, rounding=ROUND_HALF_EVEN)


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
    if rate < 0 or rate > 1:
        return ExecutionResult(requested, D(0), D(0), None, D(0), None, False,
                               "invalid fee rate (expected a decimal fraction)", 0)
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
        # Solve cash = shares*(p + feePerShare) with the EXACT fee curve so a full
        # fill lands exactly on the requested cash; the reported fee is rounded to
        # 5 dp once at the end. (Rounding per level left a sub-quantum cash residual
        # that wrongly failed the full-fill check.)
        fee_per_share = rate * level.price * (D(1) - level.price)
        all_in_per_share = level.price + fee_per_share
        take = min(level.size, remaining / all_in_per_share)
        if take <= 0:
            continue
        total = take * all_in_per_share
        if total > remaining:  # floating-point guard
            take = take * remaining / total
            total = take * all_in_per_share
        shares += take
        cost += take * level.price
        fee += take * fee_per_share
        remaining -= total
        consumed += 1
        if remaining <= Decimal("0.00000001"):
            remaining = D(0)
            break
    fee = fee.quantize(FEE_ROUNDING, rounding=ROUND_HALF_EVEN)

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


def simulate_sell(
    bids: Iterable[BookLevel],
    *,
    shares: object,
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
) -> SaleResult:
    """Walk bids for a paper sale and return proceeds after venue fees.

    A rejected full-fill returns zero proceeds so callers cannot accidentally
    value a complete position using only the liquid portion of the book.
    """
    requested = D(shares)
    if requested <= 0:
        raise ValueError("shares must be positive")

    def rejected(reason: str, consumed: int = 0) -> SaleResult:
        return SaleResult(requested, D(0), D(0), D(0), None, D(0), None,
                          False, reason, consumed)

    if identity_ambiguous:
        return rejected("market identity is ambiguous")
    if not active or resolved or restricted or not accepting_orders:
        return rejected("market is not open for paper execution")
    if not depth_complete:
        return rejected("complete order-book depth unavailable")
    if fee_rate is None:
        return rejected("fee metadata unavailable")
    rate = D(fee_rate)
    if rate < 0:
        return rejected("invalid fee rate")
    tick = D(tick_size) if tick_size is not None else None
    if tick is not None and (tick <= 0 or tick >= 1):
        return rejected("invalid tick size")
    minimum = D(min_order_size) if min_order_size is not None else None
    if minimum is not None and minimum <= 0:
        return rejected("invalid minimum order size")
    if minimum is not None and requested < minimum:
        return rejected("minimum order size not satisfied")

    remaining = requested
    filled = D(0)
    gross = D(0)
    fee = D(0)
    consumed = 0
    ordered = sorted((level for level in bids if level.size > 0),
                     key=lambda level: level.price, reverse=True)
    if tick is not None and any(level.price % tick != 0 for level in ordered):
        return rejected("bid price is not aligned to tick size")
    for level in ordered:
        take = min(level.size, remaining)
        if take <= 0:
            continue
        filled += take
        gross += take * level.price
        fee += polymarket_fee(take, level.price, rate)
        remaining -= take
        consumed += 1
        if remaining <= Decimal("0.00000001"):
            remaining = D(0)
            break

    complete = remaining == 0
    if filled == 0:
        return rejected("no executable bid depth", consumed)
    if not complete and partial_policy is PartialFillPolicy.REJECT:
        return rejected("insufficient depth for full-fill policy", consumed)
    vwap = gross / filled
    net = gross - fee
    effective = net / filled
    return SaleResult(requested, filled, gross, net, vwap, fee, effective,
                      complete, "full fill" if complete else "partial fill", consumed)
