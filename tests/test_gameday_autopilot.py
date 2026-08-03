"""The game-day autopilot gates polling and dry automation on live games."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import _gameday_transition, app, store
from app.models import Event, GameState
from app.security import SlidingWindowLimiter


def _report(**overrides):
    values = {
        "event_id": "e1",
        "name": "Away vs. Home",
        "missing": False,
        "live": False,
        "final": False,
        "abandoned": False,
        "started": False,
        "start_ts": None,
    }
    values.update(overrides)
    return values


def test_transition_waits_until_a_planned_game_is_playable():
    assert _gameday_transition("waiting", [_report()]) == "waiting"
    assert _gameday_transition("waiting", [_report(live=True)]) == "active"
    assert _gameday_transition("waiting", [_report(started=True)]) == "active"
    # A started-but-final game alone cannot activate the plan.
    assert _gameday_transition(
        "waiting", [_report(started=True, final=True)]
    ) == "completed"


def test_transition_completes_only_when_every_game_concludes():
    mixed = [_report(final=True), _report(live=True)]
    assert _gameday_transition("active", mixed) == "active"
    done = [_report(final=True), _report(abandoned=True)]
    assert _gameday_transition("active", done) == "completed"
    assert _gameday_transition("waiting", []) == "completed"


@pytest.fixture
def gameday_lane(monkeypatch, tmp_path):
    trading_db = tmp_path / "polymarket-us-trading.db"
    dry_run_db = tmp_path / "polymarket-us-dry-run.db"
    monkeypatch.setenv("POLYMARKET_US_TRADING_DB", str(trading_db))
    monkeypatch.setenv("POLYMARKET_US_DRY_RUN_DB", str(dry_run_db))
    # The login limiter's sliding window is shared process-wide by client key;
    # a fresh instance keeps these logins from starving other test modules.
    monkeypatch.setattr(
        main_module, "login_limiter", SlidingWindowLimiter(10, 5 * 60)
    )
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


def _login(client):
    response = client.post(
        "/api/login", data={"username": "admin", "password": "admin"}
    )
    assert response.status_code == 200
    client.headers.update({"X-CSRF-Token": response.json()["csrf_token"]})


def _live_state(event_id):
    observed = datetime.now(timezone.utc)
    return GameState(
        event_id=event_id,
        home_score=1,
        away_score=0,
        period="Top 3",
        clock="",
        source="test-mlb",
        provider_timestamp=observed,
        received_at=observed,
        processed_at=observed,
        status="in_progress",
        live=True,
    )


def test_autopilot_round_trip_gates_polling_and_dry_automation(gameday_lane):
    with TestClient(app) as client:
        _login(client)
        event = store.add_event(Event(
            name="Away MLB at Home MLB",
            sport="baseball",
            league="MLB",
            home="Home MLB",
            away="Away MLB",
        ))
        previous_odds = main_module._config_state["odds_api_enabled"]
        try:
            armed = client.post(
                "/api/gameday", json={"event_ids": [event.id]}
            )
            assert armed.status_code == 200
            body = armed.json()
            assert body["armed"] is True
            assert body["phase"] == "waiting"
            assert body["odds_api_enabled"] is False
            assert body["dry_automation_enabled"] is False

            # First pitch: a live state flips the plan to active and turns
            # polling and dry-run automation on.
            store.add_state(_live_state(event.id))
            active = client.get("/api/gameday").json()
            assert active["phase"] == "active"
            assert active["odds_api_enabled"] is True
            assert active["dry_automation_enabled"] is True

            # Last out: a terminal event completes the plan and stops both.
            main_module._terminal_events[event.id] = "final"
            done = client.get("/api/gameday").json()
            assert done["phase"] == "completed"
            assert done["armed"] is False
            assert done["odds_api_enabled"] is False
            assert done["dry_automation_enabled"] is False
        finally:
            main_module._terminal_events.pop(event.id, None)
            store.remove_event(event.id)
            main_module._gameday_state.update(armed=False, phase="idle", event_ids=[])
            main_module._config_state["odds_api_enabled"] = previous_odds


def test_autopilot_rejects_unmonitored_games(gameday_lane):
    with TestClient(app) as client:
        _login(client)
        assert client.post(
            "/api/gameday", json={"event_ids": ["missing"]}
        ).status_code == 400
        assert client.post(
            "/api/gameday", json={"slugs": ["not-monitored-slug"]}
        ).status_code == 400
        assert client.post("/api/gameday", json={}).status_code == 400


def test_autopilot_resolves_slugs_against_monitored_events(gameday_lane):
    with TestClient(app) as client:
        _login(client)
        event = store.add_event(Event(
            name="Away MLB at Home MLB",
            sport="baseball",
            league="MLB",
            home="Home MLB",
            away="Away MLB",
            polymarket_slug="mlb-away-home-2026-08-01",
        ))
        previous_odds = main_module._config_state["odds_api_enabled"]
        try:
            armed = client.post(
                "/api/gameday",
                json={"slugs": ["mlb-away-home-2026-08-01"]},
            )
            assert armed.status_code == 200
            assert armed.json()["events"][0]["event_id"] == event.id

            disarmed = client.delete("/api/gameday")
            assert disarmed.status_code == 200
            assert disarmed.json()["armed"] is False
        finally:
            store.remove_event(event.id)
            main_module._gameday_state.update(armed=False, phase="idle", event_ids=[])
            main_module._config_state["odds_api_enabled"] = previous_odds
