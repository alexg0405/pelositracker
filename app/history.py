import asyncio
import os
import sqlite3
import threading
import time
from typing import Iterable

from .models import Event, GameState, Quote

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_outcomes (
    event_id TEXT PRIMARY KEY,
    name TEXT,
    sport TEXT,
    home TEXT,
    away TEXT,
    league TEXT,
    polymarket_slug TEXT,
    pregame_spread REAL,
    pregame_total REAL,
    final_home_score REAL,
    final_away_score REAL,
    final_status TEXT,
    settled_ts REAL
);

CREATE TABLE IF NOT EXISTS quotes_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    market TEXT NOT NULL,
    outcome TEXT NOT NULL,
    source TEXT,
    probability REAL NOT NULL,
    ask REAL,
    bid REAL,
    liquidity REAL,
    observed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quotes_event ON quotes_history(event_id, observed_at);

CREATE TABLE IF NOT EXISTS states_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    home_score REAL NOT NULL,
    away_score REAL NOT NULL,
    period TEXT,
    clock TEXT,
    status TEXT,
    observed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_states_event ON states_history(event_id, observed_at);
"""

def _now() -> float:
    return time.time()

class HistoryDB:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("HISTORY_DB", "history.db")
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            
        self._last_quote_time = {}
        self._last_quote_prob = {}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def log_quotes(self, quotes: Iterable[Quote]) -> None:
        now = _now()
        rows = []
        for q in quotes:
            key = f"{q.event_id}:{q.market}:{q.outcome}:{q.source}"
            last_prob = self._last_quote_prob.get(key)
            last_time = self._last_quote_time.get(key, 0)
            
            # Throttle: log if prob changed by > 1% or it's been > 3 minutes
            if last_prob is None or abs(q.probability - last_prob) > 0.01 or (now - last_time) > 180:
                self._last_quote_prob[key] = q.probability
                self._last_quote_time[key] = now
                rows.append((
                    q.event_id, q.market, q.outcome, q.source, 
                    q.probability, q.ask, q.bid, q.market_liquidity or q.liquidity, 
                    q.observed_at.timestamp()
                ))
        
        if not rows:
            return
            
        with self._lock:
            self._conn.executemany(
                """INSERT INTO quotes_history 
                   (event_id, market, outcome, source, probability, ask, bid, liquidity, observed_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""", rows
            )
            self._conn.commit()

    def log_state(self, state: GameState) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO states_history 
                   (event_id, home_score, away_score, period, clock, status, observed_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (state.event_id, state.home_score, state.away_score, 
                 state.period, state.clock, state.status, state.observed_at.timestamp())
            )
            self._conn.commit()

    def log_outcome(self, event: Event, pregame_spread: float | None, pregame_total: float | None, final_state: GameState | None) -> None:
        now = _now()
        home_score = final_state.home_score if final_state else None
        away_score = final_state.away_score if final_state else None
        status = final_state.status if final_state else None
        
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO event_outcomes 
                   (event_id, name, sport, home, away, league, polymarket_slug, 
                    pregame_spread, pregame_total, final_home_score, final_away_score, final_status, settled_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event.id, event.name, event.sport, event.home, event.away, event.league, event.polymarket_slug,
                 pregame_spread, pregame_total, home_score, away_score, status, now)
            )
            self._conn.commit()

    def get_event_history(self, event_id: str) -> dict:
        """Fetch chronological quotes and states for an event for charting."""
        with self._lock:
            quotes_cur = self._conn.execute(
                "SELECT market, outcome, probability, observed_at FROM quotes_history WHERE event_id=? ORDER BY observed_at ASC", 
                (event_id,)
            )
            states_cur = self._conn.execute(
                "SELECT home_score, away_score, status, observed_at FROM states_history WHERE event_id=? ORDER BY observed_at ASC", 
                (event_id,)
            )
            return {
                "quotes": [dict(r) for r in quotes_cur.fetchall()],
                "states": [dict(r) for r in states_cur.fetchall()]
            }
