"""The optional second live lane (Alex) beside the primary (Anthony).

The feature must be strictly additive: with ENABLE_POLYMARKET_US_ALEX_LANE
unset the server behaves exactly as a single-live-lane deployment, and the
lane machinery only routes to Alex when its trader actually exists.
"""
import pytest
from fastapi import HTTPException

import app.main as main
from app.settings import Settings


def test_alex_lane_settings_default_off():
    settings = Settings.from_env({})
    assert settings.enable_polymarket_us_alex_lane is False
    assert settings.polymarket_us_alex_key_id == ""
    assert settings.polymarket_us_alex_secret_key == ""
    assert str(settings.polymarket_us_alex_trading_db) == (
        "polymarket-us-alex.db"
    )


def test_alex_lane_settings_parse_from_env():
    settings = Settings.from_env({
        "ENABLE_POLYMARKET_US_ALEX_LANE": "true",
        "POLYMARKET_US_ALEX_KEY_ID": " alex-key ",
        "POLYMARKET_US_ALEX_SECRET_KEY": " alex-secret ",
        "POLYMARKET_US_ALEX_TRADING_DB": "custom-alex.db",
    })
    assert settings.enable_polymarket_us_alex_lane is True
    assert settings.polymarket_us_alex_key_id == "alex-key"
    assert settings.polymarket_us_alex_secret_key == "alex-secret"
    assert str(settings.polymarket_us_alex_trading_db) == "custom-alex.db"


def test_lane_labels_name_the_people():
    assert main.LANE_LABELS["live"] == "Anthony"
    assert main.LANE_LABELS["alex"] == "Alex"
    assert main.LANE_LABELS["dry_run"] == "Dry run"


def test_alex_lane_resolution_requires_the_flag(monkeypatch):
    monkeypatch.setattr(main, "alex_trader", None)
    with pytest.raises(HTTPException) as excinfo:
        main._require_execution_lane("alex")
    assert excinfo.value.status_code == 409
    assert "ENABLE_POLYMARKET_US_ALEX_LANE" in str(excinfo.value.detail)


def test_alex_lane_resolution_returns_the_lane_when_present(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(main, "alex_trader", sentinel)
    assert main._require_execution_lane("alex") is sentinel


def test_credential_lane_rejects_dry_run(monkeypatch):
    monkeypatch.setattr(main, "alex_trader", object())
    with pytest.raises(HTTPException) as excinfo:
        main._require_credential_lane("dry_run")
    assert excinfo.value.status_code == 409


def test_lane_environment_credentials_split_by_person(monkeypatch):
    monkeypatch.setattr(main, "settings", Settings.from_env({
        "POLYMARKET_US_KEY_ID": "anthony-key",
        "POLYMARKET_US_SECRET_KEY": "anthony-secret",
        "POLYMARKET_US_ALEX_KEY_ID": "alex-key",
        "POLYMARKET_US_ALEX_SECRET_KEY": "alex-secret",
    }))
    assert main._lane_environment_credentials("alex") == (
        "alex-key", "alex-secret"
    )
    assert main._lane_environment_credentials("live") == (
        "anthony-key", "anthony-secret"
    )
    assert main._lane_environment_credentials(None) == (
        "anthony-key", "anthony-secret"
    )


def test_execution_data_sources_include_alex_when_present(monkeypatch):
    live = object()
    dry = object()
    alex = object()
    monkeypatch.setattr(main, "live_trader", live)
    monkeypatch.setattr(main, "dry_run_trader", dry)
    monkeypatch.setattr(main, "alex_trader", alex)
    sources = dict(main._execution_data_sources("all"))
    assert sources == {"live": live, "dry_run": dry, "alex": alex}
    only_alex = main._execution_data_sources("alex")
    assert only_alex == [("alex", alex)]

    # Without the lane the shape is exactly the historical one.
    monkeypatch.setattr(main, "alex_trader", None)
    sources = dict(main._execution_data_sources("all"))
    assert sources == {"live": live, "dry_run": dry}
