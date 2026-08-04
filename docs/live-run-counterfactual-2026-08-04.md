# Local live runs vs the current policy stack — 2026-08-04

What the retained local history looks like, and what it would have looked
like had the current policy stack (side gates, cheap-side bands, edge
ceiling, guards-off exits with per-line profit targets) been running from
the first night. Methodology follows `tools/settlement_counterfactual.py`
plus the full-path correction: every closed full-game position is graded
against the settled final score from `workstation-data/history.db`, and the
current-exit simulation uses retained path extremes
(`highest_exit_value`/`lowest_exit_value` while held, plus
`exit_recovery_observations` best/worst after the actual sale) so a
counterfactual exit is only credited where the observed path actually
reached it. Analysis script preserved in the session scratchpad; run
`python -m tools.backfill_event_outcomes --apply` first (idempotent).

The "current policy" used for grading is the live lane's saved policy as of
2026-08-04 (read from `live_trading_config`, not from docs): Under-only
totals, underdog-only run lines, ML 10–45c with execution-edge ceiling
0.10, spread 23–45c with source agreement ≥ 55, totals 15–38c with edge
floor 0.10, quality ≥ 70, stops at 0.95 (vestigial), profit targets
0.40/0.30/0.30.

## Live lane (real money, Jul 26 – Aug 3, 8 slates)

Book of record: 308 closed positions ($434.98 total stake, **−$42.70
realized**), plus 33 phone/external closes carrying no provable P/L.
301 closed full-game positions across 73 settled events grade against
settlement ($431.37 stake, −$41.48, settlement win rate 51.5% at ~37c
stake-weighted entry — the entries beat their price by ~14pp all along).

| Slice | n | stake | actual | current exits | pure hold |
|---|---|---|---|---|---|
| All graded | 301 | $431.37 | −$41.48 (−9.6%) | — | +$259.78 (+60.2%) |
| Current policy would take | 99 (49 ev) | $152.32 | −$15.81 (−10.4%) | **+$26.32 (+17.3%)** | **+$231.23 (+151.8%)** |
| Current policy rejects | 202 | $279.05 | −$25.67 (−9.2%) | — | +$28.55 (+10.2%) |

The kept book settles 76.8% winners. Same entries, same nights, the only
change is the exit stack and the filter.

What the gates remove (live, actual results of rejected positions):

- **Spread favorites: 74 positions, 27.0% win, −$14.73 actual.**
- **Overs: 39 positions, 28.2% win, −$4.50 actual, −$14.58 held.** Both
  toxic pockets confirmed again at settlement.
- Price-band rejects: 69 positions, 52.2% win, −$8.66 actual, +$14.47
  held — the bands are exposure control, not where the edge lives.
- Edge ceiling (>0.10 ML): 9 positions — high-conviction entries remain
  slightly worse than the book (conviction inversely predictive, again).
- Cheap-side underdog spreads and Unders dropped only by the band edges:
  19 dog spreads (84.2% win, +$29.75 held) and 34 Unders (38.2% win)
  fall outside today's bands; the dog spreads are the one place the live
  band is clearly leaving money.

Night by night, actual whole-book vs the current-policy subset:

| Night | book n | actual | kept n | current exits | pure hold |
|---|---|---|---|---|---|
| Jul 27 | 90 | −$19.62 | 28 | +$9.48 | +$94.82 |
| Jul 28 | 35 | −$2.17 | 6 | −$0.74 | −$0.82 |
| Jul 29 | 36 | −$1.72 | 11 | +$1.96 | +$15.66 |
| Jul 30 | 26 | −$2.76 | 4 | +$1.46 | +$8.14 |
| Jul 31 | 33 | −$6.73 | 9 | +$0.29 | +$26.79 |
| Aug 1 | 47 | −$5.90 | 21 | +$9.94 | +$50.05 |
| Aug 2 | 21 | −$2.84 | 8 | −$0.26 | +$8.96 |
| Aug 3 | 13 | +$0.26 | 12 | +$4.20 | +$27.63 |

Aug 3 is the first night actually run under the current regime (entry
snapshots carry stop 0.95): the only positive actual night, and 12 of its
13 entries pass today's gates. Every earlier night's book was fighting its
own exits: on the kept live book alone, hard stops realized −$13.28
against +$83.04 held, model reversals $0.01 against +$74.57 held.

**The missing-out number (live): −$41.48 actual against roughly +$26
(target-capped) to +$231 (pure hold) — a $68–$273 swing on ~$150 of
current-policy stake.** The truth for a capital-limited book sits between
the two columns: targets bank +17% and recycle capital; holds earn +152%
per settled dollar but ride full drawdown with no cap.

## Dry lane (simulated, Jul 29 – Aug 3)

801 closed ($6,313.43, −$373.27 realized); 792 grade (58 events).

| Slice | n | stake | actual | current exits | pure hold |
|---|---|---|---|---|---|
| All graded | 792 | $6,275.72 | −$360.32 (−5.7%) | — | +$1,904.28 (+30.3%) |
| Current policy would take | 140 (48 ev) | $1,105.95 | −$35.76 (−3.2%) | **+$258.68 (+23.4%)** | **+$1,621.05 (+146.6%)** |
| Current policy rejects | 652 | $5,169.77 | −$324.56 (−6.3%) | — | +$283.24 (+5.5%) |

The same structure at 7× the stake: the kept book settles 82.1% winners
and is strongly positive under either current exit treatment; the rejected
flow is where the losses lived — Overs (116 positions, 12.9% win,
**−$588.62 held**) and spread favorites (205 positions, 26.8% win,
−$525.53 held) are negative even with perfect patience, i.e. they are bad
entries, not bad exits. Rejected Unders/dog-spreads outside the live bands
grade positive held (+$592/+$399) — that is what the dry lane's wider
band probes (ML floor 0.06, Unders ceiling 0.45) are currently measuring.

## Caveats that bound these numbers

- The counterfactual can only re-filter trades that were actually placed.
  Entries today's policy would take but the old one never placed (e.g.
  sub-15c moneylines before the floor dropped to 0.10) are absent, so the
  current-policy columns understate the new opportunity set.
- Pure hold assumes unlimited capital, prices no drawdown, and is
  evidence about exit rules — not licence to remove stops.
- The target simulation is conservative for winners (capped at the
  target) and can only rescue losers whose retained path provably crossed
  the target (7 live / 15 dry — lower bounds, since marks are sampled).
- Dry realized P/L excludes fees while live marks are fee-adjusted (the
  standing tool convention; read lanes separately).
- Live Unders remain the thin spot: 22 kept positions grade 45.5% win,
  −$2.88 under current exits (dry: 76.2% win, +$28.63). The Under gate is
  supported by the pooled grading, but the live-only sample is too small
  to call settled.
