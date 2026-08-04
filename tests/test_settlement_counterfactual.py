"""The settlement counterfactual tool grades exits against settled outcomes."""
import json
import sqlite3
from pathlib import Path

import pytest

from tools.settlement_counterfactual import (
    bootstrap_giveback,
    calibration_pairs,
    grade_positions,
    run,
)

_POSITIONS_SCHEMA = """
CREATE TABLE live_managed_positions (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    market_type TEXT NOT NULL,
    market_scope TEXT,
    selection TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    entry_cost DOUBLE PRECISION NOT NULL,
    current_exit_value DOUBLE PRECISION,
    exit_reason TEXT,
    closed_ts DOUBLE PRECISION,
    realized_pnl DOUBLE PRECISION,
    entry_signal_edge DOUBLE PRECISION,
    entry_execution_edge DOUBLE PRECISION,
    entry_signal_quality DOUBLE PRECISION,
    entry_game_fraction_remaining DOUBLE PRECISION
);
"""

_OUTCOMES_SCHEMA = """
CREATE TABLE event_outcomes (
    event_id TEXT PRIMARY KEY,
    name TEXT,
    sport TEXT,
    home TEXT,
    away TEXT,
    final_home_score DOUBLE PRECISION,
    final_away_score DOUBLE PRECISION
);
"""


def _position(
    position_id: str,
    event_id: str,
    market_type: str,
    selection: str,
    *,
    entry: float,
    quantity: float,
    exit_value: float,
    reason: str = "hard_stop_loss",
    scope: str = "full_game",
    fraction: float | None = 0.8,
) -> tuple:
    return (
        position_id,
        "closed",
        event_id,
        "Event " + event_id,
        market_type,
        scope,
        selection,
        quantity,
        entry,
        exit_value,
        reason,
        100.0,
        (exit_value - entry) * quantity,
        0.08,
        0.05,
        70.0,
        fraction,
    )


