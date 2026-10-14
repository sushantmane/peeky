# Incremental MVP plan

The first release is a useful Python debugging tool, not a general autonomous SRE platform.

## Slice 0 — repository foundation (this bootstrap)

Package under src/peeky, private GitHub repository, contributing guide, design and threat model, adversarial review record, parallel deterministic CI, private wheel delivery.

Acceptance: installed package imports; worker/agent help works; unit and real-debugpy integration pass; wheel tested outside source tree; all required jobs start independently; first hosted run timings recorded.

## Slice 1 — reliable worker (MVP)

Launch/break/inspect/mutate/continue/close, session and stop identity, bounded framing/output, separate control events, uncertain-failure cleanup, child credential stripping, source fingerprint.

Bootstrap addresses the identified blockers. Remaining acceptance expansion: bounded DAP pipe-write deadlines, second-stop stale-handle tests, exception-stop coverage, disconnect under load, consistent machine-readable error codes, concurrent close/wait behavior, and monotonic event sequencing tests.

Exit criterion: a state-transition/failure matrix is covered with deterministic transports plus real debugpy. No successful mutation retry after a lost reply. No owned debuggee remains after close in the supported Linux configuration.

## Slice 2 — reproducible investigation (MVP)

Provider-injected bounded loop, evidence reports, standalone candidate edits, trusted verifier completion, unchanged-hash acceptance, clean-process verification, budget/failure reporting.

Bootstrap supplies the loop and regressions. Remaining acceptance: ten varied bugs with fixed independent checkers; distinguish insufficient evidence, no reproduction, no fix and verified-candidate results; preserve reports after every failure mode; demonstrate on a user-selected non-synthetic Python program.

Exit criterion: each successful example includes a passing checker and exact source diff; wrong/overfit candidates and premature exits are rejected. A manual live-model evaluation records model, success rate, actions, runtime and cost/usage where available.

## Slice 3 — remote developer workflow (MVP completion)

Document/run SSH-tunneled client and worker on separate machines, validate matching source, explicit disconnect behavior, target interpreter configuration, CLI ergonomics, artifact portability.

Exit criterion: automated two-process network tests plus one recorded separate-host run. No required cluster permissions. Verification execution location and dependency requirements are explicit.

## After MVP — independent extensions

1. Java adapter + the same adapter/session conformance suite.
2. GDB/MI or native DAP adapter; report capabilities rather than emulate unsupported jumps.
3. Multi-file patch workspace and test command orchestration.
4. Kubernetes worker installation and authenticated outbound relay.
5. Multi-tenant authorization and data egress policy, followed by production modes.

Do not implement these while the MVP acceptance gaps remain. Each must have its own design review and measurements.


## Tracked MVP work

- [Worker lifecycle and failure conformance #6](https://github.com/sushantmane/peeky/issues/6)
- [Investigation corpus and outcome reporting #7](https://github.com/sushantmane/peeky/issues/7)
- [Separate-host SSH developer workflow #8](https://github.com/sushantmane/peeky/issues/8)

All three belong to the MVP milestone; later-language and Kubernetes work stays deferred.
