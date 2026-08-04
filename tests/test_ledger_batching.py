"""Decision marks and close marks are written as batched statements.

`record_signals` used to issue one INSERT per signal; a prop-heavy tick journals
~114 of them, which is one network round trip each against the managed
PostgreSQL used in production. The batched form has to be observably identical
on the audit path, so these pin the properties that batching could plausibly
break: nothing dropped, ordering and conflict resolution unchanged, the
snapshot still restricted to placed entries, and re-recording still idempotent.
"""
from datetime import datetime, timedelta, timezone

from app.ledger import Ledger
from app.models import Event, Signal

PASSING_EXECUTION_GATES = [
    {"code": "provider_freshness", "passed": True, "status": "pass"},
    {"code": "market_identity", "passed": True, "status": "pass"},
    {"code": "market_status", "passed": True, "status": "pass"},
    {"code": "executable_fill", "passed": True, "status": "pass"},
]

NOW = datetime(2026, 3, 1, 18, 0, tzinfo=timezone.utc)


def _signal(market, outcome, *, action="WATCH", observed_at=NOW, price=0.5):
    return Signal(
        "e", market, outcome, model_probability=0.55, market_probability=price,
        edge=0.05, confidence=85.0, action=action, reasons=["r"],
        quote_source="Polymarket", market_fair_prob=0.55,
        n_reference_sources=3, observed_at=observed_at,
        decision_hash="hash-1", gate_results=PASSING_EXECUTION_GATES,
    )


def _event():
    return Event("A at B", "basketball", "B", "A", id="e")


def test_every_signal_in_a_wide_decision_is_journalled(tmp_path):
    """The batch must not drop rows as the decision gets wide."""
    ledger = Ledger(str(tmp_path / "wide.db"))
    try:
        signals = [
            _signal(f"market_{index // 2}", "home" if index % 2 else "away")
            for index in range(114)
        ]
        ledger.record_signals(_event(), signals)
        decisions = ledger.all_decisions()
        assert len(decisions) == 114
        assert {row["market"] for row in decisions} == {
            f"market_{index}" for index in range(57)
        }
    finally:
        ledger.close()


def test_close_marks_are_written_for_every_signal_in_the_batch(tmp_path):
    ledger = Ledger(str(tmp_path / "closes.db"))
    try:
        signals = [_signal(f"m{index}", "home") for index in range(20)]
        ledger.record_signals(_event(), signals)
        with ledger._db.cursor(dict_rows=True) as cur:
            ledger._db.execute(cur, "SELECT market FROM close_marks")
            markets = {dict(row)["market"] for row in cur.fetchall()}
        assert markets == {f"m{index}" for index in range(20)}
    finally:
        ledger.close()


def test_a_later_close_mark_supersedes_an_earlier_one_within_a_batch(tmp_path):
    """The ON CONFLICT newest-observation guard must survive batching."""
    ledger = Ledger(str(tmp_path / "supersede.db"))
    try:
        event = _event()
        ledger.record_signals(event, [_signal("moneyline", "home", price=0.40)])
        later = _signal("moneyline", "home", price=0.70,
                        observed_at=NOW + timedelta(seconds=30))
        ledger.record_signals(event, [later])
        with ledger._db.cursor(dict_rows=True) as cur:
            ledger._db.execute(
                cur, "SELECT executable_probability FROM close_marks")
            rows = [dict(row) for row in cur.fetchall()]
        assert len(rows) == 1
        assert rows[0]["executable_probability"] == 0.70

        # An older observation must not overwrite the newer one.
        stale = _signal("moneyline", "home", price=0.10,
                        observed_at=NOW - timedelta(seconds=30))
        ledger.record_signals(event, [stale])
        with ledger._db.cursor(dict_rows=True) as cur:
            ledger._db.execute(
                cur, "SELECT executable_probability FROM close_marks")
            rows = [dict(row) for row in cur.fetchall()]
        assert rows[0]["executable_probability"] == 0.70
    finally:
        ledger.close()


def test_recording_the_same_decision_twice_is_idempotent(tmp_path):
    ledger = Ledger(str(tmp_path / "idempotent.db"))
    try:
        event = _event()
        signals = [_signal(f"m{index}", "home") for index in range(10)]
        ledger.record_signals(event, signals)
        ledger.record_signals(event, signals)
        assert len(ledger.all_decisions()) == 10
    finally:
        ledger.close()


def test_the_request_snapshot_is_stored_only_for_placed_entries(tmp_path):
    """Batching must not widen which decisions keep the heavy request.

    The snapshot now lives in `decision_inputs` rather than inline on every
    row (see test_decision_inputs.py); what has not changed is that only a
    placed entry keeps one.
    """
    ledger = Ledger(str(tmp_path / "snapshot.db"))
    try:
        watch = _signal("moneyline", "away")
        watch.input_snapshot_json = "x" * 5_000
        placed = _signal("moneyline", "home", action="PAPER_BET")
        placed.input_snapshot_json = "y" * 5_000
        placed.decision_hash = "placed-hash"
        ledger.record_signals(_event(), [watch, placed])
        # No row carries the blob inline any more.
        assert all(
            row["input_snapshot_json"] is None for row in ledger.all_decisions()
        )
        assert ledger.decision_input("placed-hash") == "y" * 5_000
        assert ledger.decision_input("hash-1") is None  # the WATCH decision
    finally:
        ledger.close()


def test_sampled_off_decisions_still_get_their_close_mark(tmp_path, monkeypatch):
    """Throttled diagnostic rows leave the audit log but not close-line tracking."""
    monkeypatch.setenv("DECISION_MARK_MIN_SECONDS", "3600")
    monkeypatch.setenv("DECISION_MARK_HEARTBEAT_SECONDS", "7200")
    ledger = Ledger(str(tmp_path / "sampled.db"))
    try:
        event = _event()
        ledger.record_signals(event, [_signal("moneyline", "home", price=0.40)])
        # Same fingerprint a second later: throttled out of decision_marks, but
        # the close mark still has to advance.
        repeat = _signal("moneyline", "home", price=0.60,
                         observed_at=NOW + timedelta(seconds=1))
        ledger.record_signals(event, [repeat])
        assert len(ledger.all_decisions()) == 1  # second one sampled off
        with ledger._db.cursor(dict_rows=True) as cur:
            ledger._db.execute(
                cur, "SELECT executable_probability FROM close_marks")
            rows = [dict(row) for row in cur.fetchall()]
        assert rows[0]["executable_probability"] == 0.60
    finally:
        ledger.close()
