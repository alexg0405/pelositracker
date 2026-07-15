import math

import pytest
from fastapi.testclient import TestClient

from app import backtest
from app.ledger import Ledger
from app.main import app
from app.models import Event, Signal


def paper_signal(outcome, model_p, exec_p, edge):
    return Signal("e", "moneyline", outcome, model_probability=model_p,
                  market_probability=exec_p, edge=edge, confidence=90.0,
                  action="PAPER_BET", reasons=[], quote_source="DraftKings",
                  market_fair_prob=model_p, devig_method="shin", overround=1.05,
                  n_reference_sources=2)


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


def test_metrics_ignore_unsettled_and_unclosed():
    bets = [{"entry_fair_prob": 0.5, "entry_executable": 0.5, "clv": None, "settled_result": None}]
    assert backtest.clv_summary(bets)["n"] == 0
    assert backtest.brier_score(bets) is None
    assert backtest.log_loss(bets) is None


def test_ledger_roundtrip_clv_and_settlement(tmp_path):
    ledger = Ledger(str(tmp_path / "t.db"))
    try:
        event = Event(name="Hawks vs Foxes", sport="basketball", home="Hawks", away="Foxes")
        sig = paper_signal("home", 0.60, 0.55, 0.05)

        assert ledger.record_signals(event, [sig]) == 1
        assert ledger.record_signals(event, [sig]) == 0  # deduped per selection

        ledger.snapshot_closing(event.id, {("moneyline", "home"): 0.62})
        ledger.settle_moneyline(event.id, {"home", event.home})

        rows = ledger.all_bets()
        assert len(rows) == 1
        row = rows[0]
        assert row["clv"] == pytest.approx(0.62 - 0.55)     # closing fair - entry price
        assert row["settled_result"] == 1.0                 # Hawks (home) won
        assert row["devig_method"] == "shin"

        summary = backtest.summary(rows)
        assert summary["n_settled"] == 1
        assert summary["clv"]["beat_close_rate"] == pytest.approx(1.0)
        # market baseline should be worse (further from the realized 1.0)
        assert summary["market_baseline"]["brier"] > summary["model"]["brier"]
    finally:
        ledger.close()


def test_metrics_endpoint_is_available():
    with TestClient(app) as client:
        response = client.get("/api/metrics")
        assert response.status_code == 200
        body = response.json()
        assert "clv" in body and "n_bets" in body
        assert client.get("/api/bets").json() == [] or isinstance(client.get("/api/bets").json(), list)
