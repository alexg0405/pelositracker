# MLB live model research plan

## Boundary

This work is local and research-only. It does not change the existing consensus,
edge, signal-quality, calibration, recommendation, or execution calculations.
The established consensus remains the baseline. A fitted MLB artifact cannot be
installed or authorize a trade from the Model Lab.

## Predictive hierarchy

The initial candidate is a regularized, state-aware residual model. It asks
whether structured baseball state adds out-of-sample information beyond the
existing consensus:

1. Existing independent-source consensus and home/away identity.
2. Score differential, inning, top/bottom half, batting side, extra innings,
   and nonlinear close/late-game interactions.
3. The 24 base/out states, plus the ball-strike count.
4. Confirmed lineup strength; starting and current pitcher quality; times
   through the order; bullpen availability and recent workload; park and weather
   run environment; batter/pitcher handedness and expected wOBA context.
5. Price and score changes are retained as secondary diagnostics. They are not
   treated as the definition of the model.

Phase 2 now adds a small official MLB linescore poll for each monitored MLB
event. It records inning/half, score, outs, runners on each base, count, current
batter identity, and current pitcher identity. Schedule matching uses both team
orientation and start time so a doubleheader cannot be matched by names alone.
Missing or ambiguous state fails closed and is never inferred from price.

The fitted MLB form is additive:

`logit(candidate) = logit(existing consensus) + regularized state correction`.

The existing consensus is therefore a fixed offset rather than a feature whose
coefficient can be relearned. This preserves the established probability as the
benchmark and asks only whether live MLB state adds value on later games.
Missing values are imputed to the training-event mean after scaling, with
explicit state-availability indicators; a missing count is not confused with
the real value zero.

## Adaptive exit overlay

The local auto trader has a second, narrower research question: given an
already-open MLB moneyline, run-line, or total position, will its executable
cash-out value move adversely before it moves favorably over the next
configured horizon?

This overlay uses inning/half, the selection's margin relative to its line,
batting side where applicable, recent executable-price momentum, and the
unchanged engine's current edge as read-only features. Labels require future
executable marks and use adverse-first versus favorable-first 3% crossings,
with a 1% horizon fallback and neutral windows excluded. Estimates are
segmented by market type and averaged by event before they influence the
hierarchical context rate, so one frequently polled game does not pretend to
be many independent games.

The overlay can only tighten a configured meaningful-profit target, trailing
pullback, and profit-lock edge-decay trigger within an operator cap. Observe-only
mode records and scores forecasts without changing any threshold. Probability,
edge, signal quality, entries, sizing, exact contract mapping, and the hard stop
remain outside this model. Retained observations are local and persist until
the operator invokes the dedicated exact-phrase clear action.

The separate state-aware stop guard does not fit or modify the stop-loss
percentage. It requires bounded repeated evidence before an ordinary MLB stop,
provided official game state is live/fresh and the existing execution edge has
not materially reversed. A catastrophic loss boundary, explicit reversal,
terminal state, or structurally impossible Under total remains immediate.
Missing edge context receives a shorter bounded confirmation window rather than
being mislabeled as a reversal.

Every managed exit starts a non-trading shadow mark of the same contract. The
recovery ledger records best/worst executable-side snapshots after sale,
recovery to cost basis, and recovery of half the realized loss. This directly
tests the premature-stop hypothesis. Until enough independent games accumulate,
these data are descriptive and cannot justify widening a live stop.

## Why these inputs

- Lindsey's empirical framework makes score, inning, outs, and base occupancy
  the core win/run state.
- Bukiet, Harold, and Palacios model baseball as plate-appearance transitions
  and show how player-specific ability can enter run distributions.
- MLB Statcast exposes the official historical fields needed for the next
  phase: inning/half, runners, outs, pitcher, batter, xwOBA, and changes in run
  and win expectancy.
- Yo Joong Choi's 2023 CMU PhD thesis compares MLB forecasts chronologically
  over 25,165 games and reports that closing betting odds marginally
  outperformed FiveThirtyEight. That supports retaining the market consensus as
  a hard benchmark rather than assuming a sport model is superior.
