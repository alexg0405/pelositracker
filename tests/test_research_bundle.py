import gzip
import io
import json
import time
from uuid import uuid4

import pytest

from app.polymarket_us_trading import (
    PolymarketUSAutoTrader,
    TradingPolicyError,
)
from app.research_bundle import (
    ResearchBundleError,
    merge_research_bundle,
    write_research_bundle,
)
from app.sport_model_lab import SportModelLab


def _trader(path, *, key_id="environment-key", secret_key="environment-secret"):
    return PolymarketUSAutoTrader(
        str(path),
        key_id=key_id,
        secret_key=secret_key,
        clock=time.time,
    )


def _journal(trader, *, row_id, payload):
    with trader._db.transaction() as cur:
        trader._db.execute(
            cur,
            """INSERT INTO live_trading_journal
               (id,created_ts,kind,status,event_id,event_name,market_slug,
                selection,payload)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                row_id,
                time.time(),
                "research",
                "closed",
                "event-1",
                "Away at Home",
                "away-at-home",
                "Away",
                json.dumps(payload),
            ),
        )


def _candidate(lab, *, row_id):
    with lab._db.transaction() as cur:
        lab._db.execute(
            cur,
            """INSERT INTO sport_model_candidates
               (id,created_ts,sport,league,market,status,payload)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (
                row_id,
                time.time(),
                "baseball",
                "mlb",
                "moneyline",
                "candidate_does_not_beat_baseline",
                json.dumps({"research_only": True}),
            ),
        )


def test_research_bundle_is_secret_free_and_idempotent(tmp_path):
    source_trader = _trader(
        tmp_path / "source-trading.db",
        key_id="source-key-id",
        secret_key="never-export-this-secret",
    )
    source_lab = SportModelLab(str(tmp_path / "source-model.db"))
    target_trader = _trader(tmp_path / "target-trading.db")
    target_lab = SportModelLab(str(tmp_path / "target-model.db"))
    journal_id = str(uuid4())
    candidate_id = str(uuid4())
    archive = tmp_path / "evidence.ndjson.gz"
    try:
        _journal(source_trader, row_id=journal_id, payload={"edge": 0.08})
        _candidate(source_lab, row_id=candidate_id)

        written = write_research_bundle(
            archive,
            traders={"live": source_trader},
            model_lab=source_lab,
        )
        assert written["counts"]["live_trading_journal"] == 1
        assert written["counts"]["sport_model_candidates"] == 1
        compressed = archive.read_bytes()
        plain = gzip.decompress(compressed)
        assert b"never-export-this-secret" not in plain
        assert b"source-key-id" not in plain

        with archive.open("rb") as handle:
            first = merge_research_bundle(
                handle,
                trader=target_trader,
                model_lab=target_lab,
            )
        with archive.open("rb") as handle:
            second = merge_research_bundle(
                handle,
                trader=target_trader,
                model_lab=target_lab,
            )

        assert first["sha256"] == second["sha256"] == written["sha256"]
        with target_trader._db.cursor() as cur:
            target_trader._db.execute(
                cur,
                "SELECT COUNT(*) FROM live_trading_journal WHERE id=%s",
                (journal_id,),
            )
            assert cur.fetchone()[0] == 1
        with target_lab._db.cursor() as cur:
            target_lab._db.execute(
                cur,
                "SELECT COUNT(*) FROM sport_model_candidates WHERE id=%s",
                (candidate_id,),
            )
            assert cur.fetchone()[0] == 1
    finally:
        source_trader.close()
        source_lab.close()
        target_trader.close()
        target_lab.close()


def test_research_bundle_rejects_tampered_checksum(tmp_path):
    trader = _trader(tmp_path / "source.db")
    lab = SportModelLab(str(tmp_path / "lab.db"))
    archive = tmp_path / "evidence.ndjson.gz"
    target_trader = _trader(tmp_path / "target.db")
    target_lab = SportModelLab(str(tmp_path / "target-lab.db"))
    try:
        _journal(trader, row_id=str(uuid4()), payload={"edge": 0.05})
        write_research_bundle(
            archive,
            traders={"live": trader},
            model_lab=lab,
        )
        records = gzip.decompress(archive.read_bytes()).splitlines()
        trailer = json.loads(records[-1])
        trailer["sha256"] = "0" * 64
        tampered = gzip.compress(
            b"\n".join([*records[:-1], json.dumps(trailer).encode()]) + b"\n"
        )
        with pytest.raises(ResearchBundleError, match="checksum"):
            merge_research_bundle(
                io.BytesIO(tampered),
                trader=target_trader,
                model_lab=target_lab,
            )
    finally:
        trader.close()
        lab.close()
        target_trader.close()
        target_lab.close()


def test_runtime_credentials_are_memory_only_and_revoke_automation(tmp_path):
    trader = _trader(tmp_path / "trading.db")
    try:
        trader.configure({
            "automation_enabled": True,
            "execution_mode": "dry_run",
        })
        status = trader.set_runtime_credentials(
            "runtime-key-id",
            "runtime-secret-that-must-not-be-written",
        )

        assert status["credential_source"] == "runtime"
        assert status["retention"] == "process_memory_until_restart"
        assert trader.status()["policy"]["automation_enabled"] is False
        with trader._db.cursor() as cur:
            cur.execute("SELECT payload FROM live_trading_config")
            stored = "\n".join(str(row[0]) for row in cur.fetchall())
            cur.execute("SELECT payload FROM live_trading_journal")
            stored += "\n".join(str(row[0]) for row in cur.fetchall())
        assert "runtime-secret-that-must-not-be-written" not in stored
        assert "runtime-key-id" not in stored

        with pytest.raises(TradingPolicyError, match="both Polymarket"):
            trader.set_runtime_credentials("only-one-value", "")
    finally:
        trader.close()
