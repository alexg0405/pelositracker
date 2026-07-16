import asyncio
import logging
from typing import Awaitable, Callable

import httpx

from .models import Event, Quote

logger = logging.getLogger(__name__)

_book_map = {}

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
            r = await client.get("https://api.actionnetwork.com/web/v1/books")
            if r.status_code == 200:
                for b in r.json().get("books", []):
                    _book_map[b["id"]] = b["display_name"]
        except Exception as e:
            logger.error(f"Failed to fetch Action Network books: {e}")

def parse_action_quotes(event: Event, game: dict) -> list[Quote]:
    quotes = []
    for odds in game.get("odds", []):
        book_id = odds.get("book_id")
        book_name = _book_map.get(book_id)
        if not book_name: continue
        
        source = book_name.lower().replace(" nj", "").replace(" on", "").replace(" mi", "").strip()
        if "consensus" in source or "open" in source:
            continue
            
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
            
        spread_home = odds.get("spread_home")
        spread_home_line = odds.get("spread_home_line")
        if spread_home is not None and spread_home_line is not None and spread_home_line != 0:
            quotes.append(Quote(
                event_id=event.id,
                market=f"spread_{spread_home:g}",
                outcome=event.home,
                probability=implied_probability(spread_home_line),
                source=source,
            ))
            
        spread_away = odds.get("spread_away")
        spread_away_line = odds.get("spread_away_line")
        if spread_away is not None and spread_away_line is not None and spread_away_line != 0:
            quotes.append(Quote(
                event_id=event.id,
                market=f"spread_{spread_away:g}",
                outcome=event.away,
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
                    market=f"total_{total:g}",
                    outcome="Over",
                    probability=implied_probability(over_line),
                    source=source,
                ))
            if under_line is not None and under_line != 0:
                quotes.append(Quote(
                    event_id=event.id,
                    market=f"total_{total:g}",
                    outcome="Under",
                    probability=implied_probability(under_line),
                    source=source,
                ))
    return quotes

def match_game(event: Event, games: list[dict]) -> dict | None:
    target_home = event.home.lower().replace(" st.", " state").split()[-1]
    target_away = event.away.lower().replace(" st.", " state").split()[-1]
    
    for game in games:
        teams = game.get("teams", [])
        if len(teams) < 2: continue
        
        t1 = teams[0].get("display_name", "").lower()
        t2 = teams[1].get("display_name", "").lower()
        
        if (target_home in t1 or target_home in t2) and (target_away in t1 or target_away in t2):
            return game
            
    return None

async def action_network_poll(event: Event, emit: Callable[[list[Quote]], Awaitable[None]]):
    if not event.odds_api_sport: return
    
    sport = event.odds_api_sport.split("_")[-1]
    headers = {"User-Agent": "Mozilla/5.0"}
    
    async with httpx.AsyncClient() as client:
        await _ensure_books(client)
        
        while True:
            try:
                r = await client.get(f"https://api.actionnetwork.com/web/v1/scoreboard/{sport}", headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    games = data.get("games", [])
                    matched = match_game(event, games)
                    if matched:
                        quotes = parse_action_quotes(event, matched)
                        if quotes:
                            await emit(quotes)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Action Network poll error for {event.name}: {e}")
            
            await asyncio.sleep(60)
