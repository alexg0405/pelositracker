import math
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import backtest
from app.ledger import Ledger
from app.main import app
from app.models import Event, Signal


# The typed execution-safety gates the engine emits as passing for a real
# tradeable selection; a close mark is only recorded when these PASS.
PASSING_EXECUTION_GATES = [
    {"code": "provider_freshness", "passed": True, "status": "pass"},
    {"code": "market_identity", "passed": True, "status": "pass"},
    {"code": "market_status", "passed": True, "status": "pass"},
    {"code": "executable_fill", "passed": True, "status": "pass"},
]


def paper_signal(outcome, model_p, exec_p, edge, *, gate_results=None):
    shares = 20.0
    cash = shares * exec_p
    return Signal("e", "moneyline", outcome, model_probability=model_p,
                  market_probability=exec_p, edge=edge, confidence=90.0,
                  action="PAPER_BET", reasons=[], quote_source="DraftKings",
                  market_fair_prob=model_p, devig_method="shin", overround=1.05,
                  n_reference_sources=2, requested_cash=cash,
                  filled_cash=cash, filled_shares=shares,
                  execution_fee=0.0, execution_complete=True,
                  gate_results=(PASSING_EXECUTION_GATES if gate_results is None
                                else gate_results))


def test_pure_metrics_match_hand_computed_values():
    bets = [
        {"entry_fair_prob": 0.6, "entry_executable": 0.55, "clv": 0.07, "settled_result": 1.0},
        {"entry_fair_prob": 0.3, "entry_executable": 0.35, "clv": -0.02, "settled_result": 0.0},
    ]
    clv = backtest.clv_summary(bets)
    assert clv["n"] == 2
    assert clv["mean_clv"] == pytest.approx(0.025)
    assert clv["beat_close_rate"] == pytest.approx(0.5)

    assert backtest.brier_score(bets, "entry_fair_prob") == pytest.approx((0.16 + 0.09) / 2)
    expected_ll = (-math.log(0.6) - math.log(0.7)) / 2
    assert backtest.log_loss(bets, "entry_fair_prob") == pytest.approx(expected_ll)

    bins = backtest.reliability_bins(bets)
    assert backtest.expected_calibration_error(bins) == pytest.approx(0.35)


def test_evaluation_reports_calibration_decomposition_execution_and_event_blocks():
    bets = []
    for index, (probability, result, profit_direction) in enumerate((
        (.7, 1.0, 1), (.6, 1.0, 1), (.4, 0.0, -1), (.3, 0.0, -1),
    )):
        shares = 20.0
        cash = shares * .5
        bets.append({
            "event_id": f"event-{index // 2}",
            "sport": "basketball",
            "market": "moneyline",
            "entry_ts": float(index),
            "settled_ts": float(index + 10),
            "entry_fair_prob": probability,
            "entry_calibrated_prob": probability,
            "entry_executable": .5,
            "closing_executable": .52 + index * .01,
            "closing_fair_prob": .53 + index * .01,
            "clv": .02 + index * .01,
            "settled_result": result,
            "requested_cash": cash,
            "filled_cash": cash,
            "filled_shares": shares,
            "execution_fee": .05,
            "entry_independent_prob": probability + .01,
            "profit_direction": profit_direction,
        })

    decomposition = backtest.brier_decomposition(bets)
    assert decomposition is not None
    assert decomposition["reconstructed_brier"] == pytest.approx(
        decomposition["reliability"] - decomposition["resolution"]
        + decomposition["uncertainty"]
    )
    interval = backtest.event_block_interval(bets, "clv", draws=200, seed=7)
    assert interval is not None and interval["events"] == 2
    assert interval["lower"] <= interval["mean"] <= interval["upper"]

    decisions = [
        {"policy_action": "PAPER_BET", "gate_results_json": "[]"},
        {"policy_action": "WATCH", "gate_results_json": (
            '[{"code":"uncertainty_support","passed":false}]'
        )},
    ]
    report = backtest.summary(bets, decisions)
    assert report["execution"]["fill_rate"] == 1.0
    assert report["execution"]["turnover"] == pytest.approx(40.0)
    assert report["portfolio"]["largest_sport_turnover_share"] == 1.0
    assert report["bootstrap"]["mean_executable_clv"]["events"] == 2
    assert report["eligibility_coverage"]["all_opportunities"] == 2
    assert report["eligibility_coverage"]["rejection_gates"] == {
        "uncertainty_support": 1
    }
    assert report["independent_model"]["n_settled"] == 4
    assert report["independent_model"]["brier"] is not None
    assert report["independent_model"]["same_rows_calibrated_consensus"][
        "brier"
    ] == report["model"]["brier"]
    assert report["statistical_claim_supported"] is False


