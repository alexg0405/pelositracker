import asyncio
import logging
import re as _re
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from .models import Event, Quote
from .matching import best_team_pair_match
from .domain.time import parse_provider_timestamp

logger = logging.getLogger(__name__)

_book_map = {}

_SCOREBOARD_SPORTS = {
    "americanfootball_nfl": "nfl",
    "americanfootball_ncaaf": "ncaaf",
    "baseball_mlb": "mlb",
    "basketball_nba": "nba",
    "basketball_wnba": "wnba",
    "basketball_ncaab": "ncaab",
    "icehockey_nhl": "nhl",
    "mma_mixed_martial_arts": "ufc",
}


def scoreboard_sport(provider_sport: str) -> str:
    return _SCOREBOARD_SPORTS.get(provider_sport, provider_sport.split("_")[-1])

def implied_probability(american: int | None) -> float:
    if american is None:
        return 0.0
    if american > 0:
        return 100.0 / (american + 100.0)
    else:
        return -american / (-american + 100.0)

async def _ensure_books(client: httpx.AsyncClient):
    if not _book_map:
        try:
            r = await client.get("https://api.actionnetwork.com/web/v1/books", headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                for b in r.json().get("books", []):
                    _book_map[b["id"]] = b["display_name"]
        except Exception as e:
            logger.error(f"Failed to fetch Action Network books: {e}")


_STATE_SUFFIX = _re.compile(r"\s+[A-Z]{2}$")  # " NJ", " PA", " WY", etc.


def _clean_book_name(raw: str) -> str:
    """Normalize 'BetMGM NJ' / 'FanDuel PA' -> 'betmgm' / 'fanduel'."""
    cleaned = _STATE_SUFFIX.sub("", raw).strip()
    return cleaned.lower()


def parse_action_quotes(event: Event, game: dict) -> list[Quote]:
    quotes = []
    seen_sources: set[str] = set()   # dedupe across state variants
    for odds in game.get("odds", []):
        # Only use full-game odds, skip first-half/first-inning/live duplicates
        odds_type = odds.get("type", "game")
        if odds_type != "game":
            continue

        book_id = odds.get("book_id")
        book_name = _book_map.get(book_id)
        if not book_name:
            continue
        
        source = _clean_book_name(book_name)
        if "consensus" in source or "open" in source:
            continue
        if source in seen_sources:
            continue  # already got this book via another state variant
        seen_sources.add(source)
            
        ml_home = odds.get("ml_home")
        if ml_home is not None and ml_home != 0:
            quotes.append(Quote(
                event_id=event.id,
                market="moneyline",
                outcome=event.home,
                probability=implied_probability(ml_home),
                source=source,
            ))
            
        ml_away = odds.get("ml_away")
        if ml_away is not None and ml_away != 0:
            quotes.append(Quote(
                event_id=event.id,
                market="moneyline",
                outcome=event.away,
                probability=implied_probability(ml_away),
                source=source,
            ))

        ml_draw = odds.get("ml_draw")
        if ml_draw is not None and ml_draw != 0:
            quotes.append(Quote(
                event_id=event.id,
                market="moneyline",
                outcome="Draw",
                probability=implied_probability(ml_draw),
                source=source,
            ))
            
        spread_home = odds.get("spread_home")
        spread_home_line = odds.get("spread_home_line")
        if spread_home is not None and spread_home_line is not None and spread_home_line != 0:
            outcome_str = f"{event.home} {spread_home:g}" if spread_home < 0 else f"{event.home} +{spread_home:g}"
            quotes.append(Quote(
                event_id=event.id,
                market="spread",
                outcome=outcome_str,
                probability=implied_probability(spread_home_line),
                source=source,
            ))
            
        spread_away = odds.get("spread_away")
        spread_away_line = odds.get("spread_away_line")
        if spread_away is not None and spread_away_line is not None and spread_away_line != 0:
            outcome_str = f"{event.away} {spread_away:g}" if spread_away < 0 else f"{event.away} +{spread_away:g}"
            quotes.append(Quote(
                event_id=event.id,
                market="spread",
                outcome=outcome_str,
                probability=implied_probability(spread_away_line),
                source=source,
            ))
            
        total = odds.get("total")
        over_line = odds.get("over")
        under_line = odds.get("under")
        if total is not None:
            if over_line is not None and over_line != 0:
                quotes.append(Quote(
                    event_id=event.id,
                    market="total",
                    outcome=f"Over {total:g}",
                    probability=implied_probability(over_line),
                    source=source,
                ))
            if under_line is not None and under_line != 0:
                quotes.append(Quote(
                    event_id=event.id,
                    market="total",
                    outcome=f"Under {total:g}",
                    probability=implied_probability(under_line),
                    source=source,
                ))
    raw_time = game.get("last_updated") or game.get("updated_at") or game.get("timestamp")
    try:
        provider_time = parse_provider_timestamp(raw_time)
    except (TypeError, ValueError, OverflowError):
        provider_time = None
    processed_at = datetime.now(timezone.utc)
    for quote in quotes:
        quote.provider_timestamp = provider_time
        quote.observed_at = provider_time or quote.received_at
        quote.processed_at = processed_at
        quote.timestamp_trusted = provider_time is not None
    return quotes

def match_game(event: Event, games: list[dict]) -> dict | None:
    return best_team_pair_match(
        games,
        event.home,
        event.away,
        lambda game: [team.get("display_name", "") for team in game.get("teams", [])],
        event.game_start,
        lambda game: game.get("start_time"),
    )


async def _action_network_once(event: Event, client: httpx.AsyncClient,
                               emit: Callable[[list[Quote]], Awaitable[None]]) -> None:
    """Fetch one snapshot, retrying book metadata whenever it is still absent."""
    await _ensure_books(client)
    if not _book_map:
        logger.warning("Action Network book metadata is unavailable for %s; retrying", event.name)
        return

    sport = scoreboard_sport(event.odds_api_sport or "")
    headers = {"User-Agent": "Mozilla/5.0"}
    response = await client.get(
        f"https://api.actionnetwork.com/web/v1/scoreboard/{sport}", headers=headers
    )
    if response.status_code != 200:
        logger.warning("Action Network scoreboard status %s for %s", response.status_code, event.name)
        return
    matched = match_game(event, response.json().get("games", []))
    if matched:
        quotes = parse_action_quotes(event, matched)
        if quotes:
            await emit(quotes)

async def action_network_poll(event: Event, emit: Callable[[list[Quote]], Awaitable[None]]):
    if not event.odds_api_sport:
        return

    async with httpx.AsyncClient() as client:
        while True:
            try:
                await _action_network_once(event, client, emit)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Action Network poll error for {event.name}: {e}")
            
            await asyncio.sleep(25)
