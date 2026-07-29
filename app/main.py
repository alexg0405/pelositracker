from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import tempfile
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import secrets
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr
from starlette.background import BackgroundTask

from .engine import SignalEngine
from . import __version__, backtest, shadow_eval
from .accounts import AccountBook, DEFAULT_STRATEGIES, bot_entry_candidates, line_type
from .tennis_model import (
    execution_sigma,
    game_prob_from_prematch,
    implied_prematch_price as tennis_implied_prematch_price,
    match_win_probability_band,
    next_game_swing,
    parse_tennis_score,
)
from .lead_model import (
    LEAD_SPORT_PARAMS,
    implied_prematch_price as lead_implied_prematch_price,
    score_swing,
    win_probability_band,
)
from . import soccer_model
from .diagnostics import edge_health
from .advice import market_views, position_views
from .history import HistoryDB
from .ledger import Ledger
from .lines import pregame_priors
from .gameclock import game_progress, league_rule, validate_state_transition
from .models import Event, GameState, Quote, as_json
from .monitor_state import MonitorState
from .sources import (_odds_quota, exclude_restricted_games, extract_polymarket_slug,
                      game_start_matches_slug, infer_polymarket_event, odds_api_poll,
                      polymarket_event,
                      polymarket_market_stream, polymarket_sports_events,
                      polymarket_sports_stream, sports_game_status)
from .actionnetwork import action_network_poll
from .pinnacle import pinnacle_poll
from .store import Store
from .settings import Settings
from .security import AuthManager, SlidingWindowLimiter
from .calibration import load_calibration
from .model_registry import load_independent_models
from .telemetry import memory_snapshot, runtime_telemetry, start_memory_trace
from .identity import (CanonicalEvent, MappingDecision, MappingStatus)
from .domain.time import parse_provider_timestamp
from .http_clients import close_shared_client, open_shared_client
from .mlb_live import is_mlb_event, mlb_linescore_poll
from .notify import notify_webhook
from .polymarket_us_research import (
    AUTHENTICATED_BASE_URL as POLYMARKET_US_AUTHENTICATED_BASE_URL,
    PUBLIC_BASE_URL as POLYMARKET_US_PUBLIC_BASE_URL,
    PolymarketUSResearchError,
    credential_status as polymarket_us_credential_status,
    fetch_account_snapshot as fetch_polymarket_us_account,
    fetch_public_sports_events as fetch_polymarket_us_events,
)
from .polymarket_us_trading import (
    MAX_ARM_SECONDS,
    PolymarketUSAutoTrader,
    TradingPolicyError,
)
from .approval import approval_granted, approval_instruction
from .sport_model_lab import SportModelLab, _baseball_live_state
from .research_bundle import (
    ResearchBundleError,
    merge_research_bundle,
    write_research_bundle,
)


logger = logging.getLogger(__name__)

load_dotenv()
settings = Settings.from_env()
store = Store()
ledger: Ledger | None = None
account_book: AccountBook | None = None
history_db: HistoryDB | None = None
monitor_state: MonitorState | None = None
live_trader: PolymarketUSAutoTrader | None = None
dry_run_trader: PolymarketUSAutoTrader | None = None
model_lab: SportModelLab | None = None
engine = SignalEngine(settings.confidence_threshold,
                      settings.edge_threshold,
                      settings.max_data_age_seconds,
                      kelly_fraction=settings.kelly_fraction,
                      enable_independent_model=settings.enable_independent_models)
calibration_artifact = load_calibration(settings.calibration_artifact)
if calibration_artifact is not None:
    engine.install_calibration(calibration_artifact)
independent_model_artifact = load_independent_models(settings.independent_model_artifact)
if independent_model_artifact is not None:
    engine.install_independent_models(independent_model_artifact)
tasks: dict[str, list[asyncio.Task]] = {}
_event_deletions: dict[str, asyncio.Task] = {}
_finalized: set[str] = set()
# Finalized event ids still resident in the live store, oldest first. `_finalized`
# is the permanent idempotency guard (ids only, negligible); this is the subset
# whose heavy buffers we keep for dashboard review until they age past the
# retention cap. See _evict_finalized_overflow.
_finalized_order: "OrderedDict[str, None]" = OrderedDict()
_terminal_events: dict[str, str] = {}  # event_id -> final | canceled | deleted | shutdown
_event_locks: dict[str, asyncio.Lock] = {}
_pregame: dict[str, dict] = {}  # event_id -> {"spread": home point, "total": line}, captured near tip
_subscribers: set[asyncio.Queue] = set()  # SSE clients for real-time dashboard pushes
_notification_tasks: set[asyncio.Task] = set()
# The SSE snapshot is identical for every subscriber, so it is built at most
# once per change instead of once per subscriber. _notify_subscribers bumps the
# version; the first coroutine to observe a new version rebuilds under the lock
# and caches the payload, the rest reuse it. Without this, N open dashboards
# forced N full recomputes + JSON serializations of the whole event list on
# every push.
_snapshot_version = 0
_snapshot_cache: dict = {"version": -1, "payload": b""}
_snapshot_lock = asyncio.Lock()
# The global sports feed emits a payload for *every* slug it sees, not just the
# ones we track, so caching the full payload per slug forever is an unbounded
# leak. Split by access pattern instead: discovery only needs a normalized
# status + freshness (compact, every slug, TTL/LRU-bounded); the in-play models
# need the score-rich payload but only for tracked events (detail, pruned with
# its compact entry). See on_sports_status / _prune_sports_status.
_sports_status_compact: "OrderedDict[str, dict]" = OrderedDict()
_sports_status_detail: dict[str, dict] = {}
_SPORTS_STATUS_TTL_SECONDS = 600.0  # drop slugs untouched for 10 min
_SPORTS_STATUS_MAX = 512  # hard cap on compact entries (LRU eviction)
_config_state = {
    "auto_monitor": False,
    "odds_api_enabled": True,
    "odds_api_poll_seconds": settings.odds_poll_seconds,
}

_auth_users_env = os.getenv("AUTHORIZED_USERS")
if _auth_users_env:
    AUTHORIZED_USERS = {}
    for pair in _auth_users_env.split(","):
        if ":" in pair:
            u, p = pair.split(":", 1)
            AUTHORIZED_USERS[u.strip()] = p.strip()
else:
    AUTHORIZED_USERS = {
        os.getenv("ADMIN_USERNAME", "admin"): os.getenv("ADMIN_PASSWORD", "admin")
    }

auth_manager = AuthManager.from_plaintext(AUTHORIZED_USERS)
login_limiter = SlidingWindowLimiter(10, 5 * 60)
api_limiter = SlidingWindowLimiter(300, 60)


def _cookie_name(base: str) -> str:
    return f"__Host-{base}" if settings.environment in {"production", "prod"} else base

async def verify_auth(request: Request):
    session = auth_manager.verify(request.cookies.get(_cookie_name("session_token")))
    if session is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        header = request.headers.get("x-csrf-token", "")
        cookie = request.cookies.get(_cookie_name("csrf_token"), "")
        if (not header or not cookie or not secrets.compare_digest(header, cookie)
                or not secrets.compare_digest(header, session.csrf_token)):
            raise HTTPException(status_code=403, detail="CSRF validation failed")
    request.state.session = session


async def verify_execution_admin(request: Request):
    """Restrict account credentials and evidence imports to the site operator."""
    await verify_auth(request)
    username = request.state.session.username
    sole_authorized_user = (
        len(AUTHORIZED_USERS) == 1 and username in AUTHORIZED_USERS
    )
    if username != settings.admin_username and not sole_authorized_user:
        raise HTTPException(status_code=403, detail="Operator access required")

def _notify_subscribers() -> None:
    """Wake every SSE client that a snapshot changed (coalesced per client)."""
    global _snapshot_version
    _snapshot_version += 1
    # The old all-events payload can be one of the process's largest single
    # objects. Once its version is obsolete it can never be served again, so
    # release the cache reference before a subscriber starts constructing the
    # replacement. Keeping old + Python view tree + replacement + SSE framing
    # alive together caused sharp transient RAM spikes on changes such as an
    # event removal.
    _snapshot_cache["version"] = -1
    _snapshot_cache["payload"] = b""
    for queue in list(_subscribers):
        if queue.empty():
            try:
                queue.put_nowait(1)
            except asyncio.QueueFull:
                pass


def _schedule_notification(payload: dict) -> None:
    task = asyncio.create_task(notify_webhook(payload["webhook_url"], payload))
    _notification_tasks.add(task)
    task.add_done_callback(_notification_tasks.discard)
