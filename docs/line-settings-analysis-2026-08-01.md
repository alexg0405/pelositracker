# Line settings analysis — 2026-08-01

Statistical pass over every closed full-game position in both lanes, graded
against settled outcomes with the application's own settlement function (the
`tools/settlement_counterfactual.py` join, extended with excursion,
predictor-bucket, and threshold-grid analysis). All intervals are event-block
bootstraps (2,000 resamples, seeded); positions within one game are never
treated as independent.

**This analysis first found, then fixed, its own biggest blind spot.** Eight
completed events (161 dry + most of 47 live closed positions, ~25% of the
record) had no settled outcome because they were removed from monitoring in
the 8th–9th inning — end-of-night shutdowns — so finalization never wrote an
`event_outcomes` row. `tools/backfill_event_outcomes.py` recovered all eight
official finals from the MLB Stats API with monotone-score verification.
Everything below uses the repaired record: **dry 647 positions / 28 events
($4.9k stake), live 222 / 47 ($330)**. Notably, one earlier "finding" (that
tight profit scalps rescue spreads) did not survive the repaired sample —
proof of why the coverage fix had to come first.

## 1. The per-line verdict (complete record)

| Lane | Line | n | Events | Win − price | 95% CI | Settle ROI | 95% CI |
|---|---|---:|---:|---:|---|---:|---|
| dry | moneyline | 172 | 27 | **+10.2pp** | +2.3 to +16.9 | **+60.5%** | +20.0 to +100.8 |
| dry | spread | 276 | 28 | +5.6pp | −0.8 to +12.0 | +12.0% | −19.8 to +45.4 |
| dry | total | 199 | 21 | +4.4pp | −3.2 to +10.4 | +12.1% | −12.9 to +35.5 |
| live | moneyline | 48 | 23 | **+17.0pp** | +3.6 to +31.0 | **+70.3%** | +22.6 to +120.4 |
| live | spread | 88 | 31 | **+17.9pp** | +7.8 to +26.1 | **+72.7%** | +25.3 to +117.5 |
| live | total | 86 | 34 | −3.7pp | −14.3 to +7.2 | +10.5% | −28.5 to +48.8 |

Moneyline is genuinely predictive in both lanes with intervals excluding
zero. Spread is significant on live and positive-leaning on dry (94.5% of
resamples positive) — the earlier "dry spreads are a coin flip" verdict
softened once the missing events were graded. Totals remain the weak line:
the live gap interval is centered below zero (positive in only 25% of
resamples).

## 2. The current filter stack, scored retroactively

Applying today's dry policy (moneyline+spread, 20–45c, quality ≥ 70) to the
complete record:

| Lane | Selection | n | Events | Win − price | 95% CI | Settle ROI | 95% CI |
|---|---|---:|---:|---:|---|---:|---|
| dry | current stack | 186 | 27 | **+14.7pp** | +3.3 to +25.8 | **+43.3%** | +9.9 to +80.8 |
| dry | moneyline within | 61 | 21 | **+30.9pp** | +9.9 to +47.7 | **+102.5%** | +27.2 to +161.6 |
| dry | spread within | 125 | 24 | +6.8pp | −4.6 to +18.5 | +11.7% | −29.1 to +63.5 |
| live | current stack | 62 | 30 | **+23.7pp** | +12.6 to +34.7 | **+80.2%** | +38.3 to +124.1 |
| live | moneyline within | 14 | 12 | **+39.6pp** | +16.9 to +60.7 | +145.5% | +48.1 to +226.1 |
| live | spread within | 48 | 24 | **+19.0pp** | +8.0 to +31.0 | +54.8% | +15.8 to +101.6 |

The untested 2026-07-31 filter change is retroactively validated on both
lanes — the stack roughly doubles the unfiltered gap.

**Price band.** Dry moneyline by entry price: every band at or below 45c is
strongly positive; 45–55c is flat (+3.2pp); **≥55c is where the model is
badly wrong (−42.3pp, settle −61.3%, n=40)**. The 45c cap is load-bearing.
The <20c region printed +70.9pp over 9 events but at 4.3% entry fees and
extreme variance — not enough evidence to lower the floor.

**Quality floor.** Unconditionally, quality buckets look non-monotone; the
confound is price band. Within 20–45c, quality does nothing on moneyline
(every bucket ≈ +37pp pre-backfill) and everything on spread: in-band
spreads under quality 70 ran **−7.1pp / −64.8% settle ROI** (n=21, 11
events) on the complete dry record. The floor's whole value is pruning bad
spreads; it is free on moneyline. Keep it.

## 3. Exit thresholds: what the grids identify

**Stop level.** Positions crossing entry×(1−s) are graded as exiting there;
others at settlement. Right-censored — historically stopped positions have
no path after their exit — and 51% dry / 85% live of rows predate the
retained-low column, so absolute ROIs lean toward the hold value; the
evidence is the *ordering*:

| Dry moneyline | s=0.20 | s=0.28 | s=0.40 | **s=0.50** | s=0.60 | no stop |
|---|---:|---:|---:|---:|---:|---:|
| crossed | 18.6% | 17.4% | 8.7% | 4.1% | 2.9% | — |
| ROI | 27.8% | 27.8% | 42.5% | **48.8%** | 49.6% | 60.5% |

The pre-registered 0.50 sits at the start of the plateau on both lanes
(live: 0.50 → 72.0% vs 58.6% at 0.28); 0.60 adds ~nothing; the catastrophic
0.80 floor crossed 0% of moneylines. On spreads no stop level materially
changes the outcome — the stop was never the spread problem.

