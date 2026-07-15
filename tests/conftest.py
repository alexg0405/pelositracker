"""Point the ledger at a throwaway database so tests never touch a real one."""
import os
import tempfile

# Must be set before app.main (and thus the Ledger) is imported.
_fd, _path = tempfile.mkstemp(suffix=".db", prefix="test_ledger_")
os.close(_fd)
os.environ["LEDGER_DB"] = _path
