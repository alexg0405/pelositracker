"""Fail-closed Polymarket US automation for the isolated workstation.

This module consumes :class:`~app.models.Signal` objects produced by the existing
engine.  It does not alter probabilities, edge, signal quality, calibration, or
engine gates.  Its responsibilities are deliberately separate:

* map an established signal to one unambiguous Polymarket US contract;
* enforce bankroll, concentration, price, spread, freshness, and cadence limits;
* preview every live order and use bounded FOK limit orders;
* manage only positions and order ids created by this workstation;
* journal candidates, rejections, previews, fills, marks, and exits.

Live arming is process-local and expires.  A restart always returns to disarmed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import math
import re
import threading
import time
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from .approval import APPROVAL_TOKEN, approval_granted, approval_instruction
from .adaptive_exit_model import (
    ADAPTIVE_EXIT_PROFILES,
    AdaptiveExitDecision,
    AdaptiveExitModel,
)
from .database import Database
from .entry_policy import MAX_ENTRY_PRICE, MIN_ENTRY_PRICE
from .lines import is_spread_market, is_total_market, quote_line_side
from .models import Event, GameState, Signal
from .policy_advisor import (
    ADVISOR_OBJECTIVES,
    ADVISOR_TUNABLE_FIELDS,
    recommend_policy,
)


POLICY_VERSION = "pmus-live-risk-policy-v7-state-aware-exits"
RISK_PRESET_VERSION = "risk-presets-v2"
_CONTROL_TOKEN_KEY = "_execution_control_token"
RISK_PRESETS: dict[str, dict[str, Any]] = {
    "cautious": {
        "label": "Cautious",
        "description": "Higher evidence bars, smaller positions, and more cash left unused.",
        "exposure_fraction": 0.50,
        "position_fraction": 0.10,
        "event_fraction": 0.20,
        "daily_loss_fraction": 0.10,
        "minimum_cash_reserve_fraction": 0.50,
        "max_open_positions": 3,
        "max_orders_per_hour": 3,
        "min_edge": 0.06,
        "max_edge": 0.15,
        "min_signal_quality": 75.0,
        "min_reference_sources": 2,
        "min_entry_price": 0.15,
        "max_entry_price": 0.85,
        "max_spread": 0.02,
        "min_book_shares": 10.0,
        "min_hold_minutes": 20.0,
        "profit_target": 0.15,
        "trailing_drawdown": 0.04,
        "stop_loss": 0.12,
        "exit_edge": 0.01,
        "cycle_seconds": 45,
        "candidate_cooldown_seconds": 600,
        "max_entries_per_event_per_hour": 2,
        "min_mlb_fraction_remaining": 0.33,
        "volatility_stop_enabled": True,
        "stop_confirmation_readings": 3,
        "stop_grace_minutes": 1.5,
        "catastrophic_stop_multiplier": 1.60,
        "post_exit_tracking_minutes": 30.0,
    },
    "balanced": {
        "label": "Balanced",
        "description": "Moderate sizing and evidence requirements for ordinary use.",
        "exposure_fraction": 0.70,
        "position_fraction": 0.15,
        "event_fraction": 0.30,
        "daily_loss_fraction": 0.18,
        "minimum_cash_reserve_fraction": 0.30,
        "max_open_positions": 5,
        "max_orders_per_hour": 5,
        "min_edge": 0.04,
        "max_edge": 0.20,
        "min_signal_quality": 65.0,
        "min_reference_sources": 2,
        "min_entry_price": 0.12,
        "max_entry_price": 0.88,
        "max_spread": 0.03,
        "min_book_shares": 5.0,
        "min_hold_minutes": 15.0,
        "profit_target": 0.12,
        "trailing_drawdown": 0.04,
        "stop_loss": 0.16,
        "exit_edge": 0.0,
        "cycle_seconds": 30,
        "candidate_cooldown_seconds": 420,
        "max_entries_per_event_per_hour": 3,
        "min_mlb_fraction_remaining": 0.25,
        "volatility_stop_enabled": True,
        "stop_confirmation_readings": 3,
        "stop_grace_minutes": 2.0,
        "catastrophic_stop_multiplier": 1.70,
        "post_exit_tracking_minutes": 30.0,
    },
    "active": {
        "label": "Active",
        "description": "More positions and faster review while retaining positive-edge controls.",
        "exposure_fraction": 0.85,
        "position_fraction": 0.20,
        "event_fraction": 0.40,
        "daily_loss_fraction": 0.30,
        "minimum_cash_reserve_fraction": 0.15,
        "max_open_positions": 7,
        "max_orders_per_hour": 8,
        "min_edge": 0.025,
        "max_edge": 0.30,
        "min_signal_quality": 55.0,
        "min_reference_sources": 1,
        "min_entry_price": 0.10,
        "max_entry_price": 0.90,
        "max_spread": 0.04,
        "min_book_shares": 3.0,
        "min_hold_minutes": 10.0,
        "profit_target": 0.10,
        "trailing_drawdown": 0.05,
        "stop_loss": 0.22,
        "exit_edge": 0.0,
        "cycle_seconds": 20,
        "candidate_cooldown_seconds": 240,
        "max_entries_per_event_per_hour": 4,
        "min_mlb_fraction_remaining": 0.15,
        "volatility_stop_enabled": True,
        "stop_confirmation_readings": 3,
        "stop_grace_minutes": 2.5,
        "catastrophic_stop_multiplier": 1.75,
        "post_exit_tracking_minutes": 30.0,
    },
    "aggressive": {
        "label": "Aggressive research",
        "description": "Broadest bounded profile; still keeps every venue and allocation safeguard.",
        "exposure_fraction": 0.95,
        "position_fraction": 0.25,
        "event_fraction": 0.50,
        "daily_loss_fraction": 0.45,
        "minimum_cash_reserve_fraction": 0.05,
        "max_open_positions": 10,
        "max_orders_per_hour": 12,
        "min_edge": 0.015,
        "max_edge": 0.40,
        "min_signal_quality": 45.0,
        "min_reference_sources": 1,
        "min_entry_price": 0.08,
        "max_entry_price": 0.92,
        "max_spread": 0.06,
        "min_book_shares": 1.0,
        "min_hold_minutes": 5.0,
        "profit_target": 0.08,
        "trailing_drawdown": 0.06,
        "stop_loss": 0.28,
        "exit_edge": -0.01,
        "cycle_seconds": 15,
        "candidate_cooldown_seconds": 120,
        "max_entries_per_event_per_hour": 6,
        "min_mlb_fraction_remaining": 0.0,
        "volatility_stop_enabled": True,
        "stop_confirmation_readings": 3,
        "stop_grace_minutes": 3.0,
        "catastrophic_stop_multiplier": 1.75,
        "post_exit_tracking_minutes": 30.0,
    },
}
# Compatibility aliases keep API/test imports stable while every action shares
# one short, case-insensitive operator approval token.
ARM_PHRASE = APPROVAL_TOKEN
LIVE_LIQUIDATION_PHRASE = APPROVAL_TOKEN
LIVE_POSITION_EXIT_PHRASE = APPROVAL_TOKEN
DRY_RUN_HISTORY_CLEAR_PHRASE = APPROVAL_TOKEN
LIVE_PERFORMANCE_RESET_PHRASE = APPROVAL_TOKEN
RISK_SESSION_RESET_PHRASE = APPROVAL_TOKEN
POLICY_ADVICE_APPLY_PHRASE = APPROVAL_TOKEN
# The dashboard offers bounded presets up to four hours. Restarting, saving a
# policy, disarming, or stopping still closes the process-local live latch.
DEFAULT_ARM_SECONDS = 30 * 60
MAX_ARM_SECONDS = 4 * 60 * 60
VENUE_RECONCILIATION_GRACE_SECONDS = 15
VENUE_MISMATCH_CONFIRMATIONS = 2
PROFIT_TARGET_CONFIRMATION_READINGS = 2
ENGINE_GATE_CATALOG = (
    {
        "code": "provider_freshness",
        "label": "Fresh provider data",
        "description": "Require trusted, non-future provider time inside the engine age limit.",
        "core": True,
    },
    {
        "code": "reference_source_support",
        "label": "Reference-source support",
        "description": "Require the engine's independent source-family check to pass.",
        "core": True,
    },
    {
        "code": "market_identity",
        "label": "Exact market identity",
        "description": "Require unambiguous event, market, line, scope, and outcome identity.",
        "core": True,
    },
    {
        "code": "market_status",
        "label": "Open market status",
        "description": "Require an active, unresolved, unrestricted market accepting orders.",
        "core": True,
    },
    {
        "code": "entry_price_range",
        "label": "Engine entry-price bracket",
        "description": "Require the engine price to remain inside its 5c-95c entry bracket.",
        "core": True,
    },
    {
        "code": "executable_fill",
        "label": "Engine executable-fill proof",
        "description": "Require complete engine-side depth, fee metadata, and a simulated fill.",
        "core": False,
    },
    {
        "code": "consensus_policy",
        "label": "Versioned consensus policy",
        "description": "Require support for the selected consensus method.",
        "core": False,
    },
    {
        "code": "model_sample_support",
        "label": "Model sample support",
        "description": "Require the chronological model-selection sample minimum.",
        "core": False,
    },
    {
        "code": "calibration_support",
        "label": "Calibration artifact",
        "description": "Require a validated, versioned calibration policy.",
        "core": False,
    },
    {
        "code": "uncertainty_support",
        "label": "Uncertainty bootstrap",
        "description": "Require enough event-block bootstrap draws.",
        "core": False,
    },
    {
        "code": "probability_net_ev_positive",
        "label": "Probability net EV is positive",
        "description": "Require the historical bootstrap probability floor.",
        "core": False,
    },
    {
        "code": "minimum_expected_value",
        "label": "Minimum expected dollars",
        "description": "Require the engine's expected paper-dollar value floor.",
        "core": False,
    },
    {
        "code": "net_edge",
        "label": "Engine net-edge floor",
        "description": "Require calibrated probability minus executable cost to clear its floor.",
        "core": True,
    },
    {
        "code": "signal_quality",
        "label": "Engine signal-quality floor",
        "description": "Require the engine's reliability summary to clear its threshold.",
        "core": True,
    },
)
ENGINE_GATE_CODES = frozenset(item["code"] for item in ENGINE_GATE_CATALOG)
CORE_ENGINE_GATES = tuple(
    item["code"] for item in ENGINE_GATE_CATALOG if item["core"]
)
_TERMINAL_ORDER_STATES = {
    "ORDER_STATE_FILLED",
    "ORDER_STATE_CANCELED",
    "ORDER_STATE_REJECTED",
    "ORDER_STATE_EXPIRED",
    "ORDER_STATE_REPLACED",
    "filled",
    "unfilled",
    "cancel_requested",
}
_MONEYLINE_MARKETS = {
    "moneyline",
    "h2h",
    "winner",
    "match_winner",
    "drawable_outcome",
}
SUPPORTED_ENTRY_MARKET_TYPES = ("moneyline", "spread", "total")
SUPPORTED_ENTRY_MARKET_TYPE_SET = frozenset(SUPPORTED_ENTRY_MARKET_TYPES)
_WORD = re.compile(r"[a-z0-9]+")
_SIGNED_LINE = re.compile(r"(?<!\d)([+-]\d+(?:\.\d+)?)(?!\d)")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_trading_config (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    payload TEXT NOT NULL,
    updated_ts DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS live_trading_journal (
    id TEXT PRIMARY KEY,
    created_ts DOUBLE PRECISION NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    event_id TEXT,
    event_name TEXT,
    market_slug TEXT,
    selection TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_trading_journal_created
    ON live_trading_journal(created_ts DESC);
CREATE TABLE IF NOT EXISTS live_managed_positions (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    market_type TEXT NOT NULL,
    selection TEXT NOT NULL,
    position_side TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    entry_cost DOUBLE PRECISION NOT NULL,
    entry_long_price DOUBLE PRECISION NOT NULL,
    cost_basis DOUBLE PRECISION NOT NULL,
    opened_ts DOUBLE PRECISION NOT NULL,
    updated_ts DOUBLE PRECISION NOT NULL,
    highest_exit_value DOUBLE PRECISION NOT NULL,
    current_exit_value DOUBLE PRECISION,
    current_model_probability DOUBLE PRECISION,
    current_execution_edge DOUBLE PRECISION,
    return_fraction DOUBLE PRECISION,
    entry_decision_id TEXT,
    entry_order_id TEXT,
    exit_order_id TEXT,
    exit_reason TEXT,
    closed_ts DOUBLE PRECISION,
    realized_pnl DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_live_managed_positions_status
    ON live_managed_positions(status, opened_ts);
CREATE TABLE IF NOT EXISTS live_managed_orders (
    order_id TEXT PRIMARY KEY,
    market_slug TEXT NOT NULL,
    position_id TEXT,
    purpose TEXT NOT NULL,
    state TEXT NOT NULL,
    created_ts DOUBLE PRECISION NOT NULL,
    updated_ts DOUBLE PRECISION NOT NULL
);
"""

