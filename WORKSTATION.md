# Polymarket US Research Workstation

This repository can run as an isolated local copy of the PelosiTracker website.
`start-workstation.cmd` binds it to localhost, disables the separate paper-bot
subsystem, and uses workstation-specific SQLite paths.

The existing Python/Rust signal pipeline is retained unchanged. Polymarket US is
implemented as a separate execution sidecar: it consumes existing signals and
US executable prices without feeding them back into or silently altering the
established calculations.

## First run

Double-click `setup-workstation.cmd`. It creates `.venv`, installs the exact
dependencies, builds the Rust extension, and creates the ignored `.env` file.

Then double-click `start-workstation.cmd` and open:

<http://127.0.0.1:8775>

The local default login is `admin` / `admin`. The server binds only to
`127.0.0.1`, so it is not exposed to other devices.

For the existing `start.cmd`/port 8765 trading ledger, double-click
`start-dedicated-trader.cmd` instead. It starts that server in a visible command
window when necessary and opens `http://127.0.0.1:8765` as a standalone Chrome
app using an isolated profile under `%LOCALAPPDATA%\PelosiTracker`. It falls
back to Edge when Chrome is unavailable. Arc, normal Chrome profiles, cookies,
extensions, tabs, and history remain separate.

## Polymarket US API key

Public Polymarket US sports events and market prices work without a key.

When you are ready:

1. Open <https://polymarket.us/developer>.
2. Select **Create API Key**.
3. Save both values. The Secret Key is shown only once.
4. Stop the workstation with `Ctrl+C`.
5. Open `.env` in this folder and set:

   ```env
   POLYMARKET_US_KEY_ID=your-key-id
   POLYMARKET_US_SECRET_KEY=your-secret-key
   ```

6. Save `.env` and restart `start-workstation.cmd`.
7. Open the **Polymarket US Research** tab and select **Test account key**.

Do not paste either value into the website, frontend JavaScript, source files,
GitHub, screenshots, or chat. `.env` is excluded by `.gitignore`.

## Odds API polling interval

Discovery includes an **Every N seconds** control beside the Odds API master
switch. Enter a value from 5 to 3,600 seconds and select **Apply interval**.
The workstation persists the value locally and existing event pollers use it
without a server restart. `ODDS_POLL_SECONDS` remains only the initial default
when no saved dashboard value exists. The interval applies once per eligible
monitored event, so shorter intervals and more monitored events both increase
paid API usage.

## Automatic trading and safety boundary

The workstation has two isolated automation lanes: **Dry run** and **Live**.
Each lane has its own saved policy, scheduler, journal, positions, performance
history, adaptive-exit evidence, candidate cooldowns, and risk-session counters.
They read the same monitored events and unchanged model signals, but a dry-run
policy can no longer change or pause the live policy. Both lanes may run at the
same time. A restart always closes the live-order latch.

The live lane is stored in `POLYMARKET_US_TRADING_DB`; the dry-run lane is
stored in `POLYMARKET_US_DRY_RUN_DB`. Never configure both variables to the
same file. The lane selector in **Polymarket US Research → Automatic trade
controls** loads and saves only the selected lane. The two cards above the form
show whether each scheduler is running, whether live is armed, and its open
exposure.

Recommended rollout:

1. Select the **Dry-run lane**. Start with **Strict mode** or turn strict mode
   off and select **Use core gates**.
2. Enter the **Managed trading allocation** and choose Cautious, Balanced,
   Active, or Aggressive research. The named preset deterministically derives
   and versions the detailed limits. Choose Custom to edit those limits
   directly.
3. Select the full-game line types eligible for automatic entry: **Moneyline**,
   **Spread / run line**, and/or **Game total**. Disabled types remain visible
   and calculated elsewhere, but the execution sidecar rejects them before
   contract mapping or an order attempt. At least one type must remain selected.
4. Optionally add line/game-stage execution profiles. A line's all-stage
   profile inherits from the global controls; an early, middle, or late profile
   refines it when explicit MLB state is available. Profiles change only
   execution thresholds, are journaled, and are frozen into each new position.
5. Turn on automatic analysis, save the dry-run policy, and inspect that lane's
   journal for mappings, rejections, simulated fills, marks, and logical exits.
6. Only after reviewing that evidence, switch to the **Live lane**, configure
   and save its separate limits, choose 30 minutes to 4 hours, check the
   approval box, and arm the workstation. The dry-run lane keeps running with
   its own settings unless you stop it.
