"""Mirror the hosted (Supabase) trading data into local SQLite for research.

The inverse of ``tools/migrate_to_supabase.py``: the website persists its
lanes to ``DATABASE_URL`` (live in the default schema, the dry lane in
``polymarket_us_dry_run``, the Alex lane in ``polymarket_us_alex``), and this
tool copies those rows down into local mirror files so every existing
settlement-grading and policy-analysis script runs on hosted trades exactly
as it runs on workstation trades -- just pointed at the mirror paths.

Safety model:

- The PostgreSQL session is opened read-only; this tool cannot write, alter,
  or lock anything on the hosted side.
- The connection string is never read from arguments (only the
  ``DATABASE_URL`` environment variable, or the same key in a local ``.env``
  when the variable is unset), and it is never echoed -- output redacts
  credentials.
- Mirrors are plain new files under ``workstation-data/hosted-mirror`` and
  are rebuilt from scratch each pull. The tool refuses to write over the
  workstation's own lane databases, so a mirror can never masquerade as the
  local ledger of record.

Missing schemas or tables (for example a site that has not yet enabled a
lane) are reported and skipped, which makes the tool a connectivity check as
well as a sync.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

# Everything the research tooling reads day to day. quotes_history-scale
# tables live only in the history store and are deliberately not mirrored.
TRADING_TABLES = (
    "live_managed_positions",
    "live_managed_orders",
    "live_trading_journal",
    "trading_policy_sessions",
    "trading_policy_advice",
    "live_trading_config",
    "exit_recovery_observations",
    "adaptive_exit_observations",
    "candidate_observations",
)
HISTORY_TABLES = ("event_outcomes",)

# The workstation's real lane databases; a mirror must never land on them.
PROTECTED = frozenset(
    Path(p).resolve()
    for p in (
        "polymarket-us-trading.db",
        "polymarket-us-alex.db",
        "workstation-data/polymarket-us-dry-run.db",
        "workstation-data/history.db",
    )
)

BATCH_ROWS = 5_000


@dataclass(frozen=True, slots=True)
class LaneSpec:
    name: str
    postgres_schema: str  # "public" is the hosted live lane's home
    mirror_file: str
    tables: tuple[str, ...]


LANES = (
    LaneSpec("live", "public", "hosted-live.db", TRADING_TABLES),
    LaneSpec(
        "dry_run",
        "polymarket_us_dry_run",
        "hosted-dry-run.db",
        TRADING_TABLES,
    ),
    LaneSpec("alex", "polymarket_us_alex", "hosted-alex.db", TRADING_TABLES),
    LaneSpec("history", "public", "hosted-history.db", HISTORY_TABLES),
)


def redact(url: str) -> str:
    """Never echo credentials; show only the host and database."""
    return re.sub(r"//[^@]*@", "//[redacted]@", url)


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser for a local .env; no interpolation."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def database_url(env_file: Path) -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        url = read_env_file(env_file).get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set (environment or "
            f"{env_file}); nothing to pull from."
        )
    if not url.startswith(("postgres://", "postgresql://")):
        raise SystemExit(
            "DATABASE_URL does not look like a PostgreSQL DSN; refusing."
        )
    return url


def adapt(value):
    """Convert psycopg2 result values into what sqlite3 can bind."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, memoryview):
        return bytes(value)
    return value


def remote_tables(cursor, schema: str) -> set[str]:
    cursor.execute(
        """SELECT table_name FROM information_schema.tables
           WHERE table_schema = %s AND table_type = 'BASE TABLE'""",
        (schema,),
    )
    return {row[0] for row in cursor.fetchall()}


def remote_columns(cursor, schema: str, table: str) -> list[str]:
    cursor.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema = %s AND table_name = %s
           ORDER BY ordinal_position""",
        (schema, table),
    )
    return [row[0] for row in cursor.fetchall()]


def mirror_path(out_dir: Path, filename: str) -> Path:
    path = (out_dir / filename).resolve()
    if path in PROTECTED:
        raise SystemExit(
            f"refusing to write mirror over the workstation database {path}"
        )
    return path


def pull_lane(connection, lane: LaneSpec, out_dir: Path) -> list[str]:
    lines: list[str] = []
    with connection.cursor() as cursor:
        present = remote_tables(cursor, lane.postgres_schema)
        wanted = [t for t in lane.tables if t in present]
        missing = [t for t in lane.tables if t not in present]
        if not wanted:
            lines.append(
                f"{lane.name}: no tables in schema "
                f"{lane.postgres_schema!r} yet - skipped (lane not "
                "enabled on the site, or nothing has run there)"
            )
            return lines
        path = mirror_path(out_dir, lane.mirror_file)
        out_dir.mkdir(parents=True, exist_ok=True)
        local = sqlite3.connect(path)
        try:
            for table in wanted:
                columns = remote_columns(
                    cursor, lane.postgres_schema, table
                )
                quoted = ",".join(f'"{c}"' for c in columns)
                local.execute(f'DROP TABLE IF EXISTS "{table}"')
                local.execute(f'CREATE TABLE "{table}" ({quoted})')
                placeholders = ",".join("?" for _ in columns)
                copied = 0
                # Named cursor = server-side; the whole table never sits in
                # this process's memory at once.
                with connection.cursor(
                    name=f"pull_{lane.name}_{table}"
                ) as stream:
                    stream.itersize = BATCH_ROWS
                    stream.execute(
                        f'SELECT {quoted} FROM '
                        f'"{lane.postgres_schema}"."{table}"'
                    )
                    while True:
                        rows = stream.fetchmany(BATCH_ROWS)
                        if not rows:
                            break
                        local.executemany(
                            f'INSERT INTO "{table}" VALUES ({placeholders})',
                            [tuple(adapt(v) for v in row) for row in rows],
                        )
                        copied += len(rows)
                local.commit()
                lines.append(f"{lane.name}: {table} -> {copied} rows")
            if missing:
                lines.append(
                    f"{lane.name}: not present remotely: "
                    + ", ".join(missing)
                )
            lines.append(f"{lane.name}: mirror written to {path}")
        finally:
            local.close()
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="fallback source for DATABASE_URL when the variable is unset",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("workstation-data/hosted-mirror"),
        help="directory the mirror .db files are written into",
    )
    parser.add_argument(
        "--lanes",
        nargs="+",
        choices=[lane.name for lane in LANES],
        default=[lane.name for lane in LANES],
        help="which hosted lanes to mirror",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="only show remote row counts; write nothing",
    )
    args = parser.parse_args()

    try:
        import psycopg2
    except ImportError as error:  # pragma: no cover - environment guard
        raise SystemExit(
            "psycopg2 is required (pip install psycopg2-binary)"
        ) from error

    url = database_url(args.env_file)
    print(f"connecting read-only to {redact(url)}")
    connection = psycopg2.connect(url)
    try:
        connection.set_session(readonly=True, autocommit=True)
        selected = [lane for lane in LANES if lane.name in args.lanes]
        if args.list:
            with connection.cursor() as cursor:
                for lane in selected:
                    present = remote_tables(cursor, lane.postgres_schema)
                    for table in lane.tables:
                        if table not in present:
                            continue
                        cursor.execute(
                            f'SELECT COUNT(*) FROM '
                            f'"{lane.postgres_schema}"."{table}"'
                        )
                        count = cursor.fetchone()[0]
                        print(f"{lane.name}: {table} = {count} rows")
            return
        for lane in selected:
            for line in pull_lane(connection, lane, args.out_dir):
                print(line)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
