# Live Edge Monitor

The application includes a separately gated, default-dry-run Polymarket US
execution sidecar without changing the established calculation engine. For an
isolated Windows installation, start with [WORKSTATION.md](WORKSTATION.md).

An auditable sports-market research system. It records Polymarket
books, independent sportsbook source-family prices, and provider game state;
then produces reproducible `WATCH` or `PAPER_BET` policy output through a Rust
engine and FastAPI dashboard.

> The original engine remains research/paper oriented. The optional Polymarket
> US order layer has explicit bankroll limits, previewed fill-or-kill orders,
> expiring arming, and a kill switch. Signal quality is data reliability—not win
> probability or a guarantee of profit.

## Safety behavior

The system fails closed. A selection remains `WATCH` when any required input is
missing or unsafe, including provider time, identity confidence, valid game
state, independent source families, complete executable depth, market status,
fee metadata, calibration evidence, or risk capacity.

No calibration artifact and no independently validated sport model are shipped.
Therefore a default installation is display/research-only and will not emit an
eligible paper fill. See [model support](docs/model-support.md).

## Data and decision pipeline

1. A Polymarket link is resolved into event, market, outcome, and token identity.
2. Complete books are fetched with the bulk `/books` endpoint and maintained from
   verified WebSocket snapshots/deltas. A hash/timestamp gap forces resnapshot.
3. The Odds API, when configured, contributes bookmaker update timestamps and
   quota telemetry. The undocumented Action Network and Pinnacle guest adapters
   are disabled unless explicitly enabled and credentialed.
4. Provider time, receipt time, and processing time remain distinct. Unknown
   provider time never falls back to local receipt time for policy eligibility.
5. One de-vigged probability per independent source family is aggregated with
   the reviewed artifact's consensus method. Equal-family logit pooling is the
   fail-safe default; the target venue is excluded.
6. Decimal execution walks full ask depth for the configured paper notional and
   includes the market fee curve. Incomplete fills are rejected by default.
7. The Rust boundary receives an explicit UTC `as_of`; identical canonical input
   produces the same decision hash in live evaluation and replay.
8. Decision-time, close-time, fill, and settlement marks are stored separately.
   CLV compares the recorded paper fill with the last valid executable close; it
   is never reconstructed from settlement-time consensus.

## Quick start on Windows

Requirements: Python 3.10–3.15, Rust, and Microsoft C++ Build Tools.

```cmd
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
build-rust.cmd
start.cmd
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Local development defaults
to `admin` / `admin`; production startup rejects those credentials. `start.cmd`
creates `.env` from `env.example` and runs one feed-owning worker.

For the isolated Polymarket US workstation, run `setup-workstation.cmd` once,
then `start-workstation.cmd`. It binds to `127.0.0.1:8775`, disables the
separate paper-bot subsystem, and keeps its SQLite data under
`workstation-data/`.

## Core configuration

```env
THE_ODDS_API_KEY=
ODDS_POLL_SECONDS=45
ODDS_REGIONS=us
ODDS_MARKETS=h2h,spreads,totals
ODDS_BOOKMAKERS=

WORKSTATION_MODE=false
ENABLE_PAPER_BOTS=true
ENABLE_POLYMARKET_US_TRADING=false
POLYMARKET_US_KEY_ID=
POLYMARKET_US_SECRET_KEY=
POLYMARKET_US_TRADING_DB=polymarket-us-trading.db

MAX_DATA_AGE_SECONDS=120
SIGNAL_CONFIDENCE_THRESHOLD=0
SIGNAL_EDGE_THRESHOLD=0
SIGNAL_KELLY_FRACTION=0.25
PAPER_ALLOW_UNCALIBRATED=false

ENABLE_ACTION_NETWORK=false
ENABLE_PINNACLE_GUEST=false
PINNACLE_GUEST_API_KEY=
ENABLE_INDEPENDENT_MODELS=false
INDEPENDENT_MODEL_ARTIFACT=
CALIBRATION_ARTIFACT=

