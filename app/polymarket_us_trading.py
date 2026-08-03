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
from decimal import Decimal, ROUND_HALF_EVEN
from difflib import SequenceMatcher
import hashlib
import json
import math
import re
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

from .approval import APPROVAL_TOKEN, approval_granted, approval_instruction
from .adaptive_exit_model import (
    ADAPTIVE_EXIT_PROFILES,
    AdaptiveExitDecision,
    AdaptiveExitModel,
)
from .database import Database
from .entry_policy import MAX_ENTRY_PRICE, MIN_ENTRY_PRICE
from .lines import (
    FULL_GAME_SCOPE,
    MLB_FIRST_FIVE_SCOPE,
    MLB_FIRST_INNING_SCOPE,
    SUPPORTED_MARKET_SCOPES,
    base_market_type,
    market_scope,
    quote_line_side,
)
from .models import Event, GameState, Signal
from .policy_advisor import (
    ADVISOR_OBJECTIVES,
    ADVISOR_TUNABLE_FIELDS,
    recommend_policy,
)


POLICY_VERSION = "pmus-live-risk-policy-v11-entry-stability"
RISK_PRESET_VERSION = "risk-presets-v3"
POLYMARKET_US_TAKER_FEE_COEFFICIENT = Decimal("0.05")
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
        "max_signal_quality": 100.0,
        "min_source_agreement": 70.0,
        "max_signal_age_seconds": 30.0,
        "entry_confirmation_readings": 3,
        "max_confirmation_price_drift": 0.01,
        "min_reference_sources": 2,
        "min_entry_price": 0.15,
        "max_entry_price": 0.85,
        "max_spread": 0.02,
        "min_book_shares": 10.0,
        "min_hold_minutes": 20.0,
        "profit_target": 0.15,
        "minimum_locked_profit": 0.04,
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
        "max_signal_quality": 100.0,
        "min_source_agreement": 55.0,
        "max_signal_age_seconds": 45.0,
        "entry_confirmation_readings": 2,
        "max_confirmation_price_drift": 0.02,
        "min_reference_sources": 2,
        "min_entry_price": 0.12,
        "max_entry_price": 0.88,
        "max_spread": 0.03,
        "min_book_shares": 5.0,
        "min_hold_minutes": 15.0,
        "profit_target": 0.12,
        "minimum_locked_profit": 0.03,
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
        "max_signal_quality": 100.0,
        "min_source_agreement": 40.0,
        "max_signal_age_seconds": 75.0,
        "entry_confirmation_readings": 2,
        "max_confirmation_price_drift": 0.03,
        "min_reference_sources": 1,
        "min_entry_price": 0.10,
        "max_entry_price": 0.90,
        "max_spread": 0.04,
        "min_book_shares": 3.0,
        "min_hold_minutes": 10.0,
        "profit_target": 0.10,
        "minimum_locked_profit": 0.02,
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
        "max_signal_quality": 100.0,
        "min_source_agreement": 0.0,
        "max_signal_age_seconds": 120.0,
        "entry_confirmation_readings": 1,
        "max_confirmation_price_drift": 0.05,
        "min_reference_sources": 1,
        "min_entry_price": 0.08,
        "max_entry_price": 0.92,
        "max_spread": 0.06,
        "min_book_shares": 1.0,
        "min_hold_minutes": 5.0,
        "profit_target": 0.08,
        "minimum_locked_profit": 0.01,
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
SUPPORTED_ENTRY_MARKET_SCOPES = SUPPORTED_MARKET_SCOPES
SUPPORTED_ENTRY_MARKET_SCOPE_SET = frozenset(SUPPORTED_ENTRY_MARKET_SCOPES)
_WORD = re.compile(r"[a-z0-9]+")
_SIGNED_LINE = re.compile(r"(?<!\d)([+-]\d+(?:\.\d+)?)(?!\d)")
# Rejection reasons embed live numbers ("edge 4.3c is below ..."); the dedup
# key must not, or the cooldown never suppresses anything and qualification
# chatter floods the journal's retention budget (measured at 80% of the cap).
_REJECTION_KEY_NUMBERS = re.compile(r"[-+]?\d+(?:\.\d+)?")

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

