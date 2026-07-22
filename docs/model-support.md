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

Independent output also requires `INDEPENDENT_MODEL_ARTIFACT`. Registry v1
accepts only exact sport/league/market entries with immutable model/data hashes,
the declared feature and state schemas, complete required inputs, chronological
train/validation/untouched-test windows, at least 1,000 test observations from
200 events, event-block comparison support, proper-score wins over
equal-family consensus, the pregame market, and the Stern benchmark on
identical test rows, time/lead calibration slices, multiplicity control, and
later review approval. The Rust engine repeats the exact-segment, input, hash,
model type, calibration identity, and sample checks instead of trusting the
Python loader alone.

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

The promotion boundary and audit registry are implemented, but no model is
promoted. The feed does not yet provide the complete, audited feature sets and
sport/league-specific out-of-sample evidence required for basketball, soccer,
hockey, baseball, football, or player-prop models. The score/clock math remains
a non-eligible research kernel and cannot be promoted by an artifact. Registry
v1 recognizes only a fitted NBA moneyline logistic contract with verified
possession, overtime, pregame, and phase-interaction inputs; the repository
ships no fitted artifact for it.

## Paper-harness in-play tennis model (display-grade)

`ENABLE_TENNIS_MODEL` is a separate, opt-in **paper-harness** mechanism, not a
promoted registry artifact and not subject to the validated contract above. It
exists because tennis has no reference-book feed here, so the odds engine can
never estimate an edge and every tennis selection stays single-source `WATCH`.

When enabled, `app.tennis_model` computes an independent in-play win
probability from the live set/game score, anchored to the market's pre-match
price captured at the start of the match (score 0-0; joining mid-match yields
no anchor and no trades). Paper bots trade the edge of that model versus the
executable Polymarket price via `AccountBook.place(model_probabilities=...)`;
the odds engine's honest `WATCH` verdict is unchanged. Documented
simplifications: serve-neutral (the feed exposes no server), tiebreak
approximated as one game, independent sets, best-of-three by default. It is a
demonstration/strategy-exercise model, not validated calibration.

## Paper-harness in-play lead/clock model (display-grade)

`ENABLE_LEAD_MODEL` is the same opt-in paper-harness mechanism for lead/clock
sports — basketball, football, and hockey (NBA, WNBA, NCAAB, NFL, NCAAF, NHL) —
which likewise have no reference-book feed here. `app.lead_model` models the
score margin as a drifting Brownian motion: the home win probability is
`P(final_margin > 0)` where the final margin is Normal with mean
`current_lead + pregame_margin * fraction_remaining` and standard deviation
`sigma * sqrt(fraction_remaining)`. `sigma` is the sport's final-margin standard
deviation; `pregame_margin` is inverted from the market's pre-match price
(captured at tip-off), so the model reproduces the market at the start and
diverges only as the live lead and clock move. Overtime and unsupported leagues
are skipped (no comparable regulation fraction), and joining after tip-off gets
no anchor. Both in-play models feed the shared uncertainty-aware
(`PAPER_EDGE_UNCERTAINTY_Z`) and latency-aware (`PAPER_LATENCY_BUDGET_SECONDS`,
`PAPER_MAX_STATE_AGE_SECONDS`) trade gates. Per-sport parameters are documented
approximations; this is a strategy-exercise model, not validated calibration.

## Paper-harness in-play soccer model (display-grade)

`ENABLE_SOCCER_MODEL` covers soccer 1X2 (EPL, MLS). `app.soccer_model` treats
each side's remaining goals as independent Poisson arrivals over the remaining
regulation fraction; the final joint score distribution gives home/draw/away
result probabilities. The full-match scoring rates are **inverted from the
market's pre-match 1X2 price** (home-win and draw), captured at kickoff and
cached, so the model reproduces the market at the start and diverges only as the
live score and clock move. Added time / extra time is skipped (no comparable
regulation fraction) and it is restricted to moneyline/1X2 selections so spreads
and totals are never mispriced as a result bet. Simplifications: independent
Poisson (no Dixon-Coles low-score correction), constant rates, regulation only.
Note the exact Polymarket soccer market labels are unverified against live data
and the outcome mapping may need adjustment once observed. Feeds the same §5/§7
gates; strategy-exercise model, not validated calibration.