def test_metrics_ignore_unsettled_and_unclosed():
    bets = [{"entry_fair_prob": 0.5, "entry_executable": 0.5, "clv": None, "settled_result": None}]
    assert backtest.clv_summary(bets)["n"] == 0
    assert backtest.brier_score(bets) is None
    assert backtest.log_loss(bets) is None


def test_calibrated_metric_does_not_backfill_missing_probability_with_uncalibrated():
    bets = [
        {"entry_fair_prob": 0.9, "entry_calibrated_prob": 0.6, "settled_result": 1.0},
        {"entry_fair_prob": 0.9, "settled_result": 0.0},  # no calibrated probability
    ]
    # Only the row that actually carries a calibrated probability is scored.
    calibrated = backtest.brier_score(bets, "entry_calibrated_prob")
    assert calibrated == pytest.approx((0.6 - 1.0) ** 2)
    # The old silent fallback mixed the uncalibrated 0.9 into the label-0 row.
    backfilled = ((0.6 - 1.0) ** 2 + (0.9 - 0.0) ** 2) / 2
    assert calibrated != pytest.approx(backfilled)
    # The report exposes the calibrated coverage so a subset metric is visible.
    report = backtest.summary(bets)
    assert report["n_settled"] == 2
    assert report["model"]["n_scored"] == 1


def test_missing_filled_cash_invalidates_pnl_instead_of_booking_it_free():
    # A settled winner with shares filled but no recorded cash paid.
    row = {"settled_result": 1.0, "filled_shares": 20.0, "requested_cash": 10.0}
    assert backtest._paper_profit(row) is None
    # It must not surface as +20 profit or a $0-cost drawdown path.
    assert backtest.execution_summary([row])["net_paper_return"] is None
    assert backtest.portfolio_summary([row])["max_drawdown_dollars"] is None


def test_fill_rate_counts_only_complete_fills_not_a_sliver():
    bets = [
        {"requested_cash": 100.0, "filled_cash": 100.0, "filled_shares": 200.0},  # complete
        {"requested_cash": 100.0, "filled_cash": 1.0, "filled_shares": 2.0},      # partial sliver
    ]
    report = backtest.execution_summary(bets)
    assert report["submitted"] == 2
    assert report["filled"] == 1                       # only the complete order
    assert report["orders_with_partial_fill"] == 1
    assert report["fill_rate"] == pytest.approx(0.5)
    assert report["any_fill_rate"] == pytest.approx(1.0)
    assert report["cash_fill_ratio"] == pytest.approx(101.0 / 200.0)


def test_corp_reliability_decomposition_is_exact_and_nonnegative():
    # Two forecast levels, each half right -> a well-defined isotonic recalibration.
    bets = [
        {"entry_calibrated_prob": 0.2, "settled_result": 0.0},
        {"entry_calibrated_prob": 0.2, "settled_result": 1.0},
        {"entry_calibrated_prob": 0.8, "settled_result": 1.0},
        {"entry_calibrated_prob": 0.8, "settled_result": 0.0},
        {"entry_calibrated_prob": 0.8, "settled_result": 1.0},
    ]
    corp = backtest.corp_reliability(bets, "entry_calibrated_prob")
    assert corp is not None and corp["n"] == 5
    # Exact CORP identity: mean_brier == MCB - DSC + UNC, and mean_brier == the Brier score.
    assert corp["mean_brier"] == pytest.approx(
        corp["miscalibration"] - corp["discrimination"] + corp["uncertainty"])
    assert corp["mean_brier"] == pytest.approx(
        backtest.brier_score(bets, "entry_calibrated_prob"))
    # All three components are non-negative.
    assert corp["miscalibration"] >= -1e-12
    assert corp["discrimination"] >= -1e-12
    assert corp["uncertainty"] >= 0.0
    # The recalibrated reliability curve is monotonically non-decreasing.
    rates = [point["calibrated_rate"] for point in corp["curve"]]
    assert rates == sorted(rates)


