# Model and market support

No independent sport model is enabled in this repository.

The Rust routines for basketball/football/hockey score-and-clock projections and
spread/total approximations are research benchmarks only. They are not policy
eligible because the repository contains no versioned training data, leakage
audit, walk-forward evaluation, calibration bins, or out-of-sample artifact for
any sport/league/market/game-phase combination.

The live system can still display:

- normalized Polymarket books;
- independent sportsbook source-family prices;
- an equal-family logit consensus;
- gross edge and fee/slippage-adjusted paper execution estimates; and
- explicit reasons every policy gate passed, failed, or is unknown.

Without `CALIBRATION_ARTIFACT`, all selections remain `WATCH`. Setting
`ENABLE_INDEPENDENT_MODELS=true` alone does not make a selection actionable;
both valid state provenance and a validated artifact are required.

## Calibration artifact contract

Version 1 JSON requires:

- `artifact_version`, `model_version`;
- `trained_through`, `evaluated_from`, `evaluated_through` UTC timestamps;
- at least 1,000 genuinely out-of-sample observations;
- `brier_score`, `log_loss`; and
- exact lower-case `supported_markets`.
- `calibration_method: identity`, meaning chronological evaluation explicitly
  validated the untransformed consensus. Learned transforms are not accepted
  until their coefficients and parity tests are implemented.

The evaluation interval must begin after the training cutoff. Invalid or missing
artifacts stop action eligibility rather than silently falling back.

## Promotion process

Promotion requires reproducible data lineage, participant/event/market identity
audit, purged chronological splits, leakage tests, execution-aware evaluation,
calibration/reliability results, and rollback criteria. The artifact must be
reviewed and versioned separately from application code.
