# Database migrations

Runtime migrations are immutable component/version/checksum units recorded in
`schema_migrations`. Existing SQLite and PostgreSQL databases are upgraded in
place; migration checksum drift aborts startup.

The SQL snapshots in the dialect directories document the cross-store ledger.
Compatibility column additions are performed through database introspection so
the same application release can upgrade databases created by pre-ledger builds.
