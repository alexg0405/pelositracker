# ADR 0004: Deployment topology

Status: accepted.

The immediate supported topology is one FastAPI process with one collector and
decision owner. Startup rejects `WEB_CONCURRENCY != 1` in production. This
matches process-local event locks, feed tasks, SSE subscribers, sessions, and
terminal markers. A future multi-process design must introduce durable messages
and PostgreSQL leases before horizontal scaling is advertised.
