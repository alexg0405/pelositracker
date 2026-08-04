"""Per-person lane ownership: each live lane obeys only its owner's login.

Ownership must be strictly additive. With POLYMARKET_US_LANE_OWNERS unset
every route keeps its historical any-authenticated-user (or operator-only,
for credentials) behavior; with it set, an owned lane accepts risk-adding
actions only from its owner while protective stop/disarm controls stay open
to everyone.
"""
import os

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app
from app.settings import Settings


TWO_PERSON_ENV = {
    "AUTHORIZED_USERS": "anthony:secret-a,alex:secret-b",
    "ADMIN_USERNAME": "anthony",
    "POLYMARKET_US_LANE_OWNERS": "live:anthony,alex:alex",
}


def _two_person(monkeypatch, extra: dict | None = None):
    from app.security import AuthManager

    # Merge over the ambient test environment so the lifespan keeps using the
    # throwaway conftest databases rather than repo-root files.
    env = {**os.environ, **TWO_PERSON_ENV, **(extra or {})}
    monkeypatch.setattr(main, "settings", Settings.from_env(env))
    users = {"anthony": "secret-a", "alex": "secret-b"}
    monkeypatch.setattr(main, "AUTHORIZED_USERS", users)
    monkeypatch.setattr(main, "auth_manager", AuthManager.from_plaintext(users))


def _sign_in(client, username, password):
    # Authenticate directly against the auth manager so these route tests do
    # not consume shared login-throttle slots.
    authenticated = main.auth_manager.login(username, password)
    assert authenticated is not None
    token, session = authenticated
    client.cookies.set(main._cookie_name("session_token"), token)
    client.cookies.set(main._cookie_name("csrf_token"), session.csrf_token)
    client.headers.update({"X-CSRF-Token": session.csrf_token})


# --- settings parsing -------------------------------------------------------

def test_lane_owners_default_empty():
    assert Settings.from_env({}).polymarket_us_lane_owners == {}


def test_lane_owners_parse_and_cross_check_logins():
    settings = Settings.from_env(TWO_PERSON_ENV)
    assert settings.polymarket_us_lane_owners == {
        "live": "anthony",
        "alex": "alex",
    }


def test_lane_owners_reject_unknown_lane_and_malformed_pairs():
    with pytest.raises(ValueError, match="lane must be one of"):
        Settings.from_env({"POLYMARKET_US_LANE_OWNERS": "dry_run:anthony"})
    with pytest.raises(ValueError, match="lane:username"):
        Settings.from_env({"POLYMARKET_US_LANE_OWNERS": "anthony"})
    with pytest.raises(ValueError, match="twice"):
        Settings.from_env(
            {"POLYMARKET_US_LANE_OWNERS": "live:anthony,live:alex"}
        )


def test_lane_owner_must_match_a_configured_login():
    with pytest.raises(ValueError, match="no such login"):
        Settings.from_env({
            "AUTHORIZED_USERS": "anthony:secret-a",
            "POLYMARKET_US_LANE_OWNERS": "alex:alex",
        })
    # The admin fallback login satisfies the cross-check.
    settings = Settings.from_env({
        "ADMIN_USERNAME": "anthony",
        "POLYMARKET_US_LANE_OWNERS": "live:anthony",
    })
    assert settings.polymarket_us_lane_owners == {"live": "anthony"}


# --- ownership resolution ---------------------------------------------------

def test_lane_owner_resolution(monkeypatch):
    monkeypatch.setattr(main, "settings", Settings.from_env(TWO_PERSON_ENV))
    assert main._lane_owner("live") == "anthony"
    assert main._lane_owner(None) == "anthony"  # omitted lane = primary live
    assert main._lane_owner("alex") == "alex"
    assert main._lane_owner("dry_run") is None  # shared measurement bed


def test_unowned_lanes_keep_historical_behavior(monkeypatch):
    monkeypatch.setattr(main, "settings", Settings.from_env({}))
    assert main._lane_owner("live") is None
    assert main._lane_owner("alex") is None


# --- route enforcement ------------------------------------------------------

def test_owned_lane_rejects_risk_actions_from_the_other_person(monkeypatch):
    _two_person(monkeypatch)
    with TestClient(app) as client:
        _sign_in(client, "alex", "secret-b")
        for method, path in (
            ("put", "/api/polymarket-us/trading/config?lane=live"),
            ("post", "/api/polymarket-us/trading/arm?lane=live"),
            ("post", "/api/polymarket-us/trading/run?lane=live"),
            ("post", "/api/polymarket-us/trading/risk-session/reset?lane=live"),
            ("post", "/api/polymarket-us/trading/performance/reset-live?lane=live"),
            ("post", "/api/polymarket-us/trading/sync?lane=live"),
        ):
            response = getattr(client, method)(path, json={})
            assert response.status_code == 403, path
            assert "anthony" in response.json()["detail"], path


