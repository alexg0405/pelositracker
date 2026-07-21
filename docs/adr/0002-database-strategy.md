# ADR 0002: Database strategy

Status: accepted.

PostgreSQL is the supported production store; SQLite remains for local and
isolated tests. Each repository owns a component-scoped sequence in the shared
`schema_migrations` ledger. Schema and migration record commit together, and a
stored checksum must match before a migration is accepted as already applied.
Old evidence is never reinterpreted; new lineage columns are nullable.
