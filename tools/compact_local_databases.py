"""Inspect or explicitly compact free pages in local SQLite databases.

This tool never deletes research rows. VACUUM rewrites a database to reclaim
pages already freed by normal retention/deletion, so the server must be stopped
and enough temporary disk space must be available.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sqlite3


DEFAULT_PATHS = (
    Path("workstation-data/history.db"),
    Path("workstation-data/ledger.db"),
    Path("workstation-data/model-lab.db"),
    Path("workstation-data/polymarket-us-trading.db"),
    Path("polymarket-us-trading.db"),
)
CONFIRMATION = "COMPACT_LOCAL_DATABASES"


def inspect(path: Path) -> dict[str, int | str | float]:
    resolved = path.expanduser().resolve()
    with sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True) as connection:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
    return {
        "path": str(resolved),
        "bytes": resolved.stat().st_size,
        "page_size": page_size,
        "page_count": page_count,
        "free_pages": free_pages,
        "reclaimable_bytes_estimate": free_pages * page_size,
        "reclaimable_fraction": free_pages / page_count if page_count else 0.0,
    }


def compact(path: Path) -> dict[str, object]:
    resolved = path.expanduser().resolve()
    before = inspect(resolved)
    required_free = int(before["bytes"]) + 256 * 1024 * 1024
    available = shutil.disk_usage(resolved.parent).free
    if available < required_free:
        raise RuntimeError(
            f"{resolved} needs approximately {required_free:,} free bytes to "
            f"compact safely; only {available:,} are available"
        )
    connection = sqlite3.connect(str(resolved), timeout=1.0)
    try:
        # Fail rather than waiting against a running workstation.
        connection.execute("BEGIN EXCLUSIVE")
        connection.rollback()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"{resolved} is busy; stop the workstation before compacting"
        ) from exc
    finally:
        connection.close()
    return {"before": before, "after": inspect(resolved)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually VACUUM files. Without this flag the tool is read-only.",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"Required with --apply: {CONFIRMATION}",
    )
    arguments = parser.parse_args()
    paths = [path for path in (arguments.paths or DEFAULT_PATHS) if path.is_file()]
    if arguments.apply and arguments.confirm != CONFIRMATION:
        parser.error(f"--apply requires --confirm {CONFIRMATION}")
    for path in paths:
        payload = compact(path) if arguments.apply else inspect(path)
        print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
