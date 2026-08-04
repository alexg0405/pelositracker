from datetime import datetime, timezone
import threading
from types import SimpleNamespace

import pytest

from app.models import Event, GameState, Signal
from app.polymarket_us_trading import (
    ARM_PHRASE,
    CORE_ENGINE_GATES,
    DRY_RUN_HISTORY_CLEAR_PHRASE,
    LIVE_LIQUIDATION_PHRASE,
    LIVE_PERFORMANCE_RESET_PHRASE,
    LIVE_POSITION_EXIT_PHRASE,
    MAX_ARM_SECONDS,
    POLICY_ADVICE_APPLY_PHRASE,
    PolymarketUSAutoTrader,
    RISK_SESSION_RESET_PHRASE,
    TradingPolicy,
    TradingPolicyError,
    _estimated_taker_fee,
    risk_preset_fields,
)
from app.lines import SUPPORTED_MARKET_SCOPES


def passing_core_gates():
    return [
        {"code": code, "passed": True, "status": "pass"}
        for code in CORE_ENGINE_GATES
    ]


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


class FirstExitDoesNotFillOrders(FakeOrders):
    """Fill the entry, then emulate a disappearing first cash-out quote."""

    def create(self, params):
        self.created.append(params)
        order_id = f"order-{len(self.created)}"
        if len(self.created) == 2:
            return {"id": order_id, "executions": []}
        return {
            "id": order_id,
            "executions": [{
                "lastShares": str(params["quantity"]),
                "lastPx": params["price"],
                "type": "EXECUTION_TYPE_FILL",
            }],
        }


class FakeClient:
    def __init__(
        self,
        orders,
        *,
        fail_on_orders=False,
        book=None,
        portfolio_positions=None,
    ):
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
        self._portfolio_positions = (
            portfolio_positions
            if portfolio_positions is not None
            else {
                "away-at-home-moneyline": {
                    "netPosition": "100",
                    "qtyAvailable": "100",
                    "cost": "40",
                    "cashValue": "39",
                    "realized": "0",
                    "marketMetadata": {
                        "title": "Away at Home",
                        "outcome": "Away",
                    },
                }
            }
        )
        self.portfolio = SimpleNamespace(
            positions=lambda _params=None: {
                "positions": self._portfolio_positions,
                "eof": True,
            }
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


def client_factory(
    orders,
    *,
    fail_on_orders=False,
    book=None,
    portfolio_positions=None,
):
    def factory(**kwargs):
        assert kwargs["key_id"] == "key"
        assert kwargs["secret_key"] == "secret"
        return FakeClient(
            orders,
            fail_on_orders=fail_on_orders,
            book=book,
            portfolio_positions=portfolio_positions,
        )

    return factory


def test_default_policy_is_aggressive_but_bounded_for_ten_dollar_rollout():
    policy = TradingPolicy()

    assert policy.automation_enabled is False
    assert policy.execution_mode == "dry_run"
    assert policy.auto_cashout is False
    assert policy.adaptive_exit_enabled is False
    assert policy.adaptive_exit_profile == "observe"
    assert policy.adaptive_exit_horizon_minutes == 3.0
    assert policy.adaptive_exit_min_samples == 30
    assert policy.adaptive_exit_max_tightening == 0.35
    assert policy.volatility_stop_enabled is False
    assert policy.stop_confirmation_readings == 3
    assert policy.stop_grace_minutes == pytest.approx(2.0)
    assert policy.catastrophic_stop_multiplier == pytest.approx(1.75)
    assert policy.post_exit_tracking_minutes == pytest.approx(30.0)
    assert policy.require_engine_entry is True
    assert policy.required_engine_gates == CORE_ENGINE_GATES
    assert policy.allowed_market_types == ("moneyline", "spread", "total")
    assert policy.allowed_market_scopes == SUPPORTED_MARKET_SCOPES
    assert policy.allow_live_segment_markets is False
    assert policy.max_total_exposure_usd == 9.50
    assert policy.minimum_cash_reserve_usd == 0.50
    assert policy.max_position_usd == 1.75
    assert policy.max_event_exposure_usd == 3.0
    assert policy.max_daily_loss_usd == 5.0
    assert policy.max_open_positions == 6
    assert policy.max_orders_per_hour == 6
    assert policy.min_edge == 0.03
    assert policy.max_edge == 1.0
    assert policy.global_entry_enabled is True
    assert policy.max_entries_per_event_per_hour == 3
    assert policy.min_mlb_fraction_remaining == 0.0
    assert policy.min_signal_quality == 60.0
    assert policy.max_signal_quality == 100.0
    assert policy.min_source_agreement == 0.0
    assert policy.max_signal_age_seconds == 120.0
    assert policy.entry_confirmation_readings == 1
    assert policy.max_confirmation_price_drift == 1.0
    assert policy.min_reference_sources == 2
    assert policy.min_entry_price == 0.10
    assert policy.max_entry_price == 0.90
    assert policy.max_spread == 0.04
    assert policy.min_book_shares == 3.0
    assert policy.min_hold_minutes == 10.0
    assert policy.profit_target == 0.10
    assert policy.minimum_locked_profit == 0.02
    assert policy.trailing_drawdown == 0.04
    assert policy.stop_loss == 0.20


def test_analysis_cycle_can_match_fast_monitoring_without_api_polling():
    policy = TradingPolicy.from_mapping(
        {
            "cycle_seconds": 1.5,
            "candidate_cooldown_seconds": 10,
        }
    )
    assert policy.cycle_seconds == pytest.approx(1.5)

    with pytest.raises(TradingPolicyError, match="between 1 and 300"):
        TradingPolicy.from_mapping(
            {
                "cycle_seconds": 0.5,
            }
        )


def test_line_execution_profiles_overlay_global_values_by_game_stage():
    policy = TradingPolicy.from_mapping({
        "min_edge": 0.04,
        "profit_target": 0.10,
        "minimum_locked_profit": 0.02,
        "line_execution_profiles": [
            {
                "market_type": "moneyline",
                "game_stage": "all",
                "overrides": {
                    "min_edge": 0.06,
                    "profit_target": 0.08,
                    "max_signal_quality": 92.0,
                    "entry_confirmation_readings": 2,
                },
            },
            {
                "market_type": "moneyline",
                "game_stage": "late",
                "overrides": {
                    "min_edge": 0.09,
                    "minimum_locked_profit": 0.03,
                },
            },
            {
                "market_type": "spread",
                "game_stage": "middle",
                "enabled": False,
            },
        ],
    })

    early, early_key = policy.execution_policy_for("moneyline", 0.70)
    late, late_key = policy.execution_policy_for("moneyline", 0.10)
    disabled, disabled_key = policy.execution_policy_for("spread", 0.40)
    total, total_key = policy.execution_policy_for("total", 0.10)

    assert early is not None
    assert early.min_edge == pytest.approx(0.06)
    assert early.profit_target == pytest.approx(0.08)
    assert early.minimum_locked_profit == pytest.approx(0.02)
    assert early.max_signal_quality == pytest.approx(92.0)
    assert early.entry_confirmation_readings == 2
    assert early_key == "moneyline/all"
    assert late is not None
    assert late.min_edge == pytest.approx(0.09)
    assert late.profit_target == pytest.approx(0.08)
    assert late.minimum_locked_profit == pytest.approx(0.03)
    assert late_key == "moneyline/all+moneyline/late"
    assert disabled is None
    assert disabled_key == "spread/middle"
    assert total is policy
    assert total_key == "global"


def test_line_execution_profiles_reject_invalid_merged_limits():
    with pytest.raises(
        TradingPolicyError,
        match="minimum locked profit must be smaller",
    ):
        TradingPolicy.from_mapping({
            "line_execution_profiles": [
                {
                    "market_type": "total",
                    "game_stage": "all",
                    "overrides": {"profit_target": 0.04},
                },
                {
                    "market_type": "total",
                    "game_stage": "late",
                    "overrides": {"minimum_locked_profit": 0.05},
                },
            ],
        })


def test_policy_accepts_decimal_limits_and_rejects_unknown_engine_gates():
    policy = TradingPolicy.from_mapping({
        "max_total_exposure_usd": 9.37,
        "max_position_usd": 1.23,
        "max_event_exposure_usd": 2.34,
        "min_signal_quality": 61.25,
        "max_signal_quality": 91.75,
        "min_source_agreement": 52.5,
        "max_signal_age_seconds": 45.5,
        "entry_confirmation_readings": 2,
        "max_confirmation_price_drift": 0.0125,
        "min_hold_minutes": 7.5,
        "min_book_shares": 2.25,
        "profit_target": 0.0875,
        "minimum_locked_profit": 0.025,
        "trailing_drawdown": 0.0325,
        "stop_loss": 0.175,
        "adaptive_exit_enabled": True,
        "adaptive_exit_profile": "balanced",
        "adaptive_exit_horizon_minutes": 2.5,
        "adaptive_exit_min_samples": 25,
        "adaptive_exit_max_tightening": 0.275,
        "volatility_stop_enabled": True,
        "stop_confirmation_readings": 4,
        "stop_grace_minutes": 1.75,
        "catastrophic_stop_multiplier": 1.6,
        "post_exit_tracking_minutes": 45.5,
        "required_engine_gates": ["provider_freshness", "market_identity"],
        "allowed_market_types": ["moneyline", "total"],
    })

    assert policy.max_total_exposure_usd == pytest.approx(9.37)
    assert policy.max_position_usd == pytest.approx(1.23)
    assert policy.min_signal_quality == pytest.approx(61.25)
    assert policy.max_signal_quality == pytest.approx(91.75)
    assert policy.min_source_agreement == pytest.approx(52.5)
    assert policy.max_signal_age_seconds == pytest.approx(45.5)
    assert policy.entry_confirmation_readings == 2
    assert policy.max_confirmation_price_drift == pytest.approx(0.0125)
    assert policy.min_hold_minutes == pytest.approx(7.5)
    assert policy.min_book_shares == pytest.approx(2.25)
    assert policy.minimum_locked_profit == pytest.approx(0.025)
    assert policy.adaptive_exit_enabled is True
    assert policy.adaptive_exit_profile == "balanced"
    assert policy.adaptive_exit_horizon_minutes == pytest.approx(2.5)
    assert policy.adaptive_exit_min_samples == 25
    assert policy.adaptive_exit_max_tightening == pytest.approx(0.275)
    assert policy.volatility_stop_enabled is True
    assert policy.stop_confirmation_readings == 4
    assert policy.stop_grace_minutes == pytest.approx(1.75)
    assert policy.catastrophic_stop_multiplier == pytest.approx(1.6)
    assert policy.post_exit_tracking_minutes == pytest.approx(45.5)
    assert policy.required_engine_gates == (
        "provider_freshness",
        "market_identity",
    )
    assert policy.allowed_market_types == ("moneyline", "total")

    with pytest.raises(TradingPolicyError, match="unknown required engine gate"):
        TradingPolicy.from_mapping({"required_engine_gates": ["invented_gate"]})
    with pytest.raises(TradingPolicyError, match="unknown automatic-entry"):
        TradingPolicy.from_mapping({"allowed_market_types": ["player_prop"]})
    with pytest.raises(TradingPolicyError, match="at least one"):
        TradingPolicy.from_mapping({"allowed_market_types": []})
    with pytest.raises(TradingPolicyError, match="minimum quality"):
        TradingPolicy.from_mapping({
            "min_signal_quality": 80,
            "max_signal_quality": 70,
        })
    with pytest.raises(TradingPolicyError, match="minimum edge < maximum"):
        TradingPolicy.from_mapping({"min_edge": 0.10, "max_edge": 0.10})
    with pytest.raises(
        TradingPolicyError,
        match="minimum locked profit.*smaller",
    ):
        TradingPolicy.from_mapping({
            "profit_target": 0.05,
            "minimum_locked_profit": 0.05,
        })


def test_live_taker_fee_estimate_uses_current_symmetric_rounded_formula():
    assert _estimated_taker_fee(
        quantity=2,
        contract_price=0.28,
    ) == pytest.approx(0.02)
    assert _estimated_taker_fee(
        quantity=1,
        contract_price=0.445,
    ) == pytest.approx(0.01)
    assert _estimated_taker_fee(
        quantity=0,
        contract_price=0.50,
    ) == 0


def test_named_risk_preset_derives_bounded_limits_from_one_hard_allocation():
    values = risk_preset_fields("aggressive", 5.0)
    policy = TradingPolicy.from_mapping(values)

    assert policy.trading_allocation_usd == 5.0
    assert policy.risk_preset == "aggressive"
    assert policy.max_total_exposure_usd == 4.75
    assert policy.max_position_usd == 1.25
    assert policy.max_event_exposure_usd == 2.50
    assert policy.minimum_cash_reserve_usd == 0.25
    assert policy.max_total_exposure_usd <= policy.trading_allocation_usd


def test_hard_allocation_rejects_a_custom_exposure_above_its_boundary():
    with pytest.raises(TradingPolicyError, match="hard trading allocation"):
        TradingPolicy.from_mapping({
            "trading_allocation_usd": 5.0,
            "max_total_exposure_usd": 5.01,
        })


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
    market="moneyline",
    outcome="Away",
    probability=0.60,
    action="PAPER_BET",
    quality=85.0,
    agreement=85.0,
    edge=0.05,
    gate_results=None,
):
    return Signal(
        event_id="event-1",
        market=market,
        outcome=outcome,
        model_probability=probability,
        market_probability=0.40,
        edge=edge,
        confidence=quality,
        quality_agreement=agreement,
        action=action,
        reasons=[],
        observed_at=datetime.fromtimestamp(clock(), timezone.utc),
        n_reference_sources=2,
        required_edge=0.02,
        decision_id=f"decision-1-{market}-{outcome}",
        engine_version="unchanged-engine",
        configuration_hash="unchanged-config",
        model_version="unchanged-model",
        calibration_version="unchanged-calibration",
        gate_results=(
            passing_core_gates()
            if gate_results is None
            else gate_results
        ),
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


def mlb_event():
    return Event(
        id="event-1",
        name="Away at Home",
        sport="baseball",
        league="MLB",
        home="Home",
        away="Away",
        game_start="2027-01-15T00:00:00Z",
    )


def mlb_segment_payload(
    *,
    market_type="baseball_team_first_five_winner",
    question="Will Away win the first five innings?",
    line=None,
    team_name="Away",
):
    return {
        "events": [{
            "id": "us-event-1",
            "slug": "away-at-home",
            "title": "Away at Home",
            "start": "2027-01-15T00:00:00Z",
            "ended": False,
            "markets": [{
                "id": "segment-market",
                "slug": "away-at-home-segment",
                "question": question,
                "market_type": market_type,
                "market_scope": (
                    "first_inning"
                    if "first_inning" in market_type
                    else "first_five_innings"
                ),
                "line": line,
                "active": True,
                "closed": False,
                "hidden": False,
                "state": "OPEN",
                "long_best_bid": 0.39,
                "long_best_ask": 0.40,
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
        gate_results=passing_core_gates(),
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
    portfolio_positions=None,
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
            portfolio_positions=portfolio_positions,
        ),
        clock=clock,
    )


def mlb_event_and_state(clock, *, period="Top 5", home_score=1, away_score=1):
    mlb = Event(
        id="mlb-event-1",
        name="Away MLB at Home MLB",
        sport="baseball",
        league="MLB",
        home="Home MLB",
        away="Away MLB",
    )
    observed = datetime.fromtimestamp(clock(), timezone.utc)
    live_state = GameState(
        event_id=mlb.id,
        home_score=home_score,
        away_score=away_score,
        period=period,
        clock="",
        source="test-mlb",
        provider_timestamp=observed,
        received_at=observed,
        processed_at=observed,
        status="in_progress",
        live=True,
        ended=False,
    )
    return mlb, live_state


def test_state_aware_mlb_stop_requires_bounded_confirmation(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "volatility_stop_enabled": True,
        "stop_loss": 0.20,
        "stop_confirmation_readings": 3,
        "stop_grace_minutes": 2.0,
        "catastrophic_stop_multiplier": 1.75,
        "cycle_seconds": 30,
    })
    mlb, live_state = mlb_event_and_state(clock)
    position = {
        "id": "position-1",
        "event_id": mlb.id,
        "event_name": mlb.name,
        "market_slug": "over-7-5",
        "market_type": "total",
        "selection": "Over 7.5",
        "position_side": "long",
        "mode": "dry_run",
        "quantity": 2.0,
        "entry_cost": 0.40,
        "stop_triggered_ts": None,
        "stop_observation_count": 0,
        "stop_low_exit_value": None,
    }
    adaptive = trader._adaptive_exit.base_decision(
        profile="observe",
        reason="test",
        applicable=True,
        profit_target=0.10,
        trailing_drawdown=0.04,
        exit_edge=0.0,
        stop_loss=0.20,
    )

    reason, guard, triggered, count, low = trader._stop_guard_decision(
        position,
        event=mlb,
        state=live_state,
        exit_value=0.30,
        current_edge=0.05,
        return_fraction=-0.25,
        adaptive_exit=adaptive,
    )
    assert reason is None
    assert guard["status"] == "observing_recovery"
    assert count == 1

    position.update(
        stop_triggered_ts=triggered,
        stop_observation_count=count,
        stop_low_exit_value=low,
    )
    clock.value += 130
    live_state.received_at = datetime.fromtimestamp(clock(), timezone.utc)
    reason, guard, _, count, _ = trader._stop_guard_decision(
        position,
        event=mlb,
        state=live_state,
        exit_value=0.29,
        current_edge=0.04,
        return_fraction=-0.275,
        adaptive_exit=adaptive,
    )
    # One delayed poll cannot satisfy a three-reading confirmation by itself.
    assert reason is None
    assert count == 2
    assert guard["status"] == "observing_recovery"
    trader.close()


def test_catastrophic_boundary_holds_when_state_does_not_corroborate(tmp_path):
    """A lone gapped quote must not sell a position the game says is alive.

    The 2026-08-03 slate sold four positions on this path whose orders then
    filled 15-35c above the triggering quote and kept climbing: the book had
    refilled before the fill-or-kill executed. With live state available and
    the total still reachable, the quote is treated as suspect.
    """
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "volatility_stop_enabled": True,
        "stop_loss": 0.20,
        "catastrophic_stop_multiplier": 1.50,
        "stop_confirmation_readings": 3,
    })
    # Top 5, 1-1: an Over 7.5 is still reachable, so nothing corroborates a
    # collapse except the price itself.
    mlb, live_state = mlb_event_and_state(clock)
    position = {
        "id": "position-1",
        "event_id": mlb.id,
        "event_name": mlb.name,
        "market_slug": "over-7-5",
        "market_type": "total",
        "selection": "Over 7.5",
        "position_side": "long",
        "mode": "dry_run",
        "quantity": 2.0,
        "entry_cost": 0.40,
    }
    adaptive = trader._adaptive_exit.base_decision(
        profile="observe",
        reason="test",
        applicable=True,
        profit_target=0.10,
        trailing_drawdown=0.04,
        exit_edge=0.0,
        stop_loss=0.20,
    )

    reason, guard, _triggered, count, _low = trader._stop_guard_decision(
        position,
        event=mlb,
        state=live_state,
        exit_value=0.27,
        current_edge=0.05,
        return_fraction=-0.325,
        adaptive_exit=adaptive,
    )

    assert reason is None
    assert guard["catastrophic_price_unconfirmed"] is True
    assert guard["status"] == "observing_recovery"
    assert count == 1
    trader.close()


def test_catastrophic_boundary_stays_immediate_when_state_confirms(tmp_path):
    """A real collapse still exits at once: the score makes it irreversible."""
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "volatility_stop_enabled": True,
        "stop_loss": 0.20,
        "catastrophic_stop_multiplier": 1.50,
    })
    # 9 runs already scored puts an Under 7.5 permanently out of reach.
    mlb, live_state = mlb_event_and_state(
        clock, period="Top 8", home_score=5, away_score=4
    )
    position = {
        "id": "position-1",
        "event_id": mlb.id,
        "event_name": mlb.name,
        "market_slug": "under-7-5",
        "market_type": "total",
        "selection": "Under 7.5",
        "position_side": "long",
        "mode": "dry_run",
        "quantity": 2.0,
        "entry_cost": 0.40,
    }
    adaptive = trader._adaptive_exit.base_decision(
        profile="observe",
        reason="test",
        applicable=True,
        profit_target=0.10,
        trailing_drawdown=0.04,
        exit_edge=0.0,
        stop_loss=0.20,
    )

    reason, guard, *_ = trader._stop_guard_decision(
        position,
        event=mlb,
        state=live_state,
        exit_value=0.02,
        current_edge=0.05,
        return_fraction=-0.95,
        adaptive_exit=adaptive,
    )

    assert reason == "catastrophic_stop_loss"
    assert guard["status"] == "immediate"
    trader.close()