**Profit target.** Positions whose retained high reached entry×(1+t) are
graded as locked there; others at settlement:

| Lock ROI vs hold | t=0.05 | t=0.08 | t=0.10 | t=0.15 (now) | t=0.30 | t=0.50 | hold |
|---|---:|---:|---:|---:|---:|---:|---:|
| dry moneyline | 20.3 | 25.9 | 28.5 | 36.2 | 45.5 | 49.2 | **60.5** |
| dry spread | 5.1 | 3.4 | 2.0 | 7.1 | 4.9 | 4.5 | **12.0** |
| live moneyline | 63.2 | 77.4 | 76.1 | 76.0 | 75.5 | 77.4 | 70.3 |
| live spread | 57.8 | 66.4 | 66.7 | 70.7 | 69.8 | 70.0 | **72.7** |

On the complete record **every profit-lock level costs money against holding
on three of four lane×line cells** — the measured $557 giveback in
`profit_lock_after_edge_decay` (137 dry positions) is this table realized.
The pre-backfill sample suggested tight scalps rescued dry spreads; the
complete record reverses that. Only live moneyline is indifferent. Trailing
locks and the retained-profit floor measured fine (dry trailing exits saved
money vs settlement pre-backfill; small n) — leave them alone.

## 4. Recommended settings

Everything is dry-lane and phased; the pre-registered widened-stop window is
the measurement in flight and drawdown remains the veto.

**Keep — now validated on the complete record (global):**

| Setting | Value | Backing |
|---|---|---|
| Authorized lines | moneyline + spread, totals off | §1: totals CI centered below zero on the largest event sample |
| Entry price band | 0.20 – 0.45 | §2: ≥55c is −42pp; 45–55c flat; the band doubles the gap |
| min_signal_quality | 70 | §2: excludes a −64.8% in-band spread tail, free on ML |
| stop_loss / catastrophic | 0.50 / 1.6 | §3: plateau on both lanes; 0.80 floor never crossed on ML |
| candidate_cooldown / max_open | 300s / 10 | median hold 3.6 min at ≈3.3% entry fees — churn is the fee engine |
| fee_edge_margin | 1.5 | fee floor is price-dependent; unchanged evidence |

**Change via per-line execution profiles, as the next dry phase after the
current window reads out — one change per phase, counterfactual re-run after
each:**

| Profile | Field | Now | Recommend | Backing |
|---|---|---|---|---|
| moneyline/all | profit_target | 0.15 | **0.30** | §3: dry lock 36.2 → 45.5 vs hold 60.5; live indifferent |
| spread/all | profit_target | 0.15 | **0.30** (align) | §3 complete record: holding beats every lock on both lanes' spreads |

The earlier idea of a tight spread scalp target (0.05–0.08) is explicitly
**withdrawn**: it was an artifact of the incomplete sample.

**Not recommended despite surface appeal:** raising min_edge (displayed-edge
buckets are non-monotone — the 10–15c bucket settled at −30.5% while ≥15c
settled +136%; a scalar threshold cannot rank these), lowering the price
floor for the <20c pocket (fee load, variance), re-enabling totals (live CI
is the strongest negative evidence held).

**The structural recommendation:** the edge/quality non-monotonicities are
the empirical case for per-contract interval confidence over any scalar
threshold. On the complete dry record the Venn-Abers shadow layer
(`tools/venn_abers_shadow.py`) now shows every moneyline stage beating the
market price with the tightest intervals in the book (moneyline/early:
median width 0.016, 58% actionable), while spread early/middle still fail to
beat price and stay heavily refused. Promoting the interval refusal from
shadow to a dry-lane entry gate — size on interval width, refuse when the
interval spans the fee floor — is the highest-leverage qualification change
this data supports.

## 5. Capture gaps (found, and partly fixed, by this analysis)

1. **Settled outcomes were missing for whole events — fixed.** Events
   removed mid-game at end of night never reached finalization.
   `tools/backfill_event_outcomes.py` recovered all 8 (preview → verify →
   apply, insert-only). Remaining: 7 live positions whose events predate
   state retention entirely. **Runtime fix still owed:** finalization should
   write an outcome row whenever a monitored event that reached live play is
   removed or the server stops, and the backfill should run on a schedule.
2. **Retained adverse excursion is young.** `lowest_exit_value` is NULL on
   51% of dry / 85% of live graded rows (column postdates most of the
   record), so stop grids lean optimistic; self-heals with new data.
3. **Game stage is missing on most live rows** (`entry_game_fraction_remaining`
   NULL on 119/222), so live per-stage authorization evidence barely exists.
4. **Entry microstructure is patchy:** entry spread recorded on 61%/14%
   (dry/live), source agreement on 85%/21%.
5. **No excursion timestamps.** The retained high/low say nothing about
   *when* the peak occurred; time-to-peak is what a smarter cash-out trigger
   would train on. Record `highest_exit_value_ts`/`lowest_exit_value_ts`.

## 6. Anomalies parked for future measurement

- First entry in an event (dry `entries_60m`=1 bucket, n=19) ran −28.5pp;
  later entries were positive. Possible early-information disadvantage;
  check before raising per-event entry caps.
- The displayed-edge 10–15c bucket settling at −30.5% (12 events) while
  neighboring buckets are positive is consistent with a stale-quote or
  mapping artifact in mid-sized gaps; the interval layer is the mitigation.
