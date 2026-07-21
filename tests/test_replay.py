from datetime import datetime, timedelta, timezone

from app.history import HistoryDB
from app.models import Event, GameState, Quote
from app.replay import run_replay


def _moneyline_quote(event: Event, source: str, outcome: str, probability: float,
                     observed_at: datetime, *, bid: float | None = None,
                     ask: float | None = None) -> Quote:
    is_polymarket = source.casefold() == "polymarket"
    return Quote(
        event_id=event.id,
        market="moneyline",
        outcome=outcome,
        source=source,
        probability=probability,
        bid=bid,
        ask=ask,
        observed_at=observed_at,
        ask_size=1000.0 if is_polymarket else None,
        depth_complete=is_polymarket,
        fee_rate=0.0 if is_polymarket else None,
        ask_levels=((ask, 1000.0),) if is_polymarket and ask is not None else (),
    )


def test_replay_smoke_uses_historical_ticks_and_sqlite(tmp_path, monkeypatch, capsys):
    """A stale-on-the-wall-clock fixture still produces and settles a real edge."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-be-used.invalid/replay")
    history_path = tmp_path / "history.db"
    history = HistoryDB(str(history_path))
    event = Event(
        id="replay-event",
        name="Knicks vs Celtics",
        sport="basketball",
        home="Knicks",
        away="Celtics",
        league="NBA",
    )
    start = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)

    quotes = []
    for offset, source in enumerate(("Pinnacle", "Betfair", "Circa")):
        at = start + timedelta(seconds=offset)
        quotes.extend(
            (
                _moneyline_quote(event, source, event.home, 0.62, at),
                _moneyline_quote(event, source, event.away, 0.38, at),
            )
        )
    execution_at = start + timedelta(seconds=3)
    quotes.extend(
        (
            _moneyline_quote(
                event, "Polymarket", event.home, 0.445, execution_at, bid=0.44, ask=0.45
            ),
            _moneyline_quote(
                event, "Polymarket", event.away, 0.555, execution_at, bid=0.55, ask=0.56
            ),
        )
    )
    final_state = GameState(
        event_id=event.id,
        home_score=112,
        away_score=104,
        period="4",
        clock="00:00",
        source="fixture",
        status="final",
        observed_at=start + timedelta(seconds=4),
    )

    try:
        history.log_quotes(quotes)
        history.log_state(final_state)
        history.log_outcome(event, pregame_spread=-2.5, pregame_total=220.5,
                            final_state=final_state)
    finally:
        history.close()

    board = run_replay(history_path, calibrated_markets={"moneyline"})

    assert board
    assert any(bot["n_bets"] > 0 for bot in board)
    assert any(bot["wins"] > 0 for bot in board)
    assert all(bot["n_open"] == 0 for bot in board)
    output = capsys.readouterr().out
    assert "Replaying: Knicks vs Celtics" in output
    assert "BACKTEST RESULTS" in output


def test_replay_never_enters_on_quotes_observed_after_terminal_state(tmp_path, capsys):
    history_path = tmp_path / "post_final.db"
    history = HistoryDB(str(history_path))
    event = Event(
        id="post-final-event",
        name="Knicks vs Celtics",
        sport="basketball",
        home="Knicks",
        away="Celtics",
    )
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    references = []
    for source in ("Pinnacle", "Circa"):
        references.extend((
            _moneyline_quote(event, source, event.home, .62, start),
            _moneyline_quote(event, source, event.away, .38, start),
        ))
    terminal = GameState(
        event.id, 112, 104, "4", "00:00", "fixture", status="final",
        observed_at=start + timedelta(seconds=1),
    )
    post_final_execution = (
        _moneyline_quote(event, "Polymarket", event.home, .445,
                         start + timedelta(seconds=1), bid=.44, ask=.45),
        _moneyline_quote(event, "Polymarket", event.away, .555,
                         start + timedelta(seconds=1), bid=.55, ask=.56),
    )
    try:
        history.log_quotes([*references, *post_final_execution])
        history.log_state(terminal)
        history.log_outcome(event, -2.5, 220.5, terminal)
    finally:
        history.close()

    board = run_replay(history_path)

    assert board
    assert all(bot["n_bets"] == 0 for bot in board)
    capsys.readouterr()
