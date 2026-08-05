"""Isolated dev server for UI work only.

Boots the dashboard with local-only stores so a pure styling pass can never
touch the production Supabase database or spend paid Odds API credits:

* ``WORKSTATION_MODE=1`` makes ``app.main`` drop ``DATABASE_URL`` at startup,
  so every store stays on local disk.
* ``THE_ODDS_API_KEY`` is blanked, so no sportsbook request is ever billable.
* ``ENABLE_POLYMARKET_US_TRADING=1`` renders the research tab that is being
  restyled, still disarmed — arming is a separate explicit user action.

Run it instead of plain uvicorn while working on static assets.
"""

from __future__ import annotations

import os

# Must be set before app.main imports settings / calls load_dotenv().
os.environ["WORKSTATION_MODE"] = "1"
os.environ["ENABLE_POLYMARKET_US_TRADING"] = "1"
os.environ["ENABLE_PAPER_BOTS"] = "1"
os.environ["APP_ENV"] = "development"
os.environ["THE_ODDS_API_KEY"] = ""
os.environ["ADMIN_USERNAME"] = "uidev"
os.environ["ADMIN_PASSWORD"] = "uidev"
os.environ.pop("DATABASE_URL", None)

if __name__ == "__main__":
    import uvicorn

    # The harness assigns the port via PORT; fall back to 8765 when run by hand.
    port = int(os.environ.get("PORT") or "8765")
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, reload=False)
