"""Persistence for tracked events and automation settings."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import fields
from datetime import datetime

from .database import Database
from .models import Event, as_json


_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracked_events (
    event_id   TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_ts DOUBLE PRECISION NOT NULL
);
"""


class MonitorState:
    def __init__(self, path: str | None = None):
        self._db = Database.open(
            path, sqlite_envs=("STATE_DB", "LEDGER_DB"), sqlite_default="ledger.db"
        )
        self.path = self._db.target
        self.backend = self._db.backend
        self._lock = threading.Lock()
        with self._lock:
            self._db.initialize(_SCHEMA, component="monitor_state", version=1)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def auto_monitor(self, default: bool = False) -> bool:
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(cur, "SELECT value FROM monitor_config WHERE key=%s",
                                 ("auto_monitor",))
                row = cur.fetchone()
        return default if row is None else str(row[0]).casefold() == "true"

    def set_auto_monitor(self, enabled: bool) -> None:
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO monitor_config (key, value) VALUES (%s,%s)
                       ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""",
                    ("auto_monitor", "true" if enabled else "false"),
                )

    def odds_api_enabled(self, default: bool = True) -> bool:
        """Return the persisted master switch for paid Odds API polling."""
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    "SELECT value FROM monitor_config WHERE key=%s",
                    ("odds_api_enabled",),
                )
                row = cur.fetchone()
        return default if row is None else str(row[0]).casefold() == "true"

    def set_odds_api_enabled(self, enabled: bool) -> None:
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO monitor_config (key, value) VALUES (%s,%s)
                       ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""",
                    ("odds_api_enabled", "true" if enabled else "false"),
                )

    def odds_api_poll_seconds(self, default: float = 45.0) -> float:
        """Return the persisted paid-feed interval, bounded to the UI policy."""
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    "SELECT value FROM monitor_config WHERE key=%s",
                    ("odds_api_poll_seconds",),
                )
                row = cur.fetchone()
        if row is None:
            return max(1.0, min(3600.0, float(default)))
        try:
            value = float(row[0])
        except (TypeError, ValueError):
            return max(1.0, min(3600.0, float(default)))
        return max(1.0, min(3600.0, value))

    def set_odds_api_poll_seconds(self, seconds: float) -> None:
        value = max(1.0, min(3600.0, float(seconds)))
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO monitor_config (key, value) VALUES (%s,%s)
                       ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""",
                    ("odds_api_poll_seconds", f"{value:g}"),
                )

    def config_json(self, key: str) -> dict | None:
        """Return a persisted JSON config object, or None when absent/invalid."""
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    "SELECT value FROM monitor_config WHERE key=%s",
                    (key,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row[0]))
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def set_config_json(self, key: str, value: dict | None) -> None:
        """Persist a JSON config object; None deletes the key."""
        with self._lock:
            with self._db.transaction() as cur:
                if value is None:
                    self._db.execute(
                        cur,
                        "DELETE FROM monitor_config WHERE key=%s",
                        (key,),
                    )
                else:
                    self._db.execute(
                        cur,
                        """INSERT INTO monitor_config (key, value) VALUES (%s,%s)
                           ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""",
                        (key, json.dumps(value, separators=(",", ":"))),
                    )

    def save_event(self, event: Event) -> None:
        payload = json.dumps(as_json(event), separators=(",", ":"))
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO tracked_events (event_id, payload, updated_ts)
                       VALUES (%s,%s,%s) ON CONFLICT(event_id) DO UPDATE SET
                       payload=EXCLUDED.payload, updated_ts=EXCLUDED.updated_ts""",
                    (event.id, payload, time.time()),
                )

    def delete_event(self, event_id: str) -> None:
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(cur, "DELETE FROM tracked_events WHERE event_id=%s", (event_id,))

    def events(self) -> list[Event]:
        known = {field.name for field in fields(Event)}
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(cur, "SELECT payload FROM tracked_events ORDER BY updated_ts")
                payloads = [json.loads(row[0]) for row in cur.fetchall()]
        restored = []
        for payload in payloads:
            values = {key: value for key, value in payload.items() if key in known}
            if isinstance(values.get("created_at"), str):
                values["created_at"] = datetime.fromisoformat(values["created_at"])
            restored.append(Event(**values))
        return restored
