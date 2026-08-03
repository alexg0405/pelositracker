# Predictive direction and third-party model review

Date: 2026-07-31. Scope: baseball. This is a research direction document, not a
promotion path. Nothing here changes the consensus, edge, or engine gates.

## Answered: the edge is real on two lines, and the exits give it back

The open question below - illusory edge, or edge given back by early exits - is
now measured. 171 closed full-game positions were graded against the settled
score using the application's own settlement function, over 35 independent
events.

**Realised win rate against the price paid.** The entry price is the market's
implied probability, so winning more often than that is genuine edge:

| Line | n | Avg price | Win rate | Gap | Events |
|---|---:|---:|---:|---:|---:|
| Moneyline | 37 | 43.9c | 62.2% | **+18.2pp** | 16 |
| Spread | 68 | 35.4c | 54.4% | **+19.0pp** | 24 |
| Total | 66 | 38.8c | 31.8% | **-6.9pp** | 25 |
| All | 171 | 38.5c | 47.4% | +8.8pp | 35 |

**Realised return against a hold-to-settlement counterfactual**, stake-weighted,
charging the entry fee and no exit fee:

| Line | Actual | Settlement | Stake |
|---|---:|---:|---:|
| Moneyline | -6.5% | **+76.0%** | $59.81 |
| Spread | -16.3% | **+78.6%** | $103.35 |
| Total | -9.2% | -16.9% | $81.97 |
| **All** | **-11.5%** | **+46.0%** | $245.13 |

Settlement minus actual is **+57.6%** stake-weighted, with an event-block 95%
interval of **+22.2% to +88.7%**, positive in 100% of 2,000 event resamples.

So: **moneyline and spread selection is genuinely predictive, and the exit
policy is destroying it. Totals are negative on merit, and there early exit
actually reduced the loss.**

This also retracts the "edge realization -17%" figure from the earlier
correction. That was a mean of per-position ratios, which a cheap winner
returning +200% and a loser returning -100% makes meaningless. Stake-weighted is
the honest measure and it points the other way.

### What this does not license

- **Do not simply disable exits.** The counterfactual assumes unlimited capital;
  holding to settlement ties up an allocation that early exits recycle, so the
  same bankroll could not have taken all 171 positions.
- **Variance changes completely.** A 47% win rate with binary settlement means
  long losing runs and far deeper drawdown than a managed -11.5%. Stops bound a
  tail this measurement does not price.
- **Selection bias remains.** This says these trades should have been held. It
  says nothing about trades the policy declined.
- **35 events is small**, and the per-line splits rest on 16, 24 and 25 events.

### What it does support

1. **Stop trading totals.** -6.9pp against the price over 25 events, and worse
   at settlement than under the current exits. No calibration layer repairs a
   selection that is wrong.
2. **Loosen profit-taking on moneyline and spread, in dry run first.** The
   thresholds are demonstrably too tight: the positions were right and were sold
   anyway.
### It is one rule, not the exit policy as a whole

Splitting moneyline and spread by exit reason isolates it completely:

| Exit reason | n | Actual | Settlement | Given up |
|---|---:|---:|---:|---:|
| **hard_stop_loss** | 57 | -$27.55 | +$128.80 | **$156.35** |
| model_reversal | 24 | +$2.17 | +$1.33 | -$0.85 |
| catastrophic_stop_loss | 1 | -$1.93 | -$3.12 | -$1.19 |
| trailing_profit_lock | 4 | -$0.26 | -$2.59 | -$2.33 |
| profit_lock_after_edge_decay | 19 | +$6.85 | +$2.32 | -$4.52 |

Every profit-side rule was right or neutral. The entire giveback is the hard
stop, and the reason is visible in its outcomes:

**Of the 57 positions the hard stop closed, 40 (70%) would have won at
settlement.** The median configured stop was 25%.

A stop firing on positions that go on to win 70% of the time is not bounding a
tail. On a 35c contract a 25% stop triggers on a ~9c move, which is ordinary
noise in a live baseball market - one run in the fourth inning. It converts a
temporary adverse excursion into a permanent loss, which is precisely the
failure mode the retained recovery evidence was built to detect.

### The responsible next step

This is **not** licence to widen or remove the live stop. Removing it exposes
the 30% that do lose to full downside, and the counterfactual prices no
drawdown. The plan's own warning applies: do not remove stops because some
stopped positions recovered; weigh drawdown, tail loss and recovery together.

