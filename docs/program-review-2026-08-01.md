# Program review — 2026-08-01

Whole-system review of the execution pipeline and predictive stack, run
alongside `docs/line-settings-analysis-2026-08-01.md`. Findings are tiered by
expected impact on trade management and predictiveness; each carries its
evidence. Nothing here weakens the safety gates (approval token, arming
latch, price bounds, allocation limits).

## Fixed in this pass

1. **The inert confirmation stop had two mechanical causes, not a design
   flaw.** (a) The trader's cycle snapshot took the *last* game state from
   any provider; the generic Polymarket feed emits states with no
   `sport_state`, so one such packet blanked the MLB inning context —
   `_live_trading_snapshot` now prefers the newest `mlb-linescore-*` state
   (`app/main.py`). (b) `_baseball_fraction_remaining` rejected the `"end"`
   half the linescore feed reports between half-innings, going stateless
   exactly between halves — it now accepts `end/middle` with the model lab's
   completed-innings convention (`app/polymarket_us_trading.py`). Together
   these explain the measured "31/37 dry, 19/20 live stops fired immediate".
2. **Stateless-bypass reasons are now decomposable** ("no live MLB game
   state" vs "state lacks a usable inning or half") instead of one string.
3. **Events removed mid-game never wrote settled outcomes** (~25% of the
   closed record was ungradeable). `tools/backfill_event_outcomes.py`
   recovered all 8 events from the MLB Stats API with monotone-score
   verification; the analysis now grades 647/656 dry closes.

## Tier 1 — make the dry lane a valid measurement bed (do before the next collection window)

1. **Fee symmetry between lanes.** Dry entries/marks are gross (raw ask, raw
   bid); live are fee-inclusive/fee-adjusted (`polymarket_us_trading.py:6866`
   vs `:6996`, `:7227` vs `:7189`). Every exit threshold therefore acts on a
   different quantity per lane — a ~5%-of-stake wedge at 50c against profit
   targets of 8–15%. Dry should charge the same estimated taker fee so a
   dry-validated policy transfers. Related: the live trailing peak is seeded
   from the *gross* top-of-book quote but compared against *net* size-aware
   marks (`:8461` vs `:7249`), so live trailing fires early by roughly the
   fee wedge; seed on the same scale.
2. **In-lane settlement for full-game positions.** `_dry_segment_settlement_value`
   settles segments only; a full-game position whose event ends leaves
   `monitored` and stays open forever at a frozen mark. Winners that ride to
   the end are excluded from realized results while every early exit is
   included — survivorship bias in the W-L-P record (currently 4 dry / 29
   live open rows). Settle full-game dry positions from `event_outcomes`
   finals, and write an outcome row whenever a live-played event is removed
   or the server stops (the runtime twin of the backfill tool).
3. **Journal retention protects the wrong rows.** `mark` rows — the only
   home of `exit_book_depth`, `gross_exit_value`, `estimated_exit_fee`, and
   the per-mark guard payloads — are pruned first, while `qualification`
   rejections flood the same 10k budget because their dedup key embeds
   formatted floats (`:6213-6222`) and so never deduplicates. Strip values
   from the dedup key and give marks their own retention (or persist the
   trajectory fields on the position; see Tier 2.4).

## Tier 2 — trade-management robustness

1. **`model_reversal` is a single-quote exit with no confirmation** and may
   override the protected profit floor (`:8110`, `:8013`). It measured
   *well* on the counterfactual (dry giveback −$120 — its exits beat
   holding), so this is insurance, not a leak: give it the same bounded
   two-reading window the stop has, with trigger timestamp/count columns so
   recovery is measurable.
2. **Desync disables protection.** One malformed portfolio response stamps
   `sync_error` on every open live row and protective exits refuse to run
   (`:3693-3706`, `:7514-7540`); a moving external quantity never confirms a
   mismatch because confirmation requires byte-identical observations
   (`:3787-3796`); a settled market is indistinguishable from an external
   sale and the venue's own realized P&L is read then discarded (`:3535`,
   `:3634-3645`). Protective (stop-class) exits should stay armed under
   bounded desync, and settlement should be recognized as such.
