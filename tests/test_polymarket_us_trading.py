from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.models import Event, Signal
from app.polymarket_us_trading import (
    ARM_PHRASE,
    DRY_RUN_HISTORY_CLEAR_PHRASE,
    LIVE_LIQUIDATION_PHRASE,
    PolymarketUSAutoTrader,
    TradingPolicy,
    TradingPolicyError,
)


class Clock:
    def __init__(self, value=1_800_000_000.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeOrders:
    def __init__(self):
        self.previewed = []
        self.created = []
        self.canceled = []

    def preview(self, params):
        self.previewed.append(params)
        return {"order": params["request"]}

    def create(self, params):
        self.created.append(params)
        return {
            "id": f"order-{len(self.created)}",
            "executions": [{
                "lastShares": str(params["quantity"]),
                "lastPx": params["price"],
                "type": "EXECUTION_TYPE_FILL",
            }],
        }

    def cancel(self, order_id, params):
        self.canceled.append((order_id, params))


class FakeClient:
    def __init__(self, orders, *, fail_on_orders=False, book=None):
        self._orders = orders
        self._fail_on_orders = fail_on_orders
        self._book = book or {
            "marketData": {
                "offers": [{"qty": "100", "px": {"value": 0.40}}],
                "bids": [{"qty": "100", "px": {"value": 0.39}}],
                "state": "MARKET_STATE_OPEN",
            }
        }
        self.account = SimpleNamespace(
            balances=lambda: {
                "balances": [{"buyingPower": 100.0, "currentBalance": 100.0}]
            }
        )
        self.markets = SimpleNamespace(
            book=lambda _slug: self._book
        )

    @property
    def orders(self):
        if self._fail_on_orders:
            raise AssertionError("dry-run must never access the order resource")
        return self._orders

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def client_factory(orders, *, fail_on_orders=False, book=None):
    def factory(**kwargs):
        assert kwargs["key_id"] == "key"
        assert kwargs["secret_key"] == "secret"
        return FakeClient(
            orders,
            fail_on_orders=fail_on_orders,
            book=book,
        )

    return factory


def test_default_policy_is_aggressive_but_bounded_for_ten_dollar_rollout():
    policy = TradingPolicy()

    assert policy.automation_enabled is False
    assert policy.execution_mode == "dry_run"
    assert policy.auto_cashout is False
    assert policy.require_engine_entry is True
    assert policy.max_total_exposure_usd == 9.50
    assert policy.minimum_cash_reserve_usd == 0.50
    assert policy.max_position_usd == 1.75
    assert policy.max_event_exposure_usd == 3.0
    assert policy.max_daily_loss_usd == 5.0
    assert policy.max_open_positions == 6
    assert policy.max_orders_per_hour == 6
    assert policy.min_edge == 0.03
    assert policy.min_signal_quality == 60.0
    assert policy.min_reference_sources == 2
    assert policy.min_entry_price == 0.10
    assert policy.max_entry_price == 0.90
    assert policy.max_spread == 0.04
    assert policy.min_book_shares == 3.0
    assert policy.min_hold_minutes == 10.0
    assert policy.profit_target == 0.10
    assert policy.trailing_drawdown == 0.04
    assert policy.stop_loss == 0.20


def event():
    return Event(
        id="event-1",
        name="Away at Home",
        sport="basketball",
        league="NBA",
        home="Home",
        away="Away",
        game_start="2027-01-15T00:00:00Z",
    )


def signal(
    clock,
    *,
    probability=0.60,
    action="PAPER_BET",
    quality=85.0,
    edge=0.05,
):
    return Signal(
        event_id="event-1",
        market="moneyline",
        outcome="Away",
        model_probability=probability,
        market_probability=0.40,
        edge=edge,
        confidence=quality,
        action=action,
        reasons=[],
        observed_at=datetime.fromtimestamp(clock(), timezone.utc),
        n_reference_sources=2,
        required_edge=0.02,
        decision_id="decision-1",
        engine_version="unchanged-engine",
        configuration_hash="unchanged-config",
        model_version="unchanged-model",
        calibration_version="unchanged-calibration",
    )


def us_payload(*, bid=0.39, ask=0.40, ended=False):
    return {
        "events": [{
            "id": "us-event-1",
            "slug": "away-at-home",
            "title": "Away at Home",
            "start": "2027-01-15T00:00:00Z",
            "ended": ended,
            "markets": [{
                "id": "market-1",
                "slug": "away-at-home-moneyline",
                "question": "Away at Home",
                "market_type": "moneyline",
                "active": True,
                "closed": False,
                "hidden": False,
                "state": "OPEN",
                "long_best_bid": bid,
                "long_best_ask": ask,
                "minimum_trade_quantity": 1,
                "minimum_tick_size": 0.01,
                "sides": [
                    {"id": "away", "description": "Away", "long": True, "tradable": True},
                    {"id": "home", "description": "Home", "long": False, "tradable": True},
                ],
            }],
        }]
    }


def soccer_event():
    return Event(
        id="soccer-event-1",
        name="San Jose Earthquakes vs. Los Angeles Galaxy",
        sport="soccer",
        league="MLS",
        home="San Jose Earthquakes",
        away="Los Angeles Galaxy",
        game_start="2027-01-15T00:00:00Z",
    )


def soccer_signal(clock, outcome, *, market="moneyline", action="WATCH"):
    return Signal(
        event_id="soccer-event-1",
        market=market,
        outcome=outcome,
        model_probability=0.65,
        market_probability=0.40,
        edge=0.10,
        confidence=85.0,
        action=action,
        reasons=[],
        observed_at=datetime.fromtimestamp(clock(), timezone.utc),
        n_reference_sources=2,
        required_edge=0.02,
        decision_id=f"decision-{market}-{outcome}",
        engine_version="unchanged-engine",
        configuration_hash="unchanged-config",
        model_version="unchanged-model",
        calibration_version="unchanged-calibration",
    )


def binary_soccer_payload(*, outcome="San Jose Earthquakes", bid=0.39, ask=0.40):
    suffix = "draw" if outcome == "Draw" else "sje"
    team_name = "" if outcome == "Draw" else outcome
    question = (
        "Will the match end in a draw?"
        if outcome == "Draw"
        else f"Will {outcome} win against Los Angeles Galaxy?"
    )
    return {
        "events": [{
            "id": "us-soccer-event-1",
            "slug": "mls-sje-lag-2026-07-25",
            "title": "San Jose Earthquakes vs. Los Angeles Galaxy",
            "start": "2027-01-15T00:00:00Z",
            "ended": False,
            "markets": [{
                "id": f"market-{suffix}",
                "slug": f"atc-mls-sje-lag-2026-07-25-{suffix}",
                "question": question,
                "market_type": "soccer_team_full_time_winner",
                "market_type_v2": "drawable_outcome",
                "active": True,
                "closed": False,
                "hidden": False,
                "state": "OPEN",
                "long_best_bid": bid,
                "long_best_ask": ask,
                "minimum_trade_quantity": 1,
                "minimum_tick_size": 0.01,
                "sides": [
                    {
                        "id": "yes",
                        "description": "Yes",
                        "team_name": team_name,
                        "long": True,
                        "tradable": True,
                    },
                    {
                        "id": "no",
                        "description": "No",
                        "team_name": team_name,
                        "long": False,
                        "tradable": True,
                    },
                ],
            }],
        }],
    }


def make_trader(
    tmp_path,
    clock,
    orders=None,
    *,
    fail_on_orders=False,
    book=None,
):
    orders = orders or FakeOrders()
    return PolymarketUSAutoTrader(
        str(tmp_path / "trading.db"),
        key_id="key",
        secret_key="secret",
        client_factory=client_factory(
            orders,
            fail_on_orders=fail_on_orders,
            book=book,
        ),
        clock=clock,
    )


def test_policy_keeps_prices_strictly_inside_existing_hard_bracket():
    with pytest.raises(TradingPolicyError, match="5c–95c"):
        TradingPolicy.from_mapping({"min_entry_price": 0.05})
    with pytest.raises(TradingPolicyError, match="5c–95c"):
        TradingPolicy.from_mapping({"max_entry_price": 0.95})


def test_dry_run_records_position_without_accessing_order_resource(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })

    result = trader.run_cycle([(event(), [signal(clock)])], us_payload())

    assert result["entries"] == 1
    positions = trader.positions(open_only=True)
    assert len(positions) == 1
    assert positions[0]["mode"] == "dry_run"
    assert positions[0]["entry_cost"] == pytest.approx(0.40)
    assert any(item["status"] == "simulated_fill" for item in trader.journal())


