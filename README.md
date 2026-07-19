# Live Edge Monitor

A paper-only live sports market monitor with a Rust scoring engine and a FastAPI dashboard. It compares executable Polymarket prices with sportsbook reference prices to produce explainable `WATCH` or `PAPER BET` signals and evaluate paper-bot strategies.

> This project never places a wager. Signal quality is not a predicted win rate, and the output is not financial advice.

## Features

- Public Polymarket discovery, order-book, and sports-status feeds.
- Provider-confirmed live-game filtering; schedule-only starts remain unverified.
- URL-first event registration from a complete Polymarket event or mobile share link.
- Free Action Network/Pinnacle references for supported leagues, plus optional The Odds API prices and opt-in per-event player props.
- Rust-native consensus, freshness, spread, edge, uncertainty, and confidence calculations.
- Custom paper bots with independent edge thresholds and stake sizing.
- Durable paper positions, strategy results, and quote history.
- Local SQLite by default, with optional PostgreSQL for deployed environments.

## Quick start on Windows

Requirements: Python 3.10 through 3.15, Rust, and Microsoft C++ Build Tools.

```cmd
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
build-rust.cmd
start.cmd
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) and sign in. The local sample credentials are `admin` / `admin`; change them before making the service reachable from another machine. Add an active market from **Discovery** or paste its Polymarket event link.

`start.cmd` creates `.env` from `env.example` when needed and starts the server from the project directory. Rerun `build-rust.cmd` after changing `native_engine/src/lib.rs`.

## Configuration

Copy `env.example` to `.env` to override the built-in settings:

```env
THE_ODDS_API_KEY=
ODDS_POLL_SECONDS=20
ODDS_REGIONS=us
ODDS_MARKETS=h2h,spreads,totals
ODDS_BOOKMAKERS=
ODDS_PLAYER_MARKETS=

SIGNAL_CONFIDENCE_THRESHOLD=0
SIGNAL_EDGE_THRESHOLD=0
MAX_DATA_AGE_SECONDS=120
SIGNAL_KELLY_FRACTION=0.25
SIGNAL_EDGE_Z=1.0

DATABASE_URL=
LEDGER_DB=ledger.db
HISTORY_DB=history.db
# STATE_DB=state.db

ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin
```

The Polymarket public feeds do not require a key. The paid The Odds API integration remains disabled until `THE_ODDS_API_KEY` is set. `ODDS_PLAYER_MARKETS` is only added to per-event requests; unsupported sport/market combinations are rejected by that provider.

Mainline reference matching currently recognizes NBA, WNBA, NCAAB, NFL, NCAAF, MLB, NHL, UFC/MMA, boxing, MLS, EPL, La Liga, Serie A, Bundesliga, Ligue 1, Champions League, and the World Cup. Exact free-provider coverage varies by league; The Odds API fills additional supported leagues when configured. Discovery labels tennis, golf, NASCAR, esports, and any other event without a reference adapter as **PRICE ONLY**—their Polymarket books can be monitored, but the engine will not invent an edge. Player-prop edges require `ODDS_PLAYER_MARKETS` and are matched only for verified Gamma `PLAYER: STAT O/U N` contracts; unrecognized conditions remain market-only.

Keep `ODDS_POLL_SECONDS` below `MAX_DATA_AGE_SECONDS`. The 20-second poll and 120-second maximum age leave room for normal provider latency and a missed poll without accepting indefinitely stale quotes.

`SIGNAL_CONFIDENCE_THRESHOLD` and `SIGNAL_EDGE_THRESHOLD` are global engine floors, not custom-bot settings. Leave both at `0` so paper-bot strategies apply their own policy; raise them only when every engine signal should share those floors. `SIGNAL_EDGE_Z` still adds an uncertainty-aware required-edge buffer.

For multiple logins, set `AUTHORIZED_USERS=user1:password1,user2:password2` instead of the single `ADMIN_USERNAME` / `ADMIN_PASSWORD` pair. Keep secrets only in `.env`; that file is excluded from Git.

### Persistence

With no `DATABASE_URL`, the app uses SQLite: `LEDGER_DB` stores paper positions, accounts, tracked events, and auto-monitor settings, while `HISTORY_DB` stores quote and outcome history. The defaults are `ledger.db` and `history.db`. `ACCOUNTS_DB` and `STATE_DB` can optionally place account data and monitor state in separate SQLite files.

Set `DATABASE_URL` to a PostgreSQL connection string to use PostgreSQL for all persistent stores. When it is set, it takes precedence over the SQLite path variables.

## Registering an event

The dashboard accepts a Polymarket event link:

```text
https://polymarket.com/event/example-event-slug
```

The app resolves the event metadata, active markets, CLOB tokens, order books, and, when configured, a matching The Odds API event. It lists only selections currently accepting orders with an executable ask. Public market visibility does not imply that trading is available or legal for every user or location.

The API accepts the same URL-first registration:

```json
{
  "polymarket_url": "https://polymarket.com/event/example-event-slug"
}
```

Manual registration through `POST /api/events` remains available when provider inference needs help:

```json
{
  "name": "Away at Home",
  "sport": "basketball",
  "home": "Home",
  "away": "Away",
  "polymarket_slug": "exact-polymarket-event-slug",
  "odds_api_sport": "basketball_nba",
  "odds_api_event_id": "provider-event-id"
}
```

Providing an Odds API event ID avoids team-name ambiguity and limits the request to one event. API usage is billed by that provider, so tune polling and requested regions/markets for your plan while keeping the poll interval below the quote maximum age.

## Signal and bot guide

- **WATCH**: one or more engine safety gates failed.
- **PAPER BET**: the configured engine gates passed; no real wager is placed.
- **Model probability**: a consensus fair value computed without treating the target Polymarket quote as an independent reference.
- **Edge**: model fair probability minus the executable Polymarket ask on the card.
- **Required edge**: the base engine floor plus uncertainty and market-type risk adjustments.
- **Signal quality**: a 0-100 reliability score based on freshness, reference agreement, source coverage, and execution spread—not win probability.
- **Custom bot threshold**: a separate strategy-level edge floor evaluated by that paper bot.
- **Cash value**: shares multiplied by the current executable bid, before fees, slippage, or failed fills.

## Architecture

```text
Polymarket streams ─┐
Sportsbook polling ─┼─> Python feed adapters ─> Rust signal engine ─> FastAPI/SSE ─> Dashboard
Game-status feed ───┘                                  │
                                                       └─> SQLite or PostgreSQL
```

- `app/`: API, feed adapters, persistence, Python/Rust bridge, and dashboard.
- `native_engine/`: PyO3 Rust crate containing the scoring logic.
- `tests/`: engine, provider matching, persistence, API, and lifecycle coverage.
- `build-rust.cmd`: Windows native-extension build.
- `start.cmd`: local server launcher on port 8765.

## Tests

```cmd
.venv\Scripts\python.exe -m pytest -q
cargo test --manifest-path native_engine\Cargo.toml
```

## Model limitations

The signals and bot results are transparent heuristics, not a trained sport-specific model or an instruction to trade. Before considering real-money use, walk-forward test by sport, league, market, and game phase; calibrate probabilities; and account for latency, slippage, liquidity, limits, fees, market rules, suspended books, and rejected fills.

For production, use a licensed low-latency play-by-play provider as the authoritative game-state source. Public sports feeds may be delayed or incomplete.
