# Model card: source-family consensus

## Purpose

This component transforms external prices into a comparison consensus. It is
not an independent sports prediction model and does not establish that an edge
exists.

## Method

Complete outcomes from each non-target source are de-vigged. Exchange prices
are treated as already de-vigged; traditional books use the implemented Shin
solver with validity checks. One observation per canonical source family is
pooled with equal weight in logit space. Duplicated aliases therefore do not
increase support.

## Eligibility

Unknown/stale provider time, incomplete outcomes, fewer than two independent
source families, ambiguous identity, incomplete target depth, unknown fees, or
closed/restricted status fails closed. Statistical calibration is unavailable
unless a separately versioned chronological artifact explicitly supports the
market; without it the output is `WATCH`.
The current artifact contract supports only a chronologically validated
`identity` calibration. It does not pretend to apply unimplemented Platt or
isotonic parameters.

## Limitations

Market consensus can share common information and common errors. The displayed
quality dimensions measure input reliability, not chance of winning. The
conservative dispersion floor is a policy guard, not a fitted confidence
interval. No profitability or predictive-edge claim is supported by the
repository's current artifacts.