# Off-policy evaluation needs the population the policy chose *from*, not only
# the fills it chose. The display journal cannot carry that: it is pruned to a
# rolling 10,000 rows, so adding candidate volume there would evict the entry
# history the advisor already depends on. Candidate contexts therefore get their
# own append-only table with its own, much larger retention bound.
_CANDIDATE_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate_observations (
    id TEXT PRIMARY KEY,
    observed_ts DOUBLE PRECISION NOT NULL,
    mode TEXT NOT NULL,
    policy_session_id TEXT,
    policy_fingerprint TEXT,
    execution_profile TEXT,
    event_id TEXT NOT NULL,
    event_name TEXT,
    decision_id TEXT,
    signal_market TEXT,
    market_type TEXT,
    market_scope TEXT,
    selection TEXT,
    position_side TEXT,
    us_event_slug TEXT,
    market_slug TEXT,
    state TEXT NOT NULL,
    reason TEXT,
    mapped INTEGER NOT NULL,
    executable INTEGER NOT NULL,
    entered INTEGER NOT NULL,
    signal_edge DOUBLE PRECISION,
    signal_quality DOUBLE PRECISION,
    source_agreement DOUBLE PRECISION,
    signal_age_seconds DOUBLE PRECISION,
    reference_sources INTEGER,
    entry_cost DOUBLE PRECISION,
    execution_edge DOUBLE PRECISION,
    spread DOUBLE PRECISION,
    book_shares DOUBLE PRECISION,
    mapping_score DOUBLE PRECISION,
    game_fraction_remaining DOUBLE PRECISION,
    event_entries_60m INTEGER,
    entry_confirmation_readings INTEGER,
    confirmation_price_drift DOUBLE PRECISION,
    candidate_rank INTEGER,
    competing_candidates INTEGER,
    selection_propensity DOUBLE PRECISION,
    propensity_source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidate_observations_observed
    ON candidate_observations(observed_ts DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_observations_mode
    ON candidate_observations(mode, observed_ts);
"""

# The saved policy is deterministic: given one ranked candidate list it always
# selects the same contract. Logged propensities are therefore degenerate (1 for
# the attempted candidate, 0 for the rest), which does NOT identify inverse
# propensity or doubly robust estimates. The marker is written with every row so
# a later controlled-exploration mode is distinguishable from this one, and so
# no downstream reader can mistake deterministic logs for randomized ones.
DETERMINISTIC_PROPENSITY_SOURCE = "deterministic_policy"
CANDIDATE_OBSERVATION_LIMIT = 300_000
# Evaluation states that mean an order was placed and a reward will be observed.
_ENTERED_CANDIDATE_STATES = frozenset({"simulated_fill", "live_fill"})
# Measured on a real cycle, per-evaluation content is a small share of the
# status payload and every field has a consumer: the UI reads state/reason,
# and gate results plus the cycle's policy context are the audit record of the
# cycle that produced the evaluation, which the saved policy may since have
# diverged from. The oversized block was the authorization matrix, which is
# trimmed at its source instead. `last_cycle_evaluation_count` bounds-checks
# the list without stripping it.
_STATUS_EVALUATION_OMITTED_FIELDS: frozenset[str] = frozenset()

RESEARCH_EVIDENCE_TABLES = frozenset({
    "live_trading_journal",
    "live_managed_positions",
    "trading_policy_sessions",
    "trading_policy_advice",
    "candidate_observations",
})


class TradingPolicyError(ValueError):
    """A user-correctable execution-policy error."""


class TradingExecutionError(RuntimeError):
    """A bounded venue or execution failure."""


LINE_EXECUTION_PROFILE_FIELDS = frozenset({
    "min_edge",
    "max_edge",
    "fee_edge_margin",
    "min_signal_quality",
    "max_signal_quality",
    "min_source_agreement",
    "max_signal_age_seconds",
    "entry_confirmation_readings",
    "max_confirmation_price_drift",
    "min_reference_sources",
    "min_entry_price",
    "max_entry_price",
    "max_spread",
    "min_book_shares",
    "min_hold_minutes",
    "profit_target",
    "minimum_locked_profit",
    "trailing_drawdown",
    "stop_loss",
    "exit_edge",
    "min_mlb_fraction_remaining",
    # Optional per-profile capital limits. These can only tighten the lane's
    # shared boundaries; see PROFILE_TIGHTEN_ONLY_FIELDS.
    "max_position_usd",
    "max_event_exposure_usd",
    "max_entries_per_event_per_hour",
    "max_profile_exposure_usd",
    "max_profile_open_positions",
    "max_profile_orders_per_hour",
})
# A profile may narrow how much capital one line/stage can commit, but it must
# never widen the lane's shared allocation, exposure ceiling, or concentration
# caps. Those stay hard boundaries, so these overrides are clamped to the lane
# value when the effective policy is resolved.
PROFILE_TIGHTEN_ONLY_FIELDS = frozenset({
    "max_position_usd",
    "max_event_exposure_usd",
    "max_entries_per_event_per_hour",
})
PROFILE_INTEGER_FIELDS = frozenset({
    "min_reference_sources",
    "entry_confirmation_readings",
    "max_entries_per_event_per_hour",
    "max_profile_open_positions",
    "max_profile_orders_per_hour",
})
LINE_EXECUTION_PROFILE_STAGES = frozenset({
    "all",
    "early",
    "middle",
    "late",
})


def _execution_game_stage(fraction_remaining: float | None) -> str | None:
    if fraction_remaining is None:
        return None
    if fraction_remaining >= 0.50:
        return "early"
    if fraction_remaining >= 0.25:
        return "middle"
    return "late"


def _normalize_line_execution_profiles(
    value: Any,
) -> tuple[dict[str, Any], ...]:
    if value in (None, (), []):
        return ()
    if not isinstance(value, (list, tuple)):
        raise TradingPolicyError("line_execution_profiles must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise TradingPolicyError(
                "each line execution profile must be an object"
            )
        market_type = _market_kind(str(raw.get("market_type") or ""))
        stage = str(raw.get("game_stage") or "all").strip().casefold()
        if market_type not in SUPPORTED_ENTRY_MARKET_TYPE_SET:
            raise TradingPolicyError(
                "line execution profile market_type must be moneyline, spread, or total"
            )
        if stage not in LINE_EXECUTION_PROFILE_STAGES:
            raise TradingPolicyError(
                "line execution profile game_stage must be all, early, middle, or late"
            )
        key = (market_type, stage)
        if key in seen:
            raise TradingPolicyError(
                f"duplicate line execution profile: {market_type}/{stage}"
            )
        seen.add(key)
        overrides = raw.get("overrides") or {}
        if not isinstance(overrides, Mapping):
            raise TradingPolicyError(
                "line execution profile overrides must be an object"
            )
        unknown = set(overrides) - LINE_EXECUTION_PROFILE_FIELDS
        if unknown:
            raise TradingPolicyError(
                "unsupported line execution override(s): "
                + ", ".join(sorted(str(field) for field in unknown))
            )
        clean_overrides: dict[str, Any] = {}
        for field_name, raw_value in overrides.items():
            try:
                clean_overrides[str(field_name)] = (
                    int(raw_value)
                    if field_name in PROFILE_INTEGER_FIELDS
                    else float(raw_value)
                )
            except (TypeError, ValueError) as exc:
                raise TradingPolicyError(
                    f"{market_type}/{stage} {field_name} must be numeric"
                ) from exc
        normalized.append({
            "market_type": market_type,
            "game_stage": stage,
            "enabled": bool(raw.get("enabled", True)),
            "overrides": clean_overrides,
        })
    return tuple(normalized)


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
    # When the volatility stop is on but usable MLB state is missing or stale,
    # fall back to a bounded price-only confirmation window instead of an
    # immediate one-quote market sell. Off by default so the change is
    # measured in the dry lane before it alters any existing behavior.
    stateless_stop_confirmation: bool = False
    stop_confirmation_readings: int = 3
    stop_grace_minutes: float = 2.0
    catastrophic_stop_multiplier: float = 1.75
    post_exit_tracking_minutes: float = 30.0
    require_engine_entry: bool = True
    required_engine_gates: tuple[str, ...] = CORE_ENGINE_GATES
    global_entry_enabled: bool = True
    allowed_market_types: tuple[str, ...] = SUPPORTED_ENTRY_MARKET_TYPES
    allowed_market_scopes: tuple[str, ...] = SUPPORTED_ENTRY_MARKET_SCOPES
    allow_live_segment_markets: bool = False
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
    # Multiple of the round-trip taker fee an entry must clear, on top of the
    # flat min_edge. Zero disables it and preserves the flat floor exactly.
    # 1.0 is break-even after fees; above 1.0 is margin over the toll.
    fee_edge_margin: float = 0.0
    max_edge: float = 1.0
    min_signal_quality: float = 60.0
    max_signal_quality: float = 100.0
    min_source_agreement: float = 0.0
    max_signal_age_seconds: float = 120.0
    entry_confirmation_readings: int = 1
    max_confirmation_price_drift: float = 1.0
    min_reference_sources: int = 2
    # Optional per-line/stage capital limits. Zero means "no profile-specific
    # cap"; the lane's shared allocation, exposure, reserve, and buying-power
    # checks always apply on top of these.
    max_profile_exposure_usd: float = 0.0
    max_profile_open_positions: int = 0
    max_profile_orders_per_hour: int = 0
    min_entry_price: float = 0.10
    max_entry_price: float = 0.90
    max_spread: float = 0.04
    min_book_shares: float = 3.0
    min_hold_minutes: float = 10.0
    profit_target: float = 0.10
    minimum_locked_profit: float = 0.02
    trailing_drawdown: float = 0.04
    stop_loss: float = 0.20
    exit_edge: float = 0.0
    # Consecutive negative-edge readings required before the model_reversal
    # exit may sell. 1 preserves the historical single-reading behavior; the
    # 2026-08-02 settlement grading measured 62-65% of single-reading
    # reversal exits going on to win, on both lanes independently.
    reversal_confirmation_readings: int = 1
    cycle_seconds: float = 30.0
    candidate_cooldown_seconds: int = 300
    max_entries_per_event_per_hour: int = 3
    min_mlb_fraction_remaining: float = 0.0
    line_execution_profiles: tuple[dict[str, Any], ...] = ()

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
        if "allowed_market_scopes" in clean:
            raw_market_scopes = clean["allowed_market_scopes"]
            if not isinstance(raw_market_scopes, (list, tuple)):
                raise TradingPolicyError("allowed_market_scopes must be a list")
            clean["allowed_market_scopes"] = tuple(
                dict.fromkeys(
                    str(scope).strip().casefold()
                    for scope in raw_market_scopes
                )
            )
        else:
            # Legacy dry-run policies start collecting the supported MLB
            # segments. Legacy live policies remain full-game only.
            clean["allowed_market_scopes"] = (
                SUPPORTED_ENTRY_MARKET_SCOPES
                if str(clean.get("execution_mode") or "dry_run") == "dry_run"
                else (FULL_GAME_SCOPE,)
            )
        clean["line_execution_profiles"] = _normalize_line_execution_profiles(
            clean.get("line_execution_profiles")
        )
        policy = cls(**clean)
        policy.validate()
        return policy

    def execution_policy_for(
        self,
        market_type: str,
        fraction_remaining: float | None,
    ) -> tuple["TradingPolicy" | None, str]:
        """Resolve an auditable line/stage overlay around existing metrics."""
        kind = _market_kind(market_type)
        stage = _execution_game_stage(fraction_remaining)
        selected: list[dict[str, Any]] = []
        for wanted_stage in ("all", stage):
            if wanted_stage is None:
                continue
            match = next(
                (
                    profile
                    for profile in self.line_execution_profiles
                    if profile["market_type"] == kind
                    and profile["game_stage"] == wanted_stage
                ),
                None,
            )
            if match is not None:
                selected.append(match)
        profile_key = (
            "+".join(
                f"{profile['market_type']}/{profile['game_stage']}"
                for profile in selected
            )
            or "global"
        )
        # The most-specific matching profile owns authorization. This lets an
        # enabled early/middle/late profile deliberately override a disabled
        # all-stage fallback for the same line type.
        authorization_profile = selected[-1] if selected else None
        if (
            authorization_profile is not None
            and not authorization_profile.get("enabled", True)
        ):
            return None, profile_key
        if not selected:
            if not self.global_entry_enabled:
                return None, "global/fallback-disabled"
            if kind not in self.allowed_market_types:
                return None, f"global/{kind}-disabled"
            return self, "global"
        overrides: dict[str, Any] = {}
        for profile in selected:
            if not profile.get("enabled", True):
                continue
            overrides.update(profile.get("overrides") or {})
        if not overrides:
            return self, profile_key
        # A profile may only narrow the lane's capital and concentration
        # boundaries. Clamping here rather than rejecting at save time keeps a
        # profile usable when the lane allocation is later reduced beneath it.
        for field_name in PROFILE_TIGHTEN_ONLY_FIELDS:
            if field_name in overrides:
                overrides[field_name] = min(
                    overrides[field_name],
                    getattr(self, field_name),
                )
        if "max_profile_exposure_usd" in overrides:
            overrides["max_profile_exposure_usd"] = min(
                overrides["max_profile_exposure_usd"],
                self.max_total_exposure_usd,
            )
        effective = replace(
            self,
            line_execution_profiles=(),
            **overrides,
        )
        # Authorization was already resolved by selecting an enabled profile.
        # Validate its effective limits without re-applying the now-removed
        # profile authorization.
        effective.validate(check_authorization=False)
        return effective, profile_key

    def validate(self, *, check_authorization: bool = True) -> None:
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
        if not 1 <= self.reversal_confirmation_readings <= 10:
            raise TradingPolicyError(
                "reversal_confirmation_readings must be between 1 and 10"
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
        if (
            check_authorization
            and self.global_entry_enabled
            and not self.allowed_market_types
        ):
            raise TradingPolicyError(
                "global entry fallback requires at least one automatic-entry line type"
            )
        unknown_market_scopes = (
            set(self.allowed_market_scopes) - SUPPORTED_ENTRY_MARKET_SCOPE_SET
        )
        if unknown_market_scopes:
            raise TradingPolicyError(
                "unknown automatic-entry market segment(s): "
                + ", ".join(sorted(unknown_market_scopes))
            )
        if not self.allowed_market_scopes:
            raise TradingPolicyError(
                "select at least one automatic-entry market segment"
            )
        if not MIN_ENTRY_PRICE < self.min_entry_price < self.max_entry_price < MAX_ENTRY_PRICE:
            raise TradingPolicyError(
                "entry prices must stay strictly inside the established 5c–95c bounds"
            )
        if self.fee_edge_margin != 0 and not 1.0 <= self.fee_edge_margin <= 5.0:
            raise TradingPolicyError(
                "fee_edge_margin must be 0 (off) or between 1.0 (break-even "
                "after the round-trip taker fee) and 5.0"
            )
        if not 0 <= self.min_edge < self.max_edge <= 1:
            raise TradingPolicyError(
                "edge filters must satisfy 0 <= minimum edge < maximum edge <= 1"
            )
        if not 0 <= self.min_mlb_fraction_remaining <= 1:
            raise TradingPolicyError(
                "min_mlb_fraction_remaining must be between zero and one"
            )
        if not 0 <= self.min_signal_quality <= self.max_signal_quality <= 100:
            raise TradingPolicyError(
                "signal quality filters must satisfy "
                "0 <= minimum quality <= maximum quality <= 100"
            )
        for field_name in (
            "max_profile_exposure_usd",
            "max_profile_open_positions",
            "max_profile_orders_per_hour",
        ):
            value = getattr(self, field_name)
            if not math.isfinite(float(value)) or value < 0:
                raise TradingPolicyError(
                    f"{field_name} must be zero (no cap) or a positive limit"
                )
        if self.max_profile_exposure_usd > self.max_total_exposure_usd:
            raise TradingPolicyError(
                "max_profile_exposure_usd cannot exceed the lane's maximum "
                "total exposure"
            )
        if not 0 <= self.min_source_agreement <= 100:
            raise TradingPolicyError(
                "min_source_agreement must be between 0 and 100"
            )
        if not 1 <= self.max_signal_age_seconds <= 120:
            raise TradingPolicyError(
                "max_signal_age_seconds must be between 1 and 120"
            )
        if not 1 <= self.entry_confirmation_readings <= 10:
            raise TradingPolicyError(
                "entry_confirmation_readings must be between 1 and 10"
            )
        if not 0 <= self.max_confirmation_price_drift <= 1:
            raise TradingPolicyError(
                "max_confirmation_price_drift must be between 0 and 1"
            )
        if not 0 < self.max_spread < 1:
            raise TradingPolicyError("max_spread must be between zero and one")
        if self.min_book_shares <= 0 or self.min_hold_minutes < 0:
            raise TradingPolicyError("book shares must be positive and hold time non-negative")
        for name in (
            "profit_target",
            "minimum_locked_profit",
            "trailing_drawdown",
            "stop_loss",
        ):
            value = getattr(self, name)
            if not 0 <= value < 1 or (
                name != "minimum_locked_profit" and value == 0
            ):
                raise TradingPolicyError(f"{name} must be between zero and one")
        if self.minimum_locked_profit >= self.profit_target:
            raise TradingPolicyError(
                "minimum locked profit must be smaller than the profit target"
            )
        if not -1 < self.exit_edge < 1:
            raise TradingPolicyError("exit_edge must be between -1 and 1")
        if (
            not math.isfinite(self.cycle_seconds)
            or not 1 <= self.cycle_seconds <= 300
        ):
            raise TradingPolicyError("cycle_seconds must be between 1 and 300")
        if self.candidate_cooldown_seconds < self.cycle_seconds:
            raise TradingPolicyError(
                "candidate_cooldown_seconds cannot be shorter than the cycle"
            )
        if self.line_execution_profiles:
            for market_type in SUPPORTED_ENTRY_MARKET_TYPES:
                for fraction in (None, 0.75, 0.375, 0.10):
                    self.execution_policy_for(market_type, fraction)


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
    execution_policy: TradingPolicy
    execution_profile_key: str = "global"
    game_fraction_remaining: float | None = None
    event_entries_60m: int = 1
    entry_confirmation_count: int = 1
    entry_confirmation_price_drift: float = 0.0

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


ROUND_TRIP_TAKER_FEE_RATE = float(POLYMARKET_US_TAKER_FEE_COEFFICIENT) * 2.0


def _fee_implied_edge_floor(entry_cost: float, margin: float) -> float | None:
    """Edge a contract must clear to survive its own round-trip taker fee.

    The venue charges `coefficient * shares * p * (1-p)` in each direction, so
    per share the round trip costs `2 * coefficient * p * (1-p)` - which is an
    edge, in the same probability units. It peaks at 50c and falls toward both
    extremes, so a single flat floor is simultaneously too strict on a 15c
    contract and too permissive on a 50c one.

    A margin of 1.0 is exact break-even. Returns None when disabled or when the
    price is outside the tradable range.
    """
    if margin <= 0 or not 0 < entry_cost < 1:
        return None
    return margin * ROUND_TRIP_TAKER_FEE_RATE * entry_cost * (1.0 - entry_cost)


def _required_execution_edge(
    policy: "TradingPolicy",
    signal: Signal,
    entry_cost: float,
) -> tuple[float, float | None]:
    """Return the edge an entry must clear, and the fee component of it."""
    base = max(policy.min_edge, signal.required_edge)
    fee_floor = _fee_implied_edge_floor(entry_cost, policy.fee_edge_margin)
    if fee_floor is None:
        return base, None
    return max(base, fee_floor), fee_floor


def _signal_age_seconds(signal: Signal, now: float) -> float | None:
    """Age of the retained signal, or None when it carries no timestamp.

    Entry qualification already rejects an undated signal, so this is defensive
    audit plumbing rather than a live gate. Recording an explicit unknown keeps
    a future refactor from crashing the entry path or writing a fabricated age.
    """
    observed_at = signal.observed_at
    if observed_at is None:
        return None
    return max(0.0, now - observed_at.timestamp())


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
    return base_market_type(str(value or ""))


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
        "top" if half in {"top", "t"} else
        # The MLB linescore feed reports "end" between half-innings (it
        # collapses Middle/End). Rejecting it made the stop guard stateless
        # exactly between halves; the model lab already accepts it with the
        # inning treated as completed.
        "end" if half in {"end", "middle", "mid"} else ""
    )
    if not normalized_half:
        return None
    completed_halves = (
        int(inning) * 2
        if normalized_half == "end"
        else (int(inning) - 1) * 2 + (1 if normalized_half == "bottom" else 0)
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


def _dry_segment_settlement_value(
    position: Mapping[str, Any],
    event: Event,
    scores: tuple[float, float],
) -> float | None:
    """Return the exact binary payout from an official segment-end score."""
    home_score, away_score = scores
    kind = _market_kind(position.get("market_type"))
    selection = str(position.get("selection") or "")
    normalized = _words(selection)
    if kind == "moneyline":
        if normalized in {"draw", "tie"}:
            won = abs(home_score - away_score) < 1e-9
        elif _similarity(selection, event.home) >= 0.86:
            won = home_score > away_score
        elif _similarity(selection, event.away) >= 0.86:
            won = away_score > home_score
        else:
            return None
        return 1.0 if won else 0.0
    if kind == "total":
        line_values = re.findall(
            r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)",
            selection,
        )
        line = _amount(line_values[-1]) if line_values else None
        if line is None:
            return None
        total = home_score + away_score
        if normalized.startswith("over "):
            won = total > line
        elif normalized.startswith("under "):
            won = total <= line
        else:
            return None
        return 1.0 if won else 0.0
    if kind == "spread":
        line, side = quote_line_side(
            "spread",
            selection,
            event.home,
            event.away,
        )
        if line is None or side not in {"home", "away"}:
            return None
        selected_score = home_score if side == "home" else away_score
        opponent_score = away_score if side == "home" else home_score
        return 1.0 if selected_score + line > opponent_score else 0.0
    return None


# League-average run expectancy for the 24 base/out states (rest of the
# half-inning), 2010s-era published averages. A static empirical table costs
# zero degrees of freedom, which is what makes it usable at this sample size.
# Shadow telemetry only: nothing reads it at decision time yet.
_RUN_EXPECTANCY: dict[int, tuple[float, float, float]] = {
    0: (0.48, 0.25, 0.10),   # bases empty
    1: (0.85, 0.50, 0.22),   # 1B
    2: (1.06, 0.64, 0.31),   # 2B
    3: (1.37, 0.87, 0.42),   # 1B+2B
    4: (1.30, 0.95, 0.35),   # 3B
    5: (1.70, 1.15, 0.48),   # 1B+3B
    6: (1.93, 1.34, 0.56),   # 2B+3B
    7: (2.17, 1.54, 0.71),   # loaded
}


def _mlb_shadow_state(state: GameState | None) -> dict[str, Any] | None:
    """Extract the in-game context the feed already carries but no rule reads.

    Returns None without structured MLB state. Pure so the shadow layer can
    be tested without a trader.
    """
    if state is None or not isinstance(state.sport_state, dict):
        return None
    structured = state.sport_state
    def bounded(name: str, low: int, high: int) -> int | None:
        try:
            value = int(structured.get(name))
        except (TypeError, ValueError):
            return None
        return value if low <= value <= high else None

    outs = bounded("outs", 0, 2)
    base_mask = bounded("base_mask", 0, 7)
    run_expectancy = (
        _RUN_EXPECTANCY[base_mask][outs]
        if outs is not None and base_mask is not None
        else None
    )
    batter = structured.get("batter") if isinstance(structured.get("batter"), Mapping) else {}
    pitcher = structured.get("pitcher") if isinstance(structured.get("pitcher"), Mapping) else {}
    return {
        "inning": structured.get("inning"),
        "half": structured.get("half"),
        "outs": outs,
        "base_mask": base_mask,
        "balls": bounded("balls", 0, 4),
        "strikes": bounded("strikes", 0, 3),
        "run_expectancy": run_expectancy,
        "batting_side": structured.get("batting_side"),
        "batter_id": batter.get("id"),
        "batter_name": batter.get("name"),
        "pitcher_id": pitcher.get("id"),
        "pitcher_name": pitcher.get("name"),
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
        if (
            "draw" in slug_words.split()
            or "draw" in question_words
            or "tie" in slug_words.split()
            or "tie" in question_words
        ):
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


def match_us_event(
    event: Event,
    us_events: Iterable[Mapping[str, Any]],
) -> tuple[dict, float] | None:
    """Public fail-closed event matcher shared by analysis and execution."""
    return _event_match(event, us_events)


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
    signal_scope = market_scope(signal.market)
    venue_scope = str(
        market.get("market_scope") or market_scope(str(market.get("market_type") or ""))
    ).casefold()
    if kind != market_kind or signal_scope != venue_scope:
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
        descriptions = {_words(_side_description(side)) for side in sides}
        if (
            venue_scope in {MLB_FIRST_INNING_SCOPE, MLB_FIRST_FIVE_SCOPE}
            and descriptions == {"yes", "no"}
        ):
            want_long = signal_side == "over"
            matches = [
                side for side in sides
                if bool(side.get("long")) == want_long
            ]
        else:
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
        descriptions = {_words(_side_description(side)) for side in sides}
        binary_segment = (
            venue_scope == MLB_FIRST_FIVE_SCOPE
            and descriptions == {"yes", "no"}
        )
        market_line = _amount(market.get("line"))
        matches = []
        for side in sides:
            side_line = _side_line(side)
            team_score = _similarity(wanted_team, _side_team(side))
            if (
                binary_segment
                and side.get("long")
                and market_line is not None
                and abs(market_line - signal_line) <= 1e-6
                and team_score >= 0.86
            ):
                matches.append((team_score, side))
                continue
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


def _estimated_taker_fee(
    *,
    quantity: float,
    contract_price: float,
) -> float:
    """Return the current exchange taker-fee estimate, rounded per order.

    Protective exits cross the authenticated book with an immediately fillable
    FOK limit, so they are taker executions. Polymarket US publishes the
    symmetric formula ``0.05 * contracts * p * (1-p)`` and rounds the order fee
    to the nearest cent using banker's rounding.
    """
    if quantity <= 0 or not 0 < contract_price < 1:
        return 0.0
    fee = (
        POLYMARKET_US_TAKER_FEE_COEFFICIENT
        * Decimal(str(quantity))
        * Decimal(str(contract_price))
        * (Decimal("1") - Decimal(str(contract_price)))
    )
    return float(fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


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
        database_namespace: str | None = None,
    ):
        self._db = Database.open(
            path,
            sqlite_envs=("POLYMARKET_US_TRADING_DB",),
            sqlite_default=path or "polymarket-us-trading.db",
            postgres_schema=database_namespace,
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
        self._candidate_confirmations: dict[str, dict[str, Any]] = {}
        self._qualification_seen: dict[str, float] = {}
        # Shadow pitcher tracking per event: bullpen entries are the classic
        # cash-out trigger; measured before any rule may act on them.
        self._event_pitchers: dict[str, dict[str, Any]] = {}
        self._candidate_log_seen: dict[str, float] = {}
        self._candidate_log_writes = 0
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
            self._db.migrate_columns(
                "polymarket_us_live_trading",
                10,
                {
                    "live_managed_positions": {
                        "market_scope": "TEXT",
                    },
                },
            )
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """UPDATE live_managed_positions
                       SET market_scope=%s
                       WHERE market_scope IS NULL OR market_scope=''""",
                    (FULL_GAME_SCOPE,),
                )
            self._db.migrate_columns(
                "polymarket_us_live_trading",
                11,
                {
                    "live_managed_positions": {
                        "profit_floor_missed_ts": "DOUBLE PRECISION",
                        "profit_floor_missed_count": "INTEGER",
                        "profit_floor_low_exit_value": "DOUBLE PRECISION",
                        "profit_guard_payload": "TEXT",
                    },
                },
            )
            self._db.migrate_columns(
                "polymarket_us_live_trading",
                12,
                {
                    "live_managed_positions": {
                        "entry_source_agreement": "DOUBLE PRECISION",
                        "entry_signal_age_seconds": "DOUBLE PRECISION",
                        "entry_confirmation_count": "INTEGER",
                        "entry_confirmation_price_drift": "DOUBLE PRECISION",
                    },
                },
            )
            self._db.initialize(
                _CANDIDATE_LOG_SCHEMA,
                component="polymarket_us_live_trading",
                version=13,
            )
            self._db.migrate_columns(
                "polymarket_us_live_trading",
                14,
                {
                    "live_managed_positions": {
                        "entry_spread": "DOUBLE PRECISION",
                        "entry_book_shares": "DOUBLE PRECISION",
                    },
                },
            )
            self._db.migrate_columns(
                "polymarket_us_live_trading",
                15,
                {
                    "live_managed_positions": {
                        # Mirror of highest_exit_value. Peak favourable
                        # excursion already makes a tighter profit target
                        # identifiable; retaining the adverse extreme does the
                        # same for a tighter stop, which the July 2026 audit
                        # identified as the dominant realized-loss source.
                        "lowest_exit_value": "DOUBLE PRECISION",
                    },
                },
            )
            self._db.migrate_columns(
                "polymarket_us_live_trading",
                16,
                {
                    "live_managed_positions": {
                        # Consecutive-reading state for the model-reversal
                        # confirmation window, mirroring the stop guard's
                        # trigger/count pair so recoveries are auditable.
                        "reversal_triggered_ts": "DOUBLE PRECISION",
                        "reversal_observation_count": "INTEGER",
                    },
                },
            )
        self._adaptive_exit = AdaptiveExitModel(
            path,
            clock=clock,
            database_namespace=database_namespace,
        )
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
        self._candidate_confirmations.clear()
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
        self._candidate_confirmations.clear()
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

    def _open_policy_session_id(self) -> str | None:
        """Return the open policy session without creating one.

        Read-only callers must use this. `_current_policy_session` starts a
        session as a side effect, so calling it from a status or reporting path
        would fabricate sessions that never traded and pollute the per-session
        trade/event ledger.
        """
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    """SELECT id FROM trading_policy_sessions
                       WHERE ended_ts IS NULL ORDER BY started_ts DESC LIMIT 1""",
                )
                row = cur.fetchone()
        return str(row[0]) if row is not None else None

    def _current_policy_session(self) -> str:
        existing = self._open_policy_session_id()
        if existing is not None:
            return existing
        return self._start_policy_session("first_managed_trade")

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
        self._candidate_confirmations.clear()
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
        self._candidate_confirmations.clear()
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
        self._candidate_confirmations.clear()
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

    def authorization_matrix(self) -> dict[str, Any]:
        """Resolve what every line/stage combination is actually authorized to do.

        Authorization is spread across the global fallback switch, the global
        line-type list, and the line/stage profile overlay, with the most
        specific matching profile winning. Reading three controls and inferring
        the result is exactly where an operator mis-reads what is armed, so the
        server resolves all twelve combinations explicitly and reports the
        effective thresholds each one would execute under.
        """
        policy = self._policy
        stages = (("all", None), ("early", 0.75), ("middle", 0.375), ("late", 0.10))
        rows: list[dict[str, Any]] = []
        for market_type in SUPPORTED_ENTRY_MARKET_TYPES:
            for stage, fraction in stages:
                effective, profile_key = policy.execution_policy_for(
                    market_type,
                    fraction,
                )
                authorized = effective is not None
                if authorized:
                    source = (
                        "global_fallback"
                        if profile_key == "global"
                        else "line_profile"
                    )
                    blocked_reason = None
                elif profile_key == "global/fallback-disabled":
                    source = "unauthorized"
                    blocked_reason = (
                        "global fallback is off and no line profile matches"
                    )
                elif profile_key.startswith("global/"):
                    source = "unauthorized"
                    blocked_reason = (
                        f"{market_type} is not in the global line-type list"
                    )
                else:
                    source = "unauthorized"
                    blocked_reason = (
                        f"the {profile_key} profile is not authorized"
                    )
                row: dict[str, Any] = {
                    "market_type": market_type,
                    "game_stage": stage,
                    "authorized": authorized,
                    "source": source,
                    "profile_key": profile_key,
                    "blocked_reason": blocked_reason,
                    "effective": None,
                }
                if effective is not None:
                    values = {
                        field: getattr(effective, field)
                        for field in sorted(LINE_EXECUTION_PROFILE_FIELDS)
                    }
                    differs = {
                        field: value
                        for field, value in values.items()
                        if value != getattr(policy, field)
                    }
                    row["inherits_global"] = not differs
                    # Twelve identical copies of the global thresholds is the
                    # common case and was the largest block in the status
                    # payload. Send the resolved values only where a profile
                    # actually changes them; the client already holds the
                    # global policy to fall back on.
                    row["effective"] = None if not differs else values
                    row["overridden_fields"] = sorted(differs)
                rows.append(row)
        return {
            "global_fallback_authorized": policy.global_entry_enabled,
            "global_line_types": list(policy.allowed_market_types),
            "allowed_market_scopes": list(policy.allowed_market_scopes),
            "allow_live_segment_markets": policy.allow_live_segment_markets,
            "profiles": [
                {
                    "market_type": profile["market_type"],
                    "game_stage": profile["game_stage"],
                    "authorized": bool(profile.get("enabled", True)),
                    "overrides": dict(profile.get("overrides") or {}),
                }
                for profile in policy.line_execution_profiles
            ],
            "combinations": rows,
            "authorized_combinations": sum(
                1 for row in rows if row["authorized"]
            ),
            "any_authorized": any(row["authorized"] for row in rows),
        }

    def _entry_blockers(
        self,
        *,
        open_positions: Sequence[Mapping[str, Any]],
        exposure: float,
        risk_session: Mapping[str, Any],
        authorization: Mapping[str, Any],
    ) -> list[dict[str, str]]:
        """List every reason a new entry cannot happen right now.

        These are the policy- and account-level conditions that are already
        knowable without a venue round trip. Per-candidate reasons (price band,
        spread, depth, edge) stay in the cycle evaluations, because they are
        properties of a contract rather than of the lane.
        """
        policy = self._policy
        blockers: list[dict[str, str]] = []
        if not policy.automation_enabled:
            blockers.append({
                "code": "automation_off",
                "detail": "automatic analysis is not running for this lane",
            })
        if not authorization["any_authorized"]:
            blockers.append({
                "code": "nothing_authorized",
                "detail": (
                    "no line type or line/stage profile is authorized for "
                    "automatic entry"
                ),
            })
        if policy.execution_mode == "live":
            if not self.is_armed():
                blockers.append({
                    "code": "live_disarmed",
                    "detail": (
                        "the live-order latch is closed; arming is required "
                        "before any live entry"
                    ),
                })
            if not self.credential_status()["configured"]:
                blockers.append({
                    "code": "credentials_missing",
                    "detail": "Polymarket US API credentials are not configured",
                })
            if self._last_venue_sync_error is not None:
                blockers.append({
                    "code": "venue_sync_unavailable",
                    "detail": (
                        "authenticated position sync is failing; live entry is "
                        "paused to avoid account-state divergence"
                    ),
                })
        if len(open_positions) >= policy.max_open_positions:
            blockers.append({
                "code": "max_open_positions",
                "detail": (
                    f"{len(open_positions)} of {policy.max_open_positions} "
                    "managed positions are already open"
                ),
            })
        if "rolling_realized_loss" in risk_session.get("active_entry_blockers", ()):
            blockers.append({
                "code": "daily_loss_stop",
                "detail": (
                    "the rolling realized-loss stop is reached; start a new "
                    "risk session to clear it"
                ),
            })
        if "hourly_live_entries" in risk_session.get("active_entry_blockers", ()):
            blockers.append({
                "code": "hourly_order_cap",
                "detail": (
                    f"{risk_session.get('orders_last_hour')} of "
                    f"{risk_session.get('orders_limit')} hourly entries used"
                ),
            })
        remaining = min(
            policy.max_total_exposure_usd - exposure,
            policy.trading_allocation_usd - exposure,
        )
        if remaining <= 0:
            blockers.append({
                "code": "exposure_exhausted",
                "detail": (
                    f"${exposure:.2f} of managed exposure leaves no room under "
                    f"the ${policy.max_total_exposure_usd:.2f} exposure limit "
                    f"and ${policy.trading_allocation_usd:.2f} allocation"
                ),
            })
        return blockers

    def _predictive_exit_recommendation(
        self,
        *,
        labeled: float,
        labeled_events: float,
        required: float,
        brier: float | None,
    ) -> dict[str, Any]:
        """Recommend the adaptive response from the overlay's own scored record.

        This deliberately does not read trade P/L. The overlay answers a
        narrower question - will the executable mark move adversely before it
        moves favourably - and it scores its own forecasts, so its Brier value
        against whole-event support is the evidence that belongs here. A
        realized-return comparison could not separate the overlay's effect from
        the entry policy that selected the trades.

        A Brier score of 0.25 is what always forecasting 0.50 achieves. An
        overlay that cannot beat that has no business tightening a live exit.
        """
        floor = ADAPTIVE_EXIT_PROFILES["guarded"]["cold_start_confidence"]
        if labeled <= 0:
            return {
                "profile": "observe",
                "basis": "insufficient_evidence",
                "applyable": True,
                "rationale": (
                    "No labeled forecast has been scored yet. Observe-only "
                    "records and scores the overlay without letting it move "
                    "any exit."
                ),
            }
        if labeled < required or labeled_events < 5:
            return {
                "profile": "observe",
                "basis": "insufficient_evidence",
                "applyable": True,
                "rationale": (
                    f"{labeled:.0f} of {required:.0f} labeled forecasts across "
                    f"{labeled_events:.0f} of 5 events. A non-observe profile "
                    f"would already apply up to {floor * 100:.0f}% of its "
                    "bounded tightening from its cold-start floor, on evidence "
                    "this thin."
                ),
            }
        if brier is None:
            return {
                "profile": "observe",
                "basis": "insufficient_evidence",
                "applyable": True,
                "rationale": (
                    "Forecasts are labeled but no Brier score is available, so "
                    "the overlay's accuracy cannot be checked."
                ),
            }
        if brier >= 0.25:
            return {
                "profile": "observe",
                "basis": "overlay_scored",
                "applyable": True,
                "brier_score": brier,
                "rationale": (
                    f"Brier {brier:.3f} is no better than always forecasting "
                    "0.50, so the overlay has not earned the right to tighten "
                    "an exit. Keep scoring it in observe-only."
                ),
            }
        return {
            "profile": "guarded",
            "basis": "overlay_scored",
            "applyable": True,
            "brier_score": brier,
            "rationale": (
                f"Brier {brier:.3f} beats the 0.250 always-0.50 benchmark on "
                f"{labeled:.0f} labeled forecasts across {labeled_events:.0f} "
                "events. Guarded is the smallest response above observe-only; "
                "it is not evidence for Balanced or Responsive, and it says "
                "nothing about realized return."
            ),
        }

    def _predictive_exit_state(
        self,
        adaptive_exit: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Name the adaptive-exit overlay's state instead of implying one.

        The lane-level status here is a coarse readiness summary. The overlay
        itself scores support per game-state context, so a lane reported as
        `active` can still be in cold start for an unusual inning/score bucket.
        Per-position detail stays on the position record.
        """
        policy = self._policy
        profile_name = str(policy.adaptive_exit_profile or "observe")
        profile_values = ADAPTIVE_EXIT_PROFILES.get(profile_name, {})
        cold_start_confidence = float(
            profile_values.get("cold_start_confidence") or 0.0
        )
        base = {
            "profile": profile_name,
            "cold_start_confidence": cold_start_confidence,
            "maximum_tightening": policy.adaptive_exit_max_tightening,
            "horizon_minutes": policy.adaptive_exit_horizon_minutes,
            "hard_stop_unchanged": True,
            "support_is_per_game_state": True,
        }
        if not policy.adaptive_exit_enabled:
            return {
                **base,
                "status": "off",
                "detail": "adaptive MLB cash-out research is turned off",
                "can_tighten_exits": False,
            }
        if profile_name == "observe":
            return {
                **base,
                "status": "observe_only",
                "detail": (
                    "forecasts are recorded and scored, but no exit threshold "
                    "is changed"
                ),
                "can_tighten_exits": False,
            }
        labeled = _amount(adaptive_exit.get("labeled_observations")) or 0.0
        labeled_events = _amount(adaptive_exit.get("labeled_events")) or 0.0
        observations = _amount(adaptive_exit.get("observations")) or 0.0
        required = float(max(5, int(policy.adaptive_exit_min_samples)))
        if observations <= 0:
            status = "collecting"
            detail = (
                "no MLB observation retained yet for this lane"
            )
        elif labeled <= 0:
            status = "cold_start"
            detail = (
                f"{observations:.0f} observations retained but none labeled yet"
            )
        elif labeled < required or labeled_events < 5:
            status = "cold_start"
            detail = (
                f"{labeled:.0f} of {required:.0f} labeled forecasts across "
                f"{labeled_events:.0f} of 5 events; learned support is partial"
            )
        else:
            status = "active"
            detail = (
                f"{labeled:.0f} labeled forecasts across {labeled_events:.0f} "
                "events carry the estimate for well-supported game states"
            )
        return {
            **base,
            "status": status,
            "detail": detail,
            "recommendation": self._predictive_exit_recommendation(
                labeled=labeled,
                labeled_events=labeled_events,
                required=required,
                brier=_amount(adaptive_exit.get("brier_score")),
            ),
            # A non-observe profile tightens from its cold-start floor even
            # with no labeled evidence at all. Reporting cold start as "not
            # yet acting" would understate what is already changing exits.
            "can_tighten_exits": True,
            "cold_start_warning": (
                f"the {profile_name} profile applies up to "
                f"{cold_start_confidence * 100:.0f}% of its bounded tightening "
                "before any labeled evidence exists"
                if status != "active" and cold_start_confidence > 0
                else None
            ),
            "observations": observations,
            "labeled_observations": labeled,
            "labeled_events": labeled_events,
            "minimum_samples": required,
            "minimum_events": 5,
        }

    def execution_state(
        self,
        *,
        open_positions: Sequence[Mapping[str, Any]] | None = None,
        exposure: float | None = None,
        risk_session: Mapping[str, Any] | None = None,
        adaptive_exit: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Report every execution state separately instead of one blended flag.

        Callers that already loaded positions, exposure, the risk snapshot, or
        the adaptive summary pass them in so the status endpoint does not repeat
        those queries.
        """
        policy = self._policy
        if open_positions is None:
            open_positions = self.positions(open_only=True)
        if exposure is None:
            exposure = sum(
                float(row["cost_basis"]) for row in open_positions
            )
        if risk_session is None:
            risk_session = self._risk_limiter_snapshot()
        if adaptive_exit is None:
            adaptive_exit = self._adaptive_exit.summary()
        credential = self.credential_status()
        authorization = self.authorization_matrix()
        armed = self.is_armed()
        now = self._clock()
        blockers = self._entry_blockers(
            open_positions=open_positions,
            exposure=exposure,
            risk_session=risk_session,
            authorization=authorization,
        )
        return {
            "policy": {
                "version": POLICY_VERSION,
                # The UI compares this against the fingerprint of the form it
                # is showing, so "unsaved changes" is a fact rather than a guess.
                "saved_fingerprint": _policy_fingerprint(policy),
                "execution_mode": policy.execution_mode,
                "risk_preset": policy.risk_preset,
                # Read-only: reporting state must never start a session.
                "session_id": self._open_policy_session_id(),
            },
            "automation": {
                "running": policy.automation_enabled,
                "cycle_seconds": policy.cycle_seconds,
                "last_cycle_at": (
                    datetime.fromtimestamp(
                        self._last_cycle_at, timezone.utc
                    ).isoformat()
                    if self._last_cycle_at
                    else None
                ),
                "last_cycle_summary": self._last_cycle_summary,
            },
            "live_orders": {
                "armed": armed,
                "expires_at": (
                    datetime.fromtimestamp(
                        self._armed_until, timezone.utc
                    ).isoformat()
                    if armed
                    else None
                ),
                "seconds_remaining": (
                    max(0.0, self._armed_until - now) if armed else 0.0
                ),
                "restart_behavior": "always_disarmed",
                "applies_to_lane": policy.execution_mode == "live",
            },
            "protective_exits": {
                "armed": (
                    self._protective_exits_armed
                    and policy.automation_enabled
                    and policy.execution_mode == "live"
                    and policy.auto_cashout
                ),
                "auto_cashout_enabled": policy.auto_cashout,
                "behavior": "armed_until_save_disarm_stop_or_restart",
            },
            "account": {
                "connected": credential["configured"],
                "credential_source": credential["credential_source"],
                "credential_retention": credential["retention"],
                "last_sync_at": (
                    datetime.fromtimestamp(
                        self._last_venue_sync_at, timezone.utc
                    ).isoformat()
                    if self._last_venue_sync_at
                    else None
                ),
                "last_sync_error": self._last_venue_sync_error,
            },
            "authorization": authorization,
            "exposure": {
                "open_positions": len(open_positions),
                "max_open_positions": policy.max_open_positions,
                "managed_exposure_usd": round(exposure, 2),
                "trading_allocation_usd": policy.trading_allocation_usd,
                "max_total_exposure_usd": policy.max_total_exposure_usd,
                "remaining_capacity_usd": round(
                    max(
                        0.0,
                        min(
                            policy.max_total_exposure_usd - exposure,
                            policy.trading_allocation_usd - exposure,
                        ),
                    ),
                    2,
                ),
                "minimum_cash_reserve_usd": policy.minimum_cash_reserve_usd,
            },
            "entry_blockers": blockers,
            "entry_possible_now": not blockers,
            "predictive_exit": self._predictive_exit_state(adaptive_exit),
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
            #
            # Policy-wide constants and per-signal gate detail are omitted here:
            # they are identical on every row, are already carried once in
            # `policy`, and remain durably auditable in the execution journal
            # and candidate log. Repeating them made this the heaviest block in
            # a payload the dashboard polls continuously.
            "last_cycle_evaluations": [
                {
                    key: value for key, value in item.items()
                    if key not in _STATUS_EVALUATION_OMITTED_FIELDS
                }
                for item in self._last_cycle_evaluations
            ],
            "last_cycle_evaluation_count": len(self._last_cycle_evaluations),
            "risk_session": risk_session,
            "adaptive_exit": adaptive_exit,
            "execution_state": self.execution_state(
                open_positions=positions,
                exposure=exposure,
                risk_session=risk_session,
                adaptive_exit=adaptive_exit,
            ),
            "live_capable": self._policy.execution_mode == "live",
            "live_order_possible_now": (
                self._policy.automation_enabled
                and self._policy.execution_mode == "live"
                and self.is_armed()
            ),
            "server_time": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        }

    def segment_research_summary(self) -> dict[str, Any]:
        """Summarize retained dry-run evidence for exact MLB period markets.

        This is an execution-outcome report, not a fitted prediction. It keeps
        first-inning and first-five evidence visibly separate from full-game
        trades so live segment approval can be based on auditable dry-run data.
        """
        positions = [
            row
            for row in self.positions(include_hidden=True)
            if str(row.get("mode") or "") == "dry_run"
            and str(row.get("market_scope") or FULL_GAME_SCOPE)
            != FULL_GAME_SCOPE
        ]
        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in positions:
            scope = str(row.get("market_scope") or FULL_GAME_SCOPE)
            kind = _market_kind(row.get("market_type"))
            buckets.setdefault((scope, kind), []).append(row)

        rows = []
        for (scope, kind), trades in sorted(buckets.items()):
            closed = [
                trade for trade in trades
                if str(trade.get("status") or "") != "open"
                and trade.get("realized_pnl") is not None
            ]
            wins = sum(float(trade["realized_pnl"]) > 1e-9 for trade in closed)
            losses = sum(float(trade["realized_pnl"]) < -1e-9 for trade in closed)
            pushes = len(closed) - wins - losses
            realized = sum(float(trade["realized_pnl"]) for trade in closed)
            settled_cost = sum(
                float(trade.get("initial_cost_basis") or trade.get("cost_basis") or 0)
                for trade in closed
            )
            rows.append({
                "market_scope": scope,
                "market_type": kind,
                "trades": len(trades),
                "events": len({
                    str(trade.get("event_id") or "")
                    for trade in trades
                    if trade.get("event_id")
                }),
                "open": sum(
                    str(trade.get("status") or "") == "open"
                    for trade in trades
                ),
                "closed": len(closed),
                "wins": wins,
                "losses": losses,
                "pushes": pushes,
                "win_rate": wins / (wins + losses) if wins + losses else None,
                "realized_net_usd": round(realized, 4),
                "after_cost_roi": (
                    realized / settled_cost if settled_cost > 0 else None
                ),
            })
        return {
            "mode": "dry_run",
            "trades": sum(row["trades"] for row in rows),
            "events": len({
                str(position.get("event_id") or "")
                for position in positions
                if position.get("event_id")
            }),
            "closed": sum(row["closed"] for row in rows),
            "rows": rows,
            "note": (
                "Execution outcomes only; segment rows do not alter the existing "
                "probability, edge, signal-quality, or calibration calculations."
            ),
        }

    def journal(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        kinds: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        selected = tuple(
            str(kind).strip() for kind in (kinds or ()) if str(kind).strip()
        )
        clause = ""
        params: list[Any] = []
        if selected:
            clause = " WHERE kind IN (" + ",".join(["%s"] * len(selected)) + ")"
            params.extend(selected)
        params.extend([limit, offset])
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT id,created_ts,kind,status,event_id,event_name,
                              market_slug,selection,payload
                       FROM live_trading_journal"""
                    + clause
                    + " ORDER BY created_ts DESC LIMIT %s OFFSET %s",
                    tuple(params),
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

    def journal_page(
        self,
        *,
        limit: int = 25,
        offset: int = 0,
        kinds: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Return one page of the journal plus the counts needed to navigate it.

        The dashboard polls this continuously. Returning 120 rows of full
        payloads was the single largest response in the app - about 270 KB, of
        which the overwhelming majority were high-volume qualification records
        that pushed the entry and exit rows the operator actually wants off the
        visible page.
        """
        selected = tuple(
            str(kind).strip() for kind in (kinds or ()) if str(kind).strip()
        )
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT kind, COUNT(*) AS total
                       FROM live_trading_journal GROUP BY kind""",
                )
                counts = {
                    str(row["kind"]): int(row["total"])
                    for row in cur.fetchall()
                }
        total = sum(counts.values())
        filtered_total = (
            sum(counts.get(kind, 0) for kind in selected) if selected else total
        )
        items = self.journal(limit=limit, offset=offset, kinds=selected)
        return {
            "items": items,
            "total": total,
            "filtered_total": filtered_total,
            "offset": max(0, int(offset)),
            "limit": max(1, min(int(limit), 500)),
            "kinds": sorted(selected),
            "kind_counts": dict(sorted(counts.items())),
        }

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
            profit_guard_payload = row.pop("profit_guard_payload", None)
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
                row["profit_guard"] = (
                    json.loads(profit_guard_payload)
                    if profit_guard_payload else None
                )
            except (TypeError, json.JSONDecodeError):
                row["profit_guard"] = None
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
            row["profit_floor_missed_count"] = int(
                row.get("profit_floor_missed_count") or 0
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
                "profit_floor_missed_ts",
            ):
                value = row.get(key)
                row[key.removesuffix("_ts") + "_at"] = (
                    datetime.fromtimestamp(float(value), timezone.utc).isoformat()
                    if value is not None
                    else None
                )
        return rows

    def _position_execution_policy(
        self,
        position: Mapping[str, Any],
    ) -> TradingPolicy:
        """Use the effective policy captured at entry for position management."""
        payload = position.get("entry_policy")
        if isinstance(payload, Mapping):
            try:
                return TradingPolicy.from_mapping(payload)
            except TradingPolicyError:
                pass
        return self._policy

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
        source_positions: Iterable[Mapping[str, Any]] | None = None,
        data_sources: Iterable[Mapping[str, Any]] | None = None,
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

        positions = (
            [dict(position) for position in source_positions]
            if source_positions is not None
            else self.positions(include_hidden=True)
        )
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
                "market_scope": str(
                    position.get("market_scope") or FULL_GAME_SCOPE
                ),
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
                "entry_source_agreement": _amount(
                    position.get("entry_source_agreement")
                ),
                "entry_signal_age_seconds": _amount(
                    position.get("entry_signal_age_seconds")
                ),
                "entry_confirmation_count": (
                    int(position["entry_confirmation_count"])
                    if position.get("entry_confirmation_count") is not None
                    else None
                ),
                "entry_confirmation_price_drift": _amount(
                    position.get("entry_confirmation_price_drift")
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

        scope_groups = []
        for scope in SUPPORTED_ENTRY_MARKET_SCOPES:
            rows = [row for row in matching if row["market_scope"] == scope]
            if rows:
                scope_groups.append({
                    "market_scope": scope,
                    **aggregate(rows),
                })

        settings_buckets: dict[
            tuple[str, str, str, str],
            list[dict[str, Any]],
        ] = {}
        for row in matching:
            if row["result"] not in {"win", "loss", "push"}:
                continue
            key = (
                str(row["mode"]),
                str(row["market_type"]),
                str(row["market_scope"]),
                str(row["policy_signature"]),
            )
            settings_buckets.setdefault(key, []).append(row)
        settings_groups = []
        for (
            group_mode,
            kind,
            scope,
            signature,
        ), rows in settings_buckets.items():
            policy = rows[0].get("entry_policy")
            settings_groups.append({
                "mode": group_mode,
                "market_type": kind,
                "market_scope": scope,
                "policy_signature": signature,
                "settings_available": isinstance(policy, Mapping),
                "settings": (
                    {
                        key: policy.get(key)
                        for key in (
                            "risk_preset",
                            "global_entry_enabled",
                            "allowed_market_types",
                            "allowed_market_scopes",
                            "allow_live_segment_markets",
                            "min_edge",
                            "max_edge",
                            "min_signal_quality",
                            "max_signal_quality",
                            "min_source_agreement",
                            "max_signal_age_seconds",
                            "entry_confirmation_readings",
                            "max_confirmation_price_drift",
                            "min_reference_sources",
                            "min_entry_price",
                            "max_entry_price",
                            "max_spread",
                            "min_book_shares",
                            "profit_target",
                            "minimum_locked_profit",
                            "trailing_drawdown",
                            "stop_loss",
                            "exit_edge",
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
            "data_sources": [
                dict(source) for source in (data_sources or ())
            ],
            "summary": aggregate(matching),
            "line_type_summary": type_groups,
            "market_scope_summary": scope_groups,
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
                              market_scope,
                              exit_reason,
                              entry_signal_edge AS signal_edge,
                              entry_signal_quality AS signal_quality,
                              entry_reference_sources AS reference_sources,
                              entry_execution_edge AS execution_edge,
                              entry_game_fraction_remaining
                                  AS game_fraction_remaining,
                              entry_event_entries_60m AS event_entries_60m,
                              entry_source_agreement AS source_agreement,
                              entry_signal_age_seconds AS signal_age_seconds,
                              entry_confirmation_count
                                  AS entry_confirmation_readings,
                              entry_confirmation_price_drift
                                  AS confirmation_price_drift,
                              -- Peak executable exit value reached before the
                              -- actual close. This is the only retained
                              -- excursion evidence, and it is what makes a
                              -- tighter profit target identifiable.
                              highest_exit_value,
                              lowest_exit_value,
                              quantity,
                              entry_spread AS spread,
                              entry_book_shares AS book_shares,
                              policy_session_id
                       FROM live_managed_positions
                       WHERE status='closed' AND realized_pnl IS NOT NULL
                       ORDER BY opened_ts""",
                )
                return [dict(row) for row in cur.fetchall()]

    def _candidate_observation_opportunities(self) -> list[dict[str, Any]]:
        """Return the logged candidate population, entered or not."""
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT observed_ts,event_id,decision_id,signal_market,
                              market_type,market_scope,selection,position_side,
                              market_slug,state,reason,mapped,executable,entered,
                              mode,signal_edge,signal_quality,source_agreement,
                              signal_age_seconds,reference_sources,entry_cost,
                              execution_edge,spread,book_shares,
                              game_fraction_remaining,event_entries_60m,
                              entry_confirmation_readings,
                              confirmation_price_drift,execution_profile,
                              candidate_rank,competing_candidates,
                              selection_propensity,propensity_source
                       FROM candidate_observations
                       WHERE mapped=1
                       ORDER BY observed_ts""",
                )
                rows = [dict(row) for row in cur.fetchall()]
        return [
            {
                "event_id": row.get("event_id"),
                "observed_ts": float(row["observed_ts"]),
                "signal_edge": _amount(row.get("signal_edge")),
                "signal_quality": _amount(row.get("signal_quality")),
                "source_agreement": _amount(row.get("source_agreement")),
                "signal_age_seconds": _amount(row.get("signal_age_seconds")),
                "entry_confirmation_readings": _amount(
                    row.get("entry_confirmation_readings")
                ),
                "confirmation_price_drift": _amount(
                    row.get("confirmation_price_drift")
                ),
                "reference_sources": _amount(row.get("reference_sources")),
                "entry_cost": _amount(row.get("entry_cost")),
                "execution_edge": _amount(row.get("execution_edge")),
                "spread": _amount(row.get("spread")),
                "book_shares": _amount(row.get("book_shares")),
                "decision_id": row.get("decision_id") or None,
                "market_slug": str(row.get("market_slug") or ""),
                "selection": row.get("selection"),
                "position_side": row.get("position_side"),
                "market_type": _market_kind(row.get("signal_market")),
                "market_scope": row.get("market_scope"),
                "mode": row.get("mode"),
                "game_fraction_remaining": _amount(
                    row.get("game_fraction_remaining")
                ),
                "event_entries_60m": _amount(row.get("event_entries_60m")),
                "execution_profile": row.get("execution_profile"),
                "state": row.get("state"),
                "rejection_reason": row.get("reason"),
                "entered": bool(row.get("entered")),
                "candidate_rank": row.get("candidate_rank"),
                "competing_candidates": row.get("competing_candidates"),
                "selection_propensity": _amount(
                    row.get("selection_propensity")
                ),
                "propensity_source": row.get("propensity_source"),
                "evidence_source": "candidate_log",
            }
            for row in rows
        ]

    def _advisor_opportunities(self) -> list[dict[str, Any]]:
        """Merge the candidate log with the legacy entry-only journal history.

        The candidate log is the correct population, but it only exists from the
        migration forward. Entry rows recorded before it are still real observed
        opportunities, so they are retained and marked with their narrower
        provenance rather than silently dropped.
        """
        logged = self._candidate_observation_opportunities()
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
                    "source_agreement": _amount(
                        payload.get("source_agreement")
                    ),
                    "signal_age_seconds": _amount(
                        payload.get("signal_age_seconds")
                    ),
                    "entry_confirmation_readings": _amount(
                        payload.get("entry_confirmation_readings")
                    ),
                    "confirmation_price_drift": _amount(
                        payload.get("confirmation_price_drift")
                    ),
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
                    "state": "entry",
                    "rejection_reason": None,
                    "entered": True,
                    "candidate_rank": None,
                    "competing_candidates": None,
                    "selection_propensity": None,
                    "propensity_source": None,
                    "evidence_source": "entry_journal",
                },
            )
        # A contract already present in the candidate log must not be counted
        # twice by the frequency estimate.
        logged_keys = {
            "|".join((
                str(item.get("decision_id") or ""),
                str(item.get("market_slug") or ""),
                str(item.get("position_side") or ""),
            ))
            for item in logged
        }
        legacy = [
            item for key, item in opportunities.items()
            if key not in logged_keys
        ]
        return [*logged, *legacy]

    def advisor_dataset(self, *, source_lane: str) -> dict[str, Any]:
        """Return the retained inputs the policy advisor is allowed to use.

        The caller may combine independent live and simulated stores. This
        method deliberately returns only managed outcomes and logged candidate
        opportunities; unlabeled quote history is not converted into profit
        evidence.
        """
        closed_trades = self._advisor_closed_trades()
        opportunities = self._advisor_opportunities()
        positions = self.positions(include_hidden=True)
        logged = [
            row for row in opportunities
            if row.get("evidence_source") == "candidate_log"
        ]
        unentered = [row for row in logged if not row.get("entered")]
        return {
            "source_lane": source_lane,
            "backend": self._db.backend,
            "closed_trades": closed_trades,
            "opportunities": opportunities,
            "positions": positions,
            "logging_contract": {
                "candidate_log_observations": len(logged),
                "unentered_candidate_observations": len(unentered),
                "legacy_entry_only_observations": (
                    len(opportunities) - len(logged)
                ),
                "propensity_source": DETERMINISTIC_PROPENSITY_SOURCE,
                "randomized_exploration": False,
                # Stated explicitly so a reader cannot mistake a richer
                # candidate population for an identified causal design.
                "off_policy_identified": False,
                "note": (
                    "Rejected candidates are now logged, so qualified-frequency "
                    "estimates cover contracts that were never entered. The "
                    "logging policy is still deterministic, so selection "
                    "propensities are 0 or 1 and inverse-propensity or doubly "
                    "robust return estimates for rejected candidates remain "
                    "unidentified. Rejected candidates have no observed reward."
                ),
            },
            "summary": {
                "lane": source_lane,
                "backend": self._db.backend,
                "retained_positions": len(positions),
                "closed_trades": len(closed_trades),
                "opportunity_observations": len(opportunities),
                "unentered_candidate_observations": len(unentered),
                "independent_events": len({
                    str(row.get("event_id") or "")
                    for row in closed_trades
                    if row.get("event_id")
                }),
            },
        }

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
        closed_trades: Iterable[Mapping[str, Any]] | None = None,
        opportunities: Iterable[Mapping[str, Any]] | None = None,
        data_sources: Iterable[Mapping[str, Any]] | None = None,
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
        advisor_closed_trades = [
            row
            for row in (
                closed_trades
                if closed_trades is not None
                else self._advisor_closed_trades()
            )
            if _market_kind(row.get("market_type")) in allowed_market_types
        ]
        advisor_opportunities = [
            row
            for row in (
                opportunities
                if opportunities is not None
                else self._advisor_opportunities()
            )
            if _market_kind(row.get("market_type")) in allowed_market_types
        ]
        try:
            recommendation = recommend_policy(
                closed_trades=advisor_closed_trades,
                opportunities=advisor_opportunities,
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
        recommendation["evidence"]["data_sources"] = [
            dict(source) for source in (data_sources or ())
        ]
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

    def reset_dry_run_performance(self, confirmation: str) -> dict[str, Any]:
        """Start a fresh dry-run session tally without deleting anything.

        The display session boundary is a journal marker; every closed
        position, the execution journal, adaptive evidence, and the
        performance datasheet keep the full history. This is how a night's
        dry-run profit is read on its own without wiping the record.
        """
        if not approval_granted(confirmation):
            raise TradingPolicyError(
                approval_instruction("start a new dry-run tally session")
            )
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    """SELECT COUNT(*) FROM live_managed_positions
                       WHERE mode='dry_run' AND status='open'""",
                )
                open_positions = int(cur.fetchone()[0] or 0)
            if open_positions:
                raise TradingPolicyError(
                    f"{open_positions} open dry-run position"
                    f"{'' if open_positions == 1 else 's'} remain; let them "
                    "close (or exit them) before starting a fresh session "
                    "tally, so the session boundary stays unambiguous"
                )
            previous = dict(self.performance()["modes"]["dry_run"])
            reset_at = self._clock()
            self._journal(
                "performance_reset",
                "dry_run",
                payload={
                    "mode": "dry_run",
                    "reset_at": reset_at,
                    "previous_total_positions": previous["total_positions"],
                    "previous_wins": previous["wins"],
                    "previous_losses": previous["losses"],
                    "previous_pushes": previous["pushes"],
                    "previous_total_net_usd": previous["total_net_usd"],
                    "positions_preserved": True,
                    "execution_journal_preserved": True,
                    "risk_history_preserved": True,
                    "reason": "operator_started_new_dry_run_tally_session",
                },
            )
        reset_at_iso = datetime.fromtimestamp(reset_at, timezone.utc).isoformat()
        return {
            "mode": "dry_run",
            "reset_at": reset_at_iso,
            "previous": previous,
            "positions_preserved": True,
            "execution_journal_preserved": True,
            "risk_history_preserved": True,
            "summary": (
                "Dry-run W-L-P and net display reset to zero for a new "
                "session. Every historical position and all evidence were "
                "preserved."
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
        self._candidate_confirmations.clear()
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
        self._candidate_confirmations.clear()
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
        self._candidate_confirmations.pop(candidate_key, None)
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
            self._candidate_confirmations.clear()
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
        segment_results: Mapping[
            str,
            Mapping[str, tuple[float, float]],
        ] | None = None,
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
                segment_results=segment_results or {},
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
        segment_results: Mapping[
            str,
            Mapping[str, tuple[float, float]],
        ],
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
            segment_results=segment_results,
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
            confirmed, confirmation_reason, confirmation_audit = (
                self._entry_confirmation_ready(candidate, now=now)
            )
            evaluation.update(confirmation_audit)
            if not confirmed:
                evaluation.update(
                    state="confirming",
                    reason=confirmation_reason,
                )
                continue
            candidate = replace(
                candidate,
                entry_confirmation_count=int(
                    confirmation_audit["entry_confirmation_readings"]
                ),
                entry_confirmation_price_drift=float(
                    confirmation_audit["confirmation_price_drift"]
                ),
            )
            if venue_checks >= 3:
                evaluation.update(
                    state="queued",
                    reason="higher-ranked candidates used this cycle's venue-check budget",
                )
                break
            venue_checks += 1
            self._candidate_seen[candidate.key] = now
            # An order attempt consumes the confirmation window. The cooldown
            # recorded above already bounds how quickly this contract can be
            # retried, so the next attempt re-earns its readings from scratch.
            self._candidate_confirmations.pop(candidate.key, None)
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
        logged_candidates = self._record_candidate_observations(
            mapped,
            evaluations,
            now=now,
        )
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
            "logged_candidates": logged_candidates,
            "summary": self._last_cycle_summary,
            "venue_sync": venue_sync,
        }

    def _record_candidate_observations(
        self,
        mapped: Sequence[tuple[MappedCandidate, dict[str, Any]]],
        evaluations: Sequence[dict[str, Any]],
        *,
        now: float,
    ) -> int:
        """Log the population the policy chose from, not only what it chose.

        Comparing an alternative execution policy against logged behaviour needs
        the rejected candidates too. Without them the advisor can only re-filter
        past fills, so a looser policy can never appear to trade more than the
        policy that produced the data.

        One row per contract per candidate cooldown is deliberate. The cooldown
        is the shortest interval at which the same contract could actually be
        entered, so it is the correct sampling rate for a per-hour opportunity
        estimate as well as a bound on retained volume.
        """
        # Observing candidates is not trading. Attribute rows to an open session
        # when one exists, but never start one - only an actual fill does that.
        session_id = self._open_policy_session_id()
        fingerprint = _policy_fingerprint(self._policy)
        mode = self._policy.execution_mode
        cooldown = max(1.0, float(self._policy.candidate_cooldown_seconds))
        ranks = {
            id(evaluation): index for index, (_candidate, evaluation)
            in enumerate(mapped)
        }
        candidates_by_evaluation = {
            id(evaluation): candidate for candidate, evaluation in mapped
        }
        competing = len(mapped)
        rows: list[tuple[Any, ...]] = []
        for evaluation in evaluations:
            candidate = candidates_by_evaluation.get(id(evaluation))
            state = str(evaluation.get("state") or "unknown")
            market_slug = str(evaluation.get("us_market_slug") or "")
            key = ":".join((
                str(evaluation.get("event_id") or ""),
                market_slug or str(evaluation.get("market") or ""),
                str(evaluation.get("outcome") or ""),
            ))
            last = self._candidate_log_seen.get(key, 0.0)
            if now - last < cooldown:
                continue
            self._candidate_log_seen[key] = now
            entered = state in _ENTERED_CANDIDATE_STATES
            rank = ranks.get(id(evaluation))
            rows.append((
                str(uuid4()),
                now,
                mode,
                session_id,
                fingerprint,
                str(evaluation.get("execution_profile") or "") or None,
                str(evaluation.get("event_id") or ""),
                evaluation.get("event_name"),
                (
                    candidate.signal.decision_id
                    or candidate.signal.decision_hash
                    if candidate is not None else None
                ),
                evaluation.get("market"),
                _market_kind(evaluation.get("market")),
                evaluation.get("market_scope"),
                evaluation.get("outcome"),
                candidate.position_side if candidate is not None else None,
                evaluation.get("us_event_slug"),
                market_slug or None,
                state,
                (str(evaluation.get("reason"))[:500]
                 if evaluation.get("reason") else None),
                1 if candidate is not None else 0,
                1 if evaluation.get("us_entry_cost") is not None else 0,
                1 if entered else 0,
                _amount(evaluation.get("signal_edge")),
                _amount(evaluation.get("signal_quality")),
                _amount(evaluation.get("source_agreement")),
                (
                    _signal_age_seconds(candidate.signal, now)
                    if candidate is not None else None
                ),
                (
                    int(candidate.signal.n_reference_sources)
                    if candidate is not None else None
                ),
                _amount(evaluation.get("us_entry_cost")),
                _amount(evaluation.get("us_execution_edge")),
                candidate.spread if candidate is not None else None,
                candidate.book_shares if candidate is not None else None,
                candidate.mapping_score if candidate is not None else None,
                (
                    candidate.game_fraction_remaining
                    if candidate is not None else None
                ),
                (
                    int(candidate.event_entries_60m)
                    if candidate is not None else None
                ),
                (
                    int(evaluation["entry_confirmation_readings"])
                    if evaluation.get("entry_confirmation_readings") is not None
                    else None
                ),
                _amount(evaluation.get("confirmation_price_drift")),
                rank,
                competing,
                # Degenerate by construction: the saved policy is deterministic
                # given a ranked list, so exactly one candidate has propensity 1.
                (1.0 if entered else 0.0) if rank is not None else None,
                DETERMINISTIC_PROPENSITY_SOURCE,
            ))
        if len(self._candidate_log_seen) > 20_000:
            oldest = sorted(
                self._candidate_log_seen,
                key=self._candidate_log_seen.get,
            )
            for stale in oldest[:5_000]:
                self._candidate_log_seen.pop(stale, None)
        if not rows:
            return 0
        with self._lock:
            with self._db.transaction() as cur:
                for row in rows:
                    self._db.execute(
                        cur,
                        """INSERT INTO candidate_observations(
                             id,observed_ts,mode,policy_session_id,
                             policy_fingerprint,execution_profile,event_id,
                             event_name,decision_id,signal_market,market_type,
                             market_scope,selection,position_side,us_event_slug,
                             market_slug,state,reason,mapped,executable,entered,
                             signal_edge,signal_quality,source_agreement,
                             signal_age_seconds,reference_sources,entry_cost,
                             execution_edge,spread,book_shares,mapping_score,
                             game_fraction_remaining,event_entries_60m,
                             entry_confirmation_readings,confirmation_price_drift,
                             candidate_rank,competing_candidates,
                             selection_propensity,propensity_source)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                   %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                   %s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        row,
                    )
        self._candidate_log_writes += len(rows)
        if self._candidate_log_writes >= 500:
            self._candidate_log_writes = 0
            self._prune_candidate_observations()
        return len(rows)

    def _prune_candidate_observations(
        self,
        maximum: int = CANDIDATE_OBSERVATION_LIMIT,
    ) -> None:
        """Bound the candidate log without touching entry or exit evidence."""
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    "SELECT COUNT(*) FROM candidate_observations",
                )
                count = int(cur.fetchone()[0] or 0)
            excess = count - maximum
            if excess <= 0:
                return
            with self._db.transaction() as cur:
                # Three tiers, cheapest evidence first.
                #
                # Unmapped rows dominate volume - a signal with no tradable US
                # contract is logged on every cooldown, and in practice that is
                # ~99% of rows - while carrying the least information: there was
                # never a contract to trade. They must age out before anything
                # executable, or a busy slate silently evicts the mapped
                # population the advisor depends on.
                #
                # Then unentered mapped rows. Entered rows are last because they
                # are the only ones carrying an observed reward.
                self._db.execute(
                    cur,
                    """SELECT id FROM candidate_observations
                       WHERE entered=0
                       ORDER BY mapped ASC, observed_ts ASC LIMIT %s""",
                    (excess,),
                )
                stale = [row[0] for row in cur.fetchall()]
                for row_id in stale:
                    self._db.execute(
                        cur,
                        "DELETE FROM candidate_observations WHERE id=%s",
                        (row_id,),
                    )

    def _entry_confirmation_ready(
        self,
        candidate: MappedCandidate,
        *,
        now: float,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Require distinct retained signal readings without chasing the ask."""
        policy = candidate.execution_policy
        required = policy.entry_confirmation_readings
        if required <= 1:
            self._candidate_confirmations.pop(candidate.key, None)
            return True, "", {
                "entry_confirmation_readings": 1,
                "configured_entry_confirmation_readings": 1,
                "confirmation_price_drift": 0.0,
            }

        observed_at = candidate.signal.observed_at
        observed_ts = observed_at.timestamp() if observed_at is not None else now
        maximum_gap = max(
            5.0,
            policy.cycle_seconds * 3.0,
            policy.max_signal_age_seconds,
        )
        state = self._candidate_confirmations.get(candidate.key)
        if (
            state is None
            or state.get("execution_profile") != candidate.execution_profile_key
            or now - float(state.get("last_new_at") or 0.0) > maximum_gap
        ):
            state = {
                "count": 1,
                "first_entry_cost": candidate.entry_cost,
                "last_signal_ts": observed_ts,
                "last_new_at": now,
                "execution_profile": candidate.execution_profile_key,
            }
            self._candidate_confirmations[candidate.key] = state
        elif observed_ts > float(state.get("last_signal_ts") or 0.0) + 1e-6:
            drift = max(
                0.0,
                candidate.entry_cost - float(state["first_entry_cost"]),
            )
            if drift > policy.max_confirmation_price_drift:
                state = {
                    "count": 1,
                    "first_entry_cost": candidate.entry_cost,
                    "last_signal_ts": observed_ts,
                    "last_new_at": now,
                    "execution_profile": candidate.execution_profile_key,
                }
                self._candidate_confirmations[candidate.key] = state
                reason = (
                    f"ask worsened {drift * 100:.1f}c during confirmation, "
                    f"above the configured "
                    f"{policy.max_confirmation_price_drift * 100:.1f}c maximum; "
                    f"confirmation restarted at 1/{required}"
                )
                return False, reason, {
                    "entry_confirmation_readings": 1,
                    "configured_entry_confirmation_readings": required,
                    "confirmation_price_drift": drift,
                    "configured_max_confirmation_price_drift": (
                        policy.max_confirmation_price_drift
                    ),
                }
            state["count"] = int(state["count"]) + 1
            state["last_signal_ts"] = observed_ts
            state["last_new_at"] = now

        count = int(state["count"])
        drift = max(
            0.0,
            candidate.entry_cost - float(state["first_entry_cost"]),
        )
        audit = {
            "entry_confirmation_readings": count,
            "configured_entry_confirmation_readings": required,
            "confirmation_price_drift": drift,
            "configured_max_confirmation_price_drift": (
                policy.max_confirmation_price_drift
            ),
        }
        if count < required:
            if len(self._candidate_confirmations) > 5_000:
                oldest = sorted(
                    self._candidate_confirmations,
                    key=lambda key: float(
                        self._candidate_confirmations[key].get("last_new_at")
                        or 0.0
                    ),
                )
                for stale in oldest[:1_000]:
                    self._candidate_confirmations.pop(stale, None)
            return False, (
                f"qualified reading {count}/{required}; waiting for a newer "
                "retained signal before entry"
            ), audit

        # A satisfied window is retained until an entry is actually attempted.
        # Clearing it here discarded earned confirmations whenever the
        # venue-check budget, the one-entry-per-cycle limit, or a transient
        # venue failure stopped this candidate short of an order, which
        # systematically starved lower-ranked candidates. Drift protection is
        # unaffected: the next newer reading still re-checks the ask against
        # the window's first observed cost.
        return True, "", audit

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
            "event_name": event.name,
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
            "configured_max_quality": self._policy.max_signal_quality,
            "source_agreement": signal.quality_agreement,
            "configured_min_source_agreement": (
                self._policy.min_source_agreement
            ),
            "configured_max_signal_age_seconds": (
                self._policy.max_signal_age_seconds
            ),
            "reference_sources": signal.n_reference_sources,
            "configured_min_reference_sources": (
                self._policy.min_reference_sources
            ),
            "require_engine_entry": self._policy.require_engine_entry,
            "required_engine_gates": list(self._policy.required_engine_gates),
            "allowed_market_types": list(self._policy.allowed_market_types),
            "allowed_market_scopes": list(self._policy.allowed_market_scopes),
            "market_scope": market_scope(signal.market),
            "selected_engine_gate_results": self._selected_engine_gate_results(
                signal
            ),
            "us_event_slug": us_event_slug or None,
            "us_market_slug": None,
            "us_entry_cost": None,
            "us_execution_edge": None,
        }
        if candidate is not None:
            effective = candidate.execution_policy
            item.update(
                us_event_slug=str(candidate.us_event.get("slug") or "") or None,
                us_market_slug=str(candidate.market.get("slug") or "") or None,
                us_entry_cost=candidate.entry_cost,
                us_execution_edge=candidate.execution_edge,
                execution_profile=candidate.execution_profile_key,
                configured_min_edge=effective.min_edge,
                configured_max_edge=effective.max_edge,
                configured_min_quality=effective.min_signal_quality,
                configured_max_quality=effective.max_signal_quality,
                configured_min_source_agreement=(
                    effective.min_source_agreement
                ),
                configured_max_signal_age_seconds=(
                    effective.max_signal_age_seconds
                ),
                configured_entry_confirmation_readings=(
                    effective.entry_confirmation_readings
                ),
                configured_max_confirmation_price_drift=(
                    effective.max_confirmation_price_drift
                ),
                configured_min_reference_sources=(
                    effective.min_reference_sources
                ),
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
            _REJECTION_KEY_NUMBERS.sub("N", reason),
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
                "market_scope": (
                    market_scope(signal.market) if signal else None
                ),
                "signal_outcome": signal.outcome if signal else None,
                "signal_edge": signal.edge if signal else None,
                "required_edge": signal.required_edge if signal else None,
                "configured_min_edge": self._policy.min_edge,
                "configured_max_edge": self._policy.max_edge,
                "signal_quality": signal.confidence if signal else None,
                "configured_min_quality": self._policy.min_signal_quality,
                "configured_max_quality": self._policy.max_signal_quality,
                "source_agreement": (
                    signal.quality_agreement if signal else None
                ),
                "configured_min_source_agreement": (
                    self._policy.min_source_agreement
                ),
                "configured_max_signal_age_seconds": (
                    self._policy.max_signal_age_seconds
                ),
                "reference_sources": signal.n_reference_sources if signal else None,
                "configured_min_reference_sources": (
                    self._policy.min_reference_sources
                ),
                "allowed_market_scopes": list(
                    self._policy.allowed_market_scopes
                ),
                "allow_live_segment_markets": (
                    self._policy.allow_live_segment_markets
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
        global_policy = self._policy
        market_type = _market_kind(signal.market)
        signal_scope = market_scope(signal.market)
        if signal_scope not in global_policy.allowed_market_scopes:
            return None, (
                f"{signal_scope.replace('_', ' ')} markets are disabled by the "
                "automatic-entry segment policy"
            )
        if (
            global_policy.execution_mode == "live"
            and signal_scope != FULL_GAME_SCOPE
            and not global_policy.allow_live_segment_markets
        ):
            return None, (
                "MLB segment live orders are locked; collect and review dry-run "
                "segment results, then explicitly enable live segment markets"
            )
        game_fraction = _baseball_fraction_remaining(event, game_state)
        policy, profile_key = global_policy.execution_policy_for(
            market_type,
            game_fraction,
        )
        if policy is None:
            if profile_key == "global/fallback-disabled":
                return None, (
                    "global entry fallback is not authorized and no active "
                    f"{market_type or 'unknown'} line profile matched"
                )
            if profile_key.startswith("global/"):
                return None, (
                    f"{market_type or 'unknown'} lines are disabled by the "
                    "global automatic-entry line-type policy"
                )
            return None, (
                f"{profile_key} is disabled by the line-specific execution policy"
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
        if signal.confidence > policy.max_signal_quality:
            return None, (
                f"existing signal quality {signal.confidence:.0f}/100 exceeds "
                f"the configured {policy.max_signal_quality:.0f}/100 ceiling"
            )
        if signal.quality_agreement < policy.min_source_agreement:
            return None, (
                f"source agreement {signal.quality_agreement:.0f}/100 is below "
                f"the configured {policy.min_source_agreement:.0f}/100 floor"
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
        if age < -5 or age > policy.max_signal_age_seconds:
            return None, (
                "signal is future-dated or older than the configured "
                f"{policy.max_signal_age_seconds:.0f}s maximum"
            )

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
        required, fee_floor = _required_execution_edge(
            policy, signal, entry_cost
        )
        if (
            execution_edge < required
            and (
                policy.execution_mode == "live"
                or policy.require_engine_entry
            )
        ):
            detail = (
                f"US execution edge {execution_edge * 100:+.1f}c is below "
                f"the required {required * 100:+.1f}c"
            )
            if fee_floor is not None and fee_floor >= required - 1e-12:
                detail += (
                    f"; at {entry_cost * 100:.0f}c the round-trip taker fee "
                    f"alone costs {ROUND_TRIP_TAKER_FEE_RATE * entry_cost * (1 - entry_cost) * 100:.1f}c "
                    "of edge"
                )
            return None, detail
        if execution_edge > policy.max_edge:
            return None, (
                f"US execution edge {execution_edge * 100:+.1f}c exceeds "
                f"the configured {policy.max_edge * 100:+.1f}c ceiling"
            )
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
            execution_policy=policy,
            execution_profile_key=profile_key,
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
        policy = candidate.execution_policy
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
        if (
            policy.max_profile_exposure_usd > 0
            or policy.max_profile_open_positions > 0
            or policy.max_profile_orders_per_hour > 0
        ):
            profile_key = candidate.execution_profile_key
            profile_positions = [
                row for row in open_positions
                if self._position_profile_key(row) == profile_key
            ]
            profile_exposure = sum(
                float(row["cost_basis"]) for row in profile_positions
            )
            if (
                policy.max_profile_open_positions > 0
                and len(profile_positions) >= policy.max_profile_open_positions
            ):
                reasons.append(
                    f"{profile_key} already holds "
                    f"{len(profile_positions)} of its "
                    f"{policy.max_profile_open_positions} allowed open positions"
                )
            if (
                policy.max_profile_exposure_usd > 0
                and profile_exposure >= policy.max_profile_exposure_usd
            ):
                reasons.append(
                    f"{profile_key} exposure ${profile_exposure:.2f} has "
                    f"reached its ${policy.max_profile_exposure_usd:.2f} "
                    "profile limit"
                )
            if policy.max_profile_orders_per_hour > 0:
                profile_entries = self._profile_entries_last_hour(
                    profile_key,
                    policy.execution_mode,
                )
                if profile_entries >= policy.max_profile_orders_per_hour:
                    reasons.append(
                        f"{profile_key} used {profile_entries} of its "
                        f"{policy.max_profile_orders_per_hour} hourly entries"
                    )
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
        if policy.max_profile_exposure_usd > 0:
            profile_remaining = policy.max_profile_exposure_usd - sum(
                float(row["cost_basis"])
                for row in open_positions
                if self._position_profile_key(row)
                == candidate.execution_profile_key
            )
            capacity = min(capacity, profile_remaining)
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
                # Re-derived against the authenticated fill price: the fee floor
                # moves with the price actually available, not the public quote.
                required_edge, fee_floor = _required_execution_edge(
                    policy,
                    candidate.signal,
                    candidate.entry_cost,
                )
                if candidate.execution_edge < required_edge:
                    detail = (
                        f"authenticated US execution edge "
                        f"{candidate.execution_edge * 100:+.1f}c is below the "
                        f"required {required_edge * 100:+.1f}c"
                    )
                    if fee_floor is not None and fee_floor >= required_edge - 1e-12:
                        detail += (
                            " after the round-trip taker fee at "
                            f"{candidate.entry_cost * 100:.0f}c"
                        )
                    reasons.append(detail)
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
            "market_scope": market_scope(candidate.signal.market),
            "allow_live_segment_markets": policy.allow_live_segment_markets,
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
            "required_edge": _required_execution_edge(
                policy, candidate.signal, candidate.entry_cost
            )[0],
            "fee_implied_edge_floor": _fee_implied_edge_floor(
                candidate.entry_cost, policy.fee_edge_margin
            ),
            "round_trip_fee_edge_cost": _fee_implied_edge_floor(
                candidate.entry_cost, 1.0
            ),
            "configured_fee_edge_margin": policy.fee_edge_margin,
            "configured_min_quality": policy.min_signal_quality,
            "configured_max_quality": policy.max_signal_quality,
            "source_agreement": candidate.signal.quality_agreement,
            "configured_min_source_agreement": policy.min_source_agreement,
            "configured_max_signal_age_seconds": policy.max_signal_age_seconds,
            "configured_entry_confirmation_readings": (
                policy.entry_confirmation_readings
            ),
            "configured_max_confirmation_price_drift": (
                policy.max_confirmation_price_drift
            ),
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
            "execution_profile": candidate.execution_profile_key,
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
                required = max(policy.min_edge, candidate.signal.required_edge)
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
        segment_results: Mapping[
            str,
            Mapping[str, tuple[float, float]],
        ],
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
            position_policy = self._position_execution_policy(position)
            market = markets.get(position["market_slug"])
            event_context = monitored_by_id.get(str(position["event_id"]))
            event = event_context[0] if event_context is not None else None
            position_scope = str(
                position.get("market_scope") or FULL_GAME_SCOPE
            )
            completed_scores = segment_results.get(
                str(position["event_id"]),
                {},
            ).get(position_scope)
            if (
                position["mode"] == "dry_run"
                and event is not None
                and completed_scores is not None
            ):
                settlement = _dry_segment_settlement_value(
                    position,
                    event,
                    completed_scores,
                )
                if settlement is not None:
                    self._close_position(
                        position["id"],
                        exit_value=settlement,
                        reason="dry_run_segment_resolved",
                        order_id=None,
                    )
                    self._journal(
                        "settlement",
                        "simulated_segment_resolution",
                        event_id=position["event_id"],
                        event_name=position["event_name"],
                        market_slug=position["market_slug"],
                        selection=position["selection"],
                        payload={
                            "position_id": position["id"],
                            "market_scope": position_scope,
                            "home_score": completed_scores[0],
                            "away_score": completed_scores[1],
                            "settlement_value": settlement,
                            "source": "official_mlb_segment_end_state",
                        },
                    )
                    marked += 1
                    exited += 1
                    continue
            probability = (
                self._position_probability(position, market, monitored_by_id)
                if market is not None
                else None
            )
            quantity = float(position["quantity"])
            exit_depth: float | None = None
            quote_source = "public_us_snapshot"
            gross_exit_value: float | None = None
            estimated_exit_fee = 0.0
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
                gross_exit_value = exit_value
                estimated_exit_fee = _estimated_taker_fee(
                    quantity=quantity,
                    contract_price=exit_long_price,
                )
                # The entry cost already includes its actual execution fee.
                # Compare it with a conservative fee-adjusted executable exit,
                # not the visually greener raw bid.
                exit_value = max(
                    0.0,
                    gross_exit_value - estimated_exit_fee / quantity,
                )
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
                gross_exit_value = exit_value
            entry_cost = float(position["entry_cost"])
            return_fraction = exit_value / entry_cost - 1.0
            peak = max(float(position["highest_exit_value"]), exit_value)
            # Adverse extreme, seeded from entry cost so a position that only
            # ever moved up records no adverse excursion rather than a null.
            prior_trough = _amount(position.get("lowest_exit_value"))
            trough = min(
                prior_trough if prior_trough is not None else entry_cost,
                exit_value,
            )
            held_minutes = (
                self._clock() - float(position["opened_ts"])
            ) / 60.0
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
                    profit_target=position_policy.profit_target,
                    trailing_drawdown=position_policy.trailing_drawdown,
                    exit_edge=position_policy.exit_edge,
                    stop_loss=position_policy.stop_loss,
                )
            else:
                adaptive_exit = self._adaptive_exit.base_decision(
                    profile=self._policy.adaptive_exit_profile,
                    reason="adaptive MLB exit learning is disabled",
                    applicable=False,
                    profit_target=position_policy.profit_target,
                    trailing_drawdown=position_policy.trailing_drawdown,
                    exit_edge=position_policy.exit_edge,
                    stop_loss=position_policy.stop_loss,
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
                policy=position_policy,
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
                    or held_minutes >= position_policy.min_hold_minutes
                )
            )
            profit_lock_armed = prior_lock or target_confirmed
            new_lock_ts = (
                self._clock()
                if profit_lock_armed and not prior_lock
                else None
            )
            (
                reversal_confirmed,
                reversal_triggered_ts,
                reversal_observation_count,
            ) = self._reversal_confirmation(
                position,
                position_policy,
                current_edge,
            )
            profit_guard = self._profit_guard_decision(
                position,
                exit_value=exit_value,
                peak=peak,
                current_edge=current_edge,
                profit_lock_armed=profit_lock_armed,
                adaptive_exit=adaptive_exit,
                policy=position_policy,
                reversal_confirmed=reversal_confirmed,
            )
            prior_floor_missed_ts = position.get(
                "profit_floor_missed_ts"
            )
            prior_floor_missed_count = int(
                position.get("profit_floor_missed_count") or 0
            )
            prior_floor_low = _amount(
                position.get("profit_floor_low_exit_value")
            )
            if profit_guard["blocked"]:
                profit_floor_missed_ts = (
                    prior_floor_missed_ts or self._clock()
                )
                profit_floor_missed_count = (
                    prior_floor_missed_count + 1
                )
                profit_floor_low_exit_value = min(
                    exit_value,
                    (
                        prior_floor_low
                        if prior_floor_low is not None
                        else exit_value
                    ),
                )
            else:
                profit_floor_missed_ts = prior_floor_missed_ts
                profit_floor_missed_count = prior_floor_missed_count
                profit_floor_low_exit_value = prior_floor_low
                if (
                    prior_floor_missed_count
                    and profit_guard["floor_satisfied"]
                ):
                    profit_guard["status"] = "recovered_above_floor"
                    profit_guard["reason"] = (
                        "executable value recovered above the protected "
                        "fee-adjusted profit floor"
                    )
            profit_guard["missed_count"] = profit_floor_missed_count
            profit_guard["lowest_value_while_missed"] = (
                profit_floor_low_exit_value
            )
            self._update_mark(
                position["id"],
                exit_value=exit_value,
                peak=peak,
                trough=trough,
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
                reversal_triggered_ts=reversal_triggered_ts,
                reversal_observation_count=reversal_observation_count,
                profit_floor_missed_ts=profit_floor_missed_ts,
                profit_floor_missed_count=profit_floor_missed_count,
                profit_floor_low_exit_value=profit_floor_low_exit_value,
                profit_guard_payload=json.dumps(
                    profit_guard,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            marked += 1
            mlb_shadow = _mlb_shadow_state(
                game_states.get(str(position["event_id"]))
            )
            pitcher_shadow = self._pitcher_change_shadow(event, mlb_shadow)
            if mlb_shadow is not None and pitcher_shadow is not None:
                mlb_shadow = {**mlb_shadow, **pitcher_shadow}
            self._journal(
                "mark",
                "updated",
                event_id=position["event_id"],
                event_name=position["event_name"],
                market_slug=position["market_slug"],
                selection=position["selection"],
                payload={
                    "position_id": position["id"],
                    "mlb_shadow": mlb_shadow,
                    "exit_value": exit_value,
                    "gross_exit_value": gross_exit_value,
                    "estimated_exit_fee": estimated_exit_fee,
                    "fee_adjusted_exit_value": exit_value,
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
                    "reversal_confirmation": {
                        "confirmed": reversal_confirmed,
                        "observations": reversal_observation_count,
                        "required": (
                            position_policy.reversal_confirmation_readings
                        ),
                    },
                    "profit_guard": profit_guard,
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
                profit_guard=profit_guard,
                policy=position_policy,
                reversal_confirmed=reversal_confirmed,
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
                        policy=position_policy,
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
        policy: TradingPolicy | None = None,
    ) -> tuple[str | None, dict[str, Any], float | None, int, float | None]:
        """Confirm an ordinary MLB stop while preserving a catastrophic bound."""
        policy = policy or self._policy
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
            # Name the exact cause: the aggregate bypass count was previously
            # impossible to decompose into "feed down" vs "state unparseable".
            if event is None or state is None:
                bypass_reason = "no live MLB game state was available"
            else:
                bypass_reason = "MLB state lacks a usable inning or half"
            return self._stateless_stop_decision(
                policy=policy,
                base_payload=base_payload,
                bypass_reason=bypass_reason,
                exit_value=exit_value,
                current_edge=current_edge,
                return_fraction=return_fraction,
                adaptive_exit=adaptive_exit,
                prior_trigger=prior_trigger,
                prior_count=prior_count,
                prior_low=prior_low,
            )
        state_age = max(
            0.0,
            self._clock() - float(context.pop("state_received_ts")),
        )
        context["state_age_seconds"] = state_age
        base_payload["game_state"] = context
        if state_age > 180.0:
            return self._stateless_stop_decision(
                policy=policy,
                base_payload=base_payload,
                bypass_reason=f"MLB game state is stale ({state_age:.0f}s old)",
                exit_value=exit_value,
                current_edge=current_edge,
                return_fraction=return_fraction,
                adaptive_exit=adaptive_exit,
                prior_trigger=prior_trigger,
                prior_count=prior_count,
                prior_low=prior_low,
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

        return self._bounded_stop_confirmation(
            policy=policy,
            base_payload=base_payload,
            exit_value=exit_value,
            current_edge=current_edge,
            adaptive_exit=adaptive_exit,
            prior_trigger=prior_trigger,
            prior_count=prior_count,
            prior_low=prior_low,
            fraction_remaining=float(context["fraction_remaining"]),
        )

    def _stateless_stop_decision(
        self,
        *,
        policy: TradingPolicy,
        base_payload: dict[str, Any],
        bypass_reason: str,
        exit_value: float,
        current_edge: float | None,
        return_fraction: float,
        adaptive_exit: AdaptiveExitDecision,
        prior_trigger: float | None,
        prior_count: int,
        prior_low: float | None,
    ) -> tuple[str | None, dict[str, Any], float | None, int, float | None]:
        """Decide a stop that fired without usable MLB state.

        Historically this always sold immediately, which made the
        confirmation-gated stop inert in practice: most stop hits arrive
        without a usable inning state. With ``stateless_stop_confirmation``
        the ordinary stop instead runs the same bounded confirmation window
        on price alone, while catastrophic losses and material model
        reversals still exit immediately.
        """
        immediate_low = min(
            exit_value,
            prior_low if prior_low is not None else exit_value,
        )
        if not policy.stateless_stop_confirmation:
            base_payload.update(status="immediate", reason=bypass_reason)
            return (
                "hard_stop_loss",
                base_payload,
                prior_trigger,
                prior_count,
                immediate_low,
            )
        base_payload["state_unavailable_reason"] = bypass_reason
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
                immediate_low,
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
                immediate_low,
            )
        return self._bounded_stop_confirmation(
            policy=policy,
            base_payload=base_payload,
            exit_value=exit_value,
            current_edge=current_edge,
            adaptive_exit=adaptive_exit,
            prior_trigger=prior_trigger,
            prior_count=prior_count,
            prior_low=prior_low,
            fraction_remaining=None,
        )

    def _bounded_stop_confirmation(
        self,
        *,
        policy: TradingPolicy,
        base_payload: dict[str, Any],
        exit_value: float,
        current_edge: float | None,
        adaptive_exit: AdaptiveExitDecision,
        prior_trigger: float | None,
        prior_count: int,
        prior_low: float | None,
        fraction_remaining: float | None,
    ) -> tuple[str | None, dict[str, Any], float | None, int, float | None]:
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
        grace_minutes = policy.stop_grace_minutes
        if fraction_remaining is None:
            # Without game state the remaining-time shrink cannot run, so the
            # price-only window is capped at one minute rather than trusting
            # the full configured grace late in a game.
            grace_minutes = min(grace_minutes, 1.0)
        elif fraction_remaining <= 0.12:
            grace_minutes = min(grace_minutes, 0.5)
        elif fraction_remaining <= 0.25:
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
        with_state = fraction_remaining is not None
        base_payload.update(
            status="confirmed" if confirmed else "observing_recovery",
            reason=(
                "bounded confirmation window expired with the stop still breached"
                if confirmed
                else (
                    "MLB state remains live and model edge has not materially reversed"
                    if with_state
                    else "price-only confirmation window is observing for recovery"
                )
            ),
            confirmation_basis="mlb_state" if with_state else "price_only",
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

    def _pitcher_change_shadow(
        self,
        event: Event | None,
        shadow: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Track per-event pitcher identity and flag recent bullpen changes.

        Shadow telemetry: journals one row per detected change and enriches
        mark payloads; no exit or entry rule reads it yet.
        """
        if event is None or not shadow:
            return None
        pitcher_id = shadow.get("pitcher_id")
        if not pitcher_id:
            return None
        now = self._clock()
        tracker = self._event_pitchers.get(event.id)
        if tracker is None:
            tracker = {
                "pitcher_id": pitcher_id,
                "previous_pitcher_id": None,
                "changed_ts": None,
            }
            self._event_pitchers[event.id] = tracker
        elif tracker.get("pitcher_id") != pitcher_id:
            tracker.update(
                previous_pitcher_id=tracker.get("pitcher_id"),
                pitcher_id=pitcher_id,
                changed_ts=now,
            )
            self._journal(
                "shadow",
                "pitcher_change",
                event_id=event.id,
                event_name=event.name,
                payload={
                    "previous_pitcher_id": tracker["previous_pitcher_id"],
                    "pitcher_id": pitcher_id,
                    "pitcher_name": shadow.get("pitcher_name"),
                    "inning": shadow.get("inning"),
                    "half": shadow.get("half"),
                },
            )
        changed_ts = tracker.get("changed_ts")
        seconds_since = now - changed_ts if changed_ts is not None else None
        return {
            "pitcher_change_recent": (
                seconds_since is not None and seconds_since <= 180.0
            ),
            "seconds_since_pitcher_change": (
                round(seconds_since, 1) if seconds_since is not None else None
            ),
            "previous_pitcher_id": tracker.get("previous_pitcher_id"),
        }

    def _reversal_confirmation(
        self,
        position: Mapping[str, Any],
        policy: TradingPolicy,
        current_edge: float | None,
    ) -> tuple[bool, float | None, int]:
        """Consecutive-reading confirmation for the model-reversal exit.

        The 2026-08-02 settlement grading showed 62-65% of single-reading
        reversal exits going on to win, on both lanes independently — the
        same one-quote failure mode the stop guard already fixed. A recovery
        above the threshold resets the streak; with the default of one
        required reading this is exactly the historical behavior.
        """
        threshold = -max(0.03, policy.min_edge)
        if current_edge is None or current_edge > threshold:
            return False, None, 0
        required = max(1, int(policy.reversal_confirmation_readings))
        now = self._clock()
        window = max(
            60.0,
            float(policy.cycle_seconds) * float(required + 2),
        )
        prior_ts = _amount(position.get("reversal_triggered_ts"))
        prior_count = int(position.get("reversal_observation_count") or 0)
        consecutive = prior_ts is not None and now - prior_ts <= window
        triggered_at = prior_ts if consecutive else now
        count = prior_count + 1 if consecutive else 1
        return count >= required, triggered_at, count

    def _profit_guard_decision(
        self,
        position: Mapping[str, Any],
        *,
        exit_value: float,
        peak: float,
        current_edge: float | None,
        profit_lock_armed: bool,
        adaptive_exit: AdaptiveExitDecision | None,
        policy: TradingPolicy | None = None,
        reversal_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Prevent an ordinary profit exit from chasing a gap below its floor.

        The floor applies only to trailing/edge-decay profit exits. A hard stop
        or a material model reversal remains authoritative and may realize a
        loss. ``exit_value`` is already the estimated fee-adjusted executable
        value for live positions.
        """
        policy = policy or self._policy
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
        edge_decay = (
            current_edge is not None
            and current_edge <= effective_exit_edge
        )
        trailing_pullback = (
            peak > 0
            and exit_value <= peak * (1.0 - effective_trailing)
        )
        triggered = bool(
            profit_lock_armed
            and (edge_decay or trailing_pullback)
        )
        entry_cost = float(position["entry_cost"])
        floor_value = entry_cost * (
            1.0 + policy.minimum_locked_profit
        )
        floor_satisfied = exit_value + 1e-12 >= floor_value
        # A material reversal must clear the same confirmation the
        # model_reversal exit uses; a single adverse reading can no longer
        # punch through the protected profit floor.
        material_reversal = bool(reversal_confirmed)
        material_reversal_override = bool(
            triggered
            and material_reversal
            and not floor_satisfied
        )
        blocked = bool(
            triggered
            and not floor_satisfied
            and not material_reversal_override
        )
        if blocked:
            status = "profit_floor_missed_holding"
            reason = (
                "profit protection triggered after the executable market "
                "gapped below the configured fee-adjusted floor; ordinary "
                "profit selling is paused until recovery, a hard stop, or a "
                "material model reversal"
            )
        elif material_reversal_override:
            status = "material_reversal_override"
            reason = (
                "the protected floor cannot override a material model reversal"
            )
        elif triggered:
            status = "profit_exit_eligible"
            reason = "profit exit remains above the protected floor"
        elif profit_lock_armed:
            status = "armed"
            reason = "profit protection is armed; no exit trigger is active"
        else:
            status = "inactive"
            reason = "profit protection has not armed"
        return {
            "status": status,
            "reason": reason,
            "blocked": blocked,
            "profit_lock_armed": profit_lock_armed,
            "profit_exit_triggered": triggered,
            "edge_decay_triggered": edge_decay,
            "trailing_pullback_triggered": trailing_pullback,
            "material_reversal": material_reversal,
            "material_reversal_override": material_reversal_override,
            "entry_cost": entry_cost,
            "fee_adjusted_exit_value": exit_value,
            "highest_fee_adjusted_exit_value": peak,
            "minimum_locked_profit": policy.minimum_locked_profit,
            "protected_floor_value": floor_value,
            "floor_satisfied": floor_satisfied,
            "hard_stops_unchanged": True,
        }

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
        profit_guard: Mapping[str, Any] | None = None,
        policy: TradingPolicy | None = None,
        reversal_confirmed: bool = False,
    ) -> str | None:
        policy = policy or self._policy
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
            and not bool((profit_guard or {}).get("blocked"))
            and not bool(
                (profit_guard or {}).get("material_reversal_override")
            )
        ):
            return "profit_lock_after_edge_decay" if edge_invalid else "trailing_profit_lock"
        # The reversal exit sells only after the configured number of
        # consecutive negative-edge readings; a single adverse quote is
        # observation, not evidence the thesis died.
        if reversal_confirmed:
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
        policy: TradingPolicy | None = None,
    ) -> bool:
        policy = policy or self._position_execution_policy(position)
        quantity = float(position["quantity"])
        profit_exit = reason in {
            "trailing_profit_lock",
            "profit_lock_after_edge_decay",
        }
        protected_floor_value = float(position["entry_cost"]) * (
            1.0 + policy.minimum_locked_profit
        )
        if profit_exit and exit_value + 1e-12 < protected_floor_value:
            self._journal(
                "exit",
                "profit_floor_blocked",
                event_id=position["event_id"],
                event_name=position["event_name"],
                market_slug=position["market_slug"],
                selection=position["selection"],
                payload={
                    "position_id": position["id"],
                    "reason": reason,
                    "fee_adjusted_exit_value": exit_value,
                    "protected_floor_value": protected_floor_value,
                    "minimum_locked_profit": (
                        policy.minimum_locked_profit
                    ),
                    "hard_stops_unchanged": True,
                },
            )
            return False
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
                    policy.post_exit_tracking_minutes * 60
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
        if profit_exit:
            # Preserve the floor across the preview/create network gap too.
            # Iterate because the taker fee is rounded per complete order and
            # depends on the contract price.
            minimum_raw_exit = protected_floor_value
            for _ in range(4):
                contract_price = (
                    minimum_raw_exit
                    if position["position_side"] == "long"
                    else 1.0 - minimum_raw_exit
                )
                fee_per_share = _estimated_taker_fee(
                    quantity=quantity,
                    contract_price=contract_price,
                ) / quantity
                minimum_raw_exit = min(
                    0.99,
                    protected_floor_value + fee_per_share,
                )
            floor_long_price = (
                minimum_raw_exit
                if position["position_side"] == "long"
                else 1.0 - minimum_raw_exit
            )
            order_long_price = (
                max(order_long_price, floor_long_price)
                if position["position_side"] == "long"
                else min(order_long_price, floor_long_price)
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
                        "protected_floor_value": (
                            protected_floor_value
                            if profit_exit else None
                        ),
                        "floor_protected_limit_price": (
                            order_long_price if profit_exit else None
                        ),
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
                policy.post_exit_tracking_minutes * 60
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
            {
                **asdict(candidate.execution_policy),
                "execution_profile_key": candidate.execution_profile_key,
            },
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
                        market_scope,selection,position_side,quantity,entry_cost,
                        entry_long_price,
                        cost_basis,opened_ts,updated_ts,highest_exit_value,
                        current_exit_value,current_model_probability,
                        current_execution_edge,return_fraction,entry_decision_id,
                        entry_order_id,initial_quantity,initial_cost_basis,
                        external_exit_quantity,venue_sync_status,
                        venue_mismatch_count,policy_session_id,entry_policy_json,
                        entry_signal_edge,entry_signal_quality,
                        entry_reference_sources,entry_execution_edge,
                        entry_game_fraction_remaining,entry_event_entries_60m,
                        entry_source_agreement,entry_signal_age_seconds,
                        entry_confirmation_count,
                        entry_confirmation_price_drift,
                        entry_spread,entry_book_shares)
                       VALUES (%s,%s,'open',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        position_id,
                        mode,
                        candidate.event.id,
                        candidate.event.name,
                        candidate.market["slug"],
                        _market_kind(candidate.signal.market),
                        market_scope(candidate.signal.market),
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
                        float(candidate.signal.quality_agreement),
                        _signal_age_seconds(candidate.signal, now),
                        int(candidate.entry_confirmation_count),
                        float(candidate.entry_confirmation_price_drift),
                        # Retained so spread and depth stop being frequency-only
                        # evidence: the advisor cannot score a filter against
                        # realized P/L unless the entry value is on the position.
                        float(candidate.spread),
                        float(candidate.book_shares),
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

    @staticmethod
    def _position_profile_key(position: Mapping[str, Any]) -> str:
        """Return the line/stage profile frozen into a position at entry."""
        payload = position.get("entry_policy")
        if isinstance(payload, Mapping):
            key = payload.get("execution_profile_key")
            if key:
                return str(key)
        return "global"

    def _profile_entries_last_hour(self, profile_key: str, mode: str) -> int:
        """Count fills attributed to one line/stage profile in the last hour.

        The profile key is read from each position's frozen entry policy rather
        than recomputed, so a later policy edit cannot retroactively move a
        past fill into a different profile's budget.
        """
        window_start = self._clock() - 3600
        return sum(
            1
            for row in self.positions(include_hidden=True)
            if str(row.get("mode") or "") == mode
            and float(row.get("opened_ts") or 0.0) > window_start
            and self._position_profile_key(row) == profile_key
        )

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
        trough: float,
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
        reversal_triggered_ts: float | None = None,
        reversal_observation_count: int = 0,
        profit_floor_missed_ts: float | None = None,
        profit_floor_missed_count: int = 0,
        profit_floor_low_exit_value: float | None = None,
        profit_guard_payload: str | None = None,
    ) -> None:
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """UPDATE live_managed_positions SET updated_ts=%s,
                       highest_exit_value=%s,lowest_exit_value=%s,
                       current_exit_value=%s,
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
                       stop_guard_payload=%s,
                       reversal_triggered_ts=%s,
                       reversal_observation_count=%s,
                       profit_floor_missed_ts=%s,
                       profit_floor_missed_count=%s,
                       profit_floor_low_exit_value=%s,
                       profit_guard_payload=%s
                       WHERE id=%s AND status='open'""",
                    (
                        self._clock(), peak, trough, exit_value,
                        probability, edge,
                        return_fraction, profit_lock_armed_ts,
                        profit_lock_price, profit_target_observed_ts,
                        profit_target_observation_count,
                        profit_target_observed_price,
                        adaptive_exit_payload,
                        stop_triggered_ts,
                        stop_observation_count,
                        stop_low_exit_value,
                        stop_guard_payload,
                        reversal_triggered_ts,
                        reversal_observation_count,
                        profit_floor_missed_ts,
                        profit_floor_missed_count,
                        profit_floor_low_exit_value,
                        profit_guard_payload,
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
                "source_agreement": candidate.signal.quality_agreement,
                "signal_age_seconds": _signal_age_seconds(
                    candidate.signal,
                    self._clock(),
                ),
                "reference_sources": candidate.signal.n_reference_sources,
                "entry_confirmation_readings": (
                    candidate.entry_confirmation_count
                ),
                "confirmation_price_drift": (
                    candidate.entry_confirmation_price_drift
                ),
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

    def _prune_journal(
        self,
        maximum: int = 10_000,
        *,
        protected_ceiling: int = 100_000,
    ) -> None:
        """Keep the decision ticker useful without allowing unbounded local data.

        Order-audit kinds are exempt from the ordinary cap, so the table can
        exceed `maximum` once they dominate. `protected_ceiling` is the backstop
        that keeps even those bounded: at roughly two rows per managed trade it
        allows tens of thousands of trades before the oldest audit rows age out.
        """
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(cur, "SELECT COUNT(*) FROM live_trading_journal")
                count = int(cur.fetchone()[0] or 0)
            if count > protected_ceiling:
                with self._db.transaction() as cur:
                    self._db.execute(
                        cur,
                        """SELECT id FROM live_trading_journal
                           ORDER BY created_ts ASC LIMIT %s""",
                        (count - protected_ceiling,),
                    )
                    for row in list(cur.fetchall()):
                        self._db.execute(
                            cur,
                            "DELETE FROM live_trading_journal WHERE id=%s",
                            (row[0],),
                        )
                count = protected_ceiling
            excess = count - maximum
            if excess <= 0:
                return
            with self._db.transaction() as cur:
                # Entry, exit, and settlement rows are the audit trail for real
                # orders and the fallback opportunity history the advisor reads
                # for trades that predate the candidate log. Evict rejection
                # chatter first: mark rows carry the per-cycle exit-decision
                # evidence (edge paths, guard payloads) that post-session
                # replays depend on, and losing them to qualification noise
                # cost two thirds of the 2026-08-02 reversal analysis.
                stale: list[str] = []
                self._db.execute(
                    cur,
                    """SELECT id FROM live_trading_journal
                       WHERE kind='qualification'
                       ORDER BY created_ts ASC LIMIT %s""",
                    (excess,),
                )
                stale.extend(row[0] for row in cur.fetchall())
                remaining = excess - len(stale)
                if remaining > 0:
                    self._db.execute(
                        cur,
                        """SELECT id FROM live_trading_journal
                           WHERE kind NOT IN (
                               'performance_reset','risk_session_reset',
                               'entry','exit','settlement','safety',
                               'qualification'
                           )
                           ORDER BY created_ts ASC LIMIT %s""",
                        (remaining,),
                    )
                    stale.extend(row[0] for row in cur.fetchall())
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
