from types import SimpleNamespace

import pytest

from app.polymarket_us_research import (
    PolymarketUSResearchError,
    _account_snapshot_sync,
    credential_status,
    normalize_sports_events,
    public_market_quotes,
)
from app.models import Event
from app.settings import Settings


def test_credential_status_never_returns_a_secret():
    status = credential_status("12345678-abcdef", "do-not-return-this")

    assert status == {
        "configured": True,
        "key_id_hint": "123456...cdef",
        "account_access": "read-only",
        "trading_enabled": False,
    }
    assert "secret" not in repr(status).casefold()
    assert "do-not-return-this" not in repr(status)


def test_normalize_sports_events_keeps_active_us_lines_only():
    payload = {
        "events": [
            {
                "id": "game-1",
                "slug": "away-at-home",
                "title": "Away at Home",
                "category": "sports",
                "startTime": "2026-07-25T23:00:00Z",
                "live": True,
                "score": "2-1",
                "markets": [
                    {
                        "id": "market-1",
                        "slug": "away-at-home-spread",
                        "question": "Will Away +1.5 cover?",
                        "active": True,
                        "closed": False,
                        "sportsMarketType": "SPORTS_MARKET_TYPE_SPREAD",
                        "line": 1.5,
                        "bestBidQuote": {"value": 0.47, "currency": "USD"},
                        "bestAskQuote": {"value": 0.49, "currency": "USD"},
                        "marketSides": [
                            {
                                "id": "yes",
                                "description": "Away +1.5",
                                "long": True,
                                "quote": {"value": 0.48, "currency": "USD"},
                                "tradable": True,
                            },
                            {
                                "id": "no",
                                "description": "Home -1.5",
                                "long": False,
                                "quote": {"value": 0.52, "currency": "USD"},
                                "tradable": True,
                            },
                        ],
                    },
                    {
                        "id": "closed",
                        "active": True,
                        "closed": True,
                        "sportsMarketType": "SPORTS_MARKET_TYPE_MONEYLINE",
                    },
                ],
            },
            {
                "id": "not-sports",
                "title": "Non-sports",
                "category": "politics",
                "markets": [{
                    "id": "other",
                    "active": True,
                    "sportsMarketType": "SPORTS_MARKET_TYPE_FUTURE",
                }],
            },
        ]
    }

    events = normalize_sports_events(payload)

    assert len(events) == 1
    assert events[0]["live"] is True
    assert events[0]["markets"][0]["market_type"] == "spread"
    assert events[0]["markets"][0]["market_type_v2"] == "spread"
    assert events[0]["markets"][0]["long_best_bid"] == 0.47
    assert events[0]["markets"][0]["long_best_ask"] == 0.49
    assert [side["description"] for side in events[0]["markets"][0]["sides"]] == [
        "Away +1.5",
        "Home -1.5",
    ]
    assert events[0]["markets"][0]["sides"][0]["team_name"] == ""


def test_normalize_preserves_binary_soccer_selection_identity():
    payload = {
        "events": [{
            "id": "game-1",
            "slug": "mls-sje-lag-2026-07-25",
            "title": "San Jose Earthquakes vs. Los Angeles Galaxy",
            "category": "sports",
            "markets": [{
                "id": "market-1",
                "slug": "atc-mls-sje-lag-2026-07-25-sje",
                "active": True,
                "closed": False,
                "ep3Status": "OPEN",
                "sportsMarketType": "soccer_team_full_time_winner",
                "sportsMarketTypeV2": "SPORTS_MARKET_TYPE_DRAWABLE_OUTCOME",
                "marketSides": [
                    {
                        "id": "yes",
                        "description": "Yes",
                        "long": True,
                        "tradable": True,
                        "team": {"name": "San Jose Earthquakes"},
                    },
                    {
                        "id": "no",
                        "description": "No",
                        "long": False,
                        "tradable": True,
                        "team": {"name": "San Jose Earthquakes"},
                    },
                ],
            }],
        }],
    }

    market = normalize_sports_events(payload)[0]["markets"][0]

    assert market["market_type"] == "soccer_team_full_time_winner"
    assert market["market_type_v2"] == "drawable_outcome"
    assert market["sides"][0]["description"] == "Yes"
    assert market["sides"][0]["team_name"] == "San Jose Earthquakes"


