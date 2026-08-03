import asyncio
import os
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest

import app.main as main_module
from app.main import app, store
from app.models import Event, Quote, Signal


def login(client):
    response = client.post("/api/login", data={"username": "admin", "password": "admin"})
    assert response.status_code == 200
    client.headers.update({"X-CSRF-Token": response.json()["csrf_token"]})


def create_manual_event(client):
    login(client)
    response = client.post("/api/events", json={
        "name": "Away at Home", "sport": "basketball", "home": "Home", "away": "Away"
    })
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def enabled_live_trading(monkeypatch, tmp_path):
    trading_db = tmp_path / "polymarket-us-trading.db"
    dry_run_db = tmp_path / "polymarket-us-dry-run.db"
    monkeypatch.setenv("POLYMARKET_US_TRADING_DB", str(trading_db))
    monkeypatch.setenv("POLYMARKET_US_DRY_RUN_DB", str(dry_run_db))
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(
            main_module.settings,
            workstation_mode=False,
            enable_polymarket_us_trading=True,
            database_url="",
            polymarket_us_trading_db=trading_db,
            polymarket_us_dry_run_db=dry_run_db,
        ),
    )


def test_registered_event_can_be_removed():
    with TestClient(app) as client:
        event_id = create_manual_event(client)["event"]["id"]

        removed = client.delete(f"/api/events/{event_id}")
        assert removed.status_code == 204
        assert client.delete(f"/api/events/{event_id}").status_code == 204
        assert event_id not in store.events
        assert event_id not in store.states
        assert event_id not in store.quotes
        assert event_id not in store.signals


def test_event_view_excludes_heavy_internal_snapshot_but_keeps_ui_fields():
    """The per-signal input_snapshot_json embeds the whole evaluation request and
    is re-serialized for every signal of every event on each SSE push; leaving it
    in the client snapshot can OOM the fan-out. It must be dropped from the view
    while the fields the dashboard renders survive."""
    with TestClient(app) as client:
        event_id = create_manual_event(client)["event"]["id"]
        store.set_signals(event_id, [Signal(
            event_id=event_id, market="moneyline", outcome="home",
            model_probability=0.6, market_probability=0.55, edge=0.05,
            confidence=0.9, action="WATCH", reasons=["r"],
            input_snapshot_json="X" * 1_000_000,
        )])
        view = client.get(f"/api/events/{event_id}").json()
        assert view["signals"], "signal should be present in the view"
        signal = view["signals"][0]
        assert "input_snapshot_json" not in signal
        assert signal["market"] == "moneyline" and signal["edge"] == 0.05


def test_final_event_view_cannot_return_a_recommendation():
    event_id = "final-recommendation-fixture"
    store.add_event(Event(
        id=event_id, name="Away at Home", sport="basketball",
        home="Home", away="Away",
    ))
    try:
        store.add_quotes([Quote(
            event_id, "moneyline", "Home", .55, "Polymarket",
            bid=.54, ask=.55, token_id="final-token",
        )])
        store.set_signals(event_id, [Signal(
            event_id=event_id, market="moneyline", outcome="Home",
            model_probability=.65, market_probability=.55, edge=.10,
            confidence=90, action="PAPER_BET", reasons=["qualified before final"],
            quote_source="Polymarket", n_reference_sources=2,
            required_edge=.03, token_id="final-token",
            consensus_probability=.65, calibrated_consensus_probability=.65,
            net_expected_value_per_share=.10,
        )])
        main_module._terminal_events[event_id] = "final"

        view = asyncio.run(main_module.event_view(event_id, positions=[]))
        market = view["actionable_markets"][0]

        assert market["entry_action"] == "WAIT"
        assert market["new_entry_eligible"] is False
        assert market["recommendation_eligible"] is False
        assert "final or no longer active" in market["why_no_entry"]
    finally:
        main_module._terminal_events.pop(event_id, None)
        store.remove_event(event_id)


