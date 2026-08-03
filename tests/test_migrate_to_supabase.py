"""The Supabase migration plans honestly and never leaks credentials."""
import sqlite3
from pathlib import Path

from tools.migrate_to_supabase import (
    HEAVY_TABLES,
    StoreSpec,
    batches,
    default_stores,
    plan,
    redact,
    row_count,
    sqlite_tables,
    table_columns,
)


def _build(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE quotes_history (id INTEGER PRIMARY KEY, event_id TEXT);
        CREATE TABLE event_outcomes (event_id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE schema_migrations (component TEXT, version INTEGER);
        """
    )
    connection.executemany(
        "INSERT INTO quotes_history VALUES (?,?)",
        [(index, f"e{index}") for index in range(25)],
    )
    connection.executemany(
        "INSERT INTO event_outcomes VALUES (?,?)",
        [(f"e{index}", f"Game {index}") for index in range(4)],
    )
    connection.commit()
    connection.close()


def test_redact_removes_credentials_from_any_connection_string():
    assert redact(
        "postgresql://user:s3cret@db.example.supabase.co:5432/postgres"
    ) == "postgresql://[redacted]@db.example.supabase.co:5432/postgres"
    # A URL without credentials is untouched.
    assert redact("postgresql://localhost/postgres") == (
        "postgresql://localhost/postgres"
    )


def test_plan_reports_rows_and_defers_the_heavy_ladder_tables(tmp_path):
    source = tmp_path / "history.db"
    _build(source)
    spec = StoreSpec("history", source)

    deferred = plan([spec], include_heavy=False)[0]
    tables = {entry["table"]: entry for entry in deferred["tables"]}
    assert deferred["status"] == "ready"
    assert tables["quotes_history"]["rows"] == 25
    assert tables["quotes_history"]["skipped"] is True
    assert tables["event_outcomes"]["skipped"] is False

    included = plan([spec], include_heavy=True)[0]
    tables = {entry["table"]: entry for entry in included["tables"]}
    assert tables["quotes_history"]["skipped"] is False


def test_plan_marks_a_missing_store_instead_of_failing(tmp_path):
    report = plan([StoreSpec("absent", tmp_path / "nope.db")], include_heavy=False)
    assert report[0]["status"] == "missing"
    assert report[0]["tables"] == []


def test_table_introspection_round_trips(tmp_path):
    source = tmp_path / "history.db"
    _build(source)
    connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        assert sqlite_tables(connection) == [
            "event_outcomes",
            "quotes_history",
            "schema_migrations",
        ]
        assert table_columns(connection, "event_outcomes") == ["event_id", "name"]
        assert row_count(connection, "quotes_history") == 25
        chunks = list(batches(connection, "quotes_history", ["id"], size=10))
        assert [len(chunk) for chunk in chunks] == [10, 10, 5]
    finally:
        connection.close()


def test_default_stores_isolate_the_dry_lane_schema():
    specs = {spec.name: spec for spec in default_stores()}
    assert specs["trading_dry_run"].postgres_schema == "polymarket_us_dry_run"
    # The live lane keeps the default schema, exactly as the server runs it.
    assert specs["trading_live"].postgres_schema is None
    assert "quotes_history" in HEAVY_TABLES
