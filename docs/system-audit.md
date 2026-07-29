# Local predictive-system audit

Audit date: 2026-07-28. Scope: the local workstation code and local SQLite
stores. This is a reproducibility and model-risk review, not a profitability
claim.

## Material findings

### Critical model findings

- The existing Sport Model Lab was shadow-only and had just 9 settled MLB events
  at inspection time. Thousands of repeated observations do not replace
  independent games. No fitted candidate was eligible to influence an order.
- The observed managed-position sample contained 99 closed positions, with 33
  positive and 66 negative realized outcomes. A raw success rate is not a
  sufficient objective, but this sample also had negative aggregate after-cost
  P/L and does not support relaxing the policy.
- The model had score and inning but lacked the official base/out/count state
  used by MLB win expectancy. It also lacked an exact P/L training target.
- Missing feature values were converted to raw zero before standardization.
  That conflated missing state with valid baseball values such as zero outs or
  a tied score.
- The MLB fitter estimated a free coefficient for the existing consensus
  log-odds. A small sample could therefore distort the established baseline
  rather than learn an additive state correction.
- The adaptive exit learner observed only moneylines. MLB totals and run lines
  therefore fell through to a static one-reading stop even when official game
  state and the unchanged edge still supported the position.
- The original hard stop could sell on one executable quote. It retained no
  counterfactual price path after the sale, so the system could neither measure
  premature exits nor distinguish useful stops from temporary price shocks.
- The settings advisor treated an unsupported later-event test as passed. It
  optimized a large grid against a small selected-trade sample and could expose
  an Apply action without affirmative later-event evidence.

### Critical storage/performance findings

- `history.db` was approximately 6.3 GB with 6.63 million quote rows accumulated
  over roughly four days. Full order-book JSON was persisted on every websocket
  update even though the research observation interval is 15 seconds.
- `ledger.db` was approximately 2.56 GB with 274,814 decision rows written over
  about 511 seconds. Approximately 1.9 GB was free SQLite pages left inside the
  file after deletes.
- The decision row-cap query used only `as_of` as its cutoff. Rows sharing the
  cutoff timestamp survived together, so the configured 50,000-row cap was not
  actually strict.
- The live in-memory store was already bounded; the main pressure came from
  write amplification and unreclaimed SQLite pages, not the latest-value cache.

## Implemented corrections

- Added a local-only official MLB schedule/linescore adapter. It safely maps
  monitored events and captures inning/half, outs, bases, count, batter, and
  pitcher identity without consuming Odds API credits.
- Added structured sport-state persistence and completeness accounting.
- Changed MLB research fitting to a fixed existing-consensus logit plus a
  regularized state correction. The established edge calculation is untouched.
- Preserved missing values through feature preparation and imputed them at the
  training mean after scaling, with explicit availability features.
- Linked exact closed managed positions to an `after_cost_strategy_pnl` target
  by entry decision id. The label retains an explicit selection-bias warning.
- Made settings recommendations fail closed unless the later-event test has
  positive after-cost ROI and at least 80% event-bootstrap probability of
  positive return. Maximum drawdown now enters the objective.
- Extended the local adaptive exit research layer to exact MLB moneyline,
  run-line, and total contexts without changing the established probability,
  edge, quality, entry, or sizing math.
- Added an optional bounded MLB stop confirmation. Ordinary breaches wait for
  repeated readings and a short game-aware grace period while an explicit
  model reversal, terminal/irreversible state, stale state, and a separate
  catastrophic boundary remain immediate.
- Added persistent post-exit shadow marks. They issue no order and measure
  recovery to entry, partial loss recovery, rebound size, and additional
  avoided downside by market type and exit reason.
- Sampled local historical disk snapshots at 5 seconds with a 15-second
  unchanged-price heartbeat while leaving every live quote available to the
  engine.
- Made the decision row cap deterministic across timestamp ties.

## Still required before model-assisted live execution

1. Accumulate at least 200 settled MLB events and 1,000 state-complete
   observations, with broad coverage across teams, innings, score states, and
   price bands.
2. Freeze a final untouched chronological test period. Walk-forward folds used
   repeatedly during development are validation evidence, not a permanent test
   set.
3. Add versioned pregame personnel priors: confirmed lineups, current pitcher
   quality, handedness, times through order, bullpen workload, park, and weather.
   IDs alone are captured now; quality values are not yet invented.
4. Evaluate calibration, Brier score, log loss, after-cost ROI, CLV, maximum
   drawdown, and probability of positive return by whole event. Do not select on
   win rate alone.
5. Correct for policy selection bias with exploration or defensible
   off-policy/counterfactual methods before claiming the settings optimizer can
   maximize profit.
6. Promote only a frozen, hashed artifact through shadow, dry-run, and tightly
   limited live stages. Online calibration may adapt a reviewed base forecast,
   but automatic self-retraining must not silently authorize orders.
7. Compare the state-aware stop against the static policy on later whole games:
   after-cost P/L, drawdown, recovery after sale, tail loss, and event-block
   uncertainty must all be reported. A higher rebound count alone is not enough.

No honest system can promise a 70-80% win rate or guaranteed profit. The useful
goal is calibrated probability and positive after-cost value with controlled
drawdown on genuinely later games.