@pytest.fixture()
def databases(tmp_path: Path) -> tuple[Path, Path]:
    trading = tmp_path / "trading.db"
    history = tmp_path / "history.db"
    connection = sqlite3.connect(trading)
    connection.executescript(_POSITIONS_SCHEMA)
    rows = [
        # Moneyline winner stopped out: the canonical giveback case.
        _position(
            "p1", "e1", "moneyline", "Cleveland Guardians",
            entry=0.35, quantity=10.0, exit_value=0.26,
        ),
        # Spread that lost at settlement; the early exit reduced the loss.
        _position(
            "p2", "e2", "spread", "Texas Rangers +2.5",
            entry=0.40, quantity=10.0, exit_value=0.30,
            reason="model_reversal", fraction=0.4,
        ),
        # Total that won at settlement (9 runs > 8.5).
        _position(
            "p3", "e2", "total", "Over 8.5",
            entry=0.30, quantity=10.0, exit_value=0.45,
            reason="profit_lock_after_edge_decay", fraction=0.1,
        ),
        # Selection matching neither team: ungradeable.
        _position(
            "p4", "e1", "moneyline", "Springfield Isotopes",
            entry=0.50, quantity=2.0, exit_value=0.40,
        ),
        # Event with no settled outcome row.
        _position(
            "p5", "missing", "moneyline", "Cleveland Guardians",
            entry=0.50, quantity=2.0, exit_value=0.40,
        ),
        # Segment scope cannot be graded against final scores.
        _position(
            "p6", "e1", "moneyline", "Cleveland Guardians",
            entry=0.50, quantity=2.0, exit_value=1.0,
            reason="dry_run_segment_resolved", scope="first_inning",
        ),
    ]
    connection.executemany(
        "INSERT INTO live_managed_positions VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    connection.commit()
    connection.close()

    connection = sqlite3.connect(history)
    connection.executescript(_OUTCOMES_SCHEMA)
    connection.executemany(
        "INSERT INTO event_outcomes VALUES (?,?,?,?,?,?,?)",
        [
            (
                "e1", "Guardians vs. Reds", "baseball",
                "Cleveland Guardians", "Cincinnati Reds", 4.0, 2.0,
            ),
            (
                "e2", "Rangers vs. Rays", "baseball",
                "Texas Rangers", "Tampa Bay Rays", 2.0, 7.0,
            ),
        ],
    )
    connection.commit()
    connection.close()
    return trading, history


def test_grades_positions_with_the_application_settlement_semantics(
    databases: tuple[Path, Path],
) -> None:
    trading, history = databases
    report = run(trading, history, resamples=200, seed=3)

    assert report["closed_positions"] == 6
    assert report["graded_positions"] == 3
    assert report["skipped"] == {
        "not_full_game_scope": 1,
        "no_settled_outcome": 1,
        "unmapped_selection": 1,
        "unpriced_close": 0,
    }

    by_reason = {
        entry["exit_reason"]: entry for entry in report["by_exit_reason"]
    }
    # p1: stopped at 0.26 (-0.9), settlement 1.0 (+6.5): giveback 7.4.
    stop = by_reason["hard_stop_loss"]
    assert stop["actual_pnl_usd"] == pytest.approx(-0.9)
    assert stop["settlement_pnl_usd"] == pytest.approx(6.5)
    assert stop["giveback_usd"] == pytest.approx(7.4)
    assert stop["settlement_win_rate_pct"] == 100.0
    # p2: lost at settlement; the early exit avoided further loss, so the
    # counterfactual is negative for it.
    reversal = by_reason["model_reversal"]
    assert reversal["settlement_pnl_usd"] == pytest.approx(-4.0)
    assert reversal["giveback_usd"] == pytest.approx(-3.0)


def test_overall_summary_is_stake_weighted(
    databases: tuple[Path, Path],
) -> None:
    trading, history = databases
    report = run(trading, history, resamples=200, seed=3)
    overall = report["overall"]
    # Stakes: 3.5 + 4.0 + 3.0 = 10.5; actual -0.9 -1.0 +1.5 = -0.4;
    # settlement +6.5 -4.0 +7.0 = 9.5.
    assert overall["positions"] == 3
    assert overall["events"] == 2
    assert overall["stake_usd"] == pytest.approx(10.5)
    assert overall["actual_pnl_usd"] == pytest.approx(-0.4)
    assert overall["settlement_pnl_usd"] == pytest.approx(9.5)
    assert overall["giveback_usd"] == pytest.approx(9.9)
    assert overall["settlement_win_rate_pct"] == pytest.approx(66.67)


def test_bootstrap_is_deterministic_for_a_seed(
    databases: tuple[Path, Path],
) -> None:
    trading, history = databases
    first = run(trading, history, resamples=250, seed=11)
    second = run(trading, history, resamples=250, seed=11)
    assert (
        first["overall"]["bootstrap_giveback"]
        == second["overall"]["bootstrap_giveback"]
    )
    assert first["overall"]["bootstrap_giveback"]["resamples"] == 250


def test_bootstrap_refuses_a_single_event() -> None:
    graded, _ = grade_positions([], {})
    assert bootstrap_giveback(graded)["resamples"] == 0


def test_emits_calibration_pairs_with_stage_labels(
    databases: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    trading, history = databases
    pairs_path = tmp_path / "pairs.jsonl"
    report = run(trading, history, resamples=200, seed=3, pairs_path=pairs_path)
    assert report["calibration_pairs_path"] == str(pairs_path)
    pairs = [
        json.loads(line)
        for line in pairs_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(pairs) == 3
    by_id = {pair["position_id"]: pair for pair in pairs}
    assert by_id["p1"]["label"] == 1
    assert by_id["p1"]["stage"] == "early"
    assert by_id["p2"]["label"] == 0
    assert by_id["p2"]["stage"] == "middle"
    assert by_id["p3"]["stage"] == "late"
    assert by_id["p3"]["entry_price"] == pytest.approx(0.30)


def test_calibration_pairs_tolerate_missing_stage(
    databases: tuple[Path, Path],
) -> None:
    _trading, _history = databases
    graded, _ = grade_positions(
        [
            {
                "id": "p9",
                "event_id": "e1",
                "market_type": "moneyline",
                "market_scope": "full_game",
                "selection": "Cleveland Guardians",
                "quantity": 1.0,
                "entry_cost": 0.5,
                "current_exit_value": 0.4,
                "exit_reason": "hard_stop_loss",
                "realized_pnl": -0.1,
                "entry_signal_edge": None,
                "entry_execution_edge": None,
                "entry_signal_quality": None,
                "entry_game_fraction_remaining": None,
            }
        ],
        {
            "e1": _outcome_row(
                "e1", "Cleveland Guardians", "Cincinnati Reds", 4.0, 2.0
            )
        },
    )
    pairs = calibration_pairs(graded)
    assert pairs[0]["stage"] is None
    assert pairs[0]["label"] == 1


def _outcome_row(
    event_id: str,
    home: str,
    away: str,
    home_score: float,
    away_score: float,
) -> sqlite3.Row:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(_OUTCOMES_SCHEMA)
    connection.execute(
        "INSERT INTO event_outcomes VALUES (?,?,?,?,?,?,?)",
        (event_id, "n", "baseball", home, away, home_score, away_score),
    )
    return connection.execute("SELECT * FROM event_outcomes").fetchone()