_FINAL_STATUSES = {"final", "ended", "closed", "complete", "completed", "finished"}
_CANCELED_STATUSES = {"canceled", "cancelled", "abandoned", "void", "voided"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _terminal_kind(status: object) -> str | None:
    normalized = re.sub(r"[_-]+", " ", str(status or "").strip().casefold())
    if normalized in _CANCELED_STATUSES:
        return "canceled"
    if normalized in _FINAL_STATUSES:
        return "final"
    return None


def _event_lock(event_id: str) -> asyncio.Lock:
    return _event_locks.setdefault(event_id, asyncio.Lock())


async def _cancel_tasks(group: list[asyncio.Task]) -> None:
    current = asyncio.current_task()
    pending = [task for task in group if task is not current and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _require_safe_id(value: str | None, field: str) -> None:
    # These are interpolated into outbound API paths; reject path/query injection.
    if value is not None and not _SAFE_ID.match(value):
        raise HTTPException(400, f"invalid {field}")


async def on_state(state: GameState):
    event = store.events.get(state.event_id)
    if event is None or _terminal_events.get(state.event_id) == "deleted":
        return
    terminal = _terminal_kind(state.status)
    if terminal is not None:
        # Close the entry gate synchronously, before history I/O yields control
        # to quote callbacks that might otherwise place a known-result bet.
        _terminal_events.setdefault(state.event_id, terminal)
    previous_states = store.states.get(state.event_id)
    previous = previous_states[-1] if previous_states else None
    if terminal is None and event is not None and not state.quarantined:
        validation = validate_state_transition(
            sport=event.sport, league=event.league, period=state.period, clock=state.clock,
            home_score=state.home_score, away_score=state.away_score,
            previous_period=previous.period if previous else None,
            previous_clock=previous.clock if previous else None,
            previous_home_score=previous.home_score if previous else None,
            previous_away_score=previous.away_score if previous else None,
        )
        if not validation.valid:
            state.quarantined = True
            state.quarantine_reason = validation.reason
    store.add_state(state)
    if history_db is not None:
        try:
            await asyncio.to_thread(history_db.log_state, state)
        except Exception as exc:
            logger.warning("Could not persist state telemetry for %s: %s", state.event_id, exc)
    if terminal is not None:
        await finalize_event(state.event_id, canceled=terminal == "canceled")
    else:
        await record(state.event_id, as_of=state.processed_at)


def _tracked_slugs() -> set[str]:
    return {event.polymarket_slug for event in store.events.values()
            if event.polymarket_slug}


def _prune_sports_status(now_epoch: float) -> None:
    """Bound the sports-status caches by age then size, dropping each slug's
    detail entry alongside its compact one so the two never diverge."""
    stale = [slug for slug, snap in _sports_status_compact.items()
             if now_epoch - snap.get("_received_epoch", 0.0) > _SPORTS_STATUS_TTL_SECONDS]
    for slug in stale:
        _sports_status_compact.pop(slug, None)
        _sports_status_detail.pop(slug, None)
    while len(_sports_status_compact) > _SPORTS_STATUS_MAX:
        old_slug, _ = _sports_status_compact.popitem(last=False)  # oldest first
        _sports_status_detail.pop(old_slug, None)


async def on_sports_status(slug: str, payload: dict) -> None:
    """Cache authoritative live/final state for discovery without paid polling."""
    now_epoch = time.time()
    received_at = datetime.now(timezone.utc).isoformat()
    raw_status = next((payload.get(key) for key in
                       ("status", "gameStatus", "game_status", "state")
                       if payload.get(key)), None)
    # Compact snapshot for every slug: only what discovery's _game_window reads
    # (normalized status via sports_game_status, the live flag, and freshness).
    compact = {"status": raw_status, "live": payload.get("live"),
               "_received_at": received_at, "_received_epoch": now_epoch}
    _sports_status_compact[slug] = compact
    _sports_status_compact.move_to_end(slug)
    if slug in _tracked_slugs():
        # Only a tracked event's rich payload is ever read (by the in-play models).
        detail = dict(payload)
        detail["_received_at"] = received_at
        _sports_status_detail[slug] = detail
    else:
        _sports_status_detail.pop(slug, None)
    _prune_sports_status(now_epoch)
    terminal = _terminal_kind(raw_status)
    matched = next((event for event in store.events.values()
                    if event.polymarket_slug == slug), None)
    if terminal is not None and matched is not None:
        # The status callback runs before score parsing in the shared sports
        # stream. Close entry immediately even if a malformed final score keeps
        # the subsequent GameState callback from being emitted.
        _terminal_events.setdefault(matched.id, terminal)
        if terminal == "canceled":
            await finalize_event(matched.id, canceled=True)
    normalized = sports_game_status(compact)
    if not _discover_cache.get("data") or normalized is None:
        return
    if normalized == "final":
        _discover_cache["data"] = [game for game in _discover_cache["data"]
                                   if game.get("slug") != slug]
        return
    for game in _discover_cache["data"]:
        if game.get("slug") == slug:
            game["status"] = normalized
            game["status_source"] = "polymarket-live-feed"


def _paper_tradeable_quotes(quotes: list[Quote], ignore_restriction: bool) -> list[Quote]:
    """Flag region-restricted quotes as paper-waived WITHOUT mutating the raw
    ``restricted`` provider fact, so the stored and hashed observation—and its
    history row—keep the original value. The waiver is derived metadata consumed
    only by simulated execution (``Quote.paper_restricted``). The flag governs
    *real* order placement; a simulated fill is not a real order, so from a
    restricted host region (where Polymarket marks every event restricted) this is
    what lets any market trade on paper at all. No effect when disabled."""
    if ignore_restriction:
        for quote in quotes:
            if quote.restricted:
                quote.paper_restriction_waived = True
    return quotes


async def on_quotes(quotes: list[Quote]):
    event = store.events.get(quotes[0].event_id) if quotes else None
    if event is None or event.id in _terminal_events:
        return
    store.add_quotes(_paper_tradeable_quotes(quotes, settings.paper_ignore_region_restriction))
    if (event is not None and event.id not in _terminal_events
            and monitor_state is not None and event.odds_api_event_id):
        # Background matching may resolve this ID after registration.
        await asyncio.to_thread(monitor_state.save_event, event)
    if history_db is not None and quotes:
        try:
            await asyncio.to_thread(history_db.log_quotes, quotes)
            odds_provider_ids = {
                q.provider_event_id for q in quotes
                if q.provider_event_id and not q.condition_id
                and q.provider_event_id == event.odds_api_event_id
            } if event is not None else set()
            if event is not None and event.canonical_event_id and odds_provider_ids:
                try:
                    start = parse_provider_timestamp(event.game_start)
                except (TypeError, ValueError, OverflowError):
                    start = None
                canonical = CanonicalEvent.create(
                    event.sport, event.league, start, event.home, event.away
                )
                for provider_id in odds_provider_ids:
                    await asyncio.to_thread(
                        history_db.log_event_identity, canonical,
                        MappingDecision(
                            "the-odds-api", provider_id, event.canonical_event_id,
                            MappingStatus.MAPPED, 1.0,
                            "shared matcher verified participants and start window",
                            orientation="direct",
                        ),
                    )
        except Exception as exc:
            logger.warning("Could not persist quote telemetry for %s: %s", quotes[0].event_id, exc)
    if quotes:
        await record(quotes[0].event_id, as_of=max(quote.processed_at for quote in quotes))


async def on_polymarket_snapshot(event: Event, quotes: list[Quote]) -> None:
    """Replace the live Polymarket universe so removed/archived lines disappear."""
    if event.id not in store.events or event.id in _terminal_events:
        return
    removed = store.remove_source_quotes(event.id, "Polymarket")
    if quotes:
        await on_quotes(quotes)
    elif removed:
        await record(event.id)


def _is_tennis(event: Event) -> bool:
    return (event.sport or "").strip().casefold() == "tennis"


def _is_soccer(event: Event) -> bool:
    return (event.sport or "").strip().casefold() == "soccer"


def _moneyline_side(outcome: str, event: Event) -> str | None:
    """Map a moneyline outcome label to home/away for any two-sided match.

    Handles Polymarket's binary shape where one side is the team/player name and
    the other is the negated condition (``"Not <name>"``)."""
    label = (outcome or "").strip().casefold()
    home = (event.home or "").strip().casefold()
    away = (event.away or "").strip().casefold()
    if label in ("home", home):
        return "home"
    if label in ("away", away):
        return "away"
    if label.startswith("not "):
        remainder = label[4:].strip()
        if remainder in ("home", home):
            return "away"
        if remainder in ("away", away):
            return "home"
    return None


def _soccer_side(outcome: str, event: Event) -> str | None:
    """Map a soccer 1X2 outcome label to home/draw/away, or None.

    Polymarket represents 1X2 as three independent binary conditions, so the
    positive side of each is one of the three results."""
    label = (outcome or "").strip().casefold()
    if label in ("draw", "tie", "x"):
        return "draw"
    return _moneyline_side(outcome, event)


def _is_tennis_moneyline(market: str) -> bool:
    """True for either side of a tennis win market, including Polymarket's
    ``"moneyline condition"`` label on the negated binary outcome."""
    return line_type(market) == "moneyline" or (
        market or "").strip().casefold() == "moneyline condition"


def _tennis_score_now(event: Event) -> tuple[int, int, int, int] | None:
    """Parse the latest live tennis score cached from the sports feed."""
    status = _sports_status_detail.get(event.polymarket_slug or "")
    if not status:
        return None
    event_state = status.get("eventState")
    source = event_state if isinstance(event_state, dict) else status
    score = str(source.get("score") or status.get("score") or "")
    period = str(source.get("period") or status.get("period") or "")
    if not score:
        return None
    return parse_tennis_score(score, period)


def _tennis_state_age_seconds(event: Event, as_of: datetime) -> float | None:
    """Seconds since the cached live tennis score was received, or None."""
    status = _sports_status_detail.get(event.polymarket_slug or "")
    received = status.get("_received_at") if status else None
    if not received:
        return None
    try:
        stamp = datetime.fromisoformat(str(received))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = (as_of - stamp).total_seconds()
    if age < -settings.provider_clock_skew_seconds:
        return None  # future-dated: fail closed, never treat as fresh
    return max(0.0, age)


def _tennis_model_probabilities(
    event: Event, signals: list, *, as_of: datetime
) -> tuple[dict[str, float], dict[str, float]]:
    """``(probabilities, uncertainties)`` per Polymarket token for a tennis match.

    ``probabilities`` is the independent in-play win probability (pre-match
    anchor propagated through the live set/game score). ``uncertainties`` is the
    model-band half-width, widened for the execution window (feed/compute/
    network/venue latency plus how stale the score already is) so a stale or
    fast-moving state raises the lower-bound edge gate. Both empty unless the
    model is enabled, the event is tennis, we captured a clean pre-match anchor,
    a live score is available, and that score is fresh enough to represent the
    state at a realistic fill."""
    if not settings.enable_tennis_model or not _is_tennis(event):
        return {}, {}
    parsed = _tennis_score_now(event)
    if parsed is None:
        return {}, {}
    p0 = _pregame.get(event.id, {}).get("tennis_p0")
    if p0 is None or not 0 < p0 < 1:
        return {}, {}
    # Latency awareness: skip a score too stale to represent the execution-time
    # state; otherwise fold its age into the movement window.
    age = _tennis_state_age_seconds(event, as_of)
    if age is None or age > settings.max_state_age_seconds:
        return {}, {}
    window = (age or 0.0) + settings.latency_budget_seconds
    sets_home, sets_away, games_home, games_away = parsed
    g = game_prob_from_prematch(p0)
    low, pm_home, high = match_win_probability_band(
        p0, sets_home, sets_away, games_home, games_away)
    model_sigma = max(0.0, (high - low) / 2.0)
    swing = next_game_swing(sets_home, sets_away, games_home, games_away, g)
    sigma = execution_sigma(model_sigma, swing, window)
    probabilities: dict[str, float] = {}
    uncertainties: dict[str, float] = {}
    for signal in signals:
        if not signal.token_id or not _is_tennis_moneyline(signal.market):
            continue
        side = _moneyline_side(signal.outcome, event)
        if side is not None:
            probabilities[signal.token_id] = pm_home if side == "home" else 1.0 - pm_home
            uncertainties[signal.token_id] = sigma
    return probabilities, uncertainties


def _state_age_seconds(state: GameState, as_of: datetime) -> float | None:
    """Seconds since a GameState was observed *at the provider*, or None when the
    provider did not stamp it. Returning None (rather than a receipt-time age of
    ~0) lets callers fail closed: we never treat state of unknown freshness as
    fresh, since an old score is exactly the latency/adverse-selection hazard the
    model must avoid."""
    if not state.timestamp_trusted or state.provider_timestamp is None:
        return None
    stamp = state.provider_timestamp
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = (as_of - stamp).total_seconds()
    if age < -settings.provider_clock_skew_seconds:
        return None  # future-dated provider timestamp: fail closed, never fresh
    return max(0.0, age)


def _lead_model_probabilities(
    event: Event, signals: list, *, as_of: datetime
) -> tuple[dict[str, float], dict[str, float]]:
    """``(probabilities, uncertainties)`` per token for a lead/clock sport
    (basketball, football, hockey): the pre-match price anchor propagated through
    the live lead and game clock via the Brownian-margin model, with the band
    widened for the execution window. Empty unless the model is on, the league is
    supported, a live state with a known regulation fraction exists (overtime is
    skipped), a clean pre-match anchor was captured, and the state is fresh enough
    to represent a realistic fill."""
    if not settings.enable_lead_model:
        return {}, {}
    rule = league_rule(event.sport, event.league)
    params = LEAD_SPORT_PARAMS.get(rule.key) if rule is not None else None
    if params is None:
        return {}, {}
    states = store.states.get(event.id) or []
    if not states:
        return {}, {}
    state = states[-1]
    _, fraction = game_progress(event.sport, state.period, state.clock, event.league)
    if fraction is None:
        return {}, {}
    p0 = _pregame.get(event.id, {}).get("prematch_home_p")
    if p0 is None or not 0 < p0 < 1:
        return {}, {}
    age = _state_age_seconds(state, as_of)
    if age is None or age > settings.max_state_age_seconds:
        return {}, {}
    window = (age or 0.0) + settings.latency_budget_seconds
    lead = float(state.home_score) - float(state.away_score)
    low, pm_home, high = win_probability_band(p0, lead, fraction, params.sigma)
    model_sigma = max(0.0, (high - low) / 2.0)
    swing = score_swing(p0, lead, fraction, params.sigma, params.score_unit)
    sigma = execution_sigma(model_sigma, swing, window,
                            seconds_per_game=params.seconds_per_score)
    probabilities: dict[str, float] = {}
    uncertainties: dict[str, float] = {}
    for signal in signals:
        if not signal.token_id or line_type(signal.market) != "moneyline":
            continue
        side = _moneyline_side(signal.outcome, event)
        if side is not None:
            probabilities[signal.token_id] = pm_home if side == "home" else 1.0 - pm_home
            uncertainties[signal.token_id] = sigma
    return probabilities, uncertainties


_SOCCER_SECONDS_PER_GOAL = 1800.0  # rough interval between goals, for the latency term


def _soccer_model_probabilities(
    event: Event, signals: list, *, as_of: datetime
) -> tuple[dict[str, float], dict[str, float]]:
    """``(probabilities, uncertainties)`` per token for a soccer 1X2 match: the
    pre-match price inverted to Poisson scoring rates (cached at kickoff) and
    propagated through the live score and clock. Empty unless enabled, a clean
    pre-match anchor was inverted, a live state with a known regulation fraction
    exists (added/extra time is skipped), and the state is fresh enough for a
    realistic fill. Restricted to moneyline/1X2 selections so spreads and totals
    are never mispriced as a result bet."""
    if not settings.enable_soccer_model or not _is_soccer(event):
        return {}, {}
    rates = _pregame.get(event.id, {}).get("soccer_rates")
    if not rates:
        return {}, {}
    states = store.states.get(event.id) or []
    if not states:
        return {}, {}
    state = states[-1]
    _, fraction = game_progress(event.sport, state.period, state.clock, event.league)
    if fraction is None:
        return {}, {}
    age = _state_age_seconds(state, as_of)
    if age is None or age > settings.max_state_age_seconds:
        return {}, {}
    window = (age or 0.0) + settings.latency_budget_seconds
    lam_home, lam_away = rates
    home_now = int(state.home_score)
    away_now = int(state.away_score)
    probabilities: dict[str, float] = {}
    uncertainties: dict[str, float] = {}
    for signal in signals:
        if not signal.token_id:
            continue
        side = _soccer_side(signal.outcome, event)
        if side is None:
            continue
        # Home/away must be a moneyline/1X2 market; draw only exists in 1X2.
        if side != "draw" and line_type(signal.market) != "moneyline":
            continue
        low, mid, high = soccer_model.result_band(
            lam_home, lam_away, home_now, away_now, fraction, side)
        model_sigma = max(0.0, (high - low) / 2.0)
        swing = soccer_model.result_swing(
            lam_home, lam_away, home_now, away_now, fraction, side)
        sigma = execution_sigma(model_sigma, swing, window,
                                seconds_per_game=_SOCCER_SECONDS_PER_GOAL)
        probabilities[signal.token_id] = mid
        uncertainties[signal.token_id] = sigma
    return probabilities, uncertainties


def _model_probabilities(
    event: Event, signals: list, *, as_of: datetime
) -> tuple[dict[str, float], dict[str, float]]:
    """Dispatch to the sport's independent in-play model (tennis, soccer, lead)."""
    if _is_tennis(event):
        return _tennis_model_probabilities(event, signals, as_of=as_of)
    if _is_soccer(event):
        return _soccer_model_probabilities(event, signals, as_of=as_of)
    return _lead_model_probabilities(event, signals, as_of=as_of)


def _prematch_anchor(signals: list, event: Event, side: str,
                     market_ok: Callable[[str], bool]) -> float | None:
    """Pre-match model probability of the ``side`` (home/away/draw) selection on a
    market the sport's model actually prices, or None. The ``market_ok`` guard
    keeps a spread or total price from being captured as a win-probability anchor
    (which would silently mis-calibrate the whole model for that event)."""
    for signal in signals:
        if _soccer_side(signal.outcome, event) != side:
            continue
        if not market_ok(signal.market):
            continue
        prob = signal.model_probability or 0.0
        if 0.0 < prob < 1.0:
            return float(prob)
    return None


def _is_moneyline_market(market: str) -> bool:
    return line_type(market) == "moneyline"


def _tennis_final_scores(event: Event) -> tuple[float, float] | None:
    """Tennis match result as ``(sets_home, sets_away)`` from the final score
    string, or None if it can't be resolved decisively.

    The generic state parser keeps games-in-a-set in the GameState (a multi-set
    final string does not even parse), so grading a tennis moneyline off
    ``home_score``/``away_score`` compares set games, not the match winner. The
    winner of the last completed set is the match winner, so the set count grades
    it correctly."""
    parsed = _tennis_score_now(event)
    if parsed is None:
        return None
    sets_home, sets_away, _, _ = parsed
    if sets_home == sets_away:
        return None
    return float(sets_home), float(sets_away)


def _settle_scores(event: Event, states: list) -> tuple[float, float]:
    """The ``(home, away)`` scores to grade an event by. Tennis is graded by set
    count (see :func:`_tennis_final_scores`); every other sport uses the final
    live score directly."""
    if _is_tennis(event):
        tennis = _tennis_final_scores(event)
        if tennis is not None:
            return tennis
    state = states[-1]
    return state.home_score, state.away_score


def recompute(event_id: str, *, as_of: datetime) -> list:
    event = store.events.get(event_id)
    if event is None:  # event removed between emit and callback
        return []
    quotes = store.quote_values(event_id)
    prior = _pregame.setdefault(event_id, {"spread": None, "total": None})
    if prior["spread"] is None or prior["total"] is None:
        spread, total = pregame_priors(quotes, event.home, event.away)
        prior["spread"] = prior["spread"] if prior["spread"] is not None else spread
        prior["total"] = prior["total"] if prior["total"] is not None else total
    signals = engine.evaluate(event_id, quotes, store.states.get(event_id, []), event.away,
                              sport=event.sport, league=event.league,
                              home_outcome=event.home,
                              pregame_spread=prior["spread"], pregame_total=prior["total"],
                              as_of=as_of, canonical_event_id=event.canonical_event_id)
    store.set_signals(event_id, signals)
    # Anchor an in-play model to the market at the FIRST observation, whatever the
    # game state: invert the model's strength parameter from the current price and
    # live score so a game joined in progress is anchored (and can trade) rather
    # than skipped. Captured once; at true kickoff this reduces to the pre-match
    # anchor. The market guard keeps a spread/total price from being taken as the
    # win-probability anchor.
    if settings.enable_tennis_model and _is_tennis(event) and prior.get("tennis_p0") is None:
        parsed = _tennis_score_now(event)
        p_now = _prematch_anchor(signals, event, "home", _is_tennis_moneyline)
        if parsed is not None and p_now is not None:
            p0 = tennis_implied_prematch_price(p_now, *parsed)
            if p0 is not None:
                prior["tennis_p0"] = p0
    if (settings.enable_lead_model and not _is_tennis(event)
            and prior.get("prematch_home_p") is None):
        rule = league_rule(event.sport, event.league)
        params = LEAD_SPORT_PARAMS.get(rule.key) if rule is not None else None
        states = store.states.get(event_id) or []
        p_now = _prematch_anchor(signals, event, "home", _is_moneyline_market)
        if params is not None and states and p_now is not None:
            state = states[-1]
            _, fraction = game_progress(event.sport, state.period, state.clock, event.league)
            if fraction is not None:
                lead = float(state.home_score) - float(state.away_score)
                p0 = lead_implied_prematch_price(p_now, lead, fraction, params.sigma)
                if p0 is not None:
                    prior["prematch_home_p"] = p0
    if settings.enable_soccer_model and _is_soccer(event) and "soccer_rates" not in prior:
        states = store.states.get(event_id) or []
        home_p = _prematch_anchor(signals, event, "home", _is_moneyline_market)
        # A 1X2 draw is a result in its own right, so it is not filtered by the
        # moneyline guard (its market label is the draw condition itself).
        draw_p = _prematch_anchor(signals, event, "draw", lambda _market: True)
        if states and home_p is not None and draw_p is not None:
            state = states[-1]
            _, fraction = game_progress(event.sport, state.period, state.clock, event.league)
            if fraction is not None:
                # Stored once (may be None if the prior is unusable) so we neither
                # re-invert every cycle nor retry a bad anchor forever.
                prior["soccer_rates"] = soccer_model.rates_from_state(
                    home_p, draw_p, int(state.home_score), int(state.away_score), fraction)
    return signals


async def record(event_id: str, *, as_of: datetime | None = None) -> None:
    async with _event_lock(event_id):
        if event_id in _terminal_events or event_id in _finalized:
            return
        decision_at = as_of or datetime.now(timezone.utc)
        signals = recompute(event_id, as_of=decision_at)
        _notify_subscribers()  # push the fresh snapshot to the dashboard immediately
        event = store.events.get(event_id)
        # Ledger commits fsync to disk; keep that off the event loop.
        if ledger is not None and event is not None and signals:
            await asyncio.to_thread(ledger.record_signals, event, signals)
        if model_lab is not None and event is not None and signals:
            await asyncio.to_thread(
                model_lab.record,
                event,
                signals,
                store.quote_values(event_id),
                list(store.states.get(event_id, [])),
                as_of=decision_at,
            )
        # A terminal state can arrive while the ledger write is in flight. It
        # closes the gate immediately; do not begin a new account entry after it.
        if event_id in _terminal_events or event_id in _finalized:
            return
        if account_book is not None and event is not None:
            quotes = store.quote_values(event_id)
            # One current decision context, computed before mark/exit/entry, so a
            # harness-backed position is managed with the same probability family
            # that opened it (not the odds consensus).
            model_probabilities, model_uncertainty = (
                _model_probabilities(event, signals, as_of=decision_at)
                if signals else ({}, {}))
            exited_bets = await asyncio.to_thread(
                account_book.mark_and_cash_out, event, quotes, signals,
                as_of=decision_at, model_probabilities=model_probabilities,
                max_quote_age_seconds=settings.max_data_age_seconds,
            )
            for paper_event in exited_bets:
                if paper_event.get("webhook_url"):
                    _schedule_notification(paper_event)
            if signals:
                entry_signals = bot_entry_candidates(
                    signals,
                    allow_uncalibrated=settings.allow_uncalibrated_paper,
                    model_probabilities=model_probabilities,
                )
                skipped = len(signals) - len(entry_signals)
                if skipped:
                    runtime_telemetry.increment("bot_entry_prefiltered", skipped)
                if entry_signals:
                    runtime_telemetry.increment(
                        "bot_entry_candidates", len(entry_signals)
                    )
                    placed_bets = await asyncio.to_thread(
                        account_book.place, event, entry_signals, quotes,
                        as_of=decision_at,
                        allow_uncalibrated=settings.allow_uncalibrated_paper,
                        model_probabilities=model_probabilities,
                        model_uncertainty=model_uncertainty,
                        edge_uncertainty_z=settings.edge_uncertainty_z,
                        portfolio_kelly=settings.enable_portfolio_kelly,
                        max_quote_age_seconds=settings.max_data_age_seconds,
                    )
                    for paper_event in placed_bets:
                        if paper_event.get("webhook_url"):
                            _schedule_notification(paper_event)


def _winner_labels(event: Event, home_score: float, away_score: float) -> set[str]:
    if home_score > away_score:
        return {"home", event.home}
    if away_score > home_score:
        return {"away", event.away}
    return {"draw", "Draw"}  # a tie settles the Draw outcome, not nothing


def _evict_live_event_state(event_id: str) -> None:
    """Drop a settled event's heavy live buffers. Keeps its permanent finalized
    marker so a late duplicate terminal update stays idempotent; only the memory
    (quotes/states/signals/pregame anchor) is released."""
    _finalized_order.pop(event_id, None)
    _pregame.pop(event_id, None)
    _event_locks.pop(event_id, None)
    event = store.events.get(event_id)
    if event is not None and event.polymarket_slug:
        _sports_status_detail.pop(event.polymarket_slug, None)
    store.remove_event(event_id)


async def _evict_finalized_overflow() -> None:
    """Evict the oldest resident finalized events beyond the retention cap. Skips
    any that still carry open paper positions (as :func:`delete_event` does), so
    a settled game is never dropped while a bot could still be marked against it;
    such an event is simply retried after a later finalization."""
    while len(_finalized_order) > settings.finalized_event_retention:
        oldest = next(iter(_finalized_order))
        if oldest not in store.events:  # already gone (e.g. manually deleted)
            _finalized_order.pop(oldest, None)
            continue
        open_positions = (await asyncio.to_thread(account_book.open_count, oldest)
                          if account_book is not None else 0)
        if open_positions > 0:
            break  # keep it monitored; a later finalize will reconsider
        _evict_live_event_state(oldest)


async def finalize_event(event_id: str, *, canceled: bool = False) -> None:
    """Stop entry, snapshot/settle a final, or void a provider cancellation.

    Every write is idempotent. The in-memory finalized marker and persisted
    event deletion happen only after all writes succeed, so a transient failure
    can be retried by a later terminal update or process restart.
    """
    if event_id in _finalized:
        return
    if _terminal_events.get(event_id) == "canceled":
        canceled = True
    terminal = "canceled" if canceled else "final"
    _terminal_events.setdefault(event_id, terminal)
    async with _event_lock(event_id):
        if event_id in _finalized:
            return

        # Entry callbacks for this event have drained before this lock was
        # acquired. Cancel and await the infinite feed loops before settlement.
        await _cancel_tasks(tasks.pop(event_id, []))

        event = store.events.get(event_id)
        states = store.states.get(event_id) or []
        # The close mark is the last valid observation recorded before the
        # terminal gate closed. Never synthesize a fresh consensus after suspension.
        # Tennis grades by set count, not games-in-a-set (see _settle_scores).
        settle_home, settle_away = (
            _settle_scores(event, states) if (event and states) else (0.0, 0.0))
        winners = _winner_labels(event, settle_home, settle_away) \
            if (event and states and not canceled) else set()

        def _writes():
            if canceled:
                if ledger is not None:
                    ledger.void_event(event_id, status="canceled")
                if account_book is not None:
                    account_book.void_event(event_id)
                if model_lab is not None and event is not None:
                    model_lab.settle_event(
                        event, settle_home, settle_away, canceled=True
                    )
                return
            if ledger is not None:
                ledger.snapshot_closing(event_id)
                if winners:
                    ledger.settle_moneyline(event_id, winners)
            if account_book is not None and event is not None and states:
                account_book.settle(event, settle_home, settle_away)
            if history_db is not None and event is not None:
                prior = _pregame.get(event_id, {})
                final_state = states[-1] if states else None
                history_db.log_outcome(event, prior.get("spread"), prior.get("total"), final_state)
            if model_lab is not None and event is not None and states:
                model_lab.settle_event(event, settle_home, settle_away)

        await asyncio.to_thread(_writes)
        if monitor_state is not None:
            await asyncio.to_thread(monitor_state.delete_event, event_id)
        _finalized.add(event_id)
        _terminal_events[event_id] = "canceled" if canceled else "final"
        if event_id in store.events:
            _finalized_order[event_id] = None
            _finalized_order.move_to_end(event_id)
    # Trim after releasing the lock so evicting any candidate — including this
    # event itself when retention is 0 — never contends the lock we just held.
    await _evict_finalized_overflow()


async def auto_monitor_loop():
    while True:
        try:
            if _config_state["auto_monitor"]:
                games = await polymarket_sports_events(live_statuses=_sports_status_compact)
                if settings.exclude_restricted_events:
                    games = exclude_restricted_games(games)
                for game in games:
                    if game.get("status") == "live":
                        slug = game.get("slug")
                        if slug and not any(e.polymarket_slug == slug for e in store.events.values()):
                            try:
                                await add_event(EventIn(polymarket_url=f"https://polymarket.com/event/{slug}"))
                            except Exception as exc:
                                logger.warning("Auto-monitor could not add %s: %s", slug, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Auto-monitor discovery failed: %s", exc)
        await asyncio.sleep(60)


def _live_trading_snapshot() -> tuple[
    list[tuple[Event, list]],
    dict[str, GameState],
]:
    """Take one small, internally consistent input snapshot for the sidecar.

    Signals are immutable for the duration of a cycle from the trader's point of
    view.  The existing engine and live store remain the sole calculation path.
    """
    with store.lock:
        monitored = [
            (event, list(store.signals.get(event.id, [])))
            for event in store.events.values()
            if event.id not in _terminal_events and event.id not in _finalized
        ]
        latest_states = {
            event.id: store.states[event.id][-1]
            for event, _ in monitored
            if store.states.get(event.id)
        }
        return monitored, latest_states


async def _run_execution_trading_cycle(
    trader: PolymarketUSAutoTrader,
) -> dict[str, Any]:
    """Run one isolated automation lane against the shared read-only inputs."""
    if trader is None:
        raise TradingPolicyError(
            "Polymarket US execution is disabled on this deployment"
        )
    snapshot, game_states = _live_trading_snapshot()
    if not trader.policy.automation_enabled:
        result = await asyncio.to_thread(
            trader.run_cycle,
            snapshot,
            {"events": []},
            game_states=game_states,
        )
    elif not snapshot:
        result = await asyncio.to_thread(
            trader.run_cycle,
            snapshot,
            {"events": []},
            game_states=game_states,
        )
    else:
        payload = await fetch_polymarket_us_events(limit=500)
        result = await asyncio.to_thread(
            trader.run_cycle,
            snapshot,
            payload,
            game_states=game_states,
        )
    if model_lab is not None:
        positions = await asyncio.to_thread(
            trader.positions,
            include_hidden=True,
        )
        await asyncio.to_thread(model_lab.link_execution_results, positions)
    return result


async def _run_live_trading_cycle() -> dict[str, Any]:
    """Backward-compatible primary-lane helper used by existing API callers."""
    return await _run_execution_trading_cycle(_require_live_trader())


async def execution_trading_loop(
    trader: PolymarketUSAutoTrader,
    *,
    lane: Literal["live", "dry_run"],
):
    while True:
        delay = 30
        try:
            delay = trader.policy.cycle_seconds
            if trader.policy.automation_enabled:
                await _run_execution_trading_cycle(trader)
            elif lane == "live" and any(
                    position["mode"] == "live"
                    for position in await asyncio.to_thread(
                        trader.positions,
                        open_only=True,
                    )
            ):
                # Read-only reconciliation continues while live automation is
                # off so a phone-side sale is reflected locally. The isolated
                # dry-run lane never authenticates or reconciles venue orders.
                await asyncio.to_thread(trader.synchronize_live_positions)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Polymarket US %s automation cycle failed: %s",
                lane,
                exc,
            )
        await asyncio.sleep(max(10, delay))


async def live_trading_loop():
    """Backward-compatible loop for the primary live lane."""
    trader = _require_live_trader()
    await execution_trading_loop(trader, lane="live")


def _pin_execution_lane(
    trader: PolymarketUSAutoTrader,
    mode: Literal["live", "dry_run"],
) -> None:
    """Keep a workstation lane permanently assigned to one execution mode."""
    if trader.policy.execution_mode == mode:
        return
    trader.configure({
        "execution_mode": mode,
        "automation_enabled": False,
    })


def _start_event_feeds(event: Event) -> None:
    group = []
    if event.polymarket_slug:
        group.append(asyncio.create_task(
            polymarket_market_stream(
                event,
                on_quotes,
                on_polymarket_snapshot,
                on_state,
            )
        ))
    if event.odds_api_sport:
        group.append(asyncio.create_task(odds_api_poll(
            event,
            on_quotes,
            enabled=lambda: _config_state["odds_api_enabled"],
            interval_seconds=lambda: _config_state.get(
                "odds_api_poll_seconds",
                settings.odds_poll_seconds,
            ),
        )))
        if settings.enable_action_network:
            group.append(asyncio.create_task(action_network_poll(event, on_quotes)))
        if settings.enable_pinnacle_guest:
            group.append(asyncio.create_task(pinnacle_poll(
                event, on_quotes, api_key=settings.pinnacle_guest_api_key)))
    if settings.enable_mlb_live_feed and is_mlb_event(event):
        group.append(asyncio.create_task(mlb_linescore_poll(
            event,
            on_state,
            interval_seconds=settings.mlb_live_poll_seconds,
        )))
    tasks[event.id] = group


@asynccontextmanager
async def lifespan(_: FastAPI):
    global ledger, account_book, history_db, monitor_state
    global live_trader, dry_run_trader, model_lab
    sports_task: asyncio.Task | None = None
    auto_task: asyncio.Task | None = None
    execution_tasks: list[asyncio.Task] = []
    if settings.enable_memory_trace:
        start_memory_trace()
    open_shared_client()  # shared keep-alive pool for one-shot provider fetches
    try:
        ledger = Ledger()
        account_book = AccountBook() if settings.enable_paper_bots else None
        history_db = HistoryDB()
        monitor_state = MonitorState()
        live_trader = (
            PolymarketUSAutoTrader(
                (
                    str(settings.polymarket_us_trading_db)
                    if settings.workstation_mode
                    else None
                ),
                key_id=settings.polymarket_us_key_id,
                secret_key=settings.polymarket_us_secret_key,
            )
            if (
                settings.workstation_mode
                or settings.enable_polymarket_us_trading
            )
            else None
        )
        dry_run_trader = (
            PolymarketUSAutoTrader(
                str(settings.polymarket_us_dry_run_db),
                key_id=settings.polymarket_us_key_id,
                secret_key=settings.polymarket_us_secret_key,
            )
            if settings.workstation_mode and live_trader is not None
            else None
        )
        if dry_run_trader is not None:
            _pin_execution_lane(live_trader, "live")
            _pin_execution_lane(dry_run_trader, "dry_run")
        model_lab = (
            SportModelLab(
                str(settings.model_lab_db)
                if settings.workstation_mode
                else None
            )
            if (
                settings.workstation_mode
                or settings.enable_polymarket_us_trading
            )
            else None
        )
        if account_book is not None:
            account_book.seed(DEFAULT_STRATEGIES)
        _config_state["auto_monitor"] = monitor_state.auto_monitor(False)
        _config_state["odds_api_enabled"] = monitor_state.odds_api_enabled(True)
        _config_state["odds_api_poll_seconds"] = (
            monitor_state.odds_api_poll_seconds(settings.odds_poll_seconds)
        )
        for event in monitor_state.events():
            if not game_start_matches_slug(
                event.polymarket_slug, event.game_start
            ):
                logger.warning(
                    "Dropping persisted event with mismatched slug/start identity: "
                    "%s (%s / %s)",
                    event.name,
                    event.polymarket_slug,
                    event.game_start,
                )
                await asyncio.to_thread(monitor_state.delete_event, event.id)
                continue
            store.add_event(event)
            _finalized.discard(event.id)
            _terminal_events.pop(event.id, None)
            _start_event_feeds(event)
        sports_task = asyncio.create_task(polymarket_sports_stream(
            lambda: list(store.events.values()), on_state, on_sports_status))
        auto_task = asyncio.create_task(auto_monitor_loop())
        if live_trader is not None:
            execution_tasks.append(asyncio.create_task(
                execution_trading_loop(live_trader, lane="live")
            ))
        if dry_run_trader is not None:
            execution_tasks.append(asyncio.create_task(
                execution_trading_loop(dry_run_trader, lane="dry_run")
            ))
        yield
    finally:
        # Close the entry gate, then let any already-running record() section
        # finish before canceling feed tasks and closing database connections.
        for event_id in list(store.events):
            _terminal_events.setdefault(event_id, "shutdown")
        for lock in list(_event_locks.values()):
            async with lock:
                pass
        background = [task for group in tasks.values() for task in group]
        tasks.clear()
        if sports_task is not None:
            background.append(sports_task)
        if auto_task is not None:
            background.append(auto_task)
        background.extend(execution_tasks)
        background.extend(_notification_tasks)
        _notification_tasks.clear()
        await _cancel_tasks(background)
        # Close the shared HTTP pool only after every task that could borrow it
        # has been cancelled and awaited above.
        await close_shared_client()

        for database_store in (
            ledger,
            account_book,
            history_db,
            monitor_state,
            live_trader,
            dry_run_trader,
            model_lab,
        ):
            if database_store is not None:
                try:
                    await asyncio.to_thread(database_store.close)
                except Exception as exc:
                    logger.warning("Could not close %s cleanly: %s",
                                   type(database_store).__name__, exc)
        ledger = None
        account_book = None
        history_db = None
        monitor_state = None
        live_trader = None
        dry_run_trader = None
        model_lab = None
        with store.lock:
            store.events.clear()
            store.states.clear()
            store.quotes.clear()
            store.signals.clear()
            store.state_updates.clear()
            store.quote_updates.clear()
        _pregame.clear()
        _finalized.clear()
        _finalized_order.clear()
        _terminal_events.clear()
        _event_locks.clear()
        _event_deletions.clear()
        _sports_status_compact.clear()
        _sports_status_detail.clear()


app = FastAPI(title="Live Sports Signal Monitor", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    client = request.client.host if request.client else "unknown"
    if request.url.path.startswith("/api/") and not api_limiter.allow(client):
        return Response(status_code=429, content="rate limit exceeded")
    response = await call_next(request)
    # Deploys replace these files in place while keeping the same public URLs.
    # Browsers may otherwise reuse a heuristically fresh copy of index.js and keep
    # running an older recommendation collector after the backend has changed.
    if request.url.path in {"/", "/watch"} or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self'; img-src 'self' data:; connect-src 'self' "
        "https://*.polymarket.com wss://*.polymarket.com; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    if settings.environment in {"production", "prod"}:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


class EventIn(BaseModel):
    polymarket_url: str | None = None
    name: str | None = None
    sport: str | None = None
    home: str | None = None
    away: str | None = None
    league: str = ""
    polymarket_slug: str | None = None
    odds_api_sport: str | None = None
    odds_api_event_id: str | None = None
    game_start: str | None = None


class PositionIn(BaseModel):
    token_id: str
    market: str
    outcome: str
    shares: float = Field(gt=0, le=1_000_000)
    avg_entry_price: float = Field(gt=0, lt=1)


class StrategyIn(BaseModel):
    name: str
    edge_threshold: float = 0.03
    sizing: str = "kelly"
    kelly_multiplier: float = 1.0
    flat_stake: float = 100.0
    start_bankroll: float = 10000.0
    webhook_url: str = ""
    cash_out_enabled: bool = False
    # Optional game allow-list (event ids). Empty = free bet (any qualifying game).
    events: list[str] = Field(default_factory=list)


class StrategyUpdateIn(BaseModel):
    cash_out_enabled: bool


class ConfigIn(BaseModel):
    auto_monitor: bool | None = None
    odds_api_enabled: bool | None = None
    odds_api_poll_seconds: float | None = Field(default=None, ge=5, le=3600)


class LiveTradingConfigIn(BaseModel):
    automation_enabled: bool | None = None
    execution_mode: str | None = None
    auto_cashout: bool | None = None
    adaptive_exit_enabled: bool | None = None
    adaptive_exit_profile: str | None = None
    adaptive_exit_horizon_minutes: float | None = None
    adaptive_exit_min_samples: int | None = None
    adaptive_exit_max_tightening: float | None = None
    volatility_stop_enabled: bool | None = None
    stop_confirmation_readings: int | None = None
    stop_grace_minutes: float | None = None
    catastrophic_stop_multiplier: float | None = None
    post_exit_tracking_minutes: float | None = None
    require_engine_entry: bool | None = None
    required_engine_gates: list[str] | None = None
    allowed_market_types: list[str] | None = None
    trading_allocation_usd: float | None = None
    risk_preset: str | None = None
    max_total_exposure_usd: float | None = None
    minimum_cash_reserve_usd: float | None = None
    max_position_usd: float | None = None
    max_event_exposure_usd: float | None = None
    max_daily_loss_usd: float | None = None
    max_open_positions: int | None = None
    max_orders_per_hour: int | None = None
    min_edge: float | None = None
    max_edge: float | None = None
    min_signal_quality: float | None = None
    min_reference_sources: int | None = None
    min_entry_price: float | None = None
    max_entry_price: float | None = None
    max_spread: float | None = None
    min_book_shares: float | None = None
    min_hold_minutes: float | None = None
    profit_target: float | None = None
    trailing_drawdown: float | None = None
    stop_loss: float | None = None
    exit_edge: float | None = None
    cycle_seconds: int | None = None
    candidate_cooldown_seconds: int | None = None
    max_entries_per_event_per_hour: int | None = None
    min_mlb_fraction_remaining: float | None = None


class LiveTradingArmIn(BaseModel):
    confirmation: str
    seconds: int = Field(default=1800, ge=60, le=MAX_ARM_SECONDS)


class LiveTradingLiquidateIn(BaseModel):
    mode: Literal["dry_run", "live"]
    confirmation: str = ""


class DryRunHistoryClearIn(BaseModel):
    confirmation: str


class AdaptiveExitHistoryClearIn(BaseModel):
    confirmation: str


class LiveTradingPositionExitIn(BaseModel):
    confirmation: str = ""


class LivePerformanceResetIn(BaseModel):
    confirmation: str


class RiskSessionResetIn(BaseModel):
    confirmation: str


class PolicyAdviceIn(BaseModel):
    objective: Literal["protect_profit", "balanced", "more_trades"] = "balanced"
    target_trades_per_hour: float = Field(default=4.0, ge=0.25, le=30)
    analysis_mode: Literal["dry_run", "live"] | None = None
    lookback_days: Literal[0, 7, 30, 90, 180, 365] = 0
    market_types: list[Literal["moneyline", "spread", "total"]] = Field(
        default_factory=lambda: ["moneyline", "spread", "total"]
    )


class PolicyAdviceApplyIn(BaseModel):
    confirmation: str


class ModelLabFitIn(BaseModel):
    sport: str = Field(min_length=1, max_length=50)
    league: str = Field(default="", max_length=50)
    market: Literal["moneyline"] = "moneyline"


class RuntimeCredentialsIn(BaseModel):
    key_id: str = Field(min_length=1, max_length=1_000)
    secret_key: SecretStr


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/watch")
async def watch():
    return FileResponse(Path(__file__).parent / "static" / "watch.html")


@app.get("/api/health")
async def health():
    return {"status": "live", "version": __version__}


@app.get("/api/ready")
async def ready():
    dependencies = {
        "ledger": ledger is not None,
        "accounts": account_book is not None or not settings.enable_paper_bots,
        "history": history_db is not None,
        "monitor_state": monitor_state is not None,
        "native_engine": engine is not None,
        "polymarket_us_trader": (
            live_trader is not None if settings.workstation_mode else True
        ),
    }
    if not all(dependencies.values()):
        raise HTTPException(status_code=503, detail={"status": "not_ready",
                                                     "dependencies": dependencies})
    return {"status": "ready", "dependencies": dependencies,
            "tracked_events": len(store.events), "background_groups": len(tasks),
            "paper_bots_enabled": settings.enable_paper_bots}


@app.get("/api/runtime", dependencies=[Depends(verify_auth)])
async def runtime_status():
    with store.lock:
        live_quotes = sum(len(buffer) for buffer in store.quotes.values())
        live_states = sum(len(buffer) for buffer in store.states.values())
        quote_updates = sum(store.quote_updates.values())
        state_updates = sum(store.state_updates.values())
    return {
        "counters": runtime_telemetry.snapshot(),
        "odds_api_quota": dict(_odds_quota),
        "tracked_events": len(store.events),
        "feed_groups": {event_id: len(group) for event_id, group in tasks.items()},
        "odds_api_enabled": _config_state["odds_api_enabled"],
        "deletions_in_flight": len(_event_deletions),
        "notifications_in_flight": len(_notification_tasks),
        "memory": memory_snapshot(),
        "buffers": {
            "sports_status_compact": len(_sports_status_compact),
            "sports_status_detail": len(_sports_status_detail),
            "live_quotes": live_quotes,
            "live_states": live_states,
            "quote_updates": quote_updates,
            "state_updates": state_updates,
            "finalized_events": len(_finalized),
        },
    }


@app.post("/api/login")
async def login(request: Request, response: Response, username: str = Form(...),
                password: str = Form(...)):
    client = request.client.host if request.client else "unknown"
    if not login_limiter.allow(client):
        raise HTTPException(status_code=429, detail="Too many login attempts")
    authenticated = auth_manager.login(username, password)
    if authenticated:
        token, session = authenticated
        secure = settings.environment in {"production", "prod"}
        response.set_cookie(key=_cookie_name("session_token"), value=token, httponly=True,
                            secure=secure, samesite="strict", path="/")
        response.set_cookie(key=_cookie_name("csrf_token"), value=session.csrf_token, httponly=False,
                            secure=secure, samesite="strict", path="/")
        return {"status": "ok", "csrf_token": session.csrf_token,
                "expires_at": session.expires_at.isoformat()}
    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.post("/api/logout", dependencies=[Depends(verify_auth)])
async def logout(request: Request, response: Response):
    auth_manager.revoke(request.cookies.get(_cookie_name("session_token")))
    response.delete_cookie(_cookie_name("session_token"), path="/")
    response.delete_cookie(_cookie_name("csrf_token"), path="/")
    return {"status": "ok"}


@app.get("/api/config", dependencies=[Depends(verify_auth)])
async def config():
    calibrated = calibration_artifact is not None
    model_paths = {
        "tennis": settings.enable_tennis_model,
        "lead": settings.enable_lead_model,
        "soccer": settings.enable_soccer_model,
    }
    if not settings.enable_paper_bots:
        bot_mode = "Paper bots are disabled on this deployment."
    elif calibrated:
        bot_mode = "Validated consensus calibration is loaded."
    elif settings.allow_uncalibrated_paper:
        bot_mode = (
            "Research-mode uncalibrated paper entries are enabled; all other "
            "quality and execution gates still apply."
        )
    elif any(model_paths.values()):
        bot_mode = (
            "Consensus entries wait for validated calibration; enabled independent "
            "sport models may still place paper trades."
        )
    else:
        bot_mode = (
            "Bots are observing only: no calibration artifact or independent live "
            "model is enabled, so consensus entries remain blocked."
        )
    return {
        "confidence_threshold": engine.confidence_threshold,
        "edge_threshold": engine.edge_threshold,
        "max_age_seconds": engine.max_age_seconds,
        "auto_monitor": _config_state["auto_monitor"],
        "odds_api_enabled": _config_state["odds_api_enabled"],
        "odds_api_poll_seconds": _config_state.get(
            "odds_api_poll_seconds",
            settings.odds_poll_seconds,
        ),
        "workstation": {
            "enabled": settings.workstation_mode,
            "paper_bots_enabled": settings.enable_paper_bots,
            "polymarket_us_trading_enabled": live_trader is not None,
            "polymarket_us": (
                live_trader.credential_status()
                if live_trader is not None
                else polymarket_us_credential_status(
                    settings.polymarket_us_key_id,
                    settings.polymarket_us_secret_key,
                )
            ),
            "live_trading": live_trader.status() if live_trader is not None else None,
            "dry_run_trading": (
                dry_run_trader.status()
                if dry_run_trader is not None
                else None
            ),
        },
        "paper_bot_policy": {
            "enabled": settings.enable_paper_bots,
            "calibration_loaded": calibrated,
            "allow_uncalibrated": settings.allow_uncalibrated_paper,
            "models": model_paths,
            "message": bot_mode,
        },
    }


@app.post("/api/config", dependencies=[Depends(verify_auth)])
async def update_config(payload: ConfigIn):
    if payload.auto_monitor is not None:
        _config_state["auto_monitor"] = payload.auto_monitor
        if monitor_state is not None:
            await asyncio.to_thread(monitor_state.set_auto_monitor, payload.auto_monitor)
    if payload.odds_api_enabled is not None:
        _config_state["odds_api_enabled"] = payload.odds_api_enabled
        if monitor_state is not None:
            await asyncio.to_thread(
                monitor_state.set_odds_api_enabled,
                payload.odds_api_enabled,
            )
    if payload.odds_api_poll_seconds is not None:
        _config_state["odds_api_poll_seconds"] = payload.odds_api_poll_seconds
        if monitor_state is not None:
            await asyncio.to_thread(
                monitor_state.set_odds_api_poll_seconds,
                payload.odds_api_poll_seconds,
            )
    return await config()


_discover_cache: dict = {"at": 0.0, "data": []}
_polymarket_us_cache: dict = {"at": 0.0, "data": None}


def _active_polymarket_us_credentials() -> tuple[str, str]:
    if live_trader is not None:
        return live_trader._credential_pair_for_server()
    return (
        settings.polymarket_us_key_id,
        settings.polymarket_us_secret_key,
    )


@app.get("/api/discover", dependencies=[Depends(verify_auth)])
async def discover(refresh: bool = False):
    """Browse live/upcoming Polymarket sports games to add without a link."""
    now = time.monotonic()
    if not refresh and _discover_cache["data"] and now - _discover_cache["at"] < 45:
        return _discover_cache["data"]  # cache so browsing doesn't hammer Gamma
    try:
        games = await polymarket_sports_events(live_statuses=_sports_status_compact)
    except Exception as exc:
        raise HTTPException(502, f"Could not reach Polymarket: {exc}") from exc
    if settings.exclude_restricted_events:
        games = exclude_restricted_games(games)
    _discover_cache.update(at=now, data=games)
    return games


@app.get("/api/polymarket-us/status", dependencies=[Depends(verify_auth)])
async def polymarket_us_status():
    """Return safe venue capability metadata; never return either API credential."""
    result = {
        "workstation": settings.workstation_mode,
        "deployment": "local" if settings.workstation_mode else "hosted",
        "venue": "Polymarket US",
        "public_base_url": POLYMARKET_US_PUBLIC_BASE_URL,
        "authenticated_base_url": POLYMARKET_US_AUTHENTICATED_BASE_URL,
        **(
            live_trader.credential_status()
            if live_trader is not None
            else polymarket_us_credential_status(
                settings.polymarket_us_key_id,
                settings.polymarket_us_secret_key,
            )
        ),
    }
    if live_trader is not None:
        result.update({
            "trading_enabled": True,
            "automation": live_trader.status(),
            "automation_lanes": await _execution_lane_summaries(),
        })
    return result


@app.get("/api/polymarket-us/events", dependencies=[Depends(verify_auth)])
async def polymarket_us_events(refresh: bool = False, limit: int = 60):
    """Browse the US/mobile venue without mixing it into the validated engine."""
    now = time.monotonic()
    cached = _polymarket_us_cache["data"]
    if not refresh and cached is not None and now - _polymarket_us_cache["at"] < 30:
        return cached
    try:
        payload = await fetch_polymarket_us_events(limit=max(1, min(limit, 100)))
    except Exception as exc:
        logger.warning("Polymarket US public research fetch failed: %s", exc)
        raise HTTPException(502, f"Could not reach Polymarket US: {exc}") from exc
    _polymarket_us_cache.update(at=now, data=payload)
    return payload


@app.get("/api/polymarket-us/account", dependencies=[Depends(verify_auth)])
async def polymarket_us_account():
    """Read account balances and every position, including unmanaged positions."""
    key_id, secret_key = _active_polymarket_us_credentials()
    if not key_id or not secret_key:
        raise HTTPException(
            409,
            "Add a session key below or configure POLYMARKET_US_KEY_ID and "
            "POLYMARKET_US_SECRET_KEY in the server environment",
        )
    try:
        return await fetch_polymarket_us_account(
            key_id,
            secret_key,
        )
    except PolymarketUSResearchError as exc:
        logger.warning("Polymarket US read-only account check failed: %s", exc)
        raise HTTPException(502, f"Polymarket US authentication failed: {exc}") from exc


@app.post(
    "/api/polymarket-us/runtime-credentials",
    dependencies=[Depends(verify_execution_admin)],
)
async def polymarket_us_runtime_credentials(payload: RuntimeCredentialsIn):
    """Verify and install a key in process memory only.

    The request body is never logged or persisted. Installing a different
    account revokes automation authority and is rejected while a live managed
    position is open.
    """
    trader = _require_live_trader()
    key_id = payload.key_id.strip()
    secret_key = payload.secret_key.get_secret_value().strip()
    if not secret_key:
        raise HTTPException(422, "Secret Key is required")
    try:
        account = await fetch_polymarket_us_account(key_id, secret_key)
        status = await asyncio.to_thread(
            trader.set_runtime_credentials,
            key_id,
            secret_key,
            source="runtime",
        )
    except PolymarketUSResearchError as exc:
        logger.warning("Runtime Polymarket US credential verification failed")
        raise HTTPException(502, f"Polymarket US authentication failed: {exc}") from exc
    except TradingPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc
    balances = account.get("balances") if isinstance(account, dict) else []
    positions = account.get("positions") if isinstance(account, dict) else {}
    return {
        "status": "verified",
        "credentials": status,
        "account": {
            "balance_records": len(balances) if isinstance(balances, list) else 0,
            "position_records": len(positions) if isinstance(positions, dict) else 0,
            "fetched_at": account.get("fetched_at") if isinstance(account, dict) else None,
        },
        "automation_stopped": True,
        "message": (
            "Credential verified and held in server process memory only. "
            "Review the account, then explicitly enable and arm automation."
        ),
    }


@app.delete(
    "/api/polymarket-us/runtime-credentials",
    dependencies=[Depends(verify_execution_admin)],
)
async def clear_polymarket_us_runtime_credentials():
    trader = _require_live_trader()
    source = (
        "environment"
        if settings.polymarket_us_key_id and settings.polymarket_us_secret_key
        else "none"
    )
    try:
        status = await asyncio.to_thread(
            trader.set_runtime_credentials,
            settings.polymarket_us_key_id,
            settings.polymarket_us_secret_key,
            source=source,
        )
    except TradingPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "status": "runtime_credentials_forgotten",
        "credentials": status,
        "automation_stopped": True,
    }


def _require_live_trader() -> PolymarketUSAutoTrader:
    if live_trader is None:
        raise HTTPException(
            409,
            "Polymarket US automation is disabled; set "
            "ENABLE_POLYMARKET_US_TRADING=true on the server",
        )
    return live_trader


def _require_execution_lane(
    lane: Literal["live", "dry_run"] | None = None,
) -> PolymarketUSAutoTrader:
    """Return one isolated local automation lane.

    Omitting ``lane`` preserves the original single-trader API behavior for
    existing clients and deployments. The workstation UI always names a lane.
    """
    if lane == "dry_run":
        if dry_run_trader is None:
            if settings.workstation_mode:
                raise HTTPException(
                    409,
                    "The isolated dry-run lane is unavailable; restart the "
                    "local workstation.",
                )
            return _require_live_trader()
        return dry_run_trader
    return _require_live_trader()


async def _execution_lane_summaries() -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for lane, trader in (
        ("live", live_trader),
        ("dry_run", dry_run_trader),
    ):
        if trader is None:
            continue
        status = await asyncio.to_thread(trader.status)
        policy = status.get("policy") or {}
        summaries[lane] = {
            "lane": lane,
            "automation_enabled": bool(policy.get("automation_enabled")),
            "armed": bool(status.get("armed")),
            "auto_cashout": bool(policy.get("auto_cashout")),
            "open_positions": int(status.get("open_managed_positions") or 0),
            "managed_exposure_usd": float(
                status.get("managed_exposure_usd") or 0.0
            ),
            "last_cycle_summary": status.get("last_cycle_summary"),
            "cycle_seconds": policy.get("cycle_seconds"),
        }
    return summaries


async def _decorate_execution_status(
    status: dict[str, Any],
    *,
    lane: Literal["live", "dry_run"] | None,
    trader: PolymarketUSAutoTrader,
) -> dict[str, Any]:
    """Attach lane identity and both scheduler summaries to an action result."""
    status["lane"] = lane or trader.policy.execution_mode
    status["lanes"] = await _execution_lane_summaries()
    return status


@app.get(
    "/api/polymarket-us/trading/status",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_status(
    lane: Literal["live", "dry_run"] | None = None,
):
    trader = _require_execution_lane(lane)
    status = await asyncio.to_thread(trader.status)
    positions = await asyncio.to_thread(trader.positions, open_only=True)
    mlb_events = {
        event.id: event
        for event in store.events.values()
        if "baseball" in f"{event.sport} {event.league}".casefold()
        or "mlb" in f"{event.sport} {event.league}".casefold()
    }
    state_ready_events = {
        event_id
        for event_id in mlb_events
        if store.states.get(event_id)
        and _baseball_live_state(
            store.states[event_id][-1].period,
            store.states[event_id][-1].clock,
        )
        is not None
    }
    eligible_positions = [
        position
        for position in positions
        if str(position.get("event_id")) in mlb_events
        and line_type(str(position.get("market_type") or ""))
        in {"moneyline", "spread", "total"}
    ]
    eligible_state_positions = [
        position
        for position in eligible_positions
        if str(position.get("event_id")) in state_ready_events
    ]
    adaptive = status.get("adaptive_exit") or {}
    adaptive.update(
        monitored_mlb_events=len(mlb_events),
        live_state_mlb_events=len(state_ready_events),
        eligible_open_positions=len(eligible_positions),
        eligible_state_positions=len(eligible_state_positions),
    )
    if not adaptive.get("enabled"):
        adaptive["collection_note"] = (
            "Adaptive exit observation is disabled."
        )
    elif not eligible_positions:
        adaptive["collection_note"] = (
            "Adaptive exits learn executable movement while a managed MLB "
            "moneyline, run-line, or total position is open; monitoring a game "
            "alone populates the "
            "separate Sport Model Lab."
        )
    elif not eligible_state_positions:
        adaptive["collection_note"] = (
            "An eligible MLB position is open, but no usable inning "
            "state has arrived yet."
        )
    else:
        adaptive["collection_note"] = (
            "Eligible managed MLB positions have usable game state; "
            "observations are retained in 30-second buckets when marked."
        )
    status["adaptive_exit"] = adaptive
    return await _decorate_execution_status(
        status,
        lane=lane,
        trader=trader,
    )


@app.post(
    "/api/polymarket-us/trading/sync",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_sync():
    try:
        return await asyncio.to_thread(
            _require_live_trader().synchronize_live_positions
        )
    except TradingPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        logger.warning("Polymarket US portfolio synchronization failed: %s", exc)
        raise HTTPException(502, f"Portfolio synchronization failed: {exc}") from exc


@app.put(
    "/api/polymarket-us/trading/config",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_config(
    payload: LiveTradingConfigIn,
    lane: Literal["live", "dry_run"] | None = None,
):
    trader = _require_execution_lane(lane)
    try:
        values = payload.model_dump(exclude_none=True)
        if lane is not None:
            values["execution_mode"] = lane
        await asyncio.to_thread(trader.configure, values)
        status = await asyncio.to_thread(trader.status)
        return await _decorate_execution_status(
            status,
            lane=lane,
            trader=trader,
        )
    except TradingPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete(
    "/api/polymarket-us/trading/adaptive-exit/history",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_clear_adaptive_exit_history(
    payload: AdaptiveExitHistoryClearIn,
    lane: Literal["live", "dry_run"] | None = None,
):
    try:
        return await asyncio.to_thread(
            _require_execution_lane(lane).clear_adaptive_exit_history,
            payload.confirmation,
        )
    except TradingPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post(
    "/api/polymarket-us/trading/arm",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_arm(
    payload: LiveTradingArmIn,
    lane: Literal["live", "dry_run"] | None = None,
):
    trader = _require_execution_lane(lane)
    try:
        status = await asyncio.to_thread(
            trader.arm,
            payload.confirmation,
            seconds=payload.seconds,
        )
        return await _decorate_execution_status(
            status,
            lane=lane,
            trader=trader,
        )
    except TradingPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post(
    "/api/polymarket-us/trading/disarm",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_disarm(
    lane: Literal["live", "dry_run"] | None = None,
):
    trader = _require_execution_lane(lane)
    status = await asyncio.to_thread(
        trader.disarm,
        "dashboard",
    )
    return await _decorate_execution_status(
        status,
        lane=lane,
        trader=trader,
    )


@app.post(
    "/api/polymarket-us/trading/stop",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_stop(
    lane: Literal["live", "dry_run"] | None = None,
):
    trader = _require_execution_lane(lane)
    status = await asyncio.to_thread(
        trader.stop_automation,
        "dashboard",
    )
    return await _decorate_execution_status(
        status,
        lane=lane,
        trader=trader,
    )


@app.post(
    "/api/polymarket-us/trading/emergency-stop",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_emergency_stop(
    lane: Literal["live", "dry_run"] | None = None,
):
    trader = _require_execution_lane(lane)
    status = await asyncio.to_thread(
        trader.emergency_stop
    )
    return await _decorate_execution_status(
        status,
        lane=lane,
        trader=trader,
    )


@app.post(
    "/api/polymarket-us/trading/run",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_run(
    lane: Literal["live", "dry_run"] | None = None,
):
    trader = _require_execution_lane(lane)
    try:
        status = await _run_execution_trading_cycle(
            trader
        )
        return await _decorate_execution_status(
            status,
            lane=lane,
            trader=trader,
        )
    except TradingPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        logger.warning("Manual Polymarket US automation cycle failed: %s", exc)
        raise HTTPException(502, f"Automation cycle failed: {exc}") from exc


@app.post(
    "/api/polymarket-us/trading/liquidate",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_liquidate(
    payload: LiveTradingLiquidateIn,
    lane: Literal["live", "dry_run"] | None = None,
):
    if lane is not None and lane != payload.mode:
        raise HTTPException(
            400,
            "The requested automation lane must match the liquidation mode",
        )
    trader = _require_execution_lane(lane)
    try:
        open_positions = await asyncio.to_thread(trader.positions, open_only=True)
        has_target = any(position["mode"] == payload.mode for position in open_positions)
        us_payload = (
            await fetch_polymarket_us_events(limit=500)
            if has_target
            else {"events": []}
        )
        return await asyncio.to_thread(
            trader.liquidate_open_positions,
            us_payload,
            mode=payload.mode,
            confirmation=payload.confirmation,
        )
    except TradingPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        logger.warning("Manual Polymarket US liquidation failed: %s", exc)
        raise HTTPException(502, f"Could not attempt position sales: {exc}") from exc


@app.delete(
    "/api/polymarket-us/trading/history/dry-run",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_clear_dry_run_history(
    payload: DryRunHistoryClearIn,
    lane: Literal["live", "dry_run"] | None = None,
):
    try:
        return await asyncio.to_thread(
            _require_execution_lane(lane).clear_dry_run_history,
            payload.confirmation,
        )
    except TradingPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        logger.warning("Could not clear dry-run trade history: %s", exc)
        raise HTTPException(500, "Could not clear dry-run trade history") from exc


@app.post(
    "/api/polymarket-us/trading/positions/{position_id}/exit",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_exit_position(
    position_id: str,
    payload: LiveTradingPositionExitIn,
):
    try:
        trader = _require_live_trader()
        position = await asyncio.to_thread(trader.position, position_id)
        if position is None and dry_run_trader is not None:
            trader = dry_run_trader
            position = await asyncio.to_thread(trader.position, position_id)
        if position is None:
            raise HTTPException(404, "Managed position was not found")
        if position["mode"] == "live":
            if trader.policy.execution_mode != "live":
                raise HTTPException(
                    409,
                    "Set execution mode to live and save before selling a live position",
                )
            if not trader.is_armed():
                raise HTTPException(
                    409,
                    "Live trading is disarmed; arm the live-order latch first",
                )
            if not approval_granted(payload.confirmation):
                raise HTTPException(
                    409,
                    approval_instruction("sell a live position"),
                )
        us_payload = (
            await fetch_polymarket_us_events(limit=500)
            if position["mode"] == "live" and position["status"] == "open"
            else {"events": []}
        )
        return await asyncio.to_thread(
            trader.exit_position,
            us_payload,
            position_id=position_id,
            confirmation=payload.confirmation,
        )
    except HTTPException:
        raise
    except TradingPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        logger.warning(
            "Could not exit managed Polymarket US position %s: %s",
            position_id,
            exc,
        )
        raise HTTPException(502, "Could not exit managed position") from exc


@app.get(
    "/api/polymarket-us/trading/journal",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_journal(
    limit: int = 100,
    lane: Literal["live", "dry_run"] | None = None,
):
    return await asyncio.to_thread(
        _require_execution_lane(lane).journal,
        limit=limit,
    )


@app.get(
    "/api/polymarket-us/trading/positions",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_positions(
    lane: Literal["live", "dry_run"] | None = None,
):
    return await asyncio.to_thread(_require_execution_lane(lane).positions)


@app.post(
    "/api/polymarket-us/trading/positions/archive-exited",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_archive_exited_positions(
    lane: Literal["live", "dry_run"] | None = None,
):
    return await asyncio.to_thread(
        _require_execution_lane(lane).archive_exited_positions
    )


@app.get(
    "/api/polymarket-us/trading/performance",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_performance(
    lane: Literal["live", "dry_run"] | None = None,
):
    return await asyncio.to_thread(_require_execution_lane(lane).performance)


@app.get(
    "/api/polymarket-us/trading/performance-ledger",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_performance_ledger(
    mode: Literal["all", "dry_run", "live"] = "all",
    market_type: Literal["all", "moneyline", "spread", "total"] = "all",
    result: Literal[
        "all",
        "open",
        "win",
        "loss",
        "push",
        "unverified",
    ] = "all",
    query: str = "",
    format: Literal["json", "csv"] = "json",
    limit: int = 2_000,
    lane: Literal["live", "dry_run"] | None = None,
):
    try:
        ledger_view = await asyncio.to_thread(
            _require_execution_lane(lane).performance_ledger,
            mode=mode,
            market_type=market_type,
            result=result,
            query=query,
            limit=limit,
        )
    except TradingPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc
    if format == "json":
        return ledger_view

    output = io.StringIO(newline="")
    columns = (
        "opened_at",
        "closed_at",
        "mode",
        "result",
        "event_name",
        "market_type",
        "selection",
        "quantity",
        "entry_cost",
        "cost_basis_usd",
        "realized_net_usd",
        "return_fraction",
        "entry_signal_edge",
        "entry_execution_edge",
        "entry_signal_quality",
        "entry_reference_sources",
        "exit_reason",
        "policy_session_id",
        "policy_signature",
        "entry_policy",
    )
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()

    def safe_csv_cell(value: Any) -> Any:
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    for row in ledger_view["rows"]:
        export_row = {key: safe_csv_cell(row.get(key)) for key in columns}
        export_row["entry_policy"] = json.dumps(
            row.get("entry_policy"),
            sort_keys=True,
            separators=(",", ":"),
        )
        writer.writerow(export_row)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="trade-performance-{stamp}.csv"'
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _policy_advisor_model_evidence(
    trader: PolymarketUSAutoTrader,
) -> dict[str, Any]:
    positions = await asyncio.to_thread(
        trader.positions,
        include_hidden=True,
    )
    decision_ids = [
        str(position["entry_decision_id"])
        for position in positions
        if position.get("entry_decision_id")
    ]
    if model_lab is None:
        return {
            "mode": "unavailable",
            "engine_impact": "none",
            "stage": "model_lab_unavailable",
            "live_eligible": False,
            "live_blockers": ["local Sport Model Lab is unavailable"],
            "decision_score_coverage": {
                "requested": len(decision_ids),
                "scored": 0,
            },
        }
    await asyncio.to_thread(model_lab.link_execution_results, positions)
    return await asyncio.to_thread(
        model_lab.advisory_evidence,
        decision_ids,
        sport="baseball",
    )


@app.post(
    "/api/polymarket-us/trading/policy-advisor/recommend",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_policy_advice(
    payload: PolicyAdviceIn,
    lane: Literal["live", "dry_run"] | None = None,
):
    trader = _require_execution_lane(lane)
    try:
        model_evidence = await _policy_advisor_model_evidence(trader)
        return await asyncio.to_thread(
            trader.policy_advice,
            objective=payload.objective,
            target_trades_per_hour=payload.target_trades_per_hour,
            model_evidence=model_evidence,
            analysis_mode=payload.analysis_mode,
            lookback_days=payload.lookback_days,
            market_types=payload.market_types,
        )
    except TradingPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get(
    "/api/polymarket-us/trading/policy-advisor/history",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_policy_advice_history(
    limit: int = 10,
    lane: Literal["live", "dry_run"] | None = None,
):
    return await asyncio.to_thread(
        _require_execution_lane(lane).policy_advice_history,
        limit=limit,
    )


@app.get(
    "/api/polymarket-us/trading/policy-advisor/sessions",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_policy_sessions(
    limit: int = 20,
    lane: Literal["live", "dry_run"] | None = None,
):
    return await asyncio.to_thread(
        _require_execution_lane(lane).policy_sessions,
        limit=limit,
    )


@app.get(
    "/api/polymarket-us/trading/policy-advisor/model-readiness",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_model_readiness(
    lane: Literal["live", "dry_run"] | None = None,
):
    return await _policy_advisor_model_evidence(
        _require_execution_lane(lane)
    )


@app.post(
    "/api/polymarket-us/trading/policy-advisor/{advice_id}/apply",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_apply_policy_advice(
    advice_id: str,
    payload: PolicyAdviceApplyIn,
    lane: Literal["live", "dry_run"] | None = None,
):
    try:
        return await asyncio.to_thread(
            _require_execution_lane(lane).apply_policy_advice,
            advice_id,
            payload.confirmation,
        )
    except TradingPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post(
    "/api/polymarket-us/trading/performance/reset-live",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_reset_live_performance(
    payload: LivePerformanceResetIn,
):
    try:
        return await asyncio.to_thread(
            _require_live_trader().reset_live_performance,
            payload.confirmation,
        )
    except TradingPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post(
    "/api/polymarket-us/trading/risk-session/reset",
    dependencies=[Depends(verify_auth)],
)
async def polymarket_us_trading_reset_risk_session(
    payload: RiskSessionResetIn,
    lane: Literal["live", "dry_run"] | None = None,
):
    try:
        return await asyncio.to_thread(
            _require_execution_lane(lane).reset_risk_session,
            payload.confirmation,
        )
    except TradingPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc


def _require_model_lab() -> SportModelLab:
    if model_lab is None:
        raise HTTPException(404, "The research Model Lab is unavailable")
    return model_lab


@app.get("/api/model-lab/summary", dependencies=[Depends(verify_auth)])
async def model_lab_summary():
    return await asyncio.to_thread(_require_model_lab().summary)


@app.post("/api/model-lab/fit", dependencies=[Depends(verify_auth)])
async def model_lab_fit(payload: ModelLabFitIn):
    return await asyncio.to_thread(
        _require_model_lab().fit_candidate,
        sport=payload.sport,
        league=payload.league,
        market=payload.market,
    )


@app.post("/api/model-lab/export", dependencies=[Depends(verify_auth)])
async def model_lab_export():
    try:
        return await asyncio.to_thread(_require_model_lab().create_export)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get(
    "/api/research-data/export",
    dependencies=[Depends(verify_execution_admin)],
)
async def export_research_data():
    """Download secret-free evidence for a local-to-hosted merge."""
    trader = _require_live_trader()
    lab = _require_model_lab()
    descriptor, archive_path = tempfile.mkstemp(
        prefix="pelositracker-research-",
        suffix=".ndjson.gz",
    )
    os.close(descriptor)
    traders = {"live": trader}
    if dry_run_trader is not None:
        traders["dry_run"] = dry_run_trader
    try:
        await asyncio.to_thread(
            write_research_bundle,
            archive_path,
            traders=traders,
            model_lab=lab,
        )
    except Exception:
        Path(archive_path).unlink(missing_ok=True)
        raise
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    return FileResponse(
        archive_path,
        media_type="application/gzip",
        filename=f"pelositracker-research-{stamp}.ndjson.gz",
        background=BackgroundTask(Path(archive_path).unlink, missing_ok=True),
    )


@app.post(
    "/api/research-data/import",
    dependencies=[Depends(verify_execution_admin)],
)
async def import_research_data(bundle: UploadFile = File(...)):
    """Idempotently merge an exported archive into the central evidence store."""
    trader = _require_live_trader()
    lab = _require_model_lab()
    try:
        return await asyncio.to_thread(
            merge_research_bundle,
            bundle.file,
            trader=trader,
            model_lab=lab,
        )
    except ResearchBundleError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, f"Could not merge research evidence: {exc}") from exc
    finally:
        await bundle.close()


def _sort_events_by_edge():
    events = list(store.events.values())
    def max_edge(event):
        signals = store.signals.get(event.id, [])
        return max((s.edge for s in signals if s.edge is not None), default=0.0)
    events.sort(key=max_edge, reverse=True)
    return events

async def _event_view_or_none(event_id: str, positions: list[dict] | None = None):
    """``event_view`` that yields None instead of 404 when an event is evicted
    between the snapshot and its render. Retention eviction (and manual deletion)
    can drop an event mid-aggregate; a vanished row is simply omitted rather than
    failing the whole list."""
    try:
        return await event_view(event_id, positions)
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise


async def _sorted_event_views() -> list[dict]:
    """Render every tracked event, ordered by edge, sharing a single positions
    query across the fan-out. Fetching positions per event was an N+1 that the
    dashboard (``/api/events`` and every SSE push) paid on each render."""
    events = _sort_events_by_edge()
    positions_by_event: dict[str, list[dict]] = {}
    if ledger is not None:
        positions_by_event = await asyncio.to_thread(
            ledger.event_positions_bulk, [event.id for event in events])
    views = await asyncio.gather(
        *(_event_view_or_none(event.id, positions_by_event.get(event.id, []))
          for event in events))
    return [view for view in views if view is not None]


@app.get("/api/events", dependencies=[Depends(verify_auth)])
async def list_events():
    # GET refreshes and SSE notifications need the same representation. Sharing
    # the already-encoded bytes prevents a browser refresh racing an SSE wake-up
    # from independently materializing and serializing the complete dashboard.
    return Response(content=await _events_snapshot_json(), media_type="application/json")


async def _events_snapshot_json() -> bytes:
    """Build compact events JSON at most once per change across all consumers.

    The version captured on entry is the coalescing token: if the cache already
    holds it, return the shared payload; otherwise one coroutine rebuilds it
    under the lock while the rest reuse it. The cache stores JSON bytes without
    SSE framing so both ``/api/events`` and every stream subscriber share the
    exact same immutable object."""
    target = _snapshot_version
    if _snapshot_cache["version"] == target:
        return _snapshot_cache["payload"]
    async with _snapshot_lock:
        # A newer notification may have arrived while this coroutine waited.
        target = _snapshot_version
        if _snapshot_cache["version"] != target:
            views = await _sorted_event_views()
            payload = json.dumps(
                views, default=str, separators=(",", ":")
            ).encode("utf-8")
            # If state changed while rendering, callers may still use this
            # internally consistent payload, but do not publish it as the cache
            # for the newer version.
            if target == _snapshot_version:
                _snapshot_cache["payload"] = payload
                _snapshot_cache["version"] = target
            return payload
        return _snapshot_cache["payload"]


@app.get("/api/stream", dependencies=[Depends(verify_auth)])
async def stream():
    """Server-Sent Events: push the events snapshot the instant data changes."""
    async def generator():
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        _subscribers.add(queue)
        try:
            # Keep framing as three transport chunks. Concatenating "data: " +
            # a large JSON string would allocate another full-size object per
            # push solely for SSE syntax.
            yield b"data: "
            yield await _events_snapshot_json()  # initial state
            yield b"\n\n"
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=15)
                    # Score and order-book bursts can wake this queue many times
                    # in one browser frame. Coalesce them into one latest-state
                    # snapshot instead of repeatedly allocating the full JSON
                    # tree and replacing the whole dashboard DOM.
                    await asyncio.sleep(0.15)
                    while not queue.empty():
                        queue.get_nowait()
                    yield b"data: "
                    yield await _events_snapshot_json()
                    yield b"\n\n"
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"  # keep the connection warm
        finally:
            _subscribers.discard(queue)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# Internal reproducibility lineage the dashboard never renders. input_snapshot_json
# embeds the full evaluation request (every quote considered) and is serialized once
# per signal per event on every SSE push; left in, the all-events fan-out can allocate
# hundreds of MB per notification and OOM the process (e.g. when a removal triggers a
# rebuild while many events are monitored). It is still persisted to the ledger; it
# just must not ride along in the client snapshot.
_CLIENT_HIDDEN_SIGNAL_FIELDS = {"input_snapshot_json"}


def _signal_views(signals) -> list[dict]:
    # Filter before conversion. Building ``as_json(signal)`` first touched the
    # large reproducibility snapshot solely to throw it away, multiplying peak
    # RAM during every all-events render and especially during removals.
    return [
        {
            key: as_json(getattr(signal, key))
            for key in signal.__dataclass_fields__
            if key not in _CLIENT_HIDDEN_SIGNAL_FIELDS
        }
        for signal in signals
    ]


async def event_view(event_id: str, positions: list[dict] | None = None):
    # Manual deletion removes all four entries under this same lock. Capture
    # references atomically so a concurrent all-events render either sees the
    # complete event or a clean 404; indexing the defaultdicts after deletion
    # used to recreate empty orphan buffers and could also raise midway through
    # a snapshot.
    with store.lock:
        event = store.events.get(event_id)
        if not event:
            raise HTTPException(404, "event not found")
        states = store.states.get(event_id)
        quote_map = store.quotes.get(event_id)
        quotes = list(quote_map.values()) if quote_map is not None else []
        signals = store.signals.get(event_id)
        latest_state = states[-1] if states else None
        state_points = store.state_updates.get(event_id, 0)
        quote_points = store.quote_updates.get(event_id, 0)
    signals = signals if signals is not None else []
    if positions is None:
        positions = (await asyncio.to_thread(ledger.event_positions, event_id)
                     if ledger is not None else [])
    terminal = (
        _terminal_events.get(event_id)
        or ("final" if latest_state is not None and latest_state.ended else None)
        or (_terminal_kind(latest_state.status) if latest_state is not None else None)
    )
    new_entries_allowed = terminal is None and event_id not in _finalized
    return {"event": as_json(event),
            "latest_state": as_json(latest_state),
            "signals": _signal_views(signals),
            "edge_health": edge_health(quotes, signals, engine.max_age_seconds),
            "actionable_markets": market_views(
                quotes,
                signals,
                engine.edge_threshold,
                new_entries_allowed=new_entries_allowed,
                new_entries_blocker=(
                    "New entry blocked: this game is final or no longer active."
                    if not new_entries_allowed else None
                ),
            ),
            "positions": position_views(positions, quotes, signals,
                                          engine.confidence_threshold),
            "state_points": state_points,
            "quote_points": quote_points}


@app.get("/api/events/{event_id}/history", dependencies=[Depends(verify_auth)])
async def get_event_history_api(
    event_id: str, after_ts: float | None = None, limit: int | None = None
):
    if history_db is None:
        raise HTTPException(503, "History database not available")
    if limit is not None and limit <= 0:
        raise HTTPException(400, "limit must be positive")
    # Charting never needs an unbounded event history in one response. Bound
    # both the default and caller-supplied value at the database query so long
    # games cannot hydrate millions of rows into Python and the browser.
    effective_limit = min(limit or 1200, 5000)
    return await asyncio.to_thread(
        history_db.get_event_history,
        event_id,
        after_ts=after_ts,
        limit=effective_limit,
    )


@app.get("/api/events/{event_id}", dependencies=[Depends(verify_auth)])
async def get_event(event_id: str):
    return await event_view(event_id)


@app.post("/api/events", status_code=201, dependencies=[Depends(verify_auth)])
async def add_event(payload: EventIn):
    values = payload.model_dump()
    link_or_slug = payload.polymarket_url or payload.polymarket_slug
    if link_or_slug:
        try:
            slug = extract_polymarket_slug(link_or_slug)
        except Exception as exc:
            raise HTTPException(400, f"Could not parse Polymarket link: {exc}") from exc
        _require_safe_id(slug, "polymarket slug")
        try:
            poly = await polymarket_event(slug)
        except Exception as exc:
            raise HTTPException(400, f"Could not resolve Polymarket link: {exc}") from exc
        if not poly.get("active") or poly.get("closed"):
            raise HTTPException(400, "Polymarket event is not active")
        if settings.exclude_restricted_events and bool(poly.get("restricted", False)):
            raise HTTPException(
                400, "This event is region-restricted and can't be paper-traded; "
                     "set EXCLUDE_RESTRICTED_EVENTS=false to monitor it anyway.")
        actionable = [market for market in poly.get("markets", [])
                      if market.get("active", True) and not market.get("closed", False)
                      and market.get("enableOrderBook", True) and market.get("acceptingOrders", False)
                      and market.get("clobTokenIds")]
        if not actionable:
            raise HTTPException(400, "This event has no markets currently accepting orders")
        inferred = infer_polymarket_event(poly)
        values.update({
            "polymarket_slug": slug,
            "polymarket_url": f"https://polymarket.com/event/{slug}",
            "polymarket_restricted": bool(poly.get("restricted", False)),
            "name": payload.name or inferred["name"],
            "sport": payload.sport or inferred["sport"],
            "home": payload.home or inferred["home"],
            "away": payload.away or inferred["away"],
            "odds_api_sport": payload.odds_api_sport or inferred["odds_api_sport"],
            "game_start": payload.game_start or inferred["game_start"],
        })
        if not game_start_matches_slug(
            values.get("polymarket_slug"), values.get("game_start")
        ):
            raise HTTPException(
                400,
                "Polymarket fixture identity is inconsistent: the dated event "
                "slug does not match the provider game start",
            )
        # We now defer match_odds_api_event to the background polling task so the POST returns instantly.
    required = ("name", "sport", "home", "away")
    missing = [field for field in required if not values.get(field)]
    if missing:
        raise HTTPException(400, f"Missing required fields: {', '.join(missing)}")
    _require_safe_id(values.get("odds_api_sport"), "odds_api_sport")
    _require_safe_id(values.get("odds_api_event_id"), "odds_api_event_id")
    tracked_slug = values.get("polymarket_slug")
    if tracked_slug and any(existing.polymarket_slug == tracked_slug
                            for existing in store.events.values()):
        raise HTTPException(409, "This Polymarket event is already being tracked")
    event = Event(**values)
    try:
        start_time = parse_provider_timestamp(event.game_start)
    except (TypeError, ValueError, OverflowError):
        start_time = None
    canonical = CanonicalEvent.create(event.sport, event.league, start_time,
                                      event.home, event.away)
    event.canonical_event_id = canonical.canonical_event_id
    if monitor_state is not None:
        await asyncio.to_thread(monitor_state.save_event, event)
    if history_db is not None:
        mapping = None
        if event.polymarket_slug:
            mapping = MappingDecision(
                "polymarket", event.polymarket_slug,
                canonical.canonical_event_id if start_time else None,
                MappingStatus.MAPPED if start_time else MappingStatus.QUARANTINED,
                1.0 if start_time else 0.5,
                "event slug and canonical participants/start" if start_time
                else "event start unavailable; provider mapping quarantined",
                orientation="direct",
            )
        await asyncio.to_thread(history_db.log_event_identity, canonical, mapping)
    store.add_event(event)
    _start_event_feeds(event)
    _notify_subscribers()
    return await event_view(event.id)


@app.put("/api/events/{event_id}/positions", dependencies=[Depends(verify_auth)])
async def save_position(event_id: str, payload: PositionIn):
    if event_id not in store.events:
        raise HTTPException(404, "event not found")
    if ledger is None:
        raise HTTPException(503, "position ledger is not ready")
    valid_tokens = {quote.token_id for quote in store.quote_values(event_id)
                    if quote.source.casefold() == "polymarket" and quote.token_id}
    if payload.token_id not in valid_tokens:
        raise HTTPException(400, "That selection is not available for this event")
    await asyncio.to_thread(
        ledger.upsert_position, event_id, payload.token_id, payload.market,
        payload.outcome, payload.shares, payload.avg_entry_price,
    )
    _notify_subscribers()
    return await event_view(event_id)


@app.delete("/api/events/{event_id}/positions/{token_id}", status_code=204, dependencies=[Depends(verify_auth)])
async def remove_position(event_id: str, token_id: str):
    if ledger is None or not await asyncio.to_thread(
        ledger.delete_position, event_id, token_id
    ):
        raise HTTPException(404, "position not found")
    _notify_subscribers()


@app.get("/api/metrics", dependencies=[Depends(verify_auth)])
async def metrics():
    if ledger is None:
        return {"n_bets": 0, "n_settled": 0}
    bet_rows, decisions = await asyncio.gather(
        asyncio.to_thread(ledger.all_bets),
        # Lean projection: summary only counts these rows and reads two fields.
        # all_decisions() SELECT * would load every input_snapshot_json blob into
        # RAM, spiking on each metrics refresh (e.g. right after an event removal).
        asyncio.to_thread(ledger.decision_coverage),
    )
    return await asyncio.to_thread(backtest.summary, bet_rows, decisions)


@app.get("/api/leaderboard", dependencies=[Depends(verify_auth)])
async def get_leaderboard():
    if account_book is None:
        return []
    return await asyncio.to_thread(account_book.leaderboard)


@app.get("/api/model-eval", dependencies=[Depends(verify_auth)])
async def model_eval(sport: str = "tennis"):
    """Calibration + market-baseline scorecard for model-backed paper bets.

    Shadow evaluation only: it reports whether the model's probabilities are
    calibrated and beat the executable price, and makes no profitability claim.
    """
    if account_book is None:
        return {}
    bets = await asyncio.to_thread(account_book.bets_for_eval, sport or None)
    return await asyncio.to_thread(shadow_eval.model_eval_report, bets, sport or None)


@app.post("/api/accounts", status_code=201, dependencies=[Depends(verify_auth)])
async def create_account(payload: StrategyIn):
    if account_book is None:
        raise HTTPException(503, "Account book is not initialized")
    from .accounts import Strategy
    strat = Strategy(
        name=payload.name,
        blurb="Custom bot created via UI.",
        edge_threshold=payload.edge_threshold,
        sizing=payload.sizing,
        kelly_multiplier=payload.kelly_multiplier,
        flat_stake=payload.flat_stake,
        start_bankroll=payload.start_bankroll,
        webhook_url=payload.webhook_url,
        cash_out_enabled=payload.cash_out_enabled,
        events=tuple(dict.fromkeys(e for e in payload.events if e)),
    )
    created = await asyncio.to_thread(account_book.create_custom, strat)
    if not created:
        raise HTTPException(409, "A bot with this name already exists")
    return {"status": "ok"}


@app.get("/api/monitored-games", dependencies=[Depends(verify_auth)])
async def monitored_games():
    """Currently tracked games, for the per-bot allow-list picker."""
    return [
        {"id": event.id, "name": event.name, "sport": event.sport, "league": event.league}
        for event in _sort_events_by_edge()
    ]


@app.get("/api/accounts/{name}/bets", dependencies=[Depends(verify_auth)])
async def get_account_bets(name: str):
    if account_book is None:
        return []
    return await asyncio.to_thread(account_book.account_bets, name)


@app.get("/api/bot-activity", dependencies=[Depends(verify_auth)])
async def get_bot_activity(
    account: str | None = None,
    limit: int = 100,
    per_event_limit: int | None = None,
):
    if account_book is None:
        return []
    return await asyncio.to_thread(
        account_book.activity, account, limit, per_event_limit
    )


@app.get("/api/accounts/{name}/activity", dependencies=[Depends(verify_auth)])
async def get_account_activity(
    name: str,
    limit: int = 100,
    per_event_limit: int | None = None,
):
    if account_book is None:
        return []
    return await asyncio.to_thread(
        account_book.activity, name, limit, per_event_limit
    )


@app.get("/api/accounts/{name}/marks", dependencies=[Depends(verify_auth)])
async def get_account_marks(name: str):
    if account_book is None:
        return []
    return await asyncio.to_thread(account_book.account_marks, name)


@app.patch("/api/accounts/{name}", dependencies=[Depends(verify_auth)])
async def update_account(name: str, payload: StrategyUpdateIn):
    if account_book is None:
        raise HTTPException(503, "Account book is not initialized")
    updated = await asyncio.to_thread(
        account_book.set_cash_out, name, payload.cash_out_enabled
    )
    if not updated:
        raise HTTPException(404, "paper bot not found")
    return {"name": name, "cash_out_enabled": payload.cash_out_enabled}


@app.delete("/api/accounts/{name}", status_code=204, dependencies=[Depends(verify_auth)])
async def delete_account(name: str):
    if account_book is None:
        raise HTTPException(503, "Account book is not initialized")
    result = await asyncio.to_thread(account_book.remove_custom, name)
    if result == "not_found":
        raise HTTPException(404, "paper bot not found")
    if result == "preset":
        raise HTTPException(409, "Preset calibration bots cannot be removed")
    return Response(status_code=204)


@app.get("/api/accounts/{name}/bets/{bet_id}/marks",
         dependencies=[Depends(verify_auth)])
async def get_account_bet_marks(name: str, bet_id: int):
    if account_book is None:
        return []
    return await asyncio.to_thread(account_book.bet_marks, name, bet_id)


@app.get("/api/bets", dependencies=[Depends(verify_auth)])
async def bets(event_id: str | None = None):
    if ledger is None:
        return []
    if event_id:
        return await asyncio.to_thread(ledger.event_bets, event_id)
    return await asyncio.to_thread(ledger.all_bets)


@app.delete("/api/events/{event_id}", status_code=204, dependencies=[Depends(verify_auth)])
async def delete_event(event_id: str):
    deletion = _event_deletions.get(event_id)
    if deletion is None:
        with store.lock:
            if event_id not in store.events:
                # DELETE is idempotent. A browser/network retry after the first
                # request succeeded should not resurrect an error or trigger a
                # new snapshot rebuild.
                return Response(status_code=204)
        deletion = asyncio.create_task(_delete_event_once(event_id))
        _event_deletions[event_id] = deletion

        def finished(task: asyncio.Task) -> None:
            if _event_deletions.get(event_id) is task:
                _event_deletions.pop(event_id, None)
            # Retrieve failures even if the initiating HTTP request disconnected;
            # active waiters still receive the same exception from the task.
            if not task.cancelled():
                task.exception()

        deletion.add_done_callback(finished)

    # A disconnected client must not cancel the shared cleanup. Repeated DELETEs
    # await this same small task rather than queueing full cleanup operations.
    await asyncio.shield(deletion)
    return Response(status_code=204)


async def _delete_event_once(event_id: str) -> None:
    # Manual removal is not evidence of a final result. Keep an event monitored
    # while any fake-money bot position is open so positions cannot disappear
    # into a misleading administrative void/refund.
    lock = _event_lock(event_id)
    feed_tasks: list[asyncio.Task] = []
    removed = False
    async with lock:
        if event_id not in store.events:
            return
        open_positions = (await asyncio.to_thread(account_book.open_count, event_id)
                          if account_book is not None else 0)
        if open_positions > 0:
            raise HTTPException(
                409,
                "This event has open paper-bot positions. Leave it monitored until "
                "they settle or cash out; only a provider cancellation may void them.",
            )
        _terminal_events.setdefault(event_id, "deleted")
        try:
            if ledger is not None:
                await asyncio.to_thread(ledger.delete_event_positions, event_id)
            if monitor_state is not None:
                await asyncio.to_thread(monitor_state.delete_event, event_id)
            feed_tasks = tasks.pop(event_id, [])
            for task in feed_tasks:
                if not task.done():
                    task.cancel()
            _finalized.discard(event_id)
            _finalized_order.pop(event_id, None)
            _pregame.pop(event_id, None)
            event = store.events.get(event_id)
            if event is not None and event.polymarket_slug:
                _sports_status_detail.pop(event.polymarket_slug, None)
            store.remove_event(event_id)
            removed = True
            _notify_subscribers()
        except Exception:
            # Persistence did not complete, so leave the event resident and let
            # its feeds continue. The same first-click path can be retried.
            _terminal_events.pop(event_id, None)
            raise
    if feed_tasks:
        await asyncio.gather(*feed_tasks, return_exceptions=True)
    if removed:
        _terminal_events.pop(event_id, None)
        _event_locks.pop(event_id, None)


