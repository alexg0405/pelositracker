# Operations

Use one container and one worker. PostgreSQL is the production durability path;
SQLite is intended for local research and must be placed on persistent storage
if records need to survive redeploys.

- Liveness: `GET /api/health`.
- Readiness: `GET /api/ready` checks initialized repositories and native engine.
- Authenticated diagnostics: `GET /api/runtime` exposes provider counters,
  reconnects, quota headers, feed groups, and pending notifications.
- Polling: `ODDS_POLL_SECONDS=45` by default; keep it below the accepted maximum
  data age while respecting the provider quota.
- Models: `ENABLE_INDEPENDENT_MODELS=true` is inert unless
  `INDEPENDENT_MODEL_ARTIFACT` points to a valid reviewed exact-segment
  registry. Invalid artifacts abort startup; no artifact ships.
- Shutdown: provider groups are canceled/awaited, notifications are drained,
  and repositories are closed.

Back up the database before deploy. Migrations are forward-only, component
scoped, transactional, checksummed, and idempotent. A checksum mismatch aborts
startup rather than silently rewriting history. Roll back application code to
the pre-deploy branch/commit; do not reverse or delete recorded migration rows.

## Local storage maintenance (measured 2026-07-31)

Run both tools only with the workstation stopped. Each is read-only unless
given `--apply` plus its confirmation phrase, and each refuses to run against a
database another process holds open.

### Compaction

`tools/compact_local_databases.py` rewrites a file to reclaim pages already
freed by retention. It never deletes rows.

Measured on this workstation:

| Store | Before | After | Reclaimed |
|---|---:|---:|---:|
| `ledger.db` | 2,555.5 MB | 124.5 MB | 2,431.0 MB |
| `polymarket-us-dry-run.db` | 34.6 MB | 30.7 MB | 4.0 MB |
| `polymarket-us-trading.db` | 35.2 MB | 33.7 MB | 1.4 MB |
| **Total** | **9.89 GB** | **7.46 GB** | **2.44 GB** |

Verified after: `integrity_check` ok, `foreign_key_check` clean, and every
table's row count identical to a pre-compaction manifest.

`history.db` and `model-lab.db` had **0% reclaimable** and were deliberately
skipped. Compacting a 7 GB file with no free pages costs a long run and 7 GB of
temporary writes for no gain. Always inspect before applying.

### Why `history.db` is large

Its size is real data, not slack, so compaction cannot help it:

- 7.4 million quotes across 103 events over 7 days — roughly **1 GB per day**.
- `quotes_history` is a wide table (~45 columns of prices, sizes, fees, hashes,
  and lineage identifiers), averaging about 950 bytes per row.
- The retained bid/ask ladders are about **1.5 GB**, roughly a fifth of the
  file — a meaningful share, but not the main driver.

The dominant cost is therefore row count times row width. Reducing it means
retaining fewer rows: a longer capture throttle
(`HISTORY_QUOTE_MIN_SECONDS`/`HISTORY_QUOTE_HEARTBEAT_SECONDS`), or ageing out
whole finished events. Both trade away evidence and should be decided
deliberately rather than defaulted.

### Depth pruning

`tools/prune_history_depth.py` clears `bid_levels_json`/`ask_levels_json` from
quotes older than a chosen window. It never deletes a row, never touches quotes
inside the window, and refuses a window under two days.

Only `app/replay.py` reads those columns, so the cost is precise: replaying a
pruned tick sees an empty ladder instead of the original depth. Every scalar
the engine and research use lives in its own column and survives.

Reported on this workstation:

| Window | Eligible quotes | Ladder bytes |
|---|---:|---:|
| older than 2 days | 7,002,760 | 1.49 GB |
| older than 3 days | 6,630,804 | 1.43 GB |
| older than 5 days | 3,086,459 | 0.69 GB |

Pruning frees pages inside the file; run compaction afterwards to shrink it on
disk.

## Exit-policy measurement loop (added 2026-08-01)

### The settlement counterfactual, now repeatable