7. Use **Disarm** to close the live-order latch. **Stop automation now** affects
   only the lane currently selected; on the live lane it also requests
   cancellation of this workstation's managed open orders.
8. **Stop automation + wipe dry-run trades now** is an executive reset: it
   immediately invalidates an in-progress analysis cycle, turns automation off,
   and deletes every simulated position/history row without requesting a quote.
   Each open position also has its own control. Removing one dry-run simulation
   does not stop automation; selling one live position requires the live latch
   and a current previewed fill-or-kill exit.
9. **Start new risk session** resets the rolling live-entry count, rolling
   realized-loss entry stop, and candidate retry cooldown after an explicit
   confirmation. It preserves trades, P/L, positions, and the execution journal.
   Live execution must be disarmed first. Per-position stops, exposure, cash
   reserve, venue, liquidity, edge, quality, and engine safeguards remain active.

Protected actions use one short backend approval token: `approve`,
case-insensitively. The dashboard does not ask you to type it. Live arming and
bulk live selling use clearly labeled checkboxes; one-off reset, clear, apply,
and individual-sell actions use a browser confirmation dialog. Missing approval
still fails closed, and each dialog describes the specific consequence before
the shared token is sent.

The selected bounded latch authorizes new live entries and manual sell controls.
Arming also enables protective automatic exits for the life of the running
process so an already-managed position is not left unprotected merely because
the entry window expired. Saving policy, Disarm, Emergency stop, or restarting
always clears both permissions; the status strip shows them separately.

Live entries use previewed, fill-or-kill limit orders. The backend enforces
strict 5¢–95¢ hard bounds plus the narrower bracket you choose, positive
execution edge, an optional execution-edge ceiling, signal quality/reference
floors, spread/depth limits, cash reserve, exposure/concentration limits,
daily-loss stop, candidate cooldown, and order cadence. A separate per-event
hourly cap limits repeat-entry churn. The optional MLB game-stage floor requires
explicit inning state and blocks a new entry after too little regulation game
remains; `0%` disables it. Only one new entry is allowed per cycle.

The managed trading allocation is the outer boundary, independent of account
balance. A $10 allocation cannot expose the remaining account balance even if
the authenticated buying-power response is larger. Presets use only a fraction
of that allocation for simultaneous exposure and derive position, event, loss,
price, quality, depth, hold, and exit limits. They do not disable exact contract
identity, open-book, executable-price, buying-power, duplicate-position,
allocation, emergency-stop, or live-arm checks.

Automatic cash-out never triggers merely because a line increased by one cent.
Two consecutive executable readings at or above the configured profit target
arm a durable profit lock immediately, even before the fallback hold expires.
The hold is now a one-reading fallback rather than a barrier to recognizing an
early opportunity. A later edge decay or material pullback from the observed
high can trigger a sale as soon as the lock is confirmed. A configured hard stop
or material model reversal remains independent. Live marks and sell limits use
the authenticated order book across enough levels to cover the entire remaining
quantity, rather than assuming the top quote can fill every share.

The separate **minimum retained profit after protection arms** setting prevents
an ordinary trailing or edge-decay exit from chasing a quote through a formerly
green position and filling at a loss. Live cash-out status, return, and the
protected floor use the fee-adjusted executable value for the complete remaining
quantity. If a fill-or-kill profit exit misses and the next quote is below that
floor, the position remains open and the journal records
`profit_floor_missed_holding`; the next ordinary sell must recover above the
floor. The live limit order itself is also bounded at the fee-adjusted floor so
the quote cannot slip through it between preview and creation. This floor is not
a guarantee of profit and does not disable the configured hard stop or a
material model reversal, either of which may still realize a loss to bound risk.

**Adaptive MLB cash-out research** is an opt-in local overlay around that
unchanged probability and entry path. It records one bounded observation per
managed MLB moneyline, run line, or total and 30-second bucket. Inputs include
explicit inning/half, line-relative score state, which team is batting when
applicable, recent executable position-price movement, and the existing
execution edge. It forecasts whether an adverse 3% executable move will occur
before a favorable 3% move over the selected future horizon. It does not
recalculate or feed back into probability, edge, signal quality, entry gates,
price mapping, or sizing.

The response choices are:

- **Observe only**: retain and score forecasts while every exit threshold stays
  exactly as configured.
- **Guarded**: small tightening with a low cold-start influence.
- **Balanced**: moderate bounded tightening as retained support grows.
- **Responsive**: faster bounded tightening for a deliberately more reactive
  research run.

