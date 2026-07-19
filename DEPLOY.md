# Deploying Live Edge Monitor

This is a stateful, always-on service. It holds WebSocket connections, runs background pollers, serves Server-Sent Events, ships a compiled Rust extension, and records paper results. Use a host that keeps one container process running; request-only serverless platforms are not suitable for the backend.

The included `Dockerfile` builds the Rust engine and runs the FastAPI app on the host-provided `PORT`.

## Production settings

Set these deliberately in the host's environment:

- `ADMIN_USERNAME` and a strong `ADMIN_PASSWORD`, or `AUTHORIZED_USERS` for multiple logins.
- `ODDS_POLL_SECONDS=20` and `MAX_DATA_AGE_SECONDS=120`. The poll cadence must remain shorter than the accepted quote age.
- `SIGNAL_CONFIDENCE_THRESHOLD=0` and `SIGNAL_EDGE_THRESHOLD=0` when custom paper bots should enforce their own thresholds.
- `THE_ODDS_API_KEY` only when the paid sportsbook feed is wanted.
- `DATABASE_URL` for optional PostgreSQL persistence.

If `DATABASE_URL` is unset, the app falls back to SQLite. `LEDGER_DB` stores positions, accounts, tracked events, and auto-monitor settings, and `HISTORY_DB` stores quote/outcome history. Container-local SQLite files may disappear after a restart or redeploy unless the host supplies a persistent volume.

## Render blueprint

1. Push the repository to GitHub.
2. In Render, create a Blueprint from the repository; `render.yaml` defines the Docker web service and `/api/config` health check.
3. Supply authentication credentials and any optional feed/database values prompted by the Blueprint.
4. Deploy, sign in, then add an active market from **Discovery** or paste a Polymarket event link.

For durable data, either set `DATABASE_URL` to a PostgreSQL DSN or attach a persistent disk. With the example `/var/data` disk, set:

```env
LEDGER_DB=/var/data/ledger.db
HISTORY_DB=/var/data/history.db
```

Accounts and monitor state use `LEDGER_DB` by default; set `ACCOUNTS_DB=/var/data/accounts.db` or `STATE_DB=/var/data/state.db` only if separate SQLite files are desired. The optional disk and path settings are included as comments in `render.yaml`.

Instances that sleep will pause the live streams and pollers until the service wakes. An always-on instance is required for uninterrupted monitoring.

## Railway

Railway can build the included `Dockerfile` and injects `PORT`. Deploy the repository, add the production environment variables above, and use either PostgreSQL through `DATABASE_URL` or a volume mounted at `/var/data` with the two SQLite paths shown above.

## Any Docker host or VPS

Create a production `.env`, then run:

```bash
docker build -t live-edge-monitor .
docker run --env-file .env -p 8000:8000 live-edge-monitor
# open http://localhost:8000
```

If using SQLite, also mount the directory containing `LEDGER_DB` and `HISTORY_DB`. Do not bake `.env` or credentials into the image.

## Serverless frontends

A static frontend may be hosted separately, but `/api/*` must point to the always-on Docker backend. Request-scoped functions cannot preserve the service's WebSockets, polling tasks, SSE clients, or local SQLite state between invocations.
