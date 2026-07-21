# Model card: source-family consensus

## Purpose

This component transforms external prices into a comparison consensus. It is
not an independent sports prediction model and does not establish that an edge
exists.

## Method

Complete outcomes from each non-target source are de-vigged. Exchange prices
are treated as already de-vigged. Traditional books support proportional and
numerically checked Shin candidates. One observation per canonical source
family is consumed, so duplicated aliases do not increase support. Consensus
candidates are equal-family logit pooling, a predeclared sharp-source baseline,
and regularized stacked-logit coefficients supplied by the fitted artifact.

Candidate selection uses the earliest fold. Learned stacking must also beat the
simple equal-family baseline on both log loss and Brier score in a later
validation fold. Otherwise the simple baseline remains selected. The final test
period is untouched until reporting.

## Calibration and uncertainty

Statistical calibration is unavailable unless a separately versioned v2
chronological artifact supports the exact hierarchical segment; without it the
output is `WATCH`. Version 1 artifacts remain readable but display-only.

Version 2 fits monotone beta calibration
`sigmoid(a*log(p) - b*log(1-p) + c)` on the calibration fold. Beta is selected
only if it improves both validation proper scores; otherwise the artifact
records identity calibration. At least 1,000 observations are required in each
selection, calibration, validation, and untouched-test fold.

Uncertainty uses at least 200 event-block draws. Each draw keeps same-event
markets together, resamples de-vig/consensus pipeline choice, carries that
pipeline's sharp-source or stacking coefficients and missing-source policy,
refits calibration, and aligns the result with realized execution-cost error,
including the research export's fee and latency effects. Runtime recomputes each
draw from the current source-family quotes, then reports the 2.5%/97.5%
calibrated-probability interval and the fraction with positive net EV. Raw
cross-book dispersion is a data-quality signal, not a standard error.

## Eligibility

Unknown or stale provider time, incomplete outcomes, fewer than two independent
source families, ambiguous identity, incomplete target depth, unknown fees, or
closed/restricted status fails closed. Action also requires the artifact's
sample, uncertainty, positive-net-EV, minimum-dollar-EV, and net-edge gates.

## Artifact lineage

The v2 artifact records the selected pipeline, candidate scores, nested fold
cutoffs, beta coefficients, aligned bootstrap draws, exact sample sizes,
untouched-test metrics, method-specific thresholds, and a SHA-256 model hash.
The full artifact enters the engine configuration hash. Every decision also
records model, calibration, engine, configuration, and execution versions.

## Limitations

Market consensus can share common information and common errors. The displayed
quality dimensions and machine-readable gates are policy controls, not
probability accuracy. The repository ships no fitted artifact, so no
profitability or predictive-edge claim is supported by this release.
