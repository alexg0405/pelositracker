"""Read-only Polymarket US sidecar for the local research workstation.

This module deliberately does not expose order creation, modification, cancellation,
or close-position methods. Public sports inventory comes from the unauthenticated US
gateway. Optional API credentials are used only to read balances and positions.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from collections.abc import Iterable, Mapping
from urllib.parse import quote

import httpx

from .lines import (
    MLB_FIRST_FIVE_SCOPE,
    MLB_FIRST_INNING_SCOPE,
    base_market_type,
    canonical_scoped_market,
    market_scope,
)
from .models import Event, Quote

PUBLIC_BASE_URL = "https://gateway.polymarket.us"
AUTHENTICATED_BASE_URL = "https://api.polymarket.us"
MAX_PUBLIC_PAGES = 5
PUBLIC_PAGE_SIZE = 100


class PolymarketUSResearchError(RuntimeError):
    """A bounded, user-safe failure from the research sidecar."""


def credential_status(key_id: str, secret_key: str) -> dict[str, Any]:
    """Describe credential availability without returning either credential."""
    configured = bool(key_id and secret_key)
    if not key_id:
        hint = None
    elif len(key_id) <= 8:
        hint = f"{key_id[:2]}...{key_id[-2:]}"
    else:
        hint = f"{key_id[:6]}...{key_id[-4:]}"
    return {
        "configured": configured,
        "key_id_hint": hint,
        "account_access": "read-only",
        "trading_enabled": False,
    }


def _safe_transport_error(exc: BaseException) -> str:
    """Describe a gateway failure without echoing a URL, body, or header."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, PolymarketUSResearchError):
        return str(exc)
    return type(exc).__name__


def _amount(value: Any) -> float | None:
    if isinstance(value, Mapping):
        value = value.get("value")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _sports_market(market: Mapping[str, Any]) -> bool:
    market_type = str(
        market.get("sportsMarketType")
        or market.get("sportsMarketTypeV2")
        or ""
    ).upper()
    if market_type:
        return True
    return str(market.get("category") or "").casefold() == "sports"


def _sports_event(event: Mapping[str, Any]) -> bool:
    if str(event.get("category") or "").strip().casefold() == "sports":
        return True
    tags = event.get("tags")
    if not isinstance(tags, list):
        return False
    return any(
        isinstance(tag, Mapping)
        and (
            str(tag.get("slug") or "").strip().casefold() == "sports"
            or str(tag.get("label") or "").strip().casefold() == "sports"
        )
        for tag in tags
    )


