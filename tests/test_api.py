from fastapi.testclient import TestClient

from app.main import app, store
from app.models import Quote, Signal


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


def test_dashboard_contains_merged_ui_behaviors():
    with TestClient(app) as client:
        html = client.get("/").text
        javascript = client.get("/static/index.js").text
        assert "data-remove-event" in javascript
        assert "details[open][data-detail-key]" in javascript
        assert "Paste Polymarket link" in html
        assert "data-save-position" in javascript
        assert "Signal quality" in javascript
        assert "Edge buffer" in javascript or "edge_buffer" in javascript
        assert "Allow logical automatic cash-out" in html
        assert "data-cashout-toggle" in javascript


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
        assert client.get("/api/accounts/Engine%20Kelly/marks").json() == []

        restored = client.patch(
            "/api/accounts/Engine%20Kelly", json={"cash_out_enabled": False}
        )
        assert restored.status_code == 200


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
