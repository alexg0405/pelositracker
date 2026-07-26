import asyncio

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
        assert "data-cashout-toggle" in javascript
        assert 'id="discover-refresh-status"' in html
        assert "/api/discover?refresh=true" in javascript
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
        assert "odds_api_enabled" in javascript
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
        assert "/api/polymarket-us/trading/config" in javascript
        assert "/api/polymarket-us/trading/arm" in javascript
        assert "/api/polymarket-us/trading/emergency-stop" in javascript
        assert "Raw long bid" in javascript
        assert "ARM LIVE TRADING" in html
        assert "one-cent gain alone never triggers an exit" in html.casefold()
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
        assert "/api/polymarket-us/trading/liquidate" in javascript
        assert "/api/polymarket-us/trading/history/dry-run" in javascript
        assert "Total net" in javascript
        assert "W–L–P" in javascript
        assert 'id="us-liquidate-form"' in html
        assert "SELL ALL LIVE POSITIONS" in html
        assert 'class="us-activity-grid section-gap"' in html
        assert "Stop automation + wipe dry-run trades" in html
        assert "Dry run is an executive reset" in html
        assert 'id="us-clear-dry-history"' not in html
        assert "No market mapping, quote, or fill is required" in javascript


def test_workstation_exposes_only_bounded_polymarket_us_trading_routes():
    paths = {route.path for route in app.routes}
    us_paths = {path for path in paths if path.startswith("/api/polymarket-us")}

    assert us_paths == {
        "/api/polymarket-us/status",
        "/api/polymarket-us/events",
        "/api/polymarket-us/account",
        "/api/polymarket-us/trading/status",
        "/api/polymarket-us/trading/config",
        "/api/polymarket-us/trading/arm",
        "/api/polymarket-us/trading/disarm",
        "/api/polymarket-us/trading/emergency-stop",
        "/api/polymarket-us/trading/run",
        "/api/polymarket-us/trading/journal",
        "/api/polymarket-us/trading/positions",
        "/api/polymarket-us/trading/performance",
        "/api/polymarket-us/trading/liquidate",
        "/api/polymarket-us/trading/history/dry-run",
    }
    # There is deliberately no arbitrary create/modify/cancel/close route. The
    # only execution surface is the policy-controlled automation manager.
    assert not any(
        fragment in path
        for path in us_paths
        for fragment in ("/orders", "/create", "/modify", "/cancel", "/close")
    )


def test_live_trading_api_defaults_disarmed_and_rejects_unsafe_policy():
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
            })
            assert configured.status_code == 200
            status = configured.json()
            assert status["armed"] is False
            assert status["policy"]["execution_mode"] == "dry_run"
            assert status["policy"]["automation_enabled"] is False
            assert status["restart_behavior"] == "always_disarmed"

            performance = client.get("/api/polymarket-us/trading/performance")
            assert performance.status_code == 200
            summary = performance.json()
            assert set(summary["modes"]) == {"dry_run", "live"}
            assert summary["combined"]["mode"] == "combined"

            unsafe = client.put("/api/polymarket-us/trading/config", json={
                "min_entry_price": 0.05,
            })
            assert unsafe.status_code == 400
            assert "5c" in unsafe.json()["detail"]

            arm = client.post("/api/polymarket-us/trading/arm", json={
                "confirmation": "ARM LIVE TRADING",
                "seconds": 1800,
            })
            assert arm.status_code == 400
            assert "enable automation" in arm.json()["detail"]
        finally:
            main_module.auth_manager.revoke(token)


def test_empty_mode_liquidation_returns_without_fetching_market_inventory(monkeypatch):
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

    monkeypatch.setattr(main_module, "polymarket_sports_events", fake_discovery)
    main_module._discover_cache.update(
        at=main_module.time.monotonic(),
        data=[{"slug": "cached", "title": "Cached game"}],
    )
    try:
        with TestClient(app) as client:
            login(client)
            assert client.get("/api/discover").json()[0]["slug"] == "cached"
            refreshed = client.get("/api/discover?refresh=true")
            assert refreshed.status_code == 200
            assert refreshed.json()[0]["slug"] == "fresh"
            assert len(calls) == 1
    finally:
        main_module._discover_cache.update(at=0.0, data=[])


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
