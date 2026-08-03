"""Copy the local SQLite research stores into Supabase (or any PostgreSQL).

The application already speaks PostgreSQL: every store opens through
``app.database.Database``, which switches backends on ``DATABASE_URL`` and
whose pooling is already tuned for Supabase's session pooler. What has been
missing is a way to carry existing local history across. This tool does that
and nothing else -- it never reads credentials from arguments (only from the
``DATABASE_URL`` environment variable, so a connection string never lands in
shell history), and it previews by default.

Schemas are created by the stores themselves: each store class is opened
against the target URL so its own versioned migrations run, which keeps the
remote schema byte-identical to the local one instead of duplicating DDL here.

Rows are copied in batches with ``ON CONFLICT DO NOTHING``, so an interrupted
run can simply be repeated. Sequences behind ``SERIAL`` columns are reset
afterwards so future inserts do not collide with copied identifiers.

Size matters on a managed host: ``quotes_history`` is roughly 8.5M rows and
7.8 GB locally, which exceeds Supabase's smaller tiers on its own. It is
therefore excluded unless ``--include-quote-history`` is passed. Everything
the research tooling reads day to day -- positions, journals, candidate
observations, ledger marks, model-lab evidence, settled outcomes -- is well
inside a small instance.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from app.database import Database

# Tables whose volume is dominated by retained order-book ladders. Excluded by
# default; the research tools only need them for tick-level replay.
HEAVY_TABLES = frozenset({"quotes_history", "states_history"})

BATCH_ROWS = 1_000


@dataclass(frozen=True, slots=True)
class StoreSpec:
    """One local database file and the schema it should occupy remotely."""

    name: str
    sqlite_path: Path
    postgres_schema: str | None = None
    # Import path of the store class whose migrations own this schema.
    store: tuple[str, str] | None = None
    store_kwargs: dict[str, Any] = field(default_factory=dict)


def default_stores(data_dir: Path = Path("workstation-data")) -> list[StoreSpec]:
    return [
        StoreSpec(
            "history",
            data_dir / "history.db",
            store=("app.history", "HistoryDB"),
        ),
        StoreSpec(
            "ledger",
            data_dir / "ledger.db",
            store=("app.ledger", "Ledger"),
        ),
        StoreSpec(
            "state",
            data_dir / "state.db",
            store=("app.monitor_state", "MonitorState"),
        ),
        StoreSpec(
            "model_lab",
            data_dir / "model-lab.db",
            store=("app.sport_model_lab", "SportModelLab"),
        ),
        StoreSpec(
            "trading_live",
            Path("polymarket-us-trading.db"),
            store=("app.polymarket_us_trading", "PolymarketUSAutoTrader"),
            store_kwargs={"key_id": "migration", "secret_key": "migration"},
        ),
        StoreSpec(
            "trading_dry_run",
            data_dir / "polymarket-us-dry-run.db",
            # The dry lane is schema-isolated exactly as the running server
            # isolates it, so the two lanes never share a table remotely.
            postgres_schema="polymarket_us_dry_run",
            store=("app.polymarket_us_trading", "PolymarketUSAutoTrader"),
            store_kwargs={
                "key_id": "migration",
                "secret_key": "migration",
                "database_namespace": "polymarket_us_dry_run",
            },
        ),
    ]


def redact(url: str) -> str:
    """Never echo credentials; show only the host and database."""
    return re.sub(r"//[^@]*@", "//[redacted]@", url)


def sqlite_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        """SELECT name FROM sqlite_master
           WHERE type='table' AND name NOT LIKE 'sqlite_%'
           ORDER BY name"""
    ).fetchall()
    return [row[0] for row in rows]


def table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def row_count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def batches(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    size: int = BATCH_ROWS,
) -> Iterator[list[tuple]]:
    quoted = ",".join(f'"{column}"' for column in columns)
    cursor = connection.execute(f'SELECT {quoted} FROM "{table}"')
    while True:
        rows = cursor.fetchmany(size)
        if not rows:
            return
        yield [tuple(row) for row in rows]


def create_remote_schema(spec: StoreSpec, database_url: str) -> None:
    """Let the store's own migrations build the remote schema."""
    if spec.store is None:
        return
    module_name, class_name = spec.store
    module = __import__(module_name, fromlist=[class_name])
    store = getattr(module, class_name)(database_url, **spec.store_kwargs)
    close = getattr(store, "close", None)
    if callable(close):
        close()


def copy_table(
    source: sqlite3.Connection,
    target: Database,
    table: str,
    columns: Sequence[str],
) -> int:
    quoted = ",".join(f'"{column}"' for column in columns)
    placeholders = ",".join(["%s"] * len(columns))
    statement = (
        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders}) '
        "ON CONFLICT DO NOTHING"
    )
    copied = 0
    for rows in batches(source, table, columns):
        with target.transaction() as cur:
            for row in rows:
                target.execute(cur, statement, row)
        copied += len(rows)
    return copied


