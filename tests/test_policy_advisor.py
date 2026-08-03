import pytest

from app.policy_advisor import (
    ADVISOR_TUNABLE_FIELDS,
    BASELINE_CHEAT_SHEET_VERSION,
    POLICY_FIELD_CATALOG,
    recommend_policy,
)
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


def test_combined_live_and_dry_run_history_is_exploratory_even_when_profitable():
    trades = []
    opportunities = []
    start = 2_000_000_000.0
    for event_number in range(30):
        mode = "live" if event_number % 2 == 0 else "dry_run"
        for index in range(2):
            observed = start + event_number * 7200 + index * 180
            row = {
                "id": f"position-{event_number}-{index}",
                "event_id": f"event-{event_number}",
                "mode": mode,
                "opened_ts": observed,
                "closed_ts": observed + 300,
                "cost_basis": 1.0,
                "realized_pnl": 0.20,
                "signal_edge": 0.08,
                "signal_quality": 82.0,
                "reference_sources": 2,
                "entry_cost": 0.45,
                "market_type": "moneyline",
                "market_slug": f"event-{event_number}-{index}",
                "selection": f"side-{index}",
                "game_fraction_remaining": 0.60,
                "exit_reason": "research",
            }
            trades.append(row)
            opportunities.append({**row, "observed_ts": observed})
    current = {
        field: getattr(TradingPolicy(), field)
        for field in TradingPolicy.__dataclass_fields__
    }

    advice = recommend_policy(
        closed_trades=trades,
        opportunities=opportunities,
        current_policy=current,
        objective="balanced",
        target_trades_per_hour=4.0,
        analysis_mode="combined",
        market_types=("moneyline",),
    )

    assert advice["evidence"]["eligible_closed_trades"] == 60
    assert advice["evidence"]["statistical_validation_passed"] is True
    assert advice["validation_passed"] is False
    assert advice["apply_allowed"] is False
    assert advice["status"] == "exploratory_cross_lane"
    assert {
        row["label"] for row in advice["diagnostics"]["execution_modes"]
    } == {"dry_run", "live"}
    assert "cannot validate" in advice["evidence"][
        "execution_domain_warning"
    ]


def _rich_history():
    """History carrying the newer entry-stability and excursion columns."""
    trades = []
    opportunities = []
    timestamp = 1_800_000_000.0
    for event_number in range(12):
        for index, (edge, quality, agreement, age, pnl, peak) in enumerate((
            (0.08, 82.0, 88.0, 12.0, 0.30, 0.62),
            (0.04, 66.0, 61.0, 40.0, -0.10, 0.47),
            (0.02, 52.0, 35.0, 95.0, -0.25, 0.45),
        )):
            observed = timestamp + event_number * 1800 + index * 120
            row = {
                "event_id": f"event-{event_number}",
                "opened_ts": observed,
                "cost_basis": 1.0,
                "realized_pnl": pnl,
                "signal_edge": edge,
                "signal_quality": quality,
                "source_agreement": agreement,
                "signal_age_seconds": age,
                "entry_confirmation_readings": 1,
                "confirmation_price_drift": 0.0,
                "reference_sources": 2,
                "entry_cost": 0.45,
                "highest_exit_value": peak,
                "market_type": "moneyline",
                "market_slug": f"event-{event_number}-{index}",
                "selection": f"side-{index}",
            }
            trades.append(row)
            opportunities.append({
                **row,
                "observed_ts": observed,
                "spread": 0.02,
                "book_shares": 40.0,
            })
    return trades, opportunities


def _recommend_rich():
    trades, opportunities = _rich_history()
    policy = TradingPolicy()
    current = {
        field: getattr(policy, field)
        for field in policy.__dataclass_fields__
    }
    current.update({"min_edge": 0.02, "min_signal_quality": 50.0})
    return recommend_policy(
        closed_trades=trades,
        opportunities=opportunities,
        current_policy=current,
        objective="balanced",
        target_trades_per_hour=2.0,
    )


def test_field_recommendations_cover_every_catalogued_policy_field():
    result = _recommend_rich()
    records = {item["field"]: item for item in result["field_recommendations"]}

    assert set(records) == {item["field"] for item in POLICY_FIELD_CATALOG}
    for record in records.values():
        # The handoff requires support context on every suggested value.
        assert record["lane"] and record["line_types"]
        assert "trades" in record and "independent_events" in record
        assert record["date_range"]["first_ts"] is not None
        assert record["basis"] in {
            "validated",
            "observational",
            "baseline_fallback",
            "not_identifiable",
        }
        assert record["rationale"]


