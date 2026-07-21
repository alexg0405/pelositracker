# Remediation milestones

The milestones are implemented sequentially. Each milestone must leave the paper-only invariant intact and add regression evidence before the next one begins.

## A — Contracts and fail-closed inputs

Typed settings, canonical vocabulary, timestamp provenance, league clock rules, state validation, disabled undocumented providers, and golden provider fixtures.

## B — Identity, migrations, history, and replay

Versioned schema migrations; canonical event/participant/market/outcome identities; mapping decisions and quarantine; lossless observation history; explicit `as_of`; deterministic decision hashes and replay parity.

## C — Executable books and paper lifecycle

Bulk books, snapshot/delta state machine, gap recovery, Decimal depth walking, fee lineage, partial-fill policy, paper order/position lifecycle, decision/close/settlement marks, and corrected CLV.

## D — Security and operations

Per-session authentication, CSRF/rate limits/security headers, webhook SSRF controls, strict static assets, tracked tasks, single-owner guards, readiness/health, reproducible container, and expanded CI.

## E — Consensus, calibration, uncertainty, and risk

Source-family aggregation, calibration artifacts, uncertainty dimensions, net-EV policy gates, exposure controls, and outcome-level explainability.

## F — Validated models only

Enable only sport/market models with versioned out-of-sample evidence. Unsupported combinations remain display-only and are documented explicitly.