DATABASE_URL=
LEDGER_DB=ledger.db
HISTORY_DB=history.db
APP_ENV=development
WEB_CONCURRENCY=1
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin
```

Polymarket public market data requires no key. The Odds API integration starts
only when `THE_ODDS_API_KEY` is set. Its `x-requests-*` headers are retained in
`/api/runtime`. The Discovery control accepts 1–3,600 seconds without a restart;
choose an interval that stays within `MAX_DATA_AGE_SECONDS` and your provider
quota.

For MLB, the execution policy can independently select full game, first-inning
runs, and first-five innings. First-five winner, spread, and total products
follow the existing line-type filters. The event-specific period markets are
requested from The Odds API only while an enabled execution lane selects them.
Dry-run segment outcomes are retained separately by market scope. Live segment
orders remain locked until the operator explicitly enables them in the live
lane; this never changes the existing probability, edge, signal-quality, or
calibration calculations.

SQLite is the local default. `DATABASE_URL` selects PostgreSQL for every store.
All stores use component-scoped, versioned migrations with checksum drift
protection; the same migrations can be applied repeatedly. PostgreSQL stores
share one bounded process-wide connection pool instead of reserving one session
per component. `POSTGRES_POOL_MAX_CONNECTIONS` defaults to `2` and is capped at
`4`, which preserves short-transaction concurrency without exhausting small
managed session pools during an overlapping Render deploy.

Authentication uses Argon2 password hashes and individually revocable,
expiring sessions. State-changing requests require a double-submit CSRF token.
Production requires non-default credentials and one worker until distributed
feed ownership is implemented. Webhooks require HTTPS, an explicit host
allowlist, public DNS results, and no redirects.

Hosted Polymarket US execution is opt-in. Set
`ENABLE_POLYMARKET_US_TRADING=true` plus both API credential variables in the
hosting provider's private environment. Keep `WORKSTATION_MODE=false`, use the
existing `DATABASE_URL` for persistent execution history, and retain
`WEB_CONCURRENCY=1`. Source deployment alone cannot arm live orders: automation
defaults off, execution defaults to dry-run, every restart disarms the
process-local live latch, and live arming requires the exact confirmation phrase.
Hosted dry-run and live automation use separate schedulers and policies. When
PostgreSQL is configured, simulated execution is isolated in the
`polymarket_us_dry_run` schema while existing live execution remains in the
default schema, so both lanes can run at the same time without overwriting one
another. The analysis-cycle control accepts 1-300 seconds and re-evaluates the
latest retained state; it does not independently call The Odds API.

The performance datasheet aggregates the retained live and dry-run stores when
its execution filter is set to **Dry run + live**. The settings advisor also
offers an explicit all-data comparison with per-lane sample counts. Combined
live/simulated evidence is exploratory only and cannot one-click validate a live
policy; live-only validation is still required before supported application.

The authenticated **Account connection** panel can also verify and use a
Polymarket US key pasted from a phone. That key is held only in server-process
memory: the application does not write it to browser storage, PostgreSQL,
SQLite, logs, exports, or Git, and it disappears on restart. Installing or forgetting a runtime key
stops automation and disarms live execution. Account switching is blocked while
a live managed position remains open.

### Local and hosted research evidence

Database files and trade records are intentionally ignored by Git. In
production, `DATABASE_URL` is the central PostgreSQL evidence store; without it,
the Polymarket US status panel explicitly reports hosted SQLite as ephemeral.
The hosted Model Lab and managed-trade journal use the same PostgreSQL database
with component-scoped migrations.

To create one aggregated hosted evidence store after deployment:

1. Open **Polymarket US Research → Model lab → Synchronize local and hosted research
   evidence** on the local workstation.
2. Download the compressed evidence archive.
3. Open the same panel on the hosted site and upload that archive once.

The merge is checksum-validated and idempotent. It carries closed managed
trades, settings-at-entry, journal evidence, labeled adaptive-exit observations,
and Model Lab observations/targets/candidates. Live and dry-run batches retain
their original lane, so simulated evidence cannot contaminate the live store.
It excludes every credential, cookie, environment value, order, open position,
and execution control. Reusing the same archive is safe: existing destination
primary-key rows are preserved.

After that one-time upload, a dry-run lane started from a phone keeps running
inside the hosted worker even after the page is closed and writes directly to
the durable PostgreSQL dry-run schema. The website's combined datasheet and
advisor can immediately analyze those new hosted rows alongside the imported
local evidence. To refresh the workstation's local copy later, perform the same
transfer in reverse: export from the hosted site and merge the archive locally.
This deliberate archive exchange avoids exposing the production database to a
home machine or placing research databases in Git.

## Registering an event

Paste the full event URL shown in Polymarket, for example:

```text
https://polymarket.com/event/example-event-slug
```

The slug is the text after `/event/`. The dashboard resolves active CLOB tokens
and lists only selections with an executable ask. Visibility does not imply a
user is allowed to trade in their jurisdiction.

Manual API registration is also available:

```json
{
  "name": "Away at Home",
  "sport": "basketball",
  "league": "nba",
  "home": "Home",
  "away": "Away",
  "polymarket_slug": "exact-event-slug",
  "odds_api_sport": "basketball_nba",
  "odds_api_event_id": "provider-event-id"
}
```

Provider joins require sport, league, both participants, and start-time evidence.
Ambiguous doubleheaders are quarantined, not guessed.

## Reading the dashboard

- **Consensus probability**: the selected transformation of one price per
  independent source family, excluding the target venue. It is not a sport model.
- **Calibrated consensus**: consensus transformed by a reviewed chronological
  identity/beta calibration artifact; unavailable means display-only.
- **Independent model**: a separately validated sport model, when available.
  It requires operator opt-in and a reviewed exact-segment registry artifact.
  None is enabled in the repository today.
- **Market probability**: fee/slippage-adjusted executable paper price.
- **Net EV**: calibrated probability minus depth-weighted executable cost and
  fees, per share and for the simulated fill.
- **P(net EV > 0)**: share of aligned historical event-block draws whose net EV
  is positive. It is unavailable without an eligible artifact.
- **Required edge**: configured base floor plus the declared market premium.
- **Signal quality**: a policy score over completeness, provider freshness,
  identity, execution, source independence, sample support, and calibration
  support. It is not a win probability.
- **WATCH**: at least one mandatory gate failed or is unknown.
- **PAPER_BET**: all gates passed for a simulated paper order only.
- **CLV**: last valid executable close minus recorded paper fill price.

## Paper bots

Paper bots use fake bankrolls only. Entries walk complete Polymarket ask depth
and include fees. Open positions are valued at full executable bid depth after
sell fees, so the bid/ask spread is visible immediately instead of being hidden
by cost-based valuation. Each bot has an automatic cash-out switch; enabled
bots apply minimum-hold, minimum-move, net-profit, trailing-profit, calibrated-
estimate reversal, and stop thresholds. A provider cancellation may void an
open position, but manually removing an event cannot.

See [paper-bot lifecycle](docs/paper-bot-lifecycle.md) for the exact policy,
stored research fields, and analysis endpoints.

## Repository map

- `app/domain/`: canonical time, gate, and quality contracts.
- `app/identity.py`: deterministic identity and mapping decisions.
- `app/execution.py`, `app/orderbook.py`: Decimal fills and book state machine.
- `app/`: providers, API, session security, migrations, lifecycle, and replay.
- `native_engine/`: pure explicit-`as_of` consensus/policy engine.
- `migrations/`: dialect migration-ledger snapshots.
- `tests/fixtures/providers/`: golden provider payloads.
- `docs/audit-baseline.md`: pre-remediation evidence and rollback point.

Design and operating records are in [architecture](docs/architecture.md),
[data lineage](docs/data-lineage.md), [provider support](docs/provider-support.md),
[consensus model card](docs/model-card-consensus.md),
[independent-model registry](docs/independent-model-registry.md),
[backtesting methodology](docs/backtesting-methodology.md),
[paper-bot lifecycle](docs/paper-bot-lifecycle.md),
[security](docs/security.md), and [operations](docs/operations.md).

## Offline calibration workflow

Milestone E artifacts are built offline; the live service never trains or
promotes itself. Export settled, point-in-time/out-of-fold observations as
JSONL and declare candidate pipeline metadata in JSON, then run:

```cmd
.venv\Scripts\python.exe -m app.model_training observations.jsonl candidates.json calibration-v2.json --selection-through 2024-06-30T23:59:59Z --calibration-through 2024-12-31T23:59:59Z --validation-through 2025-03-31T23:59:59Z --model-version consensus-2025q1 --sport basketball --league nba --market moneyline
```

The builder refuses mixed segments, event leakage, fewer than 1,000 observations
in any fold, and fewer than 200 event-block draws. Review the artifact and its
test metrics before setting `CALIBRATION_ARTIFACT`; producing a file does not
establish a statistical or profitable edge.

Independent models use a separate `INDEPENDENT_MODEL_ARTIFACT`. Its entries
must have exact sport/league/market identity, immutable model and data hashes,
chronological train/validation/test windows, at least 1,000 untouched-test
  observations from 200 events, same-row proper-score improvement over
  consensus, pregame, and Stern baselines, time/lead calibration slices, at
  least 1,000 event-block draws, search-multiplicity control, required-input
  declarations, and explicit review approval. Registry v1 is limited to a
  fitted NBA moneyline logistic contract; score/clock Brownian routines remain
  benchmark-only.
`ENABLE_INDEPENDENT_MODELS=true` without this artifact has no effect. No such
artifact ships with this repository.

## Verification

```cmd
.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp
.venv\Scripts\python.exe -m ruff check app tests
.venv\Scripts\python.exe -m mypy app/domain app/execution.py app/identity.py app/security.py app/settings.py app/calibration.py app/model_training.py app/model_registry.py
cargo fmt --manifest-path native_engine\Cargo.toml -- --check
cargo test --manifest-path native_engine\Cargo.toml
cargo clippy --manifest-path native_engine\Cargo.toml --all-targets -- -D warnings
```

CI also performs a dependency audit and applies all migrations twice against
PostgreSQL. `/api/health` is liveness; `/api/ready` checks initialized runtime
dependencies; authenticated `/api/runtime` exposes counters and provider quota.