What the evidence does support, in order:

1. **Stop trading totals.** Negative against price and worse at settlement.
2. **Test a wider or confirmation-gated stop in dry run** on moneyline and
   spread. The state-aware stop guard already exists for exactly this; the
   measurement now says it should be exercised rather than left off.
3. **Measure drawdown, not just return**, on any candidate stop before it goes
   near the live lane.

## Correction to the fee reading below (measured after it was written)

The fee decomposition further down is arithmetically right but was over-read.
Testing the fee-aware gate against the 153 closed trades that recorded an entry
edge shows it would have removed **zero** of them at any margin up to 2x. The
entries already cleared the fee floor comfortably:

| | Median |
|---|---:|
| Recorded entry execution edge | **10.3c** |
| Round-trip fee floor at that price | 2.3c |
| Ratio | **4.5x** |

So fees were never what disqualified these trades. The harder number:

| | |
|---|---:|
| Implied return if the recorded edge were real | **+29.8%** of stake |
| Actual realized return | **-5.0%** of stake |
| Edge realization rate | **-17%** |

A median 10.3c edge produced a 5% loss. **The displayed edge is not, on this
sample, a predictor of profit.** That is the finding that should drive the
roadmap, not the fee arithmetic.

Two explanations are consistent with it and they are not separated by this
measurement:

1. The consensus-versus-Polymarket-US gap is largely not information - stale
   quotes, cross-venue pricing differences, or mapping mismatch - so the edge
   is illusory.
2. The edge is partly real at settlement, but the exit policy converts a
   settlement expectation into a path-dependent outcome and gives it back.

The implied +29.8% assumes holding to settlement; the realized -5.0% comes from
early managed exits, so the comparison is not like-for-like. Distinguishing
these two is now the highest-value measurement available, and the retained
excursion extremes plus a hold-to-settlement counterfactual can do it without
new data.

This does not retract the fee analysis. Fees are still 6.00% of stake and still
argue for fewer round trips and cheaper prices. It retracts the inference that
fees are *why* these trades lost.

## The fee decomposition

Decomposing the 213 priced closes in the live store against the published
Polymarket US taker fee (`0.05 x shares x p x (1-p)`, charged both directions):

| | Amount |
|---|---:|
| Realized net | **-$28.21** |
| Estimated fees paid | **$18.27** (6.00% of stake) |
| Net **before** fees | **-$9.94** |

By line:

| Line | Trades | Fees | Net | Net before fees | Stake |
|---|---:|---:|---:|---:|---:|
| Moneyline | 46 | $4.19 | -$2.14 | **+$2.05** | $72.88 |
| Spread | 87 | $8.28 | -$19.30 | -$11.02 | $134.58 |
| Total | 80 | $5.80 | -$6.77 | -$0.97 | $97.12 |

Roughly two thirds of the realized loss is transaction cost. Moneyline was
**positive before fees** and totals were near flat; only spreads lost on merit.
The July 2026 audit's line ranking survives, but its cause changes: moneyline
and totals are not weak signals, they are signals too small to clear the toll.

That reorders every priority below. A better probability model that does not
also reduce the toll is fighting for the smaller share of the gap.

### The fee has an exploitable shape

Because the fee is `0.05 p (1-p)` per side, per unit of stake it is:

```
round-trip fee / stake        = 0.1 * (1 - p)
break-even edge (prob. units) = 0.1 * p * (1 - p)
```

Two consequences the current policy does not encode:

1. **Cheap contracts are structurally expensive.** A 20c entry pays 8.0% of
   stake in round-trip fees; an 80c entry pays 2.0%. The entry price band is
   therefore a cost decision, not only a risk decision.
2. **Required edge is price-dependent and peaks at 50c.** A flat `min_edge`
   is too strict at the extremes and too loose in the middle, which is exactly
   where most contracts trade. Replacing it with `edge >= k * p * (1 - p)`,
   `k > 0.1`, prices the gate correctly at every point on the curve.

A third lever is structural: **an exit pays a second fee, settlement does not.**
Holding a position to resolution halves the burden to `0.05 * (1 - p)`. That is
not free money - it converts a managed exit into full downside variance - but
the current policy pays the exit toll on every position without that trade-off
ever being priced.

## Third-party data and models worth pulling

Nothing below replaces the consensus. The architecture that fits this codebase
is unchanged: consensus stays a fixed offset, and anything new enters as a
residual, a calibration layer, or a feature source.