def test_state_aware_mlb_stop_treats_missing_edge_as_bounded_not_reversal(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "volatility_stop_enabled": True,
        "stop_loss": 0.20,
        "stop_confirmation_readings": 3,
        "stop_grace_minutes": 2.0,
    })
    mlb, live_state = mlb_event_and_state(clock)
    adaptive = trader._adaptive_exit.base_decision(
        profile="observe",
        reason="test",
        applicable=True,
        profit_target=0.10,
        trailing_drawdown=0.04,
        exit_edge=0.0,
        stop_loss=0.20,
    )

    reason, guard, *_ = trader._stop_guard_decision(
        {
            "id": "position-1",
            "event_id": mlb.id,
            "event_name": mlb.name,
            "market_slug": "over-7-5",
            "market_type": "total",
            "selection": "Over 7.5",
            "position_side": "long",
            "mode": "dry_run",
            "quantity": 2.0,
            "entry_cost": 0.40,
        },
        event=mlb,
        state=live_state,
        exit_value=0.30,
        current_edge=None,
        return_fraction=-0.25,
        adaptive_exit=adaptive,
    )

    assert reason is None
    assert guard["status"] == "observing_recovery"
    assert guard["model_edge_available"] is False
    assert guard["grace_seconds"] == pytest.approx(45.0)
    trader.close()


def _stateless_stop_position(mlb):
    return {
        "id": "position-1",
        "event_id": mlb.id,
        "event_name": mlb.name,
        "market_slug": "home-ml",
        "market_type": "moneyline",
        "selection": "Home MLB",
        "position_side": "long",
        "mode": "dry_run",
        "quantity": 2.0,
        "entry_cost": 0.40,
        "stop_triggered_ts": None,
        "stop_observation_count": 0,
        "stop_low_exit_value": None,
    }


def test_missing_mlb_state_still_exits_immediately_by_default(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "volatility_stop_enabled": True,
        "stop_loss": 0.20,
    })
    mlb, _ = mlb_event_and_state(clock)
    adaptive = trader._adaptive_exit.base_decision(
        profile="observe",
        reason="test",
        applicable=True,
        profit_target=0.10,
        trailing_drawdown=0.04,
        exit_edge=0.0,
        stop_loss=0.20,
    )

    reason, guard, *_ = trader._stop_guard_decision(
        _stateless_stop_position(mlb),
        event=mlb,
        state=None,
        exit_value=0.30,
        current_edge=0.05,
        return_fraction=-0.25,
        adaptive_exit=adaptive,
    )

    assert reason == "hard_stop_loss"
    assert guard["status"] == "immediate"
    assert guard["reason"] == "no live MLB game state was available"
    trader.close()


def test_stateless_confirmation_bounds_the_stop_without_mlb_state(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "volatility_stop_enabled": True,
        "stateless_stop_confirmation": True,
        "stop_loss": 0.20,
        "stop_confirmation_readings": 3,
        "stop_grace_minutes": 2.0,
        "cycle_seconds": 30,
    })
    mlb, _ = mlb_event_and_state(clock)
    position = _stateless_stop_position(mlb)
    adaptive = trader._adaptive_exit.base_decision(
        profile="observe",
        reason="test",
        applicable=True,
        profit_target=0.10,
        trailing_drawdown=0.04,
        exit_edge=0.0,
        stop_loss=0.20,
    )

    reason, guard, triggered, count, low = trader._stop_guard_decision(
        position,
        event=mlb,
        state=None,
        exit_value=0.30,
        current_edge=0.05,
        return_fraction=-0.25,
        adaptive_exit=adaptive,
    )
    assert reason is None
    assert guard["status"] == "observing_recovery"
    assert guard["confirmation_basis"] == "price_only"
    assert guard["state_unavailable_reason"] == (
        "no live MLB game state was available"
    )
    # The price-only window never trusts the full grace without game state.
    assert guard["grace_seconds"] == pytest.approx(60.0)
    assert count == 1

    position.update(
        stop_triggered_ts=triggered,
        stop_observation_count=count,
        stop_low_exit_value=low,
    )
    clock.value += 30
    reason, guard, triggered, count, low = trader._stop_guard_decision(
        position,
        event=mlb,
        state=None,
        exit_value=0.29,
        current_edge=0.05,
        return_fraction=-0.275,
        adaptive_exit=adaptive,
    )
    assert reason is None
    assert count == 2

    position.update(
        stop_triggered_ts=triggered,
        stop_observation_count=count,
        stop_low_exit_value=low,
    )
    clock.value += 35
    reason, guard, _, count, low = trader._stop_guard_decision(
        position,
        event=mlb,
        state=None,
        exit_value=0.28,
        current_edge=0.05,
        return_fraction=-0.30,
        adaptive_exit=adaptive,
    )
    assert reason == "confirmed_stop_loss"
    assert guard["status"] == "confirmed"
    assert guard["confirmation_basis"] == "price_only"
    assert count == 3
    assert low == pytest.approx(0.28)
    trader.close()


def test_stateless_catastrophic_boundary_needs_a_second_reading(tmp_path):
    """With no game state there is no witness but the price itself.

    One gapped quote cannot sell the position; a boundary that survives the
    next reading can. A real collapse stays collapsed, a thin-book gap does
    not.
    """
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "volatility_stop_enabled": True,
        "stateless_stop_confirmation": True,
        "stop_loss": 0.20,
        "catastrophic_stop_multiplier": 1.50,
        "stop_confirmation_readings": 3,
    })
    mlb, _ = mlb_event_and_state(clock)
    adaptive = trader._adaptive_exit.base_decision(
        profile="observe",
        reason="test",
        applicable=True,
        profit_target=0.10,
        trailing_drawdown=0.04,
        exit_edge=0.0,
        stop_loss=0.20,
    )
    position = _stateless_stop_position(mlb)

    reason, guard, triggered, count, _low = trader._stop_guard_decision(
        position,
        event=mlb,
        state=None,
        exit_value=0.27,
        current_edge=0.05,
        return_fraction=-0.325,
        adaptive_exit=adaptive,
    )

    assert reason is None
    assert guard["catastrophic_price_unconfirmed"] is True
    assert count == 1

    # The boundary persists into the next reading: now it may sell.
    position.update(
        stop_triggered_ts=triggered, stop_observation_count=count
    )
    clock.value += 30
    reason, guard, *_ = trader._stop_guard_decision(
        position,
        event=mlb,
        state=None,
        exit_value=0.27,
        current_edge=0.05,
        return_fraction=-0.325,
        adaptive_exit=adaptive,
    )

    assert reason == "catastrophic_stop_loss"
    assert guard["status"] == "immediate"
    trader.close()


def test_stateless_confirmation_exits_on_material_model_reversal(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "volatility_stop_enabled": True,
        "stateless_stop_confirmation": True,
        "stop_loss": 0.20,
        "min_edge": 0.03,
    })
    mlb, _ = mlb_event_and_state(clock)
    adaptive = trader._adaptive_exit.base_decision(
        profile="observe",
        reason="test",
        applicable=True,
        profit_target=0.10,
        trailing_drawdown=0.04,
        exit_edge=0.0,
        stop_loss=0.20,
    )

    reason, guard, *_ = trader._stop_guard_decision(
        _stateless_stop_position(mlb),
        event=mlb,
        state=None,
        exit_value=0.30,
        current_edge=-0.10,
        return_fraction=-0.25,
        adaptive_exit=adaptive,
    )

    assert reason == "model_reversal_stop_loss"
    assert guard["status"] == "immediate"
    trader.close()


def test_stale_mlb_state_routes_through_the_price_only_window(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "volatility_stop_enabled": True,
        "stateless_stop_confirmation": True,
        "stop_loss": 0.20,
    })
    mlb, live_state = mlb_event_and_state(clock)
    live_state.received_at = datetime.fromtimestamp(
        clock() - 240, timezone.utc
    )
    adaptive = trader._adaptive_exit.base_decision(
        profile="observe",
        reason="test",
        applicable=True,
        profit_target=0.10,
        trailing_drawdown=0.04,
        exit_edge=0.0,
        stop_loss=0.20,
    )

    reason, guard, *_ = trader._stop_guard_decision(
        _stateless_stop_position(mlb),
        event=mlb,
        state=live_state,
        exit_value=0.30,
        current_edge=0.05,
        return_fraction=-0.25,
        adaptive_exit=adaptive,
    )

    assert reason is None
    assert guard["status"] == "observing_recovery"
    assert guard["confirmation_basis"] == "price_only"
    assert "stale" in guard["state_unavailable_reason"]
    trader.close()


def test_between_inning_state_keeps_the_stop_guard_state_aware(tmp_path):
    """The linescore feed reports "end" between half-innings; that state must
    feed the bounded MLB window, not the stateless bypass."""
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "volatility_stop_enabled": True,
        "stop_loss": 0.20,
        "stop_confirmation_readings": 3,
        "stop_grace_minutes": 2.0,
    })
    mlb, live_state = mlb_event_and_state(clock)
    live_state.sport_state = {
        "schema": "mlb-linescore-v2",
        "inning": 5,
        "half": "end",
    }
    adaptive = trader._adaptive_exit.base_decision(
        profile="observe",
        reason="test",
        applicable=True,
        profit_target=0.10,
        trailing_drawdown=0.04,
        exit_edge=0.0,
        stop_loss=0.20,
    )

    reason, guard, *_ = trader._stop_guard_decision(
        _stateless_stop_position(mlb),
        event=mlb,
        state=live_state,
        exit_value=0.30,
        current_edge=0.05,
        return_fraction=-0.25,
        adaptive_exit=adaptive,
    )

    assert reason is None
    assert guard["status"] == "observing_recovery"
    assert guard["confirmation_basis"] == "mlb_state"
    # End of the 5th: ten of eighteen regulation halves complete.
    assert guard["game_state"]["fraction_remaining"] == pytest.approx(8 / 18)
    trader.close()


