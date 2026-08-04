"""Backfill missing event_outcomes rows from the official MLB result.

Events removed from monitoring before their final out never reach
finalization, so no `event_outcomes` row is written and every closed
position on them is invisible to settlement grading — 8 dry-lane and a
further set of live-lane events (~25% of the closed record) as of
2026-08-01. This tool recovers those rows from the MLB Stats API schedule
(the same public endpoint the live feed already uses), with safety checks:

- the matched game must be Final;
- the official final score can never be below the last score this
  workstation observed live (runs are monotone);
- the match must sit inside the observed date window.

Preview by default; nothing is written without ``--apply``. Writes are
insert-only for event ids that have no outcome row; existing rows are never
touched. Run with the server stopped.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.mlb_live import MLB_STATS_API, _schedule_games, match_mlb_game
from app.models import Event

DEFAULT_LANES = (
    Path("polymarket-us-trading.db"),
    Path("workstation-data/polymarket-us-dry-run.db"),
)


@dataclass(slots=True)
class MissingEvent:
    event_id: str
    event_name: str
    away: str
    home: str
    last_state_ts: float
    last_home_score: float
    last_away_score: float


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.expanduser().resolve().as_posix()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def parse_matchup(event_name: str) -> tuple[str, str] | None:
    """Event names are consistently "Away vs. Home" in this workstation."""
    parts = [part.strip() for part in event_name.split(" vs. ")]
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


def find_missing(
    lane_paths: list[Path],
    history_path: Path,
) -> list[MissingEvent]:
    history = _read_only(history_path)
    try:
        known = {
            row["event_id"]
            for row in history.execute("SELECT event_id FROM event_outcomes")
        }
        referenced: dict[str, str] = {}
        for lane_path in lane_paths:
            if not lane_path.exists():
                continue
            lane = _read_only(lane_path)
            try:
                for row in lane.execute(
                    """SELECT DISTINCT event_id, event_name
                       FROM live_managed_positions WHERE status='closed'"""
                ):
                    if row["event_id"] not in known:
                        referenced[row["event_id"]] = row["event_name"]
            finally:
                lane.close()
        missing = []
        for event_id, event_name in sorted(referenced.items()):
            matchup = parse_matchup(event_name)
            if matchup is None:
                continue
            state = history.execute(
                """SELECT home_score, away_score, observed_at
                   FROM states_history WHERE event_id=?
                   ORDER BY observed_at DESC LIMIT 1""",
                (event_id,),
            ).fetchone()
            if state is None:
                continue
            missing.append(
                MissingEvent(
                    event_id=event_id,
                    event_name=event_name,
                    away=matchup[0],
                    home=matchup[1],
                    last_state_ts=float(state["observed_at"]),
                    last_home_score=float(state["home_score"]),
                    last_away_score=float(state["away_score"]),
                )
            )
        return missing
    finally:
        history.close()


def _game_final_scores(game: dict) -> tuple[float, float] | None:
    teams = game.get("teams") or {}
    try:
        home = float((teams.get("home") or {}).get("score"))
        away = float((teams.get("away") or {}).get("score"))
    except (TypeError, ValueError):
        return None
    return home, away


def resolve_final(
    item: MissingEvent,
    client: httpx.Client,
) -> dict[str, object]:
    """Return a decision record for one missing event."""
    observed = datetime.fromtimestamp(item.last_state_ts, timezone.utc)
    event = Event(
        name=item.event_name,
        sport="baseball",
        league="MLB",
        home=item.home,
        away=item.away,
        game_start=observed.isoformat(),
    )
    # Night games end past the UTC date boundary while the MLB schedule keys
    # on the US-local game date, so query a one-day window on each side.
    response = client.get(
        f"{MLB_STATS_API}/schedule",
        params={
            "sportId": 1,
            "startDate": (observed.date() - timedelta(days=1)).isoformat(),
            "endDate": observed.date().isoformat(),
        },
    )
    response.raise_for_status()
    game = match_mlb_game(event, _schedule_games(response.json()))
    return judge_match(item, game)


def judge_match(item: MissingEvent, game: dict | None) -> dict[str, object]:
    base: dict[str, object] = {
        "event_id": item.event_id,
        "event_name": item.event_name,
        "last_observed": (
            f"{item.last_away_score:.0f}-{item.last_home_score:.0f} away-home"
        ),
    }
    if game is None:
        return {**base, "action": "skip", "reason": "no schedule match"}
    status = (game.get("status") or {}).get("abstractGameState")
    if str(status) != "Final":
        return {
            **base,
            "action": "skip",
            "reason": f"matched game is not Final (status={status})",
        }
    scores = _game_final_scores(game)
    if scores is None:
        return {**base, "action": "skip", "reason": "no final score payload"}
    final_home, final_away = scores
    if (
        final_home < item.last_home_score - 1e-9
        or final_away < item.last_away_score - 1e-9
    ):
        return {
            **base,
            "action": "skip",
            "reason": (
                "official final "
                f"{final_away:.0f}-{final_home:.0f} is below the last "
                "observed score; identity is suspect"
            ),
        }
    teams = game.get("teams") or {}
    return {
        **base,
        "action": "write",
        "final_home_score": final_home,
        "final_away_score": final_away,
        "home": str(((teams.get("home") or {}).get("team") or {}).get("name") or item.home),
        "away": str(((teams.get("away") or {}).get("team") or {}).get("name") or item.away),
        "final_status": "final",
    }


def apply_writes(
    history_path: Path,
    decisions: list[dict[str, object]],
) -> int:
    connection = sqlite3.connect(history_path)
    written = 0
    try:
        with connection:
            for decision in decisions:
                if decision["action"] != "write":
                    continue
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO event_outcomes
                       (event_id,name,sport,home,away,league,polymarket_slug,
                        pregame_spread,pregame_total,final_home_score,
                        final_away_score,final_status,settled_ts)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        decision["event_id"],
                        decision["event_name"],
                        "baseball",
                        decision["home"],
                        decision["away"],
                        "MLB",
                        None,
                        None,
                        None,
                        decision["final_home_score"],
                        decision["final_away_score"],
                        decision["final_status"],
                        time.time(),
                    ),
                )
                written += cursor.rowcount
    finally:
        connection.close()
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history-db",
        type=Path,
        default=Path("workstation-data/history.db"),
    )
    parser.add_argument(
        "--lane-db",
        action="append",
        type=Path,
        default=None,
        help="Lane database(s) whose closed positions define the missing "
        "set; defaults to both standard lanes",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the recovered outcomes; default is preview only",
    )
    args = parser.parse_args()
    lanes = args.lane_db or list(DEFAULT_LANES)
    missing = find_missing(lanes, args.history_db)
    with httpx.Client(timeout=20.0) as client:
        decisions = [resolve_final(item, client) for item in missing]
    written = 0
    if args.apply:
        written = apply_writes(args.history_db, decisions)
    print(
        json.dumps(
            {
                "missing_events": len(missing),
                "writable": sum(1 for d in decisions if d["action"] == "write"),
                "written": written,
                "applied": bool(args.apply),
                "decisions": decisions,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
