"""The canonical request is stored once per decision, compressed.

It used to be written inline on every PAPER_BET row of `decision_marks` -- the
table that has already filled a managed database once. These pin the properties
that make the replacement safe to rely on: the artifact round-trips exactly,
rows written before the split still read, and the new table is bounded by the
same retention window as the old one.
"""
import zlib
from datetime import datetime, timedelta, timezone

from app.ledger import (
    SNAPSHOT_ENCODING_ZLIB,
    Ledger,
    decode_snapshot,
    encode_snapshot,
)
from app.models import Event, Signal

PASSING_EXECUTION_GATES = [
    {"code": "provider_freshness", "passed": True, "status": "pass"},
    {"code": "market_identity", "passed": True, "status": "pass"},
    {"code": "market_status", "passed": True, "status": "pass"},
    {"code": "executable_fill", "passed": True, "status": "pass"},
]

NOW = datetime(2026, 3, 1, 18, 0, tzinfo=timezone.utc)
# Repetitive like a real canonical request, so the ratio is representative.
SNAPSHOT = ('{"quotes":[' + ','.join(
    f'{{"market":"moneyline","outcome":"home","probability":0.5,"source":"book{i}"}}'
    for i in range(400)) + ']}')


def _signal(outcome, *, action="PAPER_BET", decision_hash="hash-1",
            observed_at=NOW, snapshot=SNAPSHOT):
    signal = Signal(
        "e", "moneyline", outcome, model_probability=0.6,
        market_probability=0.5, edge=0.1, confidence=90.0, action=action,
        reasons=[], quote_source="Polymarket", market_fair_prob=0.6,
        n_reference_sources=3, observed_at=observed_at,
        decision_hash=decision_hash, gate_results=PASSING_EXECUTION_GATES,
        requested_cash=10.0, filled_cash=10.0, filled_shares=20.0,
        execution_fee=0.0, execution_complete=True,
    )
    signal.input_snapshot_json = snapshot
    return signal


def _event():
    return Event("A at B", "basketball", "B", "A", id="e")


def test_snapshot_round_trips_exactly():
    encoding, payload = encode_snapshot(SNAPSHOT)
    assert encoding == SNAPSHOT_ENCODING_ZLIB
    assert decode_snapshot(encoding, payload) == SNAPSHOT


def test_snapshot_is_substantially_smaller_than_the_raw_request():
    _, payload = encode_snapshot(SNAPSHOT)
    # Canonical JSON repeats its key names on every quote payload, so this is a
    # large and reliable ratio rather than a marginal one.
    assert len(payload) * 5 < len(SNAPSHOT)


def test_legacy_plain_text_snapshots_still_decode():
    """Rows written before the split carry raw JSON and no encoding."""
    assert decode_snapshot(None, SNAPSHOT) == SNAPSHOT
    assert decode_snapshot("text", SNAPSHOT) == SNAPSHOT


def test_the_request_is_stored_once_per_decision_not_once_per_bet(tmp_path):
    ledger = Ledger(str(tmp_path / "inputs.db"))
    try:
        # Three placed selections from a single evaluation share one hash.
        signals = [_signal(name) for name in ("home", "away", "draw")]
        ledger.record_signals(_event(), signals)
        with ledger._db.cursor(dict_rows=True) as cur:
            ledger._db.execute(cur, "SELECT COUNT(*) AS n FROM decision_inputs")
            assert dict(cur.fetchone())["n"] == 1
        assert ledger.decision_input("hash-1") == SNAPSHOT
    finally:
        ledger.close()


def test_decision_marks_no_longer_carry_the_inline_blob(tmp_path):
    ledger = Ledger(str(tmp_path / "no-inline.db"))
    try:
        ledger.record_signals(_event(), [_signal("home")])
        assert all(
            row["input_snapshot_json"] is None for row in ledger.all_decisions()
        )
    finally:
        ledger.close()


def test_watch_decisions_store_no_snapshot(tmp_path):
    """Only placed entries keep their request; that limit is what bounds size."""
    ledger = Ledger(str(tmp_path / "watch.db"))
    try:
        ledger.record_signals(
            _event(), [_signal("home", action="WATCH", decision_hash="watch-1")])
        assert ledger.decision_input("watch-1") is None
    finally:
        ledger.close()


def test_a_legacy_inline_snapshot_is_still_readable(tmp_path):
    """An upgrade must not orphan the evidence already on disk."""
    ledger = Ledger(str(tmp_path / "legacy.db"))
    try:
        with ledger._db.transaction() as cur:
            ledger._db.execute(
                cur,
                "INSERT INTO decision_marks "
                "(decision_hash, event_id, market, outcome, as_of, "
                " policy_action, reasons, input_snapshot_json) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                ("old-hash", "e", "moneyline", "home", NOW.timestamp(),
                 "PAPER_BET", "", SNAPSHOT),
            )
        assert ledger.decision_input("old-hash") == SNAPSHOT
    finally:
        ledger.close()


def test_an_unknown_decision_has_no_snapshot(tmp_path):
    ledger = Ledger(str(tmp_path / "missing.db"))
    try:
        assert ledger.decision_input("never-recorded") is None
    finally:
        ledger.close()


def test_snapshots_age_out_with_the_retention_window(tmp_path, monkeypatch):
    """The table added to bound disk must itself be bounded."""
    monkeypatch.setenv("DECISION_RETENTION_DAYS", "1")
    ledger = Ledger(str(tmp_path / "retention.db"))
    try:
        stale = NOW - timedelta(days=400)
        ledger.record_signals(
            _event(), [_signal("home", decision_hash="stale", observed_at=stale)])
        assert ledger.decision_input("stale") == SNAPSHOT
        # A later write triggers the throttled prune.
        ledger._last_prune = 0.0
        with ledger._db.transaction() as cur:
            ledger._prune_decision_marks(cur, datetime.now(timezone.utc).timestamp())
        assert ledger.decision_input("stale") is None
    finally:
        ledger.close()


def test_the_stored_snapshot_still_matches_its_decision_hash(tmp_path):
    """Lineage is unchanged: the hash covers the uncompressed request."""
    ledger = Ledger(str(tmp_path / "lineage.db"))
    try:
        ledger.record_signals(_event(), [_signal("home")])
        restored = ledger.decision_input("hash-1")
        # Byte-identical, so any hash taken over the request still verifies.
        assert restored == SNAPSHOT
        assert zlib.crc32(restored.encode()) == zlib.crc32(SNAPSHOT.encode())
    finally:
        ledger.close()