- Li, Huang, and Li show that feature selection can improve MLB prediction and
  that simpler SVM/logistic approaches were competitive with their neural
  alternatives. Complexity therefore has to earn its place on later data.

## Evaluation contract

- Split by whole event in chronological order. Never split observations from
  one game across train and test.
- In addition to the frozen 80/20 comparison, run expanding-window
  walk-forward folds where every test game occurs after every training game.
- Give each game equal total weight so a frequently sampled game cannot dominate
  the fit.
- Compare Brier score and log loss against the unchanged existing consensus.
- Report reliability bins and a whole-event bootstrap interval for Brier
  improvement. Repeated polls are not independent uncertainty units.
- Keep the state-only phase research-only even if it beats the small research
  holdout.
- Require richer base/out and personnel data, a much larger chronological
  sample, calibration, event-block uncertainty, and explicit review before any
  separate promotion process can be considered.

Final-game outcome, three-minute monitored market movement, and actual
after-cost strategy return are separate targets. The Model Lab now links a
closed managed position only when its exact entry decision identifier matches a
research observation. The label is realized P/L divided by cost basis and its
metadata retains mode, dollars, holding time, and exit reason. This selected
sample is explicitly marked as policy-biased: it says nothing counterfactual
about rejected opportunities. A price move or winning result is never relabeled
as profit.

Content-hashed JSON Lines snapshots preserve observations, targets, and model
artifacts outside the capped dashboard journal. A snapshot is an auditable
research input, never a promotion or installation mechanism.

## References

- George R. Lindsey (1963), *An Investigation of Strategies in Baseball*:
  <https://doi.org/10.1287/opre.11.4.477>
- Bruce Bukiet, Elliotte Rusty Harold, and José Luis Palacios (1997),
  *A Markov Chain Approach to Baseball*:
  <https://doi.org/10.1287/opre.45.1.14>
- Yo Joong Choi (2023), PhD thesis, Carnegie Mellon University:
  <https://www.ml.cmu.edu/research/joint_phd_dissertations/thesis_yojoongc_phd_stat_2023.pdf>
- MLB Statcast CSV field documentation:
  <https://baseballsavant.mlb.com/csv-docs>
- MLB Win Expectancy definition and Game Strategy Explorer:
  <https://www.mlb.com/glossary/advanced-stats/win-expectancy> and
  <https://baseballsavant.mlb.com/game-strategy-explorer>
- Shu-Fen Li, Mei-Ling Huang, and Yun-Zhi Li (2022),
  *Exploring and Selecting Features to Predict the Next Outcomes of MLB Games*:
  <https://doi.org/10.3390/e24020288>
- Wheatcroft (2024), *Machine learning for sports betting: Should model
  selection be based on accuracy or calibration?*:
  <https://doi.org/10.1016/j.mlwa.2024.100539>
- Gupta and Ramdas (2023), *Online Platt Scaling with Calibeating*:
  <https://proceedings.mlr.press/v202/gupta23c.html>
- Huber and Heumann et al. (2025), *The Impacts of Increasingly Complex
  Matchup Models on Baseball Win Probability* (recent preprint, not yet a
  production validation artifact): <https://arxiv.org/abs/2511.17733>
- Kaminski and Lo, *When Do Stop-Loss Rules Stop Losses?*:
  <https://citeseerx.ist.psu.edu/document?doi=954a65e94b6cee2abf017650e7381aacef54f8b2&repid=rep1&type=pdf>
- Lo and Remorov, *Stop-Loss Strategies with Serial Correlation, Regime
  Switching, and Transaction Costs*: <https://ssrn.com/abstract=2695383>
- Simon, *Inefficient Forecasts at the Sportsbook: An Analysis of Real-Time
  Betting Line Movement*: <https://doi.org/10.1287/mnsc.2022.00456>
- Polymarket US order semantics and fees:
  <https://docs.polymarket.us/concepts/orders> and
  <https://docs.polymarket.us/fees>