def test_dashboard_contains_merged_ui_behaviors():
    with TestClient(app) as client:
        html = client.get("/").text
        javascript = client.get("/static/index.js").text
        assert "data-remove-event" in javascript
        assert "lastEvents=lastEvents.filter" in javascript
        assert 'fetch(`/api/events/${encodeURIComponent(eventId)}`,{method:"DELETE"})' in javascript
        assert "details[open][data-detail-key]" in javascript
        assert "Paste Polymarket link" in html
        assert "data-save-position" in javascript
        assert "Signal quality" in javascript
        assert "Edge buffer" in javascript or "edge_buffer" in javascript
        assert "Allow logical automatic cash-out" in html
        assert 'data-trading-lane="dry_run"' in html
        assert 'data-trading-lane="live"' in html
        assert "Both lanes may run at the same time" in html
        assert "function tradingApi" in javascript
        assert "switchTradingLane" in javascript
        assert 'id="us-lane-coordination-status"' in html
        assert 'id="us-policy-save-status"' in html
        assert "showPolicySaveNotice" in javascript
        assert "policyErrorTarget" in javascript
        assert '<option value="combined" selected>' in html
        assert "Analyze all retained data" in html
        assert "us-policy-advisor-sources" in javascript
        assert 'id="us-cycle-seconds"' in html
        assert 'min="1" max="300" step="0.5"' in html
        assert "does not make a separate Odds API request" in html
        assert "data-cashout-toggle" in javascript
        assert 'id="discover-monitor-selected"' in html
        assert "selectedDiscoverSlugs" in javascript
        assert "Discovery remains open" in javascript
        assert "is already monitored" in javascript
        assert 'id="discover-refresh-status"' in html
        assert 'refreshQuery=manual?"refresh=true":""' in javascript
        assert "league=${encodeURIComponent(discoverLeague)}" in javascript
        assert 'id="bot-activity"' in html
        assert "data-remove-bot" in javascript
        assert 'fetch(`/api/accounts/${encodeURIComponent(name)}`' in javascript
        assert 'id="bot-action-status"' in html
        assert "pendingBotRemovals" in javascript
        assert "pendingEventRemovals" in javascript
        assert "streamConnected" in javascript
        assert "loadChartLibrary" in javascript
        assert 'id="event-action-status"' in html
        assert "Removing…" in javascript
        assert "It can no longer trade" in javascript
        assert "per_event_limit=4" in javascript
        assert "activityCoverage" in javascript
        assert "Exact failed engine gate" in javascript
        assert "All monitored events" in javascript
        assert 'id="event-navigator"' in html
        assert "data-jump-event" in javascript
        assert "if(button)gotoEvent(button.dataset.jumpEvent)" in javascript
        assert 'activeLine="all"' in javascript
        assert "scrollIntoView" in javascript
        assert "if (!m.token_id || !m.market_slug) continue;" in javascript
        assert "const BEST_BET_MIN_BUY_PRICE = 0.05;" in javascript
        assert "const BEST_BET_MAX_BUY_PRICE = 0.95;" in javascript
        assert "if (!bestBetBuyPriceAllowed(m.buy_price)) continue;" in javascript
        assert 'if (m.edge == null) continue;' in javascript
        assert 'if (m.edge <= 0 && m.entry_action !== "ENTRY WINDOW") continue;' in javascript
        assert "return rows.slice(0, BEST_BETS_LIMIT)" in javascript
        assert "No positive-edge selections right now." in javascript
        assert "bestBetExclusionReason" not in javascript
        assert "best_bet_candidate" not in javascript
        assert "US QUALIFIED" in javascript
        assert "RESEARCH ONLY" in javascript
        assert "POSITION OPEN" in javascript
        assert "bestBetExecution" in javascript
        assert "last_cycle_evaluations" in javascript
        assert (
            "each row shows whether an exact polymarket us contract"
            in html.casefold()
        )
        assert 'id="odds-api-toggle"' in html
        assert 'id="odds-api-interval"' in html
        assert 'id="odds-api-interval-save"' in html
        assert 'min="1" max="3600"' in html
        assert "odds_api_enabled" in javascript
        assert "odds_api_poll_seconds" in javascript
        assert "Apply interval" in javascript
        assert 'data-entry-market-scope="first_inning"' in html
        assert 'data-entry-market-scope="first_five_innings"' in html
        assert 'id="us-allow-live-segments"' in html
        assert "allowed_market_scopes" in javascript
        assert "allow_live_segment_markets" in javascript
        assert "segment_research" in javascript
        assert 'fetch("/api/config", {' in javascript
        assert "backend pollers are paused" in javascript
        assert 'id="tab-us-research"' in html
        assert 'id="us-key-status"' in html
        assert 'id="us-events"' in html
        assert 'id="us-account"' in html
        assert "/api/polymarket-us/status" in javascript
        assert "/api/polymarket-us/events" in javascript
        assert "/api/polymarket-us/account" in javascript
        assert "/api/polymarket-us/trading/status" in javascript
        assert "/api/polymarket-us/trading/sync" in javascript
        assert "/api/polymarket-us/trading/config" in javascript
        assert 'id="us-lane-automation-toggle"' in html
        assert "will continue server-side after this page closes" in javascript
        assert "/api/polymarket-us/trading/adaptive-exit/history" in javascript
        assert "/api/polymarket-us/trading/arm" in javascript
        assert "/api/polymarket-us/trading/emergency-stop" in javascript
        assert "Stop automation now" in html
        assert 'id="us-stop-status"' in html
        assert "Automation is OFF" in javascript
        assert "refreshTradingInBackground" in javascript
        assert "Raw long bid" in javascript
        assert 'id="us-arm-confirmation" type="checkbox"' in html
        assert "I approve live orders for the selected duration" in html
        assert 'const APPROVAL_TOKEN = "approve";' in javascript
        assert "window.prompt" not in javascript
        assert 'id="us-arm-duration"' in html
        assert '<option value="14400">4 hours</option>' in html
        assert "updateArmDurationLabel" in javascript
        assert "one-cent gain alone never triggers an exit" in html.casefold()
        assert 'id="us-min-locked-profit"' in html
        assert "minimum_locked_profit" in javascript
        assert "fee-adjusted cash-out" in javascript
        assert "profit protection" in javascript
        assert "usTradingFormDirty" in javascript
        assert "requestEpoch !== usTradingHydrationEpoch" in javascript
        assert "Save execution policy · unsaved" in javascript
        assert "source edge" in javascript
        assert "configured_min_book_shares" in javascript
        assert "authenticated_book_state" in javascript
        assert "observed value and configured threshold" in html
        assert 'id="us-performance-summary"' in html
        assert "function renderTradingPerformance" in javascript
        assert "/api/polymarket-us/trading/performance" in javascript
        assert "/api/polymarket-us/trading/performance/reset-live" in javascript
        assert "Reset live tally" in html
        assert 'id="us-performance-action-status"' in html
        assert "/api/polymarket-us/trading/risk-session/reset" in javascript
        assert 'id="us-risk-session-reset"' in html
        assert "Start new risk session" in html
        assert "Start a fresh hourly-entry and rolling realized-loss window?" in javascript
        assert "does not erase P/L or bypass position stops" in javascript
        assert "/api/polymarket-us/trading/liquidate" in javascript
        assert "/api/polymarket-us/trading/history/dry-run" in javascript
        assert 'data-engine-gate="provider_freshness"' in html
        assert "required_engine_gates" in javascript
        assert "Use core gates" in html
        assert "Always enforced:" in html
        assert "data-exit-position" in javascript
        assert "/positions/${encodeURIComponent(positionId)}/exit" in javascript
        assert 'id="us-clear-exited-positions"' in html
        assert "/api/polymarket-us/trading/positions/archive-exited" in javascript
        assert "Performance tallies, model observations, and the execution journal will be preserved." in javascript
        assert "Stop accepted locally" in javascript
        assert "usTradingReloadQueued" in javascript
        assert 'step="any"' in html
        assert "Total net" in javascript
        assert "W–L–P" in javascript
        assert 'id="us-liquidate-form"' in html
        assert 'id="us-liquidate-confirmation" type="checkbox"' in html
        assert "I approve attempts to sell every open live position" in html
        assert 'class="us-activity-grid section-gap research-anchor"' in html
        assert 'id="us-dry-tally-reset"' in html
        assert "/api/polymarket-us/trading/performance/reset-dry-run" in javascript
        assert "Stop automation + wipe dry-run trades" in html
        assert "Dry run is one atomic reset" in html
        assert 'id="us-clear-dry-history"' not in html
        assert "No market mapping, quote, or fill is required" in javascript
        reset_start = javascript.index(
            'document.querySelector("#us-liquidate-form")?.addEventListener("submit"'
        )
        reset_handler = javascript[
            reset_start:javascript.index("updateLiquidationMode();", reset_start)
        ]
        assert "/api/polymarket-us/trading/history/dry-run" in reset_handler
        assert "/api/polymarket-us/trading/stop" not in reset_handler
        assert "Sync phone/account" in html
        assert "total paid" in javascript
        assert "Protective auto-exits stay armed" in javascript
        assert 'id="us-adaptive-exit-enabled"' in html
        assert "Adaptive MLB cash-out research" in html
        assert "adverse-move forecast" in javascript
        assert "Clear only the retained adaptive MLB movement history?" in javascript
        assert 'id="model-lab-export"' in html
        assert 'id="model-lab-targets"' in html
        assert "/api/model-lab/export" in javascript
        assert "Archive research snapshot" in html
        assert "after_cost_strategy_pnl" not in javascript
        assert 'id="us-policy-advisor-refresh"' in html
        assert 'id="us-policy-advisor-objective"' in html
        assert 'id="us-policy-advisor-mode"' in html
        assert 'id="us-policy-advisor-lookback"' in html
        assert 'id="us-policy-advisor-download"' in html
        assert 'data-advisor-market-type="moneyline"' in html
        assert "/api/polymarket-us/trading/policy-advisor/recommend" in javascript
        assert "Apply these suggested execution filters?" in javascript
        assert "Previous suggestion applied successfully" in javascript
        assert "Analyze again before applying" in javascript
        assert "Preview exploratory filters" in javascript
        assert "previewPolicyAdvice" in javascript
        assert "were loaded into the execution form but were not saved" in javascript
        assert "fetchWithDeadline" in javascript
        assert "exceeded 20 seconds" in javascript
        assert "Validation blockers:" in javascript
        assert 'data-entry-market-type="moneyline"' in html
        assert 'data-entry-market-type="spread"' in html
        assert 'data-entry-market-type="total"' in html
        assert 'id="us-max-edge"' in html
        assert 'id="us-max-quality"' in html
        assert 'id="us-execution-state-chips"' in html
        assert 'id="us-execution-blockers"' in html
        assert 'id="us-authorization-matrix"' in html
        # Guided setup steps wrap the existing fields in document order.
        for step in (
            "capital", "authorization", "limits",
            "behavior", "advanced", "review",
        ):
            assert f'data-policy-step="{step}"' in html
        assert 'id="us-policy-rail"' in html
        assert 'id="us-policy-steps"' in html
        assert 'id="us-effective-policy-review"' in html
        # Native validation cannot focus a control inside a hidden step, so the
        # form opts out and the submit handler reveals the owning step instead.
        assert 'id="us-trading-form" class="us-trading-form" novalidate' in html
        assert "revealFieldStep" in javascript
        assert 'data-profile-field="max_profile_exposure_usd"' in html
        assert 'data-profile-field="max_profile_open_positions"' in html
        assert 'data-profile-field="max_profile_orders_per_hour"' in html
        assert 'id="us-global-entry-enabled"' in html
        assert 'id="us-profile-copy-global"' in html
        assert 'data-profile-field="max_signal_quality"' in html
        assert 'data-profile-field="min_source_agreement"' in html
        assert 'id="us-entry-confirmation-readings"' in html
        assert 'id="us-max-event-entries-hour"' in html
        assert 'id="us-candidate-cooldown"' in html
        assert 'id="us-min-mlb-remaining"' in html
        assert 'id="us-ledger-market-type"' in html
        assert 'id="us-ledger-export"' in html
        assert "/api/polymarket-us/trading/performance-ledger" in javascript
        assert 'data-label="Opened"' in javascript
        assert "Synchronize local and hosted research evidence" in html
        assert "live and dry-run rows keep their source lane" in javascript.casefold()


