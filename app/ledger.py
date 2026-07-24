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

import logging
import os
import threading
import time
import hashlib
import json
from typing import Iterable, Sequence

from .database import Database
from .models import Event, Signal

logger = logging.getLogger(__name__)


def _retention_seconds() -> float:
    """Decision-audit retention window; keeps the ledger from exhausting a small
    managed database (e.g. a 500 MB Supabase free tier). Floored at one hour."""
    try:
        days = float(os.getenv("DECISION_MARKS_RETENTION_DAYS", "7"))
    except ValueError:
        days = 7.0
    return max(3600.0, days * 86400.0)


def _max_decision_rows() -> int:
    """Hard row cap on ``decision_marks``, a backstop when a burst of activity
    fills the retention window faster than expected. The age-based prune alone
    can't bound a small managed database (500 MB Supabase) if a busy day writes
    millions of rows inside the window. ``0`` disables the cap."""
    try:
        rows = int(os.getenv("DECISION_MARKS_MAX_ROWS", "50000"))
    except ValueError:
        rows = 50000
    return max(0, rows)


# A CLV close mark may only be recorded (replacing the last valid one) when these
# typed execution-safety gates explicitly PASS. This replaces a fragile scan of
# human-readable reason substrings: reason wording now has no policy effect, and a
# gate that is missing, failing, or of unknown state fails closed.
_REQUIRED_CLOSE_MARK_GATES = (
    "provider_freshness", "market_identity", "market_status", "executable_fill",
)


def _close_mark_gates_pass(signal: Signal) -> bool:
    passed_by_code = {
        gate.get("code"): gate.get("passed") for gate in (signal.gate_results or [])
    }
    return all(passed_by_code.get(code) is True for code in _REQUIRED_CLOSE_MARK_GATES)


