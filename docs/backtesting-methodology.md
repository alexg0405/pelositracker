# Backtesting methodology

The current replay is a deterministic audit harness, not evidence of strategy
profitability. It preserves provider/receipt/processing time, original ordering,
ask-size-only changes, terminal cutoffs, exact configuration, and decision
hashes. A historical execution study must fill only against the first eligible
complete snapshot at or after signal time plus declared latency.

Model or calibration promotion requires rolling-origin train/validation/test
splits, all markets from one event in the same fold, training cutoff before the
evaluation interval, and no threshold tuning on the final test period. Report
eligible opportunities and rejection coverage separately from selected paper
signals, plus fill/slippage/fees, Brier and log score, reliability, executable
CLV, turnover, drawdown, concentration, and event-block uncertainty intervals.

The report emits calibrated-consensus Brier/log loss, binned reliability,
Murphy reliability/resolution/uncertainty decomposition, calibration intercept
and slope, submitted/filled/fill-rate/turnover/fees/net paper return, maximum
drawdown, sport/event concentration, and an event-block interval for executable
CLV. Target-executable and reference-consensus CLV are named separately.
Decision-mark coverage includes every evaluated `WATCH` and `PAPER_BET` row plus
failed-gate counts. Target-mid close marks remain explicitly unavailable; they
are never synthesized from settlement.

When a reviewed independent model is present, its Brier/log scores are reported
only on settled rows that contain that exact model output and are paired with
the calibrated-consensus scores on those same rows. This is a cross-check
report, not evidence that the independent model affected paper selection.

Required benchmarks are executable target price, equal-family consensus,
sharp-source consensus when independently defined, uncalibrated consensus, and
no-independent-model policy. Searching many thresholds requires an explicit
multiple-comparison warning. No artifact is promoted merely from in-sample ROI.
The machine-readable report always sets `statistical_claim_supported=false`
until a separately reviewed evaluation establishes otherwise.

## What each execution-policy field can honestly be estimated from

The settings advisor reports every meaningful policy field, but not every field
can be scored the same way. Presenting them uniformly would imply evidence that
does not exist, so each field carries an explicit `evidence_mode` and `basis`.

**`grid_search`** — the joint optimizer already scores the field against
realized after-cost P/L, with a chronological whole-event split, an event-block
bootstrap, and a concentration cap. Only these fields are written by Apply, and
only when the later-event validation passes.

**`marginal`** — the field is swept alone with the rest of the policy held
fixed. A joint grid over every field would multiply the hypothesis count into
the tens of thousands and make the later-event check meaningless. The marginal
answer is narrower and is labeled as conditional on the rest of the policy.
Rows that predate the column are counted as unmeasurable rather than as passing.

Measurability is a property of the retained data, not a fixed attribute of a
field. A column added by a later migration leaves older trades unmeasurable, so
each swept option reports `unmeasurable_trades` and the field falls back to its
versioned baseline rather than scoring a biased subset. `max_spread` and
`min_book_shares` behave this way: they became scoreable only once `entry_spread`
and `entry_book_shares` were retained on the position.

**`excursion`** — two exit controls are partially identifiable, because each
position retains the extremes of the path it actually walked:
`highest_exit_value` (peak favourable) and `lowest_exit_value` (peak adverse).

If the observed path crossed a candidate threshold, an exit there was reachable
at a known price, so its after-fee return is identifiable. `profit_target` uses
the peak; `stop_loss` uses the trough. Trades whose path never crossed the
candidate keep their observed outcome, so every trade is accounted for.

Three limits are reported rather than hidden:

1. **Tightening only.** A threshold looser than the rule that actually ran has
   no observed continuation, because the position was already closed.
2. **Pre-emption.** A trade whose own exit rule *of the same family* fired
   before the candidate threshold was reached is counted in
   `unidentifiable_trades`. Any option with a non-zero count is ineligible to
   be suggested, rather than being scored on the trades that happen to remain.
3. **Sampling and fill.** Marks are taken once per cycle, so a retained extreme
   is a lower bound on the true excursion, and the counterfactual assumes a
   full fill at the threshold price plus the standard taker fee.

Because of those limits, excursion fields are reported for a deliberate
decision and are never written by Apply.

**`not_identifiable`** — trailing pullback, retained-profit floor,
model-reversal edge, minimum hold, and the adaptive-stop controls. These depend
on the whole path rather than on a single crossing, so a re-filter of past
outcomes cannot score them: a trailing stop's give-back is defined by the trail
that was running, and a reversal exit depends on an edge series the retained
extremes do not capture. These fields receive a versioned baseline value
(`BASELINE_CHEAT_SHEET_VERSION`) with a stated rationale, never a fitted number.

This is the concrete form of the rule that a sparse estimate must never be
presented as an optimized setting. Scoring an exit threshold by re-filtering
trades that a different exit threshold produced is the specific error the
distinction exists to prevent.