def test_corp_reliability_is_wired_into_the_report():
    bets = [
        {"event_id": "e", "entry_calibrated_prob": 0.6, "entry_fair_prob": 0.6,
         "entry_executable": 0.5, "settled_result": 1.0},
        {"event_id": "e", "entry_calibrated_prob": 0.4, "entry_fair_prob": 0.4,
         "entry_executable": 0.5, "settled_result": 0.0},
    ]
    report = backtest.summary(bets)
    assert report["reliability_corp"] is not None
    assert report["reliability_corp"]["n"] == 2
    assert "reliability" in report  # legacy fixed-bin diagram still present


def test_drawdown_is_order_independent_and_fails_closed_without_timestamps():
    rows = [
        {"event_id": "a", "settled_ts": 2.0, "filled_shares": 10.0,
         "filled_cash": 5.0, "settled_result": 0.0},   # -5 at t=2
        {"event_id": "b", "settled_ts": 1.0, "filled_shares": 10.0,
         "filled_cash": 5.0, "settled_result": 1.0},   # +5 at t=1
    ]
    forward = backtest.portfolio_summary(rows)["max_drawdown_dollars"]
    backward = backtest.portfolio_summary(list(reversed(rows)))["max_drawdown_dollars"]
    # +5 (t=1) then -5 (t=2): peak 5, trough 0 -> drawdown 5, whatever the input order.
    assert forward == backward == pytest.approx(5.0)
    # A realized-P&L row with no usable timestamp makes the whole path undefined.
    rows.append({"event_id": "c", "filled_shares": 10.0,
                 "filled_cash": 5.0, "settled_result": 0.0})
    assert backtest.portfolio_summary(rows)["max_drawdown_dollars"] is None


def test_net_return_does_not_subtract_execution_fee_twice():
    bets = [{
        "settled_result": 1.0,
        "filled_shares": 20.0,
        "filled_cash": 10.05,
        "execution_fee": 0.05,
        "requested_cash": 10.05,
    }]

    report = backtest.execution_summary(bets)

    assert report["fees"] == pytest.approx(0.05)
    assert report["net_paper_return"] == pytest.approx(9.95)


def test_ledger_never_invents_a_fill_when_execution_lineage_is_missing(tmp_path):
    ledger = Ledger(str(tmp_path / "missing-fill.db"))
    try:
        event = Event(name="A vs B", sport="basketball", home="A", away="B")
        signal = paper_signal("A", .6, .5, .1)
        signal.execution_complete = False
        signal.filled_shares = 0.0
        assert ledger.record_signals(event, [signal]) == 0
        assert ledger.all_bets() == []
        assert len(ledger.all_decisions()) == 1
    finally:
        ledger.close()


def test_decision_coverage_is_lean_and_excludes_heavy_snapshot(tmp_path):
    ledger = Ledger(str(tmp_path / "coverage.db"))
    try:
        event = Event(name="A vs B", sport="basketball", home="A", away="B")
        signal = paper_signal("A", .6, .5, .1)
        signal.input_snapshot_json = "x" * 100_000  # the heavy per-decision blob
        assert ledger.record_signals(event, [signal]) == 1

        full = ledger.all_decisions()
        lean = ledger.decision_coverage()

        # Same rows, but the lean projection drops the big snapshot column (and
        # every other column the metrics summary never reads).
        assert len(lean) == len(full) == 1
        assert "input_snapshot_json" in full[0]
        assert set(lean[0]) == {"policy_action", "gate_results_json", "as_of"}

        # It must still drive eligibility_coverage identically.
        assert (backtest.eligibility_coverage(lean, 1)
                == backtest.eligibility_coverage(full, 1))
    finally:
        ledger.close()