_PRUNE_THROTTLE_SECONDS = 600.0

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

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS decision_marks (
    decision_hash TEXT NOT NULL,
    event_id TEXT NOT NULL,
    market TEXT NOT NULL,
    outcome TEXT NOT NULL,
    as_of DOUBLE PRECISION NOT NULL,
    consensus_probability DOUBLE PRECISION,
    executable_probability DOUBLE PRECISION,
    gross_edge DOUBLE PRECISION,
    net_ev_per_stake DOUBLE PRECISION,
    policy_action TEXT NOT NULL,
    reasons TEXT NOT NULL,
    PRIMARY KEY(decision_hash, market, outcome)
);
CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    decision_hash TEXT NOT NULL,
    event_id TEXT NOT NULL,
    market TEXT NOT NULL,
    outcome TEXT NOT NULL,
    requested_cash DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    UNIQUE(event_id, market, outcome)
);
CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    filled_cash DOUBLE PRECISION NOT NULL,
    filled_shares DOUBLE PRECISION NOT NULL,
    effective_price DOUBLE PRECISION NOT NULL,
    fee DOUBLE PRECISION NOT NULL,
    filled_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS close_marks (
    event_id TEXT NOT NULL,
    market TEXT NOT NULL,
    outcome TEXT NOT NULL,
    executable_probability DOUBLE PRECISION NOT NULL,
    consensus_probability DOUBLE PRECISION,
    observed_at DOUBLE PRECISION NOT NULL,
    decision_hash TEXT NOT NULL,
    finalized_at DOUBLE PRECISION,
    PRIMARY KEY(event_id, market, outcome)
);
CREATE TABLE IF NOT EXISTS settlement_marks (
    event_id TEXT NOT NULL,
    market TEXT NOT NULL,
    outcome TEXT NOT NULL,
    result DOUBLE PRECISION,
    status TEXT NOT NULL,
    settled_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(event_id, market, outcome)
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
        self._last_prune = 0.0
        self._retention_seconds = _retention_seconds()
        self._max_decision_rows = _max_decision_rows()
        with self._lock:
            self._db.initialize(_SCHEMA, component="ledger", version=1)
            self._db.initialize(_SCHEMA_V2, component="ledger", version=2)
            self._db.migrate_columns("ledger", 3, {
                "bets": {
                    "decision_hash": "TEXT",
                    "closing_executable": "DOUBLE PRECISION",
                },
            })
            self._db.migrate_columns("ledger", 4, {
                "decision_marks": {
                    "decision_id": "TEXT",
                    "engine_version": "TEXT",
                    "configuration_hash": "TEXT",
                    "source_mapping_version": "TEXT",
                    "model_version": "TEXT",
                    "calibration_version": "TEXT",
                    "execution_policy_version": "TEXT",
                    "input_snapshot_json": "TEXT",
                    "token_id": "TEXT",
                    "order_book_snapshot_id": "TEXT",
                    "requested_cash": "DOUBLE PRECISION",
                    "execution_vwap": "DOUBLE PRECISION",
                    "execution_fee": "DOUBLE PRECISION",
                },
            })
            self._db.migrate_columns("ledger", 5, {
                "decision_marks": {
                    "calibrated_probability": "DOUBLE PRECISION",
                    "uncertainty_low": "DOUBLE PRECISION",
                    "uncertainty_high": "DOUBLE PRECISION",
                    "probability_net_ev_positive": "DOUBLE PRECISION",
                    "net_ev_per_share": "DOUBLE PRECISION",
                    "net_ev_total": "DOUBLE PRECISION",
                    "consensus_method": "TEXT",
                    "calibration_sample_size": "INTEGER",
                    "gate_results_json": "TEXT",
                },
                "bets": {
                    "entry_calibrated_prob": "DOUBLE PRECISION",
                    "probability_net_ev_positive": "DOUBLE PRECISION",
                    "net_ev_per_share": "DOUBLE PRECISION",
                    "net_ev_total": "DOUBLE PRECISION",
                    "requested_cash": "DOUBLE PRECISION",
                    "filled_cash": "DOUBLE PRECISION",
                    "filled_shares": "DOUBLE PRECISION",
                    "execution_fee": "DOUBLE PRECISION",
                    "consensus_method": "TEXT",
                    "calibration_sample_size": "INTEGER",
                },
            })
            self._db.migrate_columns("ledger", 6, {
                "decision_marks": {
                    "independent_model_probability": "DOUBLE PRECISION",
                    "independent_model_version": "TEXT",
                    "independent_model_hash": "TEXT",
                    "independent_calibration_version": "TEXT",
                    "independent_calibration_hash": "TEXT",
                    "independent_model_sample_size": "INTEGER",
                    "independent_model_event_count": "INTEGER",
                    "independent_model_registry_version": "TEXT",
                },
                "bets": {
                    "entry_independent_prob": "DOUBLE PRECISION",
                    "independent_model_version": "TEXT",
                    "independent_model_hash": "TEXT",
                    "independent_calibration_version": "TEXT",
                    "independent_calibration_hash": "TEXT",
                    "independent_model_sample_size": "INTEGER",
                    "independent_model_event_count": "INTEGER",
                },
            })
            # Index the retention/metrics sort key. Without it, both the prune's
            # `WHERE as_of < ...` and the metrics `ORDER BY as_of` do a full-table
            # sort that spills to a temp file — the exact write that fails with
            # DiskFull once the managed database is full.
            self._db.initialize(
                "CREATE INDEX IF NOT EXISTS idx_decision_marks_as_of "
                "ON decision_marks(as_of);",
                component="ledger", version=7,
            )
            # Bound the audit log at boot, not only while actively recording: a
            # process that starts against an already-oversized table (e.g. after
            # downtime, or right after an operator frees space) would otherwise
            # wait for the next write to shrink it.
            try:
                boot_now = _now()
                with self._db.transaction() as cur:
                    self._prune_decision_marks(cur, boot_now)
                self._last_prune = boot_now
            except Exception:
                # Never let retention housekeeping block startup — e.g. the disk
                # is still full and the DELETE can't write WAL. Space is cleared
                # out-of-band; steady-state pruning resumes once writes succeed.
                logger.exception("startup decision_marks prune failed")

    def _prune_decision_marks(self, cur, now: float) -> None:
        """Age- and count-bound ``decision_marks``. The caller holds ``_lock``
        and provides an open transaction cursor so this can run inside the write
        path or standalone at boot."""
        self._db.execute(
            cur, "DELETE FROM decision_marks WHERE as_of < %s",
            (now - self._retention_seconds,),
        )
        if self._max_decision_rows > 0:
            # Keep only the newest N rows by as_of. The subquery rides the
            # idx_decision_marks_as_of index (ORDER BY ... DESC LIMIT), so this
            # stays cheap as the table grows. No-op when the table is under cap
            # (the subquery's MIN is then the oldest row, and `< MIN` matches
            # nothing).
            self._db.execute(
                cur,
                """DELETE FROM decision_marks WHERE as_of < (
                       SELECT MIN(as_of) FROM (
                           SELECT as_of FROM decision_marks
                           ORDER BY as_of DESC LIMIT %s
                       ) AS keep
                   )""",
                (self._max_decision_rows,),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record_signals(self, event: Event, signals: Iterable[Signal]) -> int:
        """Log the entry snapshot of every PAPER_BET, once per selection."""
        signals = list(signals)
        now = max((signal.observed_at.timestamp() for signal in signals), default=_now())
        rows = [
            (
                event.id, event.name, event.sport, s.market, s.outcome, s.quote_source,
                now, s.market_probability,
                s.consensus_probability or s.market_fair_prob, s.edge, s.confidence,
                s.devig_method, s.overround, s.n_reference_sources, s.decision_hash,
                (s.calibrated_consensus_probability
                 if s.calibrated_consensus_probability is not None else s.market_fair_prob),
                s.probability_net_ev_positive, s.net_expected_value_per_share,
                s.net_expected_value_total, s.requested_cash, s.filled_cash,
                s.filled_shares, s.execution_fee, s.consensus_method,
                s.calibration_sample_size,
                s.independent_model_probability, s.independent_model_version,
                s.independent_model_hash, s.independent_calibration_version,
                s.independent_calibration_hash, s.independent_model_sample_size,
                s.independent_model_event_count,
            )
            for s in signals
            if s.action == "PAPER_BET"
            and s.execution_complete
            and s.requested_cash is not None and s.requested_cash > 0
            and s.filled_cash is not None and s.filled_cash > 0
            and s.filled_shares is not None and s.filled_shares > 0
            and s.execution_fee is not None and s.execution_fee >= 0
        ]
        with self._lock:
            inserted = 0
            with self._db.transaction() as cur:
                for signal in signals:
                    self._db.execute(
                        cur,
                        """INSERT INTO decision_marks
                           (decision_hash, event_id, market, outcome, as_of,
                            consensus_probability, executable_probability, gross_edge,
                            net_ev_per_stake, policy_action, reasons, decision_id,
                            engine_version, configuration_hash, source_mapping_version,
                            model_version, calibration_version, execution_policy_version,
                             input_snapshot_json, token_id, order_book_snapshot_id,
                             requested_cash, execution_vwap, execution_fee,
                             calibrated_probability, uncertainty_low, uncertainty_high,
                             probability_net_ev_positive, net_ev_per_share, net_ev_total,
                             consensus_method, calibration_sample_size, gate_results_json,
                             independent_model_probability, independent_model_version,
                             independent_model_hash, independent_calibration_version,
                             independent_calibration_hash, independent_model_sample_size,
                             independent_model_event_count,
                             independent_model_registry_version)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(decision_hash, market, outcome) DO NOTHING""",
                        (signal.decision_hash, event.id, signal.market, signal.outcome,
                         signal.observed_at.timestamp(), signal.consensus_probability,
                         signal.market_probability, signal.edge, signal.ev_per_stake,
                         signal.action, "\n".join(signal.reasons), signal.decision_id,
                         signal.engine_version, signal.configuration_hash,
                         signal.source_mapping_version, signal.model_version,
                         signal.calibration_version, signal.execution_policy_version,
                         (signal.input_snapshot_json
                          if signal.action == "PAPER_BET" else None), signal.token_id,
                          signal.order_book_snapshot_id, signal.requested_cash,
                          signal.execution_vwap, signal.execution_fee,
                          signal.calibrated_consensus_probability, signal.uncertainty_low,
                          signal.uncertainty_high, signal.probability_net_ev_positive,
                          signal.net_expected_value_per_share, signal.net_expected_value_total,
                          signal.consensus_method, signal.calibration_sample_size,
                          json.dumps(signal.gate_results, sort_keys=True, separators=(",", ":")),
                          signal.independent_model_probability,
                          signal.independent_model_version, signal.independent_model_hash,
                          signal.independent_calibration_version,
                          signal.independent_calibration_hash,
                          signal.independent_model_sample_size,
                          signal.independent_model_event_count,
                          signal.independent_model_registry_version),
                    )
                    if signal.market_probability > 0 and _close_mark_gates_pass(signal):
                        self._db.execute(
                            cur,
                            """INSERT INTO close_marks
                               (event_id, market, outcome, executable_probability,
                                consensus_probability, observed_at, decision_hash)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT(event_id, market, outcome) DO UPDATE SET
                                 executable_probability=EXCLUDED.executable_probability,
                                 consensus_probability=EXCLUDED.consensus_probability,
                                 observed_at=EXCLUDED.observed_at,
                                 decision_hash=EXCLUDED.decision_hash
                               WHERE EXCLUDED.observed_at >= close_marks.observed_at
                                 AND close_marks.finalized_at IS NULL""",
                            (event.id, signal.market, signal.outcome,
                             signal.market_probability, signal.consensus_probability,
                             signal.observed_at.timestamp(), signal.decision_hash),
                        )
                for row in rows:
                    self._db.execute(
                        cur,
                        """INSERT INTO bets
                           (event_id, event_name, sport, market, outcome, quote_source,
                            entry_ts, entry_executable, entry_fair_prob, entry_edge,
                             confidence, devig_method, overround, n_reference_sources,
                             decision_hash, entry_calibrated_prob,
                             probability_net_ev_positive, net_ev_per_share, net_ev_total,
                             requested_cash, filled_cash, filled_shares, execution_fee,
                             consensus_method, calibration_sample_size,
                             entry_independent_prob, independent_model_version,
                             independent_model_hash, independent_calibration_version,
                             independent_calibration_hash, independent_model_sample_size,
                             independent_model_event_count)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (event_id, market, outcome) DO NOTHING""",
                        row,
                    )
                    inserted += max(cur.rowcount, 0)
                    if cur.rowcount:
                        order_id = hashlib.sha256(
                            f"{row[14]}:{row[3]}:{row[4]}".encode("utf-8")
                        ).hexdigest()
                        requested_cash = float(row[19])
                        filled_cash = float(row[20])
                        filled_shares = float(row[21])
                        self._db.execute(
                            cur,
                            """INSERT INTO paper_orders
                               (order_id, decision_hash, event_id, market, outcome,
                                requested_cash, status, created_at, updated_at)
                               VALUES (%s,%s,%s,%s,%s,%s,'filled',%s,%s)
                               ON CONFLICT(event_id, market, outcome) DO NOTHING""",
                            (order_id, row[14], event.id, row[3], row[4],
                             requested_cash, now, now),
                        )
                        self._db.execute(
                            cur,
                            """INSERT INTO paper_fills
                               (fill_id, order_id, filled_cash, filled_shares,
                                effective_price, fee, filled_at)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT(fill_id) DO NOTHING""",
                            (hashlib.sha256(f"fill:{order_id}".encode()).hexdigest(),
                             order_id, filled_cash, filled_shares,
                             row[7], float(row[22]), now),
                        )
                # Bound the decision-audit log so it cannot exhaust a small
                # managed database (the full input snapshot is already kept only
                # for PAPER_BET rows). Age- and count-pruned on a throttled
                # cadence in the same transaction.
                if now - self._last_prune > _PRUNE_THROTTLE_SECONDS:
                    self._prune_decision_marks(cur, now)
                    self._last_prune = now
            return inserted

    def snapshot_closing(self, event_id: str,
                         fair_by_selection: dict[tuple[str, str], float] | None = None) -> None:
        """Freeze the last pre-suspension mark and compute executable-price CLV."""
        now = _now()
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur, "UPDATE close_marks SET finalized_at=%s "
                         "WHERE event_id=%s AND finalized_at IS NULL", (now, event_id))
                self._db.execute(
                    cur,
                    """UPDATE bets SET
                         closing_executable=(SELECT executable_probability FROM close_marks c
                           WHERE c.event_id=bets.event_id AND c.market=bets.market
                             AND c.outcome=bets.outcome),
                         closing_fair_prob=(SELECT consensus_probability FROM close_marks c
                           WHERE c.event_id=bets.event_id AND c.market=bets.market
                             AND c.outcome=bets.outcome),
                         clv=(SELECT executable_probability FROM close_marks c
                           WHERE c.event_id=bets.event_id AND c.market=bets.market
                             AND c.outcome=bets.outcome) - entry_executable,
                         closing_ts=(SELECT observed_at FROM close_marks c
                           WHERE c.event_id=bets.event_id AND c.market=bets.market
                             AND c.outcome=bets.outcome)
                       WHERE event_id=%s AND closing_ts IS NULL
                         AND EXISTS (SELECT 1 FROM close_marks c WHERE c.event_id=bets.event_id
                           AND c.market=bets.market AND c.outcome=bets.outcome)""",
                    (event_id,),
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
                    for result, settled_at, bet_id in updates:
                        self._db.execute(
                            cur,
                            """INSERT INTO settlement_marks
                               (event_id, market, outcome, result, status, settled_at)
                               SELECT event_id, market, outcome, %s, 'settled', %s
                               FROM bets WHERE id=%s
                               ON CONFLICT(event_id, market, outcome) DO NOTHING""",
                            (result, settled_at, bet_id),
                        )
                # Label the FULL opportunity set (WATCH/rejected outcomes too),
                # not just placed bets, so shadow evaluation can score every
                # decision without selection bias. Only moneyline outcomes get a
                # win/loss label, and only now that the event has settled -- the
                # label was not available at decision time.
                self._db.execute(
                    cur,
                    "SELECT DISTINCT market, outcome FROM decision_marks WHERE event_id=%s",
                    (event_id,),
                )
                opportunity_labels = [
                    (event_id, row["market"], row["outcome"],
                     1.0 if row["outcome"] in winner_labels else 0.0, now)
                    for row in cur.fetchall()
                    if row["market"].lower() in _MONEYLINE_MARKETS
                ]
                for ev, market, outcome, result, settled_at in opportunity_labels:
                    self._db.execute(
                        cur,
                        """INSERT INTO settlement_marks
                           (event_id, market, outcome, result, status, settled_at)
                           VALUES (%s,%s,%s,%s,'settled',%s)
                           ON CONFLICT(event_id, market, outcome) DO NOTHING""",
                        (ev, market, outcome, result, settled_at),
                    )

    def void_event(self, event_id: str, *, status: str = "void") -> None:
        """Record an idempotent non-result settlement without inventing a loss."""
        if status not in {"void", "canceled", "abandoned", "ungradeable"}:
            raise ValueError("unsupported void status")
        now = _now()
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO settlement_marks
                       (event_id, market, outcome, result, status, settled_at)
                       SELECT event_id, market, outcome, NULL, %s, %s
                       FROM bets WHERE event_id=%s
                       ON CONFLICT(event_id, market, outcome) DO UPDATE SET
                         result=NULL, status=EXCLUDED.status,
                         settled_at=EXCLUDED.settled_at""",
                    (status, now, event_id),
                )
                self._db.execute(
                    cur,
                    "UPDATE paper_orders SET status=%s, updated_at=%s WHERE event_id=%s",
                    (status, now, event_id),
                )

    def all_bets(self, *, since_ts: float | None = None,
                 limit: int | None = None) -> list[dict]:
        """All recorded entry snapshots, oldest first.

        ``since_ts``/``limit`` are optional bounds (default ``None`` keeps the
        full history) so a caller charting or paging a large ledger can hold a
        page-sized result instead of every row a long-running deployment has
        accumulated."""
        return self._select_rows(
            "SELECT * FROM bets", "entry_ts", since_ts=since_ts, limit=limit
        )

    def all_decisions(self, *, since_ts: float | None = None,
                      limit: int | None = None) -> list[dict]:
        """Return every evaluated opportunity, including WATCH/rejected rows.

        ``since_ts``/``limit`` bound the scan as in :meth:`all_bets`; both
        default to ``None`` (return everything, unchanged)."""
        return self._select_rows(
            "SELECT * FROM decision_marks", "as_of", since_ts=since_ts, limit=limit
        )

    def scored_opportunities(self) -> list[dict]:
        """Every evaluated opportunity (WATCH/rejected included) joined to its
        later settled label, for *observational* shadow evaluation without the
        selection bias of scoring only placed bets.

        Only settled outcomes appear -- the win/loss label is not available until
        the event resolves, so the join respects point-in-time availability. Each
        row is one decision-time forecast; a long-lived event contributes many
        correlated ticks, so downstream scoring must treat these as dependent
        (event-clustered), not i.i.d.
        """
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT d.event_id, d.market, d.outcome, d.as_of,
                              d.policy_action, d.consensus_probability,
                              d.calibrated_probability, d.executable_probability,
                              s.result AS settled_result
                       FROM decision_marks d
                       JOIN settlement_marks s
                         ON s.event_id=d.event_id AND s.market=d.market
                            AND s.outcome=d.outcome
                       WHERE s.status='settled' AND s.result IS NOT NULL
                       ORDER BY d.as_of""",
                )
                return [dict(row) for row in cur.fetchall()]

    def decision_coverage(self, *, since_ts: float | None = None,
                          limit: int | None = None) -> list[dict]:
        """Lean projection for eligibility coverage / the metrics endpoint.

        ``backtest.summary`` only reads ``policy_action`` and ``gate_results_json``
        (plus the row count) from decisions. ``all_decisions`` does ``SELECT *``,
        which drags the full per-decision ``input_snapshot_json`` blob — the
        entire evaluation request, every quote considered — for every row ever
        recorded. On a long-running deployment that is a multi-hundred-MB read
        the dashboard triggers on every metrics refresh (including after an event
        removal). Select only the columns the summary consumes."""
        return self._select_rows(
            "SELECT policy_action, gate_results_json, as_of FROM decision_marks",
            "as_of", since_ts=since_ts, limit=limit,
        )

    def _select_rows(self, select: str, order_column: str, *,
                     since_ts: float | None, limit: int | None) -> list[dict]:
        clauses = ""
        params: list = []
        if since_ts is not None:
            clauses += f" WHERE {order_column} >= %s"
            params.append(since_ts)
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                if limit is not None:
                    params.append(limit)
                    self._db.execute(
                        cur,
                        f"{select}{clauses} ORDER BY {order_column} DESC LIMIT %s",
                        tuple(params),
                    )
                    return [dict(row) for row in reversed(cur.fetchall())]
                self._db.execute(
                    cur, f"{select}{clauses} ORDER BY {order_column}", tuple(params)
                )
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

    def event_positions_bulk(self, event_ids: Sequence[str]) -> dict[str, list[dict]]:
        """Positions for many events in one query.

        The dashboard fan-out (``/api/events`` and the SSE snapshot) renders
        every tracked event; fetching positions per event issued one round-trip
        each (an N+1). This collapses that into a single ``IN`` query and buckets
        the rows by event, preserving the per-event ``updated_ts DESC`` ordering
        that ``event_positions`` returns."""
        ids = list(dict.fromkeys(event_ids))  # de-dup, keep order
        out: dict[str, list[dict]] = {event_id: [] for event_id in ids}
        if not ids:
            return out
        placeholders = ",".join(["%s"] * len(ids))
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    f"SELECT * FROM positions WHERE event_id IN ({placeholders}) "
                    "ORDER BY updated_ts DESC",
                    tuple(ids),
                )
                for row in cur.fetchall():
                    out.setdefault(row["event_id"], []).append(dict(row))
        return out

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
