import threading
import time
from typing import Iterable

from .database import Database
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
    pregame_spread DOUBLE PRECISION,
    pregame_total DOUBLE PRECISION,
    final_home_score DOUBLE PRECISION,
    final_away_score DOUBLE PRECISION,
    final_status TEXT,
    settled_ts DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS quotes_history (
    id SERIAL PRIMARY KEY,
    event_id TEXT NOT NULL,
    market TEXT NOT NULL,
    outcome TEXT NOT NULL,
    source TEXT,
    probability DOUBLE PRECISION NOT NULL,
    ask DOUBLE PRECISION,
    bid DOUBLE PRECISION,
    liquidity DOUBLE PRECISION,
    observed_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quotes_event ON quotes_history(event_id, observed_at);

CREATE TABLE IF NOT EXISTS states_history (
    id SERIAL PRIMARY KEY,
    event_id TEXT NOT NULL,
    home_score DOUBLE PRECISION NOT NULL,
    away_score DOUBLE PRECISION NOT NULL,
    period TEXT,
    clock TEXT,
    status TEXT,
    observed_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_states_event ON states_history(event_id, observed_at);
"""

def _now() -> float:
    return time.time()

class HistoryDB:
    def __init__(self, path: str | None = None):
        self._db = Database.open(
            path, sqlite_envs=("HISTORY_DB",), sqlite_default="history.db"
        )
        self.path = self._db.target
        self.backend = self._db.backend
        self._conn = self._db.connection
        self._lock = threading.Lock()
        with self._lock:
            self._db.initialize(_SCHEMA)
            
        self._last_quote_time = {}
        self._last_quote_prob = {}

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def log_quotes(self, quotes: Iterable[Quote]) -> None:
        now = _now()
        with self._lock:
            rows = []
            accepted = []
            for q in quotes:
                key = f"{q.event_id}:{q.market}:{q.outcome}:{q.source}"
                last_prob = self._last_quote_prob.get(key)
                last_time = self._last_quote_time.get(key, 0)

                # Throttle: log if prob changed by > 1% or it's been > 3 minutes.
                if (last_prob is None or abs(q.probability - last_prob) > 0.01
                        or (now - last_time) > 180):
                    accepted.append((key, q.probability))
                    rows.append((
                        q.event_id, q.market, q.outcome, q.source,
                        q.probability, q.ask, q.bid, q.market_liquidity or q.liquidity,
                        q.observed_at.timestamp(),
                    ))

            if not rows:
                return

            with self._db.transaction() as cur:
                self._db.execute_many(
                    cur,
                    """INSERT INTO quotes_history 
                       (event_id, market, outcome, source, probability, ask, bid, liquidity, observed_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    rows,
                )
            # Advance the throttle only after the database commit succeeds.
            for key, probability in accepted:
                self._last_quote_prob[key] = probability
                self._last_quote_time[key] = now

    def log_state(self, state: GameState) -> None:
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO states_history 
                       (event_id, home_score, away_score, period, clock, status, observed_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (state.event_id, state.home_score, state.away_score, 
                     state.period, state.clock, state.status, state.observed_at.timestamp())
                )

    def log_outcome(self, event: Event, pregame_spread: float | None, pregame_total: float | None, final_state: GameState | None) -> None:
        now = _now()
        home_score = final_state.home_score if final_state else None
        away_score = final_state.away_score if final_state else None
        status = final_state.status if final_state else None
        
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO event_outcomes 
                       (event_id, name, sport, home, away, league, polymarket_slug, 
                        pregame_spread, pregame_total, final_home_score, final_away_score, final_status, settled_ts)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(event_id) DO UPDATE SET
                         name=EXCLUDED.name, sport=EXCLUDED.sport, home=EXCLUDED.home, away=EXCLUDED.away,
                         league=EXCLUDED.league, polymarket_slug=EXCLUDED.polymarket_slug,
                         pregame_spread=EXCLUDED.pregame_spread, pregame_total=EXCLUDED.pregame_total,
                         final_home_score=EXCLUDED.final_home_score, final_away_score=EXCLUDED.final_away_score,
                         final_status=EXCLUDED.final_status, settled_ts=EXCLUDED.settled_ts""",
                    (event.id, event.name, event.sport, event.home, event.away, event.league, event.polymarket_slug,
                     pregame_spread, pregame_total, home_score, away_score, status, now)
                )

    def get_event_history(self, event_id: str) -> dict:
        """Fetch chronological quotes and states for an event for charting."""
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    "SELECT market, outcome, probability, observed_at FROM quotes_history WHERE event_id=%s ORDER BY observed_at ASC", 
                    (event_id,)
                )
                quotes_rows = cur.fetchall()
                self._db.execute(
                    cur,
                    "SELECT home_score, away_score, status, observed_at FROM states_history WHERE event_id=%s ORDER BY observed_at ASC", 
                    (event_id,)
                )
                states_rows = cur.fetchall()
                return {
                    "quotes": [dict(r) for r in quotes_rows],
                    "states": [dict(r) for r in states_rows]
                }
