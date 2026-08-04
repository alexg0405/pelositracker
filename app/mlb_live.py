"""Small, official MLB live-state adapter for the local research workstation.

Only the public schedule and linescore endpoints are used.  The adapter does
not produce a probability, edge, recommendation, or order authorization; it
adds the base/out/count state that a later, separately validated MLB residual
model needs.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx

from .http_clients import current_shared_client
from .matching import best_team_pair_match, start_timestamp, team_match_score
from .models import Event, GameState
from .resilience import RetryBackoff
from .telemetry import runtime_telemetry


logger = logging.getLogger(__name__)
MLB_STATS_API = "https://statsapi.mlb.com/api/v1"
MLB_STATE_SCHEMA = "mlb-linescore-v1"


def is_mlb_event(event: Event) -> bool:
    text = f"{event.sport} {event.league} {event.odds_api_sport or ''}".casefold()
    return "baseball" in text or "mlb" in text


@asynccontextmanager
async def _borrow_client() -> AsyncIterator[httpx.AsyncClient]:
    shared = current_shared_client()
    if shared is not None:
        yield shared
        return
    async with httpx.AsyncClient(timeout=15.0) as client:
        yield client


def parse_mlb_schedule(payload: dict) -> list[dict]:
    """Normalize one MLB Stats API schedule payload into plain game rows.

    Pure so the daily-scheduler view can be tested without the network. The
    ``official_date`` is the US-local game date the venue keys slugs on;
    ``gameDate``'s UTC date differs for most evening games.
    """
    games: list[dict] = []
    for date_entry in payload.get("dates") or []:
        if not isinstance(date_entry, dict):
            continue
        for game in date_entry.get("games") or []:
            if not isinstance(game, dict):
                continue
            teams = game.get("teams") or {}

            def team(side: str) -> dict:
                value = (teams.get(side) or {}).get("team")
                return value if isinstance(value, dict) else {}

            away, home = team("away"), team("home")
            away_name = str(away.get("name") or "").strip()
            home_name = str(home.get("name") or "").strip()
            if not away_name or not home_name:
                continue
            status = str(
                (game.get("status") or {}).get("abstractGameState") or ""
            ).strip().casefold()
            games.append({
                "game_id": game.get("gamePk"),
                "away": away_name,
                "home": home_name,
                "away_abbr": str(away.get("abbreviation") or "").strip(),
                "home_abbr": str(home.get("abbreviation") or "").strip(),
                "start": str(game.get("gameDate") or "") or None,
                "official_date": str(
                    game.get("officialDate") or ""
                ) or None,
                # preview | live | final (MLB's abstract states, lowered)
                "status": status or "preview",
            })
    return games


async def fetch_mlb_schedule(day: str | None = None) -> list[dict]:
    """Fetch one US-local day's MLB schedule with team abbreviations.

    Without ``day`` the endpoint returns MLB's own notion of "today", which
    matches how the league keys its game days across US time zones.
    """
    params: dict[str, object] = {"sportId": 1, "hydrate": "team"}
    if day:
        params.update(startDate=day, endDate=day)
    async with _borrow_client() as client:
        response = await client.get(f"{MLB_STATS_API}/schedule", params=params)
        response.raise_for_status()
        return parse_mlb_schedule(response.json())


def _schedule_window(event: Event) -> tuple[str, str]:
    target = start_timestamp(event.game_start)
    center = (
        datetime.fromtimestamp(target, timezone.utc)
        if target is not None
        else datetime.now(timezone.utc)
    )
    return (
        (center - timedelta(days=1)).date().isoformat(),
        (center + timedelta(days=1)).date().isoformat(),
    )


def _schedule_games(payload: dict) -> list[dict]:
    games: list[dict] = []
    for date in payload.get("dates") or []:
        if isinstance(date, dict):
            games.extend(game for game in date.get("games") or [] if isinstance(game, dict))
    return games


def _team_name(game: dict, side: str) -> str:
    team = ((game.get("teams") or {}).get(side) or {}).get("team") or {}
    return str(team.get("name") or "")


def _game_start(game: dict) -> object:
    return game.get("gameDate") or game.get("officialDate")


def match_mlb_game(event: Event, games: list[dict]) -> dict | None:
    """Safely map a tracked event to one MLB game, including doubleheaders."""
    candidates = [
        game
        for game in games
        if (
            team_match_score(event.home, _team_name(game, "home")) is not None
            and team_match_score(event.away, _team_name(game, "away")) is not None
        )
    ]
    return best_team_pair_match(
        candidates,
        event.home,
        event.away,
        lambda game: (_team_name(game, "home"), _team_name(game, "away")),
        event.game_start,
        _game_start,
        tolerance_seconds=6 * 3600,
    )


async def resolve_mlb_game(event: Event, client: httpx.AsyncClient) -> dict | None:
    start, end = _schedule_window(event)
    response = await client.get(
        f"{MLB_STATS_API}/schedule",
        params={"sportId": 1, "startDate": start, "endDate": end},
    )
    response.raise_for_status()
    return match_mlb_game(event, _schedule_games(response.json()))


def _person(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    identifier = payload.get("id")
    name = payload.get("fullName") or payload.get("name")
    if identifier is None and not name:
        return None
    return {"id": identifier, "name": name}


def game_state_from_linescore(
    event: Event,
    game: dict,
    payload: dict,
    *,
    received_at: datetime | None = None,
) -> GameState | None:
    """Parse the low-volume MLB linescore contract without guessing missing state."""
    inning_value = payload.get("currentInning")
    try:
        inning = int(inning_value)
    except (TypeError, ValueError):
        return None
    raw_half = str(payload.get("inningState") or "").strip().casefold()
    if raw_half.startswith("top"):
        half = "top"
    elif raw_half.startswith(("bottom", "bot")):
        half = "bottom"
    elif raw_half.startswith(("middle", "end")):
        half = "end"
    else:
        return None

    teams = payload.get("teams") or {}
    try:
        home_score = float((teams.get("home") or {}).get("runs"))
        away_score = float((teams.get("away") or {}).get("runs"))
    except (TypeError, ValueError):
        return None
    now = received_at or datetime.now(timezone.utc)
    offense = payload.get("offense") or {}
    defense = payload.get("defense") or {}

    def bounded_int(name: str, minimum: int, maximum: int) -> int | None:
        try:
            value = int(payload.get(name))
        except (TypeError, ValueError):
            return None
        return value if minimum <= value <= maximum else None

    outs = bounded_int("outs", 0, 3)
    balls = bounded_int("balls", 0, 4)
    strikes = bounded_int("strikes", 0, 3)
    runners = {
        "first": _person(offense.get("first")),
        "second": _person(offense.get("second")),
        "third": _person(offense.get("third")),
    }
    base_mask = sum(
        bit for base, bit in (("first", 1), ("second", 2), ("third", 4))
        if runners[base] is not None
    )
    batting_side = "away" if half == "top" else "home" if half == "bottom" else None
    scheduled_innings = payload.get("scheduledInnings")
    try:
        scheduled_innings = int(scheduled_innings)
    except (TypeError, ValueError):
        scheduled_innings = None
    provider_game_id = str(game.get("gamePk") or "") or None
    home_team = ((game.get("teams") or {}).get("home") or {}).get("team") or {}
    away_team = ((game.get("teams") or {}).get("away") or {}).get("team") or {}
    game_status = game.get("status") or {}
    abstract_status = str(
        game_status.get("abstractGameState") or ""
    ).strip().casefold()
    detailed_status = str(
        game_status.get("detailedState") or ""
    ).strip().casefold()
    ended = (
        abstract_status == "final"
        or detailed_status
        in {
            "final",
            "game over",
            "completed early",
        }
    )
    state_payload = {
        "schema": MLB_STATE_SCHEMA,
        "inning": inning,
        "half": half,
        "outs": outs,
        "balls": balls,
        "strikes": strikes,
        "base_mask": base_mask,
        "runners": runners,
        "batting_side": batting_side,
        "batter": _person(offense.get("batter")),
        "pitcher": _person(defense.get("pitcher")),
        "scheduled_innings": scheduled_innings,
        "official_game_id": provider_game_id,
        "official_game_status": (
            detailed_status or abstract_status or None
        ),
        "ended": ended,
    }
    state_hash = hashlib.sha256(
        json.dumps(state_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    period = (
        f"{'Top' if half == 'top' else 'Bottom'} {inning}"
        if half in {"top", "bottom"}
        else f"End {inning}"
    )
    clock_bits = []
    if outs is not None:
        clock_bits.append(f"{outs} out")
    if balls is not None and strikes is not None:
        clock_bits.append(f"{balls}-{strikes}")
    return GameState(
        event_id=event.id,
        home_score=home_score,
        away_score=away_score,
        period=period,
        clock=" · ".join(clock_bits),
        source="MLB Stats API",
        possession=(
            event.away if batting_side == "away"
            else event.home if batting_side == "home"
            else None
        ),
        status="final" if ended else "in_progress",
        received_at=now,
        processed_at=datetime.now(timezone.utc),
        provider_event_id=provider_game_id,
        canonical_event_id=event.canonical_event_id,
        league_id="mlb",
        sport_id="baseball",
        home_team_id=str(home_team.get("id") or "") or None,
        away_team_id=str(away_team.get("id") or "") or None,
        regulation_period=inning,
        overtime_number=max(0, inning - 9) or None,
        live=not ended,
        ended=ended,
        state_hash=state_hash,
        state_schema_version=MLB_STATE_SCHEMA,
        sport_state=state_payload,
    )


async def mlb_linescore_poll(
    event: Event,
    emit: Callable[[GameState], Awaitable[None]],
    *,
    interval_seconds: float = 5.0,
) -> None:
    """Poll one monitored MLB event; schedule identity is cached per task."""
    if not is_mlb_event(event):
        return
    backoff = RetryBackoff(base_seconds=2, cap_seconds=60)
    game: dict | None = None
    last_hash: str | None = None
    last_resolve = 0.0
    last_terminal_status_refresh = 0.0
    while True:
        try:
            async with _borrow_client() as client:
                now_epoch = datetime.now(timezone.utc).timestamp()
                if game is None or now_epoch - last_resolve >= 15 * 60:
                    game = await resolve_mlb_game(event, client)
                    last_resolve = now_epoch
                if game is None:
                    await asyncio.sleep(max(10.0, interval_seconds))
                    continue
                game_id = str(game.get("gamePk") or "")
                if not game_id:
                    game = None
                    continue
                response = await client.get(f"{MLB_STATS_API}/game/{game_id}/linescore")
                if response.status_code == 404:
                    game = None
                    await asyncio.sleep(max(10.0, interval_seconds))
                    continue
                response.raise_for_status()
                linescore = response.json()
                # The schedule response used to resolve identity is cached for
                # 15 minutes. Near the end of regulation, refresh that compact
                # response so an official Final status is not hidden behind a
                # stale In Progress value while the score itself is current.
                # This is deliberately limited to end-of-game innings rather
                # than adding another request to every live-state cycle.
                try:
                    inning = int(linescore.get("currentInning"))
                except (AttributeError, TypeError, ValueError):
                    inning = 0
                inning_state = str(
                    linescore.get("inningState") if isinstance(linescore, dict) else ""
                ).strip().casefold()
                if (
                    inning >= 9
                    and inning_state.startswith(("middle", "end"))
                    and now_epoch - last_terminal_status_refresh >= 15.0
                ):
                    last_terminal_status_refresh = now_epoch
                    refreshed = await resolve_mlb_game(event, client)
                    if refreshed is not None:
                        game = refreshed
                        last_resolve = now_epoch
                state = game_state_from_linescore(event, game, linescore)
                if state is not None and state.state_hash != last_hash:
                    last_hash = state.state_hash
                    await emit(state)
                    runtime_telemetry.increment("mlb_live_states")
                backoff.reset()
            await asyncio.sleep(max(2.0, interval_seconds))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime_telemetry.increment(f"mlb_live_error_{type(exc).__name__}")
            logger.debug("MLB live-state poll failed for %s: %s", event.id, exc)
            await asyncio.sleep(backoff.next_delay())