def test_only_grid_searched_fields_are_applyable():
    result = _recommend_rich()
    records = {item["field"]: item for item in result["field_recommendations"]}
    in_apply_set = {
        field for field, record in records.items() if record["in_apply_set"]
    }

    # Apply must never write a value the optimizer did not actually score.
    assert in_apply_set <= set(ADVISOR_TUNABLE_FIELDS)
    for record in records.values():
        if record["in_apply_set"]:
            assert record["evidence_mode"] == "grid_search"
            assert record["scores_realized_pnl"] is True
        # The effective flag additionally requires the validation gate, so it
        # can never claim a field will be written while Apply is locked.
        if record["applyable"]:
            assert record["in_apply_set"] is True
            assert record["basis"] == "validated"


def test_apply_flag_tracks_the_validation_gate_not_just_membership():
    result = _recommend_rich()
    records = {item["field"]: item for item in result["field_recommendations"]}
    grid = records["min_edge"]

    # This sample cannot clear the later-event validation, so Apply is locked.
    assert result["status"] != "evidence_backed_research"
    assert result["apply_allowed"] is False
    # The field is still one the optimizer tunes...
    assert grid["in_apply_set"] is True
    # ...but reporting it as "written by Apply" would overstate what happens.
    assert grid["applyable"] is False
    assert grid["basis"] == "observational"


def test_exit_controls_are_never_presented_as_fitted_settings():
    result = _recommend_rich()
    records = {item["field"]: item for item in result["field_recommendations"]}

    for field in (
        "trailing_drawdown",
        "minimum_locked_profit",
        "exit_edge",
        "min_hold_minutes",
        "adaptive_exit_profile",
        "stop_confirmation_readings",
    ):
        record = records[field]
        # Changing an exit rule changes the path that produced every retained
        # realized P/L, so these can only ever be versioned baselines.
        assert record["basis"] == "not_identifiable"
        assert record["applyable"] is False
        assert record["scores_realized_pnl"] is False
        assert record["baseline_version"] == BASELINE_CHEAT_SHEET_VERSION
        assert "cannot score it" in record["measurement_note"]


def test_profit_target_is_identifiable_from_retained_peak_excursion():
    result = _recommend_rich()
    target = next(
        item for item in result["field_recommendations"]
        if item["field"] == "profit_target"
    )

    assert target["evidence_mode"] == "excursion"
    assert target["identifiable_direction"] == "tightening_only"
    # Identifiable in one direction only and assumes a clean fill, so it is
    # reported for a deliberate decision rather than written by Apply.
    assert target["applyable"] is False
    by_value = {option["value"]: option for option in target["options"]}
    # Peak 0.62 on a 0.45 entry is about +37.8%; 0.47 is about +4.4%.
    assert by_value[0.04]["crossed_threshold"] == 24
    assert by_value[0.10]["crossed_threshold"] == 12
    assert by_value[0.10]["crossed_share"] == pytest.approx(1 / 3)
    # Trades that never touched the threshold keep their observed outcome, so
    # every trade is accounted for and none is silently dropped.
    for option in target["options"]:
        assert (
            option["crossed_threshold"] + option["kept_observed_outcome"]
            + option["unidentifiable_trades"]
        ) == option["trades_with_excursion_evidence"]
    # Crossing at 10% pays the taker fee on the way out, so the counterfactual
    # sits below the raw threshold.
    assert by_value[0.10]["counterfactual_roi_of_crossing_trades"] == (
        pytest.approx(0.0722, abs=1e-3)
    )
    assert target["basis"] == "observational"
    assert "lower bound on the true excursion" in target["measurement_note"]


def test_stop_loss_needs_retained_adverse_excursion_to_be_identifiable():
    """Without a retained trough a stop can only be a versioned baseline."""
    result = _recommend_rich()
    stop = next(
        item for item in result["field_recommendations"]
        if item["field"] == "stop_loss"
    )

    assert stop["evidence_mode"] == "excursion"
    assert stop["basis"] == "baseline_fallback"
    assert stop["baseline_version"] == BASELINE_CHEAT_SHEET_VERSION
    assert all(
        option["trades_with_excursion_evidence"] == 0
        for option in stop["options"]
    )

    trades, opportunities = _rich_history()
    for row in trades:
        # Losers drew down hard; the winner never traded below its entry.
        row["lowest_exit_value"] = (
            0.45 if float(row["realized_pnl"]) > 0 else 0.30
        )
    policy = TradingPolicy()
    current = {
        field: getattr(policy, field)
        for field in policy.__dataclass_fields__
    }
    scored = recommend_policy(
        closed_trades=trades,
        opportunities=opportunities,
        current_policy=current,
        objective="balanced",
        target_trades_per_hour=2.0,
    )
    identified = next(
        item for item in scored["field_recommendations"]
        if item["field"] == "stop_loss"
    )
    by_value = {option["value"]: option for option in identified["options"]}

    assert identified["basis"] == "observational"
    # A 0.30 trough on a 0.45 entry is a -33% excursion, so every candidate
    # stop from 10% to 25% was breached by the two losing trades per event.
    assert by_value[0.10]["crossed_threshold"] == 24
    assert by_value[0.25]["crossed_threshold"] == 24
    assert by_value[0.10]["kept_observed_outcome"] == 12
    # Stopping out earlier caps the loss, so the tightest stop wins here.
    assert identified["suggested"] == 0.10
    assert by_value[0.10]["counterfactual_roi_of_crossing_trades"] > (
        by_value[0.25]["counterfactual_roi_of_crossing_trades"]
    )