def test_normalize_preserves_exact_mlb_first_five_and_first_inning_identity():
    payload = {
        "events": [{
            "id": "mlb-game",
            "slug": "away-at-home",
            "title": "Away at Home",
            "category": "sports",
            "markets": [
                {
                    "id": "f5",
                    "slug": "away-wins-f5",
                    "question": "Away wins F5",
                    "active": True,
                    "closed": False,
                    "sportsMarketType": "baseball_team_first_five_winner",
                    "marketSides": [],
                },
                {
                    "id": "f1",
                    "slug": "run-first-inning",
                    "question": "Any run in first inning?",
                    "active": True,
                    "closed": False,
                    "sportsMarketType": "baseball_team_first_inning_run",
                    "marketSides": [],
                },
            ],
        }],
    }

    markets = normalize_sports_events(payload)[0]["markets"]

    assert markets[0]["canonical_market"] == "first_five_moneyline"
    assert markets[0]["market_scope"] == "first_five_innings"
    assert markets[1]["canonical_market"] == "first_inning_total"
    assert markets[1]["market_scope"] == "first_inning"
    assert markets[1]["line"] == pytest.approx(0.5)


def test_public_us_segment_books_become_scoped_engine_target_quotes():
    target = Event(
        id="tracked",
        name="Away at Home",
        sport="baseball",
        league="MLB",
        home="Home",
        away="Away",
    )
    us_event = {
        "id": "us-game",
        "markets": [
            {
                "id": "f5-total",
                "slug": "f5-total-4-5",
                "question": "More than 4.5 runs in F5?",
                "canonical_market": "first_five_total",
                "market_scope": "first_five_innings",
                "line": 4.5,
                "active": True,
                "closed": False,
                "long_best_bid": 0.42,
                "long_best_ask": 0.44,
                "minimum_trade_quantity": 1,
                "minimum_tick_size": 0.01,
                "sides": [
                    {"id": "yes", "description": "Yes", "long": True, "tradable": True},
                    {"id": "no", "description": "No", "long": False, "tradable": True},
                ],
            },
        ],
    }

    quotes = public_market_quotes(target, us_event)

    assert [(quote.market, quote.outcome) for quote in quotes] == [
        ("first_five_total", "Over 4.5"),
        ("first_five_total", "Under 4.5"),
    ]
    assert quotes[0].source == "Polymarket"
    assert quotes[0].bid == pytest.approx(0.42)
    assert quotes[0].ask == pytest.approx(0.44)
    assert quotes[1].bid == pytest.approx(0.56)
    assert quotes[1].ask == pytest.approx(0.58)
    assert all(quote.depth_complete is False for quote in quotes)
    assert all(quote.market_scope == "first_five_innings" for quote in quotes)


def test_account_snapshot_uses_read_only_resources(monkeypatch):
    calls: list[str] = []

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["key_id"] == "key-id"
            assert kwargs["secret_key"] == "secret"
            self.account = SimpleNamespace(
                balances=lambda: calls.append("balances") or {
                    "balances": [{"currentBalance": 25.0}]
                }
            )
            self.portfolio = SimpleNamespace(
                positions=lambda params: calls.append(f"positions:{params['limit']}") or {
                    "positions": {"market": {"quantity": 2}},
                    "eof": True,
                }
            )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @property
        def orders(self):
            raise AssertionError("read-only workstation must not access order resources")

    monkeypatch.setitem(
        __import__("sys").modules,
        "polymarket_us",
        SimpleNamespace(PolymarketUS=FakeClient),
    )

    snapshot = _account_snapshot_sync("key-id", "secret")

    assert calls == ["balances", "positions:100"]
    assert snapshot["mode"] == "read-only"
    assert snapshot["positions"] == {"market": {"quantity": 2}}


def test_account_errors_do_not_echo_credentials(monkeypatch):
    class FailingClient:
        def __init__(self, **_kwargs):
            raise RuntimeError("failed key-id with secret")

    monkeypatch.setitem(
        __import__("sys").modules,
        "polymarket_us",
        SimpleNamespace(PolymarketUS=FailingClient),
    )

    with pytest.raises(PolymarketUSResearchError) as raised:
        _account_snapshot_sync("key-id", "secret")

    assert str(raised.value) == "failed [redacted] with [redacted]"


def test_workstation_settings_disable_bots_and_require_both_key_parts():
    settings = Settings.from_env({
        "WORKSTATION_MODE": "true",
        "ENABLE_PAPER_BOTS": "false",
        "ENABLE_POLYMARKET_US_TRADING": "true",
    })
    assert settings.workstation_mode is True
    assert settings.enable_paper_bots is False
    assert settings.enable_polymarket_us_trading is True

    with pytest.raises(ValueError, match="must be set together"):
        Settings.from_env({"POLYMARKET_US_KEY_ID": "key-only"})
