from app.models import Event
from app.monitor_state import MonitorState


def test_monitor_state_restores_events_and_automation_setting(tmp_path):
    path = str(tmp_path / "state.db")
    event = Event("Away at Home", "basketball", "Home", "Away",
                  polymarket_slug="game", game_start="2026-07-19T23:20:00Z")
    state = MonitorState(path)
    state.set_auto_monitor(True)
    state.set_odds_api_enabled(False)
    state.set_odds_api_poll_seconds(12.5)
    state.save_event(event)
    state.close()

    restored = MonitorState(path)
    try:
        assert restored.auto_monitor() is True
        assert restored.odds_api_enabled() is False
        assert restored.odds_api_poll_seconds() == 12.5
        assert restored.events() == [event]
        restored.delete_event(event.id)
        assert restored.events() == []
    finally:
        restored.close()


def test_monitor_state_bounds_invalid_or_extreme_poll_intervals(tmp_path):
    state = MonitorState(str(tmp_path / "bounded-state.db"))
    try:
        state.set_odds_api_poll_seconds(1)
        assert state.odds_api_poll_seconds() == 5
        state.set_odds_api_poll_seconds(9999)
        assert state.odds_api_poll_seconds() == 3600
    finally:
        state.close()
