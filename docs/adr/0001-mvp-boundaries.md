# ADR 0001: Python + DAP + a single worker for the MVP

Status: accepted for the Python bootstrap; language scope amended: Python was the first alpha, and Java, C, C++ and Rust joined the minimum product scope. All five are delivered, as experimental ([ROADMAP.md](../ROADMAP.md)).

## Original bootstrap decision

Use debugpy through DAP, a small Python package, a single-owner loopback worker, a JSON HTTP client, and SSH for remote transport. Keep the model provider behind an injected decision function. Validate standalone candidate edits with a fixed independent verifier in a fresh process.

## Why

This preserves the proven runtime experiment while avoiding three debugger adapters and a cluster control plane before the session contract is reliable. Python and DAP offer an executable baseline for conformance tests. Single-session ownership makes stop IDs, cleanup, and timeout semantics reviewable. Private wheel artifacts were sufficient for distributing the MVP.

## Alternatives

- Scraping pdb/gdb terminal output: quick demo, brittle framing/state; rejected for the core.
- A generic plugin registry immediately: no second adapter to validate abstractions; deferred.
- Kubernetes gateway immediately: requires identity, deployment permissions, relay/reconnect and data policy; deferred.
- Live model required CI: nondeterministic latency, credentials and cost; manual evaluation instead.
- Public PyPI release: unnecessary for the private MVP; deferred.

## Consequences and scope amendment

The current Python alpha cannot debug the four additional languages yet, attach everywhere, or claim secure production isolation. Java, C, C++, and Rust are now required incremental MVP slices, rather than optional post-MVP extensions. The HTTP contract is pre-stable until machine-readable error codes and conformance tests land. Remote verification initially runs on the client, with matching dependencies required. CI speed is measured; four-minute job cutoffs are a guardrail, not a performance result.
