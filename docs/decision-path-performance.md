# Decision-path performance baseline

Date: 2026-08-04
Measured with: `.\.venv\Scripts\python.exe -m tools.bench_decision`
Fixtures: `benchmarks/fixtures/*.json` (synthetic, deterministic, checked in)

This is the measurement record that the optimization work is supposed to move.
It exists because the functional suite proves the decision path is *correct* and
proved nothing about whether it is still *fast*: before this, a change that
doubled serialization cost or put CPU work back on the event loop would have
passed CI unnoticed.

Reproduce with the same command. Absolute numbers are machine-specific — compare
a run against a run on the same box, and treat the CI `benchmark` job's artifact
as trend data with wide error bars.

## Environment

- Windows 11, Python 3.11.9 in `.venv`, Rust/Cargo 1.97.0, PyO3 0.29
- `maturin develop --release`, no `[profile.release]` tuning (see below)

## Per-decision stage latency

`repeat=40`, `warmup=5`. All figures are p50 milliseconds.

| Fixture | quotes supplied → stored → scored | ingest | prepare | native | materialize |
|---|---|---:|---:|---:|---:|
| `small` | 6 → 6 → 6 | 0.05 | 0.44 | 0.20 | 0.09 |
| `normal` | 336 → 98 → 98 | 2.41 | 4.42 | 1.87 | 0.59 |
| `heavy` | 6,840 → 1,482 → 1,482 | 40.3 | 57.3 | 23.0 | 4.53 |
| `adversarial` | 20,094 → 1,482 → 1,482 | 121.4 | 64.6 | 23.6 | 4.51 |

`ingest` loads the whole fixture into a fresh `Store` in one go, so read it as a
per-quote cost (≈6 µs at `heavy`) rather than a per-callback one: in production
the duplicate tail arrives spread across many `on_quotes` calls. `prepare`,
`native` and `materialize` are per-decision costs as shown.

Request and output sizes at the Python/Rust boundary:

| Fixture | request | scorer output | signals |
|---|---:|---:|---:|
| `small` | 5.7 KiB | 8.5 KiB | 2 |
| `normal` | 74.4 KiB | 59.2 KiB | 14 |
| `heavy` | 1,130 KiB | 485.6 KiB | 114 |
| `adversarial` | 1,130 KiB | 485.6 KiB | 114 |

Persistence, `adversarial` fixture, 10 batches of 114 decision rows on a scratch
SQLite file: write batch p50 **17.4 ms** / max 39.4 ms, ≈**5,700 rows/s**;
bounded recent-decision read p50 **20.4 ms**.

This table was taken in a later, warmer session than the loop-lag and LTO
numbers below: `native` and `materialize` read ~15% higher here than in the
first run even though nothing touching them changed. That is exactly why the
memoization result in the next section is reported as an interleaved in-session
A/B rather than as a difference between two tables.

### The headline result: scoring is not the bottleneck

The deep-optimization report ranked the two Critical findings as (1) native
scoring blocking the event loop and (2) the double JSON bridge across the
Python/Rust boundary. Measurement supports (1) as a *responsiveness* problem —
see the loop-lag table below — but contradicts the implied cost ranking:

**On a prop-heavy event the entire native round trip, including both JSON
conversions, was ~10% of the decision cost when first measured. Python-side quote
payload construction plus ingestion was ~85%.**

The first measurement, at `heavy`, was ingest 104 ms + prepare 75 ms + native
20 ms + materialize 3 ms. `prepare` alone was ~3.8× `native`, and it is pure
Python.

This has a direct consequence for the roadmap. The report's milestone 2 proposes
a typed PyO3 request/result boundary at 4–8 days and medium-high risk, with a
shadow-equivalence harness, to remove JSON from the compute path. That work
targets a component worth ~10–18% of one decision, and only part of that is the
serialization itself. Reducing `prepare` and `ingest` targets the rest, and the
first pass at it (below) took hours, not days, with no behavior change at all.

## Second pass: memoized market and source identity

`cProfile` on `prepare_request` and `Store.add_quotes` showed both lanes spending
most of their time in the same place, and it was not arithmetic. For 6,840
quotes, `lines.base_market_type` was called **93,000 times** — about 13 times per
quote — each call re-running the same regex over the same handful of market
names. `comparison_keys` is a good illustration: 0.73 s cumulative but only
0.11 s of its own, the rest being `is_spread_market` → `base_market_type` →
`re.sub`.

