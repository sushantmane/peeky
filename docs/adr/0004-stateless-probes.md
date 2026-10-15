# ADR 0004: Stateless probes alongside debugger sessions

Status: accepted direction (owner, 2026-10-14); interface unified by [ADR 0005](0005-tool-not-agent.md) (`peeky probe`, `peeky session`, `peeky describe`). Steps 1–3 are delivered experimentally: in-process `run_probe`, the `peeky-probe` CLI, a single-flight HTTP `probe` operation that runs in an isolated separate process: SIGTERM at 35 s so the probe closes its debugger and debuggee, SIGKILL at 40 s, below the 45 s client timeout and `Client.probe`. Step 3 is `peeky describe`. Steps 4–5 (query grammar with captured-evidence store, measured build caching) remain. Both modes are product requirements; session mode is unchanged.

## Context

Peeky today is session-oriented: `launch` → `wait` → `inspect` → `expand` → `set_variable` → `continue` → `report` → `close`, with session/stop identities and opaque inspection handles. That is right for incremental exploration, but every question costs several round-trips and the caller must manage lifecycle, handle revocation and cleanup.

The owner prefers stateless operation where possible and wants Peeky to be as easy to use as heappy, where one command answers one question, a manifest tells the agent what exists, canned queries need no syntax, and SQL works over captured data. ("Parse once, query forever" becomes "capture once, query the evidence".)

## Decision

Support two first-class modes over one guarded engine:

1. **Session mode** (existing): stateful, incremental, interactive. Unchanged.
2. **Probe mode** (new): one self-contained request is one complete transaction. Prepare a fresh build, launch, stop at declared locations, capture declared values, apply optional declared interventions, run to exit or a bound, return evidence and clean up. No session ID, stop ID or handle crosses the boundary. A retry is a whole new run, so retrying is safe; an uncertain mutation is never retried inside a live session.

Probe mode is a composition of the existing worker operations inside the worker process, not a second debugger implementation. Every invariant applies unchanged: fresh build and provenance checks, stop/module validation before each read and write, the same mutation rules, bounded output/time, fail-closed handling and cleanup on every path. Runtime intervention is still evidence only, never a verified fix.

## Interface sketch

The forms below are illustrative; exact syntax will be settled when implemented.

```sh
# One question, one command
peeky probe examples/rust --at src/main.rs:36 --capture start,some,request.page

# Hypothesis test: same capture, intervene, observe the outcome
peeky probe examples/rust --at src/main.rs:36 --capture start --set start=0

# SQL-shaped form compiled to the same typed request
peeky query "SELECT start, some, request.page FROM 'src/main.rs':36 LIMIT 3 SET start = 0"

# What can I ask? Target identity, valid probe lines, qualified views,
# mutable types and canned probes, in one manifest
peeky describe examples/rust
```

Results are rows: `run_id`, `hit`, `frame`, `path`, `type`, `value`, `view` and outcome/output columns, printed as a table or JSON lines and optionally saved, for example to SQLite through the standard library. Follow-up questions about **captured** evidence are ordinary SQL over that store. New runtime questions mean a new probe. The same request is an HTTP `probe` operation and a single SDK call.

Canned probes cover common questions without syntax, for example locals at a line, a value across all hits, first divergence between two inputs, and before/after one intervention.

## Constraints

- Selectors are dotted paths (`request.page`, `args[0]`) resolved through the existing bounded read-only expansion with frame/scope/ancestry checks. Enums use qualified views, and unqualified values stay opaque. No expressions are evaluated in the debuggee.
- The query language is a small parser producing the typed probe request. It is never passed to a database engine connected to the debuggee or to the debugger. SQL runs only over captured, already-bounded evidence.
- Bounds on hits, rows, nodes, wall time and output are explicit request fields with conservative defaults. Selectors beyond the bounded expansion window are reported as truncated, not as missing. A probe that never reaches its line returns `no-stop` and fails; a stop anywhere else (crash, signal, other location) fails closed before capture or intervention. Program output keeps its existing bounded capture and truncation flag.
- Statelessness costs a fresh prepare per probe. Measure before optimizing (ADR 0003). Any content-addressed prepared-build cache must key on the full build manifest (inputs, recipe, tool hashes, environment) and re-run `assert_current` before reuse. It must not weaken fresh-candidate verification.
- Evidence rows carry `artifact_manifest_id` and the ADR 0002 identities so captured data stays attributable.

## Plan

1. In-process `run_probe(target, request)` built from existing worker operations, with failure/cleanup regressions (Python first, then each qualified language).
2. HTTP `probe` operation, SDK call and `peeky probe` CLI.
3. `peeky describe` manifest and canned probes.
4. Query grammar and captured-evidence store.
5. Measured prepared-build cache, if probe latency requires it.

Until then, new worker features should be expressible as a declared probe step (location, selector, bounded intervention) rather than depending on long-lived client-held state.

## Open questions

- Hit semantics: first hit, the first N hits, or all hits up to a bound. Ordering across multiple locations.
- Whether captured-evidence storage defaults to SQLite (standard library) or an optional DuckDB/Parquet export to match heappy.
- Whether probe mode runs candidate verification in the same request or as a separate stateless `verify` call.