def test_fraction_remaining_accepts_between_inning_halves():
    event = Event(
        name="Away MLB at Home MLB",
        sport="baseball",
        league="MLB",
        home="Home MLB",
        away="Away MLB",
    )
    observed = datetime.now(timezone.utc)
    state = GameState(
        event_id=event.id,
        home_score=1,
        away_score=1,
        period="",
        clock="",
        source="test-mlb",
        provider_timestamp=observed,
        received_at=observed,
        processed_at=observed,
        status="in_progress",
        live=True,
        ended=False,
        sport_state={"schema": "mlb-linescore-v2", "inning": 5, "half": "end"},
    )
    from app.polymarket_us_trading import _baseball_fraction_remaining

    assert _baseball_fraction_remaining(event, state) == pytest.approx(8 / 18)
    state.sport_state = {"schema": "mlb-linescore-v2", "inning": 5, "half": "middle"}
    assert _baseball_fraction_remaining(event, state) == pytest.approx(8 / 18)


def _shadow_game_state(event_id, clock, **overrides):
    observed = datetime.fromtimestamp(clock(), timezone.utc)
    sport_state = {
        "schema": "mlb-linescore-v2",
        "inning": 5,
        "half": "top",
        "outs": 0,
        "balls": 1,
        "strikes": 2,
        "base_mask": 7,
        "batting_side": "away",
        "batter": {"id": 660271, "name": "Shohei Ohtani"},
        "pitcher": {"id": 592789, "name": "Bullpen Arm"},
    }
    sport_state.update(overrides)
    return GameState(
        event_id=event_id,
        home_score=2,
        away_score=1,
        period="Top 5",
        clock="",
        source="test-mlb",
        provider_timestamp=observed,
        received_at=observed,
        processed_at=observed,
        status="in_progress",
        live=True,
        ended=False,
        sport_state=sport_state,
    )


def test_mlb_shadow_state_reads_run_expectancy_and_identities(tmp_path):
    from app.polymarket_us_trading import _mlb_shadow_state

    clock = Clock()
    shadow = _mlb_shadow_state(_shadow_game_state("e1", clock))
    assert shadow["run_expectancy"] == pytest.approx(2.17)  # loaded, 0 out
    assert shadow["batter_name"] == "Shohei Ohtani"
    assert shadow["pitcher_id"] == 592789
    assert shadow["balls"] == 1 and shadow["strikes"] == 2

    empty_two_out = _mlb_shadow_state(
        _shadow_game_state("e1", clock, base_mask=0, outs=2)
    )
    assert empty_two_out["run_expectancy"] == pytest.approx(0.10)

    missing_outs = _mlb_shadow_state(
        _shadow_game_state("e1", clock, outs=None)
    )
    assert missing_outs["run_expectancy"] is None

    state = _shadow_game_state("e1", clock)
    state.sport_state = None
    assert _mlb_shadow_state(state) is None


def test_pitcher_change_shadow_flags_bullpen_entries_once(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    mlb, _ = mlb_event_and_state(clock)

    first = trader._pitcher_change_shadow(
        mlb, {"pitcher_id": 100, "pitcher_name": "Starter", "inning": 1, "half": "top"}
    )
    assert first["pitcher_change_recent"] is False
    assert first["seconds_since_pitcher_change"] is None

    clock.value += 60
    same = trader._pitcher_change_shadow(
        mlb, {"pitcher_id": 100, "pitcher_name": "Starter", "inning": 3, "half": "top"}
    )
    assert same["pitcher_change_recent"] is False

    clock.value += 60
    changed = trader._pitcher_change_shadow(
        mlb, {"pitcher_id": 200, "pitcher_name": "Reliever", "inning": 6, "half": "top"}
    )
    assert changed["pitcher_change_recent"] is True
    assert changed["previous_pitcher_id"] == 100
    assert changed["seconds_since_pitcher_change"] == pytest.approx(0.0)
    journal = [
        item for item in trader.journal()
        if item["kind"] == "shadow" and item["status"] == "pitcher_change"
    ]
    assert len(journal) == 1

    clock.value += 300
    later = trader._pitcher_change_shadow(
        mlb, {"pitcher_id": 200, "pitcher_name": "Reliever", "inning": 7, "half": "top"}
    )
    assert later["pitcher_change_recent"] is False
    trader.close()


def test_marks_carry_the_mlb_shadow_context(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })
    fixture = event()
    trader.run_cycle([(fixture, [signal(clock)])], us_payload())
    assert trader.positions(open_only=True)

    clock.value += 60
    trader.run_cycle(
        [(fixture, [signal(clock)])],
        us_payload(),
        game_states={fixture.id: _shadow_game_state(fixture.id, clock)},
    )
    marks = [
        item["details"]
        for item in trader.journal()
        if item["kind"] == "mark"
    ]
    assert marks, "expected at least one mark"
    shadowed = [m for m in marks if m.get("mlb_shadow")]
    assert shadowed, "mark should carry mlb_shadow when structured state exists"
    assert shadowed[0]["mlb_shadow"]["run_expectancy"] == pytest.approx(2.17)
    assert shadowed[0]["mlb_shadow"]["pitcher_name"] == "Bullpen Arm"
    trader.close()


def test_qualification_rejections_deduplicate_across_moving_numbers(tmp_path):
    """A rejection differing only in its live numbers must not journal again
    inside the cooldown — the chatter was measured at 80% of the retention
    budget, evicting the mark evidence replays depend on."""
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({"candidate_cooldown_seconds": 300})
    fixture = event()
    sig = signal(clock)

    trader._qualification_rejection(
        fixture, sig, "existing signal edge +4.3c is below the 5.0c floor"
    )
    clock.value += 30
    trader._qualification_rejection(
        fixture, sig, "existing signal edge +4.1c is below the 5.0c floor"
    )
    rows = [item for item in trader.journal() if item["kind"] == "qualification"]
    assert len(rows) == 1
    # A genuinely different reason still journals.
    trader._qualification_rejection(
        fixture, sig, "US buy price is outside the configured entry bracket"
    )
    rows = [item for item in trader.journal() if item["kind"] == "qualification"]
    assert len(rows) == 2
    trader.close()


def test_journal_prune_evicts_qualification_chatter_before_marks(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    # Oldest rows are marks; newer rows are qualification chatter. The old
    # oldest-first prune would delete the marks; the fix must not.
    for index in range(6):
        clock.value += 1
        trader._journal("mark", "updated", payload={"index": index})
    for index in range(8):
        clock.value += 1
        trader._journal("qualification", "rejected", payload={"index": index})

    trader._prune_journal(maximum=8)

    kinds = [item["kind"] for item in trader.journal(limit=50)]
    assert kinds.count("mark") == 6
    assert kinds.count("qualification") == 2
    trader.close()


def test_reversal_confirmation_defaults_to_single_reading_behavior(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({"min_edge": 0.02})
    position = {"reversal_triggered_ts": None, "reversal_observation_count": 0}

    confirmed, triggered, count = trader._reversal_confirmation(
        position, trader.policy, -0.05
    )
    assert confirmed is True
    assert count == 1
    # A recovered edge always resets the streak.
    assert trader._reversal_confirmation(position, trader.policy, 0.01) == (
        False, None, 0,
    )
    assert trader._reversal_confirmation(position, trader.policy, None) == (
        False, None, 0,
    )
    trader.close()


def test_reversal_confirmation_requires_consecutive_readings(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "reversal_confirmation_readings": 3,
        "min_edge": 0.02,
        "cycle_seconds": 30,
    })
    position = {"reversal_triggered_ts": None, "reversal_observation_count": 0}

    confirmed, triggered, count = trader._reversal_confirmation(
        position, trader.policy, -0.05
    )
    assert (confirmed, count) == (False, 1)
    position.update(
        reversal_triggered_ts=triggered, reversal_observation_count=count
    )
    clock.value += 30
    confirmed, triggered, count = trader._reversal_confirmation(
        position, trader.policy, -0.06
    )
    assert (confirmed, count) == (False, 2)
    position.update(
        reversal_triggered_ts=triggered, reversal_observation_count=count
    )
    clock.value += 30
    confirmed, triggered, count = trader._reversal_confirmation(
        position, trader.policy, -0.04
    )
    assert (confirmed, count) == (True, 3)

    # A streak that went quiet past the window starts over.
    position.update(
        reversal_triggered_ts=triggered - 1000,
        reversal_observation_count=2,
    )
    confirmed, _, count = trader._reversal_confirmation(
        position, trader.policy, -0.05
    )
    assert (confirmed, count) == (False, 1)
    trader.close()


def test_unconfirmed_reversal_cannot_override_the_profit_floor(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "reversal_confirmation_readings": 3,
        "minimum_locked_profit": 0.02,
    })
    position = {"id": "p1", "entry_cost": 0.40}
    common = {
        "exit_value": 0.35,  # below the protected floor
        "peak": 0.55,
        "current_edge": -0.10,
        "profit_lock_armed": True,
        "adaptive_exit": None,
        "policy": trader.policy,
    }
    unconfirmed = trader._profit_guard_decision(
        position, **common, reversal_confirmed=False
    )
    assert unconfirmed["blocked"] is True
    assert unconfirmed["material_reversal_override"] is False
    assert unconfirmed["status"] == "profit_floor_missed_holding"

    confirmed = trader._profit_guard_decision(
        position, **common, reversal_confirmed=True
    )
    assert confirmed["material_reversal_override"] is True
    assert confirmed["blocked"] is False
    trader.close()


def test_exit_reason_sells_reversal_only_when_confirmed(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({"reversal_confirmation_readings": 3})
    position = {"id": "p1", "entry_cost": 0.40}

    held = trader._exit_reason(
        position, 0.35, 0.41, -0.10, reversal_confirmed=False
    )
    assert held is None
    sold = trader._exit_reason(
        position, 0.35, 0.41, -0.10, reversal_confirmed=True
    )
    assert sold == "model_reversal"
    trader.close()


def test_reversal_confirmation_readings_validate_and_round_trip(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    with pytest.raises(TradingPolicyError, match="reversal_confirmation"):
        trader.configure({"reversal_confirmation_readings": 0})
    with pytest.raises(TradingPolicyError, match="reversal_confirmation"):
        trader.configure({"reversal_confirmation_readings": 11})
    trader.configure({"reversal_confirmation_readings": 3})
    assert trader.policy.reversal_confirmation_readings == 3
    trader.close()

    reopened = make_trader(tmp_path, clock, fail_on_orders=True)
    assert reopened.policy.reversal_confirmation_readings == 3
    reopened.close()


def test_stateless_stop_confirmation_round_trips_through_configure(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({"stateless_stop_confirmation": True})
    assert trader.policy.stateless_stop_confirmation is True
    trader.close()

    reopened = make_trader(tmp_path, clock, fail_on_orders=True)
    assert reopened.policy.stateless_stop_confirmation is True
    reopened.close()


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
    assert positions[0]["policy_session_id"]
    assert positions[0]["entry_policy"]["execution_mode"] == "dry_run"
    assert positions[0]["entry_signal_edge"] == pytest.approx(0.05)
    assert positions[0]["entry_signal_quality"] == pytest.approx(85.0)
    assert positions[0]["entry_reference_sources"] == 2
    assert any(item["status"] == "simulated_fill" for item in trader.journal())


@pytest.mark.parametrize(
    ("signal_market", "signal_outcome", "venue_type", "question", "line"),
    [
        (
            "first_five_moneyline",
            "Away",
            "baseball_team_first_five_winner",
            "Will Away win the first five innings?",
            None,
        ),
        (
            "first_five_spread",
            "Away +1.5",
            "baseball_team_first_five_spread",
            "Will Away cover +1.5 in the first five innings?",
            1.5,
        ),
        (
            "first_five_total",
            "Over 4.5",
            "baseball_team_first_five_total",
            "Will there be more than 4.5 runs in the first five innings?",
            4.5,
        ),
        (
            "first_inning_total",
            "Over 0.5",
            "baseball_team_first_inning_run",
            "Will there be any run in the first inning?",
            0.5,
        ),
    ],
)
def test_dry_run_maps_and_retains_exact_mlb_segment_trades(
    tmp_path,
    signal_market,
    signal_outcome,
    venue_type,
    question,
    line,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })

    result = trader.run_cycle(
        [(
            mlb_event(),
            [signal(
                clock,
                market=signal_market,
                outcome=signal_outcome,
            )],
        )],
        mlb_segment_payload(
            market_type=venue_type,
            question=question,
            line=line,
        ),
    )

    assert result["entries"] == 1
    position = trader.positions(open_only=True)[0]
    expected_scope = (
        "first_inning"
        if signal_market == "first_inning_total"
        else "first_five_innings"
    )
    assert position["market_scope"] == expected_scope
    assert position["entry_policy"]["allowed_market_scopes"] == list(
        SUPPORTED_MARKET_SCOPES
    )
    trader._close_position(
        position["id"],
        exit_value=0.45,
        reason="segment_test",
        order_id=None,
    )
    summary = trader.segment_research_summary()
    assert summary["trades"] == 1
    assert summary["closed"] == 1
    assert summary["events"] == 1
    assert summary["rows"][0]["market_scope"] == expected_scope
    assert summary["rows"][0]["realized_net_usd"] > 0
    trader.close()


def test_live_mlb_segments_require_a_separate_explicit_approval(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "execution_mode": "live",
        "allowed_market_scopes": ["full_game", "first_five_innings"],
        "allow_live_segment_markets": False,
    })
    baseball = mlb_event()
    candidate_signal = signal(
        clock,
        market="first_five_moneyline",
        outcome="Away",
    )
    us_event = mlb_segment_payload()["events"][0]

    candidate, reason = trader._map_signal(
        baseball,
        candidate_signal,
        us_event,
        1.0,
    )

    assert candidate is None
    assert "live orders are locked" in reason

    trader.configure({"allow_live_segment_markets": True})
    candidate, reason = trader._map_signal(
        baseball,
        candidate_signal,
        us_event,
        1.0,
    )

    assert reason == ""
    assert candidate is not None
    assert candidate.market["market_scope"] == "first_five_innings"
    trader.close()


def test_dry_first_inning_trade_settles_from_official_segment_end_score(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })
    baseball = mlb_event()
    candidate_signal = signal(
        clock,
        market="first_inning_total",
        outcome="Over 0.5",
    )
    payload = mlb_segment_payload(
        market_type="baseball_team_first_inning_run",
        question="Will there be any run in the first inning?",
        line=0.5,
    )
    entered = trader.run_cycle(
        [(baseball, [candidate_signal])],
        payload,
    )

    settled = trader.run_cycle(
        [(baseball, [candidate_signal])],
        {"events": []},
        segment_results={
            baseball.id: {"first_inning": (0.0, 1.0)},
        },
    )

    assert entered["entries"] == 1
    assert settled["exits"] == 1
    assert trader.positions(open_only=True) == []
    closed = trader.positions()[0]
    assert closed["exit_reason"] == "dry_run_segment_resolved"
    assert closed["current_exit_value"] == pytest.approx(1.0)
    assert closed["realized_pnl"] > 0
    audit = next(
        item for item in trader.journal()
        if item["kind"] == "settlement"
    )
    assert audit["status"] == "simulated_segment_resolution"
    assert audit["details"]["source"] == "official_mlb_segment_end_state"
    trader.close()


