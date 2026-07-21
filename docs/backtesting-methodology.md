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

Required benchmarks are executable target price, equal-family consensus,
sharp-source consensus when independently defined, uncalibrated consensus, and
no-independent-model policy. Searching many thresholds requires an explicit
multiple-comparison warning. No artifact is promoted merely from in-sample ROI.
