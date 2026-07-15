# Deploying Live Edge Monitor

This is a **stateful, always-on** service — it holds open WebSocket streams to
Polymarket, runs background tasks, serves Server-Sent Events, ships a compiled
Rust engine, and writes a local SQLite ledger. It needs a host that runs a
persistent process. **It cannot run on Vercel/Netlify** (serverless: no
long-lived process, no persistent WebSockets, no local disk, no Rust build).

The included `Dockerfile` builds the Rust engine and runs the app anywhere that
takes a container.

## Render (blueprint)

1. Push this repo to GitHub (already at `anthonykholo/pelositracker`).
2. In Render: **New → Blueprint**, point it at the repo. `render.yaml` is picked
   up automatically (Docker web service, health check `/api/config`).
3. Set env vars in the dashboard (all optional):
   - `THE_ODDS_API_KEY` — enables sportsbook lines/props (Polymarket + demo work
     without it).
   - `ODDS_POLL_SECONDS` (default 20), `ODDS_PLAYER_MARKETS`, `SIGNAL_*`.
4. Deploy. Open the URL and click **Launch paper demo** or a live game.

**Free plan caveats:** sleeps after ~15 min idle (streams pause until the next
request) and has no persistent disk (the CLV ledger resets on redeploy). For an
always-on monitor with a durable ledger, set `plan: starter` and uncomment the
`disk` + `LEDGER_DB` blocks in `render.yaml`.

## Railway

Railway auto-detects the `Dockerfile` and injects `$PORT` (the app binds it).
New Project → Deploy from GitHub repo → add the same env vars → deploy. Add a
Volume mounted at `/var/data` and set `LEDGER_DB=/var/data/ledger.db` for a
durable ledger.

## Any Docker host / VPS

```bash
docker build -t live-edge-monitor .
docker run -p 8000:8000 -e THE_ODDS_API_KEY=... live-edge-monitor
# open http://localhost:8000
```

## Not supported: Vercel / Netlify / Lambda

Serverless can't keep the Polymarket WebSocket clients and background tasks
alive between requests, can't build the maturin/PyO3 extension in their Python
runtime, and gives no persistent disk for the ledger. If you must involve
Vercel, host only a static frontend there and proxy `/api/*` to a backend
deployed via the Dockerfile above.
