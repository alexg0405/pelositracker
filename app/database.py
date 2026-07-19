"""Small PostgreSQL/SQLite compatibility layer for local persistence.

Production uses ``DATABASE_URL`` and psycopg2.  Local development and tests
fall back to SQLite files without making every store duplicate two versions of
the same SQL and transaction handling.
"""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

try:  # SQLite-only installs should not need a PostgreSQL driver at import time.
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover - requirements include psycopg2 in CI
    psycopg2 = None


_SERIAL_PRIMARY_KEY = re.compile(r"\bSERIAL\s+PRIMARY\s+KEY\b", re.IGNORECASE)


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
            self.connection = psycopg2.connect(target)
            self.connection.autocommit = False
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

    def initialize(self, schema: str) -> None:
        if self.backend == "postgres":
            with self.transaction() as cur:
                cur.execute(schema)
            return

        sqlite_schema = _SERIAL_PRIMARY_KEY.sub("INTEGER PRIMARY KEY AUTOINCREMENT", schema)
        try:
            self.connection.executescript(f"BEGIN;\n{sqlite_schema}\nCOMMIT;")
        except BaseException:
            self.connection.rollback()
            raise

    def _cursor(self, dict_rows: bool = False):
        if self.backend == "postgres" and dict_rows:
            return self.connection.cursor(cursor_factory=psycopg2.extras.DictCursor)
        return self.connection.cursor()

    @contextmanager
    def cursor(self, *, dict_rows: bool = False) -> Iterator:
        cur = self._cursor(dict_rows)
        try:
            yield cur
            # psycopg starts a transaction for SELECTs too; end that snapshot
            # promptly so a long-lived app connection never sits idle in one.
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            cur.close()

    @contextmanager
    def transaction(self, *, dict_rows: bool = False) -> Iterator:
        cur = self._cursor(dict_rows)
        try:
            yield cur
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            cur.close()

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
