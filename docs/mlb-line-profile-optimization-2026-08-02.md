# MLB line profile optimization — 2026-08-02

Profit-optimizing `line_execution_profiles` for the three MLB lines, derived
from settlement grading of the full closed record on both lanes. Companion to
`line-settings-analysis-2026-08-01.md`; supersedes its per-line band advice.

## Method

**Population.** Every closed full-game position on both lanes, graded against
the official final score with the application's own settlement function
(`tools/settlement_counterfactual.py` machinery). 1,030 graded positions across
72 events — 278 live, 752 dry.

**Pooling across exit regimes is deliberate and sound.** A position's
settlement grade depends only on its entry (price, line, stage, edge) and the
final score — never on how it was exited. So entry-quality questions can pool
every stop_loss/reversal regime, which is what lifts the sample from the 56
current-regime live positions to 1,030. Only exit-policy questions are filtered
by regime.

**Selection bias is negligible.** 769/772 dry and 293/324 live positions are
closed; only 6 events lack a settled outcome. Positions do not survive
unclosed, so the graded set is essentially the full entry record.

**Weighting.** Reported per-contract returns are **equal-weighted**. The dry
lane sizes positions up to $25 against live's $2, so stake-weighting lets a
handful of large dry positions dominate — that artifact alone flipped the
spread line's apparent sign. All intervals are **event-block bootstraps**
(resample whole events), which preserves the within-game correlation that
per-position resampling destroys.

## Findings

### 1. The leak is still the exit, and it is now line-specific

Realized return is negative on every line while settlement return is strongly
positive — the entry model beats the market and the exits hand it back.
What is new is that the stop's *value* differs sharply by line:

| line | stopped positions | settlement-win rate | giveback |
|---|---|---|---|
| moneyline | 80 | **67.5%** | +$706 |
| spread | 174 | 45.4% | +$624 |
| total | 107 | **29.9%** | +$168 |

Two thirds of stopped moneylines go on to win; barely a quarter of stopped
totals do — and totals' stopped population settles *negative* overall. **The
stop is destroying value on moneyline and doing its job on totals.** One
lane-wide `stop_loss` cannot serve both.

Model-reversal exits split the same way: on moneyline they give back $223, on
spread and totals they *save* $100 and $103 respectively.

### 2. Moneyline is the best line, and it has a hard price cliff

Equal-weighted settlement return, pooled: **+87.1%**, CI95 [+51.9, +121.7],
p(>0) = 1.00. By entry price:

| price | n | settlement return |
|---|---|---|
| < 0.25 | 46 | **+293%** CI [+138, +436] |
| 0.25–0.30 | 31 | +166% |
| 0.30–0.35 | 19 | +81% |
| 0.35–0.40 | 27 | +108% |
| 0.40–0.45 | 28 | +68% |
| 0.45–0.50 | 27 | −5% (p+ = 0.39) |
| ≥ 0.50 | 79 | **−32%** CI [−53, −10], p+ = 0.00 |

The edge does not decay across 0.45 — it falls off a cliff. The live band's
0.45 ceiling is exactly right. Its **0.23 floor is cutting off the richest
pocket the model has.**

A large model edge is a *red flag* on moneyline, not a green one: edge
0.03–0.08 returns +120%, 0.08–0.12 returns +37%, 0.12–0.20 returns −2%. When
the model disagrees violently with an efficient market on a moneyline, the
market is usually right.

### 3. Totals are only tradeable at high conviction and low price

As currently gated (price 0.23–0.45, min_edge 0.04, early only) totals return
+42.7%, CI [−20.9, +83.9] — indistinguishable from zero. The edge is entirely
in the high-conviction, cheap tail, and the relationship is **monotone**, which
is what distinguishes it from a cherry-picked peak:

| min_edge | n | settlement return |
|---|---|---|
| ≥0.04 | 160 | +28.6% |
| ≥0.06 | 114 | +50.6% |
| ≥0.08 | 80 | +57.1% |
| ≥0.10 | 51 | +95.4% |
| ≥0.12 | 42 | +117.4% |
| ≥0.15 | 29 | +163.5% |