def test_dry_run_uses_nested_authenticated_book_and_current_price(tmp_path):
    clock = Clock()
    book = {
        "marketData": {
            "offers": [
                {"qty": "80", "px": {"value": "0.3200"}},
                {"qty": "50", "px": {"value": "0.3250"}},
            ],
            "bids": [
                {"qty": "60", "px": {"value": "0.3150"}},
                {"qty": "30", "px": {"value": "0.3100"}},
            ],
            "state": "MARKET_STATE_OPEN",
        }
    }
    trader = make_trader(
        tmp_path,
        clock,
        fail_on_orders=True,
        book=book,
    )
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "require_engine_entry": False,
        "min_edge": 0,
        "min_book_shares": 1,
    })

    result = trader.run_cycle(
        [(event(), [signal(clock, action="WATCH")])],
        us_payload(bid=0.34, ask=0.345),
    )

    assert result["entries"] == 1
    position = trader.positions(open_only=True)[0]
    assert position["entry_cost"] == pytest.approx(0.32)
    evaluation = trader.status()["last_cycle_evaluations"][0]
    assert evaluation["authenticated_book_state"] == "MARKET_STATE_OPEN"
    assert evaluation["authenticated_entry_cost"] == pytest.approx(0.32)
    assert evaluation["authenticated_execution_edge"] == pytest.approx(0.28)
    assert evaluation["executable_book_shares"] == pytest.approx(80)
    journal = next(
        item["details"]
        for item in trader.journal()
        if item["status"] == "simulated_fill"
    )
    assert journal["public_entry_cost"] == pytest.approx(0.345)
    assert journal["configured_min_book_shares"] == 1
    assert journal["required_edge"] == pytest.approx(0.02)