def test_legacy_live_policy_defaults_to_full_game_scope_only():
    live = TradingPolicy.from_mapping({"execution_mode": "live"})
    dry = TradingPolicy.from_mapping({"execution_mode": "dry_run"})

    assert live.allowed_market_scopes == ("full_game",)
    assert dry.allowed_market_scopes == SUPPORTED_MARKET_SCOPES


def test_line_type_policy_blocks_disabled_lines_without_changing_signal(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    source_signal = signal(clock)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "allowed_market_types": ["spread", "total"],
    })

    result = trader.run_cycle([(event(), [source_signal])], us_payload())

    assert result["entries"] == 0
    assert result["qualified"] == 0
    assert trader.positions() == []
    rejection = next(
        item
        for item in trader.journal()
        if item["kind"] == "qualification"
    )
    assert "moneyline lines are disabled" in rejection["details"]["reason"]
    assert rejection["details"]["allowed_market_types"] == ["spread", "total"]
    assert source_signal.edge == pytest.approx(0.05)
    assert source_signal.confidence == pytest.approx(85.0)


def test_execution_edge_ceiling_filters_implausibly_large_edges(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "max_edge": 0.10,
    })

    result = trader.run_cycle([(event(), [signal(clock)])], us_payload())

    assert result["entries"] == 0
    rejection = next(
        item for item in trader.journal()
        if item["kind"] == "qualification"
    )
    assert "execution edge +20.0c exceeds" in rejection["details"]["reason"]
    assert rejection["details"]["configured_max_edge"] == pytest.approx(0.10)


def test_line_profile_authorizes_entry_without_global_fallback(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "global_entry_enabled": False,
        "allowed_market_types": ["spread"],
        "line_execution_profiles": [{
            "market_type": "moneyline",
            "game_stage": "all",
            "enabled": True,
            "overrides": {
                "min_edge": 0.04,
                "max_signal_quality": 90.0,
            },
        }],
    })

    result = trader.run_cycle([(event(), [signal(clock)])], us_payload())

    assert result["entries"] == 1
    position = trader.positions(open_only=True)[0]
    assert position["entry_policy"]["global_entry_enabled"] is False
    assert position["entry_policy"]["execution_profile_key"] == "moneyline/all"


def test_global_fallback_off_blocks_unprofiled_line(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "global_entry_enabled": False,
        "allowed_market_types": [],
        "line_execution_profiles": [],
    })

    result = trader.run_cycle([(event(), [signal(clock)])], us_payload())

    assert result["entries"] == 0
    rejection = next(
        item for item in trader.journal()
        if item["kind"] == "qualification"
    )
    assert "global entry fallback is not authorized" in rejection["details"]["reason"]


def test_specific_profile_can_authorize_around_disabled_all_stage_profile():
    policy = TradingPolicy.from_mapping({
        "global_entry_enabled": False,
        "allowed_market_types": [],
        "line_execution_profiles": [
            {
                "market_type": "moneyline",
                "game_stage": "all",
                "enabled": False,
                "overrides": {"min_edge": 0.12},
            },
            {
                "market_type": "moneyline",
                "game_stage": "early",
                "enabled": True,
                "overrides": {"min_edge": 0.04},
            },
        ],
    })

    early, key = policy.execution_policy_for("moneyline", 0.70)
    late, late_key = policy.execution_policy_for("moneyline", 0.10)

    assert early is not None
    assert early.min_edge == pytest.approx(0.04)
    assert key == "moneyline/all+moneyline/early"
    assert late is None
    assert late_key == "moneyline/all"


def test_quality_band_and_source_agreement_are_execution_filters(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "max_signal_quality": 80.0,
    })

    too_high = trader.run_cycle(
        [(event(), [signal(clock, quality=85.0)])],
        us_payload(),
    )
    assert too_high["entries"] == 0
    assert any(
        "quality 85/100 exceeds" in item["details"].get("reason", "")
        for item in trader.journal()
        if item["kind"] == "qualification"
    )

    trader.configure({
        "max_signal_quality": 100.0,
        "min_source_agreement": 70.0,
    })
    disagreement = trader.run_cycle(
        [(event(), [signal(clock, agreement=60.0)])],
        us_payload(),
    )
    assert disagreement["entries"] == 0
    assert any(
        "source agreement 60/100 is below" in item["details"].get("reason", "")
        for item in trader.journal()
        if item["kind"] == "qualification"
    )


def test_entry_confirmation_requires_new_signal_and_restarts_on_price_drift(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "entry_confirmation_readings": 2,
        "max_confirmation_price_drift": 0.01,
    })

    first = trader.run_cycle([(event(), [signal(clock)])], us_payload(ask=0.40))
    same = trader.run_cycle([(event(), [signal(clock)])], us_payload(ask=0.40))
    clock.value += 5
    drifted = trader.run_cycle(
        [(event(), [signal(clock)])],
        us_payload(ask=0.43),
    )
    drift_reason = trader.status()["last_cycle_evaluations"][0]["reason"]
    clock.value += 5
    stable = trader.run_cycle(
        [(event(), [signal(clock)])],
        us_payload(ask=0.43),
    )

    assert first["entries"] == 0
    assert same["entries"] == 0
    assert drifted["entries"] == 0
    assert "confirmation restarted" in drift_reason
    assert stable["entries"] == 1


def multi_market_us_payload(*, bid=0.39, ask=0.40):
    """One US event exposing enough contracts to exhaust the venue budget."""
    base = us_payload(bid=bid, ask=ask)
    base["events"][0]["markets"].append({
        "id": "market-total",
        "slug": "away-at-home-total-8pt5",
        "question": "Total runs",
        "market_type": "total",
        "line": 8.5,
        "active": True,
        "closed": False,
        "hidden": False,
        "state": "OPEN",
        "long_best_bid": bid,
        "long_best_ask": ask,
        "minimum_trade_quantity": 1,
        "minimum_tick_size": 0.01,
        "sides": [
            {"id": "over", "description": "Over 8.5", "long": True, "tradable": True},
            {"id": "under", "description": "Under 8.5", "long": False, "tradable": True},
        ],
    })
    return base


def test_confirmed_entry_window_survives_the_venue_check_budget(tmp_path):
    """A candidate deferred before its order attempt keeps its readings.

    Only three candidates may consume a venue check per cycle. Discarding an
    earned confirmation window at the moment it was satisfied — rather than
    when an order was actually attempted — permanently starved every
    lower-ranked candidate, because it had to re-earn every reading each cycle
    while never reaching the front of the queue.
    """
    clock = Clock()
    trader = PolymarketUSAutoTrader(
        str(tmp_path / "trading.db"),
        # Without credentials every attempt fails after the venue budget is
        # consumed, so no candidate can end the loop by entering.
        key_id="",
        secret_key="",
        client_factory=client_factory(FakeOrders()),
        clock=clock,
    )
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "entry_confirmation_readings": 2,
    })
    # Short sides cost 1 - bid, so they need a higher probability to clear the
    # same positive execution-edge floor as the long sides.
    wanted = (
        ("moneyline", "Away", 0.60),
        ("moneyline", "Home", 0.75),
        ("total", "Over 8.5", 0.60),
        ("total", "Under 8.5", 0.75),
    )

    def readings():
        return [
            signal(clock, market=market, outcome=outcome, probability=probability)
            for market, outcome, probability in wanted
        ]

    first = trader.run_cycle([(event(), readings())], multi_market_us_payload())
    assert first["qualified"] == 4
    assert len(trader._candidate_confirmations) == 4

    clock.value += 5
    second = trader.run_cycle([(event(), readings())], multi_market_us_payload())

    assert second["entries"] == 0
    # Three candidates spent the venue budget and consumed their windows.
    assert len(trader._candidate_seen) == 3
    # The fourth was deferred before any order attempt and keeps its readings.
    assert len(trader._candidate_confirmations) == 1
    retained = next(iter(trader._candidate_confirmations.values()))
    assert retained["count"] == 2
    assert any(
        item["state"] == "queued"
        and "venue-check budget" in item["reason"]
        for item in trader.status()["last_cycle_evaluations"]
    )


def test_candidate_log_retains_rejected_contracts_and_degenerate_propensity(
    tmp_path,
):
    """Off-policy work needs the population the policy chose from.

    The advisor previously derived "opportunities" from entry journal rows, so
    the candidate population was exactly the set of fills. A looser filter could
    only ever re-select trades that were already taken, which made its estimated
    qualified-per-hour rate structurally incapable of exceeding the rate of the
    policy that generated the data.
    """
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "candidate_cooldown_seconds": 60,
    })
    wanted = (
        ("moneyline", "Away", 0.60),
        ("moneyline", "Home", 0.75),
        ("total", "Over 8.5", 0.60),
        ("total", "Under 8.5", 0.75),
    )
    result = trader.run_cycle(
        [(event(), [
            signal(clock, market=market, outcome=outcome, probability=probability)
            for market, outcome, probability in wanted
        ])],
        multi_market_us_payload(),
    )

    assert result["entries"] == 1
    assert result["logged_candidates"] == 4

    logged = trader._candidate_observation_opportunities()
    assert len(logged) == 4
    entered = [row for row in logged if row["entered"]]
    declined = [row for row in logged if not row["entered"]]
    assert len(entered) == 1
    assert len(declined) == 3

    # Every logged candidate carries the executable context a filter needs.
    for row in logged:
        assert row["entry_cost"] is not None
        assert row["execution_edge"] is not None
        assert row["spread"] is not None
        assert row["market_type"] in {"moneyline", "total"}
        assert row["propensity_source"] == "deterministic_policy"
        assert row["competing_candidates"] == 4
    # Declined contracts record why, so a rejection can be audited later.
    assert all(row["rejection_reason"] for row in declined)
    # The saved policy is deterministic, so propensities are 0/1 - not a
    # randomized design that would identify an off-policy return estimate.
    assert sorted(row["selection_propensity"] for row in logged) == [
        0.0, 0.0, 0.0, 1.0
    ]

    contract = trader.advisor_dataset(source_lane="dry_run")["logging_contract"]
    assert contract["unentered_candidate_observations"] == 3
    assert contract["off_policy_identified"] is False
    assert contract["randomized_exploration"] is False

    # The same contract must not be re-logged inside its candidate cooldown.
    clock.value += 5
    repeat = trader.run_cycle(
        [(event(), [
            signal(clock, market=market, outcome=outcome, probability=probability)
            for market, outcome, probability in wanted
        ])],
        multi_market_us_payload(),
    )
    assert repeat["logged_candidates"] == 0
    assert len(trader._candidate_observation_opportunities()) == 4


def test_advisor_opportunities_prefer_the_candidate_log_over_entry_rows(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "candidate_cooldown_seconds": 60,
    })
    trader.run_cycle(
        [(event(), [
            signal(clock, market=market, outcome=outcome, probability=probability)
            for market, outcome, probability in (
                ("moneyline", "Away", 0.60),
                ("total", "Over 8.5", 0.60),
            )
        ])],
        multi_market_us_payload(),
    )

    opportunities = trader._advisor_opportunities()
    sources = {row["evidence_source"] for row in opportunities}

    # The entered contract exists in both the candidate log and the entry
    # journal. It must be counted once, from the richer source.
    assert sources == {"candidate_log"}
    assert len(opportunities) == 2
    assert sum(row["entered"] for row in opportunities) == 1


def test_authorization_matrix_resolves_every_line_and_stage_combination(
    tmp_path,
):
    """Authorization is spread over three controls; the server resolves it."""
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "execution_mode": "dry_run",
        "global_entry_enabled": False,
        "allowed_market_types": [],
        "line_execution_profiles": [
            {
                "market_type": "moneyline",
                "game_stage": "all",
                "enabled": False,
                "overrides": {"min_edge": 0.12},
            },
            {
                "market_type": "moneyline",
                "game_stage": "early",
                "enabled": True,
                "overrides": {"min_edge": 0.04},
            },
        ],
    })

    matrix = trader.authorization_matrix()
    by_key = {
        (row["market_type"], row["game_stage"]): row
        for row in matrix["combinations"]
    }

    assert matrix["global_fallback_authorized"] is False
    assert len(matrix["combinations"]) == 12
    # Only the explicitly authorized early moneyline profile may trade.
    assert matrix["authorized_combinations"] == 1
    assert matrix["any_authorized"] is True

    early = by_key[("moneyline", "early")]
    assert early["authorized"] is True
    assert early["source"] == "line_profile"
    assert early["effective"]["min_edge"] == pytest.approx(0.04)

    # A disabled all-stage profile blocks its own stage and states why.
    late = by_key[("moneyline", "late")]
    assert late["authorized"] is False
    assert late["effective"] is None
    assert "moneyline/all" in late["blocked_reason"]

    # An unprofiled line falls through to the disabled global fallback.
    total = by_key[("total", "all")]
    assert total["authorized"] is False
    assert "global fallback is off" in total["blocked_reason"]


def test_execution_state_names_each_blocker_separately(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "execution_mode": "dry_run",
        "automation_enabled": False,
        "global_entry_enabled": False,
        "allowed_market_types": [],
        "line_execution_profiles": [],
    })

    state = trader.execution_state()
    codes = {item["code"] for item in state["entry_blockers"]}

    assert state["entry_possible_now"] is False
    assert codes == {"automation_off", "nothing_authorized"}
    assert state["automation"]["running"] is False
    assert state["live_orders"]["armed"] is False
    assert state["live_orders"]["applies_to_lane"] is False
    assert state["protective_exits"]["armed"] is False
    assert state["authorization"]["any_authorized"] is False
    assert state["exposure"]["open_positions"] == 0
    assert state["policy"]["saved_fingerprint"]

    trader.configure({
        "automation_enabled": True,
        "global_entry_enabled": True,
        "allowed_market_types": ["moneyline"],
    })
    cleared = trader.execution_state()

    assert cleared["entry_blockers"] == []
    assert cleared["entry_possible_now"] is True
    # The saved policy changed, so the fingerprint the UI compares against must
    # change with it.
    assert (
        cleared["policy"]["saved_fingerprint"]
        != state["policy"]["saved_fingerprint"]
    )