Price runs the other way: totals are positive below 0.35, flat at 0.35–0.40
(+3.5%, p+ = 0.57) and negative above 0.40.

Late-stage totals remain the one clearly toxic pocket: **−41.4%**, CI
[−70.1, −6.5], p+ = 0.01. The existing stage guard is confirmed, and
**middle-stage totals are positive** (+34.6%, p+ = 0.94) and should be opened.

### 4. Spread is the largest book and the weakest edge

+43.7% pooled, CI [+16.6, +70.5]. But on the Aug 1–2 holdout it simulates to
**−8.3%** under current settings and only **+5.7%** under the proposal. Every
stage is positive, no price band is clearly toxic, and no gate separates
winners cleanly. It is not a pocket to widen — it is a pocket to keep small.
Widening the spread price floor to 0.15 *hurt* in ablation (+72.0% → +64.1%),
so the operator's existing 0.23 floor stays.

### 5. Profit targets and stops, simulated per line

Simulating target/stop against each position's tracked high/low marks
(pessimistic when both are touched):

- **Moneyline** improves monotonically as the stop loosens (+99% at 0.30 →
  +135% at 0.65 → +147% at 0.75) and prefers a high target. It wants to be
  left alone.
- **Spread** also prefers a loose stop, and is the one line where a profit
  target genuinely beats holding, peaking near 0.30.
- **Totals** barely respond to the stop once the entry gates are fixed — which
  is the coherent reading of finding 1: the tight stop was compensating for bad
  totals entries. Fix them at entry and the stop stops mattering. Left at the
  lane's 0.50 rather than making a strong claim on n=15.

## The profile set

Lane globals unchanged. All values are profile overrides.

| line / stage | auth | price band | min_edge | max_edge | stop_loss | profit_target | max_position |
|---|---|---|---|---|---|---|---|
| moneyline/all | on | 0.15–0.45 | 0.03 | **0.10** | **0.65** | **0.40** | lane ($2.00) |
| spread/all | on | 0.23–0.45 | 0.03 | lane | **0.60** | **0.30** | $1.50 |
| spread/late | on | (inherits) | | | | | |
| total/all | **off** | — | — | — | — | — | — |
| total/early | on | **0.15–0.38** | **0.10** | lane | lane (0.50) | **0.30** | $1.00 |
| total/middle | **on** (was off) | **0.15–0.38** | **0.10** | lane | lane (0.50) | **0.30** | $1.00 |
| total/late | **off** | — | — | — | — | — | — |

Changes from the config in the database as of this writing:

- moneyline: floor 0.23 → 0.15; `max_edge` 0.25 → 0.10; `stop_loss` 0.50 →
  0.65; `profit_target` 0.30 → 0.40.
- spread: `stop_loss` 0.50 → 0.60; `profit_target` 0.20 → 0.30. Band and size
  cap untouched.
- totals: `min_edge` 0.04 → 0.10; ceiling 0.45 → 0.38; floor 0.23 → 0.15;
  `profit_target` 0.20 → 0.30; **middle stage enabled**; the redundant
  `max_edge`/`max_entries_per_event_per_hour` overrides dropped (the latter was
  set to 25 against a lane cap of 15, so it never bound).

## Validation

Settings were chosen using data through Jul 31 and scored on Aug 1–2, which was
never used to pick them. Per $1 staked, target/stop simulated, pessimistic:

| | current profiles | proposed profiles |
|---|---|---|
| full pooled record | +41.2% [+21.5, +62.6] | **+72.0%** [+45.4, +98.2] |
| **Aug 1–2 holdout** | +18.7% [+0.5, +39.0] | **+53.0%** [+25.7, +85.9] |
| live lane only | +39.1% [+10.1, +66.0] | **+63.3%** [+30.4, +96.4] |

Per line on the holdout: moneyline +81% → **+150%**, totals +36% → **+101%**,
spread −8% → **+6%**. Position count is preserved (196 → 207 pooled), so the
gain is not simply from trading less.

## What this does not establish

