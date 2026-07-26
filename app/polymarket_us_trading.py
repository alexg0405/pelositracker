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
import json
import math
import re
import threading
import time
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from .database import Database
from .entry_policy import MAX_ENTRY_PRICE, MIN_ENTRY_PRICE
from .lines import is_spread_market, is_total_market, quote_line_side
from .models import Event, Signal


POLICY_VERSION = "pmus-live-risk-policy-v1"
ARM_PHRASE = "ARM LIVE TRADING"
LIVE_LIQUIDATION_PHRASE = "SELL ALL LIVE POSITIONS"
DRY_RUN_HISTORY_CLEAR_PHRASE = "CLEAR DRY RUN HISTORY"
MAX_ARM_SECONDS = 30 * 60
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


class TradingPolicyError(ValueError):
    """A user-correctable execution-policy error."""


class TradingExecutionError(RuntimeError):
    """A bounded venue or execution failure."""


@dataclass(frozen=True, slots=True)
class TradingPolicy:
    automation_enabled: bool = False
    execution_mode: str = "dry_run"
    auto_cashout: bool = False
    require_engine_entry: bool = True
    max_total_exposure_usd: float = 9.50
    minimum_cash_reserve_usd: float = 0.50
    max_position_usd: float = 1.75
    max_event_exposure_usd: float = 3.0
    max_daily_loss_usd: float = 5.0
    max_open_positions: int = 6
    max_orders_per_hour: int = 6
    min_edge: float = 0.03
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

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "TradingPolicy":
        known = {field.name for field in fields(cls)}
        clean = {key: value for key, value in values.items() if key in known}
        policy = cls(**clean)
        policy.validate()
        return policy

    def validate(self) -> None:
        if self.execution_mode not in {"dry_run", "live"}:
            raise TradingPolicyError("execution_mode must be dry_run or live")
        money_fields = (
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
        if self.max_total_exposure_usd <= 0:
            raise TradingPolicyError("max_total_exposure_usd must be greater than zero")
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
        if self.min_reference_sources < 1:
            raise TradingPolicyError("min_reference_sources must be at least one")
        if not MIN_ENTRY_PRICE < self.min_entry_price < self.max_entry_price < MAX_ENTRY_PRICE:
            raise TradingPolicyError(
                "entry prices must stay strictly inside the established 5c–95c bounds"
            )
        if not 0 <= self.min_edge < 1:
            raise TradingPolicyError("min_edge must be between zero and one")
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


def _price_amount(value: float) -> dict[str, Any]:
    return {"value": round(value, 8), "currency": "USD"}


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
        path: str,
        *,
        key_id: str,
        secret_key: str,
        client_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = _now,
    ):
        self._db = Database.open(
            path,
            sqlite_envs=("POLYMARKET_US_TRADING_DB",),
            sqlite_default=path,
        )
        self.path = self._db.target
        self._lock = threading.RLock()
        self._cycle_lock = threading.Lock()
        self._key_id = key_id
        self._secret_key = secret_key
        self._client_factory = client_factory
        self._clock = clock
        self._armed_until = 0.0
        self._last_cycle_at: float | None = None
        self._last_cycle_summary = "Automation has not run yet."
        self._last_cycle_evaluations: tuple[dict[str, Any], ...] = ()
        self._candidate_seen: dict[str, float] = {}
        self._qualification_seen: dict[str, float] = {}
        self._journal_writes = 0
        with self._lock:
            self._db.initialize(_SCHEMA, component="polymarket_us_live_trading", version=1)
        self._policy = self._load_policy()

    def close(self) -> None:
        with self._lock:
            self._armed_until = 0.0
            self._db.close()

    @property
    def policy(self) -> TradingPolicy:
        return self._policy

    def _load_policy(self) -> TradingPolicy:
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    "SELECT payload FROM live_trading_config WHERE singleton=%s",
                    (1,),
                )
                row = cur.fetchone()
        if row is None:
            policy = TradingPolicy()
            self._save_policy(policy)
            return policy
        try:
            return TradingPolicy.from_mapping(json.loads(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TradingPolicyError("stored trading policy is invalid") from exc

    def _save_policy(self, policy: TradingPolicy) -> None:
        payload = json.dumps(asdict(policy), sort_keys=True, separators=(",", ":"))
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO live_trading_config(singleton,payload,updated_ts)
                       VALUES (%s,%s,%s) ON CONFLICT(singleton) DO UPDATE SET
                       payload=EXCLUDED.payload,updated_ts=EXCLUDED.updated_ts""",
                    (1, payload, self._clock()),
                )

    def configure(self, values: Mapping[str, Any]) -> TradingPolicy:
        current = asdict(self._policy)
        current.update(values)
        policy = TradingPolicy.from_mapping(current)
        self._save_policy(policy)
        self._policy = policy
        # Any limit or mode edit closes the latch. The operator must review the
        # saved policy and explicitly re-arm it.
        self._armed_until = 0.0
        self._journal(
            "configuration",
            "saved",
            payload={"policy": asdict(policy), "live_disarmed": True},
        )
        return policy

    def is_armed(self) -> bool:
        return self._clock() < self._armed_until

    def arm(self, confirmation: str, *, seconds: int = MAX_ARM_SECONDS) -> dict[str, Any]:
        if confirmation != ARM_PHRASE:
            raise TradingPolicyError(f'type "{ARM_PHRASE}" exactly to arm live execution')
        if not self._policy.automation_enabled:
            raise TradingPolicyError("enable automation before arming live execution")
        if self._policy.execution_mode != "live":
            raise TradingPolicyError("set execution mode to live before arming")
        if not self._key_id or not self._secret_key:
            raise TradingPolicyError("both Polymarket US API credential values are required")
        seconds = max(60, min(int(seconds), MAX_ARM_SECONDS))
        self._armed_until = self._clock() + seconds
        self._journal(
            "safety",
            "live_armed",
            payload={"expires_at": self._armed_until, "seconds": seconds},
        )
        return self.status()

    def disarm(self, reason: str = "manual") -> dict[str, Any]:
        self._armed_until = 0.0
        self._journal("safety", "disarmed", payload={"reason": reason})
        return self.status()

    def emergency_stop(self) -> dict[str, Any]:
        self._armed_until = 0.0
        self._policy = TradingPolicy.from_mapping({
            **asdict(self._policy),
            "automation_enabled": False,
        })
        self._save_policy(self._policy)
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
            payload={"cancel_requested": canceled, "failures": failures},
        )
        return {
            **self.status(),
            "cancel_requested": canceled,
            "cancel_failures": failures,
        }

    def status(self) -> dict[str, Any]:
        positions = self.positions(open_only=True)
        exposure = sum(float(row["cost_basis"]) for row in positions)
        now = self._clock()
        return {
            "policy_version": POLICY_VERSION,
            "policy": asdict(self._policy),
            "credentials_configured": bool(self._key_id and self._secret_key),
            "armed": self.is_armed(),
            "armed_until": (
                datetime.fromtimestamp(self._armed_until, timezone.utc).isoformat()
                if self.is_armed()
                else None
            ),
            "restart_behavior": "always_disarmed",
            "open_managed_positions": len(positions),
            "managed_exposure_usd": round(exposure, 2),
            "last_cycle_at": (
                datetime.fromtimestamp(self._last_cycle_at, timezone.utc).isoformat()
                if self._last_cycle_at
                else None
            ),
            "last_cycle_summary": self._last_cycle_summary,
            # This is execution-layer metadata only. It lets the workstation
            # distinguish a research signal from an exactly mapped US contract
            # without writing anything back into the calculation engine.
            "last_cycle_evaluations": [
                dict(item) for item in self._last_cycle_evaluations
            ],
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

    def positions(self, *, open_only: bool = False) -> list[dict[str, Any]]:
        where = " WHERE status='open'" if open_only else ""
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    "SELECT * FROM live_managed_positions" + where
                    + " ORDER BY opened_ts DESC",
                )
                rows = [dict(row) for row in cur.fetchall()]
        for row in rows:
            for key in ("opened_ts", "updated_ts", "closed_ts"):
                value = row.get(key)
                row[key.removesuffix("_ts") + "_at"] = (
                    datetime.fromtimestamp(float(value), timezone.utc).isoformat()
                    if value is not None
                    else None
                )
        return rows

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
        }
        modes = {
            "dry_run": {**empty, "mode": "dry_run", "label": "Dry run"},
            "live": {**empty, "mode": "live", "label": "Live"},
        }
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
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
                       WHERE mode IN ('dry_run','live')
                       GROUP BY mode""",
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
            },
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
        if not self._cycle_lock.acquire(blocking=False):
            raise TradingPolicyError(
                "a trading analysis or liquidation cycle is already running"
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
                        "live trading is disarmed; arm the 30-minute live-order latch first"
                    )
                if confirmation != LIVE_LIQUIDATION_PHRASE:
                    raise TradingPolicyError(
                        f'type "{LIVE_LIQUIDATION_PHRASE}" exactly to sell all live '
                        "positions"
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
                market = markets.get(str(position["market_slug"]))
                failure = ""
                exit_value: float | None = None
                order_long_price: float | None = None
                if market is None:
                    failure = "current Polymarket US market snapshot is unavailable"
                elif (
                    not market.get("active", True)
                    or market.get("closed")
                    or market.get("hidden")
                    or str(market.get("state") or "OPEN").upper()
                    not in {"OPEN", "MARKET_STATE_OPEN", "EP3_STATUS_OPEN"}
                ):
                    failure = "the Polymarket US market is not open for an exit"
                else:
                    sides = [
                        side
                        for side in market.get("sides", [])
                        if isinstance(side, Mapping)
                        and bool(side.get("long"))
                        == (position["position_side"] == "long")
                    ]
                    if len(sides) != 1:
                        failure = "the managed position no longer maps to one exact side"
                    else:
                        prices = _book_prices(market, sides[0])
                        if prices is None or prices[2] is None:
                            failure = "a complete executable cash-out quote is unavailable"
                        else:
                            exit_value = prices[2]
                            order_long_price = _amount(
                                market.get(
                                    "long_best_bid"
                                    if position["position_side"] == "long"
                                    else "long_best_ask"
                                )
                            )
                            if order_long_price is None:
                                failure = "the executable sell limit is unavailable"

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

    def clear_dry_run_history(self, confirmation: str) -> dict[str, Any]:
        """Force-clear every simulated trade while preserving live and audit data."""
        if confirmation != DRY_RUN_HISTORY_CLEAR_PHRASE:
            raise TradingPolicyError(
                f'type "{DRY_RUN_HISTORY_CLEAR_PHRASE}" exactly to clear dry-run '
                "trade history"
            )
        stopped_policy = TradingPolicy.from_mapping({
            **asdict(self._policy),
            "automation_enabled": False,
        })
        policy_payload = json.dumps(
            asdict(stopped_policy),
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            # This is an operator reset, not an execution attempt. Persisting the
            # stop and deleting the simulated ledger in one transaction means no
            # quote, mapping, fill, or cycle-lock state can prevent the wipe.
            self._policy = stopped_policy
            self._armed_until = 0.0
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
    ) -> dict[str, Any]:
        if not self._cycle_lock.acquire(blocking=False):
            self._last_cycle_summary = (
                "A trading analysis cycle is already running; this duplicate was skipped."
            )
            return {"status": "busy", "evaluated": 0, "qualified": 0}
        try:
            return self._run_cycle(monitored, us_payload)
        finally:
            self._cycle_lock.release()

    def _run_cycle(
        self,
        monitored: Iterable[tuple[Event, Iterable[Signal]]],
        us_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = self._clock()
        self._last_cycle_at = now
        if not self._policy.automation_enabled:
            self._last_cycle_evaluations = ()
            self._last_cycle_summary = "Automation is off; no candidates were evaluated."
            return {"status": "off", "evaluated": 0, "qualified": 0}
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
                candidate, reason = self._map_signal(event, signal, us_event, event_score)
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

        marked, exited = self._mark_and_exit(monitored_list, us_events)
        placed = 0
        venue_checks = 0
        for index, (candidate, evaluation) in enumerate(mapped):
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
            entered, state, reason, audit = self._attempt_entry(candidate)
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
            f"mapped selections; {placed} entry, {marked} marks, {exited} exits."
        )
        return {
            "status": "completed",
            "events": len(monitored_list),
            "evaluated": len(mapped) + rejected,
            "qualified": len(mapped),
            "entries": placed,
            "marks": marked,
            "exits": exited,
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
            "required_edge": signal.required_edge,
            "configured_min_quality": self._policy.min_signal_quality,
            "reference_sources": signal.n_reference_sources,
            "configured_min_reference_sources": (
                self._policy.min_reference_sources
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
                "signal_quality": signal.confidence if signal else None,
                "configured_min_quality": self._policy.min_signal_quality,
                "reference_sources": signal.n_reference_sources if signal else None,
                "configured_min_reference_sources": (
                    self._policy.min_reference_sources
                ),
                "execution_mode": self._policy.execution_mode,
                "require_engine_entry": self._policy.require_engine_entry,
                "engine_action": signal.action if signal else None,
            },
        )

    def _map_signal(
        self,
        event: Event,
        signal: Signal,
        us_event: Mapping[str, Any],
        event_score: float,
    ) -> tuple[MappedCandidate | None, str]:
        policy = self._policy
        if signal.action != "PAPER_BET" and policy.require_engine_entry:
            return None, "existing engine action is not PAPER_BET"
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
        ), ""

    def _attempt_entry(
        self,
        candidate: MappedCandidate,
    ) -> tuple[bool, str, str | None, dict[str, Any]]:
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
        daily_loss = self._daily_realized_loss()
        reasons = []
        if len(open_positions) >= policy.max_open_positions:
            reasons.append("maximum open positions reached")
        if daily_loss >= policy.max_daily_loss_usd:
            reasons.append("daily realized-loss stop reached")
        if self._orders_last_hour() >= policy.max_orders_per_hour:
            reasons.append("hourly order limit reached")
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
                if candidate.book_shares < policy.min_book_shares:
                    reasons.append(
                        f"executable top-of-book depth "
                        f"{candidate.book_shares:.2f} shares is below the "
                        f"configured {policy.min_book_shares:.2f} at "
                        f"{candidate.entry_cost * 100:.1f}c"
                    )
        audit = {
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
            "required_edge": max(policy.min_edge, candidate.signal.required_edge),
            "configured_min_quality": policy.min_signal_quality,
            "configured_min_reference_sources": policy.min_reference_sources,
            "buying_power": balance,
            "available_capacity_usd": max(0.0, capacity),
        }
        if reasons:
            self._journal_candidate(
                candidate,
                "rejected",
                reasons=reasons,
                extra=audit,
            )
            return False, "blocked", " | ".join(reasons), audit

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
        if not self.is_armed():
            self._journal_candidate(
                candidate,
                "rejected",
                reasons=["live mode is disarmed or its 30-minute latch expired"],
                extra=audit,
            )
            return (
                False,
                "live_disarmed",
                "live mode is disarmed or its 30-minute latch expired",
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
    ) -> tuple[int, int]:
        monitored_by_id = {
            event.id: (event, list(signals)) for event, signals in monitored
        }
        markets = {
            str(market.get("slug")): dict(market)
            for event in us_events
            for market in event.get("markets", [])
            if isinstance(market, Mapping) and market.get("slug")
        }
        marked = 0
        exited = 0
        for position in self.positions(open_only=True):
            market = markets.get(position["market_slug"])
            if market is None:
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
            sides = [
                side for side in market.get("sides", [])
                if isinstance(side, Mapping)
                and bool(side.get("long")) == (position["position_side"] == "long")
            ]
            if len(sides) != 1:
                continue
            prices = _book_prices(market, sides[0])
            if prices is None:
                continue
            _, _, exit_value, _ = prices
            if exit_value is None:
                continue
            # The API expresses both LONG and SHORT order limits as a LONG/YES
            # price. A cash-out must cross the side that is executable now:
            # LONG sells at the LONG bid; SHORT sells at the LONG ask.
            exit_long_price = _amount(
                market.get(
                    "long_best_bid"
                    if position["position_side"] == "long"
                    else "long_best_ask"
                )
            )
            if exit_long_price is None:
                continue
            probability = self._position_probability(
                position, market, monitored_by_id
            )
            current_edge = probability - (
                prices[0]
            ) if probability is not None else None
            entry_cost = float(position["entry_cost"])
            return_fraction = exit_value / entry_cost - 1.0
            peak = max(float(position["highest_exit_value"]), exit_value)
            self._update_mark(
                position["id"],
                exit_value=exit_value,
                peak=peak,
                probability=probability,
                edge=current_edge,
                return_fraction=return_fraction,
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
                },
            )
            reason = self._exit_reason(position, exit_value, peak, current_edge)
            if reason and self._policy.auto_cashout:
                exited += int(
                    self._attempt_exit(
                        position,
                        market,
                        exit_value=exit_value,
                        order_long_price=exit_long_price,
                        reason=reason,
                    )
                )
        return marked, exited

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

    def _exit_reason(
        self,
        position: Mapping[str, Any],
        exit_value: float,
        peak: float,
        current_edge: float | None,
    ) -> str | None:
        policy = self._policy
        held = (self._clock() - float(position["opened_ts"])) / 60.0
        if held < policy.min_hold_minutes:
            return None
        entry = float(position["entry_cost"])
        return_fraction = exit_value / entry - 1.0
        edge_invalid = current_edge is not None and current_edge <= policy.exit_edge
        trailing = peak > 0 and exit_value <= peak * (1.0 - policy.trailing_drawdown)
        # A profit target alone does not trigger an immediate penny scalp.  The
        # model must have lost its entry edge or price must pull back materially
        # from a previously observed peak.
        if return_fraction >= policy.profit_target and (edge_invalid or trailing):
            return "profit_lock_after_edge_decay" if edge_invalid else "trailing_profit_lock"
        if return_fraction <= -policy.stop_loss and edge_invalid:
            return "stop_loss_with_model_invalidation"
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
    ) -> bool:
        quantity = float(position["quantity"])
        if position["mode"] == "dry_run":
            self._close_position(
                position["id"], exit_value=exit_value, reason=reason, order_id=None
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
            return True
        if not self.is_armed():
            self._journal(
                "exit",
                "blocked",
                event_id=position["event_id"],
                event_name=position["event_name"],
                market_slug=position["market_slug"],
                selection=position["selection"],
                payload={"reason": "live mode is disarmed", "exit_trigger": reason},
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
            "maxBlockTime": "5s",
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
            "maxBlockTime": "5s",
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
                        entry_order_id)
                       VALUES (%s,%s,'open',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s,%s,%s)""",
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

    def _update_mark(
        self,
        position_id: str,
        *,
        exit_value: float,
        peak: float,
        probability: float | None,
        edge: float | None,
        return_fraction: float,
    ) -> None:
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """UPDATE live_managed_positions SET updated_ts=%s,
                       highest_exit_value=%s,current_exit_value=%s,
                       current_model_probability=%s,current_execution_edge=%s,
                       return_fraction=%s WHERE id=%s AND status='open'""",
                    (
                        self._clock(), peak, exit_value, probability, edge,
                        return_fraction, position_id,
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

    def _daily_realized_loss(self) -> float:
        start = self._clock() - 86400
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    """SELECT COALESCE(SUM(CASE WHEN realized_pnl < 0
                              THEN -realized_pnl ELSE 0 END),0)
                       FROM live_managed_positions
                       WHERE status='closed' AND closed_ts >= %s""",
                    (start,),
                )
                row = cur.fetchone()
        return float(row[0] or 0.0)

    def _orders_last_hour(self) -> int:
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    """SELECT COUNT(*) FROM live_trading_journal
                       WHERE created_ts >= %s AND kind='entry'
                         AND status IN ('live_fill','unfilled','order_error')""",
                    (self._clock() - 3600,),
                )
                row = cur.fetchone()
        return int(row[0] or 0)

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
