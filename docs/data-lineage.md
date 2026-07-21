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
5. The SHA-256 decision hash and selection-specific decision ID identify the
   resulting decision. `decision_marks` persists the full canonical request and
   lineage even when the policy output is `WATCH`.
6. A `PAPER_BET` can create a paper order/fill only when requested and filled
   size are positive. Full ask depth and the declared fee schedule determine
   VWAP and effective price.
7. `close_marks` continuously retain only valid tradable observations and are
   frozen on suspension/finalization. Settlement is a separate idempotent mark;
   it never supplies the closing price.

Replay orders original observations by recorded time and stable row ID, stops
conservatively at terminal timestamp ties, and passes the original tick time as
`as_of`. It never rebases provider evidence to the replay machine's wall clock.
