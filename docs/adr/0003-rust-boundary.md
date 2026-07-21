# ADR 0003: Rust engine boundary

Status: accepted with review trigger.

The Rust kernel remains because it is already the tested policy implementation.
Python constructs one compact canonical current snapshot, supplies explicit
`as_of`, and hashes the exact JSON. Rust must not read wall time. The boundary is
versioned and rejects non-finite/out-of-domain values. A Python reference and
benchmark are still required before adding new numerical models; lack of a
measured advantage is a trigger to simplify back to Python.