`tools/settlement_counterfactual.py` re-runs the hold-to-settlement analysis
from `docs/predictive-direction-2026-07-31.md` on demand, read-only, using the
application's own settlement function against `event_outcomes`:

```bash
./.venv/Scripts/python.exe -m tools.settlement_counterfactual workstation-data/polymarket-us-dry-run.db
```

Pass the live database path (`polymarket-us-trading.db`, the default) for the
live lane. `--seed` fixes the event-block bootstrap so runs are reproducible;
`--emit-pairs pairs.jsonl` writes per-position calibration pairs (entry price,
edge, stage, settled label) for calibration research.

Read the output the way the direction document prescribes: the counterfactual
assumes unlimited capital and prices no drawdown. It is evidence about exit
rules, not licence to remove stops.

### Pre-registered dry-lane test (fixed before the data arrives)

The dry lane already carries the 2026-08-01 recommended policy: `stop_loss`
0.50, `catastrophic_stop_multiplier` 1.6 (an 80% floor),
`candidate_cooldown_seconds` 300, `max_open_positions` 10, totals disabled,
full-game only, 20–45c entries, quality ≥ 70, `fee_edge_margin` 1.5. Success
criteria, unchanged from the handoff: at least 20 new independent events; the
stop-fire rate falls; ROI improves; maximum drawdown less than doubles.
Drawdown is the veto. Only if all pass: live, moneyline only, smallest
allocation.

### Phase 3 candidate: the price-only stop window

`stateless_stop_confirmation` (policy flag, default off, UI toggle under the
adaptive cash-out fieldset) addresses the measured reason the confirmation
stop never engaged: stops that fire while usable MLB inning state is missing
or stale sold immediately (31/37 dry, 19/20 live bypasses). With the flag on,
those stops run the same bounded confirmation window on price alone, with
grace capped at one minute; catastrophic losses and material model reversals
still exit immediately.

Keep it **off** during the pre-registered test above so the widened stop is
measured alone. Enable it in the dry lane as its own phase afterwards, and
judge it with the settlement counterfactual plus the same drawdown veto.

### Per-line confidence intervals (shadow)

`tools/venn_abers_shadow.py` fits inductive Venn-Abers intervals per line and
game stage on the settled pairs and applies the fee-implied refusal rule:

```bash
./.venv/Scripts/python.exe -m tools.venn_abers_shadow workstation-data/polymarket-us-dry-run.db
```

First run on the dry lane (2026-08-01): moneyline/early is the only pocket
where the calibrated point clearly beats the market price (leave-one-out
Brier 0.268 vs 0.407, median interval width 0.019); spread/early does not
beat price and 43% of its entries are refused outright by the fee floor.
Shadow only — nothing reads this at entry time.

## MLB game-day autopilot (added 2026-08-01)

Plan a slate from Discovery: batch-select the day's games and press **Plan
game-day dry run**. The plan monitors the selected games immediately, then:

- holds paid Odds API polling **off** until the first planned game goes live
  (schedule time reached or a live state observed — the sports stream and
  MLB linescore feed run regardless of the odds toggle);
- at first pitch, turns Odds API polling and **dry-run** automation on;
- when every planned game is final (or 7 hours past its start without ever
  going live — postponement guard), turns both off and disarms.

The plan survives a server restart (persisted in `monitor_config`) and is
strictly dry-lane: live arming still requires the approval token. Cancelling
the plan keeps the toggles as they are. After an autopilot night, dry
automation is deliberately left off; plan the next slate (or re-enable it in
the policy form) the following day. `GET/POST/DELETE /api/gameday` is the
API surface.

The dry lane is staged (2026-08-01) with the analysis-recommended policy:
the validated global stack plus per-line profiles carrying `profit_target`
0.30 on moneyline and spread. Starting the server with no autopilot plan
runs the dry lane continuously; arming a plan scopes it to game time.

### Discovery: MLB daily schedule and league filter (added 2026-08-01)

