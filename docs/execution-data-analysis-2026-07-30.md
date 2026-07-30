# Execution-data and predictive-model audit

Snapshot date: 2026-07-30. Scope: the local live and dry-run execution
databases, Model Lab, retained quote history, and current execution-policy
code. This is retrospective research, not a claim that any setting will
produce future profit.

## Executive findings

1. The present evidence does not support one policy for every line. Across the
   priced, closed live and dry-run positions, moneylines were close to flat
   after cost while spreads and totals were materially negative.
2. Profit-protection exits worked much better than stop-loss exits in this
   sample. This does not prove that stops should be removed: the catastrophic
   tail still has to be bounded, and the policies selected the trades being
   observed.
3. Raw poll count greatly overstates sample size. More than 43,000 MLB
   moneyline observations represented only 42 games. All uncertainty and
   train/test decisions must therefore use whole events as their unit.
4. A material MLB label-integrity defect was found. Structured official MLB
   state used home/away team identifiers, but a later generic terminal packet
   could be used for settlement even when its unnamed score order was
   ambiguous. The resulting outcome calibration was nearly reversed.
5. Historic outcome labels from the ambiguous path are not safe training
   targets. They are now excluded from fitting; future outcome labels require
   a provenance-checked settlement source. The underlying price-movement,
   execution, and trade-path data remain usable for their respective targets.
6. The execution policy now supports opt-in line and MLB game-stage overlays.
   These change only execution thresholds. They do not change consensus,
   probability, edge, signal quality, calibration, or engine-gate results.

## Retained-data inventory

| Store | Approximate size | Material retained evidence |
|---|---:|---|
| Live execution | 34 MB | 188 positions, about 101,000 journal rows |
| Dry-run execution | 32 MB | 79 positions, about 31,800 journal rows |
| Sport Model Lab | 171 MB | 47,492 observations, 87,025 target rows |
| Decision ledger | 2.55 GB | about 30.6 million inserted row ids; about 2.42 GB reclaimable free pages |
| Quote history | 6.87 GB | about 7.22 million quotes, 37,852 states, 36 outcomes |

The decision and quote stores are useful for replay, but their row counts are
not independent statistical support. Disk compaction and retention work should
be scheduled while the server is stopped; it is separate from model validity.

## Realized execution results

### Live lane

- 188 positions retained.
- 171 priced closes: 61 wins and 110 losses.
- Realized net: -$25.48 on $236.75 cost basis.
- 17 external/manual closes had no verified local exit price and were excluded
  from after-cost P/L comparisons.

### Dry-run lane

- 79 priced closes: 38 wins, 39 losses, and 2 pushes.
- Realized net: -$41.60 on $793.12 simulated cost basis.
- Only nine independent games generated the dry-run closes. The large stake
  total therefore must not be mistaken for broad validation.

### Combined line-type comparison

| Line | Trades | W-L-P | Net | Cost basis | Turnover ROI | Independent events |
|---|---:|---:|---:|---:|---:|---:|
| Moneyline | 64 | 32-30-2 | -$4.42 | $302.92 | -1.46% | 26 |
| Spread / run line | 112 | 43-69-0 | -$38.47 | $448.89 | -8.57% | 29 |
| Total | 74 | 24-49-1 | -$24.18 | $278.06 | -8.70% | 27 |

The moneyline advantage over the other two line types is descriptive and
actionable for experimentation, but not yet a proven positive-return effect.
Many trades share events, and the policies that generated the data changed.

## Exit-path findings

| Exit family | Trades | Net | Interpretation |
|---|---:|---:|---|
| Meaningful-profit locks | 45 | +$36.60 | All positive in this selected sample |
| Trailing profit locks | 15 | +$8.49 | Positive after cost in this sample |
| Model reversal | 68 | +$3.33 | Roughly flat; outcome depends on line and context |
| Hard stop | 107 | -$73.81 | Dominant realized-loss source |
| Other confirmed/catastrophic stop families | small | about -$40 additional | Tail protection, not a profit center |

Forty-seven losing or push trades had first shown a positive executable mark.
Twelve trades across ten games reached at least +8% and still closed
non-positive. Their average peak return was about +14.5%, while the median
final return was about -18.2%.

That is evidence for a retained-profit floor and post-exit recovery tracking.
It is not evidence for removing stops. A counterfactual “it later recovered”
can be biased by long observation windows, settlement values, and the fact that
positions unable to recover disappear from the comparison at different times.

## Entry-context findings

### Price

- The 50-59 cent entry band returned about +7.1% across 16 trades and ten
  events.
- The 70-79 cent band returned about +7.1% across seven trades and six events.
- Other broad price bands were negative.

The favorable bands are too small to hard-code as universal truth. They are
appropriate candidates for later-event dry-run validation.

### Recorded edge

- The 12.5-15% source-edge band returned about +15.9% across 13 closes and 11
  events.
- The 10-12.5% band and the 15%+ band were negative.

Edge was not monotonic with profit. Very high displayed edge can indicate a
stale quote, disagreement, timing mismatch, or contract-mapping anomaly rather
than a free opportunity. The existing maximum-edge anomaly ceiling remains
important.

### Signal quality

Signal quality was not monotonic with realized return. This is expected:
quality is a data-reliability measure, not a win probability. It should remain
a reliability gate and should not be interpreted as “85% likely to win.”

