# Changelog

## Unreleased — Measured decision path

- Detached the Rust scorer from the interpreter (`Python::detach`) and split
  `SignalEngine.evaluate` into `prepare_request` / `evaluate_prepared` /
  `materialize_signals`, so `record()` awaits the native round trip on a worker
  instead of running it on the event loop's thread. p99 loop lag under eight
  concurrent events falls 2.4–3.8x with no throughput loss; decision hashes,
  signal ids, and lineage are bit-identical.
- Snapshotted the model/calibration lineage at request preparation, so a
  calibration installed mid-evaluation can no longer stamp a signal with lineage
  its own decision hash never covered.
- Added bounded stage-latency and size distributions (p50/p95/p99) plus an
  event-loop lag sampler, exposed on `/api/runtime` under `performance`.
- Added `tools/bench_decision.py` and immutable `benchmarks/fixtures/*.json`
  covering ingestion, preparation, the native boundary, materialization, loop lag
  under concurrency, and durable decision writes; CI publishes the report as a
  non-blocking artifact.
- Recorded the baseline in `docs/decision-path-performance.md`. It shows the
  native round trip is ~10% of a prop-heavy decision while Python-side quote
  payload construction and ingestion are ~85%, and that thin LTO produced no
  measurable gain, so no release-profile tuning was adopted.
- Memoized market and source identity normalization (`lines._market_key`,
  `market_scope`, `base_market_type`, `models.canonical_source`,
  `classify_source`) with bounded caches, after profiling showed
  `base_market_type` running ~13 times per quote on the same handful of market
  names. Interleaved in-session A/B on the prop-heavy fixture: quote ingestion
  **3.3x** faster, request preparation **1.5x** faster, canonical request and
  decision hash byte-identical. Hit rates are reported at `/api/runtime` under
  `identity_caches`; the caches evict rather than grow on unbounded input.
- Made the native-engine import fail closed at first scoring call rather than at
  import, so storage, security, and identity tests no longer need the compiled
  extension; production startup still refuses to run without it.
- Added `idx_account_bets_open_exposure`, a covering partial index on
  `account_bets(account, correlation_group, stake) WHERE status='open'`
  (accounts migration v5). `account_bets` previously had no index of its own,
  so the open-exposure sums in `place` — which run on every decision with entry
  candidates — read every row an account had ever placed: 12.2 ms at 100k rows,
  growing with history. Now 0.14 ms and 0.09 ms. Derived from real query plans
  via the new `tools/explain_queries.py`; the report's five other proposed
  indexes were measured and not adopted, since the queries they targeted are
  already served.
- Moved the canonical evaluation request out of `decision_marks` into a new
  `decision_inputs` table (ledger migration v9): one zlib-compressed row per
  `decision_hash` instead of a ~1.1 MiB blob inline on every `PAPER_BET` row.
  Measured 23.2x smaller on disk at one bet per decision and 68.8x at three.
  The decision hash still covers the uncompressed request, so replay and
  lineage verification are unchanged; `Ledger.decision_input()` reads the new
  table and falls back to the inline column for rows written earlier. The new
  table ages out on the same retention window, with its own `as_of` index.
- Batched the ledger's decision-mark and close-mark inserts through
  `Database.execute_many` instead of one statement per signal (~114 per
  prop-heavy tick). 1.21x on local SQLite; the round-trip saving it exists for
  only appears against the managed PostgreSQL backend, so CI's postgres job now
  measures that A/B and uploads it as the `postgres-write-benchmark` artifact
  rather than the speedup being asserted. The `bets` loop is unchanged: it
  branches on per-row `rowcount` and is only ever a few rows.
- Replaced shared-executor store calls with one bounded write lane per database
  (`app/dbwriter.py`). Ledger and account writes stay durable-before-continue;
  model-lab research rows are queued instead of awaited, leaving the per-event
  critical section. A full queue applies backpressure rather than dropping rows,
  because discarding model-lab observations would bias the calibration training
  sample, and shutdown drains before the stores close. Depth, wait time and
  per-operation latency are reported at `/api/runtime` under `db_writers`.
- Expanded Ruff to bugbear, asyncio, pyupgrade, simplify, perf, and Ruff-specific
  rules, added `tools/` to the lint target and `app/telemetry.py` to the type
  gate, and fixed the findings (including a blocking `unlink` on the event loop).

## Unreleased — Cost-aware paper-bot exits

- Replaced signal-price bot entries with exact full-depth Decimal ask walks,
  including fee, market-status, tick, minimum-size, identity, and depth gates.
- Added append-only executable bid marks, net open valuation, fee/P&L lineage,
  high-water tracking, and idempotent fake-money cash-outs.
- Added a persistent per-bot automatic cash-out switch and minimum-hold,
  minimum-move, hard-profit, trailing-profit, model-reversal, and stop policies.
- Rejected ungradeable markets before entry and blocked manual event removal
  while paper positions are open; authoritative provider cancellations still
  void positions.
- Added bot mark APIs and dashboard controls without adding wallets, signing,
  exchange credentials, or real-order routing.

## 0.6.0 — Independent-model evidence registry

- Added a fail-closed exact sport/league/market registry for independently
  validated model artifacts; `ENABLE_INDEPENDENT_MODELS` alone can no longer
  expose the legacy score/clock benchmark.