def test_workstation_exposes_only_bounded_polymarket_us_trading_routes():
    paths = {route.path for route in app.routes}
    us_paths = {path for path in paths if path.startswith("/api/polymarket-us")}

    assert us_paths == {
        "/api/polymarket-us/status",
        "/api/polymarket-us/events",
        "/api/polymarket-us/account",
        "/api/polymarket-us/runtime-credentials",
        "/api/polymarket-us/trading/status",
        "/api/polymarket-us/trading/sync",
        "/api/polymarket-us/trading/config",
        "/api/polymarket-us/trading/adaptive-exit/history",
        "/api/polymarket-us/trading/arm",
        "/api/polymarket-us/trading/disarm",
        "/api/polymarket-us/trading/stop",
        "/api/polymarket-us/trading/emergency-stop",
        "/api/polymarket-us/trading/run",
        "/api/polymarket-us/trading/journal",
        "/api/polymarket-us/trading/positions",
        "/api/polymarket-us/trading/positions/archive-exited",
        "/api/polymarket-us/trading/performance",
        "/api/polymarket-us/trading/performance-ledger",
        "/api/polymarket-us/trading/performance/reset-live",
        "/api/polymarket-us/trading/performance/reset-dry-run",
        "/api/polymarket-us/trading/policy-advisor/recommend",
        "/api/polymarket-us/trading/policy-advisor/history",
        "/api/polymarket-us/trading/policy-advisor/sessions",
        "/api/polymarket-us/trading/policy-advisor/model-readiness",
        "/api/polymarket-us/trading/policy-advisor/{advice_id}/apply",
        "/api/polymarket-us/trading/risk-session/reset",
        "/api/polymarket-us/trading/liquidate",
        "/api/polymarket-us/trading/history/dry-run",
        "/api/polymarket-us/trading/positions/{position_id}/exit",
    }
    # There is deliberately no arbitrary order-management route. The one
    # position-exit route can only act on a position created and tracked by the
    # bounded automation manager.
    assert not any(
        fragment in path
        for path in us_paths
        for fragment in ("/orders", "/create", "/modify", "/cancel", "/close")
    )