def test_permissive_dry_run_uses_positive_us_edge_despite_negative_display_edge(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "require_engine_entry": False,
        "min_edge": 0,
    })

    result = trader.run_cycle(
        [(event(), [signal(clock, action="WATCH", edge=-0.02)])],
        us_payload(),
    )

    assert result["entries"] == 1
    evaluation = trader.status()["last_cycle_evaluations"][0]
    assert evaluation["state"] == "simulated_fill"
    assert evaluation["signal_edge"] == pytest.approx(-0.02)
    assert evaluation["authenticated_execution_edge"] == pytest.approx(0.20)


def test_live_mode_retains_positive_source_edge_gate(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "live",
        "require_engine_entry": False,
        "min_edge": 0,
    })

    result = trader.run_cycle(
        [(event(), [signal(clock, action="WATCH", edge=-0.02)])],
        us_payload(),
    )

    assert result["entries"] == 0
    evaluation = trader.status()["last_cycle_evaluations"][0]
    assert evaluation["state"] == "research_only"
    assert evaluation["reason"] == (
        "existing signal edge -2.0c is below the configured +0.0c floor"
    )
    rejection = next(
        item["details"]
        for item in trader.journal()
        if item["kind"] == "qualification"
    )
    assert rejection["configured_min_edge"] == 0
    assert rejection["signal_edge"] == pytest.approx(-0.02)
    assert rejection["execution_mode"] == "live"


@pytest.mark.parametrize("outcome", ["San Jose Earthquakes", "Draw"])
def test_dry_run_maps_binary_soccer_moneylines_and_records_the_engine_outcome(
    tmp_path,
    outcome,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "require_engine_entry": False,
    })

    result = trader.run_cycle(
        [(soccer_event(), [soccer_signal(clock, outcome)])],
        binary_soccer_payload(outcome=outcome),
    )

    assert result["entries"] == 1
    position = trader.positions(open_only=True)[0]
    assert position["selection"] == outcome
    assert position["market_slug"].endswith(
        "draw" if outcome == "Draw" else "sje"
    )
    evaluation = trader.status()["last_cycle_evaluations"][0]
    assert evaluation["state"] == "simulated_fill"
    assert evaluation["us_entry_cost"] == pytest.approx(0.40)
    assert evaluation["us_execution_edge"] == pytest.approx(0.25)