Only adverse-movement forecasts above 50% can tighten profit-exit behavior. The user-set
maximum caps the adjustment; the meaningful profit target can be lowered by at
most half that cap, while the trailing pullback and profit-lock edge-decay
threshold can tighten within their own hard bounds. The configured stop loss is
copied into every forecast record as `hard_stop_unchanged` and is never
re-estimated by the learner.

**Protect ordinary MLB stops with bounded state-aware confirmation** addresses
single-quote stop-outs separately from the learner. When enabled, an ordinary
stop breach in a live, state-ready MLB game must persist for the selected
number of readings and maximum grace period. A transient missing comparison
edge is treated as missing evidence, not an automatic model reversal, and gets
a shortened bounded grace. These safety exits remain immediate:

- the independently configured catastrophic boundary;
- an explicit material reversal in the existing execution edge;
- terminal game state;
- an Under total that the recorded score has already made impossible; and
- stale or unavailable MLB state, where the overlay cannot justify waiting.

If the recorded score has already cleared an Over total in the position's
favor but the quote is deeply adverse, the workstation records a state/price
conflict instead of automatically realizing the loss. This is a protection
against stale or mismapped data, not a claim that the venue has settled.

Every automatic exit starts a read-only **after-sale outcome audit** for the
configured horizon. The workstation keeps marking the exact sold contract
without placing a hypothetical order, and records whether it recovered to the
entry price, recovered half the sold loss, or continued lower. This is the
counterfactual evidence needed to determine whether stop confirmation actually
reduces premature exits rather than merely postponing losses.

Resolved labels are estimated by event rather than treating every poll from one
game as an independent game. Cold-start baseball-state priors shrink toward
event-balanced observed outcomes as distinct games accumulate. The panel shows
retained observations/events, labeled support, and the Brier score for the
movement forecast. Each managed position shows its last adverse-movement
forecast, state, support, tightening, stop-confirmation progress, and reason.
The after-sale audit shows recent entry/sale/best/worst prices and recovery
state. **Clear adaptive learning history** opens a browser confirmation dialog;
approving it deletes this local learner's movement and recovery observations
while preserving managed positions, trade history, the execution journal,
Model Lab data, and every core model artifact.

Managed rows show shares bought, per-share entry, total paid, remaining shares,
size-aware cash-out value, and marked/realized P/L. **Sync phone/account** reads
the authenticated portfolio without creating or changing an order. A lower
venue quantity must appear in two consecutive snapshots after a short
settlement grace before a phone/manual partial sale or close changes the local
managed row. Because the portfolio snapshot does not prove the outside fill
price, an external close is labeled as such and the workstation does not invent
realized P/L. Manual positions remain read-only and are never adopted for
automatic trading.

**Strict mode** requires the existing engine's final action to be `PAPER_BET`,
which means every engine entry gate cleared. In selective mode, only the engine
gate checkboxes you choose must pass; failed, unknown, duplicate, or missing
results for a selected gate fail closed. This changes no probability, edge,
quality, calibration, or gate calculation—it only changes which already-recorded
engine results the separate execution sidecar requires.

The selectable list does not disable the execution layer. Exact Polymarket US
contract mapping, authenticated open-book status, the hard 5¢–95¢ range, your
price/spread/depth limits, cash and exposure limits, and live arm/preview/
fee-adjusted-edge checks are always enforced. Live mode also retains the positive
source-edge requirement.

Money fields accept cents, and percentage/share/minute fields accept decimal
values. Counts such as maximum open positions, orders per hour, independent
references, and cycle seconds remain whole-number settings.

### Trade performance datasheet

The **Trade performance datasheet** reads every retained workstation-managed
position from both the canonical live store and the isolated dry-run store,
including cards previously cleared from the dashboard view. It can be
filtered by dry-run/live mode, moneyline/spread/total, execution result, event,
selection, or market slug. Its success definition is deliberately narrow: a
profitable close has positive realized after-cost P/L. Open positions do not
enter the success rate, and a phone/manual close without a provable fill price
is labeled unverified rather than assigned invented P/L.

Each row links to the immutable policy snapshot saved when that position was
entered. The settings comparison groups identical snapshots by execution mode
and line type, then reports W-L-P, success rate, realized net, settled stake,
and after-cost ROI. Older positions without an entry snapshot remain labeled
`settings unavailable`; the workstation never backfills them from today's
configuration. Use **Export filtered CSV** to download the current filter for
deeper analysis. Tally resets and clearing exited cards from view do not erase
this ledger.