def test_ledger_roundtrip_clv_and_settlement(tmp_path):
    ledger = Ledger(str(tmp_path / "t.db"))
    try:
        event = Event(name="Hawks vs Foxes", sport="basketball", home="Hawks", away="Foxes")
        sig = paper_signal("home", 0.60, 0.55, 0.05)
        sig.independent_model_probability = .61
        sig.independent_model_version = "nba-test-v1"
        sig.independent_model_hash = "a" * 64
        sig.independent_calibration_version = "nba-cal-test-v1"
        sig.independent_calibration_hash = "b" * 64
        sig.independent_model_sample_size = 1500
        sig.independent_model_event_count = 500
        sig.independent_model_registry_version = "1:fixture"

        assert ledger.record_signals(event, [sig]) == 1
        assert ledger.record_signals(event, [sig]) == 0  # deduped per selection

        close = paper_signal("home", 0.63, 0.62, 0.01)
        close.action = "WATCH"
        close.observed_at = sig.observed_at + timedelta(seconds=1)
        close.decision_hash = "close-mark"
        ledger.record_signals(event, [close])
        ledger.snapshot_closing(event.id)
        ledger.settle_moneyline(event.id, {"home", event.home})

        rows = ledger.all_bets()
        assert len(rows) == 1
        row = rows[0]
        assert row["clv"] == pytest.approx(0.62 - 0.55)     # close executable - entry
        assert row["settled_result"] == 1.0                 # Hawks (home) won
        assert row["devig_method"] == "shin"
        assert row["entry_independent_prob"] == pytest.approx(.61)
        assert row["independent_model_version"] == "nba-test-v1"
        assert row["independent_calibration_version"] == "nba-cal-test-v1"
        decision = ledger.all_decisions()[0]
        assert decision["independent_model_hash"] == "a" * 64
        assert decision["independent_calibration_hash"] == "b" * 64
        assert decision["independent_model_registry_version"] == "1:fixture"

        summary = backtest.summary(rows)
        assert summary["n_settled"] == 1
        assert summary["clv"]["beat_close_rate"] == pytest.approx(1.0)
        # market baseline should be worse (further from the realized 1.0)
        assert summary["market_baseline"]["brier"] > summary["model"]["brier"]
    finally:
        ledger.close()


def test_close_mark_gated_by_typed_gates_not_reason_wording(tmp_path):
    ledger = Ledger(str(tmp_path / "cg.db"))
    try:
        event = Event(name="Hawks vs Foxes", sport="basketball", home="Hawks", away="Foxes")
        entry = paper_signal("home", 0.60, 0.55, 0.05)   # entry + first valid close mark
        ledger.record_signals(event, [entry])

        # Reasons literally say "stale", but the typed gates all PASS -> wording
        # has NO policy effect, so this DOES replace the close mark (0.62).
        worded = paper_signal("home", 0.63, 0.62, 0.01)
        worded.action = "WATCH"
        worded.reasons = ["price looks stale to a human, but every gate passes"]
        worded.observed_at = entry.observed_at + timedelta(seconds=1)
        worded.decision_hash = "worded-close"
        ledger.record_signals(event, [worded])

        # Clean reasons, but provider_freshness FAILS -> must NOT replace the mark.
        blocked = paper_signal("home", 0.70, 0.71, 0.01, gate_results=[
            {"code": "provider_freshness", "passed": False, "status": "fail"},
            {"code": "market_identity", "passed": True, "status": "pass"},
            {"code": "market_status", "passed": True, "status": "pass"},
            {"code": "executable_fill", "passed": True, "status": "pass"},
        ])
        blocked.action = "WATCH"
        blocked.reasons = []
        blocked.observed_at = entry.observed_at + timedelta(seconds=2)
        blocked.decision_hash = "blocked-close"
        ledger.record_signals(event, [blocked])

        ledger.snapshot_closing(event.id)
        ledger.settle_moneyline(event.id, {"home", event.home})
        row = ledger.all_bets()[0]
    finally:
        ledger.close()
    # CLV reflects the worded (0.62) mark: reason wording ignored, failing gate blocked.
    assert row["clv"] == pytest.approx(0.62 - 0.55)


def _placed_and_watched(event):
    placed = paper_signal("Hawks", 0.60, 0.55, 0.05)
    placed.consensus_probability = 0.58
    watched = Signal(event.id, "moneyline", "Foxes", model_probability=0.40,
                     market_probability=0.45, edge=-0.01, confidence=80.0,
                     action="WATCH", reasons=[], quote_source="DraftKings",
                     market_fair_prob=0.40, consensus_probability=0.42)
    return placed, watched


def test_scored_opportunities_labels_the_full_opportunity_set(tmp_path):
    ledger = Ledger(str(tmp_path / "opp.db"))
    try:
        event = Event(name="Hawks vs Foxes", sport="basketball", home="Hawks", away="Foxes")
        ledger.record_signals(event, list(_placed_and_watched(event)))
        ledger.settle_moneyline(event.id, {"Hawks", "home"})
        rows = ledger.scored_opportunities()
    finally:
        ledger.close()
    by_outcome = {row["outcome"]: row for row in rows}
    # The never-placed WATCH outcome is joined to a later label, just like the bet.
    assert by_outcome["Hawks"]["settled_result"] == 1.0
    assert by_outcome["Foxes"]["settled_result"] == 0.0
    assert by_outcome["Foxes"]["policy_action"] == "WATCH"


