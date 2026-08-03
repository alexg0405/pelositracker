"""Shadow Venn-Abers interval report over settled managed positions.

Joins closed full-game positions to settled outcomes (the settlement
counterfactual's grading), then fits per-(line, stage) inductive Venn-Abers
intervals on the model-implied entry probabilities. The output answers, from
evidence: on which lines and stages is the recorded edge tight enough to act
on, and where does the interval span the fee-implied break-even — a refusal.

Read-only; nothing here touches policy, gates, or the lanes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.venn_abers import shadow_report
from tools.settlement_counterfactual import (
    calibration_pairs,
    grade_positions,
    load_closed_positions,
    load_outcomes,
)


def run(trading_db: Path, history_db: Path) -> dict[str, object]:
    outcomes = load_outcomes(history_db)
    positions = load_closed_positions(trading_db)
    graded, skipped = grade_positions(positions, outcomes)
    report = shadow_report(calibration_pairs(graded))
    report["trading_database"] = str(trading_db)
    report["history_database"] = str(history_db)
    report["grading_skipped"] = skipped
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "trading_db",
        nargs="?",
        type=Path,
        default=Path("polymarket-us-trading.db"),
        help="Lane trading database (live default; pass "
        "workstation-data/polymarket-us-dry-run.db for the dry lane)",
    )
    parser.add_argument(
        "--history-db",
        type=Path,
        default=Path("workstation-data/history.db"),
        help="History database carrying event_outcomes",
    )
    args = parser.parse_args()
    print(json.dumps(run(args.trading_db, args.history_db), indent=2,
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
