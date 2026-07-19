import pytest

from app.accounts import Strategy, qualification_failures, qualifies, stake_for
from app.models import Signal


def valid_signal(**overrides):
    values = dict(
        event_id="e", market="moneyline", outcome="Home",
        model_probability=.60, market_probability=.50, edge=.10,
        confidence=90, action="PAPER_BET", reasons=[],
        quote_source="Polymarket", n_reference_sources=2,
        required_edge=.04, kelly_fraction=.05,
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