def test_predictive_exit_state_admits_cold_start_tightening(tmp_path):
    """Cold start is not "not acting yet" - it already moves exit thresholds."""
    clock = Clock()
    trader = make_trader(tmp_path, clock)

    trader.configure({"adaptive_exit_enabled": False})
    assert trader.execution_state()["predictive_exit"]["status"] == "off"

    trader.configure({
        "adaptive_exit_enabled": True,
        "adaptive_exit_profile": "observe",
    })
    observing = trader.execution_state()["predictive_exit"]
    assert observing["status"] == "observe_only"
    assert observing["can_tighten_exits"] is False

    trader.configure({"adaptive_exit_profile": "responsive"})
    responsive = trader.execution_state()["predictive_exit"]
    assert responsive["status"] == "collecting"
    # No labeled evidence exists, yet the responsive profile still applies half
    # of its bounded tightening. Reporting this as inactive would mislead.
    assert responsive["can_tighten_exits"] is True
    assert responsive["cold_start_confidence"] == pytest.approx(0.50)
    assert "before any labeled evidence" in responsive["cold_start_warning"]
    assert responsive["hard_stop_unchanged"] is True


def test_profile_capital_limits_can_only_tighten_the_lane_boundaries():
    """A profile may narrow the lane's capital limits, never widen them."""
    policy = TradingPolicy.from_mapping({
        "trading_allocation_usd": 10.0,
        "max_total_exposure_usd": 8.0,
        "max_position_usd": 2.0,
        "max_event_exposure_usd": 4.0,
        "max_entries_per_event_per_hour": 3,
        "line_execution_profiles": [
            {
                "market_type": "moneyline",
                "game_stage": "all",
                "enabled": True,
                "overrides": {
                    "max_position_usd": 1.0,
                    "max_profile_exposure_usd": 3.0,
                    "max_profile_open_positions": 2,
                },
            },
            {
                "market_type": "total",
                "game_stage": "all",
                "enabled": True,
                "overrides": {
                    # Deliberately larger than the lane allows.
                    "max_position_usd": 25.0,
                    "max_event_exposure_usd": 50.0,
                    "max_entries_per_event_per_hour": 99,
                    "max_profile_exposure_usd": 100.0,
                },
            },
        ],
    })

    tighter, _ = policy.execution_policy_for("moneyline", None)
    looser, _ = policy.execution_policy_for("total", None)

    assert tighter.max_position_usd == pytest.approx(1.0)
    assert tighter.max_profile_exposure_usd == pytest.approx(3.0)
    assert tighter.max_profile_open_positions == 2
    # Every attempt to widen a shared boundary is clamped back to the lane.
    assert looser.max_position_usd == pytest.approx(2.0)
    assert looser.max_event_exposure_usd == pytest.approx(4.0)
    assert looser.max_entries_per_event_per_hour == 3
    assert looser.max_profile_exposure_usd == pytest.approx(8.0)


def test_profile_exposure_limit_blocks_further_entries_for_that_line_only(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "candidate_cooldown_seconds": 60,
        "max_entries_per_event_per_hour": 9,
        "line_execution_profiles": [{
            "market_type": "moneyline",
            "game_stage": "all",
            "enabled": True,
            "overrides": {"max_profile_open_positions": 1},
        }],
    })

    first = trader.run_cycle(
        [(event(), [signal(clock, market="moneyline", outcome="Away")])],
        multi_market_us_payload(),
    )
    assert first["entries"] == 1

    clock.value += 120
    blocked = trader.run_cycle(
        [(event(), [
            signal(clock, market="moneyline", outcome="Home", probability=0.75),
        ])],
        multi_market_us_payload(),
    )
    assert blocked["entries"] == 0
    assert any(
        "moneyline/all already holds 1 of its 1 allowed open positions"
        in str(item.get("reason") or "")
        for item in trader.status()["last_cycle_evaluations"]
    )

    # The unprofiled total line keeps its own budget and is unaffected.
    clock.value += 120
    other_line = trader.run_cycle(
        [(event(), [signal(clock, market="total", outcome="Over 8.5")])],
        multi_market_us_payload(),
    )
    assert other_line["entries"] == 1


def test_profile_entry_budget_uses_the_key_frozen_at_entry(tmp_path):
    """A later policy edit must not move past fills into another budget."""
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "candidate_cooldown_seconds": 60,
        "line_execution_profiles": [{
            "market_type": "moneyline",
            "game_stage": "all",
            "enabled": True,
            "overrides": {"min_edge": 0.03},
        }],
    })
    trader.run_cycle(
        [(event(), [signal(clock, market="moneyline", outcome="Away")])],
        multi_market_us_payload(),
    )
    position = trader.positions(open_only=True)[0]

    assert trader._position_profile_key(position) == "moneyline/all"
    assert trader._profile_entries_last_hour("moneyline/all", "dry_run") == 1
    assert trader._profile_entries_last_hour("global", "dry_run") == 0

    # Removing the profile does not retroactively re-attribute the fill.
    trader.configure({"line_execution_profiles": []})
    reloaded = trader.positions(open_only=True)[0]
    assert trader._position_profile_key(reloaded) == "moneyline/all"
    assert trader._profile_entries_last_hour("global", "dry_run") == 0


def test_reading_status_never_starts_a_policy_session(tmp_path):
    """Reporting must have no side effects on the session ledger."""
    clock = Clock()
    trader = make_trader(tmp_path, clock)

    # Nothing has been saved or traded yet.
    for _ in range(3):
        assert trader.status()["execution_state"]["policy"]["session_id"] is None
    assert trader.policy_sessions() == []

    # Saving a policy legitimately opens a session.
    trader.configure({"automation_enabled": True, "execution_mode": "dry_run"})
    opened = trader.policy_sessions()
    assert len(opened) == 1

    # Polling status must report that session without ever creating another.
    for _ in range(5):
        assert trader.status()["execution_state"]["policy"]["session_id"] == (
            opened[0]["id"]
        )
    assert len(trader.policy_sessions()) == 1

    # Observing candidates is not trading either.
    trader.run_cycle(
        [(event(), [signal(clock, market="moneyline", outcome="Away")])],
        us_payload(),
    )
    assert len(trader.policy_sessions()) == 1


def test_status_reports_the_evaluation_count_alongside_the_list(tmp_path):
    """The polled payload states how many evaluations it is carrying."""
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({"automation_enabled": True, "execution_mode": "dry_run"})
    trader.run_cycle(
        [(event(), [
            signal(clock, market=market, outcome=outcome, probability=probability)
            for market, outcome, probability in (
                ("moneyline", "Away", 0.60),
                ("total", "Over 8.5", 0.60),
            )
        ])],
        multi_market_us_payload(),
    )

    status = trader.status()

    assert status["last_cycle_evaluation_count"] == 2
    assert len(status["last_cycle_evaluations"]) == 2
    # Per-candidate diagnostics and the cycle's own policy context both remain:
    # an evaluation is the audit record of the cycle that produced it, and the
    # saved policy may have changed since.
    evaluation = status["last_cycle_evaluations"][0]
    assert evaluation["state"]
    assert "configured_min_edge" in evaluation
    assert "required_engine_gates" in evaluation
    assert "selected_engine_gate_results" in evaluation


def test_authorization_matrix_sends_effective_values_only_where_they_differ(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "execution_mode": "dry_run",
        "line_execution_profiles": [{
            "market_type": "moneyline",
            "game_stage": "early",
            "enabled": True,
            "overrides": {"min_edge": 0.09},
        }],
    })

    rows = {
        (row["market_type"], row["game_stage"]): row
        for row in trader.authorization_matrix()["combinations"]
    }

    inheriting = rows[("total", "all")]
    assert inheriting["authorized"] is True
    assert inheriting["inherits_global"] is True
    # Twelve identical copies of the global thresholds are not transmitted.
    assert inheriting["effective"] is None
    assert inheriting["overridden_fields"] == []

    overridden = rows[("moneyline", "early")]
    assert overridden["inherits_global"] is False
    assert overridden["overridden_fields"] == ["min_edge"]
    assert overridden["effective"]["min_edge"] == pytest.approx(0.09)
    # The resolved block is complete, not just the changed field.
    assert overridden["effective"]["stop_loss"] == pytest.approx(
        trader.policy.stop_loss
    )


def test_journal_pruning_preserves_entry_and_exit_evidence(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({"execution_mode": "dry_run"})
    for index in range(40):
        trader._journal(
            "mark",
            "updated",
            event_id="event-1",
            payload={"index": index},
        )
    trader._journal("entry", "simulated_fill", event_id="event-1")
    trader._journal("exit", "profit_lock", event_id="event-1")
    for index in range(40):
        trader._journal(
            "qualification",
            "rejected",
            event_id="event-1",
            payload={"index": index},
        )

    trader._prune_journal(maximum=5)
    kinds = [item["kind"] for item in trader.journal(limit=500)]

    # High-volume chatter ages out; the order audit trail does not.
    assert "entry" in kinds
    assert "exit" in kinds
    assert kinds.count("mark") + kinds.count("qualification") <= 5


def test_marks_retain_both_excursion_extremes(tmp_path):
    """Adverse excursion is what makes a tighter stop identifiable later."""
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        # Keep the position open so the whole path is observed.
        "stop_loss": 0.95,
        "profit_target": 0.95,
        "auto_cashout": False,
    })
    trader.run_cycle(
        [(event(), [signal(clock, market="moneyline", outcome="Away")])],
        us_payload(bid=0.39, ask=0.40),
    )
    for bid, ask in ((0.34, 0.35), (0.30, 0.31), (0.44, 0.45)):
        clock.value += 60
        trader.run_cycle(
            [(event(), [signal(clock, market="moneyline", outcome="Away")])],
            us_payload(bid=bid, ask=ask),
        )

    with trader._db.cursor(dict_rows=True) as cur:
        trader._db.execute(
            cur,
            """SELECT entry_cost,highest_exit_value,lowest_exit_value
               FROM live_managed_positions""",
        )
        row = dict(cur.fetchone())

    assert row["entry_cost"] == pytest.approx(0.40)
    # The path dipped to 0.30 before recovering to 0.44, so a recovery must not
    # erase the adverse extreme the position actually passed through.
    assert row["highest_exit_value"] == pytest.approx(0.44)
    assert row["lowest_exit_value"] == pytest.approx(0.30)


def test_a_position_that_only_rises_records_no_adverse_excursion(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "stop_loss": 0.95,
        "profit_target": 0.95,
        "auto_cashout": False,
    })
    trader.run_cycle(
        [(event(), [signal(clock, market="moneyline", outcome="Away")])],
        us_payload(bid=0.39, ask=0.40),
    )
    clock.value += 60
    trader.run_cycle(
        [(event(), [signal(clock, market="moneyline", outcome="Away")])],
        us_payload(bid=0.49, ask=0.50),
    )

    with trader._db.cursor(dict_rows=True) as cur:
        trader._db.execute(
            cur,
            """SELECT entry_cost,lowest_exit_value
               FROM live_managed_positions""",
        )
        row = dict(cur.fetchone())

    # Seeded from entry cost, so "never went adverse" reads as a zero
    # excursion rather than a null the advisor would have to guess about.
    assert row["lowest_exit_value"] == pytest.approx(row["entry_cost"])


def test_journal_page_bounds_the_response_and_reports_what_it_omits(tmp_path):
    """Polling the whole journal was the largest response in the dashboard."""
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({"execution_mode": "dry_run"})
    for index in range(60):
        trader._journal(
            "qualification", "rejected", event_id="event-1",
            payload={"index": index},
        )
    trader._journal("entry", "simulated_fill", event_id="event-1")
    trader._journal("exit", "profit_lock", event_id="event-1")

    page = trader.journal_page(limit=25)

    assert len(page["items"]) == 25
    # The operator can see how much is not on screen instead of a silent cut.
    assert page["total"] == sum(page["kind_counts"].values())
    assert page["filtered_total"] == page["total"]
    assert page["offset"] == 0
    assert page["kind_counts"]["qualification"] == 60
    assert page["kind_counts"]["entry"] == 1
    assert page["kind_counts"]["exit"] == 1

    # Paging walks backwards through time without overlap.
    first_ids = [item["id"] for item in page["items"]]
    second = trader.journal_page(limit=25, offset=25)
    assert second["offset"] == 25
    assert not set(first_ids) & {item["id"] for item in second["items"]}

    # Filtering surfaces the order audit trail the chatter would otherwise bury.
    filtered = trader.journal_page(limit=25, kinds=["entry", "exit"])
    assert filtered["filtered_total"] == 2
    assert {item["kind"] for item in filtered["items"]} == {"entry", "exit"}
    assert filtered["kinds"] == ["entry", "exit"]
    # Unfiltered totals stay available so the filter is visibly a filter.
    assert filtered["total"] == page["total"]
    assert filtered["filtered_total"] < filtered["total"]


def test_journal_kind_filter_rejects_nothing_and_ignores_blanks(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({"execution_mode": "dry_run"})
    trader._journal("entry", "simulated_fill", event_id="event-1")
    unfiltered = trader.journal()

    # Blank or whitespace-only filters must behave as "no filter", never as a
    # filter that silently matches nothing.
    assert len(trader.journal(kinds=["", "  "])) == len(unfiltered)
    assert trader.journal_page(kinds=[])["filtered_total"] == len(unfiltered)
    # An unknown kind is a real filter and correctly matches nothing.
    assert trader.journal(kinds=["nonexistent-kind"]) == []


def test_mlb_stage_filter_requires_explicit_non_late_inning_state(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    baseball = Event(
        id="event-1",
        name="Away at Home",
        sport="baseball",
        league="MLB",
        home="Home",
        away="Away",
        game_start="2027-01-15T00:00:00Z",
    )
    received = datetime.fromtimestamp(clock(), timezone.utc)
    late_state = GameState(
        event_id=baseball.id,
        home_score=2,
        away_score=2,
        period="Top 8",
        clock="",
        source="test",
        provider_timestamp=received,
        received_at=received,
        processed_at=received,
        live=True,
        ended=False,
    )
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "min_mlb_fraction_remaining": 0.25,
    })

    result = trader.run_cycle(
        [(baseball, [signal(clock)])],
        us_payload(),
        game_states={baseball.id: late_state},
    )

    assert result["entries"] == 0
    rejection = next(
        item for item in trader.journal()
        if item["kind"] == "qualification"
    )
    assert "below the configured 25% floor" in rejection["details"]["reason"]


def test_per_event_hourly_entry_cap_blocks_repeat_churn(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "max_entries_per_event_per_hour": 1,
    })
    first = trader.run_cycle([(event(), [signal(clock)])], us_payload())
    position = trader.positions(open_only=True)[0]
    trader._close_position(
        position["id"],
        exit_value=0.41,
        reason="test",
        order_id=None,
    )
    clock.value += 301
    second = trader.run_cycle([(event(), [signal(clock)])], us_payload())

    assert first["entries"] == 1
    assert second["entries"] == 0
    blocked = next(
        item for item in trader.journal()
        if item["kind"] == "entry"
        and item["status"] == "rejected"
        and "per-event limit" in " ".join(item["details"].get("reasons", []))
    )
    assert blocked["details"]["event_entries_60m"] == 2