| Source | License | What it gives | Fit here |
|---|---|---|---|
| [pybaseball](https://github.com/jldbc/pybaseball) | MIT, ~1.7k stars, actively maintained | Statcast, FanGraphs, Baseball Reference, Retrosheet, Chadwick, Lahman | **Best single addition.** Offline feature building only - it scrapes, so it must never sit in the live request path |
| Retrosheet (via pybaseball/Chadwick) | Free to reuse, including commercially | Historical event-level play-by-play | Fits the base/out state model directly; supplies the volume the live lab lacks |
| Chadwick Bureau register | Open | Cross-source player identity | Required before any pitcher/batter feature is trustworthy |
| MLB StatsAPI | MLB terms | Live inning/score/base/out/count, batter, pitcher | **Already integrated.** The gap is depth, not access |
| [baseball_game_simulator](https://github.com/dgrifka/baseball_game_simulator) | Check before use | Batted-ball outcome model over ~300k Statcast events, 5-class | Reference architecture for a run-expectancy head; do not import wholesale |

Deliberately excluded: the several MLB "game winner predictor" repositories.
They are pre-game classifiers trained on season aggregates, validated on random
splits, and benchmarked against nothing. This workstation's benchmark is a
66-source live consensus, which is a far harder target than the coin-flip
baseline those projects beat.

## The confidence question: Venn-Abers, not a better point estimate

The request was to be *more confident on some lines and less on others*. That is
not a point-estimate problem, and improving the probability will not answer it.
It is an interval problem.

[Venn-Abers predictors](https://arxiv.org/html/2502.05676) are the right tool:

- They wrap **any** scoring classifier as a post-hoc layer, so the consensus
  stays the fixed baseline and is not refitted.
- They return a **probability interval** rather than a point. The interval
  *width* is the per-contract confidence measure being asked for.
- They carry distribution-free finite-sample validity, which matters far more
  than asymptotic guarantees at 5 to 30 independent events.
- **Venn multicalibration** extends validity to subpopulations - here, line
  type and game stage - which is precisely "confident on moneyline, not on
  spreads", derived rather than asserted.

The operational payoff is concrete: size on interval width, and refuse when the
interval spans the fee-implied break-even edge. A contract whose calibrated
probability interval cannot be distinguished from its break-even point is not a
trade, regardless of the point estimate.

This composes with what already exists. Beta calibration is already in the
codebase, online Platt scaling is already the cited drift candidate, and the
advisor already reports evidence by basis. Venn-Abers adds the missing piece:
per-contract uncertainty rather than per-policy uncertainty.

## Ranked directions

Reordered after the edge-realization measurement above.

**1. Separate "the edge is illusory" from "the exit gives it back."** A median
10.3c edge returned -5.0%. Until that is explained, every other change is
guesswork. The measurement needs no new data: compare each closed position's
realized return against a hold-to-settlement counterfactual, using the retained
excursion extremes and the settled outcome. If settlement returns are positive,
the exit policy is the problem; if they are not, the edge is.

**2. Venn-Abers calibration over the existing consensus, per line and stage.**
This is the direct answer to an edge that does not predict. An interval that
spans break-even is a refusal, and per-subpopulation validity is what turns
"trust moneyline more than spreads" into a measured statement. Fit offline on
Retrosheet plus retained trusted settlements; run in shadow.

**3. Fee-aware entry gate** (`fee_edge_margin`, implemented, default off).
It removed none of the historical sample, so it is a guardrail against a future
regime of thin edges at mid prices rather than a fix for the measured loss.
Enabling it costs nothing; expecting it to change the result would be wrong.

**4. Retrosheet-backed base/out run expectancy.** The live lab has 5 fit-ready
events. Retrosheet has decades. Fit offline, validate on later complete games,
and use the live lab only to confirm the offline model transfers.

**5. Stop trading spreads until something changes.** They lost $11.02 before
fees across 87 trades. That is not a toll problem, and no calibration layer
repairs a signal that is wrong.

## What will not help

- **A larger model on the same 5 fit-ready events.** The constraint is trusted
  settlements, not model capacity.
- **Importing a pre-game win-probability model.** The consensus already prices
  the pre-game view better than any public model; the edge, if any, is in live
  state and execution.
- **Chasing accuracy.** Wheatcroft's result already cited in
  `research-references.md` is that calibration, not accuracy, selects
  profitable sports models. Interval validity is the extension of that.
