import argparse
import asyncio
import os
import sqlite3
from datetime import datetime, timezone
from pprint import pprint

from .accounts import AccountBook, DEFAULT_STRATEGIES
from .engine import SignalEngine
from .models import Event, GameState, Quote

def run_replay(history_db_path: str = "history.db"):
    if not os.path.exists(history_db_path):
        print(f"Error: {history_db_path} not found. Run the live app to collect data first.")
        return

    # In-memory account book to test strategies
    accounts = AccountBook(path=":memory:")
    accounts.seed(DEFAULT_STRATEGIES)
    
    engine = SignalEngine(confidence_threshold=72.0, edge_threshold=0.035)

    conn = sqlite3.connect(history_db_path)
    conn.row_factory = sqlite3.Row

    # Fetch all completed events
    events = conn.execute("SELECT * FROM event_outcomes").fetchall()
    
    if not events:
        print("No completed events found in history.db")
        return

    print(f"Replaying {len(events)} events...\n")

    for e_row in events:
        event = Event(
            id=e_row["event_id"],
            name=e_row["name"],
            sport=e_row["sport"],
            home=e_row["home"],
            away=e_row["away"],
            league=e_row["league"],
            polymarket_slug=e_row["polymarket_slug"]
        )
        pregame_spread = e_row["pregame_spread"]
        pregame_total = e_row["pregame_total"]
        final_home_score = e_row["final_home_score"]
        final_away_score = e_row["final_away_score"]

        # Fetch quotes and states
        quotes_raw = conn.execute(
            "SELECT * FROM quotes_history WHERE event_id=? ORDER BY observed_at ASC", 
            (event.id,)
        ).fetchall()
        
        states_raw = conn.execute(
            "SELECT * FROM states_history WHERE event_id=? ORDER BY observed_at ASC", 
            (event.id,)
        ).fetchall()

        # Interleave timeline by observed_at timestamp
        timeline = []
        for q in quotes_raw:
            timeline.append((q["observed_at"], "quote", q))
        for s in states_raw:
            timeline.append((s["observed_at"], "state", s))
            
        timeline.sort(key=lambda x: x[0])

        current_quotes = {}
        current_states = []

        print(f"Replaying: {event.name} ({len(timeline)} ticks)")

        for timestamp, kind, data in timeline:
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            if kind == "quote":
                q_obj = Quote(
                    event_id=event.id,
                    market=data["market"],
                    outcome=data["outcome"],
                    source=data["source"],
                    probability=data["probability"],
                    ask=data["ask"],
                    bid=data["bid"],
                    liquidity=data["liquidity"],
                    observed_at=dt
                )
                key = (q_obj.market, q_obj.outcome, q_obj.source)
                current_quotes[key] = q_obj
            elif kind == "state":
                s_obj = GameState(
                    event_id=event.id,
                    home_score=data["home_score"],
                    away_score=data["away_score"],
                    period=data["period"],
                    clock=data["clock"],
                    status=data["status"],
                    observed_at=dt
                )
                current_states.append(s_obj)

            # Evaluate at this tick
            signals = engine.evaluate(
                event_id=event.id,
                quotes=list(current_quotes.values()),
                states=current_states,
                away_team=event.away,
                sport=event.sport,
                home_outcome=event.home,
                pregame_spread=pregame_spread,
                pregame_total=pregame_total
            )

            if signals:
                accounts.place(event, signals)

        # Settle event
        if final_home_score is not None and final_away_score is not None:
            accounts.settle(event, final_home_score, final_away_score)

    print("\n" + "="*50)
    print("BACKTEST RESULTS (LEADERBOARD)")
    print("="*50)
    board = accounts.leaderboard()
    for rank, bot in enumerate(board, 1):
        roi = bot['roi'] * 100
        wr = (bot['win_rate'] * 100) if bot['win_rate'] is not None else 0
        print(f"{rank}. {bot['name']:<25} | Equity: ${bot['equity']:<8.2f} | ROI: {roi:>6.2f}% | WR: {wr:>5.1f}% | Bets: {bot['n_bets']}")
        
    accounts.close()
    conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay historical telemetry and test strategies.")
    parser.add_argument("--db", type=str, default="history.db", help="Path to history.db")
    args = parser.parse_args()
    
    run_replay(args.db)