These are pure functions of their string arguments over a vocabulary of a few
dozen bookmakers and a few hundred market names, so they are now memoized with
bounded `lru_cache`s: `lines._market_key`, `lines.market_scope`,
`lines.base_market_type`, `models.canonical_source`, `models.classify_source`.
The multi-argument callers (`comparison_keys`, `quote_line_side`) are left
uncached deliberately — almost all of their cost was these calls, so caching them
too would add cache keys for very little further gain.

Measured as an interleaved in-session A/B (both arms alternating in one process,
so machine drift hits both), `heavy` fixture, median of 3 rounds:

| Stage | memoized off | memoized on | speedup |
|---|---:|---:|---:|
| `ingest` | 142.6 ms | 43.1 ms | **3.31×** |
| `prepare` | 95.4 ms | 64.1 ms | **1.49×** |

The canonical request and the decision hash are byte-identical with the caches on
and off, which the A/B asserts directly and `tests/test_identity_caches.py` pins
against the undecorated implementations.

Caches are bounded by `maxsize`, so a provider emitting unbounded distinct market
strings degrades to cache misses — today's cost — rather than growing memory;
that eviction behavior is a test. Hit/miss rates are reported at
`GET /api/runtime` under `identity_caches`, because a memoization win is only
real if the hit rate is high on production vocabulary.

After this pass, `heavy` is ingest 40 ms + prepare 57 ms + native 23 ms +
materialize 4.5 ms. `prepare` is still the largest single stage. What remains
inside it is genuinely different work from what was removed: canonical JSON
encoding of a 1.1 MiB request (~15%, and it is the audit artifact, so it is not
free to change), the `Decimal` `simulate_buy` depth walk per exchange quote, and
building a 35-key dict per quote.

Two related observations fell out of building the fixtures:

- `SignalEngine._freshest_valid_payloads` is now close to a no-op on the live
  path. Its docstring still says "the store retains up to a couple thousand
  quotes per event", but `Store.quotes` has since become
  `dict[key -> latest Quote]` keyed by the same `comparison_keys` +
  `canonical_source` tuple the engine reduces on. It still matters for
  `app/replay.py`, which does not go through the store. The comment is stale.
- The reduction runs *after* `_quote_payload`, so the expensive `simulate_buy`
  walk is paid for quotes that are then dropped. In the `adversarial` fixture the
  store already removes most of them, but ordering the reduction before pricing
  would be strictly cheaper.

Neither is fixed here; both are recorded so the next pass starts from evidence.

A third finding, in the execution lane rather than the monitoring lane:
`_execution_snapshot_with_us_segments` (`app/main.py`) still calls
`engine.evaluate` synchronously on the event loop's thread, once per MLB event
per trading cycle. It is now timed as `trading.us_segment_eval`. It was left
synchronous deliberately: it reads `store.states` without holding `store.lock`,
which is only safe because `add_state` runs on that same thread, so the snapshot
has to move under the lock before the work can move to a worker. That is a small
but genuine change to the money path and belongs in its own review.

## Third pass: bounded per-store write lanes

Every store call went to `asyncio.to_thread` — the default executor. Dozens of
call sites, one shared and effectively unbounded queue, no priority, and no way
to see the backlog. Inside the per-event lock, `record()` awaited the ledger
write and then the model-lab write in series, even though they target different
databases.

`app/dbwriter.py` gives each store one lane: a dedicated worker thread, a bounded
queue, and two submission modes.

| Mode | Used for | Semantics |
|---|---|---|
| `submit` | `ledger.record_signals`, `account.mark_and_cash_out`, `account.place` | caller awaits durability |
| `schedule` | `model_lab.record` | caller does not await; work is queued, never dropped |

The split follows what each caller can honestly tolerate. The ledger row is the
decision's audit record and the bet's evidence, so an account entry must never
precede it. Nothing later in the decision reads model-lab rows, so they leave the
critical section.

Measured, `heavy` fixture, 114 decision rows per batch:

| Write | p50 | p95 | On the awaited path? |
|---|---:|---:|---|
| `ledger.record_signals` | 7.8 ms | 15.2 ms | yes — audit and money |
| `model_lab.record` | 0.75 ms | 1.7 ms | **no longer** |

Be honest about the size of this: the report ranked bounded writers "High", and
the latency actually removed from the critical section is ~0.75 ms of a ~125 ms
prop-heavy decision. The persistence cost that matters is `ledger.record_signals`,
and that one has to stay durable. The real deliverables here are structural
rather than a headline number:

* ledger, accounts and model-lab writes no longer contend for the same executor
  threads, so a research write cannot delay a fill;
* the backlog has a bound, a depth, and a wait time, all on `/api/runtime` under
  `db_writers` and as `db.<lane>.*` stages;
* there is now one obvious place to move further work off the critical section.

**Deliberate deviation from the report.** It suggests sampling or rejecting
low-priority writes when the queue is full. That is right for pure telemetry and
wrong here: model-lab rows are the training sample for calibration, so dropping
them under load would bias the fit toward quiet periods — a research-validity bug
no latency number justifies. A full queue applies backpressure to the submitter
instead, and `stop()` drains before the stores close. Saturation therefore shows
up as latency and queue depth, which are measurable, rather than as missing
evidence, which is not.

## Event-loop lag

`normal` fixture, 8 events scoring concurrently, lag sampled by an ordinary
timer task at a 5 ms interval. `event_loop_thread` calls the native round trip
inline (the pre-change shape); `worker_thread` awaits it via
`evaluate_prepared_async`, with the Rust side detaching around the scoring
kernel.

| Mode | lag p50 | lag p99 | lag max | throughput |
|---|---:|---:|---:|---:|
| `event_loop_thread` | 18.0 ms | 27.0 ms | 27.0 ms | 703 eval/s |
| `worker_thread` | 0.0 ms | 11.0 ms | 11.0 ms | 1,142 eval/s |

p99 loop lag is **2.4–3.8×** lower across runs when the native call is
offloaded, and throughput is not worse. This is the acceptance signal for the
`Python::detach` + `evaluate_prepared_async` change: the goal was never a faster
scorer, it was that scoring stops starving provider callbacks, health checks and
SSE heartbeats.

Windows timer granularity is ~15.6 ms, which is why the lag values quantize.
Treat the ratio as directional and re-measure on Linux for absolute numbers.

## Rust release-profile tuning: measured, rejected

`lto = "thin"` + `codegen-units = 1`, five runs each way, `repeat=300`:

| Build | `heavy` native p50 | run-to-run range | build time |
|---|---:|---|---:|
| default | 19.84 ms | 19.73 – 20.82 ms | 7.4 s |
| thin LTO | 19.63 ms | 19.58 – 20.35 ms | 44.0 s |

~1% at p50, inside the run-to-run spread, for a 6× slower build. The report's own
acceptance test was "repeated benchmark demonstrates benefit", so the profile is
**not** enabled; `native_engine/Cargo.toml` records this. Profile-guided
optimization is not worth attempting either while scoring is 10% of the cost.

`panic = "abort"` is separately unsafe here: PyO3 converts a Rust panic into a
Python exception, so aborting would take the whole trading process down instead
of failing one decision.

## What is instrumented in the running service

`GET /api/runtime` returns `performance.stages` and `performance.sizes`, each a
name → `{count, samples, p50, p95, p99, max, mean}` map built from bounded
recent-sample windows (`app/telemetry.py::DistributionRegistry`).

Stages: `event.lock_wait`, `event.snapshot`, `engine.prepare`,
`engine.canonicalize`, `engine.native`, `engine.materialize`, `ledger.write`,
`model_lab.write`, `account.model_probabilities`, `account.mark`,
`account.place`, `sse.snapshot`, `decision.total`, `trading.us_segment_eval`,
`trading.cycle`, `event_loop.lag`.

Sizes: `decision_request_bytes`, `decision_quote_payloads`,
`decision_output_count`, `sse_snapshot_bytes`, `sse_subscriber_count`.

`identity_caches` reports hits, misses, entries and `maxsize` for each memoized
identity function. `db_writers` reports depth, peak depth, bound, completed and
failed counts for each write lane; per-lane stages are `db.<lane>.queue_depth`,
`db.<lane>.queue_wait` and `db.<lane>.<operation>`.

Metric names are code-level literals only. `DistributionRegistry` caps the
number of distinct names and counts rejections, so interpolating an event id or
market slug into a name fails visibly instead of growing the registry.

## Not yet measured

The report's test pyramid also asks for browser render/long-task/heap numbers and
production-shaped PostgreSQL query plans. Neither is covered here: there is no JS
test runner in the repo, and no production-shaped database copy was available.
The composite and partial indexes the report proposes remain hypotheses until
they can be checked with `EXPLAIN (ANALYZE, BUFFERS)` against realistic row
counts — most of them do not exist yet, which is verified, but that they would
help is not.