def test_protective_actions_stay_open_to_everyone(monkeypatch):
    _two_person(monkeypatch)
    with TestClient(app) as client:
        monkeypatch.setattr(main, "alex_trader", None)
        _sign_in(client, "anthony", "secret-a")
        # Anthony is not Alex, yet disarm/stop/emergency-stop pass the
        # authorization layer and fail only on lane availability (409), never
        # on ownership (403).
        for path in (
            "/api/polymarket-us/trading/disarm?lane=alex",
            "/api/polymarket-us/trading/stop?lane=alex",
            "/api/polymarket-us/trading/emergency-stop?lane=alex",
        ):
            response = client.post(path)
            assert response.status_code == 409, path
            assert "ENABLE_POLYMARKET_US_ALEX_LANE" in response.json()["detail"]


def test_owner_manages_their_own_lane_credentials_without_admin(monkeypatch):
    _two_person(monkeypatch)
    with TestClient(app) as client:
        monkeypatch.setattr(main, "alex_trader", None)
        _sign_in(client, "alex", "secret-b")
        # Authorization passes for the owner (Alex is not the site admin);
        # the request then fails only because the lane is not running here.
        response = client.post(
            "/api/polymarket-us/runtime-credentials?lane=alex",
            json={"key_id": "k", "secret_key": "s"},
        )
        assert response.status_code == 409
        assert "ENABLE_POLYMARKET_US_ALEX_LANE" in response.json()["detail"]
        # The other person's lane refuses the same request outright.
        denied = client.post(
            "/api/polymarket-us/runtime-credentials?lane=live",
            json={"key_id": "k", "secret_key": "s"},
        )
        assert denied.status_code == 403


def test_unowned_lane_credentials_stay_operator_only(monkeypatch):
    _two_person(monkeypatch, extra={"POLYMARKET_US_LANE_OWNERS": ""})
    with TestClient(app) as client:
        _sign_in(client, "alex", "secret-b")
        response = client.post(
            "/api/polymarket-us/runtime-credentials",
            json={"key_id": "k", "secret_key": "s"},
        )
        assert response.status_code == 403
        assert "Operator" in response.json()["detail"]


# --- lane-aware position exit ----------------------------------------------

class _StubPolicy:
    execution_mode = "live"


class _StubTrader:
    def __init__(self, position=None):
        self._position = position
        self.exited = []
        self.policy = _StubPolicy()

    def position(self, position_id):
        return self._position

    def is_armed(self):
        return True

    def exit_position(self, us_payload, position_id, confirmation):
        self.exited.append(position_id)
        return {"status": "exit_attempted", "position_id": position_id}


def test_exit_route_finds_alex_positions_and_enforces_their_owner(monkeypatch):
    _two_person(monkeypatch)
    alex_position = {"id": "p-1", "mode": "live", "status": "closed"}
    alex_stub = _StubTrader(alex_position)
    with TestClient(app) as client:
        monkeypatch.setattr(main, "live_trader", _StubTrader(None))
        monkeypatch.setattr(main, "dry_run_trader", _StubTrader(None))
        monkeypatch.setattr(main, "alex_trader", alex_stub)
        _sign_in(client, "anthony", "secret-a")
        # Found via the no-lane search that previously never reached Alex,
        # then refused because the position belongs to Alex's book.
        denied = client.post(
            "/api/polymarket-us/trading/positions/p-1/exit",
            json={"confirmation": "approve"},
        )
        assert denied.status_code == 403
        assert alex_stub.exited == []
        _sign_in(client, "alex", "secret-b")
        allowed = client.post(
            "/api/polymarket-us/trading/positions/p-1/exit",
            json={"confirmation": "approve"},
        )
        assert allowed.status_code == 200
        assert alex_stub.exited == ["p-1"]


def test_status_reports_identity_and_owned_lanes(monkeypatch):
    _two_person(monkeypatch)
    with TestClient(app) as client:
        _sign_in(client, "alex", "secret-b")
        payload = client.get("/api/polymarket-us/status").json()
        assert payload["user"]["username"] == "alex"
        assert payload["user"]["execution_admin"] is False
        assert payload["user"]["owned_lanes"] == ["alex"]
        assert payload["user"]["lane_owners"] == {
            "live": "anthony",
            "alex": "alex",
        }