def test_opportunity_scores_are_flagged_observational(tmp_path):
    ledger = Ledger(str(tmp_path / "opp2.db"))
    try:
        event = Event(name="Hawks vs Foxes", sport="basketball", home="Hawks", away="Foxes")
        ledger.record_signals(event, list(_placed_and_watched(event)))
        ledger.settle_moneyline(event.id, {"Hawks", "home"})
        report = backtest.opportunity_scores(ledger.scored_opportunities())
    finally:
        ledger.close()
    assert report["observational"] is True
    assert report["statistical_claim_supported"] is False
    assert report["all_opportunities"]["sample_size"] == 2
    assert report["paper_bet"]["sample_size"] == 1        # the placed Hawks bet
    assert report["watch_or_rejected"]["sample_size"] == 1  # the never-placed Foxes watch


def test_draw_settles_the_draw_outcome_not_nothing(tmp_path):
    ledger = Ledger(str(tmp_path / "d.db"))
    try:
        event = Event(name="A vs B", sport="soccer", home="A", away="B")
        for outcome in ("A", "B", "Draw"):
            ledger.record_signals(event, [
                Signal(event.id, "h2h", outcome, model_probability=0.33,
                       market_probability=0.33, edge=0.03, confidence=80.0,
                       action="PAPER_BET", reasons=[], quote_source="Book",
                       market_fair_prob=0.33, devig_method="shin", overround=1.06,
                       n_reference_sources=2, requested_cash=6.6,
                       filled_cash=6.6, filled_shares=20,
                       execution_fee=0.0, execution_complete=True),
            ])
        # 1-1 final -> Draw wins.
        ledger.settle_moneyline(event.id, {"draw", "Draw"})
        settled = {b["outcome"]: b["settled_result"] for b in ledger.all_bets()}
        assert settled == {"A": 0.0, "B": 0.0, "Draw": 1.0}
    finally:
        ledger.close()


def test_empty_winner_set_settles_nothing(tmp_path):
    ledger = Ledger(str(tmp_path / "e.db"))
    try:
        event = Event(name="A vs B", sport="soccer", home="A", away="B")
        ledger.record_signals(event, [
            Signal(event.id, "h2h", "A", model_probability=0.5, market_probability=0.5,
                   edge=0.03, confidence=80.0, action="PAPER_BET", reasons=[],
                   quote_source="Book", market_fair_prob=0.5, devig_method="shin",
                   overround=1.05, n_reference_sources=2, requested_cash=10,
                   filled_cash=10, filled_shares=20,
                   execution_fee=0.0, execution_complete=True),
        ])
        ledger.settle_moneyline(event.id, set())  # unknown result must not mis-settle
        assert ledger.all_bets()[0]["settled_result"] is None
    finally:
        ledger.close()


def test_canceled_event_records_void_without_inventing_a_result(tmp_path):
    ledger = Ledger(str(tmp_path / "void.db"))
    try:
        event = Event(name="A vs B", sport="basketball", home="A", away="B")
        ledger.record_signals(event, [paper_signal("A", 0.55, 0.50, 0.05)])
        ledger.void_event(event.id, status="canceled")
        ledger.void_event(event.id, status="canceled")
        with ledger._db.cursor(dict_rows=True) as cur:
            ledger._db.execute(
                cur, "SELECT result, status FROM settlement_marks WHERE event_id=%s",
                (event.id,),
            )
            rows = [dict(row) for row in cur.fetchall()]
        assert rows == [{"result": None, "status": "canceled"}]
        assert ledger.all_bets()[0]["settled_result"] is None
    finally:
        ledger.close()


def test_watch_page_serves():
    with TestClient(app) as client:
        response = client.get("/watch")
        assert response.status_code == 200
        assert "stream-url" in response.text and "Where to watch" in response.text


def test_metrics_endpoint_is_available():
    with TestClient(app) as client:
        assert client.post("/api/login", data={"username": "admin", "password": "admin"}).status_code == 200
        response = client.get("/api/metrics")
        assert response.status_code == 200
        body = response.json()
        assert "clv" in body and "n_bets" in body
        assert client.get("/api/bets").json() == [] or isinstance(client.get("/api/bets").json(), list)
