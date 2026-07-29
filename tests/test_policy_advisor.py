from app.policy_advisor import recommend_policy
from app.polymarket_us_trading import TradingPolicy


def _history():
    trades = []
    opportunities = []
    timestamp = 1_800_000_000.0
    for event_number in range(12):
        for index, (edge, quality, pnl) in enumerate((
            (0.08, 82.0, 0.30),
            (0.04, 66.0, -0.10),
            (0.02, 52.0, -0.25),
        )):
            observed = timestamp + event_number * 1800 + index * 120
            row = {
                "event_id": f"event-{event_number}",
                "opened_ts": observed,
                "cost_basis": 1.0,
                "realized_pnl": pnl,
                "signal_edge": edge,
                "signal_quality": quality,
                "reference_sources": 2,
                "entry_cost": 0.45,
                "market_type": "moneyline",
                "market_slug": f"event-{event_number}-{index}",
                "selection": f"side-{index}",
            }
            trades.append(row)
            opportunities.append({
                **row,
                "observed_ts": observed,
            })
    return trades, opportunities


def test_policy_advisor_uses_event_blocked_profit_evidence_and_frequency_goal():
    trades, opportunities = _history()
    policy = TradingPolicy()
    current = {
        field: getattr(policy, field)
        for field in policy.__dataclass_fields__
    }
    current.update({
        "min_edge": 0.02,
        "min_signal_quality": 50.0,
        "max_orders_per_hour": 6,
        "candidate_cooldown_seconds": 300,
    })

    protect = recommend_policy(
        closed_trades=trades,
        opportunities=opportunities,
        current_policy=current,
        objective="protect_profit",
        target_trades_per_hour=2.0,
        model_evidence={
            "stage": "fitted_shadow",
            "live_eligible": False,
        },
    )
    active = recommend_policy(
        closed_trades=trades,
        opportunities=opportunities,
        current_policy=current,
        objective="more_trades",
        target_trades_per_hour=10.0,
        model_evidence={
            "stage": "fitted_shadow",
            "live_eligible": False,
        },
    )

    # Twelve independent events are below the robust production-analysis
    # minimum. The optimizer may still show its diagnostic comparison, but it
    # must fail closed rather than silently apply it.
    assert protect["status"] == "exploratory"
    assert protect["apply_allowed"] is False
    assert protect["evidence"]["eligible_closed_trades"] == 36
    assert protect["evidence"]["independent_events"] == 12
    assert protect["evidence"]["test_events"] == 4
    assert (
        protect["suggested_policy"]["min_edge"] > current["min_edge"]
        or protect["suggested_policy"]["min_signal_quality"]
        > current["min_signal_quality"]
    )
    assert protect["evidence"]["suggested_test"]["turnover_roi"] > 0
    assert protect["evidence"]["event_block_bootstrap"]["draws"] == 2000
    assert protect["model_used_to_change_settings"] is False

    assert active["suggested_policy"]["max_orders_per_hour"] == 10
    assert active["suggested_policy"]["candidate_cooldown_seconds"] == 120
    assert active["target_trades_per_hour"] == 10.0
    assert "can guarantee profit" in active["guarantee"].casefold()


def test_policy_advisor_fits_upper_edge_line_type_and_repeat_filters():
    trades = []
    opportunities = []
    start = 1_900_000_000.0
    for event_number in range(30):
        event_start = start + event_number * 7200
        cases = (
            ("moneyline", 0.09, 84.0, 0.28, "primary"),
            ("moneyline", 0.18, 86.0, -0.30, "extreme"),
            ("spread", 0.09, 84.0, -0.20, "spread"),
            ("moneyline", 0.09, 84.0, -0.18, "repeat"),
        )
        for index, (market_type, edge, quality, pnl, selection) in enumerate(cases):
            observed = event_start + index * 120
            row = {
                "event_id": f"event-{event_number}",
                "opened_ts": observed,
                "closed_ts": observed + 300,
                "cost_basis": 1.0,
                "realized_pnl": pnl,
                "signal_edge": edge,
                "signal_quality": quality,
                "reference_sources": 2,
                "entry_cost": 0.45,
                "market_type": market_type,
                "market_slug": f"event-{event_number}-{selection}",
                "selection": selection,
                "game_fraction_remaining": 0.60,
                "exit_reason": "research",
            }
            trades.append(row)
            opportunities.append({**row, "observed_ts": observed})
    current = {
        field: getattr(TradingPolicy(), field)
        for field in TradingPolicy.__dataclass_fields__
    }
    current.update({
        "min_edge": 0.02,
        "max_edge": 1.0,
        "min_signal_quality": 50.0,
        "allowed_market_types": ("moneyline", "spread"),
        "max_entries_per_event_per_hour": 10,
        "candidate_cooldown_seconds": 30,
    })

    advice = recommend_policy(
        closed_trades=trades,
        opportunities=opportunities,
        current_policy=current,
        objective="protect_profit",
        target_trades_per_hour=2.0,
        analysis_mode="dry_run",
        market_types=("moneyline", "spread"),
    )

    assert advice["status"] == "evidence_backed_research"
    assert advice["apply_allowed"] is True
    assert advice["suggested_policy"]["allowed_market_types"] == ["moneyline"]
    assert advice["suggested_policy"]["max_edge"] <= 0.15
    assert advice["evidence"]["event_block_bootstrap"]["lower_95"] > 0
    assert advice["diagnostics"]["line_types"]
    assert advice["candidate_frontier"]
