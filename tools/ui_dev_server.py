"""Isolated dev server for UI work only.

Boots the dashboard with local-only stores so a styling pass can never touch a
production database or spend paid Odds API credits:

* ``WORKSTATION_MODE=1`` makes ``app.main`` drop ``DATABASE_URL`` at startup, so
  every store opens its local SQLite file.
* ``THE_ODDS_API_KEY`` is blanked, so no sportsbook request is ever billable.
* ``ENABLE_POLYMARKET_US_TRADING=1`` renders the research tab, still disarmed --
  arming live orders stays a separate explicit action.

Run this instead of plain uvicorn while working on static assets::

    python -m tools.ui_dev_server

Every value below is a default, applied only when the variable is unset, so a
real environment or ``.env`` always wins. Credentials are not hardcoded: set
``UI_DEV_USERNAME`` / ``UI_DEV_PASSWORD`` to pick the throwaway login, otherwise
a random password is generated for the run and printed once.
"""

from __future__ import annotations

import os
import secrets

# Must all be set before app.main imports settings or calls load_dotenv().
_DEFAULTS = {
    "WORKSTATION_MODE": "1",
    "ENABLE_POLYMARKET_US_TRADING": "1",
    "ENABLE_PAPER_BOTS": "1",
    "APP_ENV": "development",
    "THE_ODDS_API_KEY": "",
}
for _key, _value in _DEFAULTS.items():
    os.environ.setdefault(_key, _value)

# Local-first by policy: never follow a configured production DSN from here.
os.environ.pop("DATABASE_URL", None)

_USERNAME = os.environ.get("UI_DEV_USERNAME", "uidev")
# A generated password keeps a working credential out of version control while
# still leaving the server usable without any setup.
_GENERATED = "UI_DEV_PASSWORD" not in os.environ
_PASSWORD = os.environ.get("UI_DEV_PASSWORD") or secrets.token_urlsafe(12)
os.environ.setdefault("ADMIN_USERNAME", _USERNAME)
os.environ.setdefault("ADMIN_PASSWORD", _PASSWORD)


def main() -> None:
    import uvicorn

    # The harness assigns the port via PORT; fall back to 8765 when run by hand.
    port = int(os.environ.get("PORT") or "8765")
    if _GENERATED:
        print(f"UI dev login: {_USERNAME} / {_PASSWORD}")
    print(f"UI dev server on http://127.0.0.1:{port} (local SQLite, no Odds API)")
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    main()