def test_binary_soccer_contract_for_a_different_team_does_not_map(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "automation_enabled": True,
        "require_engine_entry": False,
    })

    result = trader.run_cycle(
        [(soccer_event(), [soccer_signal(clock, "Los Angeles Galaxy")])],
        binary_soccer_payload(outcome="San Jose Earthquakes"),
    )

    assert result["entries"] == 0
    evaluation = trader.status()["last_cycle_evaluations"][0]
    assert evaluation["state"] == "research_only"
    assert evaluation["reason"] == "no exact US market/line/outcome mapping"


def test_full_game_soccer_spread_maps_by_exact_team_and_signed_line(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "require_engine_entry": False,
    })
    market = {
        "id": "spread-1",
        "slug": "asc-mls-sje-lag-2026-07-25-neg-1pt5",
        "question": "Will San Jose cover -1.5 vs Los Angeles Galaxy?",
        "market_type": "soccer_team_full_game_spread",
        "market_type_v2": "spread",
        "line": -1.5,
        "active": True,
        "closed": False,
        "hidden": False,
        "state": "OPEN",
        "long_best_bid": 0.39,
        "long_best_ask": 0.40,
        "minimum_trade_quantity": 1,
        "sides": [
            {
                "description": "-1.50",
                "team_name": "San Jose Earthquakes",
                "long": True,
                "tradable": True,
            },
            {
                "description": "+1.50",
                "team_name": "Los Angeles Galaxy",
                "long": False,
                "tradable": True,
            },
        ],
    }
    half_market = {
        **market,
        "id": "spread-half",
        "slug": "asc-mls-sje-lag-2026-07-25-fh-neg-1pt5",
        "market_type": "soccer_team_first_half_spread",
    }
    payload = binary_soccer_payload()
    payload["events"][0]["markets"] = [market, half_market]

    result = trader.run_cycle(
        [(
            soccer_event(),
            [soccer_signal(clock, "San Jose Earthquakes -1.5", market="spread")],
        )],
        payload,
    )

    assert result["entries"] == 1
    position = trader.positions(open_only=True)[0]
    assert position["market_slug"] == market["slug"]
    assert position["position_side"] == "long"


def test_full_game_soccer_total_maps_without_matching_half_markets(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "require_engine_entry": False,
    })
    market = {
        "id": "total-1",
        "slug": "tsc-mls-sje-lag-2026-07-25-3pt5",
        "question": "Will the total be more than 3.5?",
        "market_type": "soccer_team_full_game_total",
        "market_type_v2": "total",
        "line": 3.5,
        "active": True,
        "closed": False,
        "hidden": False,
        "state": "OPEN",
        "long_best_bid": 0.39,
        "long_best_ask": 0.40,
        "minimum_trade_quantity": 1,
        "sides": [
            {"description": "Over", "long": True, "tradable": True},
            {"description": "Under", "long": False, "tradable": True},
        ],
    }
    payload = binary_soccer_payload()
    payload["events"][0]["markets"] = [market]

    result = trader.run_cycle(
        [(soccer_event(), [soccer_signal(clock, "Over 3.5", market="total")])],
        payload,
    )

    assert result["entries"] == 1
    assert trader.positions(open_only=True)[0]["market_slug"] == market["slug"]