_POLICY_ADVISOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS trading_policy_sessions (
    id TEXT PRIMARY KEY,
    started_ts DOUBLE PRECISION NOT NULL,
    ended_ts DOUBLE PRECISION,
    mode TEXT NOT NULL,
    reason TEXT NOT NULL,
    policy_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_policy_sessions_started
    ON trading_policy_sessions(started_ts DESC);

CREATE TABLE IF NOT EXISTS trading_policy_advice (
    id TEXT PRIMARY KEY,
    created_ts DOUBLE PRECISION NOT NULL,
    session_id TEXT,
    objective TEXT NOT NULL,
    target_trades_per_hour DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL,
    suggested_policy_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    model_evidence_json TEXT NOT NULL,
    applied_ts DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_policy_advice_created
    ON trading_policy_advice(created_ts DESC);
"""

RESEARCH_EVIDENCE_TABLES = frozenset({
    "live_trading_journal",
    "live_managed_positions",
    "trading_policy_sessions",
    "trading_policy_advice",
})


class TradingPolicyError(ValueError):
    """A user-correctable execution-policy error."""


class TradingExecutionError(RuntimeError):
    """A bounded venue or execution failure."""


def risk_preset_fields(name: str, allocation: float) -> dict[str, Any]:
    """Return deterministic execution limits for one operator-selected risk style."""
    if name not in RISK_PRESETS:
        raise TradingPolicyError(f"unknown risk preset: {name}")
    if not math.isfinite(allocation) or allocation <= 0:
        raise TradingPolicyError("trading_allocation_usd must be greater than zero")
    preset = RISK_PRESETS[name]
    def money(fraction: float) -> float:
        return round(max(0.01, allocation * fraction), 2)

    return {
        "risk_preset": name,
        "risk_preset_version": RISK_PRESET_VERSION,
        "trading_allocation_usd": round(allocation, 2),
        "max_total_exposure_usd": money(preset["exposure_fraction"]),
        "minimum_cash_reserve_usd": round(
            allocation * preset["minimum_cash_reserve_fraction"], 2
        ),
        "max_position_usd": money(preset["position_fraction"]),
        "max_event_exposure_usd": money(preset["event_fraction"]),
        "max_daily_loss_usd": money(preset["daily_loss_fraction"]),
        **{
            key: value
            for key, value in preset.items()
            if key
            not in {
                "label",
                "description",
                "exposure_fraction",
                "position_fraction",
                "event_fraction",
                "daily_loss_fraction",
                "minimum_cash_reserve_fraction",
            }
        },
    }


@dataclass(frozen=True, slots=True)
class TradingPolicy:
    automation_enabled: bool = False
    execution_mode: str = "dry_run"
    auto_cashout: bool = False
    adaptive_exit_enabled: bool = False
    adaptive_exit_profile: str = "observe"
    adaptive_exit_horizon_minutes: float = 3.0
    adaptive_exit_min_samples: int = 30
    adaptive_exit_max_tightening: float = 0.35
    volatility_stop_enabled: bool = False
    stop_confirmation_readings: int = 3
    stop_grace_minutes: float = 2.0
    catastrophic_stop_multiplier: float = 1.75
    post_exit_tracking_minutes: float = 30.0
    require_engine_entry: bool = True
    required_engine_gates: tuple[str, ...] = CORE_ENGINE_GATES
    allowed_market_types: tuple[str, ...] = SUPPORTED_ENTRY_MARKET_TYPES
    trading_allocation_usd: float = 10.0
    risk_preset: str = "custom"
    risk_preset_version: str = RISK_PRESET_VERSION
    max_total_exposure_usd: float = 9.50
    minimum_cash_reserve_usd: float = 0.50
    max_position_usd: float = 1.75
    max_event_exposure_usd: float = 3.0
    max_daily_loss_usd: float = 5.0
    max_open_positions: int = 6
    max_orders_per_hour: int = 6
    min_edge: float = 0.03
    max_edge: float = 1.0
    min_signal_quality: float = 60.0
    min_reference_sources: int = 2
    min_entry_price: float = 0.10
    max_entry_price: float = 0.90
    max_spread: float = 0.04
    min_book_shares: float = 3.0
    min_hold_minutes: float = 10.0
    profit_target: float = 0.10
    trailing_drawdown: float = 0.04
    stop_loss: float = 0.20
    exit_edge: float = 0.0
    cycle_seconds: int = 30
    candidate_cooldown_seconds: int = 300
    max_entries_per_event_per_hour: int = 3
    min_mlb_fraction_remaining: float = 0.0

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "TradingPolicy":
        known = {field.name for field in fields(cls)}
        clean = {key: value for key, value in values.items() if key in known}
        # Policies saved before the allocation field existed used total exposure
        # as their outer boundary. Preserve that exact configuration on upgrade
        # by adopting at least that amount as the initial hard allocation.
        if "trading_allocation_usd" not in clean:
            clean["trading_allocation_usd"] = max(
                10.0, float(clean.get("max_total_exposure_usd", 0.0) or 0.0)
            )
        if "required_engine_gates" in clean:
            raw_gates = clean["required_engine_gates"]
            if not isinstance(raw_gates, (list, tuple)):
                raise TradingPolicyError("required_engine_gates must be a list")
            clean["required_engine_gates"] = tuple(
                dict.fromkeys(str(code) for code in raw_gates)
            )
        if "allowed_market_types" in clean:
            raw_market_types = clean["allowed_market_types"]
            if not isinstance(raw_market_types, (list, tuple)):
                raise TradingPolicyError("allowed_market_types must be a list")
            clean["allowed_market_types"] = tuple(
                dict.fromkeys(
                    str(market_type).strip().casefold()
                    for market_type in raw_market_types
                )
            )
        policy = cls(**clean)
        policy.validate()
        return policy

    def validate(self) -> None:
        if self.execution_mode not in {"dry_run", "live"}:
            raise TradingPolicyError("execution_mode must be dry_run or live")
        money_fields = (
            "trading_allocation_usd",
            "max_total_exposure_usd",
            "minimum_cash_reserve_usd",
            "max_position_usd",
            "max_event_exposure_usd",
            "max_daily_loss_usd",
        )
        for name in money_fields:
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise TradingPolicyError(f"{name} must be a non-negative number")
        if self.trading_allocation_usd <= 0:
            raise TradingPolicyError("trading_allocation_usd must be greater than zero")
        if self.max_total_exposure_usd <= 0:
            raise TradingPolicyError("max_total_exposure_usd must be greater than zero")
        if self.max_total_exposure_usd > self.trading_allocation_usd:
            raise TradingPolicyError(
                "maximum total exposure cannot exceed the hard trading allocation"
            )
        if self.risk_preset not in {"custom", *RISK_PRESETS}:
            raise TradingPolicyError("risk_preset must be custom or a named preset")
        if self.adaptive_exit_profile not in ADAPTIVE_EXIT_PROFILES:
            raise TradingPolicyError(
                "adaptive_exit_profile must be observe, guarded, balanced, or responsive"
            )
        if not 0.5 <= self.adaptive_exit_horizon_minutes <= 15:
            raise TradingPolicyError(
                "adaptive_exit_horizon_minutes must be between 0.5 and 15"
            )
        if not 5 <= self.adaptive_exit_min_samples <= 10000:
            raise TradingPolicyError(
                "adaptive_exit_min_samples must be between 5 and 10000"
            )
        if not 0 <= self.adaptive_exit_max_tightening <= 0.60:
            raise TradingPolicyError(
                "adaptive_exit_max_tightening must be between 0 and 0.60"
            )
        if not 2 <= self.stop_confirmation_readings <= 10:
            raise TradingPolicyError(
                "stop_confirmation_readings must be between 2 and 10"
            )
        if not 0.5 <= self.stop_grace_minutes <= 15:
            raise TradingPolicyError(
                "stop_grace_minutes must be between 0.5 and 15"
            )
        if not 1.10 <= self.catastrophic_stop_multiplier <= 3.0:
            raise TradingPolicyError(
                "catastrophic_stop_multiplier must be between 1.10 and 3.0"
            )
        if not 5 <= self.post_exit_tracking_minutes <= 180:
            raise TradingPolicyError(
                "post_exit_tracking_minutes must be between 5 and 180"
            )
        if not 0 < self.max_position_usd <= self.max_total_exposure_usd:
            raise TradingPolicyError(
                "max_position_usd must be positive and no larger than total exposure"
            )
        if not 0 < self.max_event_exposure_usd <= self.max_total_exposure_usd:
            raise TradingPolicyError(
                "max_event_exposure_usd must be positive and no larger than total exposure"
            )
        if self.max_open_positions < 1 or self.max_orders_per_hour < 1:
            raise TradingPolicyError("position and order limits must be at least one")
        if not 1 <= self.max_entries_per_event_per_hour <= 50:
            raise TradingPolicyError(
                "max_entries_per_event_per_hour must be between 1 and 50"
            )
        if self.min_reference_sources < 1:
            raise TradingPolicyError("min_reference_sources must be at least one")
        unknown_gates = set(self.required_engine_gates) - ENGINE_GATE_CODES
        if unknown_gates:
            raise TradingPolicyError(
                "unknown required engine gate(s): "
                + ", ".join(sorted(unknown_gates))
            )
        unknown_market_types = (
            set(self.allowed_market_types) - SUPPORTED_ENTRY_MARKET_TYPE_SET
        )
        if unknown_market_types:
            raise TradingPolicyError(
                "unknown automatic-entry line type(s): "
                + ", ".join(sorted(unknown_market_types))
            )
        if not self.allowed_market_types:
            raise TradingPolicyError(
                "select at least one automatic-entry line type"
            )
        if not MIN_ENTRY_PRICE < self.min_entry_price < self.max_entry_price < MAX_ENTRY_PRICE:
            raise TradingPolicyError(
                "entry prices must stay strictly inside the established 5c–95c bounds"
            )
        if not 0 <= self.min_edge < self.max_edge <= 1:
            raise TradingPolicyError(
                "edge filters must satisfy 0 <= minimum edge < maximum edge <= 1"
            )
        if not 0 <= self.min_mlb_fraction_remaining <= 1:
            raise TradingPolicyError(
                "min_mlb_fraction_remaining must be between zero and one"
            )
        if not 0 <= self.min_signal_quality <= 100:
            raise TradingPolicyError("min_signal_quality must be between 0 and 100")
        if not 0 < self.max_spread < 1:
            raise TradingPolicyError("max_spread must be between zero and one")
        if self.min_book_shares <= 0 or self.min_hold_minutes < 0:
            raise TradingPolicyError("book shares must be positive and hold time non-negative")
        for name in ("profit_target", "trailing_drawdown", "stop_loss"):
            value = getattr(self, name)
            if not 0 < value < 1:
                raise TradingPolicyError(f"{name} must be between zero and one")
        if not -1 < self.exit_edge < 1:
            raise TradingPolicyError("exit_edge must be between -1 and 1")
        if not 10 <= self.cycle_seconds <= 300:
            raise TradingPolicyError("cycle_seconds must be between 10 and 300")
        if self.candidate_cooldown_seconds < self.cycle_seconds:
            raise TradingPolicyError(
                "candidate_cooldown_seconds cannot be shorter than the cycle"
            )


def _policy_fingerprint(policy: TradingPolicy | Mapping[str, Any]) -> str:
    """Hash the complete saved execution policy for stale-advice protection."""
    payload = asdict(policy) if isinstance(policy, TradingPolicy) else dict(policy)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class MappedCandidate:
    event: Event
    signal: Signal
    us_event: dict[str, Any]
    market: dict[str, Any]
    position_side: str
    selection: str
    entry_cost: float
    order_long_price: float
    exit_value: float | None
    spread: float
    book_shares: float
    execution_edge: float
    mapping_score: float
    game_fraction_remaining: float | None = None
    event_entries_60m: int = 1

    @property
    def key(self) -> str:
        return f"{self.event.id}:{self.market['slug']}:{self.position_side}"


@dataclass(frozen=True, slots=True)
class ExecutableBookQuote:
    """One authenticated, top-of-book execution snapshot."""

    state: str
    best_bid: float | None
    best_ask: float | None
    entry_cost: float | None
    order_long_price: float | None
    exit_value: float | None
    spread: float | None
    depth: float


def _now() -> float:
    return time.time()


def _amount(value: Any) -> float | None:
    if isinstance(value, Mapping):
        value = value.get("value")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _words(value: Any) -> str:
    return " ".join(_WORD.findall(str(value or "").casefold()))


def _similarity(left: Any, right: Any) -> float:
    a, b = _words(left), _words(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _parse_timestamp(value: Any) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _market_kind(value: Any) -> str:
    key = str(value or "").strip().casefold()
    key = key.removeprefix("sports_market_type_")
    if key in _MONEYLINE_MARKETS:
        return "moneyline"
    # The US API's specific sportsMarketType values retain sport and scope.
    # Only full-game/full-time variants are equivalent to the engine's base
    # moneyline/spread/total markets; first/second-half markets must not match.
    if key.endswith(("_team_full_time_winner", "_team_full_game_winner")):
        return "moneyline"
    if key.endswith("_fight_winner"):
        return "moneyline"
    if key.endswith("_team_full_game_spread"):
        return "spread"
    if key.endswith("_team_full_game_total"):
        return "total"
    if is_spread_market(key):
        return "spread"
    if is_total_market(key):
        return "total"
    return key


def _signal_probability(signal: Signal) -> float | None:
    value = (
        signal.calibrated_consensus_probability
        if signal.calibrated_consensus_probability is not None
        else signal.model_probability
    )
    return value if math.isfinite(value) and 0 < value < 1 else None


def _baseball_fraction_remaining(
    event: Event,
    state: GameState | None,
) -> float | None:
    """Return explicit regulation-game progress without inventing missing state."""
    identity = f"{event.sport} {event.league}".casefold()
    if "baseball" not in identity and "mlb" not in identity:
        return None
    if state is None:
        return None
    structured = state.sport_state if isinstance(state.sport_state, dict) else {}
    inning = _amount(structured.get("inning"))
    half = str(structured.get("half") or "").strip().casefold()
    if inning is None:
        text = f"{state.period} {state.clock}".strip()
        match = re.search(
            r"\b(top|bottom|bot)\s*(?:of\s*(?:the\s*)?)?"
            r"(\d{1,2})(?:st|nd|rd|th)?\b",
            text,
            re.IGNORECASE,
        )
        if match:
            half, inning_text = match.groups()
            half = half.casefold()
            inning = float(inning_text)
        else:
            compact = re.search(r"\b([tb])\s*(\d{1,2})\b", text, re.IGNORECASE)
            if compact:
                half, inning_text = compact.groups()
                half = half.casefold()
                inning = float(inning_text)
    if inning is None or int(inning) < 1:
        return None
    normalized_half = (
        "bottom" if half in {"bottom", "bot", "b"} else
        "top" if half in {"top", "t"} else ""
    )
    if not normalized_half:
        return None
    completed_halves = (int(inning) - 1) * 2 + (
        1 if normalized_half == "bottom" else 0
    )
    return max(0.0, min(1.0, (18 - completed_halves) / 18))


def _mlb_stop_context(
    event: Event | None,
    state: GameState | None,
    position: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return explicit MLB state used only by the bounded stop guard."""
    if event is None or state is None:
        return None
    fraction = _baseball_fraction_remaining(event, state)
    if fraction is None:
        return None
    terminal = bool(state.ended) or str(state.status or "").casefold() in {
        "final",
        "ended",
        "complete",
        "completed",
        "finished",
    }
    structured = state.sport_state if isinstance(state.sport_state, dict) else {}
    inning = _amount(structured.get("inning"))
    half = str(structured.get("half") or "").strip().casefold()
    if inning is None:
        text = f"{state.period} {state.clock}".strip()
        match = re.search(
            r"\b(top|bottom|bot)\s*(?:of\s*(?:the\s*)?)?"
            r"(\d{1,2})(?:st|nd|rd|th)?\b",
            text,
            re.IGNORECASE,
        )
        if match:
            half, inning_text = match.groups()
            inning = float(inning_text)
    market_type = _market_kind(position.get("market_type"))
    selection = str(position.get("selection") or "")
    normalized = _words(selection)
    line_values = re.findall(r"(?<!\d)([+-]?\d+(?:\.\d+)?)(?!\d)", selection)
    line = _amount(line_values[-1]) if line_values else None
    current_total = float(state.home_score) + float(state.away_score)
    structurally_lost = False
    settled_in_favor = False
    if market_type == "total" and line is not None:
        if normalized.startswith("under "):
            structurally_lost = current_total > line
        elif normalized.startswith("over "):
            settled_in_favor = current_total > line
    return {
        "inning": int(inning) if inning is not None else None,
        "half": half or None,
        "fraction_remaining": fraction,
        "state_received_ts": state.received_at.timestamp(),
        "terminal": terminal,
        "market_type": market_type,
        "selection": selection,
        "line": line,
        "current_total_runs": current_total,
        "structurally_lost": structurally_lost,
        "settled_in_favor": settled_in_favor,
        "outs": structured.get("outs"),
        "base_mask": structured.get("base_mask"),
    }


def _side_description(side: Mapping[str, Any]) -> str:
    return str(side.get("description") or side.get("identifier") or "").strip()


def _side_team(side: Mapping[str, Any]) -> str:
    return str(side.get("team_name") or "").strip()


def _side_line(side: Mapping[str, Any]) -> float | None:
    match = _SIGNED_LINE.search(_side_description(side))
    if not match:
        return None
    return _amount(match.group(1))


def _binary_moneyline_side(
    wanted: str,
    market: Mapping[str, Any],
    sides: list[dict[str, Any]],
) -> tuple[dict[str, Any], float] | None:
    """Match an outcome-specific Polymarket US Yes/No contract.

    Soccer full-time winner inventory is represented as three distinct binary
    contracts (home win, away win, draw), not one three-way market.  Team
    identity is carried on the long side's ``team_name``; draw identity is
    explicit in the market slug/question.  Requiring those exact markers keeps
    this fail-closed instead of guessing from generic Yes/No labels.
    """
    descriptions = {_words(_side_description(side)) for side in sides}
    if descriptions != {"yes", "no"}:
        return None
    long_sides = [side for side in sides if side.get("long")]
    if len(long_sides) != 1:
        return None
    long_side = long_sides[0]
    wanted_words = _words(wanted)
    if wanted_words == "draw":
        slug_words = _words(market.get("slug"))
        question_words = set(_words(market.get("question")).split())
        if "draw" in slug_words.split() or "draw" in question_words:
            return long_side, 1.0
        return None
    subject = _side_team(long_side)
    score = _similarity(wanted, subject)
    return (long_side, score) if subject and score >= 0.86 else None


def _event_match(event: Event, us_events: Iterable[Mapping[str, Any]]) -> tuple[dict, float] | None:
    wanted_start = _parse_timestamp(event.game_start)
    scored: list[tuple[float, dict]] = []
    for raw in us_events:
        if raw.get("ended"):
            continue
        title = str(raw.get("title") or "")
        direct = (_similarity(event.home, title) + _similarity(event.away, title)) / 2
        title_words = set(_words(title).split())
        home_words = set(_words(event.home).split())
        away_words = set(_words(event.away).split())
        coverage = (
            len(home_words & title_words) / max(1, len(home_words))
            + len(away_words & title_words) / max(1, len(away_words))
        ) / 2
        score = max(direct, coverage)
        us_start = _parse_timestamp(raw.get("start"))
        if wanted_start is not None and us_start is not None:
            delta = abs(wanted_start - us_start)
            if delta > 12 * 3600:
                continue
            score = 0.85 * score + 0.15 * max(0.0, 1.0 - delta / (12 * 3600))
        if score >= 0.72:
            scored.append((score, dict(raw)))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.06:
        return None
    return scored[0][1], scored[0][0]


def _selection_side(
    event: Event,
    signal: Signal,
    market: Mapping[str, Any],
) -> tuple[dict[str, Any], float] | None:
    sides = [
        dict(side) for side in market.get("sides", [])
        if isinstance(side, Mapping) and side.get("tradable", True)
    ]
    if len(sides) != 2 or sum(bool(side.get("long")) for side in sides) != 1:
        return None
    kind = _market_kind(signal.market)
    market_kind = _market_kind(market.get("market_type"))
    if kind != market_kind:
        return None
    wanted = str(signal.outcome or "").strip()
    if kind == "total":
        signal_line, signal_side = quote_line_side(
            signal.market, wanted, event.home, event.away
        )
        market_line = _amount(market.get("line"))
        if (
            signal_line is None
            or market_line is None
            or abs(signal_line - market_line) > 1e-6
            or signal_side not in {"over", "under"}
        ):
            return None
        matches = [
            side for side in sides
            if _words(_side_description(side)).startswith(signal_side)
        ]
        return (matches[0], 1.0) if len(matches) == 1 else None
    if kind == "spread":
        signal_line, signal_side = quote_line_side(
            signal.market, wanted, event.home, event.away
        )
        if signal_line is None or signal_side not in {"home", "away"}:
            return None
        wanted_team = event.home if signal_side == "home" else event.away
        matches = []
        for side in sides:
            side_line = _side_line(side)
            team_score = _similarity(wanted_team, _side_team(side))
            if (
                side_line is not None
                and abs(side_line - signal_line) <= 1e-6
                and team_score >= 0.86
            ):
                matches.append((team_score, side))
        return (matches[0][1], matches[0][0]) if len(matches) == 1 else None
    if kind == "moneyline":
        binary = _binary_moneyline_side(wanted, market, sides)
        if binary is not None:
            return binary
        aliases = {
            "home": event.home,
            "away": event.away,
        }
        wanted = aliases.get(_words(wanted), wanted)
        scored = sorted(
            [(_similarity(wanted, _side_description(side)), side) for side in sides],
            key=lambda item: item[0],
            reverse=True,
        )
        if not scored or scored[0][0] < 0.76:
            return None
        if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.12:
            return None
        return scored[0][1], scored[0][0]
    return None


def _book_prices(
    market: Mapping[str, Any],
    side: Mapping[str, Any],
) -> tuple[float, float, float | None, float] | None:
    long_bid = _amount(market.get("long_best_bid"))
    long_ask = _amount(market.get("long_best_ask"))
    if long_bid is None or long_ask is None or not 0 < long_bid <= long_ask < 1:
        return None
    if side.get("long"):
        return long_ask, long_ask, long_bid, long_ask - long_bid
    # The order API always expresses price in LONG/YES terms.  Buying SHORT/NO
    # at its ask therefore uses the current LONG bid; selling it uses LONG ask.
    return 1.0 - long_bid, long_bid, 1.0 - long_ask, long_ask - long_bid


def _book_market_data(book: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Unwrap the official SDK's ``{"marketData": ...}`` response."""
    if not isinstance(book, Mapping):
        return {}
    nested = book.get("marketData")
    return nested if isinstance(nested, Mapping) else book


def _book_levels(
    book: Mapping[str, Any],
    key: str,
) -> list[tuple[float, float]]:
    raw = book.get(key)
    if not isinstance(raw, list):
        return []
    levels: list[tuple[float, float]] = []
    for level in raw:
        if not isinstance(level, Mapping):
            continue
        price = _amount(level.get("px"))
        quantity = _amount(level.get("qty"))
        if (
            price is None
            or quantity is None
            or not 0 < price < 1
            or quantity <= 0
        ):
            continue
        levels.append((price, quantity))
    return levels


def _executable_book_quote(
    book: Mapping[str, Any] | None,
    position_side: str,
) -> ExecutableBookQuote:
    """Read the current executable price and matching top-level depth.

    Polymarket US expresses both sides in LONG/YES-price terms. Buying SHORT
    therefore executes against the highest LONG bid.
    """
    data = _book_market_data(book)
    state = str(data.get("state") or "").upper()
    bids = _book_levels(data, "bids")
    offers = _book_levels(data, "offers")
    best_bid = max((price for price, _ in bids), default=None)
    best_ask = min((price for price, _ in offers), default=None)
    if (
        best_bid is None
        or best_ask is None
        or best_bid > best_ask
    ):
        return ExecutableBookQuote(
            state=state,
            best_bid=best_bid,
            best_ask=best_ask,
            entry_cost=None,
            order_long_price=None,
            exit_value=None,
            spread=None,
            depth=0.0,
        )
    spread = best_ask - best_bid
    if position_side == "long":
        entry_cost = best_ask
        order_long_price = best_ask
        exit_value = best_bid
        depth = sum(
            quantity
            for price, quantity in offers
            if abs(price - best_ask) <= 1e-8
        )
    else:
        entry_cost = 1.0 - best_bid
        order_long_price = best_bid
        exit_value = 1.0 - best_ask
        depth = sum(
            quantity
            for price, quantity in bids
            if abs(price - best_bid) <= 1e-8
        )
    return ExecutableBookQuote(
        state=state,
        best_bid=best_bid,
        best_ask=best_ask,
        entry_cost=entry_cost,
        order_long_price=order_long_price,
        exit_value=exit_value,
        spread=spread,
        depth=depth,
    )


def _size_aware_exit_quote(
    book: Mapping[str, Any] | None,
    position_side: str,
    quantity: float,
) -> tuple[str, float, float, float] | None:
    """Return state, average exit value, LONG limit price, and total depth.

    The limit price is the worst level required to fill the complete managed
    quantity. The average value is used for cash-out monitoring, avoiding a
    misleading top-of-book mark that cannot fill the position.
    """
    if quantity <= 0:
        return None
    data = _book_market_data(book)
    state = str(data.get("state") or "").upper()
    if position_side == "long":
        levels = sorted(_book_levels(data, "bids"), reverse=True)
    else:
        levels = sorted(_book_levels(data, "offers"))
    total_depth = sum(level_quantity for _, level_quantity in levels)
    remaining = quantity
    notional = 0.0
    limit_price: float | None = None
    for price, level_quantity in levels:
        taken = min(remaining, level_quantity)
        if taken <= 0:
            continue
        exit_value = price if position_side == "long" else 1.0 - price
        notional += taken * exit_value
        remaining -= taken
        limit_price = price
        if remaining <= 1e-8:
            break
    if remaining > 1e-8 or limit_price is None:
        return None
    return state, notional / quantity, limit_price, total_depth


def _price_amount(value: float) -> dict[str, Any]:
    # Polymarket US defines Amount.value as a string.  Sending a JSON number is
    # accepted by some read/preview paths but rejected by live order handling.
    encoded = f"{round(value, 8):.8f}".rstrip("0").rstrip(".")
    return {"value": encoded or "0", "currency": "USD"}


def _order_fill(
    response: Mapping[str, Any],
    fallback_price: float,
) -> tuple[float, float, float]:
    shares = 0.0
    notional = 0.0
    fees = 0.0
    for execution in response.get("executions", []) or []:
        if not isinstance(execution, Mapping):
            continue
        try:
            quantity = float(execution.get("lastShares") or 0.0)
        except (TypeError, ValueError):
            continue
        price = _amount(execution.get("lastPx"))
        if quantity > 0 and price is not None:
            shares += quantity
            notional += quantity * price
            fee = _amount(execution.get("commissionNotionalCollected"))
            fees += max(0.0, fee or 0.0)
    return shares, (notional / shares if shares > 0 else fallback_price), fees


def _preview_fee_per_share(
    preview: Mapping[str, Any],
    *,
    quantity: float,
    entry_cost: float,
) -> float | None:
    order = preview.get("order") if isinstance(preview, Mapping) else None
    if not isinstance(order, Mapping) or quantity <= 0:
        return None
    total = _amount(order.get("commissionNotionalTotalCollected"))
    if total is not None and total >= 0:
        return total / quantity
    try:
        basis_points = float(order.get("commissionsBasisPoints"))
    except (TypeError, ValueError):
        return None
    return max(0.0, entry_cost * basis_points / 10_000.0)


class PolymarketUSAutoTrader:
    """Persistent journal plus a process-local live execution latch."""

    def __init__(
        self,
        path: str | None,
        *,
        key_id: str,
        secret_key: str,
        client_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = _now,
    ):
        self._db = Database.open(
            path,
            sqlite_envs=("POLYMARKET_US_TRADING_DB",),
            sqlite_default=path or "polymarket-us-trading.db",
        )
        self.path = self._db.target
        self._lock = threading.RLock()
        self._cycle_lock = threading.Lock()
        self._key_id = key_id
        self._secret_key = secret_key
        self._credential_source = (
            "environment" if key_id and secret_key else "none"
        )
        self._client_factory = client_factory
        self._clock = clock
        self._armed_until = 0.0
        # Entry authority expires after the operator-selected bounded duration.
        # Protective cash-out
        # authority is separate: arming live trading enables it for the life of
        # this process, and save/disarm/stop/restart always clears it.
        self._protective_exits_armed = False
        # Incrementing this token invalidates a cycle that was already in
        # progress. It is deliberately independent of the cycle lock so an
        # operator stop/reset can take effect without waiting for venue I/O.
        self._control_generation = 0
        self._last_cycle_at: float | None = None
        self._last_cycle_summary = "Automation has not run yet."
        self._last_cycle_evaluations: tuple[dict[str, Any], ...] = ()
        self._last_venue_sync_at: float | None = None
        self._last_venue_sync_summary = "Venue positions have not been synchronized yet."
        self._last_venue_sync_error: str | None = None
        self._last_venue_positions: tuple[dict[str, Any], ...] = ()
        self._candidate_seen: dict[str, float] = {}
        self._qualification_seen: dict[str, float] = {}
        self._journal_writes = 0
        with self._lock:
            self._db.initialize(_SCHEMA, component="polymarket_us_live_trading", version=1)
            self._db.migrate_columns(
                "polymarket_us_live_trading",
                2,
                {
                    "live_managed_positions": {
                        "initial_quantity": "DOUBLE PRECISION",
                        "initial_cost_basis": "DOUBLE PRECISION",
                        "external_exit_quantity": "DOUBLE PRECISION",
                        "venue_sync_status": "TEXT",
                        "venue_net_position": "DOUBLE PRECISION",
                        "venue_qty_available": "DOUBLE PRECISION",
                        "venue_observed_quantity": "DOUBLE PRECISION",
                        "venue_mismatch_count": "INTEGER",
                        "venue_sync_ts": "DOUBLE PRECISION",
                        "venue_update_time": "TEXT",
                        "profit_lock_armed_ts": "DOUBLE PRECISION",
                        "profit_lock_price": "DOUBLE PRECISION",
                    },
                },
            )
            self._db.migrate_columns(
                "polymarket_us_live_trading",
                3,
                {
                    "live_managed_positions": {
                        "dashboard_hidden": "INTEGER",
                    },
                },
            )
            self._db.migrate_columns(
                "polymarket_us_live_trading",
                4,
                {
                    "live_managed_positions": {
                        "profit_target_observed_ts": "DOUBLE PRECISION",
                        "profit_target_observation_count": "INTEGER",
                        "profit_target_observed_price": "DOUBLE PRECISION",
                    },
                },
            )
            self._db.migrate_columns(
                "polymarket_us_live_trading",
                5,
                {
                    "live_managed_positions": {
                        "adaptive_exit_payload": "TEXT",
                    },
                },
            )
            self._db.migrate_columns(
                "polymarket_us_live_trading",
                6,
                {
                    "live_managed_positions": {
                        "policy_session_id": "TEXT",
                        "entry_policy_json": "TEXT",
                        "entry_signal_edge": "DOUBLE PRECISION",
                        "entry_signal_quality": "DOUBLE PRECISION",
                        "entry_reference_sources": "INTEGER",
                        "entry_execution_edge": "DOUBLE PRECISION",
                    },
                },
            )
            self._db.initialize(
                _POLICY_ADVISOR_SCHEMA,
                component="polymarket_us_live_trading",
                version=7,
            )
            self._db.migrate_columns(
                "polymarket_us_live_trading",
                8,
                {
                    "live_managed_positions": {
                        "entry_game_fraction_remaining": "DOUBLE PRECISION",
                        "entry_event_entries_60m": "INTEGER",
                    },
                },
            )
            self._db.migrate_columns(
                "polymarket_us_live_trading",
                9,
                {
                    "live_managed_positions": {
                        "stop_triggered_ts": "DOUBLE PRECISION",
                        "stop_observation_count": "INTEGER",
                        "stop_low_exit_value": "DOUBLE PRECISION",
                        "stop_guard_payload": "TEXT",
                    },
                },
            )
        self._adaptive_exit = AdaptiveExitModel(path, clock=clock)
        self._control_token = ""
        self._policy = self._load_policy()
        self._backfill_position_entry_context()

    def close(self) -> None:
        with self._lock:
            self._armed_until = 0.0
            self._protective_exits_armed = False
            self._adaptive_exit.close()
            self._db.close()

    def credential_status(self) -> dict[str, Any]:
        """Return a safe credential fingerprint and never either credential."""
        with self._lock:
            key_id = self._key_id
            configured = bool(key_id and self._secret_key)
            source = self._credential_source
        if not key_id:
            hint = None
        elif len(key_id) <= 8:
            hint = f"{key_id[:2]}...{key_id[-2:]}"
        else:
            hint = f"{key_id[:6]}...{key_id[-4:]}"
        return {
            "configured": configured,
            "key_id_hint": hint,
            "credential_source": source,
            "retention": (
                "process_memory_until_restart"
                if source == "runtime"
                else "server_environment"
                if source == "environment"
                else "none"
            ),
        }

    def _credential_pair_for_server(self) -> tuple[str, str]:
        """Internal account-read bridge; callers must never serialize this."""
        with self._lock:
            return self._key_id, self._secret_key

    def set_runtime_credentials(
        self,
        key_id: str,
        secret_key: str,
        *,
        source: str = "runtime",
    ) -> dict[str, Any]:
        """Replace credentials in memory after revoking execution authority.

        Credential changes never enter SQLite/PostgreSQL or the journal. Open
        live positions must remain attached to their original account, so the
        operator has to close/synchronize them before switching credentials.
        """
        key_id = str(key_id or "").strip()
        secret_key = str(secret_key or "").strip()
        if bool(key_id) != bool(secret_key):
            raise TradingPolicyError("both Polymarket US credential values are required")
        if len(key_id) > 1_000 or len(secret_key) > 8_000:
            raise TradingPolicyError("Polymarket US credential value is too long")
        if source not in {"runtime", "environment", "none"}:
            raise TradingPolicyError("invalid credential source")

        self._stop_automation_controls("credentials_changed")
        if not self._cycle_lock.acquire(timeout=5.0):
            raise TradingPolicyError(
                "Automation is stopped, but the prior cycle is still finishing; "
                "wait for the stop acknowledgement and try the key again."
            )
        try:
            with self._lock:
                with self._db.cursor() as cur:
                    self._db.execute(
                        cur,
                        """SELECT COUNT(*) FROM live_managed_positions
                           WHERE mode='live' AND status='open'""",
                    )
                    open_live = int(cur.fetchone()[0])
                if open_live:
                    raise TradingPolicyError(
                        "Cannot switch accounts while a live managed position is "
                        "open; synchronize or close it first."
                    )
                self._key_id = key_id
                self._secret_key = secret_key
                self._credential_source = source if key_id else "none"
                self._last_venue_positions = ()
                self._last_venue_sync_at = None
                self._last_venue_sync_error = None
                self._last_venue_sync_summary = (
                    "Credentials changed in process memory; refresh the account "
                    "and synchronize positions before arming."
                    if key_id
                    else "No Polymarket US credentials are active."
                )
        finally:
            self._cycle_lock.release()
        return self.credential_status()

    def iter_research_batches(
        self,
        *,
        batch_size: int = 500,
    ):
        """Yield retained evidence, excluding controls and every open position."""
        with self._lock:
            for table in sorted(RESEARCH_EVIDENCE_TABLES):
                for rows in self._db.iter_table_batches(
                    table, batch_size=batch_size
                ):
                    if table == "live_managed_positions":
                        rows = [
                            row for row in rows
                            if str(row.get("status") or "").casefold() == "closed"
                        ]
                    if rows:
                        yield table, rows
        yield from self._adaptive_exit.iter_research_batches(
            batch_size=batch_size
        )

    def merge_research_batch(
        self,
        table: str,
        rows: list[dict[str, Any]],
    ) -> int:
        if table in RESEARCH_EVIDENCE_TABLES:
            if table == "live_managed_positions":
                rows = [
                    row for row in rows
                    if str(row.get("status") or "").casefold() == "closed"
                ]
            with self._lock:
                return self._db.merge_table_rows(table, rows)
        return self._adaptive_exit.merge_research_batch(table, rows)

    @property
    def policy(self) -> TradingPolicy:
        self._refresh_policy_authority()
        return self._policy

    def _load_policy(self) -> TradingPolicy:
        policy, token = self._stored_policy_state()
        if policy is None:
            policy = TradingPolicy()
            token = self._save_policy(policy)
        elif not token:
            # Policies written before cross-process execution authority existed
            # receive a token on first load. This changes no policy value.
            token = self._save_policy(policy)
        self._control_token = token
        return policy

    def _stored_policy_state(self) -> tuple[TradingPolicy | None, str]:
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    "SELECT payload FROM live_trading_config WHERE singleton=%s",
                    (1,),
                )
                row = cur.fetchone()
        if row is None:
            return None, ""
        try:
            raw = json.loads(row[0])
            if not isinstance(raw, Mapping):
                raise TypeError("stored policy payload must be an object")
            return (
                TradingPolicy.from_mapping(raw),
                str(raw.get(_CONTROL_TOKEN_KEY) or ""),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TradingPolicyError("stored trading policy is invalid") from exc

    def _save_policy(self, policy: TradingPolicy) -> str:
        token = uuid4().hex
        payload = json.dumps(
            {**asdict(policy), _CONTROL_TOKEN_KEY: token},
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO live_trading_config(singleton,payload,updated_ts)
                       VALUES (%s,%s,%s) ON CONFLICT(singleton) DO UPDATE SET
                       payload=EXCLUDED.payload,updated_ts=EXCLUDED.updated_ts""",
                    (1, payload, self._clock()),
                )
        self._control_token = token
        return token

    def _adopt_stored_policy(
        self,
        policy: TradingPolicy,
        token: str,
    ) -> None:
        """Adopt a policy saved by another runtime and revoke local authority."""
        self._policy = policy
        self._control_token = token
        self._armed_until = 0.0
        self._protective_exits_armed = False
        self._control_generation += 1
        self._last_cycle_summary = (
            "A newer execution policy was saved by another local server. "
            "This runtime disarmed itself and adopted that saved policy."
        )

    def _refresh_policy_authority(self) -> bool:
        """Make the database policy authoritative across local server processes."""
        policy, token = self._stored_policy_state()
        if policy is None:
            return False
        if token != self._control_token:
            self._adopt_stored_policy(policy, token)
            return True
        return False

    def _execution_authority_is_current(
        self,
        generation: int,
        token: str,
        mode: str,
    ) -> bool:
        """Fail closed before any fill when another process changed controls."""
        if not self._cycle_is_current(generation):
            return False
        policy, stored_token = self._stored_policy_state()
        if policy is None:
            return False
        if (
            stored_token != token
            or not policy.automation_enabled
            or policy.execution_mode != mode
        ):
            if stored_token != self._control_token:
                self._adopt_stored_policy(policy, stored_token)
            return False
        return True

    def configure(self, values: Mapping[str, Any]) -> TradingPolicy:
        self._refresh_policy_authority()
        current = asdict(self._policy)
        current.update(values)
        selected_preset = str(current.get("risk_preset") or "custom")
        if selected_preset in RISK_PRESETS:
            allocation = float(current.get("trading_allocation_usd") or 0.0)
            current.update(risk_preset_fields(selected_preset, allocation))
        policy = TradingPolicy.from_mapping(current)
        self._save_policy(policy)
        self._policy = policy
        self._control_generation += 1
        # Any limit or mode edit closes the latch. The operator must review the
        # saved policy and explicitly re-arm it.
        self._armed_until = 0.0
        self._protective_exits_armed = False
        self._start_policy_session("execution_policy_saved")
        self._journal(
            "configuration",
            "saved",
            payload={"policy": asdict(policy), "live_disarmed": True},
        )
        return policy

    def _start_policy_session(self, reason: str) -> str:
        """Create an auditable settings boundary without touching performance."""
        session_id = str(uuid4())
        now = self._clock()
        policy_json = json.dumps(
            asdict(self._policy),
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """UPDATE trading_policy_sessions SET ended_ts=%s
                       WHERE ended_ts IS NULL""",
                    (now,),
                )
                self._db.execute(
                    cur,
                    """INSERT INTO trading_policy_sessions
                       (id,started_ts,ended_ts,mode,reason,policy_json)
                       VALUES (%s,%s,NULL,%s,%s,%s)""",
                    (
                        session_id,
                        now,
                        self._policy.execution_mode,
                        reason,
                        policy_json,
                    ),
                )
        return session_id

    def _current_policy_session(self) -> str:
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    """SELECT id FROM trading_policy_sessions
                       WHERE ended_ts IS NULL ORDER BY started_ts DESC LIMIT 1""",
                )
                row = cur.fetchone()
        return str(row[0]) if row is not None else self._start_policy_session(
            "first_managed_trade"
        )

    def _backfill_position_entry_context(self) -> None:
        """Recover signal fields for older positions from retained fill journals."""
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT id,entry_decision_id,market_slug
                       FROM live_managed_positions
                       WHERE entry_signal_edge IS NULL
                          OR entry_signal_quality IS NULL
                          OR entry_reference_sources IS NULL
                          OR entry_execution_edge IS NULL""",
                )
                positions = [dict(row) for row in cur.fetchall()]
                if not positions:
                    return
                self._db.execute(
                    cur,
                    """SELECT created_ts,market_slug,payload
                       FROM live_trading_journal
                       WHERE kind='entry'
                         AND status IN ('simulated_fill','live_fill')
                       ORDER BY created_ts DESC""",
                )
                journal_rows = [dict(row) for row in cur.fetchall()]
        by_decision: dict[tuple[str, str], dict[str, Any]] = {}
        by_market: dict[str, dict[str, Any]] = {}
        for row in journal_rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            market_slug = str(row.get("market_slug") or "")
            decision_id = str(payload.get("decision_id") or "")
            context = {
                "signal_edge": _amount(payload.get("signal_edge")),
                "signal_quality": _amount(payload.get("signal_quality")),
                "reference_sources": _amount(payload.get("reference_sources")),
                "execution_edge": _amount(payload.get("execution_edge")),
            }
            if decision_id:
                by_decision.setdefault((decision_id, market_slug), context)
            if market_slug:
                by_market.setdefault(market_slug, context)
        updates = []
        for position in positions:
            decision_id = str(position.get("entry_decision_id") or "")
            market_slug = str(position.get("market_slug") or "")
            context = by_decision.get((decision_id, market_slug))
            if context is None:
                context = by_market.get(market_slug)
            if context is None:
                continue
            updates.append((
                context["signal_edge"],
                context["signal_quality"],
                (
                    int(context["reference_sources"])
                    if context["reference_sources"] is not None else None
                ),
                context["execution_edge"],
                position["id"],
            ))
        if updates:
            with self._lock:
                with self._db.transaction() as cur:
                    self._db.execute_many(
                        cur,
                        self._db.sql(
                            """UPDATE live_managed_positions SET
                               entry_signal_edge=COALESCE(entry_signal_edge,%s),
                               entry_signal_quality=COALESCE(
                                   entry_signal_quality,%s
                               ),
                               entry_reference_sources=COALESCE(
                                   entry_reference_sources,%s
                               ),
                               entry_execution_edge=COALESCE(
                                   entry_execution_edge,%s
                               )
                               WHERE id=%s"""
                        ),
                        updates,
                    )

    def is_armed(self) -> bool:
        return self._clock() < self._armed_until

    def arm(
        self,
        confirmation: str,
        *,
        seconds: int = DEFAULT_ARM_SECONDS,
    ) -> dict[str, Any]:
        self._refresh_policy_authority()
        if not approval_granted(confirmation):
            raise TradingPolicyError(
                approval_instruction("arm live execution")
            )
        if not self._policy.automation_enabled:
            raise TradingPolicyError("enable automation before arming live execution")
        if self._policy.execution_mode != "live":
            raise TradingPolicyError("set execution mode to live before arming")
        if not self._key_id or not self._secret_key:
            raise TradingPolicyError("both Polymarket US API credential values are required")
        seconds = max(60, min(int(seconds), MAX_ARM_SECONDS))
        # Rotating the shared token makes this arming action the only current
        # authority. A second server with the same database remains disarmed.
        self._save_policy(self._policy)
        self._control_generation += 1
        self._armed_until = self._clock() + seconds
        self._protective_exits_armed = True
        self._journal(
            "safety",
            "live_armed",
            payload={
                "expires_at": self._armed_until,
                "seconds": seconds,
                "protective_exits_armed_until_stop_or_restart": True,
            },
        )
        return self.status()

    def disarm(self, reason: str = "manual") -> dict[str, Any]:
        self._refresh_policy_authority()
        self._armed_until = 0.0
        self._protective_exits_armed = False
        self._control_generation += 1
        self._save_policy(self._policy)
        self._journal("safety", "disarmed", payload={"reason": reason})
        return self.status()

    def _stop_automation_controls(self, reason: str) -> float:
        """Close every execution latch before doing any potentially slow cleanup."""
        self._refresh_policy_authority()
        stopped_at = self._clock()
        self._armed_until = 0.0
        self._protective_exits_armed = False
        self._control_generation += 1
        self._policy = TradingPolicy.from_mapping({
            **asdict(self._policy),
            "automation_enabled": False,
        })
        self._last_cycle_summary = (
            "Automation stop accepted; no new entries can start and any active "
            "analysis cycle has been invalidated."
        )
        self._save_policy(self._policy)
        return stopped_at

    def stop_automation(self, reason: str = "manual") -> dict[str, Any]:
        """Fast stop acknowledgement with no venue reads or order cancellation."""
        stopped_at = self._stop_automation_controls(reason)
        self._journal(
            "safety",
            "automation_stopped",
            payload={
                "reason": reason,
                "live_disarmed": True,
                "active_cycle_invalidated": True,
                "venue_cleanup_deferred": True,
            },
        )
        return {
            "policy": asdict(self._policy),
            "armed": False,
            "armed_until": None,
            "protective_exits_armed": False,
            "last_cycle_summary": self._last_cycle_summary,
            "stop_ack": {
                "accepted_at": datetime.fromtimestamp(
                    stopped_at, timezone.utc
                ).isoformat(),
                "automation_disabled": True,
                "live_disarmed": True,
                "active_cycle_invalidated": True,
                "venue_cleanup_deferred": True,
            },
        }

    def emergency_stop(self) -> dict[str, Any]:
        stopped_at = self._stop_automation_controls("emergency_stop")
        canceled: list[str] = []
        failures: list[dict[str, str]] = []
        managed = self._managed_open_orders()
        if self._key_id and self._secret_key and managed:
            try:
                with self._client() as client:
                    for row in managed:
                        try:
                            client.orders.cancel(
                                row["order_id"],
                                {"marketSlug": row["market_slug"]},
                            )
                            self._set_order_state(row["order_id"], "cancel_requested")
                            canceled.append(row["order_id"])
                        except Exception as exc:  # venue error is journaled, not hidden
                            failures.append({
                                "order_id": row["order_id"],
                                "error": self._safe_error(exc),
                            })
            except Exception as exc:
                failures.append({"order_id": "client", "error": self._safe_error(exc)})
        self._journal(
            "safety",
            "emergency_stop",
            payload={
                "controls_stopped_at": stopped_at,
                "cancel_requested": canceled,
                "failures": failures,
            },
        )
        return {
            **self.status(),
            "cancel_requested": canceled,
            "cancel_failures": failures,
        }

    def status(self) -> dict[str, Any]:
        self._refresh_policy_authority()
        positions = self.positions(open_only=True)
        exposure = sum(float(row["cost_basis"]) for row in positions)
        now = self._clock()
        risk_session = self._risk_limiter_snapshot()
        adaptive_exit = self._adaptive_exit.summary()
        adaptive_exit.update(
            enabled=self._policy.adaptive_exit_enabled,
            selected_profile=self._policy.adaptive_exit_profile,
            horizon_minutes=self._policy.adaptive_exit_horizon_minutes,
            minimum_samples=self._policy.adaptive_exit_min_samples,
            maximum_tightening=self._policy.adaptive_exit_max_tightening,
        )
        credential = self.credential_status()
        return {
            "policy_version": POLICY_VERSION,
            "policy": asdict(self._policy),
            "risk_preset_version": RISK_PRESET_VERSION,
            "risk_presets": {
                name: {
                    "label": values["label"],
                    "description": values["description"],
                    "derived_policy": risk_preset_fields(
                        name, self._policy.trading_allocation_usd
                    ),
                }
                for name, values in RISK_PRESETS.items()
            },
            "engine_gate_catalog": [dict(item) for item in ENGINE_GATE_CATALOG],
            "credentials_configured": credential["configured"],
            "credential_source": credential["credential_source"],
            "credential_retention": credential["retention"],
            "storage": {
                "backend": self._db.backend,
                "durable": self._db.backend == "postgres",
                "location": (
                    "DATABASE_URL PostgreSQL"
                    if self._db.backend == "postgres"
                    else "local SQLite file"
                ),
            },
            "armed": self.is_armed(),
            "armed_until": (
                datetime.fromtimestamp(self._armed_until, timezone.utc).isoformat()
                if self.is_armed()
                else None
            ),
            "restart_behavior": "always_disarmed",
            "protective_exits_armed": (
                self._protective_exits_armed
                and self._policy.automation_enabled
                and self._policy.execution_mode == "live"
                and self._policy.auto_cashout
            ),
            "protective_exit_behavior": "armed_until_save_disarm_stop_or_restart",
            "open_managed_positions": len(positions),
            "managed_exposure_usd": round(exposure, 2),
            "last_cycle_at": (
                datetime.fromtimestamp(self._last_cycle_at, timezone.utc).isoformat()
                if self._last_cycle_at
                else None
            ),
            "last_cycle_summary": self._last_cycle_summary,
            "last_venue_sync_at": (
                datetime.fromtimestamp(
                    self._last_venue_sync_at, timezone.utc
                ).isoformat()
                if self._last_venue_sync_at
                else None
            ),
            "last_venue_sync_summary": self._last_venue_sync_summary,
            "last_venue_sync_error": self._last_venue_sync_error,
            "venue_positions": [
                dict(item) for item in self._last_venue_positions
            ],
            # This is execution-layer metadata only. It lets the workstation
            # distinguish a research signal from an exactly mapped US contract
            # without writing anything back into the calculation engine.
            "last_cycle_evaluations": [
                dict(item) for item in self._last_cycle_evaluations
            ],
            "risk_session": risk_session,
            "adaptive_exit": adaptive_exit,
            "live_capable": self._policy.execution_mode == "live",
            "live_order_possible_now": (
                self._policy.automation_enabled
                and self._policy.execution_mode == "live"
                and self.is_armed()
            ),
            "server_time": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        }

    def journal(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT id,created_ts,kind,status,event_id,event_name,
                              market_slug,selection,payload
                       FROM live_trading_journal
                       ORDER BY created_ts DESC LIMIT %s""",
                    (limit,),
                )
                rows = cur.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["created_at"] = datetime.fromtimestamp(
                float(item.pop("created_ts")), timezone.utc
            ).isoformat()
            try:
                item["details"] = json.loads(item.pop("payload"))
            except (TypeError, json.JSONDecodeError):
                item["details"] = {}
            result.append(item)
        return result

    def positions(
        self,
        *,
        open_only: bool = False,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        if open_only:
            where = " WHERE status='open'"
        elif include_hidden:
            where = ""
        else:
            where = " WHERE COALESCE(dashboard_hidden,0)=0"
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    "SELECT * FROM live_managed_positions" + where
                    + " ORDER BY opened_ts DESC",
                )
                rows = [dict(row) for row in cur.fetchall()]
        for row in rows:
            adaptive_payload = row.pop("adaptive_exit_payload", None)
            stop_guard_payload = row.pop("stop_guard_payload", None)
            entry_policy_payload = row.pop("entry_policy_json", None)
            try:
                row["adaptive_exit"] = (
                    json.loads(adaptive_payload) if adaptive_payload else None
                )
            except (TypeError, json.JSONDecodeError):
                row["adaptive_exit"] = None
            try:
                row["stop_guard"] = (
                    json.loads(stop_guard_payload)
                    if stop_guard_payload else None
                )
            except (TypeError, json.JSONDecodeError):
                row["stop_guard"] = None
            try:
                row["entry_policy"] = (
                    json.loads(entry_policy_payload)
                    if entry_policy_payload else None
                )
            except (TypeError, json.JSONDecodeError):
                row["entry_policy"] = None
            row["initial_quantity"] = float(
                row.get("initial_quantity")
                if row.get("initial_quantity") is not None
                else row["quantity"]
            )
            row["initial_cost_basis"] = float(
                row.get("initial_cost_basis")
                if row.get("initial_cost_basis") is not None
                else row["cost_basis"]
            )
            row["external_exit_quantity"] = float(
                row.get("external_exit_quantity") or 0.0
            )
            row["venue_mismatch_count"] = int(
                row.get("venue_mismatch_count") or 0
            )
            row["profit_target_observation_count"] = int(
                row.get("profit_target_observation_count") or 0
            )
            row["stop_observation_count"] = int(
                row.get("stop_observation_count") or 0
            )
            for key in ("opened_ts", "updated_ts", "closed_ts"):
                value = row.get(key)
                row[key.removesuffix("_ts") + "_at"] = (
                    datetime.fromtimestamp(float(value), timezone.utc).isoformat()
                    if value is not None
                    else None
                )
            for key in (
                "venue_sync_ts",
                "profit_lock_armed_ts",
                "profit_target_observed_ts",
                "stop_triggered_ts",
            ):
                value = row.get(key)
                row[key.removesuffix("_ts") + "_at"] = (
                    datetime.fromtimestamp(float(value), timezone.utc).isoformat()
                    if value is not None
                    else None
                )
        return rows

    def clear_adaptive_exit_history(
        self,
        confirmation: str,
    ) -> dict[str, Any]:
        """Clear only the local movement learner, never positions or journals."""
        try:
            result = self._adaptive_exit.clear(confirmation)
        except ValueError as exc:
            raise TradingPolicyError(str(exc)) from exc
        self._journal(
            "adaptive_exit",
            "history_cleared",
            payload={
                "deleted_observations": result["deleted_observations"],
                "deleted_exit_recoveries": result[
                    "deleted_exit_recoveries"
                ],
                "positions_preserved": True,
                "journal_preserved": True,
            },
        )
        return result

    def archive_exited_positions(self) -> dict[str, Any]:
        """Hide exited cards while preserving performance and audit evidence."""
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """UPDATE live_managed_positions
                       SET dashboard_hidden=1
                       WHERE status!='open'
                         AND COALESCE(dashboard_hidden,0)=0""",
                )
                archived = max(0, int(cur.rowcount or 0))
        self._journal(
            "position_control",
            "exited_archived",
            payload={
                "archived_positions": archived,
                "positions_deleted": False,
                "performance_preserved": True,
                "execution_journal_preserved": True,
            },
        )
        return {
            "archived_positions": archived,
            "positions_deleted": False,
            "performance_preserved": True,
            "execution_journal_preserved": True,
            "summary": (
                f"Cleared {archived} exited position card"
                f"{'' if archived == 1 else 's'} from the managed-position view. "
                "Tallies and audit history were preserved."
            ),
        }

    def position(self, position_id: str) -> dict[str, Any] | None:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    "SELECT * FROM live_managed_positions WHERE id=%s",
                    (position_id,),
                )
                row = cur.fetchone()
        return dict(row) if row is not None else None

    def synchronize_live_positions(self) -> dict[str, Any]:
        """Read the authoritative venue portfolio and reconcile local live rows.

        This method never creates, cancels, modifies, previews, or closes an
        order. Two identical mismatch snapshots are required before local
        quantity is reduced, which avoids treating a transient portfolio lag as
        a phone-side sale.
        """
        if not self._cycle_lock.acquire(timeout=20):
            raise TradingPolicyError(
                "the current trading cycle did not finish in time; try sync again"
            )
        try:
            return self._reconcile_live_positions()
        finally:
            self._cycle_lock.release()

    @staticmethod
    def _venue_position_view(
        market_slug: str,
        position: Mapping[str, Any],
    ) -> dict[str, Any]:
        metadata = position.get("marketMetadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        return {
            "market_slug": market_slug,
            "title": str(metadata.get("title") or market_slug),
            "outcome": str(metadata.get("outcome") or ""),
            "net_position": _amount(position.get("netPosition")) or 0.0,
            "qty_available": _amount(position.get("qtyAvailable")),
            "cost_basis": _amount(position.get("cost")),
            "cash_value": _amount(position.get("cashValue")),
            "realized_pnl": _amount(position.get("realized")),
            "expired": bool(position.get("expired")),
            "update_time": str(position.get("updateTime") or "") or None,
        }

    @staticmethod
    def _read_venue_positions(client: Any) -> dict[str, dict[str, Any]]:
        gathered: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        for _ in range(20):
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = client.portfolio.positions(params)
            if not isinstance(page, Mapping):
                raise TradingExecutionError(
                    "Polymarket US returned an invalid portfolio response"
                )
            raw_positions = page.get("positions")
            if not isinstance(raw_positions, Mapping):
                raise TradingExecutionError(
                    "Polymarket US portfolio response has no positions map"
                )
            for slug, value in raw_positions.items():
                if isinstance(value, Mapping):
                    gathered[str(slug)] = dict(value)
            if bool(page.get("eof", True)):
                return gathered
            cursor = str(page.get("nextCursor") or "")
            if not cursor:
                raise TradingExecutionError(
                    "Polymarket US portfolio pagination ended without a cursor"
                )
        raise TradingExecutionError(
            "Polymarket US portfolio exceeded the bounded pagination limit"
        )

    def _set_venue_sync_observation(
        self,
        position_id: str,
        *,
        status: str,
        net_position: float,
        qty_available: float | None,
        observed_quantity: float,
        mismatch_count: int,
        update_time: str | None,
    ) -> None:
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """UPDATE live_managed_positions SET
                       venue_sync_status=%s,venue_net_position=%s,
                       venue_qty_available=%s,venue_observed_quantity=%s,
                       venue_mismatch_count=%s,venue_sync_ts=%s,
                       venue_update_time=%s,updated_ts=%s
                       WHERE id=%s AND mode='live' AND status='open'""",
                    (
                        status,
                        net_position,
                        qty_available,
                        observed_quantity,
                        mismatch_count,
                        self._clock(),
                        update_time,
                        self._clock(),
                        position_id,
                    ),
                )

    def _apply_external_quantity(
        self,
        position: Mapping[str, Any],
        *,
        observed_quantity: float,
        net_position: float,
        qty_available: float | None,
        update_time: str | None,
    ) -> str:
        current_quantity = float(position["quantity"])
        remaining = max(0.0, min(current_quantity, observed_quantity))
        externally_closed = max(0.0, current_quantity - remaining)
        now = self._clock()
        fully_closed = remaining <= 1e-8
        sync_status = (
            "externally_closed"
            if fully_closed
            else "partially_sold_externally"
        )
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """UPDATE live_managed_positions SET
                       initial_quantity=COALESCE(initial_quantity,quantity),
                       initial_cost_basis=COALESCE(initial_cost_basis,cost_basis),
                       external_exit_quantity=
                           COALESCE(external_exit_quantity,0)+%s,
                       quantity=%s,cost_basis=%s,status=%s,updated_ts=%s,
                       closed_ts=%s,exit_reason=%s,realized_pnl=NULL,
                       return_fraction=NULL,venue_sync_status=%s,
                       venue_net_position=%s,venue_qty_available=%s,
                       venue_observed_quantity=%s,venue_mismatch_count=0,
                       venue_sync_ts=%s,venue_update_time=%s
                       WHERE id=%s AND mode='live' AND status='open'""",
                    (
                        externally_closed,
                        remaining,
                        remaining * float(position["entry_cost"]),
                        "external_closed" if fully_closed else "open",
                        now,
                        now if fully_closed else None,
                        (
                            "external_phone_or_manual_close"
                            if fully_closed
                            else "external_phone_or_manual_partial_sale"
                        ),
                        sync_status,
                        net_position,
                        qty_available,
                        observed_quantity,
                        now,
                        update_time,
                        position["id"],
                    ),
                )
        self._journal(
            "venue_reconciliation",
            sync_status,
            event_id=position["event_id"],
            event_name=position["event_name"],
            market_slug=position["market_slug"],
            selection=position["selection"],
            payload={
                "position_id": position["id"],
                "previous_managed_quantity": current_quantity,
                "remaining_managed_quantity": remaining,
                "externally_closed_quantity": externally_closed,
                "venue_net_position": net_position,
                "realized_pnl_verified": False,
                "reason": (
                    "venue portfolio changed outside this workstation; local "
                    "quantity was reconciled without inventing an exit price"
                ),
            },
        )
        return sync_status

    def _reconcile_live_positions(self) -> dict[str, Any]:
        now = self._clock()
        if not self._key_id or not self._secret_key:
            raise TradingExecutionError(
                "Polymarket US credentials are required for venue synchronization"
            )
        try:
            with self._client() as client:
                venue_positions = self._read_venue_positions(client)
        except Exception as exc:
            error = self._safe_error(exc)
            self._last_venue_sync_at = now
            self._last_venue_sync_error = error
            self._last_venue_sync_summary = f"Venue sync failed: {error}"
            with self._lock:
                with self._db.transaction() as cur:
                    self._db.execute(
                        cur,
                        """UPDATE live_managed_positions SET
                           venue_sync_status='sync_error',venue_sync_ts=%s
                           WHERE mode='live' AND status='open'""",
                        (now,),
                    )
            self._journal(
                "venue_reconciliation",
                "sync_error",
                payload={"error": error, "read_only": True},
            )
            return {
                "status": "error",
                "summary": self._last_venue_sync_summary,
                "error": error,
                "positions": [],
            }

        views = [
            self._venue_position_view(slug, value)
            for slug, value in venue_positions.items()
        ]
        views.sort(
            key=lambda item: (
                abs(float(item["net_position"])),
                item["market_slug"],
            ),
            reverse=True,
        )
        self._last_venue_positions = tuple(
            item
            for item in views
            if abs(float(item["net_position"])) > 1e-8
        )
        self._last_venue_sync_at = now
        self._last_venue_sync_error = None

        in_sync = 0
        pending = 0
        partial = 0
        closed = 0
        grace = 0
        for position in [
            row
            for row in self.positions(open_only=True)
            if row["mode"] == "live"
        ]:
            raw = venue_positions.get(str(position["market_slug"]), {})
            net_position = _amount(raw.get("netPosition")) or 0.0
            qty_available = _amount(raw.get("qtyAvailable"))
            update_time = str(raw.get("updateTime") or "") or None
            observed = (
                max(0.0, net_position)
                if position["position_side"] == "long"
                else max(0.0, -net_position)
            )
            current_quantity = float(position["quantity"])
            if now - float(position["opened_ts"]) < VENUE_RECONCILIATION_GRACE_SECONDS:
                grace += 1
                self._set_venue_sync_observation(
                    position["id"],
                    status="entry_settlement_grace",
                    net_position=net_position,
                    qty_available=qty_available,
                    observed_quantity=observed,
                    mismatch_count=0,
                    update_time=update_time,
                )
                continue
            if observed + 1e-8 >= current_quantity:
                in_sync += 1
                self._set_venue_sync_observation(
                    position["id"],
                    status=(
                        "in_sync"
                        if observed <= current_quantity + 1e-8
                        else "in_sync_with_manual_excess"
                    ),
                    net_position=net_position,
                    qty_available=qty_available,
                    observed_quantity=observed,
                    mismatch_count=0,
                    update_time=update_time,
                )
                continue

            previous_observed = position.get("venue_observed_quantity")
            same_observation = (
                previous_observed is not None
                and abs(float(previous_observed) - observed) <= 1e-8
            )
            mismatch_count = (
                int(position.get("venue_mismatch_count") or 0) + 1
                if same_observation
                else 1
            )
            if mismatch_count < VENUE_MISMATCH_CONFIRMATIONS:
                pending += 1
                self._set_venue_sync_observation(
                    position["id"],
                    status="mismatch_pending_confirmation",
                    net_position=net_position,
                    qty_available=qty_available,
                    observed_quantity=observed,
                    mismatch_count=mismatch_count,
                    update_time=update_time,
                )
                self._journal(
                    "venue_reconciliation",
                    "mismatch_pending",
                    event_id=position["event_id"],
                    event_name=position["event_name"],
                    market_slug=position["market_slug"],
                    selection=position["selection"],
                    payload={
                        "position_id": position["id"],
                        "managed_quantity": current_quantity,
                        "observed_same_side_quantity": observed,
                        "confirmation": mismatch_count,
                        "required_confirmations": VENUE_MISMATCH_CONFIRMATIONS,
                    },
                )
                continue
            outcome = self._apply_external_quantity(
                position,
                observed_quantity=observed,
                net_position=net_position,
                qty_available=qty_available,
                update_time=update_time,
            )
            if outcome == "externally_closed":
                closed += 1
            else:
                partial += 1

        self._last_venue_sync_summary = (
            f"Venue sync complete: {len(self._last_venue_positions)} account "
            f"positions; {in_sync} managed in sync, {pending} pending confirmation, "
            f"{partial} partial external sale, {closed} external close"
            + (f", {grace} settling." if grace else ".")
        )
        return {
            "status": "ok",
            "summary": self._last_venue_sync_summary,
            "error": None,
            "positions": [dict(item) for item in self._last_venue_positions],
            "managed": {
                "in_sync": in_sync,
                "pending": pending,
                "partial_external": partial,
                "externally_closed": closed,
                "settling": grace,
            },
        }

    def performance(self) -> dict[str, Any]:
        """Aggregate managed trade results without loading position history.

        Wins and losses are execution outcomes, not game-result labels. Open
        positions are marked using the latest executable cash-out value and do
        not enter the W-L-P record until they close.
        """
        empty = {
            "total_positions": 0,
            "open_positions": 0,
            "priced_open_positions": 0,
            "unpriced_open_positions": 0,
            "closed_positions": 0,
            "wins": 0,
            "losses": 0,
            "pushes": 0,
            "win_rate": None,
            "closed_cost_basis_usd": 0.0,
            "realized_net_usd": 0.0,
            "open_cost_basis_usd": 0.0,
            "open_cashout_value_usd": 0.0,
            "open_unrealized_pnl_usd": 0.0,
            "total_net_usd": 0.0,
            "total_net_complete": True,
            "session_started_at": None,
        }
        modes = {
            "dry_run": {**empty, "mode": "dry_run", "label": "Dry run"},
            "live": {**empty, "mode": "live", "label": "Live"},
        }
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT status,MAX(created_ts) AS reset_ts
                       FROM live_trading_journal
                       WHERE kind='performance_reset' AND status IN ('dry_run','live')
                       GROUP BY status""",
                )
                reset_cutoffs = {
                    str(row["status"]): float(row["reset_ts"])
                    for row in cur.fetchall()
                    if row["reset_ts"] is not None
                }
                self._db.execute(
                    cur,
                    """SELECT mode,
                              COUNT(*) AS total_positions,
                              SUM(CASE WHEN status='open' THEN 1 ELSE 0 END)
                                  AS open_positions,
                              SUM(CASE WHEN status='open'
                                        AND current_exit_value IS NOT NULL
                                       THEN 1 ELSE 0 END)
                                  AS priced_open_positions,
                              SUM(CASE WHEN status='open'
                                        AND current_exit_value IS NULL
                                       THEN 1 ELSE 0 END)
                                  AS unpriced_open_positions,
                              SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END)
                                  AS closed_positions,
                              SUM(CASE WHEN status='closed'
                                        AND realized_pnl > 0.000000001
                                       THEN 1 ELSE 0 END) AS wins,
                              SUM(CASE WHEN status='closed'
                                        AND realized_pnl < -0.000000001
                                       THEN 1 ELSE 0 END) AS losses,
                              SUM(CASE WHEN status='closed'
                                        AND realized_pnl >= -0.000000001
                                        AND realized_pnl <= 0.000000001
                                       THEN 1 ELSE 0 END) AS pushes,
                              COALESCE(SUM(CASE WHEN status='closed'
                                               THEN cost_basis ELSE 0 END),0)
                                  AS closed_cost_basis_usd,
                              COALESCE(SUM(CASE WHEN status='closed'
                                               THEN realized_pnl ELSE 0 END),0)
                                  AS realized_net_usd,
                              COALESCE(SUM(CASE WHEN status='open'
                                               THEN cost_basis ELSE 0 END),0)
                                  AS open_cost_basis_usd,
                              COALESCE(SUM(CASE WHEN status='open'
                                                AND current_exit_value IS NOT NULL
                                               THEN current_exit_value * quantity
                                               ELSE 0 END),0)
                                  AS open_cashout_value_usd,
                              COALESCE(SUM(CASE WHEN status='open'
                                                AND current_exit_value IS NOT NULL
                                               THEN (current_exit_value-entry_cost)
                                                    * quantity
                                               ELSE 0 END),0)
                                  AS open_unrealized_pnl_usd
                       FROM live_managed_positions
                       WHERE (mode='dry_run' AND opened_ts>%s)
                          OR (mode='live' AND opened_ts>%s)
                       GROUP BY mode""",
                    (
                        reset_cutoffs.get("dry_run", 0.0),
                        reset_cutoffs.get("live", 0.0),
                    ),
                )
                rows = [dict(row) for row in cur.fetchall()]

        integer_fields = (
            "total_positions",
            "open_positions",
            "priced_open_positions",
            "unpriced_open_positions",
            "closed_positions",
            "wins",
            "losses",
            "pushes",
        )
        money_fields = (
            "closed_cost_basis_usd",
            "realized_net_usd",
            "open_cost_basis_usd",
            "open_cashout_value_usd",
            "open_unrealized_pnl_usd",
        )
        for row in rows:
            mode = str(row.get("mode") or "")
            if mode not in modes:
                continue
            bucket = modes[mode]
            for field in integer_fields:
                bucket[field] = int(row.get(field) or 0)
            for field in money_fields:
                bucket[field] = round(float(row.get(field) or 0.0), 4)

        def finish(bucket: dict[str, Any]) -> dict[str, Any]:
            decisions = bucket["wins"] + bucket["losses"]
            bucket["win_rate"] = (
                bucket["wins"] / decisions if decisions else None
            )
            bucket["total_net_usd"] = round(
                bucket["realized_net_usd"]
                + bucket["open_unrealized_pnl_usd"],
                4,
            )
            bucket["total_net_complete"] = (
                bucket["unpriced_open_positions"] == 0
            )
            return bucket

        for bucket in modes.values():
            cutoff = reset_cutoffs.get(bucket["mode"])
            bucket["session_started_at"] = (
                datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
                if cutoff is not None
                else None
            )
            finish(bucket)
        combined = {
            **empty,
            "mode": "combined",
            "label": "Combined",
        }
        for field in integer_fields + money_fields:
            combined[field] = sum(modes[mode][field] for mode in modes)
        finish(combined)
        return {
            "as_of": datetime.fromtimestamp(
                self._clock(), timezone.utc
            ).isoformat(),
            "currency": "USD",
            "modes": modes,
            "combined": combined,
            "definitions": {
                "win_loss_push": (
                    "Closed trades grouped by realized P/L; pushes are within "
                    "one-billionth of a dollar of zero."
                ),
                "win_rate": "Wins divided by wins plus losses; pushes excluded.",
                "open_unrealized_pnl": (
                    "Latest executable cash-out mark minus entry cost; future "
                    "exit fees are not yet known."
                ),
                "total_net": (
                    "Realized net plus priced open unrealized P/L. Completeness "
                    "is false when any open position lacks a cash-out mark."
                ),
                "session_reset": (
                    "A live tally reset changes this display baseline only. "
                    "Positions, the execution journal, and risk-control history "
                    "remain preserved."
                ),
            },
        }

    def performance_ledger(
        self,
        *,
        mode: str = "all",
        market_type: str = "all",
        result: str = "all",
        query: str = "",
        limit: int = 2_000,
    ) -> dict[str, Any]:
        """Return an auditable, filterable view of managed execution outcomes.

        A "win" here means a closed position with positive realized after-cost
        P/L. It is deliberately not presented as a prediction or game-outcome
        win. Grouped settings come from the immutable entry-time policy
        snapshot; missing historical snapshots remain explicitly unavailable.
        """
        mode = str(mode or "all").strip().casefold()
        market_type = str(market_type or "all").strip().casefold()
        result = str(result or "all").strip().casefold()
        query = str(query or "").strip().casefold()[:200]
        if mode not in {"all", "dry_run", "live"}:
            raise TradingPolicyError("ledger mode must be all, dry_run, or live")
        if market_type not in {"all", *SUPPORTED_ENTRY_MARKET_TYPES}:
            raise TradingPolicyError(
                "ledger line type must be all, moneyline, spread, or total"
            )
        if result not in {
            "all",
            "open",
            "win",
            "loss",
            "push",
            "unverified",
        }:
            raise TradingPolicyError(
                "ledger result must be all, open, win, loss, push, or unverified"
            )
        limit = max(1, min(int(limit), 10_000))

        def execution_result(position: Mapping[str, Any]) -> str:
            if str(position.get("status") or "") == "open":
                return "open"
            pnl = _amount(position.get("realized_pnl"))
            if pnl is None:
                return "unverified"
            if pnl > 0.000000001:
                return "win"
            if pnl < -0.000000001:
                return "loss"
            return "push"

        positions = self.positions(include_hidden=True)
        matching: list[dict[str, Any]] = []
        for position in positions:
            canonical_market = _market_kind(position.get("market_type"))
            position_result = execution_result(position)
            if mode != "all" and position.get("mode") != mode:
                continue
            if market_type != "all" and canonical_market != market_type:
                continue
            if result != "all" and position_result != result:
                continue
            if query and query not in " ".join((
                str(position.get("event_name") or ""),
                str(position.get("selection") or ""),
                str(position.get("market_slug") or ""),
            )).casefold():
                continue

            policy = position.get("entry_policy")
            if not isinstance(policy, Mapping):
                policy = None
                policy_signature = "unavailable"
            else:
                canonical_policy = json.dumps(
                    dict(policy),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                policy_signature = hashlib.sha256(
                    canonical_policy.encode("utf-8")
                ).hexdigest()[:12]
            matching.append({
                "id": position["id"],
                "opened_at": position.get("opened_at"),
                "closed_at": position.get("closed_at"),
                "mode": position.get("mode"),
                "status": position.get("status"),
                "result": position_result,
                "event_id": position.get("event_id"),
                "event_name": position.get("event_name"),
                "market_slug": position.get("market_slug"),
                "market_type": canonical_market,
                "selection": position.get("selection"),
                "quantity": round(float(position.get("initial_quantity") or 0), 6),
                "entry_cost": round(float(position.get("entry_cost") or 0), 6),
                "cost_basis_usd": round(
                    float(position.get("initial_cost_basis") or 0), 4
                ),
                "current_exit_value": _amount(
                    position.get("current_exit_value")
                ),
                "realized_net_usd": (
                    round(float(position["realized_pnl"]), 4)
                    if position.get("realized_pnl") is not None else None
                ),
                "return_fraction": _amount(position.get("return_fraction")),
                "exit_reason": position.get("exit_reason"),
                "entry_signal_edge": _amount(
                    position.get("entry_signal_edge")
                ),
                "entry_execution_edge": _amount(
                    position.get("entry_execution_edge")
                ),
                "entry_signal_quality": _amount(
                    position.get("entry_signal_quality")
                ),
                "entry_reference_sources": (
                    int(position["entry_reference_sources"])
                    if position.get("entry_reference_sources") is not None
                    else None
                ),
                "policy_session_id": position.get("policy_session_id"),
                "policy_signature": policy_signature,
                "entry_policy": dict(policy) if policy is not None else None,
            })

        def aggregate(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
            rows = list(rows)
            wins = sum(row["result"] == "win" for row in rows)
            losses = sum(row["result"] == "loss" for row in rows)
            pushes = sum(row["result"] == "push" for row in rows)
            open_count = sum(row["result"] == "open" for row in rows)
            unverified = sum(row["result"] == "unverified" for row in rows)
            verifiable = wins + losses + pushes
            decisions = wins + losses
            realized = sum(
                float(row.get("realized_net_usd") or 0)
                for row in rows
                if row["result"] in {"win", "loss", "push"}
            )
            settled_cost = sum(
                float(row.get("cost_basis_usd") or 0)
                for row in rows
                if row["result"] in {"win", "loss", "push"}
            )
            return {
                "trades": len(rows),
                "events": len({
                    str(row.get("event_id") or "")
                    for row in rows
                    if row.get("event_id")
                }),
                "verifiable_closed": verifiable,
                "open": open_count,
                "unverified": unverified,
                "wins": wins,
                "losses": losses,
                "pushes": pushes,
                "win_rate": wins / decisions if decisions else None,
                "realized_net_usd": round(realized, 4),
                "settled_cost_basis_usd": round(settled_cost, 4),
                "after_cost_roi": (
                    realized / settled_cost if settled_cost > 0 else None
                ),
            }

        type_groups = []
        for kind in SUPPORTED_ENTRY_MARKET_TYPES:
            rows = [row for row in matching if row["market_type"] == kind]
            if rows:
                type_groups.append({
                    "market_type": kind,
                    **aggregate(rows),
                })

        settings_buckets: dict[
            tuple[str, str, str],
            list[dict[str, Any]],
        ] = {}
        for row in matching:
            if row["result"] not in {"win", "loss", "push"}:
                continue
            key = (
                str(row["mode"]),
                str(row["market_type"]),
                str(row["policy_signature"]),
            )
            settings_buckets.setdefault(key, []).append(row)
        settings_groups = []
        for (group_mode, kind, signature), rows in settings_buckets.items():
            policy = rows[0].get("entry_policy")
            settings_groups.append({
                "mode": group_mode,
                "market_type": kind,
                "policy_signature": signature,
                "settings_available": isinstance(policy, Mapping),
                "settings": (
                    {
                        key: policy.get(key)
                        for key in (
                            "risk_preset",
                            "allowed_market_types",
                            "min_edge",
                            "min_signal_quality",
                            "min_reference_sources",
                            "min_entry_price",
                            "max_entry_price",
                            "max_spread",
                            "min_book_shares",
                            "profit_target",
                            "stop_loss",
                            "auto_cashout",
                            "require_engine_entry",
                            "required_engine_gates",
                        )
                    }
                    if isinstance(policy, Mapping) else None
                ),
                **aggregate(rows),
            })
        settings_groups.sort(
            key=lambda group: (
                group["realized_net_usd"],
                group["verifiable_closed"],
            ),
            reverse=True,
        )
        matching.sort(
            key=lambda row: str(row.get("opened_at") or ""),
            reverse=True,
        )
        return {
            "generated_at": datetime.fromtimestamp(
                self._clock(), timezone.utc
            ).isoformat(),
            "filters": {
                "mode": mode,
                "market_type": market_type,
                "result": result,
                "query": query,
            },
            "definitions": {
                "success": (
                    "A closed managed position with positive realized after-cost "
                    "P/L; this is an execution result, not necessarily the final "
                    "game outcome."
                ),
                "settings": (
                    "The immutable execution-policy snapshot saved when the "
                    "position was entered. Missing older snapshots are not guessed."
                ),
                "sample_warning": (
                    "Compare net, after-cost ROI, and independent trade/event "
                    "counts together; a high rate from a small group is weak evidence."
                ),
            },
            "summary": aggregate(matching),
            "line_type_summary": type_groups,
            "settings_groups": settings_groups[:200],
            "total_matching_rows": len(matching),
            "rows_truncated": len(matching) > limit,
            "rows": matching[:limit],
        }

    def _advisor_closed_trades(self) -> list[dict[str, Any]]:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT id,event_id,mode,opened_ts,closed_ts,cost_basis,
                              entry_cost,realized_pnl,entry_decision_id,
                              market_slug,selection,position_side,market_type,
                              exit_reason,
                              entry_signal_edge AS signal_edge,
                              entry_signal_quality AS signal_quality,
                              entry_reference_sources AS reference_sources,
                              entry_execution_edge AS execution_edge,
                              entry_game_fraction_remaining
                                  AS game_fraction_remaining,
                              entry_event_entries_60m AS event_entries_60m,
                              policy_session_id
                       FROM live_managed_positions
                       WHERE status='closed' AND realized_pnl IS NOT NULL
                       ORDER BY opened_ts""",
                )
                return [dict(row) for row in cur.fetchall()]

    def _advisor_opportunities(self) -> list[dict[str, Any]]:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT id,created_ts,event_id,market_slug,selection,payload
                       FROM live_trading_journal
                       WHERE kind='entry'
                       ORDER BY created_ts""",
                )
                rows = [dict(row) for row in cur.fetchall()]
        opportunities: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            decision_id = str(payload.get("decision_id") or "")
            key = "|".join((
                decision_id or str(row["id"]),
                str(row.get("market_slug") or ""),
                str(payload.get("position_side") or ""),
            ))
            opportunities.setdefault(
                key,
                {
                    "event_id": row.get("event_id"),
                    "observed_ts": float(row["created_ts"]),
                    "signal_edge": _amount(payload.get("signal_edge")),
                    "signal_quality": _amount(payload.get("signal_quality")),
                    "reference_sources": _amount(
                        payload.get("reference_sources")
                    ),
                    "entry_cost": _amount(payload.get("entry_cost")),
                    "execution_edge": _amount(payload.get("execution_edge")),
                    "decision_id": decision_id or None,
                    "market_slug": str(row.get("market_slug") or ""),
                    "selection": row.get("selection"),
                    "position_side": payload.get("position_side"),
                    "market_type": _market_kind(
                        payload.get("signal_market")
                    ),
                    "mode": payload.get("execution_mode"),
                    "game_fraction_remaining": _amount(
                        payload.get("game_fraction_remaining")
                    ),
                    "event_entries_60m": _amount(
                        payload.get("event_entries_60m")
                    ),
                },
            )
        return list(opportunities.values())

    def policy_sessions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT s.id,s.started_ts,s.ended_ts,s.mode,s.reason,
                              s.policy_json,
                              COUNT(p.id) AS trades,
                              COUNT(DISTINCT p.event_id) AS events,
                              COALESCE(SUM(CASE WHEN p.status='closed'
                                               THEN p.realized_pnl ELSE 0 END),0)
                                  AS realized_net_usd,
                              SUM(CASE WHEN p.status='open' THEN 1 ELSE 0 END)
                                  AS open_positions
                       FROM trading_policy_sessions s
                       LEFT JOIN live_managed_positions p
                         ON p.policy_session_id=s.id
                       GROUP BY s.id,s.started_ts,s.ended_ts,s.mode,s.reason,
                                s.policy_json
                       ORDER BY s.started_ts DESC LIMIT %s""",
                    (limit,),
                )
                rows = [dict(row) for row in cur.fetchall()]
        result = []
        for row in rows:
            try:
                policy = json.loads(row.pop("policy_json"))
            except (TypeError, json.JSONDecodeError):
                policy = {}
            started_ts = float(row.pop("started_ts"))
            ended_value = row.pop("ended_ts")
            result.append({
                **row,
                "started_at": datetime.fromtimestamp(
                    started_ts, timezone.utc
                ).isoformat(),
                "ended_at": (
                    datetime.fromtimestamp(
                        float(ended_value), timezone.utc
                    ).isoformat()
                    if ended_value is not None else None
                ),
                "trades": int(row.get("trades") or 0),
                "events": int(row.get("events") or 0),
                "open_positions": int(row.get("open_positions") or 0),
                "realized_net_usd": round(
                    float(row.get("realized_net_usd") or 0.0), 4
                ),
                "policy": policy,
            })
        return result

    def policy_advice(
        self,
        *,
        objective: str,
        target_trades_per_hour: float,
        model_evidence: Mapping[str, Any] | None = None,
        analysis_mode: str | None = None,
        lookback_days: int = 0,
        market_types: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        self._refresh_policy_authority()
        source_policy = asdict(self._policy)
        source_policy_hash = _policy_fingerprint(source_policy)
        requested_market_types = tuple(
            dict.fromkeys(
                _market_kind(value)
                for value in (
                    market_types
                    if market_types is not None
                    else self._policy.allowed_market_types
                )
            )
        )
        allowed_market_types = set(requested_market_types)
        closed_trades = [
            row
            for row in self._advisor_closed_trades()
            if _market_kind(row.get("market_type")) in allowed_market_types
        ]
        opportunities = [
            row
            for row in self._advisor_opportunities()
            if _market_kind(row.get("market_type")) in allowed_market_types
        ]
        try:
            recommendation = recommend_policy(
                closed_trades=closed_trades,
                opportunities=opportunities,
                current_policy=source_policy,
                objective=objective,
                target_trades_per_hour=float(target_trades_per_hour),
                model_evidence=model_evidence,
                analysis_mode=analysis_mode,
                lookback_days=int(lookback_days),
                market_types=requested_market_types,
            )
        except ValueError as exc:
            raise TradingPolicyError(str(exc)) from exc
        recommendation["evidence"]["source_policy_hash"] = source_policy_hash
        recommendation["source_policy_hash"] = source_policy_hash
        advice_id = str(uuid4())
        created_ts = self._clock()
        session_id = self._current_policy_session()
        recommendation.update({
            "id": advice_id,
            "created_at": datetime.fromtimestamp(
                created_ts, timezone.utc
            ).isoformat(),
            "policy_session_id": session_id,
            "available_objectives": ADVISOR_OBJECTIVES,
        })
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO trading_policy_advice
                       (id,created_ts,session_id,objective,target_trades_per_hour,
                        status,suggested_policy_json,evidence_json,
                        model_evidence_json,applied_ts)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)""",
                    (
                        advice_id,
                        created_ts,
                        session_id,
                        objective,
                        float(target_trades_per_hour),
                        recommendation["status"],
                        json.dumps(
                            recommendation["suggested_policy"],
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        json.dumps(
                            recommendation["evidence"],
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        json.dumps(
                            recommendation["model_evidence"],
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
        self._journal(
            "policy_advisor",
            "recommended",
            payload={
                "advice_id": advice_id,
                "objective": objective,
                "target_trades_per_hour": target_trades_per_hour,
                "status": recommendation["status"],
                "changes": recommendation["changes"],
                "model_used_to_change_settings": False,
            },
        )
        return recommendation

    def apply_policy_advice(
        self,
        advice_id: str,
        confirmation: str,
    ) -> dict[str, Any]:
        self._refresh_policy_authority()
        if not approval_granted(confirmation):
            raise TradingPolicyError(
                approval_instruction("apply the suggested filters")
            )
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT status,suggested_policy_json,evidence_json,
                              applied_ts
                       FROM trading_policy_advice WHERE id=%s""",
                    (advice_id,),
                )
                row = cur.fetchone()
        if row is None:
            raise TradingPolicyError("policy advice was not found")
        if row["applied_ts"] is not None:
            raise TradingPolicyError(
                "policy advice was already applied; analyze the current "
                "settings to generate a fresh recommendation"
            )
        try:
            suggested = json.loads(row["suggested_policy_json"])
            evidence = json.loads(row["evidence_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise TradingPolicyError("stored policy advice is invalid") from exc
        source_policy_hash = str(evidence.get("source_policy_hash") or "")
        if not source_policy_hash:
            raise TradingPolicyError(
                "this recommendation predates stale-settings protection; "
                "analyze the current settings again"
            )
        if source_policy_hash != _policy_fingerprint(self._policy):
            raise TradingPolicyError(
                "execution settings changed after this recommendation was "
                "generated; analyze again before applying suggested filters"
            )
        if row["status"] != "evidence_backed_research":
            raise TradingPolicyError(
                "policy advice is diagnostic only and cannot be applied until "
                "its later-event, whole-event validation checks pass"
            )
        if evidence.get("validation_passed") is not True:
            raise TradingPolicyError(
                "stored policy advice lacks a passing robust validation record"
            )
        changes = {
            field: suggested[field]
            for field in ADVISOR_TUNABLE_FIELDS
            if field in suggested
        }
        changes["risk_preset"] = "custom"
        policy = self.configure(changes)
        applied_ts = self._clock()
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """UPDATE trading_policy_advice SET applied_ts=%s
                       WHERE id=%s AND applied_ts IS NULL""",
                    (applied_ts, advice_id),
                )
        self._journal(
            "policy_advisor",
            "applied",
            payload={
                "advice_id": advice_id,
                "applied_fields": sorted(changes),
                "live_disarmed": True,
                "core_calculations_unchanged": True,
            },
        )
        return {
            "advice_id": advice_id,
            "applied_at": datetime.fromtimestamp(
                applied_ts, timezone.utc
            ).isoformat(),
            "policy": asdict(policy),
            "live_disarmed": True,
            "summary": (
                "Suggested execution filters were saved. Live orders are "
                "disarmed for review; probability, edge, quality, calibration, "
                "and engine-gate calculations were not changed."
            ),
        }

    def policy_advice_history(self, *, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT id,created_ts,session_id,objective,
                              target_trades_per_hour,status,
                              suggested_policy_json,evidence_json,
                              model_evidence_json,applied_ts
                       FROM trading_policy_advice
                       ORDER BY created_ts DESC LIMIT %s""",
                    (limit,),
                )
                rows = [dict(row) for row in cur.fetchall()]
        result = []
        for row in rows:
            try:
                suggested = json.loads(row.pop("suggested_policy_json"))
                evidence = json.loads(row.pop("evidence_json"))
                model_evidence = json.loads(row.pop("model_evidence_json"))
            except (TypeError, json.JSONDecodeError):
                suggested, evidence, model_evidence = {}, {}, {}
            created_ts = float(row.pop("created_ts"))
            applied_ts = row.pop("applied_ts")
            result.append({
                **row,
                "created_at": datetime.fromtimestamp(
                    created_ts, timezone.utc
                ).isoformat(),
                "applied_at": (
                    datetime.fromtimestamp(
                        float(applied_ts), timezone.utc
                    ).isoformat()
                    if applied_ts is not None else None
                ),
                "suggested_policy": suggested,
                "evidence": evidence,
                "model_evidence": model_evidence,
            })
        return result

    def reset_live_performance(self, confirmation: str) -> dict[str, Any]:
        """Start a fresh live display tally without deleting execution evidence."""
        if not approval_granted(confirmation):
            raise TradingPolicyError(
                approval_instruction("reset the live tally")
            )
        if self.is_armed():
            raise TradingPolicyError(
                "disarm live trading before resetting its performance tally"
            )
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    """SELECT COUNT(*) FROM live_managed_positions
                       WHERE mode='live' AND status='open'""",
                )
                open_positions = int(cur.fetchone()[0] or 0)
            if open_positions:
                raise TradingPolicyError(
                    f"{open_positions} open live position"
                    f"{'' if open_positions == 1 else 's'} remain; sell or "
                    "synchronize them before starting a fresh live tally"
                )
            previous = dict(self.performance()["modes"]["live"])
            reset_at = self._clock()
            self._journal(
                "performance_reset",
                "live",
                payload={
                    "mode": "live",
                    "reset_at": reset_at,
                    "previous_total_positions": previous["total_positions"],
                    "previous_wins": previous["wins"],
                    "previous_losses": previous["losses"],
                    "previous_pushes": previous["pushes"],
                    "previous_total_net_usd": previous["total_net_usd"],
                    "positions_preserved": True,
                    "execution_journal_preserved": True,
                    "risk_history_preserved": True,
                    "reason": "operator_started_new_live_tally_session",
                },
            )
        reset_at_iso = datetime.fromtimestamp(reset_at, timezone.utc).isoformat()
        return {
            "mode": "live",
            "reset_at": reset_at_iso,
            "previous": previous,
            "positions_preserved": True,
            "execution_journal_preserved": True,
            "risk_history_preserved": True,
            "summary": (
                "Live W-L-P and net display reset to zero. Historical positions, "
                "the execution journal, and daily-loss safeguards were preserved."
            ),
        }

    def reset_risk_session(self, confirmation: str) -> dict[str, Any]:
        """Start fresh rolling entry counters without erasing audit evidence.

        The reset is intentionally narrow. It waives only entry attempts and
        realized losses that occurred before this explicit session boundary,
        plus the process-local candidate retry cooldown. Position stops,
        exposure, buying power, venue state, mapping, price, liquidity, edge,
        quality, and selected engine gates remain enforced.
        """
        self._refresh_policy_authority()
        if not approval_granted(confirmation):
            raise TradingPolicyError(
                approval_instruction("start a new risk session")
            )
        if self._policy.execution_mode == "live" and self.is_armed():
            raise TradingPolicyError(
                "disarm live trading before resetting entry circuit breakers; "
                "review the fresh counters, then explicitly re-arm"
            )

        previous = self._risk_limiter_snapshot()
        reset_at = self._clock()
        cleared_candidate_cooldowns = len(self._candidate_seen)
        # Invalidate any analysis cycle that began under the previous risk
        # window before recording the new boundary.
        self._control_generation += 1
        self._candidate_seen.clear()
        self._journal(
            "risk_session_reset",
            "started",
            payload={
                "reset_at": reset_at,
                "previous_orders_last_hour": previous["orders_last_hour"],
                "previous_realized_loss_24h_usd": previous[
                    "realized_loss_24h_usd"
                ],
                "cleared_candidate_cooldowns": cleared_candidate_cooldowns,
                "positions_preserved": True,
                "performance_preserved": True,
                "execution_journal_preserved": True,
                "per_position_stop_loss_preserved": True,
                "exposure_limits_preserved": True,
                "venue_and_signal_safeguards_preserved": True,
                "reason": "operator_started_new_entry_risk_session",
            },
        )
        policy_session_id = self._start_policy_session(
            "operator_started_new_risk_session"
        )
        current = self._risk_limiter_snapshot()
        self._last_cycle_summary = (
            "New risk session started. Hourly entry and rolling realized-loss "
            "counters are zero; candidate cooldowns were cleared. All position, "
            "account, venue, liquidity, edge, quality, and engine safeguards "
            "remain active."
        )
        return {
            "reset_at": datetime.fromtimestamp(
                reset_at, timezone.utc
            ).isoformat(),
            "previous": previous,
            "current": current,
            "cleared_candidate_cooldowns": cleared_candidate_cooldowns,
            "policy_session_id": policy_session_id,
            "positions_preserved": True,
            "performance_preserved": True,
            "execution_journal_preserved": True,
            "per_position_stop_loss_preserved": True,
            "summary": self._last_cycle_summary,
        }

    def liquidate_open_positions(
        self,
        us_payload: Mapping[str, Any],
        *,
        mode: str,
        confirmation: str = "",
    ) -> dict[str, Any]:
        """Attempt an executable exit for every managed position in one mode.

        Dry-run liquidation is an operator reset: every simulated position and its
        performance history are removed without waiting for a market quote. Live
        positions reuse the normal previewed FOK sell path and remain open when a
        quote is unavailable, an order fails, or the order does not fill.
        """
        if mode not in {"dry_run", "live"}:
            raise TradingPolicyError("mode must be dry_run or live")
        if mode == "dry_run":
            return self.clear_dry_run_history(DRY_RUN_HISTORY_CLEAR_PHRASE)
        self._control_generation += 1
        if not self._cycle_lock.acquire(timeout=20):
            raise TradingPolicyError(
                "the current trading cycle did not stop in time; try the sale again"
            )
        try:
            positions = [
                position
                for position in self.positions(open_only=True)
                if position["mode"] == mode
            ]
            if not positions:
                result = {
                    "mode": mode,
                    "requested": 0,
                    "attempted": 0,
                    "filled": 0,
                    "failed": 0,
                    "remaining": 0,
                    "results": [],
                    "summary": f"No open {mode.replace('_', '-')} positions to sell.",
                }
                self._journal(
                    "bulk_exit",
                    "no_positions",
                    payload={key: value for key, value in result.items() if key != "results"},
                )
                return result

            if mode == "live":
                if self._policy.execution_mode != "live":
                    raise TradingPolicyError(
                        "set execution mode to live and save the policy before selling "
                        "live positions"
                    )
                if not self.is_armed():
                    raise TradingPolicyError(
                        "live trading is disarmed; arm the live-order latch first"
                    )
                if not approval_granted(confirmation):
                    raise TradingPolicyError(
                        approval_instruction("sell all live positions")
                    )

            markets = {
                str(market.get("slug")): dict(market)
                for event in us_payload.get("events", [])
                if isinstance(event, Mapping)
                for market in event.get("markets", [])
                if isinstance(market, Mapping) and market.get("slug")
            }
            results: list[dict[str, Any]] = []
            attempted = 0
            filled = 0
            reason = f"manual_clear_all_{mode}"

            for position in positions:
                market, exit_value, order_long_price, failure = (
                    self._manual_exit_quote(position, markets)
                )

                if failure:
                    self._journal(
                        "exit",
                        "manual_bulk_blocked",
                        event_id=position["event_id"],
                        event_name=position["event_name"],
                        market_slug=position["market_slug"],
                        selection=position["selection"],
                        payload={
                            "position_id": position["id"],
                            "mode": mode,
                            "reason": failure,
                            "exit_trigger": reason,
                        },
                    )
                    results.append({
                        "position_id": position["id"],
                        "event_name": position["event_name"],
                        "selection": position["selection"],
                        "status": "blocked",
                        "reason": failure,
                    })
                    continue

                assert exit_value is not None
                assert order_long_price is not None
                assert market is not None
                attempted += 1
                did_fill = self._attempt_exit(
                    position,
                    market,
                    exit_value=exit_value,
                    order_long_price=order_long_price,
                    reason=reason,
                )
                filled += int(did_fill)
                results.append({
                    "position_id": position["id"],
                    "event_name": position["event_name"],
                    "selection": position["selection"],
                    "status": "filled" if did_fill else "not_filled",
                    "reason": (
                        "position sold"
                        if did_fill
                        else "sell attempt did not fill; position remains open"
                    ),
                })

            failed = len(positions) - filled
            remaining = sum(
                1
                for position in self.positions(open_only=True)
                if position["mode"] == mode
            )
            status = "completed" if failed == 0 else "partial"
            summary = (
                f"{filled} of {len(positions)} open {mode.replace('_', '-')} "
                f"positions sold; {remaining} remain open."
            )
            result = {
                "mode": mode,
                "requested": len(positions),
                "attempted": attempted,
                "filled": filled,
                "failed": failed,
                "remaining": remaining,
                "results": results,
                "summary": summary,
            }
            self._journal(
                "bulk_exit",
                status,
                payload={key: value for key, value in result.items() if key != "results"},
            )
            return result
        finally:
            self._cycle_lock.release()

    def exit_position(
        self,
        us_payload: Mapping[str, Any],
        *,
        position_id: str,
        confirmation: str = "",
    ) -> dict[str, Any]:
        """Force-remove one simulation or attempt one bounded live sale."""
        position = self.position(position_id)
        if position is None:
            raise TradingPolicyError("managed position was not found")
        if position["status"] != "open":
            raise TradingPolicyError("managed position is already closed")

        # Invalidate an analysis cycle immediately so it cannot mark, exit, or
        # recreate this position while the operator action is taking priority.
        self._control_generation += 1
        candidate_key = ":".join((
            str(position["event_id"]),
            str(position["market_slug"]),
            str(position["position_side"]),
        ))
        self._candidate_seen[candidate_key] = self._clock()

        if position["mode"] == "dry_run":
            with self._lock:
                with self._db.transaction() as cur:
                    self._db.execute(
                        cur,
                        "DELETE FROM live_managed_orders WHERE position_id=%s",
                        (position_id,),
                    )
                    self._db.execute(
                        cur,
                        """DELETE FROM live_managed_positions
                           WHERE id=%s AND mode='dry_run' AND status='open'""",
                        (position_id,),
                    )
                    if int(cur.rowcount or 0) != 1:
                        raise TradingPolicyError(
                            "dry-run position changed before it could be removed"
                        )
            summary = (
                f"Removed dry-run position {position['selection']} from "
                f"{position['event_name']}. Automation remains "
                f"{'ON' if self._policy.automation_enabled else 'OFF'}."
            )
            self._journal(
                "position_control",
                "dry_run_removed",
                event_id=position["event_id"],
                event_name=position["event_name"],
                market_slug=position["market_slug"],
                selection=position["selection"],
                payload={
                    "position_id": position_id,
                    "mode": "dry_run",
                    "automation_enabled": self._policy.automation_enabled,
                    "quote_required": False,
                },
            )
            return {
                "position_id": position_id,
                "mode": "dry_run",
                "status": "removed",
                "remaining": sum(
                    1
                    for item in self.positions(open_only=True)
                    if item["mode"] == "dry_run"
                ),
                "automation_enabled": self._policy.automation_enabled,
                "quote_required": False,
                "summary": summary,
            }

        if self._policy.execution_mode != "live":
            raise TradingPolicyError(
                "set execution mode to live and save the policy before selling "
                "a live position"
            )
        if not self.is_armed():
            raise TradingPolicyError(
                "live trading is disarmed; arm the live-order latch first"
            )
        if not approval_granted(confirmation):
            raise TradingPolicyError(
                approval_instruction("sell a live position")
            )
        if not self._cycle_lock.acquire(timeout=20):
            raise TradingPolicyError(
                "the current trading cycle did not stop in time; try the sale again"
            )
        try:
            markets = {
                str(market.get("slug")): dict(market)
                for event in us_payload.get("events", [])
                if isinstance(event, Mapping)
                for market in event.get("markets", [])
                if isinstance(market, Mapping) and market.get("slug")
            }
            market, exit_value, order_long_price, failure = (
                self._manual_exit_quote(position, markets)
            )
            if failure:
                self._journal(
                    "exit",
                    "manual_individual_blocked",
                    event_id=position["event_id"],
                    event_name=position["event_name"],
                    market_slug=position["market_slug"],
                    selection=position["selection"],
                    payload={
                        "position_id": position_id,
                        "mode": "live",
                        "reason": failure,
                    },
                )
                return {
                    "position_id": position_id,
                    "mode": "live",
                    "status": "blocked",
                    "remaining": 1,
                    "summary": failure,
                }
            assert market is not None
            assert exit_value is not None
            assert order_long_price is not None
            filled = self._attempt_exit(
                position,
                market,
                exit_value=exit_value,
                order_long_price=order_long_price,
                reason="manual_individual_live",
            )
            return {
                "position_id": position_id,
                "mode": "live",
                "status": "filled" if filled else "not_filled",
                "remaining": 0 if filled else 1,
                "summary": (
                    f"Sold live position {position['selection']}."
                    if filled
                    else "The live sell did not fill; the position remains open."
                ),
            }
        finally:
            self._cycle_lock.release()

    @staticmethod
    def _manual_exit_quote(
        position: Mapping[str, Any],
        markets: Mapping[str, Mapping[str, Any]],
    ) -> tuple[
        dict[str, Any] | None,
        float | None,
        float | None,
        str,
    ]:
        market = markets.get(str(position["market_slug"]))
        if market is None:
            return None, None, None, (
                "current Polymarket US market snapshot is unavailable"
            )
        if (
            not market.get("active", True)
            or market.get("closed")
            or market.get("hidden")
            or str(market.get("state") or "OPEN").upper()
            not in {"OPEN", "MARKET_STATE_OPEN", "EP3_STATUS_OPEN"}
        ):
            return dict(market), None, None, (
                "the Polymarket US market is not open for an exit"
            )
        sides = [
            side
            for side in market.get("sides", [])
            if isinstance(side, Mapping)
            and bool(side.get("long"))
            == (position["position_side"] == "long")
        ]
        if len(sides) != 1:
            return dict(market), None, None, (
                "the managed position no longer maps to one exact side"
            )
        prices = _book_prices(market, sides[0])
        if prices is None or prices[2] is None:
            return dict(market), None, None, (
                "a complete executable cash-out quote is unavailable"
            )
        order_long_price = _amount(
            market.get(
                "long_best_bid"
                if position["position_side"] == "long"
                else "long_best_ask"
            )
        )
        if order_long_price is None:
            return dict(market), None, None, (
                "the executable sell limit is unavailable"
            )
        return dict(market), prices[2], order_long_price, ""

    def clear_dry_run_history(self, confirmation: str) -> dict[str, Any]:
        """Force-clear every simulated trade while preserving live and audit data."""
        if not approval_granted(confirmation):
            raise TradingPolicyError(
                approval_instruction("clear dry-run trade history")
            )
        self._refresh_policy_authority()
        stopped_policy = TradingPolicy.from_mapping({
            **asdict(self._policy),
            "automation_enabled": False,
        })
        control_token = uuid4().hex
        policy_payload = json.dumps(
            {**asdict(stopped_policy), _CONTROL_TOKEN_KEY: control_token},
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            # This is an operator reset, not an execution attempt. Persisting the
            # stop and deleting the simulated ledger in one transaction means no
            # quote, mapping, fill, or cycle-lock state can prevent the wipe.
            self._policy = stopped_policy
            self._control_token = control_token
            self._armed_until = 0.0
            self._protective_exits_armed = False
            self._control_generation += 1
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO live_trading_config(singleton,payload,updated_ts)
                       VALUES (%s,%s,%s) ON CONFLICT(singleton) DO UPDATE SET
                       payload=EXCLUDED.payload,updated_ts=EXCLUDED.updated_ts""",
                    (1, policy_payload, self._clock()),
                )
                self._db.execute(
                    cur,
                    """SELECT COUNT(*) FROM live_managed_positions
                       WHERE mode=%s AND status='open'""",
                    ("dry_run",),
                )
                open_count = int(cur.fetchone()[0] or 0)
                self._db.execute(
                    cur,
                    """SELECT COUNT(*) FROM live_managed_positions
                       WHERE mode=%s AND status='closed'""",
                    ("dry_run",),
                )
                closed_count = int(cur.fetchone()[0] or 0)
                deleted = open_count + closed_count
                self._db.execute(
                    cur,
                    """DELETE FROM live_managed_orders
                       WHERE position_id IN (
                           SELECT id FROM live_managed_positions WHERE mode=%s
                       )""",
                    ("dry_run",),
                )
                self._db.execute(
                    cur,
                    "DELETE FROM live_managed_positions WHERE mode=%s",
                    ("dry_run",),
                )
                self._db.execute(
                    cur,
                    "SELECT COUNT(*) FROM live_managed_positions WHERE mode=%s",
                    ("dry_run",),
                )
                verified_remaining = int(cur.fetchone()[0] or 0)
                if verified_remaining:
                    raise TradingExecutionError(
                        "dry-run reset verification found remaining simulated trades"
                    )
            self._candidate_seen.clear()
            self._qualification_seen.clear()
            self._last_cycle_evaluations = ()
            self._last_cycle_summary = (
                "Dry-run automation was stopped and all simulated trades were cleared."
            )

        if deleted:
            summary = (
                f"STOPPED AND CLEARED: removed {open_count} open dry-run position"
                f"{'' if open_count == 1 else 's'} and {closed_count} completed "
                f"trade record{'' if closed_count == 1 else 's'}. Automation is OFF "
                "and zero dry-run trades remain."
            )
        else:
            summary = (
                "STOPPED AND CLEARED: automation is OFF and zero dry-run trades remain."
            )
        self._journal(
            "history_reset",
            "forced",
            payload={
                "mode": "dry_run",
                "deleted_positions": deleted,
                "forced_open_positions": open_count,
                "deleted_closed_positions": closed_count,
                "verified_remaining_positions": verified_remaining,
                "automation_enabled": False,
                "live_disarmed": True,
                "live_positions_preserved": True,
                "execution_journal_preserved": True,
                "quote_required": False,
                "reason": "operator_executive_dry_run_reset",
            },
        )
        return {
            "mode": "dry_run",
            "requested": deleted,
            "attempted": open_count,
            "filled": open_count,
            "failed": 0,
            "remaining": verified_remaining,
            "results": [],
            "deleted_positions": deleted,
            "forced_open_positions": open_count,
            "deleted_closed_positions": closed_count,
            "verified_remaining_positions": verified_remaining,
            "automation_enabled": False,
            "live_disarmed": True,
            "live_positions_preserved": True,
            "execution_journal_preserved": True,
            "quote_required": False,
            "summary": summary,
        }

    def run_cycle(
        self,
        monitored: Iterable[tuple[Event, Iterable[Signal]]],
        us_payload: Mapping[str, Any],
        *,
        game_states: Mapping[str, GameState] | None = None,
    ) -> dict[str, Any]:
        if not self._cycle_lock.acquire(blocking=False):
            self._last_cycle_summary = (
                "A trading analysis cycle is already running; this duplicate was skipped."
            )
            return {"status": "busy", "evaluated": 0, "qualified": 0}
        try:
            self._refresh_policy_authority()
            generation = self._control_generation
            control_token = self._control_token
            return self._run_cycle(
                monitored,
                us_payload,
                generation=generation,
                control_token=control_token,
                game_states=game_states or {},
            )
        finally:
            self._cycle_lock.release()

    def _run_cycle(
        self,
        monitored: Iterable[tuple[Event, Iterable[Signal]]],
        us_payload: Mapping[str, Any],
        *,
        generation: int,
        control_token: str,
        game_states: Mapping[str, GameState],
    ) -> dict[str, Any]:
        now = self._clock()
        self._last_cycle_at = now
        if not self._policy.automation_enabled:
            self._last_cycle_evaluations = ()
            self._last_cycle_summary = "Automation is off; no candidates were evaluated."
            return {"status": "off", "evaluated": 0, "qualified": 0}
        # Reconcile the authenticated venue portfolio before any live mark,
        # exit, or entry. Dry-run-only cycles stay fully offline.
        has_open_live = any(
            position["mode"] == "live"
            for position in self.positions(open_only=True)
        )
        if (
            self._policy.execution_mode == "live"
            or has_open_live
        ) and self._key_id and self._secret_key:
            venue_sync = self._reconcile_live_positions()
        else:
            venue_sync = {
                "status": "skipped",
                "summary": "Venue sync is not needed for this dry-run-only cycle.",
                "error": None,
                "positions": [],
            }
        us_events = [
            event for event in us_payload.get("events", [])
            if isinstance(event, Mapping) and not event.get("ended")
        ]
        mapped: list[tuple[MappedCandidate, dict[str, Any]]] = []
        evaluations: list[dict[str, Any]] = []
        rejected = 0
        monitored_list = [
            (event, list(signals)) for event, signals in monitored
        ]
        for event, signals in monitored_list:
            if not self._cycle_is_current(generation):
                return self._stopped_cycle_result()
            signal_list = signals
            match = _event_match(event, us_events)
            if match is None:
                self._qualification_rejection(
                    event,
                    None,
                    "no unique Polymarket US event mapping",
                )
                evaluations.extend(
                    self._candidate_evaluation(
                        event,
                        signal,
                        "research_only",
                        reason="no unique Polymarket US event mapping",
                    )
                    for signal in signal_list
                )
                rejected += 1
                continue
            us_event, event_score = match
            for signal in signal_list:
                if not self._cycle_is_current(generation):
                    return self._stopped_cycle_result()
                candidate, reason = self._map_signal(
                    event,
                    signal,
                    us_event,
                    event_score,
                    game_state=game_states.get(event.id),
                )
                if candidate is None:
                    self._qualification_rejection(
                        event,
                        signal,
                        reason,
                        us_event_slug=str(us_event.get("slug") or ""),
                    )
                    evaluations.append(
                        self._candidate_evaluation(
                            event,
                            signal,
                            "research_only",
                            reason=reason,
                            us_event_slug=str(us_event.get("slug") or ""),
                        )
                    )
                    rejected += 1
                else:
                    evaluation = self._candidate_evaluation(
                        event,
                        signal,
                        "us_qualified",
                        candidate=candidate,
                    )
                    evaluations.append(evaluation)
                    mapped.append((candidate, evaluation))
        mapped.sort(
            key=lambda item: (
                item[0].execution_edge,
                item[0].signal.confidence,
                item[0].mapping_score,
            ),
            reverse=True,
        )

        if not self._cycle_is_current(generation):
            return self._stopped_cycle_result()
        marked, exited, shadow_marks = self._mark_and_exit(
            monitored_list,
            us_events,
            generation=generation,
            control_token=control_token,
            game_states=game_states,
        )
        placed = 0
        venue_checks = 0
        for index, (candidate, evaluation) in enumerate(mapped):
            if not self._cycle_is_current(generation):
                return self._stopped_cycle_result()
            if self._position_exists(candidate):
                evaluation.update(
                    state="position_open",
                    reason="a managed position is already open",
                )
                continue
            last_seen = self._candidate_seen.get(candidate.key, 0.0)
            if now - last_seen < self._policy.candidate_cooldown_seconds:
                evaluation.update(
                    state="cooldown",
                    reason="candidate is inside the configured retry cooldown",
                )
                continue
            if venue_checks >= 3:
                evaluation.update(
                    state="queued",
                    reason="higher-ranked candidates used this cycle's venue-check budget",
                )
                break
            venue_checks += 1
            self._candidate_seen[candidate.key] = now
            entered, state, reason, audit = self._attempt_entry(
                candidate,
                generation=generation,
                control_token=control_token,
            )
            if state == "automation_stopped":
                return self._stopped_cycle_result()
            evaluation.update(state=state, reason=reason, **audit)
            if audit.get("authenticated_entry_cost") is not None:
                evaluation["us_entry_cost"] = audit["authenticated_entry_cost"]
            if audit.get("authenticated_execution_edge") is not None:
                evaluation["us_execution_edge"] = audit[
                    "authenticated_execution_edge"
                ]
            placed += int(entered)
            if entered:
                for _, waiting in mapped[index + 1:]:
                    if waiting["state"] == "us_qualified":
                        waiting.update(
                            state="queued",
                            reason="one-entry-per-cycle concentration limit",
                        )
                break  # one new position per cycle prevents burst concentration

        self._last_cycle_evaluations = tuple(evaluations[:1_000])
        self._last_cycle_summary = (
            f"Reviewed {len(monitored_list)} monitored events and {len(mapped)} "
            f"mapped selections; {placed} entry, {marked} marks, {exited} exits, "
            f"{shadow_marks} post-exit shadow marks."
        )
        return {
            "status": "completed",
            "events": len(monitored_list),
            "evaluated": len(mapped) + rejected,
            "qualified": len(mapped),
            "entries": placed,
            "marks": marked,
            "exits": exited,
            "post_exit_shadow_marks": shadow_marks,
            "summary": self._last_cycle_summary,
            "venue_sync": venue_sync,
        }

    def _cycle_is_current(self, generation: int) -> bool:
        return (
            self._policy.automation_enabled
            and generation == self._control_generation
        )

    def _stopped_cycle_result(self) -> dict[str, Any]:
        self._last_cycle_evaluations = ()
        self._last_cycle_summary = (
            "Automation was stopped; the in-progress analysis cycle was canceled."
        )
        return {
            "status": "stopped",
            "evaluated": 0,
            "qualified": 0,
            "entries": 0,
            "marks": 0,
            "exits": 0,
            "summary": self._last_cycle_summary,
        }

    def _candidate_evaluation(
        self,
        event: Event,
        signal: Signal,
        state: str,
        *,
        reason: str = "",
        us_event_slug: str = "",
        candidate: MappedCandidate | None = None,
    ) -> dict[str, Any]:
        item = {
            "event_id": event.id,
            "market": signal.market,
            "outcome": signal.outcome,
            "state": state,
            "reason": reason or None,
            "signal_edge": signal.edge,
            "signal_quality": signal.confidence,
            "configured_min_edge": self._policy.min_edge,
            "configured_max_edge": self._policy.max_edge,
            "required_edge": signal.required_edge,
            "configured_min_quality": self._policy.min_signal_quality,
            "reference_sources": signal.n_reference_sources,
            "configured_min_reference_sources": (
                self._policy.min_reference_sources
            ),
            "require_engine_entry": self._policy.require_engine_entry,
            "required_engine_gates": list(self._policy.required_engine_gates),
            "allowed_market_types": list(self._policy.allowed_market_types),
            "selected_engine_gate_results": self._selected_engine_gate_results(
                signal
            ),
            "us_event_slug": us_event_slug or None,
            "us_market_slug": None,
            "us_entry_cost": None,
            "us_execution_edge": None,
        }
        if candidate is not None:
            item.update(
                us_event_slug=str(candidate.us_event.get("slug") or "") or None,
                us_market_slug=str(candidate.market.get("slug") or "") or None,
                us_entry_cost=candidate.entry_cost,
                us_execution_edge=candidate.execution_edge,
            )
        return item

    def _qualification_rejection(
        self,
        event: Event,
        signal: Signal | None,
        reason: str,
        *,
        us_event_slug: str = "",
    ) -> None:
        key = ":".join((
            event.id,
            signal.market if signal else "event",
            signal.outcome if signal else "",
            reason,
        ))
        now = self._clock()
        last = self._qualification_seen.get(key, 0.0)
        if now - last < self._policy.candidate_cooldown_seconds:
            return
        self._qualification_seen[key] = now
        if len(self._qualification_seen) > 5_000:
            oldest = sorted(self._qualification_seen, key=self._qualification_seen.get)
            for stale in oldest[:1_000]:
                self._qualification_seen.pop(stale, None)
        self._journal(
            "qualification",
            "rejected",
            event=event,
            signal=signal,
            payload={
                "reason": reason,
                "us_event_slug": us_event_slug or None,
                "signal_market": signal.market if signal else None,
                "signal_outcome": signal.outcome if signal else None,
                "signal_edge": signal.edge if signal else None,
                "required_edge": signal.required_edge if signal else None,
                "configured_min_edge": self._policy.min_edge,
                "configured_max_edge": self._policy.max_edge,
                "signal_quality": signal.confidence if signal else None,
                "configured_min_quality": self._policy.min_signal_quality,
                "reference_sources": signal.n_reference_sources if signal else None,
                "configured_min_reference_sources": (
                    self._policy.min_reference_sources
                ),
                "execution_mode": self._policy.execution_mode,
                "require_engine_entry": self._policy.require_engine_entry,
                "required_engine_gates": list(
                    self._policy.required_engine_gates
                ),
                "allowed_market_types": list(
                    self._policy.allowed_market_types
                ),
                "selected_engine_gate_results": (
                    self._selected_engine_gate_results(signal)
                    if signal else []
                ),
                "engine_action": signal.action if signal else None,
            },
        )

    def _selected_engine_gate_results(self, signal: Signal) -> list[dict[str, Any]]:
        selected = set(self._policy.required_engine_gates)
        return [
            dict(gate)
            for gate in signal.gate_results or []
            if str(gate.get("code") or "") in selected
        ]

    def _engine_gate_blocker(self, signal: Signal) -> str:
        policy = self._policy
        if policy.require_engine_entry:
            return (
                ""
                if signal.action == "PAPER_BET"
                else "strict engine mode requires every entry gate to clear"
            )
        by_code: dict[str, dict[str, Any]] = {}
        duplicates: set[str] = set()
        for gate in signal.gate_results or []:
            code = str(gate.get("code") or "")
            if code in by_code:
                duplicates.add(code)
            else:
                by_code[code] = dict(gate)
        failures: list[str] = []
        for code in policy.required_engine_gates:
            gate = by_code.get(code)
            if code in duplicates:
                failures.append(f"{code}=duplicate")
            elif gate is None:
                failures.append(f"{code}=missing")
            elif gate.get("passed") is not True:
                status = str(gate.get("status") or "unknown").casefold()
                failures.append(f"{code}={status}")
        if not failures:
            return ""
        return "selected engine gate(s) did not clear: " + ", ".join(failures)

    def _map_signal(
        self,
        event: Event,
        signal: Signal,
        us_event: Mapping[str, Any],
        event_score: float,
        *,
        game_state: GameState | None = None,
    ) -> tuple[MappedCandidate | None, str]:
        policy = self._policy
        market_type = _market_kind(signal.market)
        if market_type not in policy.allowed_market_types:
            return None, (
                f"{market_type or 'unknown'} lines are disabled by the "
                "automatic-entry line-type policy"
            )
        engine_gate_blocker = self._engine_gate_blocker(signal)
        if engine_gate_blocker:
            return None, engine_gate_blocker
        probability = _signal_probability(signal)
        if probability is None:
            return None, "model probability is unavailable"
        # A permissive dry run is specifically intended to test the local US
        # venue. Its authenticated execution edge is checked below after exact
        # mapping, so a stale or differently-priced global display edge should
        # not prevent the experiment. Live mode and strict engine mode retain
        # the positive source-signal gate.
        enforce_source_edge = (
            policy.execution_mode == "live" or policy.require_engine_entry
        )
        if enforce_source_edge and (signal.edge <= 0 or signal.edge < policy.min_edge):
            return None, (
                f"existing signal edge {signal.edge * 100:+.1f}c is below "
                f"the configured {policy.min_edge * 100:+.1f}c floor"
            )
        if signal.edge > policy.max_edge:
            return None, (
                f"existing signal edge {signal.edge * 100:+.1f}c exceeds "
                f"the configured {policy.max_edge * 100:+.1f}c ceiling"
            )
        if signal.confidence < policy.min_signal_quality:
            return None, (
                f"existing signal quality {signal.confidence:.0f}/100 is below "
                f"the configured {policy.min_signal_quality:.0f}/100 floor"
            )
        if signal.n_reference_sources < policy.min_reference_sources:
            return None, (
                f"{signal.n_reference_sources} independent reference source "
                f"families is below the configured minimum of "
                f"{policy.min_reference_sources}"
            )
        if signal.observed_at is None:
            return None, "signal timestamp is unavailable"
        age = self._clock() - signal.observed_at.timestamp()
        if age < -5 or age > 120:
            return None, "signal is stale or future-dated"

        found: list[tuple[dict[str, Any], dict[str, Any], float]] = []
        for raw_market in us_event.get("markets", []):
            if not isinstance(raw_market, Mapping):
                continue
            if (
                not raw_market.get("active", True)
                or raw_market.get("closed")
                or raw_market.get("hidden")
                or str(raw_market.get("state") or "OPEN").upper()
                not in {"OPEN", "MARKET_STATE_OPEN", "EP3_STATUS_OPEN"}
            ):
                continue
            selected = _selection_side(event, signal, raw_market)
            if selected is not None:
                side, score = selected
                found.append((dict(raw_market), side, score))
        if len(found) != 1:
            return None, (
                "no exact US market/line/outcome mapping"
                if not found
                else "US market mapping is ambiguous"
            )
        market, side, selection_score = found[0]
        prices = _book_prices(market, side)
        if prices is None:
            return None, "complete executable US bid/ask is unavailable"
        entry_cost, order_long_price, exit_value, spread = prices
        if not policy.min_entry_price <= entry_cost <= policy.max_entry_price:
            return None, "US buy price is outside the configured entry bracket"
        if not MIN_ENTRY_PRICE < entry_cost < MAX_ENTRY_PRICE:
            return None, "US buy price is outside the established 5c–95c hard bracket"
        if spread > policy.max_spread:
            return None, "US bid/ask spread is wider than the configured maximum"
        execution_edge = probability - entry_cost
        required = max(policy.min_edge, signal.required_edge)
        if (
            execution_edge < required
            and (
                policy.execution_mode == "live"
                or policy.require_engine_entry
            )
        ):
            return None, (
                f"US execution edge {execution_edge * 100:+.1f}c is below "
                f"the required {required * 100:+.1f}c"
            )
        if execution_edge > policy.max_edge:
            return None, (
                f"US execution edge {execution_edge * 100:+.1f}c exceeds "
                f"the configured {policy.max_edge * 100:+.1f}c ceiling"
            )
        game_fraction = _baseball_fraction_remaining(event, game_state)
        if policy.min_mlb_fraction_remaining > 0:
            identity = f"{event.sport} {event.league}".casefold()
            if "baseball" in identity or "mlb" in identity:
                if game_fraction is None:
                    return None, (
                        "MLB game progress is unavailable while the configured "
                        "entry-stage filter requires explicit inning state"
                    )
                if game_fraction < policy.min_mlb_fraction_remaining:
                    return None, (
                        f"MLB game has {game_fraction * 100:.0f}% of regulation "
                        f"remaining, below the configured "
                        f"{policy.min_mlb_fraction_remaining * 100:.0f}% floor"
                    )
        position_side = "long" if side.get("long") else "short"
        return MappedCandidate(
            event=event,
            signal=signal,
            us_event=dict(us_event),
            market=market,
            position_side=position_side,
            # Store the established engine selection rather than the venue's
            # generic "Yes" label used by outcome-specific binary contracts.
            selection=str(signal.outcome or _side_description(side)),
            entry_cost=entry_cost,
            order_long_price=order_long_price,
            exit_value=exit_value,
            spread=spread,
            book_shares=0.0,
            execution_edge=execution_edge,
            mapping_score=min(event_score, selection_score),
            game_fraction_remaining=game_fraction,
        ), ""

    def _attempt_entry(
        self,
        candidate: MappedCandidate,
        *,
        generation: int,
        control_token: str,
    ) -> tuple[bool, str, str | None, dict[str, Any]]:
        if not self._cycle_is_current(generation):
            return (
                False,
                "automation_stopped",
                "automation was stopped before the candidate could be evaluated",
                {},
            )
        if self._position_exists(candidate):
            return (
                False,
                "position_open",
                "a managed position is already open",
                {},
            )
        policy = self._policy
        public_entry_cost = candidate.entry_cost
        open_positions = self.positions(open_only=True)
        total_exposure = sum(float(row["cost_basis"]) for row in open_positions)
        event_exposure = sum(
            float(row["cost_basis"])
            for row in open_positions
            if row["event_id"] == candidate.event.id
        )
        risk_limiters = self._risk_limiter_snapshot()
        daily_loss = risk_limiters["realized_loss_24h_usd"]
        orders_last_hour = risk_limiters["orders_last_hour"]
        recent_event_entries = self._event_entries_last_hour(
            candidate.event.id,
            policy.execution_mode,
        )
        candidate = replace(
            candidate,
            event_entries_60m=recent_event_entries + 1,
        )
        reasons = []
        if (
            policy.execution_mode == "live"
            and self._last_venue_sync_error is not None
        ):
            reasons.append(
                "authenticated venue position sync is unavailable; live entry "
                "is paused to avoid account-state divergence"
            )
        if len(open_positions) >= policy.max_open_positions:
            reasons.append("maximum open positions reached")
        if daily_loss >= policy.max_daily_loss_usd:
            reasons.append("daily realized-loss stop reached")
        if orders_last_hour >= policy.max_orders_per_hour:
            reasons.append("hourly order limit reached")
        if recent_event_entries >= policy.max_entries_per_event_per_hour:
            reasons.append(
                f"event already has {recent_event_entries} managed entries in "
                "the last hour, reaching the configured per-event limit of "
                f"{policy.max_entries_per_event_per_hour}"
            )
        capacity = min(
            policy.max_position_usd,
            policy.max_total_exposure_usd - total_exposure,
            policy.max_event_exposure_usd - event_exposure,
        )
        if capacity <= 0:
            reasons.append("exposure capacity is exhausted")

        account: dict[str, Any] = {}
        book: dict[str, Any] = {}
        balance: float | None = None
        book_quote = ExecutableBookQuote("", None, None, None, None, None, None, 0.0)
        venue_checked = False
        if not reasons:
            if not self._cycle_is_current(generation):
                return (
                    False,
                    "automation_stopped",
                    "automation was stopped before the venue check",
                    {},
                )
            if not self._key_id or not self._secret_key:
                reasons.append("Polymarket US credentials are not configured")
            else:
                try:
                    with self._client() as client:
                        account = client.account.balances()
                        book = client.markets.book(candidate.market["slug"])
                    venue_checked = True
                except Exception as exc:
                    reasons.append(f"venue check failed: {self._safe_error(exc)}")

        if venue_checked:
            balance = self._buying_power(account)
            book_quote = _executable_book_quote(book, candidate.position_side)
            if balance is None:
                reasons.append("buying power is unavailable")
            elif balance - policy.minimum_cash_reserve_usd <= 0:
                reasons.append(
                    f"buying power ${balance:.2f} would breach the configured "
                    f"${policy.minimum_cash_reserve_usd:.2f} cash reserve"
                )
            else:
                capacity = min(capacity, balance - policy.minimum_cash_reserve_usd)
            if book_quote.state != "MARKET_STATE_OPEN":
                reasons.append(
                    f"authenticated US order book state is "
                    f"{book_quote.state or 'missing'}; expected MARKET_STATE_OPEN"
                )
            if (
                book_quote.entry_cost is None
                or book_quote.order_long_price is None
                or book_quote.spread is None
            ):
                reasons.append(
                    "authenticated US order book has no complete executable "
                    "bid/offer pair"
                )
            else:
                probability = _signal_probability(candidate.signal)
                assert probability is not None
                execution_edge = probability - book_quote.entry_cost
                candidate = replace(
                    candidate,
                    entry_cost=book_quote.entry_cost,
                    order_long_price=book_quote.order_long_price,
                    exit_value=book_quote.exit_value,
                    spread=book_quote.spread,
                    book_shares=book_quote.depth,
                    execution_edge=execution_edge,
                )
                if not policy.min_entry_price <= candidate.entry_cost <= policy.max_entry_price:
                    reasons.append(
                        f"authenticated US buy price "
                        f"{candidate.entry_cost * 100:.1f}c is outside the "
                        f"configured {policy.min_entry_price * 100:.1f}c-"
                        f"{policy.max_entry_price * 100:.1f}c bracket"
                    )
                if not MIN_ENTRY_PRICE < candidate.entry_cost < MAX_ENTRY_PRICE:
                    reasons.append(
                        f"authenticated US buy price "
                        f"{candidate.entry_cost * 100:.1f}c is outside the "
                        "established 5c-95c hard bracket"
                    )
                if candidate.spread > policy.max_spread:
                    reasons.append(
                        f"authenticated US spread {candidate.spread * 100:.1f}c "
                        f"exceeds the configured {policy.max_spread * 100:.1f}c "
                        "maximum"
                    )
                required_edge = max(
                    policy.min_edge,
                    candidate.signal.required_edge,
                )
                if candidate.execution_edge < required_edge:
                    reasons.append(
                        f"authenticated US execution edge "
                        f"{candidate.execution_edge * 100:+.1f}c is below the "
                        f"required {required_edge * 100:+.1f}c"
                    )
                if candidate.execution_edge > policy.max_edge:
                    reasons.append(
                        f"authenticated US execution edge "
                        f"{candidate.execution_edge * 100:+.1f}c exceeds the "
                        f"configured {policy.max_edge * 100:+.1f}c ceiling"
                    )
                if candidate.book_shares < policy.min_book_shares:
                    reasons.append(
                        f"executable top-of-book depth "
                        f"{candidate.book_shares:.2f} shares is below the "
                        f"configured {policy.min_book_shares:.2f} at "
                        f"{candidate.entry_cost * 100:.1f}c"
                    )
        audit = {
            "risk_preset": policy.risk_preset,
            "risk_preset_version": policy.risk_preset_version,
            "trading_allocation_usd": policy.trading_allocation_usd,
            "public_entry_cost": public_entry_cost,
            "authenticated_book_state": book_quote.state or None,
            "authenticated_best_bid": book_quote.best_bid,
            "authenticated_best_ask": book_quote.best_ask,
            "authenticated_entry_cost": book_quote.entry_cost,
            "authenticated_execution_edge": (
                (
                    _signal_probability(candidate.signal) - book_quote.entry_cost
                )
                if book_quote.entry_cost is not None
                and _signal_probability(candidate.signal) is not None
                else None
            ),
            "authenticated_spread": book_quote.spread,
            "executable_book_shares": book_quote.depth if venue_checked else None,
            "configured_min_book_shares": policy.min_book_shares,
            "configured_min_edge": policy.min_edge,
            "configured_max_edge": policy.max_edge,
            "required_edge": max(policy.min_edge, candidate.signal.required_edge),
            "configured_min_quality": policy.min_signal_quality,
            "configured_min_reference_sources": policy.min_reference_sources,
            "buying_power": balance,
            "available_capacity_usd": max(0.0, capacity),
            "event_entries_60m": candidate.event_entries_60m,
            "configured_max_entries_per_event_per_hour": (
                policy.max_entries_per_event_per_hour
            ),
            "game_fraction_remaining": candidate.game_fraction_remaining,
            "configured_min_mlb_fraction_remaining": (
                policy.min_mlb_fraction_remaining
            ),
        }
        if reasons:
            self._journal_candidate(
                candidate,
                "rejected",
                reasons=reasons,
                extra=audit,
            )
            return False, "blocked", " | ".join(reasons), audit
        if not self._cycle_is_current(generation):
            return (
                False,
                "automation_stopped",
                "automation was stopped before order sizing",
                audit,
            )

        # Sizing is execution policy, not a new probability calculation.  It
        # increases gradually with already-computed edge and quality, remains
        # capped by every bankroll limit, and never exceeds visible depth.
        edge_strength = min(1.0, max(0.0, candidate.execution_edge - policy.min_edge) / 0.10)
        quality_strength = min(1.0, max(0.0, candidate.signal.confidence) / 100.0)
        conviction = 0.5 * edge_strength + 0.5 * quality_strength
        stake = min(capacity, policy.max_position_usd * (0.25 + 0.75 * conviction))
        minimum_quantity = _amount(candidate.market.get("minimum_trade_quantity")) or 1.0
        # The official SDK defines order quantity as whole shares. Price tick is
        # deliberately not reused as a quantity increment.
        quantity = math.floor(
            min(candidate.book_shares, stake / candidate.entry_cost)
        )
        if quantity < minimum_quantity or quantity * candidate.entry_cost > capacity + 1e-8:
            self._journal_candidate(
                candidate,
                "rejected",
                reasons=["risk capacity cannot fund the venue minimum quantity"],
                extra=audit,
            )
            return (
                False,
                "blocked",
                "risk capacity cannot fund the venue minimum quantity",
                audit,
            )

        order = self._entry_order(candidate, quantity)
        if policy.execution_mode == "dry_run":
            if not self._execution_authority_is_current(
                generation,
                control_token,
                "dry_run",
            ):
                return (
                    False,
                    "automation_stopped",
                    "saved execution controls changed before the simulated fill",
                    audit,
                )
            self._create_position(
                candidate,
                quantity=quantity,
                entry_cost=candidate.entry_cost,
                mode="dry_run",
                order_id=None,
            )
            self._journal_candidate(
                candidate,
                "simulated_fill",
                extra={
                    **audit,
                    "quantity": quantity,
                    "stake": quantity * candidate.entry_cost,
                },
            )
            return True, "simulated_fill", None, audit
        if not self._execution_authority_is_current(
            generation,
            control_token,
            "live",
        ) or not self.is_armed():
            self._journal_candidate(
                candidate,
                "rejected",
                reasons=[
                    "live mode is disarmed, its selected latch expired, or a "
                    "newer server changed the saved execution controls"
                ],
                extra=audit,
            )
            return (
                False,
                "live_disarmed",
                "live mode is disarmed, its selected latch expired, or a "
                "newer server changed the saved execution controls",
                audit,
            )
        try:
            with self._client() as client:
                preview = client.orders.preview({"request": order})
                preview_fee = _preview_fee_per_share(
                    preview,
                    quantity=quantity,
                    entry_cost=candidate.entry_cost,
                )
                self._journal_candidate(
                    candidate,
                    "previewed",
                    extra={
                        **audit,
                        "quantity": quantity,
                        "preview_fee_per_share": preview_fee,
                        "preview": self._safe_payload(preview),
                    },
                )
                fee_adjusted_edge = candidate.execution_edge - (preview_fee or 0.0)
                required = max(self._policy.min_edge, candidate.signal.required_edge)
                if fee_adjusted_edge < required:
                    self._journal_candidate(
                        candidate,
                        "rejected",
                        reasons=["previewed fee-adjusted edge is below the policy floor"],
                        extra={
                            **audit,
                            "preview_fee_per_share": preview_fee,
                            "fee_adjusted_edge": fee_adjusted_edge,
                        },
                    )
                    return (
                        False,
                        "blocked",
                        "previewed fee-adjusted edge is below the policy floor",
                        audit,
                    )
                if (
                    not self._execution_authority_is_current(
                        generation,
                        control_token,
                        "live",
                    )
                    or not self.is_armed()
                ):
                    self._journal_candidate(
                        candidate,
                        "rejected",
                        reasons=[
                            "automation was stopped or live execution was disarmed "
                            "after preview"
                        ],
                        extra=audit,
                    )
                    return (
                        False,
                        "automation_stopped",
                        "automation was stopped or live execution was disarmed "
                        "after preview",
                        audit,
                    )
                response = client.orders.create(order)
        except Exception as exc:
            self._journal_candidate(
                candidate,
                "order_error",
                reasons=[self._safe_error(exc)],
                extra=audit,
            )
            return False, "order_error", self._safe_error(exc), audit
        order_id = str(response.get("id") or "")
        shares, long_fill_price, execution_fees = _order_fill(
            response, candidate.order_long_price
        )
        if shares <= 0:
            if order_id:
                self._record_order(
                    order_id, candidate.market["slug"], None, "entry", "unfilled"
                )
            self._journal_candidate(
                candidate,
                "unfilled",
                extra={**audit, "order_id": order_id or None},
            )
            return (
                False,
                "unfilled",
                "fill-or-kill order returned no filled shares",
                audit,
            )
        raw_fill_cost = (
            long_fill_price
            if candidate.position_side == "long"
            else 1 - long_fill_price
        )
        fill_cost = raw_fill_cost + execution_fees / shares
        position_id = self._create_position(
            candidate,
            quantity=shares,
            entry_cost=fill_cost,
            mode="live",
            order_id=order_id or None,
        )
        if order_id:
            self._record_order(
                order_id, candidate.market["slug"], position_id, "entry", "filled"
            )
        self._journal_candidate(
            candidate,
            "live_fill",
            extra={
                **audit,
                "order_id": order_id or None,
                "quantity": shares,
                "entry_cost": fill_cost,
                "stake": shares * fill_cost,
                "execution_fees": execution_fees,
            },
        )
        return True, "live_fill", None, audit

    def _mark_and_exit(
        self,
        monitored: list[tuple[Event, Iterable[Signal]]],
        us_events: list[Mapping[str, Any]],
        *,
        generation: int,
        control_token: str,
        game_states: Mapping[str, GameState],
    ) -> tuple[int, int, int]:
        monitored_by_id = {
            event.id: (event, list(signals)) for event, signals in monitored
        }
        markets = {
            str(market.get("slug")): dict(market)
            for event in us_events
            for market in event.get("markets", [])
            if isinstance(market, Mapping) and market.get("slug")
        }
        open_positions = self.positions(open_only=True)
        authenticated_books: dict[str, Mapping[str, Any]] = {}
        book_errors: dict[str, str] = {}
        live_slugs = {
            str(position["market_slug"])
            for position in open_positions
            if position["mode"] == "live"
        }
        if live_slugs:
            try:
                with self._client() as client:
                    for slug in live_slugs:
                        try:
                            book = client.markets.book(slug)
                            if isinstance(book, Mapping):
                                authenticated_books[slug] = book
                            else:
                                book_errors[slug] = (
                                    "authenticated order book response was invalid"
                                )
                        except Exception as exc:
                            book_errors[slug] = self._safe_error(exc)
            except Exception as exc:
                error = self._safe_error(exc)
                book_errors.update(
                    (slug, error)
                    for slug in live_slugs
                    if slug not in authenticated_books
                )
        marked = 0
        exited = 0
        for position in open_positions:
            if not self._cycle_is_current(generation):
                break
            market = markets.get(position["market_slug"])
            probability = (
                self._position_probability(position, market, monitored_by_id)
                if market is not None
                else None
            )
            quantity = float(position["quantity"])
            exit_depth: float | None = None
            quote_source = "public_us_snapshot"
            if position["mode"] == "live":
                book = authenticated_books.get(str(position["market_slug"]))
                size_quote = _size_aware_exit_quote(
                    book,
                    str(position["position_side"]),
                    quantity,
                )
                if size_quote is None:
                    reason = book_errors.get(
                        str(position["market_slug"]),
                        "authenticated order book cannot fill the complete position",
                    )
                    self._journal(
                        "mark",
                        "unavailable",
                        event_id=position["event_id"],
                        event_name=position["event_name"],
                        market_slug=position["market_slug"],
                        selection=position["selection"],
                        payload={
                            "position_id": position["id"],
                            "reason": reason,
                            "quantity": quantity,
                            "quote_source": "authenticated_us_book",
                        },
                    )
                    continue
                state, exit_value, exit_long_price, exit_depth = size_quote
                if state != "MARKET_STATE_OPEN":
                    self._journal(
                        "mark",
                        "unavailable",
                        event_id=position["event_id"],
                        event_name=position["event_name"],
                        market_slug=position["market_slug"],
                        selection=position["selection"],
                        payload={
                            "position_id": position["id"],
                            "reason": (
                                "authenticated order book is not open "
                                f"({state or 'unknown'})"
                            ),
                            "quote_source": "authenticated_us_book",
                        },
                    )
                    continue
                entry_quote = _executable_book_quote(
                    book,
                    str(position["position_side"]),
                )
                current_edge = (
                    probability - entry_quote.entry_cost
                    if probability is not None
                    and entry_quote.entry_cost is not None
                    else None
                )
                quote_source = "authenticated_us_book_size_aware"
            elif market is None:
                self._journal(
                    "mark",
                    "unavailable",
                    event_id=position["event_id"],
                    event_name=position["event_name"],
                    market_slug=position["market_slug"],
                    selection=position["selection"],
                    payload={"reason": "current US market snapshot unavailable"},
                )
                continue
            else:
                sides = [
                    side for side in market.get("sides", [])
                    if isinstance(side, Mapping)
                    and bool(side.get("long"))
                    == (position["position_side"] == "long")
                ]
                if len(sides) != 1:
                    continue
                prices = _book_prices(market, sides[0])
                if prices is None:
                    continue
                entry_cost_now, _, exit_value, _ = prices
                if exit_value is None:
                    continue
                # The API expresses both LONG and SHORT order limits as a
                # LONG/YES price.
                exit_long_price = _amount(
                    market.get(
                        "long_best_bid"
                        if position["position_side"] == "long"
                        else "long_best_ask"
                    )
                )
                if exit_long_price is None:
                    continue
                current_edge = (
                    probability - entry_cost_now
                    if probability is not None
                    else None
                )
            entry_cost = float(position["entry_cost"])
            return_fraction = exit_value / entry_cost - 1.0
            peak = max(float(position["highest_exit_value"]), exit_value)
            held_minutes = (
                self._clock() - float(position["opened_ts"])
            ) / 60.0
            event_context = monitored_by_id.get(str(position["event_id"]))
            event = event_context[0] if event_context is not None else None
            if self._policy.adaptive_exit_enabled:
                adaptive_exit = self._adaptive_exit.observe(
                    position=position,
                    event=event,
                    state=game_states.get(str(position["event_id"])),
                    exit_value=exit_value,
                    highest_exit_value=peak,
                    return_fraction=return_fraction,
                    current_edge=current_edge,
                    profile=self._policy.adaptive_exit_profile,
                    horizon_seconds=int(
                        self._policy.adaptive_exit_horizon_minutes * 60
                    ),
                    minimum_samples=self._policy.adaptive_exit_min_samples,
                    maximum_tightening=(
                        self._policy.adaptive_exit_max_tightening
                    ),
                    profit_target=self._policy.profit_target,
                    trailing_drawdown=self._policy.trailing_drawdown,
                    exit_edge=self._policy.exit_edge,
                    stop_loss=self._policy.stop_loss,
                )
            else:
                adaptive_exit = self._adaptive_exit.base_decision(
                    profile=self._policy.adaptive_exit_profile,
                    reason="adaptive MLB exit learning is disabled",
                    applicable=False,
                    profit_target=self._policy.profit_target,
                    trailing_drawdown=self._policy.trailing_drawdown,
                    exit_edge=self._policy.exit_edge,
                    stop_loss=self._policy.stop_loss,
                )
            (
                stop_reason,
                stop_guard,
                stop_triggered_ts,
                stop_observation_count,
                stop_low_exit_value,
            ) = self._stop_guard_decision(
                position,
                event=event,
                state=game_states.get(str(position["event_id"])),
                exit_value=exit_value,
                current_edge=current_edge,
                return_fraction=return_fraction,
                adaptive_exit=adaptive_exit,
            )
            prior_lock = position.get("profit_lock_armed_ts") is not None
            target_hit = (
                return_fraction
                >= adaptive_exit.effective_profit_target
            )
            prior_target_count = int(
                position.get("profit_target_observation_count") or 0
            )
            prior_target_ts = position.get("profit_target_observed_ts")
            confirmation_window = max(
                120.0,
                float(self._policy.cycle_seconds) * 3.0,
            )
            consecutive_target = (
                target_hit
                and prior_target_count > 0
                and prior_target_ts is not None
                and self._clock() - float(prior_target_ts) <= confirmation_window
            )
            if prior_lock:
                target_count = max(
                    PROFIT_TARGET_CONFIRMATION_READINGS,
                    prior_target_count,
                )
                target_observed_ts = prior_target_ts
                target_observed_price = position.get(
                    "profit_target_observed_price"
                )
            elif target_hit:
                target_count = (
                    prior_target_count + 1 if consecutive_target else 1
                )
                target_observed_ts = (
                    prior_target_ts if consecutive_target else self._clock()
                )
                target_observed_price = max(
                    float(position.get("profit_target_observed_price") or 0.0),
                    exit_value,
                )
            else:
                # Confirmation requires consecutive executable readings. A
                # quote that falls back below target resets an unarmed attempt.
                target_count = 0
                target_observed_ts = None
                target_observed_price = None
            target_confirmed = (
                target_hit
                and (
                    target_count >= PROFIT_TARGET_CONFIRMATION_READINGS
                    or held_minutes >= self._policy.min_hold_minutes
                )
            )
            profit_lock_armed = prior_lock or target_confirmed
            new_lock_ts = (
                self._clock()
                if profit_lock_armed and not prior_lock
                else None
            )
            self._update_mark(
                position["id"],
                exit_value=exit_value,
                peak=peak,
                probability=probability,
                edge=current_edge,
                return_fraction=return_fraction,
                profit_lock_armed_ts=new_lock_ts,
                profit_lock_price=peak if new_lock_ts is not None else None,
                profit_target_observed_ts=target_observed_ts,
                profit_target_observation_count=target_count,
                profit_target_observed_price=target_observed_price,
                adaptive_exit_payload=json.dumps(
                    adaptive_exit.payload(),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                stop_triggered_ts=stop_triggered_ts,
                stop_observation_count=stop_observation_count,
                stop_low_exit_value=stop_low_exit_value,
                stop_guard_payload=json.dumps(
                    stop_guard,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            marked += 1
            self._journal(
                "mark",
                "updated",
                event_id=position["event_id"],
                event_name=position["event_name"],
                market_slug=position["market_slug"],
                selection=position["selection"],
                payload={
                    "position_id": position["id"],
                    "exit_value": exit_value,
                    "highest_exit_value": peak,
                    "return_fraction": return_fraction,
                    "execution_edge": current_edge,
                    "quantity": quantity,
                    "estimated_cashout_value": exit_value * quantity,
                    "exit_book_depth": exit_depth,
                    "quote_source": quote_source,
                    "profit_lock_armed": profit_lock_armed,
                    "profit_target_hit": target_hit,
                    "profit_target_observation_count": target_count,
                    "profit_target_confirmation_readings": (
                        PROFIT_TARGET_CONFIRMATION_READINGS
                    ),
                    "profit_target_confirmed": target_confirmed,
                    "base_profit_target": (
                        adaptive_exit.base_profit_target
                    ),
                    "effective_profit_target": (
                        adaptive_exit.effective_profit_target
                    ),
                    "adaptive_exit": adaptive_exit.payload(),
                    "stop_guard": stop_guard,
                    "held_minutes": held_minutes,
                    "venue_sync_status": position.get("venue_sync_status"),
                },
            )
            reason = self._exit_reason(
                position,
                exit_value,
                peak,
                current_edge,
                profit_lock_armed=profit_lock_armed,
                adaptive_exit=adaptive_exit,
                stop_reason=stop_reason,
            )
            if (
                reason
                and self._policy.auto_cashout
                and self._cycle_is_current(generation)
            ):
                if not self._execution_authority_is_current(
                    generation,
                    control_token,
                    str(self._policy.execution_mode),
                ):
                    break
                if (
                    position["mode"] == "live"
                    and str(position.get("venue_sync_status") or "")
                    in {
                        "sync_error",
                        "mismatch_pending_confirmation",
                        "entry_settlement_grace",
                    }
                ):
                    self._journal(
                        "exit",
                        "blocked",
                        event_id=position["event_id"],
                        event_name=position["event_name"],
                        market_slug=position["market_slug"],
                        selection=position["selection"],
                        payload={
                            "position_id": position["id"],
                            "reason": (
                                "venue quantity is not yet confirmed; protective "
                                "cash-out will retry after reconciliation"
                            ),
                            "venue_sync_status": position.get("venue_sync_status"),
                            "exit_trigger": reason,
                        },
                    )
                    continue
                exited += int(
                    self._attempt_exit(
                        position,
                        market,
                        exit_value=exit_value,
                        order_long_price=exit_long_price,
                        reason=reason,
                        protective=True,
                    )
                )
        shadow_marks = self._mark_exit_recoveries(markets, game_states)
        return marked, exited, shadow_marks

    def _mark_exit_recoveries(
        self,
        markets: Mapping[str, Mapping[str, Any]],
        game_states: Mapping[str, GameState],
    ) -> int:
        """Continue observing sold contracts without submitting hypothetical orders."""
        marked = 0
        now = self._clock()
        for shadow in self._adaptive_exit.active_exit_recoveries():
            market = markets.get(str(shadow["market_slug"]))
            exit_value: float | None = None
            if market is not None:
                sides = [
                    side
                    for side in market.get("sides", [])
                    if isinstance(side, Mapping)
                    and bool(side.get("long"))
                    == (str(shadow["position_side"]) == "long")
                ]
                if len(sides) == 1:
                    prices = _book_prices(market, sides[0])
                    if prices is not None:
                        exit_value = prices[2]
            state = game_states.get(str(shadow["event_id"]))
            terminal = bool(
                state
                and (
                    state.ended
                    or str(state.status or "").casefold()
                    in {"final", "ended", "complete", "completed", "finished"}
                )
            )
            matured = now >= (
                float(shadow["exit_ts"])
                + float(shadow["horizon_seconds"])
            )
            if exit_value is None and not (terminal or matured):
                continue
            value = (
                float(exit_value)
                if exit_value is not None
                else float(
                    shadow.get("last_exit_value")
                    or shadow["exit_value"]
                )
            )
            result = self._adaptive_exit.observe_exit_recovery(
                str(shadow["position_id"]),
                exit_value=value,
                terminal=terminal or matured,
            )
            if result is not None:
                marked += 1
        return marked

    @staticmethod
    def _position_probability(
        position: Mapping[str, Any],
        market: Mapping[str, Any],
        monitored_by_id: Mapping[str, tuple[Event, list[Signal]]],
    ) -> float | None:
        context = monitored_by_id.get(str(position["event_id"]))
        if context is None:
            return None
        event, signals = context
        matching: list[float] = []
        for signal in signals:
            selected = _selection_side(event, signal, market)
            if selected is None:
                continue
            side, _ = selected
            if bool(side.get("long")) != (position["position_side"] == "long"):
                continue
            probability = _signal_probability(signal)
            if probability is not None:
                matching.append(probability)
        return matching[0] if len(matching) == 1 else None

    def _stop_guard_decision(
        self,
        position: Mapping[str, Any],
        *,
        event: Event | None,
        state: GameState | None,
        exit_value: float,
        current_edge: float | None,
        return_fraction: float,
        adaptive_exit: AdaptiveExitDecision,
    ) -> tuple[str | None, dict[str, Any], float | None, int, float | None]:
        """Confirm an ordinary MLB stop while preserving a catastrophic bound."""
        policy = self._policy
        prior_trigger = _amount(position.get("stop_triggered_ts"))
        prior_count = int(position.get("stop_observation_count") or 0)
        prior_low = _amount(position.get("stop_low_exit_value"))
        stop_hit = return_fraction <= -policy.stop_loss
        base_payload: dict[str, Any] = {
            "enabled": policy.volatility_stop_enabled,
            "status": "inactive",
            "stop_hit": stop_hit,
            "return_fraction": return_fraction,
            "configured_stop_loss": policy.stop_loss,
            "current_edge": current_edge,
            "engine_probability_unchanged": True,
        }
        if not stop_hit:
            if prior_count:
                base_payload.update(
                    status="recovered_before_exit",
                    prior_confirmations=prior_count,
                    prior_low_exit_value=prior_low,
                )
            return None, base_payload, None, 0, None
        if not policy.volatility_stop_enabled:
            base_payload.update(
                status="immediate",
                reason="volatility-aware confirmation is disabled",
            )
            return (
                "hard_stop_loss",
                base_payload,
                prior_trigger,
                prior_count,
                min(exit_value, prior_low if prior_low is not None else exit_value),
            )

        context = _mlb_stop_context(event, state, position)
        if context is None:
            base_payload.update(
                status="immediate",
                reason="usable MLB inning state is unavailable",
            )
            return (
                "hard_stop_loss",
                base_payload,
                prior_trigger,
                prior_count,
                min(exit_value, prior_low if prior_low is not None else exit_value),
            )
        state_age = max(
            0.0,
            self._clock() - float(context.pop("state_received_ts")),
        )
        context["state_age_seconds"] = state_age
        base_payload["game_state"] = context
        if state_age > 180.0:
            base_payload.update(
                status="immediate",
                reason=f"MLB game state is stale ({state_age:.0f}s old)",
            )
            return (
                "hard_stop_loss",
                base_payload,
                prior_trigger,
                prior_count,
                min(exit_value, prior_low if prior_low is not None else exit_value),
            )
        if context["settled_in_favor"]:
            # A low quote conflicts with an already-cleared half-point total.
            # Selling automatically would turn stale/mismapped data into a real
            # loss, so require venue resolution or an operator decision.
            base_payload.update(
                status="held_state_price_conflict",
                reason=(
                    "the observed MLB score already clears this total in the "
                    "position's favor; the low executable quote conflicts with "
                    "game state"
                ),
            )
            return None, base_payload, None, 0, None

        catastrophic_loss = min(
            0.95,
            policy.stop_loss * policy.catastrophic_stop_multiplier,
        )
        base_payload["catastrophic_stop_loss"] = catastrophic_loss
        if return_fraction <= -catastrophic_loss:
            base_payload.update(
                status="immediate",
                reason="catastrophic loss boundary reached",
            )
            return (
                "catastrophic_stop_loss",
                base_payload,
                prior_trigger,
                prior_count,
                min(exit_value, prior_low if prior_low is not None else exit_value),
            )
        if context["terminal"] or context["structurally_lost"]:
            base_payload.update(
                status="immediate",
                reason=(
                    "game state makes the selected total irreversible"
                    if context["structurally_lost"]
                    else "game state is terminal"
                ),
            )
            return (
                "state_confirmed_stop_loss",
                base_payload,
                prior_trigger,
                prior_count,
                min(exit_value, prior_low if prior_low is not None else exit_value),
            )
        material_reversal = -max(0.03, policy.min_edge)
        if current_edge is not None and current_edge <= material_reversal:
            base_payload.update(
                status="immediate",
                reason="current model edge materially reversed",
                material_reversal_threshold=material_reversal,
            )
            return (
                "model_reversal_stop_loss",
                base_payload,
                prior_trigger,
                prior_count,
                min(exit_value, prior_low if prior_low is not None else exit_value),
            )

        now = self._clock()
        confirmation_window = max(
            policy.stop_grace_minutes * 120.0,
            float(policy.cycle_seconds)
            * float(policy.stop_confirmation_readings + 2),
        )
        consecutive = (
            prior_trigger is not None
            and now - prior_trigger <= confirmation_window
        )
        triggered_at = prior_trigger if consecutive else now
        confirmations = prior_count + 1 if consecutive else 1
        low = min(exit_value, prior_low if consecutive and prior_low is not None else exit_value)
        fraction = float(context["fraction_remaining"])
        grace_minutes = policy.stop_grace_minutes
        if fraction <= 0.12:
            grace_minutes = min(grace_minutes, 0.5)
        elif fraction <= 0.25:
            grace_minutes = min(grace_minutes, 1.0)
        predicted = adaptive_exit.predicted_adverse_probability
        if (
            predicted is not None
            and adaptive_exit.confidence >= 0.50
            and predicted >= 0.70
        ):
            grace_minutes *= 0.50
        if current_edge is None:
            # Missing reference/model context is not evidence that the thesis
            # reversed.  Keep the protection bounded, but do not turn a
            # transient upstream gap into a one-quote market sell.
            grace_minutes = min(grace_minutes, 0.75)
        if (
            current_edge is not None
            and current_edge <= adaptive_exit.effective_exit_edge
        ):
            grace_minutes = min(grace_minutes, 0.75)
        grace_seconds = max(
            float(policy.cycle_seconds),
            grace_minutes * 60.0,
        )
        elapsed = max(0.0, now - triggered_at)
        confirmed = (
            confirmations >= policy.stop_confirmation_readings
            and elapsed >= grace_seconds
        )
        base_payload.update(
            status="confirmed" if confirmed else "observing_recovery",
            reason=(
                "bounded confirmation window expired with the stop still breached"
                if confirmed
                else "MLB state remains live and model edge has not materially reversed"
            ),
            confirmations=confirmations,
            required_confirmations=policy.stop_confirmation_readings,
            triggered_at=datetime.fromtimestamp(
                triggered_at, timezone.utc
            ).isoformat(),
            elapsed_seconds=elapsed,
            grace_seconds=grace_seconds,
            stop_low_exit_value=low,
            adaptive_adverse_probability=predicted,
            adaptive_confidence=adaptive_exit.confidence,
            model_edge_available=current_edge is not None,
        )
        return (
            "confirmed_stop_loss" if confirmed else None,
            base_payload,
            triggered_at,
            confirmations,
            low,
        )

    def _exit_reason(
        self,
        position: Mapping[str, Any],
        exit_value: float,
        peak: float,
        current_edge: float | None,
        *,
        profit_lock_armed: bool = False,
        adaptive_exit: AdaptiveExitDecision | None = None,
        stop_reason: str | None = None,
    ) -> str | None:
        policy = self._policy
        effective_exit_edge = (
            adaptive_exit.effective_exit_edge
            if adaptive_exit is not None
            else policy.exit_edge
        )
        effective_trailing = (
            adaptive_exit.effective_trailing_drawdown
            if adaptive_exit is not None
            else policy.trailing_drawdown
        )
        edge_invalid = (
            current_edge is not None
            and current_edge <= effective_exit_edge
        )
        trailing = (
            peak > 0
            and exit_value <= peak * (1.0 - effective_trailing)
        )
        if stop_reason:
            return stop_reason
        # Reaching the target arms a durable lock. It exits only after later
        # edge decay or a material pullback from the observed high, so a one
        # cent uptick is not treated as a successful scalp.
        if (
            profit_lock_armed
            and (edge_invalid or trailing)
        ):
            return "profit_lock_after_edge_decay" if edge_invalid else "trailing_profit_lock"
        if current_edge is not None and current_edge <= -max(0.03, policy.min_edge):
            return "model_reversal"
        return None

    def _attempt_exit(
        self,
        position: Mapping[str, Any],
        market: Mapping[str, Any],
        *,
        exit_value: float,
        order_long_price: float,
        reason: str,
        protective: bool = False,
    ) -> bool:
        quantity = float(position["quantity"])
        candidate_key = ":".join((
            str(position["event_id"]),
            str(position["market_slug"]),
            str(position["position_side"]),
        ))
        if position["mode"] == "dry_run":
            self._close_position(
                position["id"], exit_value=exit_value, reason=reason, order_id=None
            )
            self._adaptive_exit.track_exit(
                position=position,
                exit_value=exit_value,
                reason=reason,
                horizon_seconds=int(
                    self._policy.post_exit_tracking_minutes * 60
                ),
            )
            self._journal(
                "exit",
                "simulated_fill",
                event_id=position["event_id"],
                event_name=position["event_name"],
                market_slug=position["market_slug"],
                selection=position["selection"],
                payload={
                    "position_id": position["id"],
                    "reason": reason,
                    "quantity": quantity,
                    "exit_value": exit_value,
                },
            )
            self._candidate_seen[candidate_key] = self._clock()
            return True
        live_authorized = (
            self._protective_exits_armed
            if protective
            else self.is_armed()
        )
        if not live_authorized:
            self._journal(
                "exit",
                "blocked",
                event_id=position["event_id"],
                event_name=position["event_name"],
                market_slug=position["market_slug"],
                selection=position["selection"],
                payload={
                    "reason": (
                        "protective auto-exits are disarmed"
                        if protective
                        else "live entry/order latch is disarmed"
                    ),
                    "exit_trigger": reason,
                    "protective_exit": protective,
                },
            )
            return False
        intent = (
            "ORDER_INTENT_SELL_LONG"
            if position["position_side"] == "long"
            else "ORDER_INTENT_SELL_SHORT"
        )
        order = {
            "marketSlug": position["market_slug"],
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": _price_amount(order_long_price),
            "quantity": quantity,
            "tif": "TIME_IN_FORCE_FILL_OR_KILL",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": True,
            "maxBlockTime": "5",
        }
        try:
            with self._client() as client:
                preview = client.orders.preview({"request": order})
                self._journal(
                    "exit",
                    "previewed",
                    event_id=position["event_id"],
                    event_name=position["event_name"],
                    market_slug=position["market_slug"],
                    selection=position["selection"],
                    payload={
                        "position_id": position["id"],
                        "reason": reason,
                        "preview": self._safe_payload(preview),
                    },
                )
                response = client.orders.create(order)
        except Exception as exc:
            self._journal(
                "exit",
                "order_error",
                event_id=position["event_id"],
                event_name=position["event_name"],
                market_slug=position["market_slug"],
                selection=position["selection"],
                payload={"reason": reason, "error": self._safe_error(exc)},
            )
            return False
        order_id = str(response.get("id") or "")
        shares, long_fill_price, execution_fees = _order_fill(
            response, order_long_price
        )
        if shares + 1e-8 < quantity:
            if order_id:
                self._record_order(
                    order_id, position["market_slug"], position["id"], "exit", "unfilled"
                )
            self._journal(
                "exit",
                "unfilled",
                event_id=position["event_id"],
                event_name=position["event_name"],
                market_slug=position["market_slug"],
                selection=position["selection"],
                payload={"reason": reason, "order_id": order_id or None},
            )
            return False
        raw_fill_value = (
            long_fill_price
            if position["position_side"] == "long"
            else 1 - long_fill_price
        )
        fill_value = raw_fill_value - execution_fees / shares
        self._close_position(
            position["id"], exit_value=fill_value, reason=reason, order_id=order_id or None
        )
        self._adaptive_exit.track_exit(
            position=position,
            exit_value=fill_value,
            reason=reason,
            horizon_seconds=int(
                self._policy.post_exit_tracking_minutes * 60
            ),
        )
        if order_id:
            self._record_order(
                order_id, position["market_slug"], position["id"], "exit", "filled"
            )
        self._journal(
            "exit",
            "live_fill",
            event_id=position["event_id"],
            event_name=position["event_name"],
            market_slug=position["market_slug"],
            selection=position["selection"],
            payload={
                "position_id": position["id"],
                "reason": reason,
                "order_id": order_id or None,
                "quantity": shares,
                "exit_value": fill_value,
                "execution_fees": execution_fees,
            },
        )
        self._candidate_seen[candidate_key] = self._clock()
        return True

    @staticmethod
    def _entry_order(candidate: MappedCandidate, quantity: float) -> dict[str, Any]:
        return {
            "marketSlug": candidate.market["slug"],
            "intent": (
                "ORDER_INTENT_BUY_LONG"
                if candidate.position_side == "long"
                else "ORDER_INTENT_BUY_SHORT"
            ),
            "type": "ORDER_TYPE_LIMIT",
            "price": _price_amount(candidate.order_long_price),
            "quantity": quantity,
            "tif": "TIME_IN_FORCE_FILL_OR_KILL",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": True,
            "maxBlockTime": "5",
        }

    def _client(self):
        factory = self._client_factory
        if factory is None:
            try:
                from polymarket_us import PolymarketUS
            except ImportError as exc:  # pragma: no cover
                raise TradingExecutionError(
                    "official polymarket-us SDK is not installed"
                ) from exc
            factory = PolymarketUS
        return factory(
            key_id=self._key_id,
            secret_key=self._secret_key,
            timeout=15.0,
        )

    @staticmethod
    def _buying_power(payload: Mapping[str, Any]) -> float | None:
        balances = payload.get("balances") if isinstance(payload, Mapping) else None
        if not isinstance(balances, list):
            return None
        values = [
            _amount(item.get("buyingPower"))
            for item in balances
            if isinstance(item, Mapping)
        ]
        valid = [value for value in values if value is not None]
        return max(valid) if valid else None

    def _create_position(
        self,
        candidate: MappedCandidate,
        *,
        quantity: float,
        entry_cost: float,
        mode: str,
        order_id: str | None,
    ) -> str:
        position_id = str(uuid4())
        now = self._clock()
        policy_session_id = self._current_policy_session()
        entry_policy_json = json.dumps(
            asdict(self._policy),
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            if mode == "dry_run" and not self._policy.automation_enabled:
                raise TradingPolicyError(
                    "dry-run automation was stopped before the simulated fill"
                )
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO live_managed_positions(
                        id,mode,status,event_id,event_name,market_slug,market_type,
                        selection,position_side,quantity,entry_cost,entry_long_price,
                        cost_basis,opened_ts,updated_ts,highest_exit_value,
                        current_exit_value,current_model_probability,
                        current_execution_edge,return_fraction,entry_decision_id,
                        entry_order_id,initial_quantity,initial_cost_basis,
                        external_exit_quantity,venue_sync_status,
                        venue_mismatch_count,policy_session_id,entry_policy_json,
                        entry_signal_edge,entry_signal_quality,
                        entry_reference_sources,entry_execution_edge,
                        entry_game_fraction_remaining,entry_event_entries_60m)
                       VALUES (%s,%s,'open',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s)""",
                    (
                        position_id,
                        mode,
                        candidate.event.id,
                        candidate.event.name,
                        candidate.market["slug"],
                        _market_kind(candidate.signal.market),
                        candidate.selection,
                        candidate.position_side,
                        quantity,
                        entry_cost,
                        candidate.order_long_price,
                        quantity * entry_cost,
                        now,
                        now,
                        candidate.exit_value or 0.0,
                        candidate.exit_value,
                        _signal_probability(candidate.signal),
                        candidate.execution_edge,
                        (
                            candidate.exit_value / entry_cost - 1.0
                            if candidate.exit_value is not None
                            else None
                        ),
                        candidate.signal.decision_id or candidate.signal.decision_hash,
                        order_id,
                        quantity,
                        quantity * entry_cost,
                        0.0,
                        "not_applicable" if mode == "dry_run" else "awaiting_sync",
                        0,
                        policy_session_id,
                        entry_policy_json,
                        float(candidate.signal.edge),
                        float(candidate.signal.confidence),
                        int(candidate.signal.n_reference_sources),
                        float(candidate.execution_edge),
                        candidate.game_fraction_remaining,
                        int(candidate.event_entries_60m),
                    ),
                )
        return position_id

    def _position_exists(self, candidate: MappedCandidate) -> bool:
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    """SELECT 1 FROM live_managed_positions
                       WHERE status='open' AND market_slug=%s AND position_side=%s""",
                    (candidate.market["slug"], candidate.position_side),
                )
                return cur.fetchone() is not None

    def _event_entries_last_hour(self, event_id: str, mode: str) -> int:
        """Count actual managed fills, including positions since closed."""
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    """SELECT COUNT(*) FROM live_managed_positions
                       WHERE event_id=%s AND mode=%s AND opened_ts>%s""",
                    (event_id, mode, self._clock() - 3600),
                )
                row = cur.fetchone()
        return int(row[0] or 0) if row is not None else 0

    def _update_mark(
        self,
        position_id: str,
        *,
        exit_value: float,
        peak: float,
        probability: float | None,
        edge: float | None,
        return_fraction: float,
        profit_lock_armed_ts: float | None = None,
        profit_lock_price: float | None = None,
        profit_target_observed_ts: float | None = None,
        profit_target_observation_count: int = 0,
        profit_target_observed_price: float | None = None,
        adaptive_exit_payload: str | None = None,
        stop_triggered_ts: float | None = None,
        stop_observation_count: int = 0,
        stop_low_exit_value: float | None = None,
        stop_guard_payload: str | None = None,
    ) -> None:
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """UPDATE live_managed_positions SET updated_ts=%s,
                       highest_exit_value=%s,current_exit_value=%s,
                       current_model_probability=%s,current_execution_edge=%s,
                       return_fraction=%s,
                       profit_lock_armed_ts=COALESCE(
                           profit_lock_armed_ts,%s
                       ),
                       profit_lock_price=COALESCE(profit_lock_price,%s),
                       profit_target_observed_ts=%s,
                       profit_target_observation_count=%s,
                       profit_target_observed_price=%s,
                       adaptive_exit_payload=%s,
                       stop_triggered_ts=%s,
                       stop_observation_count=%s,
                       stop_low_exit_value=%s,
                       stop_guard_payload=%s
                       WHERE id=%s AND status='open'""",
                    (
                        self._clock(), peak, exit_value, probability, edge,
                        return_fraction, profit_lock_armed_ts,
                        profit_lock_price, profit_target_observed_ts,
                        profit_target_observation_count,
                        profit_target_observed_price,
                        adaptive_exit_payload,
                        stop_triggered_ts,
                        stop_observation_count,
                        stop_low_exit_value,
                        stop_guard_payload,
                        position_id,
                    ),
                )

    def _close_position(
        self,
        position_id: str,
        *,
        exit_value: float,
        reason: str,
        order_id: str | None,
    ) -> None:
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """UPDATE live_managed_positions SET status='closed',
                       updated_ts=%s,closed_ts=%s,current_exit_value=%s,
                       exit_reason=%s,exit_order_id=%s,
                       realized_pnl=(%s-entry_cost)*quantity
                       WHERE id=%s AND status='open'""",
                    (
                        self._clock(), self._clock(), exit_value, reason,
                        order_id, exit_value, position_id,
                    ),
                )

    def _risk_limiter_snapshot(self) -> dict[str, Any]:
        """Return the reset-aware rolling entry circuit-breaker counters."""
        now = self._clock()
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    """SELECT MAX(created_ts) FROM live_trading_journal
                       WHERE kind='risk_session_reset' AND status='started'""",
                )
                reset_row = cur.fetchone()
                reset_at = (
                    float(reset_row[0])
                    if reset_row is not None and reset_row[0] is not None
                    else None
                )
                loss_start = max(now - 86400, reset_at or 0.0)
                order_start = max(now - 3600, reset_at or 0.0)
                self._db.execute(
                    cur,
                    """SELECT COALESCE(SUM(CASE WHEN realized_pnl < 0
                              THEN -realized_pnl ELSE 0 END),0)
                       FROM live_managed_positions
                       WHERE status='closed' AND closed_ts > %s""",
                    (loss_start,),
                )
                loss_row = cur.fetchone()
                self._db.execute(
                    cur,
                    """SELECT COUNT(*) FROM live_trading_journal
                       WHERE created_ts > %s AND kind='entry'
                         AND status IN ('live_fill','unfilled','order_error')""",
                    (order_start,),
                )
                order_row = cur.fetchone()
        realized_loss = float(loss_row[0] or 0.0)
        orders = int(order_row[0] or 0)
        loss_limit = float(self._policy.max_daily_loss_usd)
        order_limit = int(self._policy.max_orders_per_hour)
        blockers = []
        if realized_loss >= loss_limit:
            blockers.append("rolling_realized_loss")
        if orders >= order_limit:
            blockers.append("hourly_live_entries")
        return {
            "reset_at": (
                datetime.fromtimestamp(reset_at, timezone.utc).isoformat()
                if reset_at is not None
                else None
            ),
            "orders_last_hour": orders,
            "orders_limit": order_limit,
            "orders_remaining": max(0, order_limit - orders),
            "realized_loss_24h_usd": round(realized_loss, 4),
            "realized_loss_limit_usd": round(loss_limit, 4),
            "realized_loss_remaining_usd": round(
                max(0.0, loss_limit - realized_loss),
                4,
            ),
            "active_entry_blockers": blockers,
            "order_window_started_at": datetime.fromtimestamp(
                order_start, timezone.utc
            ).isoformat(),
            "loss_window_started_at": datetime.fromtimestamp(
                loss_start, timezone.utc
            ).isoformat(),
            "reset_requires_live_disarm": True,
            "resettable": [
                "hourly live-entry attempt counter",
                "rolling realized-loss entry stop",
                "candidate retry cooldown",
            ],
            "always_enforced": [
                "per-position hard stop",
                "open-position and exposure limits",
                "buying power and cash reserve",
                "exact contract mapping and open authenticated venue",
                "price, spread, and executable depth",
                "source and executable edge",
                "signal quality and selected engine gates",
            ],
        }

    def _daily_realized_loss(self) -> float:
        return float(self._risk_limiter_snapshot()["realized_loss_24h_usd"])

    def _orders_last_hour(self) -> int:
        return int(self._risk_limiter_snapshot()["orders_last_hour"])

    def _record_order(
        self,
        order_id: str,
        market_slug: str,
        position_id: str | None,
        purpose: str,
        state: str,
    ) -> None:
        now = self._clock()
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO live_managed_orders(
                         order_id,market_slug,position_id,purpose,state,created_ts,updated_ts)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(order_id) DO UPDATE SET
                         state=EXCLUDED.state,updated_ts=EXCLUDED.updated_ts""",
                    (order_id, market_slug, position_id, purpose, state, now, now),
                )

    def _set_order_state(self, order_id: str, state: str) -> None:
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    "UPDATE live_managed_orders SET state=%s,updated_ts=%s WHERE order_id=%s",
                    (state, self._clock(), order_id),
                )

    def _managed_open_orders(self) -> list[dict[str, Any]]:
        terminal = tuple(_TERMINAL_ORDER_STATES)
        placeholders = ",".join(["%s"] * len(terminal))
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    f"""SELECT order_id,market_slug FROM live_managed_orders
                        WHERE state NOT IN ({placeholders})""",
                    terminal,
                )
                return [dict(row) for row in cur.fetchall()]

    def _journal_candidate(
        self,
        candidate: MappedCandidate,
        status: str,
        *,
        reasons: list[str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        self._journal(
            "entry",
            status,
            event=candidate.event,
            signal=candidate.signal,
            market_slug=candidate.market["slug"],
            selection=candidate.selection,
            payload={
                "reasons": reasons or [],
                "position_side": candidate.position_side,
                "entry_cost": candidate.entry_cost,
                "execution_edge": candidate.execution_edge,
                "signal_edge": candidate.signal.edge,
                "signal_market": candidate.signal.market,
                "signal_outcome": candidate.signal.outcome,
                "signal_quality": candidate.signal.confidence,
                "reference_sources": candidate.signal.n_reference_sources,
                "mapping_score": candidate.mapping_score,
                "engine_action": candidate.signal.action,
                "execution_mode": self._policy.execution_mode,
                "decision_id": (
                    candidate.signal.decision_id or candidate.signal.decision_hash
                ),
                **dict(extra or {}),
            },
        )

    def _journal(
        self,
        kind: str,
        status: str,
        *,
        event: Event | None = None,
        signal: Signal | None = None,
        event_id: str | None = None,
        event_name: str | None = None,
        market_slug: str | None = None,
        selection: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        now = self._clock()
        body = {
            "policy_version": POLICY_VERSION,
            "engine_version": signal.engine_version if signal else None,
            "configuration_hash": signal.configuration_hash if signal else None,
            "model_version": signal.model_version if signal else None,
            "calibration_version": signal.calibration_version if signal else None,
            **dict(payload or {}),
        }
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO live_trading_journal(
                         id,created_ts,kind,status,event_id,event_name,
                         market_slug,selection,payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        str(uuid4()),
                        now,
                        kind,
                        status,
                        event.id if event is not None else event_id,
                        event.name if event is not None else event_name,
                        market_slug,
                        signal.outcome if selection is None and signal else selection,
                        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str),
                    ),
                )
        self._journal_writes += 1
        if self._journal_writes % 100 == 0:
            self._prune_journal()

    def _prune_journal(self, maximum: int = 10_000) -> None:
        """Keep the decision ticker useful without allowing unbounded local data."""
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(cur, "SELECT COUNT(*) FROM live_trading_journal")
                count = int(cur.fetchone()[0] or 0)
            excess = count - maximum
            if excess <= 0:
                return
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """SELECT id FROM live_trading_journal
                       WHERE kind NOT IN ('performance_reset','risk_session_reset')
                       ORDER BY created_ts ASC LIMIT %s""",
                    (excess,),
                )
                stale = [row[0] for row in cur.fetchall()]
                for row_id in stale:
                    self._db.execute(
                        cur,
                        "DELETE FROM live_trading_journal WHERE id=%s",
                        (row_id,),
                    )

    def _safe_error(self, exc: Exception) -> str:
        # SDK messages may contain request metadata.  Never include credentials or
        # an arbitrary object repr in the durable journal.
        message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
        safe = str(message)
        status_code = getattr(exc, "status_code", None)
        if status_code is not None:
            safe = f"HTTP {status_code} {type(exc).__name__}: {safe}"
        for credential in (self._key_id, self._secret_key):
            if credential:
                safe = safe.replace(credential, "[redacted]")
        return safe[:500]

    @staticmethod
    def _safe_payload(payload: Any) -> Any:
        # Preview responses contain order economics, not credentials.  Bound the
        # serialized size so a verbose upstream response cannot inflate the DB.
        try:
            encoded = json.dumps(payload, default=str)
            return json.loads(encoded[:10_000]) if len(encoded) <= 10_000 else {
                "truncated": True,
                "size": len(encoded),
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"unavailable": True}