def test_live_trading_api_defaults_disarmed_and_rejects_unsafe_policy(
    enabled_live_trading,
):
    with TestClient(app) as client:
        # Authenticate directly so this route test does not consume a shared
        # login-throttle slot and make the later rate-limit test order-dependent.
        authenticated = main_module.auth_manager.login("admin", "admin")
        assert authenticated is not None
        token, session = authenticated
        client.cookies.set(main_module._cookie_name("session_token"), token)
        client.cookies.set(main_module._cookie_name("csrf_token"), session.csrf_token)
        client.headers.update({"X-CSRF-Token": session.csrf_token})
        try:
            configured = client.put("/api/polymarket-us/trading/config", json={
                "automation_enabled": False,
                "execution_mode": "dry_run",
                "allowed_market_types": ["moneyline", "total"],
                "adaptive_exit_enabled": True,
                "adaptive_exit_profile": "observe",
                "adaptive_exit_horizon_minutes": 2.5,
                "adaptive_exit_min_samples": 20,
                "adaptive_exit_max_tightening": 0.25,
                "volatility_stop_enabled": True,
                "stateless_stop_confirmation": True,
                "stop_confirmation_readings": 4,
                "stop_grace_minutes": 2.5,
                "catastrophic_stop_multiplier": 1.8,
                "reversal_confirmation_readings": 5,
                "post_exit_tracking_minutes": 45,
                "minimum_locked_profit": 0.025,
            })
            assert configured.status_code == 200
            status = configured.json()
            assert status["armed"] is False
            assert status["policy"]["execution_mode"] == "dry_run"
            assert status["policy"]["automation_enabled"] is False
            assert status["policy"]["allowed_market_types"] == [
                "moneyline",
                "total",
            ]
            assert status["policy"]["adaptive_exit_enabled"] is True
            assert status["policy"]["adaptive_exit_horizon_minutes"] == 2.5
            assert status["policy"]["volatility_stop_enabled"] is True
            assert status["policy"]["stateless_stop_confirmation"] is True
            assert status["policy"]["stop_confirmation_readings"] == 4
            # The UI saves through this whitelist model; a field missing here
            # is silently dropped, which is exactly how the reversal-readings
            # control appeared to "stick" at its old value.
            assert status["policy"]["reversal_confirmation_readings"] == 5
            assert status["policy"]["post_exit_tracking_minutes"] == 45
            assert status["policy"]["minimum_locked_profit"] == pytest.approx(
                0.025
            )
            assert status["adaptive_exit"]["observations"] == 0
            assert status["restart_behavior"] == "always_disarmed"

            performance = client.get("/api/polymarket-us/trading/performance")
            assert performance.status_code == 200
            summary = performance.json()
            assert set(summary["modes"]) == {"dry_run", "live"}
            assert summary["combined"]["mode"] == "combined"

            ledger = client.get(
                "/api/polymarket-us/trading/performance-ledger",
                params={
                    "mode": "dry_run",
                    "market_type": "moneyline",
                    "result": "all",
                },
            )
            assert ledger.status_code == 200
            assert ledger.json()["filters"]["market_type"] == "moneyline"
            exported = client.get(
                "/api/polymarket-us/trading/performance-ledger",
                params={"format": "csv", "market_type": "moneyline"},
            )
            assert exported.status_code == 200
            assert exported.headers["content-type"].startswith("text/csv")
            assert "entry_policy" in exported.text.splitlines()[0]

            unsafe = client.put("/api/polymarket-us/trading/config", json={
                "min_entry_price": 0.05,
            })
            assert unsafe.status_code == 400
            assert "5c" in unsafe.json()["detail"]

            arm = client.post("/api/polymarket-us/trading/arm", json={
                "confirmation": " APPROVE ",
                "seconds": 1800,
            })
            assert arm.status_code == 400
            assert "enable automation" in arm.json()["detail"]

            rejected_clear = client.request(
                "DELETE",
                "/api/polymarket-us/trading/adaptive-exit/history",
                json={"confirmation": "clear it"},
            )
            assert rejected_clear.status_code == 409
            cleared = client.request(
                "DELETE",
                "/api/polymarket-us/trading/adaptive-exit/history",
                json={"confirmation": "APPROVE"},
            )
            assert cleared.status_code == 200
            assert cleared.json()["retained_positions"] is True
            assert cleared.json()["retained_journal"] is True
        finally:
            main_module.auth_manager.revoke(token)


