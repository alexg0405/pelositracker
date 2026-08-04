"""The hosted-data puller: pure helpers only (no network in tests)."""
from decimal import Decimal

import pytest

from tools.pull_hosted_data import (
    PROTECTED,
    adapt,
    database_url,
    mirror_path,
    read_env_file,
    redact,
)


def test_redact_strips_credentials():
    url = "postgresql://user:secret@db.example.com:5432/postgres"
    cleaned = redact(url)
    assert "secret" not in cleaned
    assert "user" not in cleaned
    assert "db.example.com" in cleaned


def test_env_file_parse(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\nDATABASE_URL='postgresql://u:p@h/db'\nOTHER=1\n",
        encoding="utf-8",
    )
    values = read_env_file(env)
    assert values["DATABASE_URL"] == "postgresql://u:p@h/db"


def test_database_url_requires_postgres_dsn(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=sqlite:///nope.db\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        database_url(env)


def test_database_url_missing_everywhere(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        database_url(tmp_path / ".env")


def test_adapt_converts_postgres_types():
    assert adapt(Decimal("0.25")) == 0.25
    assert isinstance(adapt(Decimal("0.25")), float)
    assert adapt(True) == 1
    assert adapt(memoryview(b"xy")) == b"xy"
    assert adapt(None) is None
    assert adapt("text") == "text"


def test_mirror_path_refuses_workstation_databases(tmp_path):
    protected = next(iter(PROTECTED))
    with pytest.raises(SystemExit):
        mirror_path(protected.parent, protected.name)
    safe = mirror_path(tmp_path, "hosted-live.db")
    assert safe == (tmp_path / "hosted-live.db").resolve()