def test_excursion_option_is_not_suggested_when_its_own_rule_fired_first():
    """A threshold the actual exit rule pre-empted has no observed continuation."""
    trades, opportunities = _rich_history()
    for row in trades:
        row["lowest_exit_value"] = 0.44
        # Every trade was closed by the stop family itself.
        row["exit_reason"] = "stop_loss"
    policy = TradingPolicy()
    current = {
        field: getattr(policy, field)
        for field in policy.__dataclass_fields__
    }
    result = recommend_policy(
        closed_trades=trades,
        opportunities=opportunities,
        current_policy=current,
        objective="balanced",
        target_trades_per_hour=2.0,
    )
    stop = next(
        item for item in result["field_recommendations"]
        if item["field"] == "stop_loss"
    )

    # A 0.44 trough on a 0.45 entry never reaches a 10% adverse move, and the
    # position's own stop already fired, so no candidate is comparable.
    assert all(
        option["unidentifiable_trades"] == len(trades)
        for option in stop["options"]
    )
    assert stop["basis"] == "baseline_fallback"
    assert stop["suggested"] == 0.20


def test_measurability_follows_the_data_not_a_static_field_attribute():
    """Spread and depth are scoreable only on trades opened after the v12 migration."""
    trades, opportunities = _rich_history()
    policy = TradingPolicy()
    current = {
        field: getattr(policy, field)
        for field in policy.__dataclass_fields__
    }
    # Opportunities carry spread/depth; these older closed trades do not.
    result = recommend_policy(
        closed_trades=trades,
        opportunities=opportunities,
        current_policy=current,
        objective="balanced",
        target_trades_per_hour=2.0,
    )
    records = {item["field"]: item for item in result["field_recommendations"]}

    for field in ("max_spread", "min_book_shares"):
        record = records[field]
        assert record["evidence_mode"] == "marginal"
        # Frequency is measurable because opportunities carry the value...
        assert any(
            option["qualified_observations"] > 0
            for option in record["options"]
        )
        # ...but every closed trade predates the column, so no value can be
        # scored and the versioned baseline is shown instead.
        assert record["basis"] == "baseline_fallback"
        assert record["baseline_version"] == BASELINE_CHEAT_SHEET_VERSION
        assert all(
            option["unmeasurable_trades"] == len(trades)
            for option in record["options"]
        )
        assert "unmeasurable_trades" in record["measurement_note"]

    # With the column present on closed trades, the same field becomes
    # scoreable without any catalog change.
    for row in trades:
        row["spread"] = 0.02 if float(row["realized_pnl"]) > 0 else 0.07
    scored = recommend_policy(
        closed_trades=trades,
        opportunities=opportunities,
        current_policy=current,
        objective="balanced",
        target_trades_per_hour=2.0,
    )
    spread = next(
        item for item in scored["field_recommendations"]
        if item["field"] == "max_spread"
    )
    assert spread["basis"] == "observational"
    assert all(
        option["unmeasurable_trades"] == 0 for option in spread["options"]
    )
    by_value = {option["value"]: option for option in spread["options"]}
    # The tight band isolates every profitable trade and shows a far better
    # ROI, but it only covers 12 trades - below the directional threshold.
    assert by_value[0.02]["all_history"]["turnover_roi"] == pytest.approx(0.30)
    assert by_value[0.02]["support"] == "sparse"
    assert by_value[0.08]["all_history"]["turnover_roi"] < 0
    assert by_value[0.08]["support"] == "directional"
    # A sparse high-ROI band must not be presented as the optimized setting,
    # even though it looks best on the retained sample.
    assert spread["suggested"] == 0.08


def test_marginal_sweep_falls_back_to_a_versioned_baseline_when_sparse():
    trades, opportunities = _rich_history()
    policy = TradingPolicy()
    current = {
        field: getattr(policy, field)
        for field in policy.__dataclass_fields__
    }
    # Strip the newer columns so nothing can be scored locally.
    for row in trades:
        row.pop("source_agreement", None)
    for row in opportunities:
        row.pop("source_agreement", None)

    result = recommend_policy(
        closed_trades=trades,
        opportunities=opportunities,
        current_policy=current,
        objective="balanced",
        target_trades_per_hour=2.0,
    )
    record = next(
        item for item in result["field_recommendations"]
        if item["field"] == "min_source_agreement"
    )

    assert record["basis"] == "baseline_fallback"
    assert record["baseline_version"] == BASELINE_CHEAT_SHEET_VERSION
    assert record["suggested"] == 0.0
    # Every option must declare how much of the sample it could not measure.
    assert all(
        option["unmeasurable_observations"] == len(opportunities)
        for option in record["options"]
    )