def test_extreme_one_cent_contract_is_never_an_entry(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({"automation_enabled": True})

    result = trader.run_cycle(
        [(event(), [signal(clock, probability=0.20)])],
        us_payload(bid=0.005, ask=0.01),
    )

    assert result["entries"] == 0
    assert trader.positions(open_only=True) == []


def test_overlapping_cycle_is_skipped_instead_of_placing_a_duplicate(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({"automation_enabled": True})
    assert trader._cycle_lock.acquire(blocking=False)
    try:
        result = trader.run_cycle([(event(), [signal(clock)])], us_payload())
    finally:
        trader._cycle_lock.release()

    assert result["status"] == "busy"
    assert trader.positions(open_only=True) == []


def test_live_mode_requires_expiring_arm_and_previews_before_create(tmp_path):
    clock = Clock()
    orders = FakeOrders()
    trader = make_trader(tmp_path, clock, orders)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "live",
    })

    blocked = trader.run_cycle([(event(), [signal(clock)])], us_payload())
    assert blocked["entries"] == 0
    assert orders.created == []

    clock.value += 301  # clear candidate cooldown
    trader.arm(ARM_PHRASE)
    filled = trader.run_cycle([(event(), [signal(clock)])], us_payload())

    assert filled["entries"] == 1
    assert len(orders.previewed) == len(orders.created) == 1
    request = orders.created[0]
    assert request["type"] == "ORDER_TYPE_LIMIT"
    assert request["tif"] == "TIME_IN_FORCE_FILL_OR_KILL"
    assert request["manualOrderIndicator"] == "MANUAL_ORDER_INDICATOR_AUTOMATIC"


def test_restart_is_always_disarmed(tmp_path):
    clock = Clock()
    first = make_trader(tmp_path, clock)
    first.configure({"automation_enabled": True, "execution_mode": "live"})
    first.arm(ARM_PHRASE)
    assert first.is_armed()
    first.close()

    second = make_trader(tmp_path, clock)
    assert second.policy.execution_mode == "live"
    assert second.policy.automation_enabled is True
    assert second.is_armed() is False


def test_one_cent_gain_does_not_cash_out_but_logical_edge_decay_does(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "auto_cashout": True,
        "min_hold_minutes": 15,
        "profit_target": 0.15,
    })
    trader.run_cycle([(event(), [signal(clock)])], us_payload())

    clock.value += 16 * 60
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.60)])],
        us_payload(bid=0.41, ask=0.42),
    )
    assert len(trader.positions(open_only=True)) == 1

    clock.value += 301
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.45)])],
        us_payload(bid=0.48, ask=0.49),
    )
    assert trader.positions(open_only=True) == []
    closed = trader.positions()[0]
    assert closed["exit_reason"] == "profit_lock_after_edge_decay"
    assert closed["realized_pnl"] > 0


def test_performance_separates_dry_run_live_and_combined_results(tmp_path):
    clock = Clock()
    orders = FakeOrders()
    trader = make_trader(tmp_path, clock, orders)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })

    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    first = trader.positions(open_only=True)[0]
    trader._close_position(
        first["id"],
        exit_value=0.50,
        reason="test_profit",
        order_id=None,
    )

    clock.value += 301
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    second = trader.positions(open_only=True)[0]
    trader._close_position(
        second["id"],
        exit_value=0.30,
        reason="test_loss",
        order_id=None,
    )

    clock.value += 301
    trader.configure({
        "execution_mode": "live",
        "require_engine_entry": True,
    })
    trader.arm(ARM_PHRASE)
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    third = trader.positions(open_only=True)[0]
    trader._close_position(
        third["id"],
        exit_value=0.45,
        reason="test_live_profit",
        order_id="test-exit",
    )

    summary = trader.performance()
    dry = summary["modes"]["dry_run"]
    live = summary["modes"]["live"]
    combined = summary["combined"]

    assert dry["total_positions"] == dry["closed_positions"] == 2
    assert dry["open_positions"] == 0
    assert (dry["wins"], dry["losses"], dry["pushes"]) == (1, 1, 0)
    assert dry["win_rate"] == pytest.approx(0.5)
    assert dry["realized_net_usd"] == pytest.approx(0.0)
    assert dry["total_net_complete"] is True

    assert live["total_positions"] == live["closed_positions"] == 1
    assert (live["wins"], live["losses"], live["pushes"]) == (1, 0, 0)
    assert live["win_rate"] == pytest.approx(1.0)
    assert live["realized_net_usd"] > 0

    assert combined["total_positions"] == 3
    assert (combined["wins"], combined["losses"], combined["pushes"]) == (2, 1, 0)
    assert combined["win_rate"] == pytest.approx(2 / 3)
    assert combined["total_net_usd"] == pytest.approx(
        dry["total_net_usd"] + live["total_net_usd"]
    )
    assert "realized P/L" in summary["definitions"]["win_loss_push"]