Treat the group table as descriptive evidence, not a guaranteed optimizer.
Win rate, net, ROI, number of closed trades, and independent events need to be
read together because a small settings group can look excellent by chance.

## Sport Model Lab

The local **Sport Model Lab** records one bounded moneyline observation per
selection every 15 seconds. Each row keeps the existing engine probability,
market price, edge, signal quality, lineage, live score/clock/possession, recent
score swing, and price movement. A final event labels those observations for
later research.

Basketball, football, hockey, supported soccer leagues, and the bounded
baseball research phase can accumulate candidates. The local MLB adapter uses
the official schedule and compact linescore endpoints to add inning/half,
oriented score differential, batting side, outs, base occupancy, ball/strike
count, and current batter/pitcher identity. It does not invent lineup quality,
player-strength priors, bullpen availability, park, or weather state; those
remain named requirements for a richer candidate. Tennis remains
blocked until sets, game score, server, and match format are structured. The UI
shows missing inputs instead of inventing neutral sports state.

**Fit research candidate** runs an offline regularized logistic comparison with
an 80/20 chronological split by whole event and equal total weight per event.
This prevents observations from the same game leaking into both sides of the
test or one long game dominating the fit. The candidate is stored with a hash,
feature scaling, coefficients, sample counts, and Brier/log-loss comparisons.
It also runs an expanding-window walk-forward evaluation: every fold trains
only on earlier settled games, tests on later whole games, reports calibration
bins, and bootstraps its Brier improvement by settled event rather than by
repeated 15-second rows.
It is always marked research-only: the web process cannot install or promote
it, and it has no route into probability calculations or order authorization.

## Data and reproducibility

Local SQLite research data is ignored by Git. The canonical live execution
store is `polymarket-us-trading.db`; `workstation-data/polymarket-us-dry-run.db`
is the independent simulated-execution store. Both launchers use these same
paths so switching launchers cannot make retained live history appear missing.
The files contain execution policies, managed positions, bounded audit journals,
and adaptive-exit observations. They do not contain either API credential.
`workstation-data/model-lab.db` contains research observations and candidate
fit reports. Set `MODEL_LAB_DB` in `.env` only if you want a different local
path.

The Model Lab keeps learning targets explicitly separate. `event_outcome` is
the settled moneyline result. `market_probability_change_3m` is the first
monitored market-probability change observed 180–360 seconds later and is not
executable P/L. `after_cost_strategy_pnl` is created only when a closed managed
position's exact entry decision id matches a research observation. It stores
realized after-cost return and explicit selection-bias metadata; the lab will
not silently substitute an outcome or mid-price move for profit.

Use **Archive research snapshot** before a major experiment or cleanup. It
writes deterministic JSON Lines plus a SHA-256 manifest under
`workstation-data/research-exports/`. The archive contains observations,
explicit targets, and candidate artifacts, but no API credentials. It is
independent of the dashboard journal's retention cap and never affects the
calculation or execution engine.

## Synchronizing website and workstation evidence

The Model Lab's **Synchronize local and hosted research evidence** panel is
bidirectional. Export locally and merge on the website once to seed the durable
hosted PostgreSQL store. Later phone-started dry runs remain server-side after
the browser closes and are immediately included by the website's combined
datasheet and advisor. Export from the website and merge locally whenever you
want the workstation to include those newer remote sessions.

The archive preserves live versus dry-run lane identity and merges by immutable
evidence primary key, so repeated transfers are safe. It intentionally excludes
open positions, orders, API credentials, cookies, environment configuration,
and mutable execution controls. Never add the SQLite databases themselves to
Git.

## Reactive settings advisor

The local **Reactive settings advisor** creates an auditable execution-policy
comparison whenever you request one. Choose **Protect profit**, **Balanced**, or
**Seek more qualified trades**, enter a desired qualified-trade rate, and select
live, dry-run, or all retained history, a lookback window, and the line types to
compare.
The advisor:

- attaches the exact execution-policy snapshot, source edge, signal quality,
  reference count, executable edge, and settings-session id to every new
  managed position;
- evaluates realized after-cost P/L with a chronological whole-event split;
- estimates qualified opportunity frequency from deduplicated entry decisions;
- bootstraps ROI by event rather than treating repeated trades or polls as
  independent;
- compares lower and upper edge bounds, signal quality, entry price, line type,
  event-entry count, same-contract cooldown, and explicit MLB game stage;
