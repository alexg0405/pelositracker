"""Point every local store at throwaway databases during tests."""
import os
import tempfile


def _temporary_db(prefix: str) -> str:
    descriptor, path = tempfile.mkstemp(suffix=".db", prefix=prefix)
    os.close(descriptor)
    return path


# Must be set before app.main creates the stores during lifespan startup.
# An empty value also prevents load_dotenv() from restoring a developer's real
# PostgreSQL URL during collection. Individual database-selection tests opt in
# explicitly with monkeypatch.
os.environ["DATABASE_URL"] = ""
os.environ["LEDGER_DB"] = _temporary_db("test_ledger_")
os.environ["ACCOUNTS_DB"] = _temporary_db("test_accounts_")
os.environ["HISTORY_DB"] = _temporary_db("test_history_")
os.environ["STATE_DB"] = _temporary_db("test_state_")