3. **Liquidity asymmetry.** Entry requires only top-of-book depth, but a
   position the book cannot fill in full is never marked at all
   (`_size_aware_exit_quote` returns None → no stop/profit evaluation,
   stale mark in performance). Mark from available depth with an explicit
   partial flag, and add retry/backoff (already in `app/sources.py`) to the
   one-shot book/portfolio reads.
4. **Persist what the exit decision saw.** Excursion *timestamps* (when the
   peak/trough printed), exit-time spread and depth, state age at stop
   evaluation, fees paid as separate columns, and why `current_edge` was
   None (no signal vs ambiguous). Also: dry manual exits DELETE the row
   (`:5253`) — close it instead so the sample survives; post-exit recovery
   shadows compare a fee-adjusted sale against a gross public bid
   (`:7575`), biasing "premature exit" evidence against live.
5. **Cycle hygiene.** Cooldown is consumed before the entry attempt
   (`:5751` vs `:5756`), so a transient venue error burns a full cooldown;
   `now` is captured before venue I/O and backdates confirmation windows on
   slow cycles (`:5595`); pacing state is memory-only, so a restart clears
   post-exit cooldowns; mixed policy vintages inside one exit decision
   (frozen thresholds, current adaptive/auto_cashout flags).

## Tier 3 — predictive capability (qualification and cash-out)

1. **US closing-line value.** Nothing measures entry price vs late market
   price for US trades — the one entry-quality metric that needs no
   settlement and works at 5–30 events. `candidate_observations` already
   retains a US price series across holding periods (open positions keep
   logging); a `tools/clv_us.py` join is ~120 lines, and three columns
   (`closing_exit_value`, `closing_ts`, `clv`) on positions make it durable.
2. **Use the state the system already collects.** `outs` and `base_mask`
   are recorded everywhere and used nowhere: a tabular RE24/win-expectancy
   lookup costs zero degrees of freedom and plugs in as (a) two model-lab
   features and (b) a dense `base_out_state` dimension replacing three
   sparse categoricals in the adaptive exit context (which currently has
   ~3,750 cells per line against tens of events — structurally dead).
   Also unused at exit time despite being computed every mark: book-depth
   and spread shifts (`exit_book_depth` is journaled then pruned), pitcher
   change (a one-row `pitcher_id` lookback — the classic bullpen cash-out
   trigger), and time-since-last-score.
3. **Retrosheet-backed offline corpus** (direction #4). The model lab's
   entire coupling surface is one row shape (`_prepare_rows`); an offline
   loader plus a `retrosheet_event_file` settlement source and a
   load-coefficients transfer-validation mode (~30 lines) lets hundreds of
   historical events do the fitting while the 5 live fit-ready events only
   validate transfer. pybaseball scrapes — keep it strictly under `tools/`.
   Chadwick id mapping is prerequisite for any batter/pitcher feature.
4. **Promote the Venn-Abers layer toward the entry path.** It is complete,
   tested, and reachable only from a CLI tool. Next: surface per-line/stage
   intervals and refusal counts in the research UI (shadow), then a
   dry-lane refusal gate — refuse entries whose interval spans the
   fee-implied break-even. The line-settings analysis shows scalar
   edge/quality thresholds cannot rank the middle of the edge distribution;
   intervals can.
5. **Model lab records moneyline only** (`sport_model_lab.py:728`) — it can
   never inform spread, the second-largest authorized line. Record all
   authorized lines even while fitting stays moneyline-first.

## Tier 4 — parked deliberately

- Off-policy evaluation (IPS/DR): propensities are degenerate by design;
  any number produced now would be wrong undetectably. Needs a controlled
  exploration mode first.
- Engine-registry promotion of an MLB model: `MIN_TEST_EVENTS = 200` is
  years away at current volume; the display-only registry seam is the
  eventual path.
- Fitting the 3-minute market-movement target: labels exist, but trusted
  settlements — not model capacity — remain the constraint.

## Stale handoff items

- "The settlement counterfactual is still an ad-hoc script" — built:
  `tools/settlement_counterfactual.py`.
- "The confirmation-gated stop does not engage … do not rely on it" — the
  two mechanical causes are fixed in this pass; the next dry window
  measures the guard as designed.