- shows execution-mode-, line-, edge-, quality-, price-, repeat-entry-,
  game-stage-, line-by-game-stage-, and exit-reason diagnostics plus five
  leading candidate policies; and
- reports the fitted baseball model's shadow readiness and decision coverage.

All-data analysis explicitly inventories each retained lane and may pool
simulated and live outcomes for candidate discovery. Because those execution
domains differ, a combined result is always exploratory and cannot one-click
validate a live policy. Re-run a promising candidate against live-only history
before treating it as live execution evidence.
**Download analysis JSON** exports the complete scope, diagnostics, candidate
frontier, and validation evidence without changing a setting.

A recommendation is never applied automatically. **Apply suggested filters**
is enabled only after at least 40 complete trades across 20 independent events
also produce at least eight later-event trades across five events, positive
later-event after-cost ROI, no more than 35% stake concentration in one event,
at least 90% whole-event bootstrap probability of positive return, and a
positive whole-event 95% lower bound. Otherwise the settings remain a
diagnostic comparison that can be reviewed but not silently applied. A valid
apply opens a browser confirmation dialog, changes only the displayed execution
filters, switches the named risk style to Custom, and disarms live orders for
review. Allocation, exposure, cash reserve, position sizing, stop-loss,
probability, edge, signal quality, calibration, and engine-gate calculations
are not changed by the fitter.

Every recommendation is bound to a hash of the complete saved execution policy
that produced it. Editing or saving controls makes the displayed recommendation
stale, so the page disables its Apply button and asks for a fresh analysis. After
a successful apply, the page automatically analyzes the newly saved policy and
replaces the consumed recommendation with a new id. This permits repeated
analyze/apply iterations without resubmitting an already-used recommendation or
silently applying advice calculated from older settings.

“Seek more trades” cannot learn the profitability of trades that were never
placed. Its opportunity-rate estimate can lower bounded execution filters, but
the report always identifies this selection-bias limitation. Comparing many
candidate filters can still overfit despite the chronological split and
whole-event resampling, so the report states that limitation as well. No
objective or settings recommendation guarantees profit.

## Predictive-model path to live use

Fitted sport candidates currently run in shadow mode only. The workstation now
scores exact historical decision ids against the latest compatible frozen
candidate when available, but those probabilities cannot change a setting or
authorize an order.

For an MLB candidate to become eligible for a later reviewed live overlay, all
of these must be true:

1. At least 200 independently settled, state-complete events and 1,000 settled
   observations.
2. The candidate beats the unchanged engine baseline over chronological
   walk-forward events, with the whole-event 95% Brier-improvement interval
   above zero.
3. Official base/out/count state is captured across enough events, and
   versioned personnel-quality priors are added. Current batter/pitcher IDs are
   captured, but IDs alone are not a quality model.
4. Research observations are linked exactly to authenticated entry and exit
   fills so after-cost P/L can be evaluated without substituting a mid-price or
   game result.
5. A frozen, versioned artifact passes calibration and leakage review, then a
   dry-run/shadow rollout, before a separately approved limited-live stage.

Until every requirement clears, the dashboard reports the blocker and the
fitted model remains shadow evidence with `engine impact: none`.
The source repository includes the calculation and model-training code plus the
research bibliography. It does not include production database history, paper
PDFs, or separately supplied calibration/model artifacts. Add reviewed artifact
paths to `.env` only when you have those exact files.

### Local storage diagnostics

The engine evaluates every live update, but workstation disk telemetry is
sampled separately: at most one snapshot per selection every 5 seconds and an
unchanged-price heartbeat every 15 seconds. Decision diagnostics use the same
limits while actual entries and closing-line marks remain immediate.

For a read-only size/row audit while the workstation is stopped or running:

```bat
.venv\Scripts\python.exe tools\audit_local_databases.py --details
```

SQLite does not return deleted pages to Windows automatically. After stopping
the workstation and archiving research, inspect reclaimable space:

```bat
.venv\Scripts\python.exe tools\compact_local_databases.py
```

Compaction never deletes rows, but it rewrites each file and requires temporary
free space. Run its explicit `--apply --confirm COMPACT_LOCAL_DATABASES` form
only while the workstation is stopped. The tool refuses a busy database or
insufficient disk space.

The execution journal and Model Lab are training/evaluation data, not automatic
production retraining. Updating a predictive model still requires explicit
review, stronger chronological out-of-sample validation, calibration, versioned
artifacts, and a separate installation step so the live engine is never silently
contaminated by leakage or a handful of recent outcomes.
