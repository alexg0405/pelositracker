CREATE TABLE IF NOT EXISTS schema_migrations (
    component TEXT NOT NULL,
    version INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    applied_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (component, version)
);
