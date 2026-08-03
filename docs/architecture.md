# Architecture

## Supported topology

One FastAPI process owns provider subscriptions, normalization, decisions, SSE
clients, and finalization. Durable observations and paper records use
PostgreSQL in production or SQLite for local research. `WEB_CONCURRENCY` must
be `1`; production startup rejects any other value because feed ownership and
event locks are process-local.

## Components

- `app/main.py`: lifespan, authenticated API, task supervision, and orchestration.
- `app/sources.py`: Polymarket and The Odds API transport normalization.
- `app/domain/`, `app/identity.py`, `app/gameclock.py`: time, identity, gate,
  and league-state contracts.
- `app/orderbook.py`, `app/execution.py`: verified book state and deterministic
  Decimal paper fills.
- `app/engine.py` and `native_engine/`: canonical JSON boundary and pure
  explicit-`as_of` policy calculation.
- `app/model_training.py`, `app/calibration.py`, `app/model_registry.py`: offline nested chronological
  model selection, beta calibration, event-block artifact construction, and
  strict consensus/independent-model artifact loading. Training never runs in
  the web process and no independent model artifact ships.
- `app/history.py`, `app/ledger.py`: immutable evidence and decision/order/fill/
  close/settlement marks.
- `app/polymarket_us_research.py`, `app/polymarket_us_trading.py`: the Polymarket
  US execution sidecar — read-only public inventory, authenticated account and
  order-book reads, and the isolated dry-run/live execution lanes.
- `app/static/`: static HTML, CSS, local vendored libraries, and event-driven JS.

## Execution boundary

The original engine, paper-bot subsystem, and `app/execution.py` remain
paper-only: deterministic Decimal fills against verified book state, with no
wallet, private key, or signing component anywhere in the repository.

The Polymarket US sidecar is **not** paper-only. When the workstation is started
with `ENABLE_POLYMARKET_US_TRADING=true`, its live lane authenticates to
Polymarket US with an API key/secret and can place real fill-or-kill limit
orders. That capability is gated by, in order: a saved live-mode policy, a
bounded arming latch that a restart always clears, an approval token on every
protected action, hard 5¢–95¢ price bounds, and the allocation/exposure/reserve
limits described in `WORKSTATION.md`. The dry-run lane never authenticates and
never routes an order.

Treat the two lanes as different trust domains. Anything that widens what the
live lane may submit is a real-money change and needs the security review in
`docs/security.md`, not just a test.

## Concurrency boundary

Per-event locks serialize quote evaluation against finalization. Database work
in feed callbacks and finalization runs in the bounded asyncio thread executor.
Provider and notification tasks are owned by the lifespan and drained on
shutdown. Horizontal collectors require a future durable lease/message-stream
design and are not supported by this release.
