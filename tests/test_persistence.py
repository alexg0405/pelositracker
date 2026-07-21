import sqlite3

import pytest

import app.database as database_module
from app.accounts import AccountBook, Strategy
from app.database import Database
from app.history import HistoryDB
from app.ledger import Ledger
from app.models import Event, GameState, Quote, Signal


def _paper_signal(event_id: str) -> Signal:
    return Signal(
        event_id,
        "moneyline",
        "home",
        model_probability=0.60,
        market_probability=0.50,
        edge=0.10,
        confidence=90.0,
        action="PAPER_BET",
        reasons=[],
        quote_source="Polymarket",
        market_fair_prob=0.60,
        n_reference_sources=2,
        token_id="token-home",
    )


def _paper_quote(event: Event) -> Quote:
    return Quote(
        event.id, "moneyline", "home", 0.50, "Polymarket",
        token_id="token-home", bid=0.48, ask=0.50,
        bid_levels=((0.48, 1_000.0),), ask_levels=((0.50, 1_000.0),),
        depth_complete=True, fee_rate=0.0,
    )


def test_store_environment_fallbacks_use_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("ACCOUNTS_DB", str(tmp_path / "accounts.db"))
    monkeypatch.setenv("HISTORY_DB", str(tmp_path / "history.db"))

    stores = [Ledger(), AccountBook(), HistoryDB()]
    try:
        assert [store.backend for store in stores] == ["sqlite", "sqlite", "sqlite"]
        assert [store.path for store in stores] == [
            str(tmp_path / "ledger.db"),
            str(tmp_path / "accounts.db"),
            str(tmp_path / "history.db"),
        ]
    finally:
        for store in stores:
            store.close()


def test_database_url_keeps_postgres_as_the_default(monkeypatch):
    class FakeConnection:
        autocommit = True

        def close(self):
            pass

    connection = FakeConnection()
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/app")
    monkeypatch.setattr(database_module.psycopg2, "connect", lambda target: connection)

    database = Database.open(None, sqlite_envs=("LEDGER_DB",), sqlite_default="ledger.db")
    try:
        assert database.backend == "postgres"
        assert database.target == "postgresql://example.invalid/app"
        assert connection.autocommit is False
    finally:
        database.close()


def test_transaction_rolls_back_all_sqlite_writes_on_error(tmp_path):
    database = Database.open(str(tmp_path / "rollback.db"), sqlite_envs=(), sqlite_default="")
    try:
        database.initialize("CREATE TABLE values_test (id INTEGER PRIMARY KEY, value TEXT NOT NULL);")
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction() as cur:
                database.execute(cur, "INSERT INTO values_test VALUES (%s,%s)", (1, "first"))
                database.execute(cur, "INSERT INTO values_test VALUES (%s,%s)", (1, "duplicate"))

        with database.cursor() as cur:
            database.execute(cur, "SELECT COUNT(*) FROM values_test")
            assert cur.fetchone()[0] == 0
    finally:
        database.close()


def test_ledger_sqlite_position_upsert_and_delete(tmp_path):
    ledger = Ledger(str(tmp_path / "positions.db"))
    try:
        created = ledger.upsert_position("event", "token", "moneyline", "home", 10, 0.45)
        assert created["shares"] == pytest.approx(10)

        updated = ledger.upsert_position("event", "token", "moneyline", "home", 20, 0.50)
        assert updated["shares"] == pytest.approx(20)
        assert len(ledger.event_positions("event")) == 1
        assert ledger.delete_position("event", "token") is True
        assert ledger.delete_position("event", "token") is False
    finally:
        ledger.close()


def test_account_book_places_dedupes_settles_and_lists_every_account(tmp_path):
    book = AccountBook(str(tmp_path / "accounts.db"))
    event = Event(name="Celtics at Knicks", sport="basketball", home="Knicks", away="Celtics")
    active = Strategy(
        "Active",
        "Places the qualifying test signal.",
        sizing="flat",
        flat_stake=100.0,
        start_bankroll=1_000.0,
    )
    idle = Strategy(
        "Idle",
        "Must still appear with no bets.",
        edge_threshold=0.99,
        sizing="flat",
        flat_stake=100.0,
        start_bankroll=1_000.0,
    )
    try:
        book.seed([active, idle])
        placed = book.place(event, [_paper_signal(event.id)], [_paper_quote(event)])
        assert [bet["bot_name"] for bet in placed] == ["Active"]
        assert book.place(event, [_paper_signal(event.id)], [_paper_quote(event)]) == []

        before = {row["name"]: row for row in book.leaderboard()}
        assert set(before) == {"Active", "Idle"}
        assert before["Active"]["n_open"] == 1
        assert before["Idle"]["n_bets"] == 0

        assert book.settle(event, home_score=110, away_score=100) == 1
        assert book.settle(event, home_score=110, away_score=100) == 0
        bet = book.account_bets("Active")[0]
        assert bet["status"] == "win"
        assert bet["pnl"] == pytest.approx(100.0)

        after = {row["name"]: row for row in book.leaderboard()}
        assert after["Active"]["bankroll"] == pytest.approx(1_100.0)
        assert after["Active"]["wins"] == 1
        assert after["Idle"]["bankroll"] == pytest.approx(1_000.0)
    finally:
        book.close()


def test_history_sqlite_roundtrip_is_lossless(tmp_path):
    history = HistoryDB(str(tmp_path / "history.db"))
    event = Event(name="Celtics at Knicks", sport="basketball", home="Knicks", away="Celtics")
    quote = Quote(event.id, "moneyline", "Knicks", 0.55, "Pinnacle",
                  provider_event_id="provider-game", provider_market_id="main",
                  condition_id="condition", outcome_id="home")
    state = GameState(event.id, 60, 55, "3", "08:00", "test", status="in_progress")
    try:
        history.log_quotes([quote])
        history.log_quotes([quote])
        history.log_state(state)
        history.log_outcome(event, -2.5, 220.5, state)
        history.log_outcome(event, -3.0, 221.5, state)

        rows = history.get_event_history(event.id)
        assert len(rows["quotes"]) == 2
        assert rows["quotes"][0]["probability"] == pytest.approx(0.55)
        assert len(rows["states"]) == 1
        assert rows["states"][0]["home_score"] == pytest.approx(60)
    finally:
        history.close()