def test_performance_ledger_filters_and_attributes_entry_settings(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "risk_preset": "custom",
        "allowed_market_types": ["moneyline"],
        "min_edge": 0.025,
        "min_signal_quality": 72.0,
    })
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    position = trader.positions(open_only=True)[0]
    trader._close_position(
        position["id"],
        exit_value=0.47,
        reason="test_profitable_close",
        order_id=None,
    )

    ledger = trader.performance_ledger(
        mode="dry_run",
        market_type="moneyline",
        result="win",
        query="away",
    )

    assert ledger["summary"]["trades"] == 1
    assert ledger["summary"]["wins"] == 1
    assert ledger["summary"]["losses"] == 0
    assert ledger["summary"]["win_rate"] == 1.0
    assert ledger["summary"]["realized_net_usd"] > 0
    assert ledger["line_type_summary"][0]["market_type"] == "moneyline"
    assert ledger["settings_groups"][0]["settings"]["min_edge"] == pytest.approx(
        0.025
    )
    assert ledger["settings_groups"][0]["settings"][
        "allowed_market_types"
    ] == ["moneyline"]
    assert ledger["rows"][0]["policy_signature"] != "unavailable"
    assert ledger["rows"][0]["entry_signal_quality"] == pytest.approx(85.0)

    assert trader.performance_ledger(
        market_type="spread"
    )["summary"]["trades"] == 0


def test_performance_ledger_can_aggregate_independent_execution_stores(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    dry_position = trader.positions(open_only=True)[0]
    live_position = {
        **dry_position,
        "id": "independent-live-position",
        "mode": "live",
        "event_id": "independent-live-event",
        "event_name": "Live Away at Live Home",
    }

    ledger = trader.performance_ledger(
        mode="all",
        source_positions=[dry_position, live_position],
        data_sources=[
            {"lane": "dry_run", "retained_positions": 1},
            {"lane": "live", "retained_positions": 1},
        ],
    )

    assert ledger["summary"]["trades"] == 2
    assert {row["mode"] for row in ledger["rows"]} == {"dry_run", "live"}
    assert ledger["data_sources"] == [
        {"lane": "dry_run", "retained_positions": 1},
        {"lane": "live", "retained_positions": 1},
    ]


def test_policy_advisor_is_reactive_auditable_and_explicitly_applied(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "risk_preset": "custom",
        "max_orders_per_hour": 6,
        "candidate_cooldown_seconds": 300,
    })

    advice = trader.policy_advice(
        objective="more_trades",
        target_trades_per_hour=8.0,
        model_evidence={
            "stage": "collecting",
            "live_eligible": False,
            "live_blockers": ["insufficient independent events"],
        },
    )

    assert advice["status"] == "exploratory"
    assert advice["suggested_policy"]["max_orders_per_hour"] == 8
    assert advice["suggested_policy"]["candidate_cooldown_seconds"] == 120
    assert advice["model_used_to_change_settings"] is False
    assert len(advice["source_policy_hash"]) == 64
    assert advice["evidence"]["source_policy_hash"] == advice[
        "source_policy_hash"
    ]
    assert trader.policy_advice_history()[0]["id"] == advice["id"]
    assert trader.policy_sessions()[0]["policy"]["execution_mode"] == "dry_run"

    with pytest.raises(TradingPolicyError, match=POLICY_ADVICE_APPLY_PHRASE):
        trader.apply_policy_advice(advice["id"], "apply it")

    with pytest.raises(TradingPolicyError, match="diagnostic only"):
        trader.apply_policy_advice(
            advice["id"],
            POLICY_ADVICE_APPLY_PHRASE,
        )
    assert trader.policy_advice_history()[0]["applied_at"] is None
    assert trader.policy.max_orders_per_hour == 6


def test_policy_advisor_rejects_stale_settings_and_accepts_fresh_analysis(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "risk_preset": "custom",
        "max_orders_per_hour": 6,
        "candidate_cooldown_seconds": 300,
    })
    stale = trader.policy_advice(
        objective="more_trades",
        target_trades_per_hour=8.0,
    )

    trader.configure({"min_signal_quality": 77.0})

    with pytest.raises(TradingPolicyError, match="settings changed.*analyze again"):
        trader.apply_policy_advice(
            stale["id"],
            POLICY_ADVICE_APPLY_PHRASE,
        )
    assert trader.policy.min_signal_quality == pytest.approx(77.0)

    fresh = trader.policy_advice(
        objective="more_trades",
        target_trades_per_hour=8.0,
    )
    with pytest.raises(TradingPolicyError, match="diagnostic only"):
        trader.apply_policy_advice(
            fresh["id"],
            POLICY_ADVICE_APPLY_PHRASE,
        )
    assert trader.policy.min_signal_quality == pytest.approx(77.0)


def test_cycle_records_mlb_state_overlay_and_dry_reset_preserves_learning(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    baseball = Event(
        id="event-1",
        name="Away at Home",
        sport="baseball",
        league="MLB",
        home="Home",
        away="Away",
        game_start="2027-01-15T00:00:00Z",
    )
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "adaptive_exit_enabled": True,
        "adaptive_exit_profile": "observe",
    })
    trader.run_cycle([(baseball, [signal(clock)])], us_payload())

    clock.value += 45
    received = datetime.fromtimestamp(clock(), timezone.utc)
    game_state = GameState(
        event_id=baseball.id,
        home_score=2,
        away_score=2,
        period="Top 8",
        clock="",
        source="test-live-feed",
        provider_timestamp=received,
        received_at=received,
        processed_at=received,
        live=True,
        ended=False,
    )
    trader.run_cycle(
        [(baseball, [signal(clock)])],
        us_payload(bid=0.38, ask=0.39),
        game_states={baseball.id: game_state},
    )

    position = trader.positions(open_only=True)[0]
    assert position["adaptive_exit"]["applicable"] is True
    assert position["adaptive_exit"]["profile"] == "observe"
    assert position["adaptive_exit"]["state"]["inning"] == 8
    assert trader.status()["adaptive_exit"]["observations"] == 1

    trader.clear_dry_run_history(DRY_RUN_HISTORY_CLEAR_PHRASE)
    assert trader.positions() == []
    assert trader.status()["adaptive_exit"]["observations"] == 1


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


def test_selective_engine_gate_mode_requires_only_checked_gate_results(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "require_engine_entry": False,
        "required_engine_gates": ["provider_freshness", "market_identity"],
        "min_edge": 0,
    })
    gates = passing_core_gates() + [{
        "code": "calibration_support",
        "passed": None,
        "status": "unknown",
    }]

    result = trader.run_cycle(
        [(event(), [signal(clock, action="WATCH", gate_results=gates)])],
        us_payload(),
    )

    assert result["entries"] == 1
    evaluation = trader.status()["last_cycle_evaluations"][0]
    assert evaluation["required_engine_gates"] == [
        "provider_freshness",
        "market_identity",
    ]
    assert {item["code"] for item in evaluation["selected_engine_gate_results"]} == {
        "provider_freshness",
        "market_identity",
    }


@pytest.mark.parametrize("gate_state", [
    [{"code": "provider_freshness", "passed": False, "status": "fail"}],
    [],
])
def test_selective_engine_gate_mode_fails_closed_for_failed_or_missing_gate(
    tmp_path,
    gate_state,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "require_engine_entry": False,
        "required_engine_gates": ["provider_freshness"],
        "min_edge": 0,
    })

    result = trader.run_cycle(
        [(event(), [signal(clock, action="WATCH", gate_results=gate_state)])],
        us_payload(),
    )

    assert result["entries"] == 0
    rejection = trader.status()["last_cycle_evaluations"][0]
    assert rejection["state"] == "research_only"
    assert "provider_freshness=" in rejection["reason"]


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
    assert isinstance(request["price"]["value"], str)
    assert request["maxBlockTime"] == "5"


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


def test_newer_runtime_live_arm_revokes_stale_dry_run_authority(tmp_path):
    clock = Clock()
    stale = make_trader(tmp_path, clock, fail_on_orders=False)
    stale.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })

    current = make_trader(tmp_path, clock, FakeOrders())
    current.configure({
        "automation_enabled": True,
        "execution_mode": "live",
    })
    current.arm(ARM_PHRASE)

    result = stale.run_cycle([(event(), [signal(clock)])], us_payload())

    assert result["entries"] == 0
    assert stale.policy.execution_mode == "live"
    assert stale.is_armed() is False
    assert stale.positions(open_only=True) == []
    assert not any(
        item["status"] == "simulated_fill"
        for item in stale.journal(limit=100)
    )


def test_live_arm_duration_can_be_selected_up_to_four_hours(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({"automation_enabled": True, "execution_mode": "live"})

    armed = trader.arm(ARM_PHRASE, seconds=2 * 60 * 60)

    assert MAX_ARM_SECONDS == 4 * 60 * 60
    assert armed["armed"] is True
    assert armed["armed_until"] == datetime.fromtimestamp(
        clock() + (2 * 60 * 60),
        timezone.utc,
    ).isoformat()


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


def test_exited_position_cards_can_be_hidden_without_erasing_performance(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    exited = trader.positions(open_only=True)[0]
    trader._close_position(
        exited["id"],
        exit_value=0.50,
        reason="test_profit",
        order_id=None,
    )
    clock.value += 301
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    open_position = trader.positions(open_only=True)[0]
    performance_before = trader.performance()

    result = trader.archive_exited_positions()

    assert result["archived_positions"] == 1
    assert result["positions_deleted"] is False
    assert trader.positions() == [open_position]
    retained = trader.positions(include_hidden=True)
    assert {row["id"] for row in retained} == {exited["id"], open_position["id"]}
    assert trader.performance() == performance_before
    assert any(
        item["kind"] == "position_control"
        and item["status"] == "exited_archived"
        for item in trader.journal()
    )


def test_live_tally_reset_preserves_audit_positions_and_risk_history(tmp_path):
    clock = Clock()
    orders = FakeOrders()
    trader = make_trader(tmp_path, clock, orders)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "live",
    })
    trader.arm(ARM_PHRASE)
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    position = trader.positions(open_only=True)[0]
    trader._close_position(
        position["id"],
        exit_value=0.30,
        reason="test_loss",
        order_id="exit-1",
    )
    trader.disarm()
    before = trader.performance()["modes"]["live"]
    risk_loss_before = trader._daily_realized_loss()

    reset = trader.reset_live_performance(LIVE_PERFORMANCE_RESET_PHRASE)
    after = trader.performance()["modes"]["live"]

    assert before["total_positions"] == 1
    assert before["losses"] == 1
    assert reset["positions_preserved"] is True
    assert reset["execution_journal_preserved"] is True
    assert reset["risk_history_preserved"] is True
    assert after["total_positions"] == 0
    assert after["wins"] == after["losses"] == after["pushes"] == 0
    assert after["total_net_usd"] == 0
    assert after["session_started_at"] == reset["reset_at"]
    assert len(trader.positions()) == 1
    assert trader.positions()[0]["status"] == "closed"
    assert trader._daily_realized_loss() == pytest.approx(risk_loss_before)
    assert any(
        item["kind"] == "performance_reset" and item["status"] == "live"
        for item in trader.journal()
    )
    trader._prune_journal(maximum=0)
    assert trader.performance()["modes"]["live"]["total_positions"] == 0


def test_live_tally_reset_refuses_to_hide_an_open_position(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "live",
    })
    trader.arm(ARM_PHRASE)
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    trader.disarm()

    with pytest.raises(TradingPolicyError, match="open live position"):
        trader.reset_live_performance(LIVE_PERFORMANCE_RESET_PHRASE)

    assert trader.performance()["modes"]["live"]["open_positions"] == 1


def test_dry_run_session_tally_zeroes_display_and_preserves_everything(
    tmp_path,
):
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
        exit_value=0.50,
        reason="test_win",
        order_id=None,
    )
    before = trader.performance()["modes"]["dry_run"]

    reset = trader.reset_dry_run_performance(LIVE_PERFORMANCE_RESET_PHRASE)
    after = trader.performance()["modes"]["dry_run"]

    assert before["total_positions"] == 1
    assert before["wins"] == 1
    assert reset["mode"] == "dry_run"
    assert reset["positions_preserved"] is True
    assert after["total_positions"] == 0
    assert after["wins"] == after["losses"] == after["pushes"] == 0
    assert after["total_net_usd"] == 0
    assert after["session_started_at"] == reset["reset_at"]
    # The record is a display boundary only: the closed position survives.
    assert len(trader.positions()) == 1
    assert trader.positions()[0]["status"] == "closed"
    assert any(
        item["kind"] == "performance_reset" and item["status"] == "dry_run"
        for item in trader.journal()
    )

    # A new dry position after the boundary counts in the fresh session.
    clock.value += 600
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    fresh = trader.performance()["modes"]["dry_run"]
    assert fresh["total_positions"] == 1
    assert fresh["open_positions"] == 1
    trader.close()


def test_dry_run_session_tally_requires_approval_and_no_open_positions(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })
    with pytest.raises(TradingPolicyError, match="approve"):
        trader.reset_dry_run_performance("nope")

    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    with pytest.raises(TradingPolicyError, match="open dry-run position"):
        trader.reset_dry_run_performance(LIVE_PERFORMANCE_RESET_PHRASE)
    assert trader.performance()["modes"]["dry_run"]["open_positions"] == 1
    trader.close()


def test_new_risk_session_resets_only_entry_counters_and_preserves_evidence(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, FakeOrders())
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "live",
        "max_orders_per_hour": 1,
        "max_daily_loss_usd": 0.05,
    })
    trader.arm(ARM_PHRASE)
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    position = trader.positions(open_only=True)[0]
    trader._close_position(
        position["id"],
        exit_value=0.30,
        reason="test_loss",
        order_id="exit-1",
    )
    before_performance = trader.performance()
    before = trader.status()["risk_session"]
    assert before["orders_last_hour"] == 1
    assert before["realized_loss_24h_usd"] > 0.05
    assert set(before["active_entry_blockers"]) == {
        "rolling_realized_loss",
        "hourly_live_entries",
    }

    with pytest.raises(TradingPolicyError, match="disarm live trading"):
        trader.reset_risk_session(RISK_SESSION_RESET_PHRASE)
    with pytest.raises(TradingPolicyError, match=RISK_SESSION_RESET_PHRASE):
        trader.reset_risk_session("reset everything")

    trader.disarm()
    clock.value += 1
    reset = trader.reset_risk_session(RISK_SESSION_RESET_PHRASE)
    current = trader.status()["risk_session"]

    assert reset["positions_preserved"] is True
    assert reset["performance_preserved"] is True
    assert reset["execution_journal_preserved"] is True
    assert reset["per_position_stop_loss_preserved"] is True
    assert reset["previous"]["orders_last_hour"] == 1
    assert reset["previous"]["realized_loss_24h_usd"] > 0.05
    assert current["orders_last_hour"] == 0
    assert current["realized_loss_24h_usd"] == 0
    assert current["active_entry_blockers"] == []
    assert "per-position hard stop" in current["always_enforced"]
    after_performance = trader.performance()
    assert after_performance["modes"] == before_performance["modes"]
    assert after_performance["combined"] == before_performance["combined"]
    retained = trader.positions()
    assert len(retained) == 1
    assert retained[0]["status"] == "closed"
    assert any(
        item["kind"] == "risk_session_reset" and item["status"] == "started"
        for item in trader.journal()
    )

    # Automatic journal pruning must not resurrect pre-reset loss/order history.
    trader._prune_journal(maximum=0)
    assert trader._orders_last_hour() == 0
    assert trader._daily_realized_loss() == 0


