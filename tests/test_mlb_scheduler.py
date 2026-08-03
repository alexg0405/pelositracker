"""The MLB daily schedule reaches Discovery even before the venue lists it."""
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import _merge_mlb_schedule, _mlb_game_slug, app
from app.mlb_live import parse_mlb_schedule
from app.security import SlidingWindowLimiter

_SCHEDULE_PAYLOAD = {
    "dates": [{
        "games": [
            {
                "gamePk": 1,
                "gameDate": "2026-08-02T02:10:00Z",
                "officialDate": "2026-08-01",
                "status": {"abstractGameState": "Preview"},
                "teams": {
                    "away": {"team": {"name": "Arizona Diamondbacks", "abbreviation": "AZ"}},
                    "home": {"team": {"name": "Seattle Mariners", "abbreviation": "SEA"}},
                },
            },
            {
                "gamePk": 2,
                "gameDate": "2026-08-01T19:07:00Z",
                "officialDate": "2026-08-01",
                "status": {"abstractGameState": "Live"},
                "teams": {
                    "away": {"team": {"name": "St. Louis Cardinals", "abbreviation": "STL"}},
                    "home": {"team": {"name": "Toronto Blue Jays", "abbreviation": "TOR"}},
                },
            },
            {
                "gamePk": 3,
                "gameDate": "2026-08-01T17:05:00Z",
                "officialDate": "2026-08-01",
                "status": {"abstractGameState": "Final"},
                "teams": {
                    "away": {"team": {"name": "Miami Marlins", "abbreviation": "MIA"}},
                    "home": {"team": {"name": "New York Mets", "abbreviation": "NYM"}},
                },
            },
        ],
    }],
}


def test_parse_schedule_keeps_official_date_and_status():
    games = parse_mlb_schedule(_SCHEDULE_PAYLOAD)
    assert len(games) == 3
    evening = games[0]
    # The UTC gameDate crosses midnight; the official (slug) date must not.
    assert evening["official_date"] == "2026-08-01"
    assert evening["start"] == "2026-08-02T02:10:00Z"
    assert evening["away_abbr"] == "AZ"
    assert [game["status"] for game in games] == ["preview", "live", "final"]


def test_game_slug_uses_venue_abbreviations_and_official_date():
    games = parse_mlb_schedule(_SCHEDULE_PAYLOAD)
    # MLB's modern "AZ" must map to the venue's traditional "ari".
    assert _mlb_game_slug(games[0]) == "mlb-az-sea-2026-08-01".replace(
        "az", "ari"
    )
    assert _mlb_game_slug(games[1]) == "mlb-stl-tor-2026-08-01"
    assert _mlb_game_slug({"away_abbr": "", "home_abbr": "SEA"}) is None


def test_merge_appends_unlisted_games_and_enriches_listed_ones():
    schedule = parse_mlb_schedule(_SCHEDULE_PAYLOAD)
    listed = [{
        "slug": "mlb-stl-tor-2026-08-01",
        "title": "Cardinals vs. Blue Jays",
        "league": "MLB",
        "status": "upcoming",
    }]
    merged = _merge_mlb_schedule(listed, schedule)
    by_slug = {game["slug"]: game for game in merged}

    # The listed game is enriched with live status, never duplicated.
    assert len(merged) == 2
    cardinals = by_slug["mlb-stl-tor-2026-08-01"]
    assert cardinals["status"] == "live"
    assert cardinals["status_source"] == "mlb-schedule"
    assert cardinals["title"] == "Cardinals vs. Blue Jays"

    # The unlisted evening game is appended with a constructed slug.
    evening = by_slug["mlb-ari-sea-2026-08-01"]
    assert evening["listed"] is False
    assert evening["status"] == "upcoming"
    assert evening["title"] == "Arizona Diamondbacks @ Seattle Mariners"

    # Finals never enter the picker, and live games rank first.
    assert "mlb-mia-nym-2026-08-01" not in by_slug
    assert merged[0]["slug"] == "mlb-stl-tor-2026-08-01"


def test_discover_league_filter_merges_the_mlb_schedule(monkeypatch):
    async def fake_discovery(**kwargs):
        fake_discovery.leagues = kwargs.get("leagues")
        return [{
            "slug": "mlb-stl-tor-2026-08-01",
            "title": "Cardinals vs. Blue Jays",
            "league": "MLB",
            "status": "upcoming",
        }]

    async def fake_schedule(day=None):
        return parse_mlb_schedule(_SCHEDULE_PAYLOAD)

    monkeypatch.setattr(main_module, "polymarket_sports_events", fake_discovery)
    monkeypatch.setattr(main_module, "fetch_mlb_schedule", fake_schedule)
    # A fresh limiter keeps this login from starving the shared window.
    monkeypatch.setattr(
        main_module, "login_limiter", SlidingWindowLimiter(10, 5 * 60)
    )
    main_module._discover_cache.clear()
    main_module._mlb_schedule_cache.update(at=0.0, data=[])
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/login", data={"username": "admin", "password": "admin"}
            )
            assert response.status_code == 200
            client.headers.update(
                {"X-CSRF-Token": response.json()["csrf_token"]}
            )
            games = client.get("/api/discover?league=mlb").json()
            assert fake_discovery.leagues == ["mlb"]
            slugs = [game["slug"] for game in games]
            assert "mlb-ari-sea-2026-08-01" in slugs
            assert "mlb-stl-tor-2026-08-01" in slugs
            assert games[0]["status"] == "live"

            # The league-scoped cache is independent of the default view.
            assert "" not in main_module._discover_cache
            assert "mlb" in main_module._discover_cache
    finally:
        main_module._discover_cache.clear()
        main_module._mlb_schedule_cache.update(at=0.0, data=[])


def test_unlisted_schedule_slug_matches_the_venue_game_format():
    games = parse_mlb_schedule(_SCHEDULE_PAYLOAD)
    slug = _mlb_game_slug(games[1])
    assert slug is not None
    import re

    assert re.fullmatch(r"mlb-[a-z]{2,4}-[a-z]{2,4}-\d{4}-\d{2}-\d{2}", slug)