### MLB game time

Only 104 of 267 retained positions had modern game-fraction metadata. Entries
with less than 35% of regulation remaining were less negative in aggregate than
middle-game entries, but support was small and confounded by line, price, and
policy version. Game-stage effects must be estimated within line type, not
pooled across every contract.

## MLB model-integrity finding

The Model Lab held 43,532 baseball moneyline observations across 42 events.
Only 31 events carried result labels and only 8,852 observations were
state-complete. The retained dates covered roughly four days.

Calibration by probability bin was nearly reversed: low predicted
probabilities frequently had positive labels and high probabilities frequently
had negative labels. Inspection showed that official MLB observations had
identified home/away state, while some settlements used a generic terminal
score packet with no reliable orientation.

Implemented correction:

- generic MLB score packets remain available to the live display, but an
  unnamed packet is not accepted as a trusted MLB model-settlement source;
- settlement prefers the structured official MLB linescore with team ids;
- every research outcome records its settlement source and details;
- candidate fitting and readiness count only trusted settlement sources;
- the target version was advanced, so old ambiguous outcome candidates cannot
  be selected as current evidence.

No historic outcome label was silently “flipped” or guessed. Existing
untrusted labels remain auditable but do not qualify for fitting.

## Predictive architecture direction

The honest next model is a hierarchy, not a larger bin sorter:

1. **Fixed market/engine baseline.** Keep the established consensus probability
   as a fixed offset.
2. **State residual.** Learn only the incremental MLB correction from score,
   inning/half, outs, bases, count, batting side, and interaction terms.
3. **Personnel and run environment.** Add versioned pitcher/batter handedness,
   starter/current pitcher strength, bullpen workload, confirmed lineup,
   park, and weather inputs only when their provenance is retained.
4. **Line-specific heads.** Moneyline outcome probability, run-line cover
   probability, and total-run distribution are distinct targets. They should
   share baseball state but must not share one undifferentiated response.
5. **Execution model.** Separately predict after-cost fill/exit behavior,
   adverse excursion, favorable excursion, and probability of reaching a
   protected profit before a stop.
6. **Online calibration in shadow.** Adapt a post-hoc calibration layer to
   drift only after trusted outcomes arrive. Online calibration must not
   silently replace or promote the base model.
7. **Policy evaluation.** Log the probability that the current policy selected
   each candidate. Without an explicit logging propensity or controlled
   exploration, rejected trades have no observed reward and alternative-policy
   ROI is not identifiable. Use whole-event chronological evaluation and
   doubly robust/off-policy methods only after that logging contract exists.

MLB's own win-expectancy definition uses score, inning, outs, runners, and run
environment. Hierarchical batter/pitcher work supports partial pooling when
individual matchup samples are sparse. Sports-betting research supports
selecting probability models by calibration rather than accuracy alone.
Online Platt scaling is a reasonable shadow recalibration candidate under
drift, and doubly robust off-policy evaluation is the appropriate direction
for comparing future execution policies from logged behavior.

## Policy-profile implementation

The saved policy accepts a versioned list of execution overlays:

- line: moneyline, spread, or total;
- stage: all, early (50%+ remaining), middle (25-50%), or late (under 25%);
- optional inherited overrides for edge bracket, quality, references, price
  bracket, spread, depth, hold, profit target, retained-profit floor, trailing
  pullback, stop, reversal edge, and minimum game remaining.

Precedence is global -> all-stage line profile -> exact line/stage profile.
Unknown game stage receives only the all-stage profile. The effective profile
is journaled and its complete effective policy is frozen into the position at
entry, so later edits cannot silently rewrite that position's exit rules.

## Recommended research sequence

1. Collect future trusted MLB settlements and keep the current outcome model in
   shadow until the calibration direction is sane.
2. Run moneyline, spread, and total analyses separately. Treat fewer than 20
   independent events per line/stage as sparse.
3. Validate the observed moneyline advantage and the 12.5-15% edge candidate
   on later whole games in dry run; do not apply them as live truths from the
   current sample.
4. Keep the retained-profit protection and shadow recovery audit. Compare stop
   variants by later-game after-cost P/L, drawdown, tail loss, and recovery—not
   rebound count alone.
5. Start logging all eligible candidate contexts plus selection probability,
   not only fills. This is required for defensible policy learning.
6. Reserve an untouched future test block after model and threshold choices
   are frozen.
7. Compact the ledger and history databases only during a planned server-off
   maintenance window, then add measured retention/aggregation tiers.

## Primary references

- MLB, Win Expectancy:
  <https://www.mlb.com/glossary/advanced-stats/win-expectancy>
- MLB, Game Strategy Explorer:
  <https://baseballsavant.mlb.com/game-strategy-explorer>
- Wheatcroft, model selection by calibration:
  <https://doi.org/10.1016/j.mlwa.2024.100539>
- Gupta and Ramdas, Online Platt Scaling with Calibeating:
  <https://proceedings.mlr.press/v202/gupta23c.html>
- Wang, Agarwal, and Dudik, adaptive off-policy evaluation:
  <https://proceedings.mlr.press/v70/wang17a.html>
- Mott et al., hierarchical batter/pitcher matchup models:
  <https://arxiv.org/abs/2511.17733>
- Brill, Yurko, and Wyner, clustered sports-play uncertainty:
  <https://arxiv.org/abs/2406.16171>
