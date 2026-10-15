# Incremental MVP plan

The first alpha is Python. The minimum product MVP includes Python, Java, C, C++, and Rust, delivered incrementally. See [language priorities and acceptance criteria](LANGUAGES.md).

## Slice 0 — repository foundation (this bootstrap)

Package under src/peeky, private GitHub repository, contributing guide, design and threat model, adversarial review record, parallel deterministic CI, private wheel delivery.

Acceptance: installed package imports; worker/agent help works; unit and real-debugpy integration pass; wheel tested outside source tree; all required jobs start independently; first hosted run timings recorded.

## Slice 1 — reliable worker (MVP)

Launch/break/inspect/mutate/continue/close, session and stop identity, bounded framing/output, separate control events, uncertain-failure cleanup, child credential stripping, source fingerprint.

Bootstrap and subsequent slices address bounded DAP pipe-write deadlines, second-stop stale-handle rejection, stalled-pipe cleanup and failed-launch responses. Remaining acceptance expansion: exception-stop coverage, consistent machine-readable error codes, concurrent close/wait behavior, and broader monotonic event sequencing tests.

Exit criterion: a state-transition/failure matrix is covered with deterministic transports plus real debugpy. No successful mutation retry after a lost reply. No owned debuggee remains after close in the supported Linux configuration.

## Slice 2 — reproducible investigation (MVP)

Provider-injected bounded loop, evidence reports, standalone candidate edits, trusted verifier completion, unchanged-hash acceptance, clean-process verification, budget/failure reporting.

Bootstrap supplies the loop and regressions. Remaining acceptance: ten varied bugs with fixed independent checkers; distinguish insufficient evidence, no reproduction, no fix and verified-candidate results; preserve reports after every failure mode; demonstrate on a user-selected non-synthetic Python program.

Exit criterion: each successful example includes a passing checker and exact source diff; wrong/overfit candidates and premature exits are rejected. A manual live-model evaluation records model, success rate, actions, runtime and cost/usage where available.

Status (2026-10-15, `feat/corpus`):
- **Done:** ten bugs in `examples/corpus/`, each with an independent checker, a general fix and an overfit (`tests/integration/test_corpus.py`); distinct `no_reproduction` / `insufficient_evidence` / `no_fix` / `budget_exhausted` / `verified` outcomes.
- **Done:** a live evaluation with three agents, all of which reached verified general fixes ([evaluation](evaluations/2026-10-15-agent-cli.md)). Its ten usability findings are fixed.
- **Remaining:** the user-selected non-synthetic program.
- **Deferred usability work:** `--set-on-hit`, names in scope per line in `describe`, negative selector indexes, and probe rows grouped by hit.

## Slice 3 — remote developer workflow

Document/run SSH-tunneled client and worker on separate machines, validate matching source, explicit disconnect behavior, target interpreter configuration, CLI ergonomics, artifact portability.

Exit criterion: automated two-process network tests plus one recorded separate-host run. No required cluster permissions. Verification execution location and dependency requirements are explicit.

## Language slices — part of the minimum MVP

1. Extract adapter and build/verification boundaries while preserving Python conformance.
2. Java headless adapter integration, launch/inspect/primitive mutation and compiled-fix verification.
3. Shared native adapter with C as the first fixture.
4. C++ language-specific fixtures and rebuilt-candidate verification.
5. Rust language-specific fixtures, formatters, Cargo verification and capability reporting.

Close common lifecycle blockers before adding Java; the entire Python corpus is not a prerequisite. Each slice gets adversarial review, real-adapter conformance tests, and a parallel CI lane before claiming support.

## Usability direction — stateless probes (ADR 0004)

Support both stateful sessions and stateless one-request probes over the same guarded engine, with a heappy-style experience: one command per question, a `describe` manifest, canned probes and SQL over captured evidence. Session mode stays unchanged. Probe mode is planned, not started. Until it lands, new worker features should be expressible as declared probe steps rather than long-lived client-held state. See [ADR 0004](adr/0004-stateless-probes.md).

## After the five-language MVP

Broader multi-file autonomous patching, Kubernetes workers/outbound relay, multi-tenant authorization and production data policies. Minimal explicit build/workspace recipes needed for compiled-language verification belong in the language slices, not this deferred list.


## Tracked MVP work

- [Worker lifecycle and failure conformance #6](https://github.com/sushantmane/peeky/issues/6)
- [Investigation corpus and outcome reporting #7](https://github.com/sushantmane/peeky/issues/7)
- [Separate-host SSH developer workflow #8](https://github.com/sushantmane/peeky/issues/8)

These foundation issues belong to the MVP milestone. Language issues extend that milestone; Kubernetes remains deferred.

### Tracked language slices

- [MVP P1: extract debugger and build/verification boundaries](https://github.com/sushantmane/peeky/issues/9)
- [MVP P2: Java debugger integration and verified compiled fixes](https://github.com/sushantmane/peeky/issues/10)
- [MVP P3: native adapter and C conformance](https://github.com/sushantmane/peeky/issues/11)
- [MVP P4: C++ language conformance on native adapter](https://github.com/sushantmane/peeky/issues/12)
- [MVP P5: Rust language conformance and Cargo verification](https://github.com/sushantmane/peeky/issues/13)