def test_live_and_dry_run_lanes_keep_independent_policies_and_controls(
    enabled_live_trading,
):
    with TestClient(app) as client:
        authenticated = main_module.auth_manager.login("admin", "admin")
        assert authenticated is not None
        token, session = authenticated
        client.cookies.set(main_module._cookie_name("session_token"), token)
        client.cookies.set(main_module._cookie_name("csrf_token"), session.csrf_token)
        client.headers.update({"X-CSRF-Token": session.csrf_token})
        try:
            assert main_module.live_trader is not None
            assert main_module.dry_run_trader is not None
            assert main_module.live_trader.path != main_module.dry_run_trader.path

            live = client.put(
                "/api/polymarket-us/trading/config",
                params={"lane": "live"},
                json={
                    "execution_mode": "dry_run",
                    "automation_enabled": True,
                    "min_edge": 0.071,
                    "cycle_seconds": 2.5,
                },
            )
            dry = client.put(
                "/api/polymarket-us/trading/config",
                params={"lane": "dry_run"},
                json={
                    "execution_mode": "live",
                    "automation_enabled": True,
                    "min_edge": 0.012,
                    "cycle_seconds": 1.5,
                },
            )
            assert live.status_code == 200
            assert dry.status_code == 200
            assert live.json()["policy"]["execution_mode"] == "live"
            assert dry.json()["policy"]["execution_mode"] == "dry_run"

            live_status = client.get(
                "/api/polymarket-us/trading/status",
                params={"lane": "live"},
            ).json()
            dry_status = client.get(
                "/api/polymarket-us/trading/status",
                params={"lane": "dry_run"},
            ).json()
            assert live_status["policy"]["min_edge"] == pytest.approx(0.071)
            assert live_status["policy"]["cycle_seconds"] == pytest.approx(2.5)
            assert dry_status["policy"]["min_edge"] == pytest.approx(0.012)
            assert dry_status["policy"]["cycle_seconds"] == pytest.approx(1.5)
            assert set(dry_status["lanes"]) == {"live", "dry_run"}

            stopped_dry = client.post(
                "/api/polymarket-us/trading/stop",
                params={"lane": "dry_run"},
            )
            assert stopped_dry.status_code == 200
            assert stopped_dry.json()["lane"] == "dry_run"
            assert (
                stopped_dry.json()["lanes"]["live"]["automation_enabled"]
                is True
            )
            assert client.get(
                "/api/polymarket-us/trading/status",
                params={"lane": "dry_run"},
            ).json()["policy"]["automation_enabled"] is False
            assert client.get(
                "/api/polymarket-us/trading/status",
                params={"lane": "live"},
            ).json()["policy"]["automation_enabled"] is True

            resumed_dry = client.put(
                "/api/polymarket-us/trading/config",
                params={"lane": "dry_run"},
                json={"automation_enabled": True},
            )
            assert resumed_dry.status_code == 200
            cleared_dry = client.request(
                "DELETE",
                "/api/polymarket-us/trading/history/dry-run",
                params={"lane": "dry_run"},
                json={"confirmation": "approve"},
            )
            assert cleared_dry.status_code == 200
            assert client.get(
                "/api/polymarket-us/trading/status",
                params={"lane": "dry_run"},
            ).json()["policy"]["automation_enabled"] is False
            assert client.get(
                "/api/polymarket-us/trading/status",
                params={"lane": "live"},
            ).json()["policy"]["automation_enabled"] is True
        finally:
            main_module.auth_manager.revoke(token)


def test_dry_reset_route_stops_automation_in_one_request_and_live_tally_resets(
    enabled_live_trading,
):
    with TestClient(app) as client:
        authenticated = main_module.auth_manager.login("admin", "admin")
        assert authenticated is not None
        token, session = authenticated
        client.cookies.set(main_module._cookie_name("session_token"), token)
        client.cookies.set(main_module._cookie_name("csrf_token"), session.csrf_token)
        client.headers.update({"X-CSRF-Token": session.csrf_token})
        try:
            configured = client.put(
                "/api/polymarket-us/trading/config",
                json={
                    "automation_enabled": True,
                    "execution_mode": "dry_run",
                },
            )
            assert configured.status_code == 200
            assert configured.json()["policy"]["automation_enabled"] is True

            reset = client.request(
                "DELETE",
                "/api/polymarket-us/trading/history/dry-run",
                json={"confirmation": "approve"},
            )
            assert reset.status_code == 200
            assert reset.json()["automation_enabled"] is False
            assert reset.json()["verified_remaining_positions"] == 0
            status = client.get("/api/polymarket-us/trading/status")
            assert status.json()["policy"]["automation_enabled"] is False

            live_tally = client.post(
                "/api/polymarket-us/trading/performance/reset-live",
                json={"confirmation": "APPROVE"},
            )
            assert live_tally.status_code == 200
            assert live_tally.json()["positions_preserved"] is True
            assert live_tally.json()["risk_history_preserved"] is True

            rejected_risk_reset = client.post(
                "/api/polymarket-us/trading/risk-session/reset",
                json={"confirmation": "reset"},
            )
            assert rejected_risk_reset.status_code == 409
            risk_reset = client.post(
                "/api/polymarket-us/trading/risk-session/reset",
                json={"confirmation": "approve"},
            )
            assert risk_reset.status_code == 200
            assert risk_reset.json()["current"]["orders_last_hour"] == 0
            assert risk_reset.json()["current"]["realized_loss_24h_usd"] == 0
            assert risk_reset.json()["per_position_stop_loss_preserved"] is True
        finally:
            main_module.auth_manager.revoke(token)


