"""Optional Pinnacle/Arcadia guest-feed adapter.

This undocumented contract is disabled by default. Operators must supply their
own credential and explicitly enable it; no availability or rate limit is assumed.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from .models import Event, Quote
from .matching import best_team_pair_match
from .domain.time import parse_provider_timestamp

logger = logging.getLogger(__name__)

_API_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"

# Map from our internal odds_api_sport keys to Pinnacle league IDs
_SPORT_TO_LEAGUE: dict[str, int] = {
    "baseball_mlb": 246,
    "basketball_wnba": 578,
    "basketball_nba": 487,
    "americanfootball_nfl": 889,
    "icehockey_nhl": 1456,
    "soccer_usa_mls": 2627,
    "soccer_epl": 1980,
}


def _implied_prob(american: int | float | None) -> float:
    """Convert American odds to implied probability."""
    if american is None:
        return 0.0
    if american > 0:
        return 100.0 / (american + 100.0)
    else:
        return -american / (-american + 100.0)


def _match_pinnacle_game(event: Event, matchups: list[dict]) -> dict | None:
    """Match both normalized team identities, then disambiguate by start time."""
    eligible = [matchup for matchup in matchups
                if matchup.get("type") == "matchup" and not matchup.get("special")]
    return best_team_pair_match(
        eligible,
        event.home,
        event.away,
        lambda matchup: [participant.get("name", "")
                          for participant in matchup.get("participants", [])],
        event.game_start,
        lambda matchup: matchup.get("startTime"),
    )


def _parse_pinnacle_quotes(event: Event, matchup: dict, markets: list[dict]) -> list[Quote]:
    """Convert Pinnacle market data into Quote objects."""
    quotes: list[Quote] = []

    for mk in markets:
        first_new_quote = len(quotes)
        mk_type = mk.get("type", "")
        prices = mk.get("prices", [])
        key = mk.get("key", "")

        # Only process full-game markets (key starts with s;0;)
        # s;0; = full game, s;1; = first half, s;3; = first period/inning
        if not key.startswith("s;0;"):
            continue

        if mk_type == "moneyline":
            for p in prices:
                desig = str(p.get("designation", "")).casefold()
                price = p.get("price")
                if price is None:
                    continue
                if desig == "home":
                    quotes.append(Quote(
                        event_id=event.id,
                        market="moneyline",
                        outcome=event.home,
                        probability=_implied_prob(price),
                        source="pinnacle",
                    ))
                elif desig == "away":
                    quotes.append(Quote(
                        event_id=event.id,
                        market="moneyline",
                        outcome=event.away,
                        probability=_implied_prob(price),
                        source="pinnacle",
                    ))
                elif desig in {"draw", "tie"}:
                    quotes.append(Quote(
                        event_id=event.id,
                        market="moneyline",
                        outcome="Draw",
                        probability=_implied_prob(price),
                        source="pinnacle",
                    ))

        elif mk_type == "spread":
            for p in prices:
                desig = p.get("designation", "")
                price = p.get("price")
                points = p.get("points")
                if price is None or points is None:
                    continue
                if desig == "home":
                    sign = "+" if points >= 0 else ""
                    quotes.append(Quote(
                        event_id=event.id,
                        market="spread",
                        outcome=f"{event.home} {sign}{points:g}",
                        probability=_implied_prob(price),
                        source="pinnacle",
                    ))
                elif desig == "away":
                    sign = "+" if points >= 0 else ""
                    quotes.append(Quote(
                        event_id=event.id,
                        market="spread",
                        outcome=f"{event.away} {sign}{points:g}",
                        probability=_implied_prob(price),
                        source="pinnacle",
                    ))

        elif mk_type == "total":
            # Skip team totals
            if "tt;" in key:
                continue
            for p in prices:
                desig = p.get("designation", "")
                price = p.get("price")
                points = p.get("points")
                if price is None or points is None:
                    continue
                if desig == "over":
                    quotes.append(Quote(
                        event_id=event.id,
                        market="total",
                        outcome=f"Over {points:g}",
                        probability=_implied_prob(price),
                        source="pinnacle",
                    ))
                elif desig == "under":
                    quotes.append(Quote(
                        event_id=event.id,
                        market="total",
                        outcome=f"Under {points:g}",
                        probability=_implied_prob(price),
                        source="pinnacle",
                    ))

        raw_time = mk.get("lastUpdated") or mk.get("updatedAt") \
            or matchup.get("lastUpdated") or matchup.get("updatedAt")
        try:
            provider_time = parse_provider_timestamp(raw_time)
        except (TypeError, ValueError, OverflowError):
            provider_time = None
        processed_at = datetime.now(timezone.utc)
        for quote in quotes[first_new_quote:]:
            quote.provider_timestamp = provider_time
            quote.observed_at = provider_time or quote.received_at
            quote.processed_at = processed_at
            quote.timestamp_trusted = provider_time is not None

    return quotes


async def pinnacle_poll(event: Event, emit: Callable[[list[Quote]], Awaitable[None]],
                        *, api_key: str):
    """Poll Pinnacle for sharp odds on a tracked event."""
    if not event.odds_api_sport:
        return

    league_id = _SPORT_TO_LEAGUE.get(event.odds_api_sport)
    if league_id is None:
        logger.warning(f"No Pinnacle league mapping for sport: {event.odds_api_sport}")
        return

    if not api_key:
        raise ValueError("Pinnacle guest adapter requires an explicit API key")
    headers = {"x-api-key": api_key}
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                # Step 1: Get matchups for the league
                r = await client.get(
                    f"{_API_BASE}/leagues/{league_id}/matchups",
                    headers=headers,
                    params={"brandId": "0"},
                )
                if r.status_code != 200:
                    logger.warning(f"Pinnacle matchups status {r.status_code} for {event.name}")
                    await asyncio.sleep(60)
                    continue

                matchups = r.json()
                matched = _match_pinnacle_game(event, matchups)
                if not matched:
                    logger.debug(f"No Pinnacle match for {event.name}")
                    await asyncio.sleep(60)
                    continue

                matchup_id = matched.get("id")

                # Step 2: Get markets/odds for this matchup
                r2 = await client.get(
                    f"{_API_BASE}/matchups/{matchup_id}/markets/related/straight",
                    headers=headers,
                    params={"primaryOnly": "true"},
                )
                if r2.status_code != 200:
                    logger.warning(f"Pinnacle markets status {r2.status_code} for {event.name}")
                    await asyncio.sleep(60)
                    continue

                markets = r2.json()
                quotes = _parse_pinnacle_quotes(event, matched, markets)
                if quotes:
                    logger.info(f"Pinnacle: {len(quotes)} quotes for {event.name}")
                    await emit(quotes)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pinnacle poll error for {event.name}: {e}")

            await asyncio.sleep(20)  # Keep quotes fresh within max_age window
