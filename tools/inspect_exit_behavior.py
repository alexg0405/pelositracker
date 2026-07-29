"""Read-only diagnostics for managed-position exit behavior.

This intentionally opens the trading database in query-only mode.  It is an
operator/research tool: it never changes positions, policies, or journal rows.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _rows(
    connection: sqlite3.Connection,
    query: str,
    params: tuple[object, ...] = (),
) -> list[dict[str, object]]:
    return [dict(row) for row in connection.execute(query, params)]


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def inspect(path: Path) -> dict[str, object]:
    resolved = path.expanduser().resolve()
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        report: dict[str, object] = {
            "exit_reasons": _rows(
                connection,
                """SELECT exit_reason,COUNT(*) positions,
                          SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
                          SUM(CASE WHEN realized_pnl<0 THEN 1 ELSE 0 END) losses,
                          SUM(COALESCE(realized_pnl,0)) realized_pnl,
                          AVG(realized_pnl) average_pnl
                   FROM live_managed_positions
                   WHERE status='closed'
                   GROUP BY exit_reason
                   ORDER BY positions DESC""",
            ),
            "market_exit_reasons": _rows(
                connection,
                """SELECT market_type,exit_reason,COUNT(*) positions,
                          SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
                          SUM(CASE WHEN realized_pnl<0 THEN 1 ELSE 0 END) losses,
                          SUM(COALESCE(realized_pnl,0)) realized_pnl,
                          AVG(realized_pnl) average_pnl
                   FROM live_managed_positions
                   WHERE status='closed'
                   GROUP BY market_type,exit_reason
                   ORDER BY market_type,positions DESC""",
            ),
            "recent_hard_stops": _rows(
                connection,
                """SELECT id,event_name,market_type,selection,entry_cost,
                          current_exit_value,quantity,realized_pnl,opened_ts,
                          closed_ts,current_execution_edge,entry_signal_edge,
                          entry_signal_quality
                   FROM live_managed_positions
                   WHERE exit_reason='hard_stop_loss'
                   ORDER BY closed_ts DESC
                   LIMIT 25""",
            ),
            "journal_counts": _rows(
                connection,
                """SELECT kind,status,COUNT(*) rows
                   FROM live_trading_journal
                   GROUP BY kind,status
                   ORDER BY rows DESC""",
            ),
        }
        report["adaptive_exit"] = (
            _rows(
                connection,
                """SELECT mode,market_type,COUNT(*) observations,
                          COUNT(DISTINCT event_id) events,
                          SUM(CASE WHEN adverse_label=1 THEN 1 ELSE 0 END)
                              adverse,
                          SUM(CASE WHEN adverse_label=0 THEN 1 ELSE 0 END)
                              favorable
                   FROM adaptive_exit_observations
                   GROUP BY mode,market_type
                   ORDER BY observations DESC""",
            )
            if _table_exists(connection, "adaptive_exit_observations")
            else []
        )
        report["post_exit_recovery"] = (
            _rows(
                connection,
                """SELECT market_type,exit_reason,COUNT(*) exits,
                          SUM(CASE WHEN recovered_entry_ts IS NOT NULL
                                   THEN 1 ELSE 0 END) recovered_entry,
                          SUM(CASE WHEN recovered_half_loss_ts IS NOT NULL
                                   THEN 1 ELSE 0 END) recovered_half_loss,
                          AVG(best_exit_value-exit_value) average_rebound,
                          AVG(exit_value-worst_exit_value)
                              average_avoided_further_loss
                   FROM exit_recovery_observations
                   GROUP BY market_type,exit_reason
                   ORDER BY exits DESC""",
            )
            if _table_exists(connection, "exit_recovery_observations")
            else []
        )
        return report
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path("polymarket-us-trading.db"),
    )
    args = parser.parse_args()
    print(json.dumps(inspect(args.path), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