def test_policy_advisor_api_reports_model_readiness_and_applies_explicitly(
    enabled_live_trading,
):
    with TestClient(app) as client:
        authenticated = main_module.auth_manager.login("admin", "admin")
        assert authenticated is not None
        token, session = authenticated
        client.cookies.set(main_module._cookie_name("session_token"), token)
        client.cookies.set(main_module._cookie_name("csrf_token"), session.csrf_token)
        client.headers.update({"X-CSRF-Token": session.csrf_token})
        try:
            recommended = client.post(
                "/api/polymarket-us/trading/policy-advisor/recommend",
                json={
                    "objective": "more_trades",
                    "target_trades_per_hour": 8,
                    "analysis_mode": "live",
                    "lookback_days": 30,
                    "market_types": ["moneyline"],
                },
            )
            assert recommended.status_code == 200
            advice = recommended.json()
            assert advice["model_used_to_change_settings"] is False
            assert advice["model_evidence"]["engine_impact"] == "none"
            assert advice["suggested_policy"]["max_orders_per_hour"] == 8
            assert advice["scope"] == {
                "analysis_mode": "live",
                "lookback_days": 30,
                "market_types": ["moneyline"],
            }
            assert advice["apply_allowed"] is False
            assert len(advice["source_policy_hash"]) == 64

            combined = client.post(
                "/api/polymarket-us/trading/policy-advisor/recommend",
                params={"lane": "live"},
                json={
                    "objective": "balanced",
                    "target_trades_per_hour": 4,
                    "analysis_mode": "combined",
                    "lookback_days": 0,
                    "market_types": ["moneyline", "spread", "total"],
                },
            )
            assert combined.status_code == 200
            combined_advice = combined.json()
            assert combined_advice["scope"]["analysis_mode"] == "combined"
            assert {
                source["lane"]
                for source in combined_advice["evidence"]["data_sources"]
            } == {"live", "dry_run"}
            assert combined_advice["apply_allowed"] is False
            assert "simulated and live" in combined_advice["evidence"][
                "execution_domain_warning"
            ]

            ledger = client.get(
                "/api/polymarket-us/trading/performance-ledger",
                params={"mode": "all"},
            )
            assert ledger.status_code == 200
            assert {
                source["lane"] for source in ledger.json()["data_sources"]
            } == {"live", "dry_run"}

            history = client.get(
                "/api/polymarket-us/trading/policy-advisor/history"
            )
            assert history.status_code == 200
            assert history.json()[0]["id"] == combined_advice["id"]
            sessions = client.get(
                "/api/polymarket-us/trading/policy-advisor/sessions"
            )
            assert sessions.status_code == 200
            assert sessions.json()
            readiness = client.get(
                "/api/polymarket-us/trading/policy-advisor/model-readiness"
            )
            assert readiness.status_code == 200
            assert readiness.json()["live_eligible"] is False

            rejected = client.post(
                f"/api/polymarket-us/trading/policy-advisor/{advice['id']}/apply",
                json={"confirmation": "apply"},
            )
            assert rejected.status_code == 409
            held_back = client.post(
                f"/api/polymarket-us/trading/policy-advisor/{advice['id']}/apply",
                json={"confirmation": "APPROVE"},
            )
            assert held_back.status_code == 409
            assert "diagnostic only" in held_back.json()["detail"]

            refreshed = client.post(
                "/api/polymarket-us/trading/policy-advisor/recommend",
                json={
                    "objective": "more_trades",
                    "target_trades_per_hour": 9,
                },
            )
            assert refreshed.status_code == 200
            assert refreshed.json()["id"] != advice["id"]
            assert refreshed.json()["apply_allowed"] is False
        finally:
            main_module.auth_manager.revoke(token)


def test_empty_mode_liquidation_returns_without_fetching_market_inventory(
    monkeypatch,
    enabled_live_trading,
):
    async def unexpected_fetch(*, limit):
        pytest.fail(f"empty liquidation unexpectedly fetched {limit} US events")

    with TestClient(app) as client:
        authenticated = main_module.auth_manager.login("admin", "admin")
        assert authenticated is not None
        token, session = authenticated
        client.cookies.set(main_module._cookie_name("session_token"), token)
        client.cookies.set(main_module._cookie_name("csrf_token"), session.csrf_token)
        client.headers.update({"X-CSRF-Token": session.csrf_token})
        assert main_module.live_trader is not None
        monkeypatch.setattr(
            main_module.live_trader,
            "positions",
            lambda *, open_only=False: [],
        )
        monkeypatch.setattr(
            main_module.live_trader,
            "liquidate_open_positions",
            lambda _payload, *, mode, confirmation: {
                "mode": mode,
                "requested": 0,
                "remaining": 0,
            },
        )
        monkeypatch.setattr(
            main_module,
            "fetch_polymarket_us_events",
            unexpected_fetch,
        )

        try:
            response = client.post(
                "/api/polymarket-us/trading/liquidate",
                json={"mode": "dry_run"},
            )

            assert response.status_code == 200
            assert response.json()["requested"] == 0
            assert response.json()["remaining"] == 0
        finally:
            main_module.auth_manager.revoke(token)


