"""Report or drop retained order-book depth from aged quote history.

`quotes_history` keeps the full bid/ask ladder as JSON on every row.

Measured on the July 2026 workstation, before assuming this is the whole
problem: the store held 7.4 million quotes over 7 days (about 1 GB per day) in
a 7.04 GB file, and the ladders accounted for roughly 1.5 GB of that - about a
fifth. The remaining bulk is row count multiplied by a wide row: `quotes_history`
carries roughly 45 columns, many of them text identifiers and lineage fields.

So this tool is a partial lever, not the answer. Reclaiming the rest requires
retaining fewer rows - a longer capture throttle, or ageing out whole events -
which is a different decision with different evidence consequences.

Only `app/replay.py` reads the two ladder columns. Clearing them therefore
costs exactly one thing: a replayed tick for a pruned row sees an empty ladder
instead of the original depth. Every scalar the engine and the research
pipeline use - probability, bid, ask, sizes, fees, hashes, quarantine state,
lineage - is stored in its own column and is untouched.

That is a real loss of evidence, so this tool reports by default and requires
an explicit window plus a confirmation phrase to change anything. It never
deletes a row and never touches quotes inside the retention window.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import time


DEFAULT_PATH = Path("workstation-data/history.db")
CONFIRMATION = "PRUNE_HISTORY_DEPTH"
MINIMUM_WINDOW_DAYS = 2


def _cutoff(older_than_days: float, *, now: float | None = None) -> float:
    return (time.time() if now is None else now) - older_than_days * 86400.0


def inspect(path: Path, older_than_days: float, *, now: float | None = None) -> dict:
    resolved = path.expanduser().resolve()
    cutoff = _cutoff(older_than_days, now=now)
    with sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro", uri=True
    ) as connection:
        total, = connection.execute(
            "SELECT COUNT(*) FROM quotes_history"
        ).fetchone()
        eligible, depth_bytes = connection.execute(
            """SELECT COUNT(*),
                      COALESCE(SUM(
                          LENGTH(COALESCE(bid_levels_json,''))
                          + LENGTH(COALESCE(ask_levels_json,''))
                      ), 0)
               FROM quotes_history
               WHERE observed_at < ?
                 AND (bid_levels_json IS NOT NULL
                      OR ask_levels_json IS NOT NULL)""",
            (cutoff,),
        ).fetchone()
    return {
        "path": str(resolved),
        "bytes": resolved.stat().st_size,
        "older_than_days": older_than_days,
        "cutoff_epoch": cutoff,
        "total_quotes": int(total),
        "eligible_quotes": int(eligible),
        "reclaimable_depth_bytes": int(depth_bytes),
        "note": (
            "Reclaimable bytes are the stored ladder text. The file only "
            "shrinks after a separate VACUUM; until then the pages are free "
            "but still allocated."
        ),
    }


def prune(path: Path, older_than_days: float, *, now: float | None = None) -> dict:
    if older_than_days < MINIMUM_WINDOW_DAYS:
        raise RuntimeError(
            f"refusing to prune depth newer than {MINIMUM_WINDOW_DAYS} days; "
            "recent ladders are the ones replay is most likely to need"
        )
    resolved = path.expanduser().resolve()
    before = inspect(resolved, older_than_days, now=now)
    cutoff = float(before["cutoff_epoch"])
    connection = sqlite3.connect(str(resolved), timeout=1.0)
    try:
        # Fail rather than waiting against a running workstation.
        connection.execute("BEGIN EXCLUSIVE")
        cursor = connection.execute(
            """UPDATE quotes_history
               SET bid_levels_json=NULL, ask_levels_json=NULL
               WHERE observed_at < ?
                 AND (bid_levels_json IS NOT NULL
                      OR ask_levels_json IS NOT NULL)""",
            (cutoff,),
        )
        updated = cursor.rowcount
        connection.commit()
    except sqlite3.OperationalError as exc:
        connection.rollback()
        raise RuntimeError(
            f"{resolved} is busy; stop the workstation before pruning"
        ) from exc
    finally:
        connection.close()
    return {
        "before": before,
        "updated_quotes": int(updated),
        "after": inspect(resolved, older_than_days, now=now),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    parser.add_argument(
        "--older-than-days",
        type=float,
        required=True,
        help="Only quotes observed before this many days ago are eligible.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually clear the ladders. Without this the tool is read-only.",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"Required with --apply: {CONFIRMATION}",
    )
    arguments = parser.parse_args()
    if arguments.apply and arguments.confirm != CONFIRMATION:
        parser.error(f"--apply requires --confirm {CONFIRMATION}")
    if not arguments.path.is_file():
        parser.error(f"{arguments.path} is not a file")
    payload = (
        prune(arguments.path, arguments.older_than_days)
        if arguments.apply
        else inspect(arguments.path, arguments.older_than_days)
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
