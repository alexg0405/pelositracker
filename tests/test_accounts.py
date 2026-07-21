import pytest

from app.accounts import AccountBook, Strategy, qualification_failures, qualifies, stake_for
from app.models import Event, Signal


def valid_signal(**overrides):
    values = dict(
        event_id="e", market="moneyline", outcome="Home",
        model_probability=.60, market_probability=.50, edge=.10,
        confidence=90, action="PAPER_BET", reasons=[],
        quote_source="Polymarket", n_reference_sources=2,
        required_edge=.04, kelly_fraction=.05,
        consensus_probability=.60, calibrated_consensus_probability=.60,
        probability_net_ev_positive=.99, net_expected_value_total=5.0,
    )
    values.update(overrides)
    return Signal(**values)


@pytest.mark.parametrize("changes, expected", [
    ({"action": "WATCH"}, "engine gates"),
    ({"quote_source": "Pinnacle"}, "Polymarket"),
    ({"market_probability": 0}, "invalid executable"),
    ({"n_reference_sources": 1}, "too few"),
    ({"edge": .03, "required_edge": .04}, "risk-adjusted"),
])
def test_bot_rejects_every_hard_engine_or_execution_failure(changes, expected):
    strategy = Strategy("test", edge_threshold=0)
    signal = valid_signal(**changes)
    assert not qualifies(strategy, signal)
    assert any(expected in reason for reason in qualification_failures(strategy, signal))


def test_validated_polymarket_signal_qualifies_and_depth_caps_stake():
    strategy = Strategy("flat", edge_threshold=.05, sizing="flat", flat_stake=100)
    signal = valid_signal(fillable_size=40)  # 40 shares * $0.50 = $20 available
    assert qualifies(strategy, signal)
    assert stake_for(strategy, signal, 1000) == pytest.approx(20)


def test_unknown_depth_does_not_become_a_zero_dollar_stake():
    strategy = Strategy("flat", edge_threshold=.05, sizing="flat", flat_stake=100)
    signal = valid_signal(fillable_size=None)
    assert qualifies(strategy, signal)
    assert stake_for(strategy, signal, 1000) == pytest.approx(100)


def test_sport_and_correlated_group_caps_are_durable_and_enforced(tmp_path):
    book = AccountBook(str(tmp_path / "accounts.db"))
    strategy = Strategy(
        "risk-test", sizing="flat", flat_stake=100.0, start_bankroll=1000.0,
        edge_threshold=0.0, max_stake_pct=1.0, max_event_exposure_pct=1.0,
        max_sport_exposure_pct=.08, max_correlated_exposure_pct=.05,
        max_total_exposure_pct=1.0,
    )
    book.seed([strategy])
    first = Event("A vs B", "basketball", "A", "B", id="event-1")
    second = Event("C vs D", "basketball", "C", "D", id="event-2")
    moneyline = valid_signal(
        event_id=first.id, outcome="A", decision_id="decision-1",
        fillable_size=1000,
    )
    spread = valid_signal(
        event_id=first.id, market="spread", outcome="A -1.5",
        decision_id="decision-2", fillable_size=1000,
    )
    other_event = valid_signal(
        event_id=second.id, outcome="C", decision_id="decision-3",
        fillable_size=1000,
    )
    try:
        placed_first = book.place(first, [moneyline, spread])
        placed_second = book.place(second, [other_event])
        rows = book.account_bets("risk-test")
    finally:
        book.close()

    assert sum(item["stake"] for item in placed_first) == pytest.approx(50.0)
    assert sum(item["stake"] for item in placed_second) == pytest.approx(30.0)
    assert sum(row["stake"] for row in rows) == pytest.approx(80.0)
    assert {row["sport"] for row in rows} == {"basketball"}
    assert all(row["correlation_group"] and row["decision_id"] for row in rows)