def _market_type(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    normalized = normalized.removeprefix("SPORTS_MARKET_TYPE_")
    return normalized.casefold() or "sports"


def _normalize_side(side: Mapping[str, Any]) -> dict[str, Any]:
    team = side.get("team") if isinstance(side.get("team"), Mapping) else {}
    return {
        "id": str(side.get("id") or ""),
        "identifier": str(side.get("identifier") or ""),
        # Polymarket US commonly exposes soccer outcomes as separate binary
        # Yes/No markets.  In that representation ``description`` is only
        # "Yes" or "No" and the actual selection identity lives on ``team``.
        # Preserve both fields so execution can match the outcome exactly.
        "team_name": str(team.get("name") or ""),
        "description": str(
            side.get("description")
            or team.get("name")
            or side.get("identifier")
            or "Outcome"
        ),
        "long": bool(side.get("long")),
        "tradable": bool(side.get("tradable", True)),
        "reference_price": _amount(side.get("quote") or side.get("price")),
    }


def _normalize_market(market: Mapping[str, Any]) -> dict[str, Any]:
    raw_sides = market.get("marketSides")
    sides = raw_sides if isinstance(raw_sides, list) else []
    raw_market_type = (
        market.get("sportsMarketType")
        or market.get("sportsMarketTypeV2")
    )
    canonical_market = canonical_scoped_market(str(raw_market_type or ""))
    line = _amount(market.get("line"))
    if (
        market_scope(canonical_market) == MLB_FIRST_INNING_SCOPE
        and base_market_type(canonical_market) == "total"
        and line is None
    ):
        line = 0.5
    return {
        "id": str(market.get("id") or ""),
        "slug": str(market.get("slug") or ""),
        "question": str(
            market.get("question")
            or market.get("title")
            or market.get("slug")
            or "Market"
        ),
        "market_type": _market_type(
            raw_market_type
        ),
        "market_type_v2": _market_type(
            market.get("sportsMarketTypeV2") or market.get("sportsMarketType")
        ),
        "canonical_market": canonical_market,
        "market_scope": market_scope(canonical_market),
        "line": line,
        "active": bool(market.get("active", True)),
        "closed": bool(market.get("closed", False)),
        "hidden": bool(market.get("hidden", False)),
        "state": str(
            market.get("ep3Status")
            or market.get("state")
            or "OPEN"
        ),
        "long_best_bid": _amount(market.get("bestBidQuote") or market.get("bestBid")),
        "long_best_ask": _amount(market.get("bestAskQuote") or market.get("bestAsk")),
        "minimum_trade_quantity": _amount(market.get("minimumTradeQty")),
        "minimum_tick_size": _amount(market.get("orderPriceMinTickSize")),
        "game_start": market.get("gameStartTime") or market.get("startDate"),
        "sides": [_normalize_side(side) for side in sides if isinstance(side, Mapping)],
    }


def normalize_sports_events(
    payload: Mapping[str, Any],
    *,
    limit: int = 60,
) -> list[dict[str, Any]]:
    """Reduce the large public response to the fields the workstation renders."""
    raw_events = payload.get("events")
    events = raw_events if isinstance(raw_events, list) else []
    normalized: list[dict[str, Any]] = []
    for event in events:
        if (
            not isinstance(event, Mapping)
            or not _sports_event(event)
            or bool(event.get("ended", False))
        ):
            continue
        raw_markets = event.get("markets")
        markets = raw_markets if isinstance(raw_markets, list) else []
        active_markets = [
            _normalize_market(market)
            for market in markets
            if (
                isinstance(market, Mapping)
                and _sports_market(market)
                and bool(market.get("active", True))
                and not bool(market.get("closed", False))
                and not bool(market.get("archived", False))
                and not bool(market.get("hidden", False))
                and str(
                    market.get("ep3Status") or market.get("state") or "OPEN"
                ).upper() in {"OPEN", "MARKET_STATE_OPEN", "EP3_STATUS_OPEN"}
            )
        ]
        if not active_markets:
            continue
        start = (
            event.get("startTime")
            or event.get("eventDate")
            or event.get("startDate")
            or active_markets[0].get("game_start")
        )
        normalized.append({
            "id": str(event.get("id") or ""),
            "slug": str(event.get("slug") or ""),
            "title": str(event.get("title") or event.get("slug") or "Sports event"),
            "subtitle": str(event.get("subtitle") or ""),
            "start": start,
            "live": bool(event.get("live", False)),
            "ended": bool(event.get("ended", False)),
            "score": str(event.get("score") or ""),
            "period": str(event.get("period") or ""),
            "category": str(event.get("category") or "sports"),
            "markets": active_markets,
        })

    def sort_key(event: Mapping[str, Any]) -> tuple[int, str]:
        return (0 if event.get("live") else 1, str(event.get("start") or "9999"))

    normalized.sort(key=sort_key)
    return normalized[:max(1, min(limit, 100))]


def public_market_quotes(
    event: Event,
    us_event: Mapping[str, Any],
) -> list[Quote]:
    """Translate exact US period books into inputs for the unchanged engine.

    Public best bid/ask is sufficient to price a research candidate, but it is
    deliberately marked as incomplete execution depth with unknown fees. The
    auto trader still re-reads the authenticated order book before every fill.
    """
    now = datetime.now(timezone.utc)
    quotes: list[Quote] = []
    for market in us_event.get("markets", []):
        if not isinstance(market, Mapping):
            continue
        canonical = str(
            market.get("canonical_market")
            or canonical_scoped_market(str(market.get("market_type") or ""))
        )
        scope = market_scope(canonical)
        kind = base_market_type(canonical)
        if scope not in {MLB_FIRST_INNING_SCOPE, MLB_FIRST_FIVE_SCOPE}:
            continue
        long_bid = _amount(market.get("long_best_bid"))
        long_ask = _amount(market.get("long_best_ask"))
        if (
            long_bid is None
            or long_ask is None
            or not 0 < long_bid <= long_ask < 1
        ):
            continue
        sides = [
            side for side in market.get("sides", [])
            if isinstance(side, Mapping) and side.get("tradable", True)
        ]
        long_side = next(
            (side for side in sides if bool(side.get("long"))),
            None,
        )
        short_side = next(
            (side for side in sides if not bool(side.get("long"))),
            None,
        )
        if long_side is None or short_side is None:
            continue
        line = _amount(market.get("line"))
        if scope == MLB_FIRST_INNING_SCOPE and kind == "total" and line is None:
            line = 0.5

        selections: list[tuple[Mapping[str, Any], str, float, float]] = []
        if kind == "moneyline" and scope == MLB_FIRST_FIVE_SCOPE:
            team = str(long_side.get("team_name") or "").strip()
            question = str(market.get("question") or "")
            if not team and any(
                word in question.casefold() for word in (" tie", "draw")
            ):
                team = "Draw"
            if team:
                selections.append((long_side, team, long_bid, long_ask))
        elif kind == "spread" and scope == MLB_FIRST_FIVE_SCOPE and line is not None:
            team = str(long_side.get("team_name") or "").strip()
            if team:
                selections.append(
                    (long_side, f"{team} {line:+g}", long_bid, long_ask)
                )
        elif (
            kind == "total"
            and scope in {MLB_FIRST_INNING_SCOPE, MLB_FIRST_FIVE_SCOPE}
            and line is not None
        ):
            selections.extend((
                (long_side, f"Over {line:g}", long_bid, long_ask),
                (short_side, f"Under {line:g}", 1.0 - long_ask, 1.0 - long_bid),
            ))

        for side, outcome, bid, ask in selections:
            quotes.append(Quote(
                event_id=event.id,
                market=canonical,
                outcome=outcome,
                probability=(bid + ask) / 2.0,
                source="Polymarket",
                bid=bid,
                ask=ask,
                token_id=str(side.get("id") or "") or None,
                market_slug=str(market.get("slug") or "") or None,
                question=str(market.get("question") or "") or None,
                min_order_size=_amount(market.get("minimum_trade_quantity")),
                tick_size=_amount(market.get("minimum_tick_size")),
                accepting_orders=True,
                received_at=now,
                processed_at=now,
                depth_complete=False,
                provider_event_id=str(us_event.get("id") or "") or None,
                canonical_event_id=event.canonical_event_id,
                provider_market_id=str(market.get("id") or "") or None,
                market_scope=scope,
                line=line,
                outcome_id=str(side.get("id") or outcome),
                active=bool(market.get("active", True)),
                resolved=bool(market.get("closed", False)),
            ))
    return quotes


async def fetch_public_sports_events(*, limit: int = 60) -> dict[str, Any]:
    """Fetch active US events, paging only until enough sports inventory is found."""
    wanted = max(1, min(limit, MAX_PUBLIC_PAGES * PUBLIC_PAGE_SIZE))
    gathered: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        for page in range(MAX_PUBLIC_PAGES):
            response = await client.get(
                f"{PUBLIC_BASE_URL}/v1/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "archived": "false",
                    "limit": str(PUBLIC_PAGE_SIZE),
                    "offset": str(page * PUBLIC_PAGE_SIZE),
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise PolymarketUSResearchError("Polymarket US returned an invalid event payload")
            page_events = normalize_sports_events(payload, limit=100)
            gathered.extend(page_events)
            raw_events = payload.get("events")
            if len(gathered) >= wanted or not isinstance(raw_events, list) or not raw_events:
                break
    unique: dict[str, dict[str, Any]] = {}
    for event in gathered:
        key = event.get("id") or event.get("slug")
        if key:
            unique[str(key)] = event
    events = list(unique.values())
    events.sort(key=lambda event: (
        0 if event.get("live") else 1,
        str(event.get("start") or "9999"),
    ))
    return {
        "source": PUBLIC_BASE_URL,
        "venue": "Polymarket US",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "events": events[:wanted],
    }


async def fetch_public_sports_events_by_slugs(
    slugs: Iterable[str],
) -> dict[str, Any]:
    """Fetch monitored US events directly instead of relying on list position.

    The general event inventory contains hundreds of futures and unrelated
    sports. A live game can therefore move beyond the bounded pagination window
    used by the research browser. Monitored Polymarket event slugs are stable
    identities, and the public gateway exposes an exact event-by-slug endpoint,
    so execution should use that endpoint rather than infer absence from a
    partial global list.

    One slug per monitored event means one transport failure per monitored
    event. A single transient gateway error must not deny the caller every
    other event's prices, because the execution cycle that consumes this
    payload also marks open positions and evaluates protective exits. Failures
    are therefore reported per slug in ``resolution`` and the caller decides
    whether the remaining coverage is good enough.
    """
    requested = tuple(dict.fromkeys(
        str(slug or "").strip()
        for slug in slugs
        if str(slug or "").strip()
    ))
    if not requested:
        return {
            "source": PUBLIC_BASE_URL,
            "venue": "Polymarket US",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "events": [],
            "resolution": {
                "requested": 0,
                "resolved": 0,
                "not_found": (),
                "failed": {},
            },
        }

    semaphore = asyncio.Semaphore(8)

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        async def fetch_one(slug: str) -> dict[str, Any] | None:
            async with semaphore:
                response = await client.get(
                    f"{PUBLIC_BASE_URL}/v1/events/slug/{quote(slug, safe='')}"
                )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise PolymarketUSResearchError(
                    "Polymarket US returned an invalid event-by-slug payload"
                )
            raw_event = payload.get("event")
            if not isinstance(raw_event, Mapping):
                raise PolymarketUSResearchError(
                    "Polymarket US event-by-slug response omitted the event"
                )
            normalized = normalize_sports_events(
                {"events": [raw_event]},
                limit=1,
            )
            return normalized[0] if normalized else None

        fetched = await asyncio.gather(
            *(fetch_one(slug) for slug in requested),
            return_exceptions=True,
        )

    events: list[dict[str, Any]] = []
    not_found: list[str] = []
    failed: dict[str, str] = {}
    for slug, outcome in zip(requested, fetched):
        if isinstance(outcome, BaseException):
            failed[slug] = _safe_transport_error(outcome)
        elif outcome is None:
            not_found.append(slug)
        else:
            events.append(outcome)
    events.sort(key=lambda event: (
        0 if event.get("live") else 1,
        str(event.get("start") or "9999"),
    ))
    return {
        "source": PUBLIC_BASE_URL,
        "venue": "Polymarket US",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "events": events,
        "resolution": {
            "requested": len(requested),
            "resolved": len(events),
            "not_found": tuple(not_found),
            "failed": failed,
        },
    }


def _account_snapshot_sync(key_id: str, secret_key: str) -> dict[str, Any]:
    try:
        from polymarket_us import PolymarketUS
    except ImportError as exc:  # pragma: no cover - incomplete local installation
        raise PolymarketUSResearchError(
            "The official polymarket-us SDK is not installed; rerun setup-workstation.cmd"
        ) from exc

    try:
        with PolymarketUS(
            key_id=key_id,
            secret_key=secret_key,
            timeout=15.0,
        ) as client:
            # Read operations only. This module intentionally never calls any order
            # creation, cancellation, modification, preview, or close-position method.
            balances = client.account.balances()
            positions = client.portfolio.positions({"limit": 100})
    except Exception as exc:
        message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
        safe = str(message)
        for credential in (key_id, secret_key):
            if credential:
                safe = safe.replace(credential, "[redacted]")
        raise PolymarketUSResearchError(safe[:500]) from exc

    return {
        "venue": "Polymarket US",
        "mode": "read-only",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "balances": balances.get("balances", []) if isinstance(balances, Mapping) else [],
        "positions": positions.get("positions", {}) if isinstance(positions, Mapping) else {},
        "positions_eof": (
            bool(positions.get("eof", True)) if isinstance(positions, Mapping) else True
        ),
    }


async def fetch_account_snapshot(key_id: str, secret_key: str) -> dict[str, Any]:
    if not key_id or not secret_key:
        raise PolymarketUSResearchError(
            "Add POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY to .env, then restart"
        )
    return await asyncio.to_thread(_account_snapshot_sync, key_id, secret_key)
