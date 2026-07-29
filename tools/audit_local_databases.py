"""Print read-only storage and row-count diagnostics for local SQLite data."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


DEFAULT_PATHS = (
    Path("workstation-data/history.db"),
    Path("workstation-data/ledger.db"),
    Path("workstation-data/model-lab.db"),
    Path("workstation-data/polymarket-us-trading.db"),
    Path("polymarket-us-trading.db"),
)


def _rows(
    connection: sqlite3.Connection,
    query: str,
) -> list[dict[str, object]]:
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(query)]


def _domain_details(
    connection: sqlite3.Connection,
    tables: set[str],
) -> dict[str, object]:
    details: dict[str, object] = {}
    if "quotes_history" in tables:
        details["quotes"] = _rows(
            connection,
            """SELECT MIN(observed_at) first_observed_ts,
                      MAX(observed_at) last_observed_ts,
                      COUNT(DISTINCT event_id) events,
                      COUNT(DISTINCT source) sources,
                      COUNT(DISTINCT book_hash) book_snapshots,
                      AVG(LENGTH(COALESCE(bid_levels_json,''))+
                          LENGTH(COALESCE(ask_levels_json,''))) avg_depth_json_bytes
               FROM quotes_history""",
        )[0]
    if "decision_marks" in tables:
        details["decisions"] = _rows(
            connection,
            """SELECT MIN(as_of) first_decision_ts,
                      MAX(as_of) last_decision_ts,
                      COUNT(DISTINCT event_id) events,
                      COUNT(DISTINCT decision_id) decisions,
                      AVG(LENGTH(COALESCE(gate_results_json,''))) avg_gate_json_bytes,
                      AVG(LENGTH(COALESCE(input_snapshot_json,''))) avg_snapshot_json_bytes
               FROM decision_marks""",
        )[0]
    if "sport_model_observations" in tables:
        details["model_segments"] = _rows(
            connection,
            """SELECT sport,league,market,COUNT(*) observations,
                      COUNT(DISTINCT event_id) events,
                      SUM(CASE WHEN result_label IS NOT NULL AND canceled=0
                               THEN 1 ELSE 0 END) labeled,
                      COUNT(DISTINCT CASE WHEN result_label IS NOT NULL
                                           AND canceled=0 THEN event_id END)
                          labeled_events,
                      SUM(CASE WHEN result_label IS NOT NULL AND canceled=0
                                    AND fraction_remaining IS NOT NULL
                                    AND score_differential IS NOT NULL
                               THEN 1 ELSE 0 END) state_complete
               FROM sport_model_observations
               GROUP BY sport,league,market
               ORDER BY observations DESC""",
        )
        details["model_targets"] = _rows(
            connection,
            """SELECT target_name,horizon_seconds,COUNT(*) labels,
                      COUNT(DISTINCT o.event_id) events
               FROM sport_model_targets t
               JOIN sport_model_observations o ON o.id=t.observation_id
               GROUP BY target_name,horizon_seconds
               ORDER BY target_name,horizon_seconds""",
        )
        details["model_candidates"] = _rows(
            connection,
            """SELECT sport,league,market,status,COUNT(*) candidates,
                      MAX(created_ts) latest_created_ts
               FROM sport_model_candidates
               GROUP BY sport,league,market,status
               ORDER BY latest_created_ts DESC""",
        )
    if "adaptive_exit_observations" in tables:
        details["adaptive_exit"] = _rows(
            connection,
            """SELECT mode,COUNT(*) observations,
                      COUNT(DISTINCT event_id) events,
                      SUM(CASE WHEN resolved_ts IS NOT NULL THEN 1 ELSE 0 END)
                          resolved,
                      SUM(CASE WHEN adverse_label=1 THEN 1 ELSE 0 END) adverse,
                      SUM(CASE WHEN adverse_label=0 THEN 1 ELSE 0 END) favorable
               FROM adaptive_exit_observations GROUP BY mode""",
        )
    if "live_managed_positions" in tables:
        details["managed_positions"] = _rows(
            connection,
            """SELECT mode,status,COUNT(*) positions,
                      SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
                      SUM(CASE WHEN realized_pnl<0 THEN 1 ELSE 0 END) losses,
                      SUM(COALESCE(realized_pnl,0)) realized_pnl
               FROM live_managed_positions GROUP BY mode,status""",
        )
    return details


def inspect(path: Path, *, details: bool = False) -> dict[str, object]:
    resolved = path.expanduser().resolve()
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro",
        uri=True,
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {
            table: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
            )
            for table in tables
        }
        payload: dict[str, object] = {
            "path": str(path),
            "bytes": resolved.stat().st_size,
            "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
            "page_count": int(
                connection.execute("PRAGMA page_count").fetchone()[0]
            ),
            "free_pages": int(
                connection.execute("PRAGMA freelist_count").fetchone()[0]
            ),
            "rows": counts,
        }
        if details:
            payload["details"] = _domain_details(connection, set(tables))
        return payload
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument(
        "--details",
        action="store_true",
        help="Include domain-specific coverage and payload-size diagnostics.",
    )
    arguments = parser.parse_args()
    for path in arguments.paths or DEFAULT_PATHS:
        if path.is_file():
            print(
                json.dumps(
                    inspect(path, details=arguments.details),
                    sort_keys=True,
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
