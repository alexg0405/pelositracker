"""Durable paper-bet ledger — the 'truth loop'.

Every time a signal fires PAPER_BET we record one row per (event, market,
outcome) at its entry price. When the event locks we snapshot the closing
consensus fair value and compute CLV (closing_fair_prob - entry_executable),
which is the primary, settlement-free measure of whether the edge was real.
Moneyline bets are additionally settled from the final score so calibration
metrics (Brier, log-loss) can be computed offline in backtest.py.

CLV needs only market data; it is available for every market. Settlement
(win/loss) is only derived for moneyline here, because spreads/totals/props
need data the system does not yet ingest.
"""
from __future__ import annotations

import threading
import time
from typing import Iterable

from .database import Database
from .models import Event, Signal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    id                  SERIAL PRIMARY KEY,
    event_id            TEXT NOT NULL,
    event_name          TEXT,
    sport               TEXT,
    market              TEXT NOT NULL,
    outcome             TEXT NOT NULL,
    quote_source        TEXT,
    entry_ts            DOUBLE PRECISION NOT NULL,
    entry_executable    DOUBLE PRECISION NOT NULL,
    entry_fair_prob     DOUBLE PRECISION NOT NULL,
    entry_edge          DOUBLE PRECISION NOT NULL,
    confidence          DOUBLE PRECISION,
    devig_method        TEXT,
    overround           DOUBLE PRECISION,
    n_reference_sources INTEGER,
    closing_fair_prob   DOUBLE PRECISION,
    clv                 DOUBLE PRECISION,
    closing_ts          DOUBLE PRECISION,
    settled_result      DOUBLE PRECISION,
    settled_ts          DOUBLE PRECISION,
    UNIQUE(event_id, market, outcome)
);
CREATE TABLE IF NOT EXISTS closing_lines (
    event_id          TEXT NOT NULL,
    market            TEXT NOT NULL,
    outcome           TEXT NOT NULL,
    closing_fair_prob DOUBLE PRECISION NOT NULL,
    closing_ts        DOUBLE PRECISION NOT NULL,
    UNIQUE(event_id, market, outcome)
);
CREATE TABLE IF NOT EXISTS positions (
    event_id        TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    market          TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    shares          DOUBLE PRECISION NOT NULL,
    avg_entry_price DOUBLE PRECISION NOT NULL,
    created_ts      DOUBLE PRECISION NOT NULL,
    updated_ts      DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(event_id, token_id)
);
"""

_MONEYLINE_MARKETS = {"moneyline", "h2h", "winner"}


def _now() -> float:
    return time.time()


class Ledger:
    """Thread-safe append log backed by PostgreSQL or local SQLite."""

    def __init__(self, path: str | None = None):
        self._db = Database.open(
            path, sqlite_envs=("LEDGER_DB",), sqlite_default="ledger.db"
        )
        self.path = self._db.target
        self.backend = self._db.backend
        self._conn = self._db.connection
        self._lock = threading.Lock()
        with self._lock:
            self._db.initialize(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record_signals(self, event: Event, signals: Iterable[Signal]) -> int:
        """Log the entry snapshot of every PAPER_BET, once per selection."""
        now = _now()
        rows = [
            (
                event.id, event.name, event.sport, s.market, s.outcome, s.quote_source,
                now, s.market_probability, s.market_fair_prob, s.edge, s.confidence,
                s.devig_method, s.overround, s.n_reference_sources,
            )
            for s in signals
            if s.action == "PAPER_BET"
        ]
        if not rows:
            return 0
        with self._lock:
            inserted = 0
            with self._db.transaction() as cur:
                for row in rows:
                    self._db.execute(
                        cur,
                        """INSERT INTO bets
                           (event_id, event_name, sport, market, outcome, quote_source,
                            entry_ts, entry_executable, entry_fair_prob, entry_edge,
                            confidence, devig_method, overround, n_reference_sources)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (event_id, market, outcome) DO NOTHING""",
                        row,
                    )
                    inserted += max(cur.rowcount, 0)
            return inserted

    def snapshot_closing(self, event_id: str, fair_by_selection: dict[tuple[str, str], float]) -> None:
        """Record the closing consensus fair and compute CLV for open bets."""
        if not fair_by_selection:
            return
        now = _now()
        with self._lock:
            with self._db.transaction() as cur:
                for (market, outcome), fair in fair_by_selection.items():
                    self._db.execute(
                        cur,
                        """INSERT INTO closing_lines (event_id, market, outcome, closing_fair_prob, closing_ts)
                           VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT(event_id, market, outcome)
                           DO UPDATE SET closing_fair_prob=EXCLUDED.closing_fair_prob,
                                         closing_ts=EXCLUDED.closing_ts""",
                        (event_id, market, outcome, fair, now),
                    )
                    # CLV = closing fair prob - the price we entered at. Only set once.
                    self._db.execute(
                        cur,
                        """UPDATE bets
                           SET closing_fair_prob=%s, clv=%s - entry_executable, closing_ts=%s
                           WHERE event_id=%s AND market=%s AND outcome=%s AND closing_fair_prob IS NULL""",
                        (fair, fair, now, event_id, market, outcome),
                    )

    def settle_moneyline(self, event_id: str, winner_labels: set[str]) -> None:
        """Settle moneyline-style bets from the final result (win=1, loss=0)."""
        if not winner_labels:  # never settle every bet to a loss on unknown result
            return
        now = _now()
        with self._lock:
            with self._db.transaction(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT id, market, outcome FROM bets
                       WHERE event_id=%s AND settled_result IS NULL""",
                    (event_id,),
                )
                updates = [
                    (1.0 if row["outcome"] in winner_labels else 0.0, now, row["id"])
                    for row in cur.fetchall()
                    if row["market"].lower() in _MONEYLINE_MARKETS
                ]
                if updates:
                    self._db.execute_many(
                        cur,
                        "UPDATE bets SET settled_result=%s, settled_ts=%s WHERE id=%s",
                        updates,
                    )

    def all_bets(self) -> list[dict]:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(cur, "SELECT * FROM bets ORDER BY entry_ts")
                return [dict(row) for row in cur.fetchall()]

    def event_bets(self, event_id: str) -> list[dict]:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur, "SELECT * FROM bets WHERE event_id=%s ORDER BY entry_ts", (event_id,)
                )
                return [dict(row) for row in cur.fetchall()]

    def upsert_position(self, event_id: str, token_id: str, market: str, outcome: str,
                        shares: float, avg_entry_price: float) -> dict:
        now = _now()
        with self._lock:
            with self._db.transaction(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO positions
                       (event_id, token_id, market, outcome, shares, avg_entry_price, created_ts, updated_ts)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(event_id, token_id) DO UPDATE SET
                         market=EXCLUDED.market, outcome=EXCLUDED.outcome, shares=EXCLUDED.shares,
                         avg_entry_price=EXCLUDED.avg_entry_price, updated_ts=EXCLUDED.updated_ts""",
                    (event_id, token_id, market, outcome, shares, avg_entry_price, now, now),
                )
                self._db.execute(
                    cur,
                    "SELECT * FROM positions WHERE event_id=%s AND token_id=%s", (event_id, token_id)
                )
                return dict(cur.fetchone())

    def event_positions(self, event_id: str) -> list[dict]:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    "SELECT * FROM positions WHERE event_id=%s ORDER BY updated_ts DESC", (event_id,)
                )
                return [dict(row) for row in cur.fetchall()]

    def delete_position(self, event_id: str, token_id: str) -> bool:
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    "DELETE FROM positions WHERE event_id=%s AND token_id=%s", (event_id, token_id)
                )
                rc = cur.rowcount
            return rc > 0

    def delete_event_positions(self, event_id: str) -> None:
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(cur, "DELETE FROM positions WHERE event_id=%s", (event_id,))