- **The counterfactual prices no drawdown and no capital constraint.** A looser
  moneyline stop raises variance; with a $10 daily-loss stop on a $25
  allocation, a bad night can halt the lane on positions that would have
  recovered. The stop widening is the one change with a real downside, and it
  is the one to stage rather than adopt wholesale.
- **The stop leg of the simulation binds on only half the record.**
  `lowest_exit_value` arrived in migration v15, so 509 of 1,030 positions carry
  it; the rest fall through to settlement and make the simulation read
  optimistically where the stop would have fired.
- **Totals rest on 15–25 admitted positions.** The monotone edge relationship
  is the evidence, not the point estimate. `min_edge` 0.10 is the recommendation
  over 0.12 because it holds more volume at nearly identical simulated return
  (+63.3% vs +60.0% on the live lane).
- **The 3-vs-5 reversal-readings A/B is still undecided.** Only 24 graded
  reversal exits carry an explicit readings value, all at 3, and no graded exit
  yet carries 5. Tonight's slate is the first real sample; do not conclude on it
  before the Aug 3 grading.
- ~~**`reversal_confirmation_readings` is not a profile field.**~~ Closed the
  same evening: it is now in `LINE_EXECUTION_PROFILE_FIELDS` (integer 1–10,
  UI field "Model-reversal confirmations"), and the applied profiles carry
  moneyline 5 / spread 3 / totals 3.
- Note the coupling: a profile's `min_edge` also sets its reversal trigger
  (`threshold = -max(0.03, min_edge)`), so raising totals' `min_edge` to 0.10
  also makes totals hold longer through reversals — a mild negative on that line,
  swamped by the entry-side gain.

## Applying

`PUT /api/polymarket-us/trading/config?lane=live` with a
`line_execution_profiles` list, the equivalent fields in the UI, or offline via
`tools/apply_line_profiles.py` (preview default, `--apply` writes, embedded
recommended payloads per lane). The payload validates against `TradingPolicy`
and resolves all twelve line/stage combinations as tabled above.

**Applied 2026-08-02, late evening, server stopped:** both lanes carry the
recommended profiles above plus per-line reversal readings (ML 5, spread 3,
totals 3); they load on the next server start. **Revised 2026-08-03,
operator-directed:** ML and spread reversal windows raised to the maximum
(10 readings) after the same-night audit showed confirmed 5-reading
reversal sales still selling settlement winners — 8 on the Aug 2 slate,
+$29.10 of forgone settlement value, 4 of 7 tracked sales recovering to
entry within 30 minutes. Totals keep 3. The live lane's own save at
autopilot end-of-slate had already set `automation_enabled` false, so the
morning start remains: review settings → enable → **Arm last**. One deliberate
scope note: the dry lane's ML profile pins readings 5, which ends the lane-wide
3-vs-5 A/B for future ML entries — the Aug 3 morning readout still grades
tonight's untouched sample, and per-line telemetry supersedes the lane-wide
experiment from here.

Standing ritual applies: **every policy save disarms the live lane** — settings,
Save once, Arm last. Exit policy is captured at entry
(`_position_execution_policy` reads `entry_policy_json`), so a save changes new
entries only and never re-prices an open position.

## Correction — 2026-08-03 (full-path replay)

**The target/stop simulation columns above are inflated and must not be
re-used.** They read each position's tracked `highest/lowest_exit_value`,
which stop at the moment the position actually exited — an early exit
truncates the path, so the simulation never saw the adverse excursion that
followed. Joining each exit's 30-minute post-exit window
(`exit_recovery_observations.worst/best_exit_value`) into the extremes and
replaying Aug 1–2 under the applied profiles:

| basis | live (52 admitted) | dry (95 admitted) |
|---|---|---|
| actual as-run | −$6.88 | −$82.09 |
| applied profiles, truncated paths (old method) | +$44.35 | +$182.85 |
| applied profiles, full paths (corrected) | **−$10.70** | **−$14.36** |
| stops off on ML/spread, targets kept | +$45.60 | +$208.57 |
| pure hold-to-settlement (exact, no path data) | **+$66.21** | **+$351.60** |