def test_live_cashout_uses_the_executable_long_bid_for_a_long_position(tmp_path):
    clock = Clock()
    orders = FakeOrders()
    book = {
        "marketData": {
            "offers": [{"qty": "100", "px": {"value": 0.40}}],
            "bids": [{"qty": "100", "px": {"value": 0.39}}],
            "state": "MARKET_STATE_OPEN",
        }
    }
    trader = make_trader(tmp_path, clock, orders, book=book)
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
    book["marketData"]["bids"] = [
        {"qty": "100", "px": {"value": 0.48}}
    ]
    book["marketData"]["offers"] = [
        {"qty": "100", "px": {"value": 0.49}}
    ]
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.45)])],
        us_payload(bid=0.48, ask=0.49),
    )

    assert len(orders.created) == 2
    exit_order = orders.created[1]
    assert exit_order["intent"] == "ORDER_INTENT_SELL_LONG"
    assert exit_order["price"]["value"] == "0.48"
    assert trader.positions(open_only=True) == []


def test_failed_live_profit_exit_never_chases_the_next_quote_below_floor(
    tmp_path,
):
    clock = Clock()
    orders = FirstExitDoesNotFillOrders()
    book = {
        "marketData": {
            "offers": [{"qty": "100", "px": {"value": 0.40}}],
            "bids": [{"qty": "100", "px": {"value": 0.39}}],
            "state": "MARKET_STATE_OPEN",
        }
    }
    trader = make_trader(tmp_path, clock, orders, book=book)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "live",
        "auto_cashout": True,
        "min_hold_minutes": 10,
        "profit_target": 0.10,
        "minimum_locked_profit": 0.02,
        "trailing_drawdown": 0.04,
    })
    trader.arm(ARM_PHRASE)
    trader.run_cycle([(event(), [signal(clock)])], us_payload())

    for bid in (0.47, 0.48):
        clock.value += 15
        book["marketData"]["bids"] = [
            {"qty": "100", "px": {"value": bid}}
        ]
        book["marketData"]["offers"] = [
            {"qty": "100", "px": {"value": bid + 0.01}}
        ]
        trader.run_cycle(
            [(event(), [signal(clock, probability=0.70)])],
            us_payload(bid=bid, ask=bid + 0.01),
        )

    assert trader.positions(open_only=True)[0]["profit_lock_armed_at"] is not None

    # A profitable pullback triggers an FOK attempt. The fake venue reports no
    # fill, reproducing a quote that disappears between preview and create.
    clock.value += 15
    book["marketData"]["bids"] = [
        {"qty": "100", "px": {"value": 0.44}}
    ]
    book["marketData"]["offers"] = [
        {"qty": "100", "px": {"value": 0.45}}
    ]
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.70)])],
        us_payload(bid=0.44, ask=0.45),
    )
    assert len(orders.created) == 2
    assert trader.positions(open_only=True)

    # The next book is below the fee-adjusted retained-profit floor. The
    # workstation must hold instead of submitting a loss-making chase order.
    clock.value += 15
    book["marketData"]["bids"] = [
        {"qty": "100", "px": {"value": 0.39}}
    ]
    book["marketData"]["offers"] = [
        {"qty": "100", "px": {"value": 0.40}}
    ]
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.70)])],
        us_payload(bid=0.39, ask=0.40),
    )

    assert len(orders.created) == 2
    held = trader.positions(open_only=True)[0]
    assert held["profit_guard"]["status"] == "profit_floor_missed_holding"
    latest_mark = next(
        item
        for item in trader.journal()
        if item["kind"] == "mark"
        and item["details"].get("profit_guard", {}).get("blocked")
    )
    assert latest_mark["details"]["gross_exit_value"] == pytest.approx(0.39)
    assert latest_mark["details"]["estimated_exit_fee"] > 0
    assert (
        latest_mark["details"]["fee_adjusted_exit_value"]
        < latest_mark["details"]["profit_guard"]["protected_floor_value"]
    )


def test_profit_lock_stays_armed_until_a_material_pullback(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "auto_cashout": True,
        "min_hold_minutes": 10,
        "profit_target": 0.15,
        "trailing_drawdown": 0.04,
    })
    trader.run_cycle([(event(), [signal(clock)])], us_payload())

    clock.value += 11 * 60
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.70)])],
        us_payload(bid=0.48, ask=0.49),
    )
    armed = trader.positions(open_only=True)[0]
    assert armed["profit_lock_armed_at"] is not None

    clock.value += 301
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.70)])],
        us_payload(bid=0.45, ask=0.46),
    )
    assert trader.positions(open_only=True) == []
    assert trader.positions()[0]["exit_reason"] == "trailing_profit_lock"


def test_two_target_readings_arm_and_protect_before_fallback_hold_expires(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "auto_cashout": True,
        "min_hold_minutes": 10,
        "profit_target": 0.10,
        "trailing_drawdown": 0.04,
    })
    trader.run_cycle([(event(), [signal(clock)])], us_payload())

    clock.value += 120
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.70)])],
        us_payload(bid=0.45, ask=0.46),
    )
    pending = trader.positions(open_only=True)[0]
    assert pending["profit_lock_armed_at"] is None
    assert pending["profit_target_observation_count"] == 1

    clock.value += 15
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.70)])],
        us_payload(bid=0.46, ask=0.47),
    )
    armed = trader.positions(open_only=True)[0]
    assert armed["profit_lock_armed_at"] is not None
    assert armed["profit_target_observation_count"] >= 2

    clock.value += 15
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.70)])],
        us_payload(bid=0.43, ask=0.44),
    )

    assert trader.positions(open_only=True) == []
    closed = trader.positions()[0]
    assert closed["exit_reason"] == "trailing_profit_lock"
    held_minutes = (
        datetime.fromisoformat(closed["closed_at"])
        - datetime.fromisoformat(closed["opened_at"])
    ).total_seconds() / 60
    assert held_minutes < 10


def test_profit_lock_does_not_chase_a_gap_below_the_protected_floor(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "auto_cashout": True,
        "min_hold_minutes": 10,
        "profit_target": 0.10,
        "minimum_locked_profit": 0.02,
        "trailing_drawdown": 0.04,
    })
    trader.run_cycle([(event(), [signal(clock)])], us_payload())

    # Two executable target readings arm protection before the fallback timer.
    clock.value += 120
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.70)])],
        us_payload(bid=0.45, ask=0.46),
    )
    clock.value += 15
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.70)])],
        us_payload(bid=0.46, ask=0.47),
    )

    # The next quote gaps through both the trailing trigger and the retained
    # profit floor. This is not a hard stop or material model reversal, so the
    # profit exit must not convert a formerly green position into a loss.
    clock.value += 15
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.70)])],
        us_payload(bid=0.39, ask=0.40),
    )

    held = trader.positions(open_only=True)
    assert len(held) == 1
    assert held[0]["profit_guard"]["status"] == "profit_floor_missed_holding"
    assert held[0]["profit_guard"]["protected_floor_value"] == pytest.approx(
        0.408
    )
    assert held[0]["profit_floor_missed_count"] == 1
    assert held[0]["realized_pnl"] is None

    # A later quote recovers above the protected floor while the pullback
    # remains active. The ordinary profit exit can now complete positively.
    clock.value += 15
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.70)])],
        us_payload(bid=0.42, ask=0.43),
    )

    assert trader.positions(open_only=True) == []
    closed = trader.positions()[0]
    assert closed["exit_reason"] == "trailing_profit_lock"
    assert closed["realized_pnl"] > 0


def test_material_model_reversal_can_override_the_protected_profit_floor(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "auto_cashout": True,
        "min_hold_minutes": 10,
        "profit_target": 0.10,
        "minimum_locked_profit": 0.02,
        "trailing_drawdown": 0.04,
        "stop_loss": 0.50,
    })
    trader.run_cycle([(event(), [signal(clock)])], us_payload())

    clock.value += 120
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.70)])],
        us_payload(bid=0.45, ask=0.46),
    )
    clock.value += 15
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.70)])],
        us_payload(bid=0.46, ask=0.47),
    )

    # The authenticated entry quote is 43c while the model is now at 35c:
    # -8c is a material reversal, so safety is allowed to override the floor.
    clock.value += 15
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.35)])],
        us_payload(bid=0.39, ask=0.43),
    )

    assert trader.positions(open_only=True) == []
    closed = trader.positions()[0]
    assert closed["exit_reason"] == "model_reversal"
    mark = next(
        item
        for item in trader.journal()
        if item["kind"] == "mark"
        and item["details"].get("profit_guard", {}).get("status")
        == "material_reversal_override"
    )
    assert mark["details"]["profit_guard"]["hard_stops_unchanged"] is True


def test_hard_stop_is_not_delayed_by_minimum_profit_hold(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "auto_cashout": True,
        "min_hold_minutes": 30,
        "stop_loss": 0.20,
    })
    trader.run_cycle([(event(), [signal(clock)])], us_payload())

    clock.value += 60
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.70)])],
        us_payload(bid=0.30, ask=0.31),
    )

    assert trader.positions(open_only=True) == []
    assert trader.positions()[0]["exit_reason"] == "hard_stop_loss"


def test_state_aware_stop_persists_confirmation_and_audits_later_recovery(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    baseball = Event(
        id="event-1",
        name="Away at Home",
        sport="baseball",
        league="MLB",
        home="Home",
        away="Away",
        game_start="2027-01-15T00:00:00Z",
    )
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "auto_cashout": True,
        "stop_loss": 0.20,
        "volatility_stop_enabled": True,
        "stop_confirmation_readings": 2,
        "stop_grace_minutes": 1.0,
        "catastrophic_stop_multiplier": 2.0,
        "post_exit_tracking_minutes": 5.0,
        "candidate_cooldown_seconds": 300,
    })
    trader.run_cycle([(baseball, [signal(clock)])], us_payload())

    def live_state():
        observed = datetime.fromtimestamp(clock(), timezone.utc)
        return GameState(
            event_id=baseball.id,
            home_score=1,
            away_score=1,
            period="Top 5",
            clock="",
            source="test-mlb",
            provider_timestamp=observed,
            received_at=observed,
            processed_at=observed,
            status="in_progress",
            live=True,
            ended=False,
        )

    clock.value += 60
    trader.run_cycle(
        [(baseball, [signal(clock, probability=0.70)])],
        us_payload(bid=0.30, ask=0.31),
        game_states={baseball.id: live_state()},
    )
    observing = trader.positions(open_only=True)[0]
    assert observing["stop_observation_count"] == 1
    assert observing["stop_guard"]["status"] == "observing_recovery"

    clock.value += 60
    trader.run_cycle(
        [(baseball, [signal(clock, probability=0.70)])],
        us_payload(bid=0.29, ask=0.30),
        game_states={baseball.id: live_state()},
    )
    assert trader.positions(open_only=True) == []
    assert trader.positions()[0]["exit_reason"] == "confirmed_stop_loss"
    assert trader.status()["adaptive_exit"]["exit_recovery"]["exits"] == 1

    clock.value += 60
    trader.run_cycle(
        [(baseball, [signal(clock, probability=0.70)])],
        us_payload(bid=0.41, ask=0.42),
        game_states={baseball.id: live_state()},
    )
    recovery = trader.status()["adaptive_exit"]["exit_recovery"]
    assert recovery["recovered_entry"] == 1
    assert recovery["recent"][0]["best_exit_value"] == pytest.approx(0.41)
    trader.close()


def test_protective_cashout_survives_entry_latch_expiration(tmp_path):
    clock = Clock()
    orders = FakeOrders()
    book = {
        "marketData": {
            "offers": [{"qty": "100", "px": {"value": 0.40}}],
            "bids": [{"qty": "100", "px": {"value": 0.39}}],
            "state": "MARKET_STATE_OPEN",
        }
    }
    trader = make_trader(tmp_path, clock, orders, book=book)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "live",
        "auto_cashout": True,
        "min_hold_minutes": 0,
        "profit_target": 0.10,
    })
    trader.arm(ARM_PHRASE)
    trader.run_cycle([(event(), [signal(clock)])], us_payload())

    clock.value += 1801
    assert trader.is_armed() is False
    assert trader.status()["protective_exits_armed"] is True
    book["marketData"]["bids"] = [
        {"qty": "100", "px": {"value": 0.48}}
    ]
    book["marketData"]["offers"] = [
        {"qty": "100", "px": {"value": 0.49}}
    ]
    trader.run_cycle(
        [(event(), [signal(clock, probability=0.45)])],
        us_payload(bid=0.48, ask=0.49),
    )

    assert len(orders.created) == 2
    assert orders.created[-1]["intent"] == "ORDER_INTENT_SELL_LONG"
    assert trader.positions(open_only=True) == []


def test_phone_close_reconciles_after_two_reads_without_submitting_order(tmp_path):
    clock = Clock()
    orders = FakeOrders()
    venue_positions = {}
    trader = make_trader(
        tmp_path,
        clock,
        orders,
        portfolio_positions=venue_positions,
    )
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "live",
    })
    trader.arm(ARM_PHRASE)
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    assert len(orders.created) == 1

    clock.value += 16
    first = trader.synchronize_live_positions()
    assert first["managed"]["pending"] == 1
    assert trader.positions(open_only=True)[0]["venue_sync_status"] == (
        "mismatch_pending_confirmation"
    )

    second = trader.synchronize_live_positions()
    assert second["managed"]["externally_closed"] == 1
    assert len(orders.created) == 1
    assert trader.positions(open_only=True) == []
    reconciled = trader.positions()[0]
    assert reconciled["status"] == "external_closed"
    assert reconciled["realized_pnl"] is None
    assert reconciled["exit_reason"] == "external_phone_or_manual_close"


def test_phone_partial_sale_reduces_only_managed_remaining_quantity(tmp_path):
    clock = Clock()
    orders = FakeOrders()
    venue_positions = {}
    trader = make_trader(
        tmp_path,
        clock,
        orders,
        portfolio_positions=venue_positions,
    )
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "live",
    })
    trader.arm(ARM_PHRASE)
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    original = trader.positions(open_only=True)[0]
    assert float(original["quantity"]) > 2

    venue_positions["away-at-home-moneyline"] = {
        "netPosition": "2",
        "qtyAvailable": "2",
        "cost": "0.80",
        "cashValue": "0.78",
        "realized": "0",
        "marketMetadata": {"title": "Away at Home", "outcome": "Away"},
    }
    clock.value += 16
    trader.synchronize_live_positions()
    trader.synchronize_live_positions()

    remaining = trader.positions(open_only=True)[0]
    assert remaining["quantity"] == pytest.approx(2.0)
    assert remaining["cost_basis"] == pytest.approx(0.8)
    assert remaining["external_exit_quantity"] == pytest.approx(
        float(original["quantity"]) - 2.0
    )
    assert remaining["venue_sync_status"] == "partially_sold_externally"
    assert len(orders.created) == 1


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


