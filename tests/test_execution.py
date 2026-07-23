from decimal import Decimal

import pytest

from app.execution import (
    BookLevel,
    PartialFillPolicy,
    fee_rate_from_basis_points,
    polymarket_fee,
    simulate_buy,
    simulate_sell,
)
from app.orderbook import BookGapError, OrderBookState


def levels(*pairs):
    return [BookLevel.create(price, size) for price, size in pairs]


def test_decimal_depth_walk_includes_multiple_levels_and_fees():
    result = simulate_buy(
        levels(("0.50", "100"), ("0.55", "200")),
        cash="100",
        fee_rate="0.03",
    )
    assert result.complete
    assert result.levels_consumed == 2
    assert result.vwap is not None and result.vwap > Decimal("0.50")
    assert result.effective_probability > result.vwap
    assert result.fee > 0


def test_unknown_fee_or_insufficient_depth_fails_closed():
    unknown_fee = simulate_buy(levels(("0.50", "1000")), cash="100", fee_rate=None)
    shallow = simulate_buy(levels(("0.50", "1")), cash="100", fee_rate="0")
    assert not unknown_fee.complete and "fee metadata" in unknown_fee.reason
    assert not shallow.complete and shallow.filled_shares == 0


def test_partial_fill_requires_explicit_policy():
    partial = simulate_buy(levels(("0.50", "1")), cash="100", fee_rate="0",
                           partial_policy=PartialFillPolicy.ALLOW)
    assert not partial.complete
    assert partial.filled_shares == Decimal("1")


def test_fee_curve_is_deterministic():
    assert polymarket_fee(Decimal("10"), Decimal("0.5"), Decimal("0.03")) \
        == Decimal("0.07500000")


def test_fee_rounds_to_five_decimals_and_drops_a_sub_minimum_fee():
    # 1 * 0.00001 * 0.5 * 0.5 = 0.0000025 USDC rounds to 0 at 5 dp (min fee 0.00001).
    assert polymarket_fee(Decimal("1"), Decimal("0.5"), Decimal("0.00001")) == Decimal("0")


def test_fee_rate_given_in_basis_points_is_rejected_not_overcharged():
    with pytest.raises(ValueError):
        polymarket_fee(Decimal("10"), Decimal("0.5"), Decimal("30"))


def test_basis_points_convert_to_a_decimal_fraction():
    assert fee_rate_from_basis_points(30) == Decimal("0.003")


def test_orderbook_applies_zero_size_removal_and_detects_hash_gap():
    state = OrderBookState("token")
    state.apply_snapshot({
        "asset_id": "token", "timestamp": "1000", "hash": "h1",
        "bids": [{"price": "0.49", "size": "10"}],
        "asks": [{"price": "0.51", "size": "8"}],
    })
    state.apply_change(
        {"timestamp": "1001", "previous_hash": "h1", "hash": "h2"},
        {"side": "sell", "price": "0.51", "size": "0"},
    )
    assert state.best_ask() is None
    with pytest.raises(BookGapError):
        state.apply_change(
            {"timestamp": "1002", "previous_hash": "wrong", "hash": "h3"},
            {"side": "buy", "price": "0.48", "size": "2"},
        )
    assert not state.synchronized


@pytest.mark.parametrize("status", [
    {"active": False}, {"resolved": True}, {"restricted": True},
    {"accepting_orders": False}, {"depth_complete": False},
])
def test_market_status_and_depth_fail_closed(status):
    result = simulate_buy(levels(("0.50", "1000")), cash="100", fee_rate="0", **status)
    assert not result.complete and result.filled_shares == 0


def test_minimum_order_and_tick_alignment_are_enforced():
    too_small = simulate_buy(
        levels(("0.50", "10")), cash="1", fee_rate="0", min_order_size="3"
    )
    off_tick = simulate_buy(
        levels(("0.505", "100")), cash="10", fee_rate="0", tick_size="0.01"
    )
    assert not too_small.complete and "minimum" in too_small.reason
    assert not off_tick.complete and "tick" in off_tick.reason


def test_sell_walks_best_bids_and_deducts_fees():
    result = simulate_sell(
        levels(("0.55", "100"), ("0.50", "200")),
        shares="150",
        fee_rate="0.03",
    )
    assert result.complete
    assert result.levels_consumed == 2
    assert result.vwap == Decimal("0.5333333333333333333333333333")
    assert result.fee > 0
    assert result.net_proceeds < result.gross_proceeds
    assert result.effective_probability < result.vwap


def test_sell_fails_closed_without_full_depth_or_fee_metadata():
    shallow = simulate_sell(levels(("0.50", "1")), shares="10", fee_rate="0")
    unknown_fee = simulate_sell(levels(("0.50", "10")), shares="10", fee_rate=None)
    incomplete = simulate_sell(
        levels(("0.50", "10")), shares="10", fee_rate="0", depth_complete=False
    )
    assert not shallow.complete and shallow.net_proceeds == 0
    assert not unknown_fee.complete and "fee metadata" in unknown_fee.reason
    assert not incomplete.complete and "depth" in incomplete.reason