31 of 52 live admitted positions crossed even the widened stop lines
(0.60/0.65) somewhere on their full observed path. Separately, disabling
the reversal exit *as those nights were configured* (0.50 stops) made both
lanes worse (live −$14.34 → −$22.85, dry −$268 → −$481): with a 0.50 stop
armed, the stop caught the continuation deeper than the reversal had sold.

**What survives:** every settlement-graded claim (entry gates, line
verdicts, the totals stage/edge pocket, totals' stop earning its keep) —
settlement grading never used marks. **What falls:** the simulated exit
magnitudes, including the stop-widening gains taken at face value. On
ML/spread, every price-triggered exit tested — reversal at any window,
stops at any width — converts in-play volatility into realized loss; the
measured edge is a settlement edge.

**Direction (2026-08-03):** the dry lane now runs the stops-off test — ML
and spread `stop_loss` 0.95 (vestigial; max loss = the $1–2 stake), targets
kept, totals unchanged at 0.50. Live remains at 0.65/0.60 pending the
operator's call; the same payload applies it in one command.

## Addendum — 2026-08-03: the totals model is really an Unders model

Splitting the pooled totals record (297 positions, 65 events) by selection
side, settlement basis:

| side | n | settlement win | per $1 |
|---|---|---|---|
| Over | 155 | 16.8% | **−57.6%** |
| Under | 142 | 64.8% | **+85.8%** |

Overs lose in every stage, edge bucket, price band, and line value measured
(cheap Overs went 0-for-18; Over/late −85%). Unders win in every one of the
same cuts (Under/late +78% — the "toxic late totals" pocket was Overs all
along; the stage guard was a proxy). Inside the admitted pocket the split
holds: Unders +157.8%, Overs −43.3%.

**Change:** new lane policy field `allowed_total_sides` (default
`over,under`; UI fieldset "Game-total sides eligible for automatic entry";
gate applies in `_map_signal` before line profiles; candidate rejections
read "over totals are disabled by the totals side policy"). Both lanes are
set to **Under-only**. The dry lane additionally runs the widened-Unders
experiment: totals `min_edge` 0.10 → 0.06 (Unders graded +69–74% even at
0.03–0.10 edge) and `profit_target` 0.30 → 0.60 (admitted Unders average
~0.30 entry and pay +150%+ settled; a 0.30 clip was the pocket's biggest
giveaway). Totals guards (stop 0.50, readings 3) stay — they measure
protective on this line.

The field requires a server started from this code; an older runtime
ignores it and its next save drops it. Restart before arming.

## Addendum — 2026-08-03 slate: the catastrophic path was selling book gaps

The first guards-off slate promoted the last un-audited exit rule into the
light. Four live positions exited `catastrophic_stop_loss`; the 30-minute
post-exit tracker shows **all four recovered to entry price**, and three of
the four *filled 15–35c above the quote that triggered them* — the
fill-or-kill executed after the book had already refilled. Dry showed the
same at size: an Under 7.5 sold at 2c traded to 63c (+$27 forgone), another
at 1c to 45c.

The rule fires when `return_fraction <= -min(0.95, stop_loss ×
catastrophic_stop_multiplier)`. The 0.95 cap means **a 0.95 stop_loss does
not disable it** — guards-off left it armed — and it exits `immediate`,
deliberately skipping the confirmation window every other exit now uses.
That combination is precisely what a one-tick liquidity gap needs to become
a realized loss.

The code already refused the symmetric mistake: `settled_in_favor` holds a
low quote that conflicts with a *favorable* score. There was no mirror for
the adverse direction. There is now:

- **State-aware path**: the price-only catastrophic exit requires live game
  state to corroborate the collapse (`terminal` or `structurally_lost`). If
  the game says the position is still reachable, the quote is marked
  `catastrophic_price_unconfirmed` and falls through to the ordinary bounded
  confirmation window. A genuinely lost position still exits immediately —
  that branch is unchanged.
- **Stateless path**: with no witness but the price, the boundary must
  survive one further reading before selling.

Live cost of the mispriced exits on the night was only $0.31 (small totals
stakes), but the mechanism scales with position size, which is why it was
fixed the same evening rather than queued.