def test_individual_dry_run_position_removal_is_immediate_and_keeps_automation_on(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })
    trader.run_cycle([(event(), [signal(clock)])], us_payload())
    position = trader.positions(open_only=True)[0]

    result = trader.exit_position(
        {"events": []},
        position_id=position["id"],
    )

    assert result["status"] == "removed"
    assert result["quote_required"] is False
    assert result["automation_enabled"] is True
    assert trader.positions() == []
    assert trader.policy.automation_enabled is True
    assert any(
        item["kind"] == "position_control"
        and item["status"] == "dry_run_removed"
        for item in trader.journal()
    )


def test_individual_live_position_exit_requires_confirmation_and_sells_only_target(
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
    position = trader.positions(open_only=True)[0]

    with pytest.raises(TradingPolicyError, match=LIVE_POSITION_EXIT_PHRASE):
        trader.exit_position(
            us_payload(bid=0.47, ask=0.48),
            position_id=position["id"],
            confirmation="",
        )

    result = trader.exit_position(
        us_payload(bid=0.47, ask=0.48),
        position_id=position["id"],
        confirmation=LIVE_POSITION_EXIT_PHRASE,
    )

    assert result["status"] == "filled"
    assert result["remaining"] == 0
    assert trader.positions(open_only=True) == []
    assert len(orders.created) == 2
    assert orders.created[-1]["intent"] == "ORDER_INTENT_SELL_LONG"
    assert trader.positions()[0]["exit_reason"] == "manual_individual_live"


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
    assert isinstance(orders.created[-1]["price"]["value"], str)
    assert orders.created[-1]["maxBlockTime"] == "5"
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


def test_executive_reset_cancels_a_cycle_blocked_in_venue_io(tmp_path):
    clock = Clock()
    venue_entered = threading.Event()
    release_venue = threading.Event()

    class SlowClient(FakeClient):
        def __init__(self):
            super().__init__(FakeOrders(), fail_on_orders=True)
            self.account = SimpleNamespace(balances=self._balances)

        def _balances(self):
            venue_entered.set()
            assert release_venue.wait(timeout=5)
            return {
                "balances": [{"buyingPower": 100.0, "currentBalance": 100.0}]
            }

    trader = PolymarketUSAutoTrader(
        str(tmp_path / "trading.db"),
        key_id="key",
        secret_key="secret",
        client_factory=lambda **_kwargs: SlowClient(),
        clock=clock,
    )
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })
    result = {}

    def run():
        result.update(
            trader.run_cycle([(event(), [signal(clock)])], us_payload())
        )

    worker = threading.Thread(target=run)
    worker.start()
    assert venue_entered.wait(timeout=2)

    cleared = trader.clear_dry_run_history(DRY_RUN_HISTORY_CLEAR_PHRASE)
    release_venue.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert cleared["automation_enabled"] is False
    assert result["status"] == "stopped"
    assert trader.positions() == []


def test_stop_from_second_runtime_cancels_stale_cycle_before_fill(tmp_path):
    clock = Clock()
    venue_entered = threading.Event()
    release_venue = threading.Event()

    class SlowClient(FakeClient):
        def __init__(self):
            super().__init__(FakeOrders(), fail_on_orders=True)
            self.account = SimpleNamespace(balances=self._balances)

        def _balances(self):
            venue_entered.set()
            assert release_venue.wait(timeout=5)
            return {
                "balances": [{"buyingPower": 100.0, "currentBalance": 100.0}]
            }

    stale = PolymarketUSAutoTrader(
        str(tmp_path / "trading.db"),
        key_id="key",
        secret_key="secret",
        client_factory=lambda **_kwargs: SlowClient(),
        clock=clock,
    )
    stale.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
    })
    result = {}

    worker = threading.Thread(
        target=lambda: result.update(
            stale.run_cycle([(event(), [signal(clock)])], us_payload())
        )
    )
    worker.start()
    assert venue_entered.wait(timeout=2)

    controller = make_trader(tmp_path, clock)
    controller.stop_automation("second_runtime")
    release_venue.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert result["status"] == "stopped"
    assert stale.positions() == []


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


def test_fast_stop_closes_controls_without_waiting_for_venue_cleanup(tmp_path):
    clock = Clock()
    orders = FakeOrders()
    trader = make_trader(tmp_path, clock, orders)
    trader.configure({"automation_enabled": True, "execution_mode": "live"})
    trader.arm(ARM_PHRASE)
    trader._record_order("managed-1", "market-1", None, "entry", "open")

    stopped = trader.stop_automation("dashboard")

    assert stopped["armed"] is False
    assert stopped["policy"]["automation_enabled"] is False
    assert stopped["stop_ack"] == {
        "accepted_at": datetime.fromtimestamp(
            clock(), timezone.utc
        ).isoformat(),
        "automation_disabled": True,
        "live_disarmed": True,
        "active_cycle_invalidated": True,
        "venue_cleanup_deferred": True,
    }
    assert orders.canceled == []


def test_venue_errors_are_redacted_before_they_can_reach_the_journal(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)

    safe = trader._safe_error(RuntimeError("request used key and secret"))

    assert safe == "request used [redacted] and [redacted]"


def test_venue_http_errors_include_status_without_exposing_credentials(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    error = RuntimeError("request used key and secret")
    error.status_code = 500

    safe = trader._safe_error(error)

    assert safe == "HTTP 500 RuntimeError: request used [redacted] and [redacted]"


def _adaptive_recommendation(trader, **kwargs):
    return trader._predictive_exit_recommendation(**kwargs)


def test_adaptive_recommendation_holds_at_observe_until_the_overlay_scores(
    tmp_path,
):
    """The overlay must earn the right to move a live exit."""
    clock = Clock()
    trader = make_trader(tmp_path, clock)

    nothing = _adaptive_recommendation(
        trader, labeled=0, labeled_events=0, required=30, brier=None
    )
    assert nothing["profile"] == "observe"
    assert nothing["basis"] == "insufficient_evidence"

    thin = _adaptive_recommendation(
        trader, labeled=12, labeled_events=2, required=30, brier=0.10
    )
    # Good Brier but far too little support: a non-observe profile would
    # already tighten from its cold-start floor on this.
    assert thin["profile"] == "observe"
    assert thin["basis"] == "insufficient_evidence"

    unscored = _adaptive_recommendation(
        trader, labeled=60, labeled_events=8, required=30, brier=None
    )
    assert unscored["profile"] == "observe"


def test_adaptive_recommendation_requires_beating_the_always_half_benchmark(
    tmp_path,
):
    clock = Clock()
    trader = make_trader(tmp_path, clock)

    # 0.25 is exactly what always forecasting 0.50 achieves.
    no_better = _adaptive_recommendation(
        trader, labeled=60, labeled_events=8, required=30, brier=0.25
    )
    assert no_better["profile"] == "observe"
    assert no_better["basis"] == "overlay_scored"
    assert "no better than always forecasting" in no_better["rationale"]

    earned = _adaptive_recommendation(
        trader, labeled=60, labeled_events=8, required=30, brier=0.18
    )
    # The smallest step above observe-only, never straight to responsive.
    assert earned["profile"] == "guarded"
    assert earned["basis"] == "overlay_scored"
    assert earned["brier_score"] == pytest.approx(0.18)
    assert "not evidence for Balanced or Responsive" in earned["rationale"]
    assert "says nothing about realized return" in earned["rationale"]


def test_execution_state_carries_the_adaptive_recommendation(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({
        "execution_mode": "dry_run",
        "adaptive_exit_enabled": True,
        "adaptive_exit_profile": "responsive",
    })

    predictive = trader.execution_state()["predictive_exit"]

    assert predictive["recommendation"]["profile"] == "observe"
    # The lane is running Responsive on no scored evidence, so the state must
    # surface both the recommendation and the cold-start caveat.
    assert predictive["cold_start_warning"]
    assert predictive["can_tighten_exits"] is True


def test_fee_implied_edge_floor_tracks_the_venue_fee_curve():
    """Break-even edge is the round-trip fee, which peaks at 50c."""
    from app.polymarket_us_trading import (
        ROUND_TRIP_TAKER_FEE_RATE,
        _fee_implied_edge_floor,
    )

    # Disabled and out-of-range prices contribute no floor at all.
    assert _fee_implied_edge_floor(0.50, 0.0) is None
    assert _fee_implied_edge_floor(0.0, 1.0) is None
    assert _fee_implied_edge_floor(1.0, 1.0) is None

    # At break-even margin the floor is exactly the round-trip fee per share.
    at_half = _fee_implied_edge_floor(0.50, 1.0)
    assert at_half == pytest.approx(ROUND_TRIP_TAKER_FEE_RATE * 0.25)
    # Symmetric about 50c, and cheaper toward both extremes.
    assert _fee_implied_edge_floor(0.20, 1.0) == pytest.approx(
        _fee_implied_edge_floor(0.80, 1.0)
    )
    assert _fee_implied_edge_floor(0.20, 1.0) < at_half
    assert _fee_implied_edge_floor(0.90, 1.0) < _fee_implied_edge_floor(0.70, 1.0)
    # Margin scales it linearly.
    assert _fee_implied_edge_floor(0.50, 2.0) == pytest.approx(2 * at_half)


def test_fee_aware_gate_blocks_an_edge_that_cannot_clear_its_own_fee(tmp_path):
    clock = Clock()
    trader = make_trader(tmp_path, clock, fail_on_orders=True)
    # Entry at 40c: round-trip fee costs 0.1*0.4*0.6 = 2.4c of edge.
    # A 3c flat floor lets a 5c edge through; a 1.5x fee margin needs 3.6c.
    trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "min_edge": 0.03,
        "fee_edge_margin": 0.0,
    })
    permitted = trader.run_cycle(
        [(event(), [signal(clock, probability=0.435)])],
        us_payload(bid=0.39, ask=0.40),
    )
    assert permitted["entries"] == 1

    clock.value += 600
    blocked_trader = make_trader(tmp_path / "b", clock, fail_on_orders=True)
    blocked_trader.configure({
        "automation_enabled": True,
        "execution_mode": "dry_run",
        "min_edge": 0.03,
        "fee_edge_margin": 1.5,
    })
    blocked = blocked_trader.run_cycle(
        [(event(), [signal(clock, probability=0.435)])],
        us_payload(bid=0.39, ask=0.40),
    )
    assert blocked["entries"] == 0
    assert any(
        "round-trip taker fee" in str(item.get("reason") or "")
        for item in blocked_trader.status()["last_cycle_evaluations"]
    )


def _book_at(ask, bid):
    return {
        "marketData": {
            "offers": [{"qty": "100", "px": {"value": ask}}],
            "bids": [{"qty": "100", "px": {"value": bid}}],
            "state": "MARKET_STATE_OPEN",
        }
    }


def test_fee_aware_gate_is_price_dependent_not_a_flat_raise(tmp_path):
    """The same 3.5c edge clears at a cheap price and fails at a mid price."""
    clock = Clock()

    # 20c entry: fee floor = 1.5 * 0.1 * 0.2 * 0.8 = 2.4c, under the 3.5c edge.
    cheap = make_trader(
        tmp_path / "cheap", clock, fail_on_orders=True,
        book=_book_at(0.20, 0.19),
    )
    cheap.configure({
        "automation_enabled": True, "execution_mode": "dry_run",
        "min_edge": 0.03, "fee_edge_margin": 1.5,
    })
    assert cheap.run_cycle(
        [(event(), [signal(clock, probability=0.235)])],
        us_payload(bid=0.19, ask=0.20),
    )["entries"] == 1

    # 50c entry, same 3.5c edge: fee floor = 1.5 * 0.1 * 0.25 = 3.75c, over it.
    mid = make_trader(
        tmp_path / "mid", clock, fail_on_orders=True,
        book=_book_at(0.50, 0.49),
    )
    mid.configure({
        "automation_enabled": True, "execution_mode": "dry_run",
        "min_edge": 0.03, "fee_edge_margin": 1.5,
    })
    assert mid.run_cycle(
        [(event(), [signal(clock, probability=0.535)])],
        us_payload(bid=0.49, ask=0.50),
    )["entries"] == 0


def test_fee_edge_margin_validates_and_is_profile_overridable():
    from app.polymarket_us_trading import LINE_EXECUTION_PROFILE_FIELDS

    assert "fee_edge_margin" in LINE_EXECUTION_PROFILE_FIELDS
    assert TradingPolicy().fee_edge_margin == 0.0  # off preserves behaviour
    with pytest.raises(TradingPolicyError, match="fee_edge_margin"):
        TradingPolicy.from_mapping({"fee_edge_margin": 0.5})
    with pytest.raises(TradingPolicyError, match="fee_edge_margin"):
        TradingPolicy.from_mapping({"fee_edge_margin": 6.0})

    # A line profile may set its own margin, so an expensive line can demand
    # more headroom than a cheap one.
    policy = TradingPolicy.from_mapping({
        "fee_edge_margin": 1.2,
        "line_execution_profiles": [{
            "market_type": "spread", "game_stage": "all", "enabled": True,
            "overrides": {"fee_edge_margin": 2.0},
        }],
    })
    spread, _ = policy.execution_policy_for("spread", None)
    moneyline, _ = policy.execution_policy_for("moneyline", None)
    assert spread.fee_edge_margin == pytest.approx(2.0)
    assert moneyline.fee_edge_margin == pytest.approx(1.2)


def test_candidate_pruning_sheds_unmapped_noise_before_executable_evidence(
    tmp_path,
):
    """Unmapped rows are ~99% of volume and carry the least information.

    A signal with no tradable US contract is logged on every cooldown. If
    pruning treated it the same as an executable candidate, a busy slate would
    evict the mapped population the advisor depends on to make room for rows
    that were never tradable.
    """
    clock = Clock()
    trader = make_trader(tmp_path, clock)
    trader.configure({"execution_mode": "dry_run"})

    def insert(row_id, *, mapped, entered, ts):
        with trader._db.transaction() as cur:
            trader._db.execute(
                cur,
                """INSERT INTO candidate_observations(
                     id,observed_ts,mode,event_id,state,reason,mapped,executable,
                     entered,propensity_source)
                   VALUES (%s,%s,'dry_run','event-1','x',NULL,%s,%s,%s,'deterministic_policy')""",
                (row_id, ts, mapped, mapped, entered),
            )

    # Newest unmapped, oldest mapped: age alone would keep the wrong ones.
    for i in range(6):
        insert(f"unmapped-{i}", mapped=0, entered=0, ts=2000 + i)
    for i in range(3):
        insert(f"mapped-{i}", mapped=1, entered=0, ts=1000 + i)
    insert("entered-0", mapped=1, entered=1, ts=900)

    trader._prune_candidate_observations(maximum=4)

    with trader._db.cursor(dict_rows=True) as cur:
        trader._db.execute(cur, "SELECT id, mapped, entered FROM candidate_observations")
        kept = {dict(r)["id"] for r in cur.fetchall()}

    # Every unmapped row goes first, despite being the newest.
    assert not any(k.startswith("unmapped-") for k in kept)
    # The executable evidence survives, including the observed-reward row.
    assert {"mapped-0", "mapped-1", "mapped-2", "entered-0"} == kept