Discovery previously showed only Polymarket's own ranked pull, which let
busier sports crowd tonight's MLB games out of the 80-entry cap entirely.
Two fixes: league chips (`/api/discover?league=mlb`) scope the pull and the
cap to one league, and the MLB Stats API daily schedule is merged into the
MLB and All views — every scheduled game appears with its start time, live
games are tagged from the official feed, and games the venue has not listed
yet appear as "awaiting Polymarket listing" under their constructed slug
(`mlb-<away>-<home>-<official date>`, with AZ→ari / ATH→oak mapping).
Discovery defaults to the MLB view; the choice persists per browser.

## Moving local research data to Supabase (added 2026-08-02)

The application already speaks PostgreSQL: every store opens through
`app.database.Database`, which switches backend on `DATABASE_URL`, and the
pooling defaults (`POSTGRES_POOL_MAX_CONNECTIONS`, TCP keepalives) were
already tuned for Supabase's session pooler. Pointing the workstation at
Supabase is therefore a configuration change plus a one-time data copy.

### One-time copy

```bash
./.venv/Scripts/python.exe -m tools.migrate_to_supabase
```

Previews by default: it reports every store, its target schema, and how many
rows would move. To execute, put the Supabase connection string in the
environment (never on the command line, so it stays out of shell history)
and re-run with `--apply`:

```bash
./.venv/Scripts/python.exe -m tools.migrate_to_supabase --apply
```

Each store's own migrations create the remote schema, so the remote tables
are byte-identical to the local ones rather than a hand-maintained copy. Rows
are inserted in batches with `ON CONFLICT DO NOTHING`, so an interrupted run
is resumed by repeating the command, and `SERIAL` sequences are advanced past
the copied identifiers afterwards.

The dry lane keeps its schema isolation remotely (`polymarket_us_dry_run`),
exactly as the running server isolates it locally, so the two execution lanes
can never share a table.

### Size: the quote ladder does not fit, and is not needed

Measured 2026-08-02:

| Store | Size | Rows copied |
|---|---:|---:|
| history (`event_outcomes`, mappings, canonical events) | 7.8 GB | 544 |
| ledger (decision/close/settlement marks) | 250 MB | 54,285 |
| model lab (targets, observations) | 524 MB | 352,249 |
| live trading lane | 77 MB | 90,425 |
| dry-run trading lane | 199 MB | 279,015 |
| **Total copied** | | **776,524** |

`quotes_history` and `states_history` are ~8.65M rows and essentially all of
that 7.8 GB. They are excluded unless `--include-quote-history` is passed:
only tick-level replay reads them, they exceed Supabase's smaller tiers on
their own, and every research tool in `tools/` works from the settled
outcomes and lane databases that do fit comfortably.

### Where new data goes: the split policy

**Hosted (website) runs persist to Supabase; workstation runs stay local.**
This is enforced, not just configured: a hosted deployment (`WORKSTATION_MODE`
unset) opens every store — research stores and both execution lanes, the dry
lane in its `polymarket_us_dry_run` schema — against `DATABASE_URL`. The
workstation launcher sets `WORKSTATION_MODE=true`, and in that mode the
server deliberately ignores `DATABASE_URL` even when one is present in
`.env`, logging that all stores stay on local SQLite. A connection string
configured for the website or the sync tool can therefore never cause a
local session to write to the shared database.

To point the website at Supabase: set `DATABASE_URL` in the hosting
provider's environment (for Render, the service's environment settings —
`render.yaml` deploys from `main`). `/api/ready` confirms every store opened
against the target; a store that cannot reach Supabase fails there rather
than silently falling back.

The one-time copy above remains available if local history should seed the
hosted database; it opens the local files read-only, so they are never
modified. Local analysis tools keep reading the local files either way.

## Per-line execution profiles from settlement evidence (added 2026-08-02)

The 2026-08-02 settlement grading (1,030 graded positions, 72 events, both
lanes — `docs/mlb-line-profile-optimization-2026-08-02.md`) found the exit
rules line-specific: 67.5% of stopped moneylines settle as winners while
only 29.9% of stopped totals do, and the model-reversal exit gives back
~$222 on moneyline while saving ~$100 each on spread and totals. Two changes
carry that into execution:

### `reversal_confirmation_readings` is now a profile field

A line/stage profile can override the model-reversal confirmation window
(`LINE_EXECUTION_PROFILE_FIELDS`, integer 1–10, UI field "Model-reversal
confirmations"). The effective per-line value is captured in
`entry_policy_json` at entry like every other profile field, so a position
keeps its window for life. Payloads carrying per-line readings require a
server started from this code or newer; `tools/apply_line_profiles.py`
refuses to write them under a running older server.

### The offline profile apply tool

```bash
./.venv/Scripts/python.exe -m tools.apply_line_profiles --lane dry            # preview
./.venv/Scripts/python.exe -m tools.apply_line_profiles --lane dry --apply    # write
```

Writes the exact `_save_policy` shape (full normalized policy + fresh
control token) plus a `trading_policy_sessions` boundary
(`external_profile_apply`), changing only `line_execution_profiles`. With
the server stopped the policy loads on next start. With a server running,
the rotated token makes it adopt the save on its next cycle and defensively
disarm — the dry lane continues (its entries and simulated exits never
consult the arm latches), the live lane requires re-arming, and the tool
demands `--allow-running-server` before touching a listening port.
`--payload FILE` applies a custom profile list; `--strip-new-fields`
degrades the embedded recommendation for a running pre-2026-08-02 server.

Both lanes had the embedded recommendation applied on 2026-08-02 with the
server stopped: moneyline px 0.15–0.45 / max_edge 0.10 / stop 0.65 / target
0.40; spread unchanged band, stop 0.60 / target 0.30; totals early+middle
only, px 0.15–0.38 / min_edge 0.10 / target 0.30, late blocked. On
2026-08-03 (operator-directed) the ML and spread reversal windows were
raised to the maximum (readings 10, ~5 sustained adverse minutes): the Aug 2
audit showed confirmed 5-reading reversal sales still selling settlement
winners — 8 in that night alone, 4 of 7 tracked sales recovering to entry
within 30 minutes — so the wide stop owns disaster protection and the
reversal exit is near-vestigial on those lines. Totals keep readings 3;
reversal exits genuinely save money there. The standing cautions apply: a UI
save rebuilds profiles from the editor, and an advisor apply can overwrite
them — check profiles after either.

## Two-person lanes: Anthony and Alex (added 2026-08-04)

The primary live lane displays as **Anthony**; an optional second live lane
(**Alex**) lets a second person trade their own Polymarket account beside
it, with an isolated policy, book, journal, and arming latch. Enable with
`ENABLE_POLYMARKET_US_ALEX_LANE=true` (plus optional
`POLYMARKET_US_ALEX_KEY_ID`/`POLYMARKET_US_ALEX_SECRET_KEY`, or paste a
session key from the dashboard with the Alex lane selected — runtime keys
install into whichever live lane is active). Storage follows the standard
split: hosted runs use the `polymarket_us_alex` schema in `DATABASE_URL`;
workstation runs use `POLYMARKET_US_ALEX_TRADING_DB`
(default `polymarket-us-alex.db`). With the flag unset — the workstation
default — the server behaves exactly as a single-live-lane deployment and
the Alex button stays hidden.

## Settings derivation 2026-08-04 (post Aug-3 slate)

The lock readout graded 16 of 19 decay sales as settlement winners
(+$170.51 left), confirming the deeper decay direction. Live adopted only
CI-validated changes: ML floor 0.10 (10-15c graded +403.8%/$1, CI > 0) and
spread `min_source_agreement` 55 (sub-55 dogs' CI includes zero; 55-70 is
validated volume). The Unders ceiling stays 0.38 on live (38-45c CI
includes zero) while dry keeps measuring 0.45. Dry probes one regime step
further everywhere: ML floor 0.06, `min_edge` 0.01 (ML/spread) and 0.03
(totals), decay -0.50/-0.30/-0.35, agreement gate removed so the sub-55
control band keeps accruing.
