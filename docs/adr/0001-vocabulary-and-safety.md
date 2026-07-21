# ADR 0001: Vocabulary and fail-closed safety

Status: Accepted
Date: 2026-07-20

## Context

The application used `model_probability`, `confidence`, `edge`, and `CLV` for several different concepts. It also treated unavailable timestamps, game clocks, order-book depth, fees, and model calibration as benign defaults. That makes decisions difficult to reproduce and can make weak data appear actionable.

## Decision

The canonical vocabulary is:

- **source probability**: one source family's normalized probability estimate.
- **consensus probability**: a deterministic aggregation across independent source families.
- **independent model probability**: output from a separately trained and validated sport model. It is absent unless a versioned validation artifact exists.
- **market probability**: the executable price-derived probability for the selected outcome.
- **gross edge**: probability advantage before execution cost.
- **net expected value**: expected paper value after executable depth, fees, slippage, and explicit policy costs.
- **signal quality**: freshness, identity, state, source-independence, liquidity, and calibration quality dimensions. It is not a probability.
- **policy eligibility**: whether every mandatory safety gate passed.
- **decision confidence**: a display-only summary of validated quality dimensions. It cannot override a failed gate.
- **decision-time mark**: the immutable market/consensus state used for a decision.
- **close-time mark**: the final valid executable observation before suspension or resolution.
- **CLV**: comparison between a recorded fill price and the separately recorded close-time mark. It is never reconstructed from settlement-time consensus.

Every evaluation requires an explicit timezone-aware `as_of`. Provider time, local receipt time, and processing time remain separate. Missing or implausible provider time is quarantined and cannot be made actionable by receipt time.

All real-money functionality is out of scope. The only execution representation is deterministic paper-fill simulation and an auditable paper-order lifecycle.

## Consequences

- Existing API names may remain temporarily as deprecated compatibility aliases, but canonical records and new UI copy use the vocabulary above.
- Unavailable calibration, provider timestamps, state validity, identity confidence, full executable depth, or fee metadata yields `WATCH`/`NO_ACTION`.
- Replay and live evaluation use the same pure decision boundary.
- Independent sport models remain disabled until supported by versioned out-of-sample validation evidence.