def test_performance_marks_open_pnl_and_reports_empty_modes(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)

    empty = trader.performance()
    assert empty["modes"]["dry_run"]["total_positions"] == 0
    assert empty["modes"]["live"]["total_positions"] == 0
    assert empty["combined"]["win_rate"] is None

    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })
    trader.run_cycle([(event(), [signal(clock)])], us_payload())

    summary = trader.performance()["modes"]["dry_run"]
    assert summary["open_positions"] == 1
    assert summary["priced_open_positions"] == 1
    assert summary["closed_positions"] == 0
    assert summary["open_unrealized_pnl_usd"] < 0
    assert summary["total_net_usd"] == pytest.approx(
        summary["open_unrealized_pnl_usd"]
    )


def test_live_cashout_uses_the_executable_long_bid_for_a_long_position(tmp_path):
    clock = Clock()
    orders = FakeOrders()
    trader = make_trader(tmp_path, clock, orders)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "live",
        "auto_cashout": True,
        "min_hold_minutes": 15,
        "profit_target": 0.15,
    })
    trader.arm(ARM_PHRASE)
    trader.run_cycle([(event(), [signal(clock)])], us_payload())

    clock.value += 16 * 60
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.45)])],
        us_payload(bid=0.48, ask=0.49),
    )

    assert len(orders.created) == 2
    exit_order = orders.created[1]
    assert exit_order["intent"] == "ORDER_INTENT_SELL_LONG"
    assert exit_order["price"]["value"] == pytest.approx(0.48)
    assert trader.positions(open_only=True) == []


def test_clear_dry_run_positions_force_wipes_without_a_market_quote(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })
    trader.run_cycle([(event(), [signal(clock)])], us_payload())

    result = trader.liquidate_open_positions({"events": []}, mode="dry_run")

    assert result["requested"] == result["attempted"] == result["filled"] == 1
    assert result["failed"] == result["remaining"] == 0
    assert result["deleted_positions"] == 1
    assert result["forced_open_positions"] == 1
    assert result["deleted_closed_positions"] == 0
    assert result["quote_required"] is False
    assert result["verified_remaining_positions"] == 0
    assert result["automation_enabled"] is False
    assert trader.positions() == []
    assert trader.performance()["modes"]["dry_run"]["total_positions"] == 0
    assert trader.run_cycle([(event(), [signal(clock)])], us_payload())["status"] == "off"
    assert trader.positions() == []
    assert any(
        item["kind"] == "history_reset" and item["status"] == "forced"
        for item in trader.journal()
    )


def test_clear_live_positions_requires_live_mode_arm_and_exact_confirmation(tmp_path):
    clock = Clock()
    orders = FakeOrders()
    trader = make_trader(tmp_path, clock, orders)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "live",
    })
    trader.arm(ARM_PHRASE)
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    trader.disarm()

    with pytest.raises(TradingPolicyError, match="disarmed"):
        trader.liquidate_open_positions(
            us_payload(bid=0.47, ask=0.48),
            mode="live",
            confirmation=LIVE_LIQUIDATION_PHRASE,
        )

    trader.arm(ARM_PHRASE)
    with pytest.raises(TradingPolicyError, match=LIVE_LIQUIDATION_PHRASE):
        trader.liquidate_open_positions(
            us_payload(bid=0.47, ask=0.48),
            mode="live",
            confirmation="sell everything",
        )

    result = trader.liquidate_open_positions(
        us_payload(bid=0.47, ask=0.48),
        mode="live",
        confirmation=LIVE_LIQUIDATION_PHRASE,
    )

    assert result["filled"] == 1
    assert result["remaining"] == 0
    assert len(orders.created) == 2
    assert orders.created[-1]["intent"] == "ORDER_INTENT_SELL_LONG"
    assert orders.created[-1]["tif"] == "TIME_IN_FORCE_FILL_OR_KILL"
    assert trader.positions()[0]["exit_reason"] == "manual_clear_all_live"


