"""Replay recorded telemetry through the current signal and account engines.

The signal engine evaluates freshness against the wall clock.  Historical
timestamps therefore need to be shifted as a group at each replay tick: the
current tick becomes "now", while the age of every quote/state in the snapshot
is preserved.  Without that shift, every real history database eventually
looks stale and the backtest can never place a bet.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .accounts import AccountBook, DEFAULT_STRATEGIES
from .engine import SignalEngine
from .models import Event, GameState, Quote


_FINAL_STATUSES = {"final", "ended", "closed", "complete", "completed", "finished"}
_CANCELED_STATUSES = {"canceled", "cancelled", "abandoned", "void", "voided"}


def _terminal_kind(status: object) -> str | None:
    normalized = str(status or "").strip().casefold().replace("_", " ").replace("-", " ")
    if normalized in _CANCELED_STATUSES:
        return "canceled"
    if normalized in _FINAL_STATUSES:
        return "final"
    return None


def _at_utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _rebase_snapshot(
    quotes: list[Quote], states: list[GameState], tick_at: datetime
) -> tuple[list[Quote], list[GameState]]:
    """Make a historical tick current without changing relative data ages."""
    shift = datetime.now(timezone.utc) - tick_at
    return (
        [replace(quote, observed_at=quote.observed_at + shift) for quote in quotes],
        [replace(state, observed_at=state.observed_at + shift) for state in states],
    )


def run_replay(history_db_path: str | os.PathLike[str] = "history.db") -> list[dict]:
    """Run an offline strategy replay and return the final leaderboard.

    ``history_db_path`` is always opened as SQLite.  The paper accounts use an
    explicit in-memory SQLite database as well, so an environment-level
    ``DATABASE_URL`` cannot accidentally send an offline replay to production.
    """
    history_path = Path(history_db_path)
    if not history_path.is_file():
        print(f"Error: {history_path} not found. Run the live app to collect data first.")
        return []

    accounts = AccountBook(path=":memory:")
    conn: sqlite3.Connection | None = None
    try:
        accounts.seed(DEFAULT_STRATEGIES)
        engine = SignalEngine(confidence_threshold=72.0, edge_threshold=0.035)

        conn = sqlite3.connect(os.fspath(history_path))
        conn.row_factory = sqlite3.Row
        events = conn.execute(
            "SELECT * FROM event_outcomes ORDER BY settled_ts ASC, event_id ASC"
        ).fetchall()

        if not events:
            print(f"No completed events found in {history_path}")
            return []

        print(f"Replaying {len(events)} events...\n")

        for event_row in events:
            event = Event(
                id=event_row["event_id"],
                name=event_row["name"] or event_row["event_id"],
                sport=event_row["sport"] or "",
                home=event_row["home"] or "home",
                away=event_row["away"] or "away",
                league=event_row["league"] or "",
                polymarket_slug=event_row["polymarket_slug"],
            )

            quotes_raw = conn.execute(
                """SELECT * FROM quotes_history
                   WHERE event_id=? ORDER BY observed_at ASC, id ASC""",
                (event.id,),
            ).fetchall()
            states_raw = conn.execute(
                """SELECT * FROM states_history
                   WHERE event_id=? ORDER BY observed_at ASC, id ASC""",
                (event.id,),
            ).fetchall()

            # The secondary order keeps quote ingestion deterministic when a
            # batch shares one timestamp, matching the history insert order.
            timeline = [
                (row["observed_at"], 0, row["id"], "quote", row) for row in quotes_raw
            ]
            timeline.extend(
                (row["observed_at"], 1, row["id"], "state", row) for row in states_raw
            )
            timeline.sort(key=lambda item: item[:3])
            terminal_markers = sorted(
                (row["observed_at"], row["id"], _terminal_kind(row["status"]))
                for row in states_raw
                if _terminal_kind(row["status"]) is not None
            )
            terminal_at = terminal_markers[0][0] if terminal_markers else None

            current_quotes: dict[tuple[str, str, str], Quote] = {}
            current_states: list[GameState] = []
            replay_terminal = terminal_markers[0][2] if terminal_markers else None
            print(f"Replaying: {event.name} ({len(timeline)} ticks)")

            for timestamp, _, _, kind, row in timeline:
                if terminal_at is not None and timestamp >= terminal_at:
                    # Timestamp ties are conservatively treated as closed. The
                    # replay cannot prove a quote sharing the terminal state's
                    # timestamp was observable before the result.
                    break
                tick_at = _at_utc(timestamp)
                if kind == "quote":
                    quote = Quote(
                        event_id=event.id,
                        market=row["market"],
                        outcome=row["outcome"],
                        source=row["source"] or "unknown",
                        probability=row["probability"],
                        ask=row["ask"],
                        bid=row["bid"],
                        liquidity=row["liquidity"],
                        observed_at=tick_at,
                    )
                    key = (quote.market, quote.outcome, quote.source)
                    current_quotes[key] = quote
                else:
                    state = GameState(
                        event_id=event.id,
                        home_score=row["home_score"],
                        away_score=row["away_score"],
                        period=row["period"] or "",
                        clock=row["clock"] or "",
                        source="history-replay",
                        status=row["status"] or "in_progress",
                        observed_at=tick_at,
                    )
                    current_states.append(state)

                quotes, states = _rebase_snapshot(
                    list(current_quotes.values()), current_states, tick_at
                )
                signals = engine.evaluate(
                    event_id=event.id,
                    quotes=quotes,
                    states=states,
                    away_outcome=event.away,
                    sport=event.sport,
                    home_outcome=event.home,
                    pregame_spread=event_row["pregame_spread"],
                    pregame_total=event_row["pregame_total"],
                )
                if signals:
                    accounts.place(event, signals)

            home_score = event_row["final_home_score"]
            away_score = event_row["final_away_score"]
            outcome_terminal = _terminal_kind(event_row["final_status"])
            if replay_terminal == "canceled" or outcome_terminal == "canceled":
                accounts.void_event(event.id)
            elif home_score is not None and away_score is not None:
                accounts.settle(event, home_score, away_score)

        board = accounts.leaderboard()
        print("\n" + "=" * 50)
        print("BACKTEST RESULTS (LEADERBOARD)")
        print("=" * 50)
        for rank, bot in enumerate(board, 1):
            roi = bot["roi"] * 100
            win_rate = (bot["win_rate"] * 100) if bot["win_rate"] is not None else 0
            print(
                f"{rank}. {bot['name']:<25} | Equity: ${bot['equity']:<8.2f} | "
                f"ROI: {roi:>6.2f}% | WR: {win_rate:>5.1f}% | Bets: {bot['n_bets']}"
            )
        return board
    finally:
        if conn is not None:
            conn.close()
        accounts.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay historical telemetry and test strategies.")
    parser.add_argument("--db", type=str, default="history.db", help="Path to history.db")
    args = parser.parse_args()
    run_replay(args.db)
