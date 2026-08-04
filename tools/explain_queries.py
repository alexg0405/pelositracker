"""Check the recurring store queries against real query plans.

The optimization report proposed six composite/partial indexes. They were
explicitly labelled hypotheses, and they should be treated as such: an index
that does not match the actual predicate is dead weight that still has to be
maintained on every write. This tool populates a store with production-shaped
row counts, runs the queries the application really issues, and reports which
of them scan.

It then applies candidate indexes and re-runs, so an index is added only when it
demonstrably changes a plan and a timing -- not because a table "looks like it
needs one".

    python -m tools.explain_queries
    python -m tools.explain_queries --rows 200000

SQLite is used deliberately: `EXPLAIN QUERY PLAN` names the index it chose, and
the leading-column rules that decide whether a composite index is usable are the
same ones PostgreSQL applies to a btree. Absolute timings here are not
production numbers; the plan changes are the finding.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

# (label, sql, params) -- taken verbatim from the call sites noted in comments.
QUERIES: list[tuple[str, str, tuple]] = [
    # accounts.open_count, called on the finalization path per event.
    ("account_bets by event (open count)",
     "SELECT COUNT(*) FROM account_bets WHERE event_id=? AND status='open'",
     ("event-900",)),
    # accounts.mark_and_cash_out, called on EVERY decision for the event.
    ("account_bets by event (mark)",
     "SELECT * FROM account_bets WHERE event_id=? AND status='open'",
     ("event-900",)),
    # accounts.place exposure check, called on every decision that has entries.
    # This one sums over one account's whole open book, so it touches far more
    # rows than the event-scoped lookups and is the most index-sensitive.
    ("account_bets by account (exposure)",
     "SELECT COALESCE(SUM(stake),0) FROM account_bets "
     "WHERE account=? AND status='open'",
     ("alex",)),
    # accounts.place correlation check.
    ("account_bets by account+group",
     "SELECT COALESCE(SUM(stake),0) FROM account_bets "
     "WHERE account=? AND status='open' AND correlation_group=?",
     ("alex", "group-3")),
    # accounts.mark_and_cash_out join, per decision.
    ("account_bets join accounts",
     "SELECT b.*, a.strategy FROM account_bets b JOIN accounts a "
     "ON a.name=b.account WHERE b.event_id=? AND b.status='open'",
     ("event-900",)),
]

# Each variant is applied on its own, measured, then dropped, so the numbers
# are attributable to one index rather than to a pile of them.
VARIANTS: list[tuple[str, list[tuple[str, str]]]] = [
    ("report's proposal", [
        ("idx_account_bets_open_event",
         "CREATE INDEX IF NOT EXISTS idx_account_bets_open_event "
         "ON account_bets(account, event_id, placed_ts) WHERE status='open'"),
    ]),
    # Tested because the event-scoped predicates looked unserved. They are not:
    # the UNIQUE(account, event_id, ...) auto-index is already skip-scanned for
    # them. Kept as a recorded negative result -- this index earns nothing and
    # would still cost a write on every placed bet.
    ("event_id leading (rejected)", [
        ("idx_account_bets_event_status",
         "CREATE INDEX IF NOT EXISTS idx_account_bets_event_status "
         "ON account_bets(event_id, status)"),
    ]),
    # The expensive queries sum `stake` over one account's open book. Carrying
    # correlation_group and stake in the index makes both of them covering, so
    # the sum never touches the table.
    ("covering partial", [
        ("idx_account_bets_open_exposure",
         "CREATE INDEX IF NOT EXISTS idx_account_bets_open_exposure "
         "ON account_bets(account, correlation_group, stake) "
         "WHERE status='open'"),
    ]),
]


def build(path: Path, rows: int) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE accounts (name TEXT PRIMARY KEY, strategy TEXT);
        CREATE TABLE account_bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            event_id TEXT NOT NULL,
            market TEXT NOT NULL,
            outcome TEXT NOT NULL,
            stake DOUBLE PRECISION NOT NULL,
            placed_ts DOUBLE PRECISION NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            correlation_group TEXT,
            UNIQUE(account, event_id, market, outcome)
        );
        """
    )
    accounts = ["alex", "anthony", "kelly", "flat", "shadow", "research"]
    db.executemany("INSERT INTO accounts VALUES (?,?)",
                   [(name, "kelly") for name in accounts])
    rng = random.Random(20260804)
    batch = []
    for index in range(rows):
        # (account, event_id, market, outcome) must stay distinct or the UNIQUE
        # constraint silently swallows most of the rows and the whole
        # measurement is taken against a table two orders of magnitude too
        # small. event and market are derived so the tuple is unique by
        # construction: ~20 markets per event, which is the shape of a real
        # multi-market event.
        account = accounts[index % len(accounts)]
        event = f"event-{index // 20}"
        market = f"market-{index % 20}"
        # A long-running deployment is overwhelmingly settled history with a
        # small live tail; an index that only helps the tail must still be
        # selective against the bulk.
        status = "open" if rng.random() < 0.05 else "win"
        batch.append((account, event, market, "home", 10.0, 1.7e9 + index,
                      status, f"group-{index % 12}"))
    db.executemany(
        "INSERT INTO account_bets "
        "(account,event_id,market,outcome,stake,placed_ts,status,correlation_group) "
        "VALUES (?,?,?,?,?,?,?,?)", batch)
    db.commit()
    db.execute("ANALYZE")
    db.commit()
    db.close()


def plan(db: sqlite3.Connection, sql: str, params: tuple) -> str:
    rows = db.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return " | ".join(str(row[-1]) for row in rows)


def timed(db: sqlite3.Connection, sql: str, params: tuple, repeat: int = 30) -> float:
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        db.execute(sql, params).fetchall()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples) * 1000


def report(db: sqlite3.Connection, title: str) -> dict[str, tuple[str, float]]:
    print(f"\n{title}")
    results = {}
    for label, sql, params in QUERIES:
        detail = plan(db, sql, params)
        elapsed = timed(db, sql, params)
        scans = "SCAN" in detail and "SEARCH" not in detail
        flag = "FULL SCAN" if scans else "index"
        print(f"  {label:38s} {elapsed:8.3f} ms  [{flag}]")
        print(f"      {detail}")
        results[label] = (detail, elapsed)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "plans.db"
        build(path, args.rows)
        db = sqlite3.connect(path)
        try:
            print(f"account_bets rows: "
                  f"{db.execute('SELECT COUNT(*) FROM account_bets').fetchone()[0]:,}")

            results = {
                "baseline": report(
                    db, "BASELINE (only the UNIQUE constraint's auto-index)")
            }
            for title, indexes in VARIANTS:
                for _, sql in indexes:
                    db.execute(sql)
                db.execute("ANALYZE")
                results[title] = report(db, f"WITH: {title}")
                # Dropped again so each variant is measured alone.
                for name, _ in indexes:
                    db.execute(f"DROP INDEX {name}")
                db.execute("ANALYZE")

            columns = ["baseline", *(title for title, _ in VARIANTS)]
            print("\nSUMMARY (median ms)")
            header = "".join(f"{name[:20]:>22s}" for name in columns)
            print(f"  {'query':38s}{header}")
            for label, _, _ in QUERIES:
                cells = "".join(
                    f"{results[name][label][1]:22.3f}" for name in columns)
                print(f"  {label:38s}{cells}")
        finally:
            db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