def reset_sequences(target: Database, table: str, columns: Sequence[str]) -> None:
    """Advance identity sequences past the copied rows.

    Copied ``SERIAL`` values are inserted explicitly, which leaves the
    sequence at its old position; the next natural insert would then collide.
    """
    if "id" not in columns:
        return
    with target.transaction() as cur:
        target.execute(
            cur,
            """SELECT setval(
                   pg_get_serial_sequence(%s, 'id'),
                   COALESCE((SELECT MAX(id) FROM "%s"), 1),
                   true
               )
               WHERE pg_get_serial_sequence(%s, 'id') IS NOT NULL"""
            % ("%s", table, "%s"),
            (table, table),
        )


def plan(
    specs: Sequence[StoreSpec],
    *,
    include_heavy: bool,
) -> list[dict[str, Any]]:
    report = []
    for spec in specs:
        if not spec.sqlite_path.exists():
            report.append({"store": spec.name, "status": "missing", "tables": []})
            continue
        connection = read_only(spec.sqlite_path)
        try:
            tables = []
            for table in sqlite_tables(connection):
                skipped = table in HEAVY_TABLES and not include_heavy
                tables.append({
                    "table": table,
                    "rows": row_count(connection, table),
                    "skipped": skipped,
                })
        finally:
            connection.close()
        report.append({
            "store": spec.name,
            "status": "ready",
            "schema": spec.postgres_schema or "public",
            "size_mb": round(spec.sqlite_path.stat().st_size / 1048576, 1),
            "tables": tables,
        })
    return report


def migrate(
    specs: Sequence[StoreSpec],
    database_url: str,
    *,
    include_heavy: bool,
) -> list[dict[str, Any]]:
    results = []
    for spec in specs:
        if not spec.sqlite_path.exists():
            continue
        create_remote_schema(spec, database_url)
        target = Database(
            database_url,
            "postgres",
            postgres_schema=spec.postgres_schema,
        )
        source = read_only(spec.sqlite_path)
        try:
            for table in sqlite_tables(source):
                if table == "schema_migrations":
                    continue  # owned by the store's own migration ledger
                if table in HEAVY_TABLES and not include_heavy:
                    results.append({
                        "store": spec.name, "table": table, "status": "skipped",
                    })
                    continue
                columns = table_columns(source, table)
                local = row_count(source, table)
                copied = copy_table(source, target, table, columns)
                reset_sequences(target, table, columns)
                with target.cursor() as cur:
                    target.execute(cur, f'SELECT COUNT(*) FROM "{table}"')
                    remote = int(cur.fetchone()[0])
                results.append({
                    "store": spec.name,
                    "table": table,
                    "status": "copied" if remote >= local else "incomplete",
                    "local_rows": local,
                    "sent_rows": copied,
                    "remote_rows": remote,
                })
        finally:
            source.close()
            target.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the copy. Without it the tool only reports a plan.",
    )
    parser.add_argument(
        "--include-quote-history",
        action="store_true",
        help="Also copy quotes_history/states_history (~8.6M rows, ~7.8 GB).",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("workstation-data"))
    parser.add_argument(
        "--store",
        action="append",
        default=None,
        help="Limit to named store(s); repeatable.",
    )
    args = parser.parse_args()

    specs = default_stores(args.data_dir)
    if args.store:
        wanted = {name.strip() for name in args.store}
        specs = [spec for spec in specs if spec.name in wanted]
        if not specs:
            parser.error(f"no store matched {sorted(wanted)}")

    preview = plan(specs, include_heavy=args.include_quote_history)
    movable = 0
    for entry in preview:
        if entry["status"] != "ready":
            print(f"{entry['store']:16} MISSING {entry.get('schema', '')}")
            continue
        rows = sum(t["rows"] for t in entry["tables"] if not t["skipped"])
        skipped = sum(t["rows"] for t in entry["tables"] if t["skipped"])
        movable += rows
        print(
            f"{entry['store']:16} schema={entry['schema']:22} "
            f"{entry['size_mb']:8.1f} MB  copy {rows:>9,} rows"
            + (f"  (skipping {skipped:,})" if skipped else "")
        )
    print(f"{'TOTAL':16} {'':30} {'':11} copy {movable:>9,} rows")

    if not args.apply:
        print(
            "\nPreview only. Set DATABASE_URL to the Supabase connection string "
            "and re-run with --apply to copy."
        )
        return 0

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        parser.error(
            "DATABASE_URL is not set. Export the Supabase connection string in "
            "the environment (never pass it as an argument)."
        )
    print(f"\nTarget: {redact(database_url)}")
    results = migrate(
        specs,
        database_url,
        include_heavy=args.include_quote_history,
    )
    incomplete = [r for r in results if r["status"] == "incomplete"]
    for entry in results:
        if entry["status"] == "skipped":
            continue
        print(
            f"  {entry['store']:16} {entry['table']:30} "
            f"local {entry['local_rows']:>9,} -> remote {entry['remote_rows']:>9,} "
            f"[{entry['status']}]"
        )
    print(
        f"\n{len(results)} table(s) processed; "
        f"{len(incomplete)} incomplete."
    )
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
