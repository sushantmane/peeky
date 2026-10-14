# Initial adversarial review

Date: 2026-10-14. Two independent reviewer agents examined the prototype before the repository bootstrap: architecture/runtime failure handling, and testing/verification/CI. A third agent independently prepared and validated package/workflow configuration. These are AI-assisted engineering reviews, not an external security audit.

## Findings and dispositions

| ID | Severity | Original finding | Bootstrap disposition / regression |
| --- | --- | --- | --- |
| A1 | P1 | Stdout events could evict stopped/terminated events; frames unbounded | Separate output and control buffers; framing limits; output-flood and malformed-frame tests |
| A2 | P1 | Lost mutation/continue reply left session reusable | Fail closed and clear session/stop IDs; injected uncertain-mutation tests |
| A3 | P1 | Worker token inherited by debuggee/verifier | Remove DEBUG_TOKEN from owned child env; synthetic token checks |
| V1 | P1 | Candidate SystemExit(0) bypassed independent checks | Trusted completion wrapper catches BaseException and requires completion; exit0/os._exit regressions |
| V2 | P1 | Process timeout/close could leave children | POSIX process-group ownership/cleanup; descendant and real debuggee tests |
| V3 | P2 | Source/check integrity only annotated report | Gate candidate acceptance on original/check hashes; checker mutation regressions |
| A4 | P2 | Report had no session identity | Require session ID for report; stale report tests |
| A5 | P2 | 30-second wait held controller lock | Short wait calls with bounded client polling; deterministic polling test |
| V4 | P2 | Seven tests covered only two happy-path programs | New framing/fault/verification/process tests; installed-wheel smoke; still synthetic corpus |
| V5 | P2 | Temporary subprocess logs grew without bound | Drain bounded output; terminate on output budget; cap retained evidence |

## Cross-review

Reviewers then inspected each other's fixes and the design. Follow-up questions covered baseline-error report persistence, stale-session cleanup masking original errors, candidate self-modification, and truthful timeout/remote guarantees. The final validation records which checks pass; unresolved product-scale features remain in ROADMAP.md.

## Explicit residual limits

- Trusted programs can execute code and access the worker's OS permissions. No sandbox or multi-tenant isolation.
- The verifier shares a process with candidate code. Completion detection handles demonstrated early-exit failures; it is not tamper-proof against hostile code.
- Process-group cleanup does not cover a child deliberately escaping its group.
- Required CI is Linux-only initially. SSH across separate hosts and Kubernetes are not covered by local network tests.
- Live-model success on synthetic examples is not a broad reliability result. Build a varied bug corpus before claiming general accuracy.
- DAP pipe writes are synchronous; an adapter that stops reading can block a send before its response deadline starts. Bound send/write handling in the worker reliability slice before broader remote use.
- Required CI execution target is <5 minutes, measured on hosted runs; queue time cannot be guaranteed.

No P1 finding is waived as a production guarantee. The MVP boundary and regression tests determine readiness, not the number of review passes.
