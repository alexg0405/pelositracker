import sqlite3
import time

import pytest

from tools.prune_history_depth import (
    MINIMUM_WINDOW_DAYS,
    inspect,
    prune,
)


def _history(tmp_path, now):
    path = tmp_path / "history.db"
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE quotes_history (
            id INTEGER PRIMARY KEY, event_id TEXT, probability REAL,
            bid REAL, ask REAL, observed_at REAL,
            bid_levels_json TEXT, ask_levels_json TEXT
        )"""
    )
    rows = [
        # (age in days, has depth)
        (30.0, True), (30.0, True), (10.0, True), (0.5, True), (0.1, True),
    ]
    for index, (age, depth) in enumerate(rows):
        con.execute(
            "INSERT INTO quotes_history VALUES (?,?,?,?,?,?,?,?)",
            (
                index, "event-1", 0.55, 0.54, 0.56, now - age * 86400,
                '[{"p":0.54,"s":100}]' if depth else None,
                '[{"p":0.56,"s":100}]' if depth else None,
            ),
        )
    con.commit()
    con.close()
    return path


def test_inspect_is_read_only_and_scopes_to_the_window(tmp_path):
    now = time.time()
    path = _history(tmp_path, now)

    report = inspect(path, older_than_days=7, now=now)

    assert report["total_quotes"] == 5
    # Only the three rows older than seven days are eligible.
    assert report["eligible_quotes"] == 3
    assert report["reclaimable_depth_bytes"] > 0
    # Nothing was modified by inspecting.
    con = sqlite3.connect(path)
    assert con.execute(
        "SELECT COUNT(*) FROM quotes_history WHERE bid_levels_json IS NOT NULL"
    ).fetchone()[0] == 5


def test_prune_clears_only_aged_depth_and_keeps_every_row_and_scalar(tmp_path):
    now = time.time()
    path = _history(tmp_path, now)

    result = prune(path, older_than_days=7, now=now)

    assert result["updated_quotes"] == 3
    con = sqlite3.connect(path)
    # No row is ever deleted.
    assert con.execute("SELECT COUNT(*) FROM quotes_history").fetchone()[0] == 5
    # Recent ladders are untouched.
    assert con.execute(
        "SELECT COUNT(*) FROM quotes_history WHERE bid_levels_json IS NOT NULL"
    ).fetchone()[0] == 2
    # Every scalar the engine and research use survives on pruned rows.
    aged = con.execute(
        """SELECT probability, bid, ask, event_id FROM quotes_history
           WHERE bid_levels_json IS NULL"""
    ).fetchall()
    assert len(aged) == 3
    assert all(row == (0.55, 0.54, 0.56, "event-1") for row in aged)
    assert result["after"]["eligible_quotes"] == 0


def test_prune_refuses_a_window_that_would_touch_recent_depth(tmp_path):
    now = time.time()
    path = _history(tmp_path, now)

    with pytest.raises(RuntimeError, match="refusing to prune depth newer"):
        prune(path, older_than_days=MINIMUM_WINDOW_DAYS - 0.5, now=now)

    con = sqlite3.connect(path)
    assert con.execute(
        "SELECT COUNT(*) FROM quotes_history WHERE bid_levels_json IS NOT NULL"
    ).fetchone()[0] == 5