- Required chronological train/validation/untouched-test windows, model/data/
  calibration hashes, minimum test observations/events, same-row proper-score
  wins against consensus, pregame, and Stern baselines, time/lead calibration
  slices, event-block support, search control, and explicit review approval.
- Restricted the future runtime contract to a fitted NBA moneyline logistic
  model requiring pregame, score, time, possession, overtime, and phase
  features; the legacy Brownian score/clock formula remains benchmark-only.
- Persisted independent-model and calibration lineage in ledger v6 and added
  same-row evaluation without changing paper actions.
- Shipped no fitted model artifact. Every sport model therefore remains
  unavailable and no predictive-edge claim is made.

## 0.5.0 — Chronological calibration and paper risk controls

- Added a v2 fitted-artifact contract with event-grouped nested chronological
  folds, de-vig/consensus candidate metrics, monotone beta-or-identity
  calibration, model hashes, and aligned event-block pipeline/calibration/
  execution-cost uncertainty draws.
- Replaced dispersion-based pseudo-confidence with explicit calibrated
  probability intervals, probability of positive net EV, net EV after
  executable cost, and machine-readable policy gates.
- Added per-decision, event, sport, transparent correlated-group, and aggregate
  paper exposure caps plus decision lineage.
- Persisted Milestone E decision/fill fields in forward-only ledger v5 and
  accounts v2 migrations and expanded evaluation with scoring decomposition,
  execution, drawdown, concentration, and event-block summaries.
- Kept v1 artifacts and every unsupported sport model display-only. No fitted
  artifact, real-order capability, or statistical edge claim is shipped.

## Unreleased -- Auditable paper-research remediation

- Added explicit provider/receipt/processing/as-of timestamps and fail-closed freshness gates.
- Added canonical event/market identity decisions, quarantine states, versioned migration records, and lossless quote/state history.
- Made replay deterministic and tied decisions to canonical hashes and declared engine/config/model/calibration/execution versions.
- Added full-depth paper execution with fee, status, minimum-size, tick, partial-fill, portfolio, and closing-mark controls.
- Replaced subjective source weights with one observation per canonical source family and disabled actionable signals without chronological calibration support.
- Added Argon2id authentication, per-session revocation, CSRF protection, rate limits, SSRF-safe notifications, strict CSP, security headers, and local pinned frontend assets.
- Added provider supervision, readiness/runtime diagnostics, a single-worker production guard, non-root container execution, and broader CI checks.
- Preserved the paper-only boundary: no wallets, signing, exchange credentials, or real order routing were added.

## Unreleased — Live-status and signal audit

- Replaced the five-hour schedule guess with fresh Polymarket `sport_result` status for `LIVE`; schedule-only games now show `STARTED · VERIFYING`.
- Removed false matchup discovery caused by matching the letters `vs` inside participant names.
- Forced Polymarket cards to calculate edge against the exact executable Polymarket ask displayed on the website.
- Separated data quality from edge size and exposed freshness, agreement, source-coverage, and execution components.
- Changed entry ceilings to use the full risk-adjusted required edge and added raw edge / required edge / edge-buffer display.
- Removed the obsolete offline demo path so monitored signals come from live provider data.

## 0.4.0 — URL-first markets and paper positions

- Added full Polymarket event-link registration, including mobile share links.
- Automatically infers event metadata and attempts a quota-free The Odds API event match.
- Shows only active, order-accepting selections with an executable Polymarket ask.
- Added initial CLOB order-book snapshots, live depth, spread, liquidity, minimum size, and tick metadata.
- Added entry price ceilings, margin-to-ceiling guidance, and execution/data risk flags.
- Added durable user-entered paper positions with cash-out value, P/L, remaining hold edge, and explainable `HOLD`, `CONSIDER CASH`, or `EXIT WATCH` statuses.
- Preserved the market-relative Rust engine, cyberpunk HUD, and durable CLV/calibration truth loop.

## 0.3.2 — The Odds API V4

- Corrected authentication and request paths for The Odds API V4.
- Added configurable regions, markets, bookmakers, and a quota-safer polling default.
- Filtered sport-wide responses to the registered matchup and preserved spread/total points.
- Added sanitized terminal warnings and adapter tests without consuming API credits.
- Changed the default The Odds API polling interval to 45 seconds.

## 0.3.1 — Python 3.14 compatibility

- Upgraded PyO3 to 0.29 for Python 3.14 support.
- Refreshed the pinned Python dependencies and verified them on Python 3.14.
- Added a visible `env.example` so browser-based GitHub uploads do not omit the template.
- Made `.env` optional at startup and added Python 3.14 to CI.

## 0.3.0 — Merged release

- Merged the redesigned compact dashboard with the Rust-backed application.
- Preserved Rust-native scoring and the FastAPI feed architecture.
- Added persistent **Why this signal?** panels across live refreshes.
- Added event removal with task cancellation and in-memory cleanup.
- Clarified model probability, estimated edge, and signal-quality labels.
- Added accessible form labels, keyboard focus states, responsive layouts, and inline errors.
- Added refresh de-duplication and safer client-side rendering.
- Added `start.cmd`, repository cleanup rules, and GitHub Actions CI.
- Consolidated two divergent app copies into one canonical repository layout.