def test_individual_dry_run_exit_is_bounded_and_skips_market_inventory(
    monkeypatch,
    enabled_live_trading,
):
    async def unexpected_fetch(*, limit):
        pytest.fail(f"dry-run position removal unexpectedly fetched {limit} US events")

    with TestClient(app) as client:
        authenticated = main_module.auth_manager.login("admin", "admin")
        assert authenticated is not None
        token, session = authenticated
        client.cookies.set(main_module._cookie_name("session_token"), token)
        client.cookies.set(main_module._cookie_name("csrf_token"), session.csrf_token)
        client.headers.update({"X-CSRF-Token": session.csrf_token})
        assert main_module.live_trader is not None
        monkeypatch.setattr(
            main_module.live_trader,
            "position",
            lambda position_id: {
                "id": position_id,
                "mode": "dry_run",
                "status": "open",
            },
        )
        monkeypatch.setattr(
            main_module.live_trader,
            "exit_position",
            lambda _payload, *, position_id, confirmation: {
                "position_id": position_id,
                "status": "removed",
                "confirmation": confirmation,
            },
        )
        monkeypatch.setattr(
            main_module,
            "fetch_polymarket_us_events",
            unexpected_fetch,
        )

        try:
            response = client.post(
                "/api/polymarket-us/trading/positions/dry-1/exit",
                json={"confirmation": ""},
            )

            assert response.status_code == 200
            assert response.json()["position_id"] == "dry-1"
            assert response.json()["status"] == "removed"
        finally:
            main_module.auth_manager.revoke(token)


def test_best_current_bets_displays_existing_signal_quality():
    with TestClient(app) as client:
        javascript = client.get("/static/index.js").text

    start = javascript.index("function renderBestBets")
    end = javascript.index("function renderEvents", start)
    renderer = javascript[start:end]
    assert 'm.confidence == null ? "—" : Math.round(m.confidence)' in renderer
    assert '<div class="hint">signal quality</div>' in renderer


def test_odds_api_master_switch_updates_without_changing_auto_monitor(monkeypatch):
    class MonitorStub:
        def __init__(self):
            self.values = []

        def set_odds_api_enabled(self, value):
            self.values.append(value)

    monitor = MonitorStub()
    monkeypatch.setattr(
        main_module,
        "_config_state",
        {"auto_monitor": True, "odds_api_enabled": True},
    )
    monkeypatch.setattr(main_module, "monitor_state", monitor)

    response = asyncio.run(main_module.update_config(
        main_module.ConfigIn(odds_api_enabled=False)
    ))

    assert response["odds_api_enabled"] is False
    assert response["auto_monitor"] is True
    assert monitor.values == [False]


def test_odds_api_interval_updates_and_persists_without_restart(monkeypatch):
    class MonitorStub:
        def __init__(self):
            self.values = []

        def set_odds_api_poll_seconds(self, value):
            self.values.append(value)

    monitor = MonitorStub()
    monkeypatch.setattr(
        main_module,
        "_config_state",
        {
            "auto_monitor": False,
            "odds_api_enabled": True,
            "odds_api_poll_seconds": 45.0,
        },
    )
    monkeypatch.setattr(main_module, "monitor_state", monitor)

    response = asyncio.run(main_module.update_config(
        main_module.ConfigIn(odds_api_poll_seconds=1.5)
    ))

    assert response["odds_api_poll_seconds"] == 1.5
    assert response["odds_api_enabled"] is True
    assert monitor.values == [1.5]


def test_event_history_api_caps_default_and_requested_page_size(monkeypatch):
    class HistoryStub:
        def __init__(self):
            self.limits = []

        def get_event_history(self, event_id, *, after_ts=None, limit=None):
            self.limits.append((event_id, after_ts, limit))
            return {"quotes": [], "states": []}

    stub = HistoryStub()
    monkeypatch.setattr(main_module, "history_db", stub)
    assert asyncio.run(main_module.get_event_history_api("event")) == {
        "quotes": [], "states": []
    }
    assert asyncio.run(
        main_module.get_event_history_api("event", limit=999999)
    ) == {"quotes": [], "states": []}

    assert [call[2] for call in stub.limits] == [1200, 5000]


def test_bot_cashout_toggle_and_mark_feed_are_authenticated_api_contracts():
    with TestClient(app) as client:
        login(client)
        updated = client.patch(
            "/api/accounts/Engine%20Kelly", json={"cash_out_enabled": True}
        )
        assert updated.status_code == 200
        assert updated.json()["cash_out_enabled"] is True

        board = client.get("/api/leaderboard").json()
        account = next(item for item in board if item["name"] == "Engine Kelly")
        assert account["cash_out_enabled"] is True
        assert account["event_scope"] == []
        assert client.get("/api/accounts/Engine%20Kelly/marks").json() == []
        assert client.get(
            "/api/accounts/Engine%20Kelly/activity?limit=80&per_event_limit=8"
        ).status_code == 200
        assert client.get(
            "/api/bot-activity?limit=80&per_event_limit=4"
        ).status_code == 200

        restored = client.patch(
            "/api/accounts/Engine%20Kelly", json={"cash_out_enabled": False}
        )
        assert restored.status_code == 200


def test_custom_bot_can_be_removed_but_preset_bot_cannot():
    with TestClient(app) as client:
        login(client)
        name = "Disposable custom bot"
        created = client.post("/api/accounts", json={
            "name": name,
            "edge_threshold": .03,
            "sizing": "flat",
            "flat_stake": 25,
        })
        assert created.status_code == 201
        duplicate = client.post("/api/accounts", json={
            "name": name,
            "edge_threshold": .03,
            "sizing": "flat",
            "flat_stake": 25,
        })
        assert duplicate.status_code == 409
        account = next(
            item for item in client.get("/api/leaderboard").json()
            if item["name"] == name
        )
        assert account["is_custom"] is True

        removed = client.delete("/api/accounts/Disposable%20custom%20bot")
        assert removed.status_code == 204
        assert all(
            item["name"] != name for item in client.get("/api/leaderboard").json()
        )
        assert client.delete("/api/accounts/Engine%20Kelly").status_code == 409
        assert client.get("/api/bot-activity").status_code == 200


