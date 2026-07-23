"""Small PostgreSQL/SQLite compatibility layer for local persistence.

Production uses ``DATABASE_URL`` and psycopg2.  Local development and tests
fall back to SQLite files without making every store duplicate two versions of
the same SQL and transaction handling.
"""
from __future__ import annotations

import os
import re
import sqlite3
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

try:  # SQLite-only installs should not need a PostgreSQL driver at import time.
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover - requirements include psycopg2 in CI
    psycopg2 = None


_SERIAL_PRIMARY_KEY = re.compile(r"\bSERIAL\s+PRIMARY\s+KEY\b", re.IGNORECASE)

# Postgres errors that mean the socket is dead -- typically a managed pooler
# (e.g. Supabase) dropping an idle connection between polling cycles. We reconnect
# on these so a long-lived, single-worker server heals without a manual restart.
_CONNECTION_ERRORS: tuple = (
    (psycopg2.OperationalError, psycopg2.InterfaceError) if psycopg2 is not None else ()
)

# TCP keepalives stop a managed pooler from silently dropping the app's idle
# connection in the first place.
_KEEPALIVES = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
}


def _looks_postgres(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith(("postgres://", "postgresql://")) or (
        "=" in lowered and any(part in lowered for part in ("dbname=", "host=", "user="))
    )


class Database:
    """A connection plus the few dialect operations used by the app stores."""

    def __init__(self, target: str, backend: str):
        self.target = target
        self.backend = backend
        if backend == "postgres":
            if psycopg2 is None:  # pragma: no cover - only on incomplete installs
                raise RuntimeError("psycopg2 is required when DATABASE_URL is configured")
            self.connection = self._connect_postgres()
        else:
            if target != ":memory:" and not target.startswith("file:"):
                parent = Path(target).expanduser().resolve().parent
                parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(
                target,
                check_same_thread=False,
                uri=target.startswith("file:"),
                timeout=10,
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA busy_timeout = 10000")
            # WAL lets readers (position/history lookups on the dashboard fan-out)
            # proceed without blocking on the single writer thread. It's a
            # persistent, per-file setting; skip in-memory DBs where it doesn't apply.
            if target != ":memory:":
                self.connection.execute("PRAGMA journal_mode = WAL")

    @classmethod
    def open(
        cls,
        explicit_path: str | None,
        *,
        sqlite_envs: Sequence[str],
        sqlite_default: str,
    ) -> "Database":
        # An explicit constructor argument is intentionally authoritative so
        # tests and maintenance tools can select an isolated SQLite file even
        # on a machine that also has DATABASE_URL configured.
        if explicit_path is not None:
            target = os.fspath(explicit_path)
            return cls(target, "postgres" if _looks_postgres(target) else "sqlite")

        database_url = os.getenv("DATABASE_URL")
        if database_url:
            return cls(database_url, "postgres")

        target = next((os.getenv(name) for name in sqlite_envs if os.getenv(name)), sqlite_default)
        return cls(os.fspath(target), "sqlite")

    def sql(self, statement: str) -> str:
        return statement if self.backend == "postgres" else statement.replace("%s", "?")

    def initialize(self, schema: str, *, component: str = "legacy", version: int = 1) -> None:
        """Apply one immutable, versioned schema migration.

        Every store records its own component/version/checksum in the shared
        migration ledger, so stores can safely share a database.
        """
        if not re.fullmatch(r"[a-z][a-z0-9_]*", component) or version < 1:
            raise ValueError("invalid migration identity")
        checksum = hashlib.sha256(schema.encode("utf-8")).hexdigest()
        ledger = """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            component TEXT NOT NULL,
            version INTEGER NOT NULL,
            checksum TEXT NOT NULL,
            applied_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (component, version)
        )
        """
        import time
        if self.backend == "postgres":
            with self.transaction() as cur:
                cur.execute(ledger)
                cur.execute(
                    "SELECT checksum FROM schema_migrations WHERE component=%s AND version=%s",
                    (component, version),
                )
                existing = cur.fetchone()
                if existing:
                    if existing[0] != checksum:
                        raise RuntimeError(f"migration checksum mismatch: {component} v{version}")
                    return
                cur.execute(schema)
                cur.execute(
                    "INSERT INTO schema_migrations(component, version, checksum, applied_at) "
                    "VALUES (%s,%s,%s,%s)",
                    (component, version, checksum, time.time()),
                )
            return

        sqlite_schema = _SERIAL_PRIMARY_KEY.sub("INTEGER PRIMARY KEY AUTOINCREMENT", schema)
        try:
            self.connection.execute(ledger)
            self.connection.commit()
            existing = self.connection.execute(
                "SELECT checksum FROM schema_migrations WHERE component=? AND version=?",
                (component, version),
            ).fetchone()
            if existing:
                if existing[0] != checksum:
                    raise RuntimeError(f"migration checksum mismatch: {component} v{version}")
                return
            safe_component = component.replace("'", "''")
            safe_checksum = checksum.replace("'", "''")
            self.connection.executescript(
                "BEGIN;\n"
                f"{sqlite_schema}\n"
                "INSERT INTO schema_migrations(component, version, checksum, applied_at) "
                f"VALUES ('{safe_component}',{int(version)},'{safe_checksum}',{time.time()});\n"
                "COMMIT;"
            )
        except BaseException:
            self.connection.rollback()
            raise

    def columns(self, table: str) -> set[str]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise ValueError("unsafe table name")
        with self.cursor() as cur:
            if self.backend == "postgres":
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name=%s",
                    (table,),
                )
                return {row[0] for row in cur.fetchall()}
            cur.execute(f"PRAGMA table_info({table})")
            return {row[1] for row in cur.fetchall()}

    def ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        """Compatibility migration for databases created before the ledger."""
        existing = self.columns(table)
        with self.transaction() as cur:
            for name, sql_type in columns.items():
                if name not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    def migrate_columns(self, component: str, version: int,
                        tables: dict[str, dict[str, str]]) -> None:
        """Transactionally add compatibility columns and record their checksum."""
        if not re.fullmatch(r"[a-z][a-z0-9_]*", component) or version < 1:
            raise ValueError("invalid migration identity")
        for table, columns in tables.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                raise ValueError("unsafe migration table")
            for name, sql_type in columns.items():
                if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                        or not re.fullmatch(r"[A-Z][A-Z0-9 ]*", sql_type)):
                    raise ValueError("unsafe migration column")
        descriptor = json.dumps(tables, sort_keys=True, separators=(",", ":"))
        checksum = hashlib.sha256(descriptor.encode("utf-8")).hexdigest()
        import time
        with self.transaction() as cur:
            self.execute(
                cur,
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    component TEXT NOT NULL, version INTEGER NOT NULL,
                    checksum TEXT NOT NULL, applied_at DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY(component, version))""",
            )
            self.execute(
                cur, "SELECT checksum FROM schema_migrations "
                     "WHERE component=%s AND version=%s", (component, version))
            existing_migration = cur.fetchone()
            if existing_migration:
                if existing_migration[0] != checksum:
                    raise RuntimeError(f"migration checksum mismatch: {component} v{version}")
                return
            for table, columns in tables.items():
                if self.backend == "postgres":
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema=current_schema() AND table_name=%s", (table,))
                    existing = {row[0] for row in cur.fetchall()}
                else:
                    cur.execute(f"PRAGMA table_info({table})")
                    existing = {row[1] for row in cur.fetchall()}
                for name, sql_type in columns.items():
                    if name not in existing:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
            self.execute(
                cur,
                "INSERT INTO schema_migrations(component, version, checksum, applied_at) "
                "VALUES (%s,%s,%s,%s)", (component, version, checksum, time.time()),
            )

    def _connect_postgres(self):
        connection = psycopg2.connect(self.target, **_KEEPALIVES)
        connection.autocommit = False
        return connection

    def _reconnect(self) -> None:
        """Drop and reopen a dead Postgres connection so the next call succeeds."""
        if self.backend != "postgres":
            return
        try:
            self.connection.close()
        except Exception:
            pass
        self.connection = self._connect_postgres()

    def _cursor(self, dict_rows: bool = False):
        # Reopen first if a managed pooler dropped the idle connection, so the app
        # recovers without a restart.
        if self.backend == "postgres" and self.connection.closed:
            self._reconnect()
        if self.backend == "postgres" and dict_rows:
            return self.connection.cursor(cursor_factory=psycopg2.extras.DictCursor)
        return self.connection.cursor()

    def _safe_rollback(self) -> None:
        try:
            self.connection.rollback()
        except Exception:
            self._reconnect()

    @contextmanager
    def cursor(self, *, dict_rows: bool = False) -> Iterator:
        cur = self._cursor(dict_rows)
        try:
            yield cur
            # psycopg starts a transaction for SELECTs too; end that snapshot
            # promptly so a long-lived app connection never sits idle in one.
            self.connection.commit()
        except _CONNECTION_ERRORS:
            self._reconnect()  # dead socket: heal for the next call, then surface
            raise
        except BaseException:
            self._safe_rollback()
            raise
        finally:
            try:
                cur.close()
            except Exception:
                pass

    @contextmanager
    def transaction(self, *, dict_rows: bool = False) -> Iterator:
        cur = self._cursor(dict_rows)
        try:
            yield cur
            self.connection.commit()
        except _CONNECTION_ERRORS:
            self._reconnect()
            raise
        except BaseException:
            self._safe_rollback()
            raise
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def execute(self, cur, statement: str, params: Sequence | None = None):
        if params is None:
            return cur.execute(self.sql(statement))
        return cur.execute(self.sql(statement), params)

    def execute_many(self, cur, statement: str, rows: Iterable[Sequence]) -> None:
        if self.backend == "postgres":
            psycopg2.extras.execute_batch(cur, statement, rows)
        else:
            cur.executemany(self.sql(statement), rows)

    def close(self) -> None:
        self.connection.close()
