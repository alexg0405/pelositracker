# Data lineage

1. An adapter records the upstream provider timestamp independently from local
   receipt and processing timestamps. Missing upstream time stays `null`.
2. Identity resolution records the canonical participant/event ID and a
   mapping decision. Ambiguous or start-time-unknown mappings are quarantined.
3. Every quote and state observation is appended to history, including depth,
   sizes, status, hashes, fees, and quarantine evidence.
4. Evaluation receives an explicit UTC `as_of` and a canonical input snapshot.
   The snapshot embeds the configuration and its SHA-256, engine, mapping,
   model, calibration, execution-policy, event, quote, and state lineage.
5. A reviewed v2 model policy contributes the selected de-vig/consensus method,
   calibration coefficients, aligned event-block draws, thresholds, sample
   count, version, and model hash to that configuration snapshot. Missing
   policy evidence remains explicit and display-only.
6. The SHA-256 decision hash and selection-specific decision ID identify the
   resulting decision. `decision_marks` persists the full canonical request and
   lineage, canonical probabilities/EV, and every machine gate even when the
   policy output is `WATCH`.
7. Optional independent-model output requires an exact reviewed registry
   policy. Its model/data/calibration hashes, test sample/event support,
   registry version, required inputs, parameters, and calibration enter the
   canonical request; its probability plus model/calibration lineage are
   persisted separately from consensus. It remains a cross-check, not an
   action override.
8. A `PAPER_BET` can create a paper order/fill only when requested and filled
   size are positive. Full ask depth and the declared fee schedule determine
   VWAP and effective price.
9. `close_marks` continuously retain only valid tradable observations and are
   frozen on suspension/finalization. Settlement is a separate idempotent mark;
   it never supplies the closing price.

Replay orders original observations by recorded time and stable row ID, stops
conservatively at terminal timestamp ties, and passes the original tick time as
`as_of`. It never rebases provider evidence to the replay machine's wall clock.

## Execution candidate log

`candidate_observations` in each Polymarket US execution store records the
population the saved policy chose *from*, not only the contracts it entered.
One row is written per contract per candidate cooldown — the shortest interval
at which that contract could actually be re-entered, which makes the cooldown
the correct sampling rate for a per-hour frequency estimate as well as a bound
on retained volume.

Each row retains the executable context (entry cost, execution edge, spread,
book depth, mapping score), the signal context (edge, quality, source
agreement, signal age, reference sources), the game-stage and repeat-entry
context, the resolved `execution_profile` key, the policy session and policy
fingerprint, and the disposition (`state`, `reason`, `mapped`, `executable`,
`entered`).

Three properties matter for anyone reading this table:

1. **It is not the display journal.** `live_trading_journal` is pruned to a
   rolling 10,000 rows; candidate contexts have their own, much larger bound and
   age out unentered rows before entered ones, so observed-reward rows survive
   longest.
2. **Propensities are degenerate.** `propensity_source` is
   `deterministic_policy`: the saved policy always selects the same contract
   from the same ranked list, so `selection_propensity` is 1 for the attempted
   candidate and 0 for the rest. That is recorded honestly and explicitly.
   Inverse-propensity and doubly robust return estimates are **not** identified
   from these logs. A future controlled-exploration mode must write a different
   `propensity_source` so the two regimes can never be pooled by accident.
3. **Rejected candidates have no reward.** The log fixes the *frequency*
   question — a looser filter can now show that it would have qualified more
   contracts — but it does not create counterfactual P/L. Nothing downstream may
   infer the return of a trade that was never placed.

The dry-run wipe deliberately leaves this table intact, matching the journal's
existing "preserve audit data" behaviour.

## Execution journal retention

`live_trading_journal` is a rolling window pruned to 10,000 rows. Pruning now
skips `entry`, `exit`, `settlement`, and `safety` rows alongside the existing
`performance_reset` and `risk_session_reset` exemptions. Those kinds are the
audit trail for real orders and the fallback opportunity history the advisor
reads for trades that predate the candidate log; the high-volume `mark` and
`qualification` chatter ages out instead.

Reporting paths must not have side effects on this evidence. In particular
`_current_policy_session()` opens a session when none exists, so status and
reporting callers use `_open_policy_session_id()`, which returns `None` rather
than fabricating a session that never traded. Only an actual fill, or saving a
policy, opens one.

Order-audit kinds are exempt from the ordinary cap, so the table can exceed
10,000 rows once they dominate. A separate `protected_ceiling` backstop keeps
even those bounded; at roughly two rows per managed trade it allows tens of
thousands of trades before the oldest audit rows age out.

### Measured cost of the previous retention order (2026-07-31)

The exemption was added after measuring what the old order had already
destroyed:

| Store | Managed positions | Surviving `entry` rows | Trades with no entry row |
|---|---:|---:|---:|
| Live | 222 | 47 | 175 (79%) |
| Dry run | 342 | 15 | 327 (96%) |

At the time of measurement 9,171 of the live journal's 10,030 rows were
`qualification` records. That chatter evicted the order audit trail, and the
damage was not limited to display: `_backfill_position_entry_context` recovers
`entry_signal_edge`, `entry_signal_quality`, and `entry_reference_sources` for
older positions **from those entry rows**. With them gone, 61 of the 196 priced
live closes (31%) permanently lack the signal metadata the advisor requires, so
the usable sample is 135 trades rather than 196.

The decision ledger cannot recover them either: `decision_marks` is itself
capped and none of the 61 decision identifiers remain in it. This evidence is
unrecoverable. The retention change prevents further loss; it cannot undo this.
Any comparison against the July 2026 audit should treat that audit's counts as
the larger, pre-loss sample.