def test_position_can_be_saved_and_removed_for_a_visible_selection():
    with TestClient(app) as client:
        created = create_manual_event(client)
        event_id = created["event"]["id"]
        store.add_quotes([Quote(event_id, "moneyline", "home", .52, "Polymarket",
                                bid=.51, ask=.53, token_id="token-1")])
        saved = client.put(f"/api/events/{event_id}/positions", json={
            "token_id": "token-1", "market": "moneyline", "outcome": "home",
            "shares": 20, "avg_entry_price": .48,
        })
        assert saved.status_code == 200
        assert saved.json()["positions"][0]["advice"] in {
            "HOLD", "HOLD / MONITOR", "CONSIDER CASH", "EXIT WATCH"
        }
        removed = client.delete(f"/api/events/{event_id}/positions/token-1")
        assert removed.status_code == 204


# --- Phase 0.7: watch-page / dashboard shared auth + CSRF contract -----------

_EVENT_BODY = {"name": "Away at Home", "sport": "basketball", "home": "Home", "away": "Away"}


def test_add_event_requires_an_authenticated_session():
    with TestClient(app) as client:
        assert client.post("/api/events", json=_EVENT_BODY).status_code == 401


def test_discover_requires_authentication():
    with TestClient(app) as client:
        assert client.get("/api/discover").status_code == 401


def test_manual_discovery_refresh_bypasses_the_short_cache(monkeypatch):
    calls = []

    async def fake_discovery(**kwargs):
        calls.append(kwargs)
        return [{"slug": "fresh", "title": "Fresh game"}]

    async def fake_schedule(day=None):
        return []

    monkeypatch.setattr(main_module, "polymarket_sports_events", fake_discovery)
    monkeypatch.setattr(main_module, "fetch_mlb_schedule", fake_schedule)
    main_module._discover_cache[""] = {
        "at": main_module.time.monotonic(),
        "data": [{"slug": "cached", "title": "Cached game"}],
    }
    try:
        with TestClient(app) as client:
            login(client)
            assert client.get("/api/discover").json()[0]["slug"] == "cached"
            refreshed = client.get("/api/discover?refresh=true")
            assert refreshed.status_code == 200
            assert refreshed.json()[0]["slug"] == "fresh"
            assert len(calls) == 1
    finally:
        main_module._discover_cache.clear()


def test_add_event_rejects_a_missing_csrf_header():
    # Exactly the watch.js defect: a logged-in session whose POST omits the CSRF
    # header (as the un-wrapped watch page did) must be rejected with 403.
    with TestClient(app) as client:
        login(client)
        client.headers.pop("X-CSRF-Token", None)
        assert client.post("/api/events", json=_EVENT_BODY).status_code == 403


def test_add_event_succeeds_with_session_and_csrf_header():
    with TestClient(app) as client:
        login(client)  # sets the X-CSRF-Token header, as the shared wrapper does
        assert client.post("/api/events", json=_EVENT_BODY).status_code == 201


def test_watch_and_dashboard_share_the_csrf_fetch_module():
    with TestClient(app) as client:
        assert 'src="/static/csrf.js"' in client.get("/").text
        assert 'src="/static/csrf.js"' in client.get("/watch").text
        module = client.get("/static/csrf.js")
        assert module.status_code == 200
        assert "X-CSRF-Token" in module.text


def test_live_trading_snapshot_prefers_structured_mlb_state():
    """A sport-state-free packet arriving after the linescore poll must not
    blank the trader's inning context for the cycle."""
    from datetime import datetime, timezone

    from app.models import GameState

    event = store.add_event(Event(
        name="Away MLB at Home MLB",
        sport="baseball",
        league="MLB",
        home="Home MLB",
        away="Away MLB",
    ))
    try:
        observed = datetime.now(timezone.utc)

        def state(source, sport_state):
            return GameState(
                event_id=event.id,
                home_score=1,
                away_score=1,
                period="Top 5",
                clock="",
                source=source,
                provider_timestamp=observed,
                received_at=observed,
                processed_at=observed,
                status="in_progress",
                sport_state=sport_state,
            )

        store.add_state(state(
            "mlb-linescore",
            {"schema": "mlb-linescore-v2", "inning": 5, "half": "top"},
        ))
        store.add_state(state("polymarket", None))

        _, latest_states = main_module._live_trading_snapshot()
        chosen = latest_states[event.id]
        assert isinstance(chosen.sport_state, dict)
        assert chosen.sport_state.get("inning") == 5

        # Without any structured state the newest packet still wins.
        with store.lock:
            store.states[event.id].clear()
        store.add_state(state("polymarket", None))
        _, latest_states = main_module._live_trading_snapshot()
        assert latest_states[event.id].sport_state is None
    finally:
        store.remove_event(event.id)


def test_workstation_mode_never_follows_database_url(monkeypatch):
    """Local-first policy: website runs persist to DATABASE_URL, workstation
    runs must not — even when the variable is present in the environment."""
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://user:secret@db.example.supabase.co/postgres"
    )
    with TestClient(app):
        assert main_module.ledger.backend == "sqlite"
        assert main_module.history_db.backend == "sqlite"
        assert main_module.monitor_state.backend == "sqlite"
        # The guard removes the variable so no later store construction can
        # follow it either.
        assert os.environ.get("DATABASE_URL") is None
