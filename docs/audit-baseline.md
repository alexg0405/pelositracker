# Audit baseline

Date: 2026-07-20
Baseline commit: `2cf36c4` (`main`)
Rollback branch: `backup-main-pre-audit-remediation-20260720`

This is the pre-remediation evidence record for the paper-only sports-market research application. The system is not an order-routing product and must not acquire wallet, signing, deposit, or real-money execution capabilities.

## Reproduction environment

- Windows / PowerShell
- Python 3.14.0 in `.venv`
- Rust 1.97.0 / Cargo 1.97.0
- Git 2.55.0.windows.2
- Docker unavailable in the audit environment
- Node unavailable on the system `PATH`

The existing `psycopg2-binary==2.9.10` pin has no Python 3.14 wheel. Its source build requires `pg_config`, which is not installed. This is a dependency compatibility defect rather than an application-test failure.

## Baseline verification

```powershell
.\.venv\Scripts\python.exe -m maturin develop --release
.\.venv\Scripts\python.exe -m pytest -q
& "$env:USERPROFILE\.cargo\bin\cargo.exe" test --manifest-path native_engine\Cargo.toml
& "$env:USERPROFILE\.cargo\bin\cargo.exe" fmt --manifest-path native_engine\Cargo.toml -- --check
```

- Python: 97 passed, 1 failed.
- The only Python failure is `tests/test_persistence.py::test_database_url_keeps_postgres_as_the_default`, caused by the unavailable PostgreSQL driver in this Python 3.14 environment.
- Rust: 10 passed.
- Rust formatting: passed.
- Clippy: unavailable because the Rust component is not installed.

## Findings and evidence

### Time, state, and determinism

- `app/models.py` uses wall-clock defaults and overloads `observed_at`; provider, receipt, and processing timestamps are not distinct.
- `app/gameclock.py` converts missing clocks to plausible numeric values and has no explicit per-league clock rules or state-regression quarantine.
- `app/engine.py` has no required `as_of` input.
- `native_engine/src/lib.rs` reads `SystemTime`, so identical input can produce different decisions.
- `app/replay.py` rebases historic observations to the current wall clock.

### Identity and persistence

- Providers are joined through fuzzy/substring matching without a canonical event/participant/market identity layer.
- Stores create their own tables with `CREATE TABLE IF NOT EXISTS`; there is no ordered, versioned migration ledger.
- Quote history throttles only on price probability, losing size, depth, status, and book-hash changes.
- Final consensus is recomputed at settlement and labelled CLV instead of preserving decision-time and close-time mark lineage.

### Providers and market data

- Polymarket books are fetched one token at a time even though the documented bulk `/books` endpoint is available.
- WebSocket deltas can force `accepting_orders=True`; sequence/hash gaps and reconnect snapshot recovery are not modeled.
- Sports scores have no verified home/away orientation and provider timestamps are commonly replaced with receipt time.
- Odds API update timestamps, bookmaker/source identities, and quota headers are discarded.
- Pinnacle and Action Network adapters are enabled without a validated contract; the Pinnacle adapter contains an embedded shared credential and an inaccurate API claim.

### Execution, lifecycle, and risk

- A top-of-book indication can become `PAPER_BET` without complete executable depth.
- There is no deterministic Decimal depth walk, partial-fill policy, fee schedule lineage, paper-order lifecycle, bankroll reservation, or close/settlement state machine.
- Confidence and model fields mix source consensus, independent model output, signal strength, and policy eligibility.

### Security and operations

- Authentication relies on a single global token and insecure default credentials; there is no per-session revocation/expiry, CSRF protection, or rate limiting.
- Webhook destinations are arbitrary URLs, creating an SSRF path.
- Inline scripts/styles, inline handlers, unpinned CDN assets, duplicate DOM IDs, and unsafe event interpolation prevent a strict Content Security Policy.
- Background notification tasks are untracked, health is not dependency-aware, and multi-worker ownership is not guarded.
- The container installs Rust through an unpinned network script, runs as root, and has no container health check.

## Safety posture during remediation

- Paper-only is a hard invariant.
- Unknown provenance, time, state, identity, liquidity, fee, or calibration data fails closed to `WATCH` or `NO_ACTION`.
- No independent sport model is eligible for actionable output without a versioned, out-of-sample validation artifact.
- Every decision must be reproducible from immutable inputs plus an explicit `as_of` value.