def test_clear_dry_run_positions_also_removes_completed_history(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    position = trader.positions(open_only=True)[0]
    trader._close_position(
        position["id"],
        exit_value=0.47,
        reason="test_completed_dry_run",
        order_id=None,
    )

    result = trader.liquidate_open_positions({"events": []}, mode="dry_run")

    assert result["requested"] == result["deleted_positions"] == 1
    assert result["attempted"] == result["filled"] == 0
    assert result["deleted_closed_positions"] == 1
    assert result["failed"] == result["remaining"] == 0
    assert result["automation_enabled"] is False
    assert trader.positions() == []


def test_executive_dry_run_reset_ignores_a_held_cycle_lock(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    assert trader._cycle_lock.acquire(blocking=False)
    try:
        result = trader.clear_dry_run_history(DRY_RUN_HISTORY_CLEAR_PHRASE)
    finally:
        trader._cycle_lock.release()

    assert result["deleted_positions"] == 1
    assert result["verified_remaining_positions"] == 0
    assert result["automation_enabled"] is False
    assert trader.positions() == []
    assert trader.policy.automation_enabled is False


def test_clear_dry_run_history_force_wipes_open_and_closed_but_preserves_live_audit(
    tmp_path,
):
    clock = Clock()
    orders = FakeOrders()
    trader = make_trader(tmp_path, clock, orders)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "live",
    })
    trader.arm(ARM_PHRASE)
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    live_position = trader.positions(open_only=True)[0]
    trader._close_position(
        live_position["id"],
        exit_value=0.50,
        reason="test_live_history",
        order_id="live-exit",
    )

    clock.value += 301
    trader.configure({"execution_mode": "dry_run"})
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    completed_dry = trader.positions(open_only=True)[0]
    trader._close_position(
        completed_dry["id"],
        exit_value=0.47,
        reason="test_completed_dry_run",
        order_id=None,
    )
    clock.value += 301
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    journal_before = len(trader.journal(limit=500))

    with pytest.raises(TradingPolicyError, match=DRY_RUN_HISTORY_CLEAR_PHRASE):
        trader.clear_dry_run_history("clear it")

    result = trader.clear_dry_run_history(DRY_RUN_HISTORY_CLEAR_PHRASE)

    assert result["deleted_positions"] == 2
    assert result["forced_open_positions"] == 1
    assert result["deleted_closed_positions"] == 1
    assert result["remaining"] == 0
    assert result["verified_remaining_positions"] == 0
    assert result["automation_enabled"] is False
    assert result["live_disarmed"] is True
    assert result["quote_required"] is False
    assert result["live_positions_preserved"] is True
    assert result["execution_journal_preserved"] is True
    remaining = trader.positions()
    assert len(remaining) == 1
    assert remaining[0]["mode"] == "live"
    performance = trader.performance()
    assert performance["modes"]["dry_run"]["total_positions"] == 0
    assert performance["modes"]["live"]["total_positions"] == 1
    journal = trader.journal(limit=500)
    assert len(journal) == journal_before + 1
    reset_items = [item for item in journal if item["kind"] == "history_reset"]
    assert len(reset_items) == 1
    assert reset_items[0]["status"] == "forced"
    assert reset_items[0]["details"]["deleted_positions"] == 2
    assert reset_items[0]["details"]["forced_open_positions"] == 1
    assert reset_items[0]["details"]["verified_remaining_positions"] == 0


def test_emergency_stop_cancels_only_managed_open_order_ids(tmp_path):
    clock = Clock()
    orders = FakeOrders()
    trader = make_trader(tmp_path, clock, orders)
    trader.configure({"automation_enabled": True, "execution_mode": "live"})
    trader.arm(ARM_PHRASE)
    trader._record_order("managed-1", "market-1", None, "entry", "open")

    stopped = trader.emergency_stop()

    assert stopped["armed"] is False
    assert stopped["policy"]["automation_enabled"] is False
    assert orders.canceled == [
        ("managed-1", {"marketSlug": "market-1"})
    ]


def test_venue_errors_are_redacted_before_they_can_reach_the_journal(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)

    safe = trader._safe_error(RuntimeError("request used key and secret"))

    assert safe == "request used [redacted] and [redacted]"
