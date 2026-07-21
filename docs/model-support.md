# Model and market support

No independent sport model is enabled in this repository.

The Rust routines for basketball, football, hockey, and other score/clock
projections are research benchmarks only. They are not policy eligible because
the repository contains no versioned training data, leakage audit, walk-forward
evaluation, calibration bins, or out-of-sample artifact for any exact
sport/league/market/game-phase combination.

The live system can still display normalized Polymarket books, independent
sportsbook source-family prices, source-family consensus, executable paper
costs, and explicit policy gates. Without `CALIBRATION_ARTIFACT`, all selections
remain `WATCH`. Setting `ENABLE_INDEPENDENT_MODELS=true` alone cannot promote a
model.

## Calibration artifact contract

Legacy v1 JSON remains readable for historical dashboards but is never action
eligible. Actionable v2 JSON requires:

- a SHA-256 model hash and model/calibration versions;
- explicit model-selection, calibration, validation, and untouched-test dates;
- at least 1,000 observations in every chronological fold;
- method-specific de-vig and consensus candidate scores;
- monotone identity or beta calibration; and
- at least 200 aligned event-block calibration and execution-cost draws per
  segment.

Invalid, undersized, leaking, or missing artifacts stop action eligibility
rather than silently falling back.

The offline builder is `python -m app.model_training`. Its JSONL observations
must be settled, point-in-time/out-of-fold rows with durable event IDs,
candidate probabilities, executable cost, and realized execution-cost error.
It writes a reviewable artifact but never installs or promotes it.

## Promotion process

Promotion requires reproducible data lineage, participant/event/market identity
audit, purged chronological splits, leakage tests, execution-aware evaluation,
calibration/reliability results, and rollback criteria. The artifact must be
reviewed and versioned separately from application code.

## Milestone F status

Milestone F remains disabled. The feed does not yet provide the complete,
audited feature sets and sport/league-specific out-of-sample evidence required
for basketball, soccer, hockey, baseball, football, or player-prop models.
Hard-coded projections remain non-eligible research benchmarks only.
