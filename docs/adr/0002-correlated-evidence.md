# ADR 0002: Correlate debugger evidence with code, logs, and metrics

Status: accepted direction; incremental implementation proposed. This ADR does not claim the current Python alpha implements the event schema, metric collection, redaction, or latency targets below.

## Context and decision

An investigation should explain which code was running, what the debugger actually observed, which intervention occurred, and which subsequent behavior changed. Logs and metrics can support that explanation, provided their identities, collection boundaries, missing data, and timing uncertainty remain visible. A timestamp coincidence alone must not become a causal claim.

Add a small versioned evidence envelope to the existing reports first. Correlate records locally through explicit IDs and artifact manifests. Add optional imported logs and metrics after the envelope is tested. Do not introduce a tracing SDK, telemetry backend, background network exporter, or automatic application instrumentation in this slice. Collection and report generation must remain useful offline.

This extends the session and verification contracts in [DESIGN.md](../DESIGN.md) and the source/build requirements in [ROADMAP.md](../ROADMAP.md#what-a-language-needs-before-peeky-claims-it). It does not replace debugger capabilities or provide replay.

## Identities and provenance

| Field | Meaning and lifetime |
| --- | --- |
| `schema_version` | Version of the persisted envelope; readers reject unsupported major versions rather than guessing semantics |
| `investigation_id` | Random ID created by the controller for one investigation; joins its baseline, interventions, candidates, and verifications |
| `run_id` | A new random ID for every process execution, including baseline, debug launch, and candidate verification; identifies a specific run, not a program name |
| `worker_instance_id` | Random ID per worker start; a restarted worker cannot reuse its previous clock domain or sequence |
| `session_id` | Existing debugger-session identity; absent for executions without a debugger |
| `stop_id` | Existing paused-state identity; required for observations and mutations involving frame/scope handles; invalid after continue |
| `operation_id` | ID for one logical client request; correlates send, worker receipt, adapter response, and client result; does not authorize replay or make mutation retry safe |
| `event_id`, `sequence` | Producer ID plus strictly increasing sequence; deduplicates imported records and detects missing ranges within that producer |
| `parent_event_id` | Explicit relationship, such as a mutation result referring to its request; not an assertion that unrelated application behavior was caused by it |
| `artifact_manifest_id` | Digest of a canonical source/build manifest; links an observation or verification to the exact inputs and executable artifacts |
| `trace_id`, `span_id` | Optional IDs imported from an application-supported tracing context; preserve the provider and validation status, and omit when unavailable |

Require a known `investigation_id` and `run_id` for Peeky-owned data. External records with no trustworthy run mapping remain `unattributed`; do not infer ownership from PID or approximate time. PIDs, thread IDs, pod names, and paths are useful attributes but are reusable and are not primary identities. DAP frame/scope references remain transient values scoped to a stop and must never be used as stable evidence IDs.

The controller sends the run and operation IDs explicitly when the future protocol supports them. A worker records both the request and its result, including failures. An uncertain mutation has an `unknown` outcome, not a synthetic success or failure. If a client loses the result, preserving its operation ID helps diagnosis; it still closes the session instead of retrying the mutation.

The manifest records source paths relative to a declared workspace, file digests, source revision when present, and whether the workspace is dirty. A commit SHA alone is insufficient. For Python, identify the interpreter, relevant environment/lock metadata, and the selected source snapshot; do not claim that one entrypoint digest identifies every imported dependency.

For Java/C/C++/Rust, record the build recipe, toolchain versions, build flags, source/workspace fingerprint, executable/class artifacts, and debug-symbol identities together. Record loaded-artifact identity where the adapter exposes it. A successful build plus matching source files is provenance evidence; it is not proof of the loaded executable if that identity cannot be checked. Missing or mismatched required identities block mutation and verified-candidate acceptance as specified by the language recipe. Do not collect unrestricted environment variables or command-line secrets in the manifest.

## Time and ordering

Each producer records `monotonic_ns`, UTC `wall_time`, and a `clock_domain_id` for its own process lifetime. Monotonic values measure intervals only within that domain. Never subtract a controller timestamp from a worker timestamp or compare monotonic values across restarts/hosts.

Record both event receipt and observation where available. For example, `adapter_event_received` and `stop_consumed` describe different instants: the worker's receipt time is not the exact moment the target paused, and the later controller inspection is not the same as either. If an application log provides its own timestamp, retain that separately from `collected_at`; do not silently substitute one for the other.

Producer sequence establishes local order. Request/result IDs and explicit parent relationships establish partial order across components. Wall time is for display and approximate cross-host alignment. Clock offsets/uncertainty may be recorded if measured, but unknown uncertainty stays unknown. The report must not fabricate a total ordering for concurrent or buffered external records.

A log line with an explicit matching run or trace context can be linked directly. A line that only falls within a wall-time interval may be shown as a nearby candidate, with `match_basis: time_window` and the uncertainty. It must not be presented as a confirmed event from that request. Paused execution changes scheduling and latency; tag debugged runs and pause intervals so application metrics are not mistaken for normal production performance.

## Record shape and data budget

Start with explicit record kinds: `run_started`, `operation_requested`, `operation_result`, `stop_observed`, `runtime_snapshot`, `intervention`, `target_output`, `verification_result`, and `run_finished`. Keep structured status and references separate from rendered text. An observation records the requested and actual breakpoint location and its source/artifact identity.

Persist bounded NDJSON records with a small summary in the investigation report. Critical records include identity, operation status, intervention outcome, provenance, and verification completion. Runtime values and target output are best-effort attachments. Never let an output flood silently remove critical records.

Proposed initial limits, to be configurable within hard worker caps:

- Maximum serialized record size: 16 KiB, measured in encoded bytes. Truncate individual values before serialization and retain an explicit truncation marker and original size when known.
- Optional evidence buffer: 1 MiB per active run. No unbounded queue or recursive object expansion; collect the requested frame/scope only.
- Persisted optional attachments: 8 MiB per investigation. Drop optional payloads after the cap and keep dropped-record/byte counts plus affected sequence ranges where known.
- Critical journal: 512 KiB per investigation. Reserve space for terminal status and loss metadata. If critical evidence cannot be retained, stop accepting new interventions and return `evidence_incomplete`; do not label the investigation fully verified.

These are future evidence-format budgets, not a description of the existing DAP frame/output limits. Budget boundary behavior needs tests, including multibyte text and burst output. Persisted truncation and loss must be visible to the model and human reader. A passing checker can still be reported accurately when optional logs were dropped; qualify the missing evidence instead of treating optional logging as part of the correctness oracle.

Redact at the worker before optional values are persisted, transmitted, or sent to a model. Strip credentials from child environments as today. Add explicit allowlists for collected fields and configurable key/value redactors for optional logs; replace sensitive values with a typed redaction marker. Avoid collecting secrets in the first place. Do not hash low-entropy secrets as a substitute for redaction. Paths should be workspace-relative where possible. Record redaction-policy version and redaction counts, not the removed contents.

Redaction cannot guarantee arbitrary application text is safe. Default optional collection to off, explain that explicitly enabled values/logs may contain application data, and preserve the current trusted-development-target boundary. Do not add remote export by default. If a required provenance field is redacted or unavailable, record that fact and apply the recipe's acceptance rule instead of inventing identity.

## Correlation and causal statements

Separate observations from hypotheses and conclusions in the report:

1. **Observed:** the selected frame contained a value, an intervention request returned a result, or a checker completed for an identified candidate.
2. **Correlated:** explicitly identified logs/metrics changed during a corresponding run or trace; include matching basis and missing-data flags.
3. **Intervention evidence:** changing a specific runtime value was followed by a changed result in the controlled experiment. Note other changed inputs and nondeterminism; this supports a hypothesis rather than proving it generally.
4. **Verified candidate:** the unchanged supplied checker completed successfully for an identified candidate in a fresh execution, with provenance/integrity gates satisfied. This establishes only the checker's tested behavior.

Preserve baseline and intervention as separate runs. A useful comparison states what input, artifact, environment, and debugger configuration stayed the same, and what changed. An unobserved external dependency or scheduling difference remains a limitation. Never treat a latency decrease in a paused/debugged run as a performance improvement without a separate non-debugged measurement.

## Useful measurements and overhead targets

Collect counters and elapsed durations at existing operation boundaries, without polling target memory or adding per-line instrumentation. Labels must have bounded cardinality: operation, adapter/language, outcome, and error class. Keep unique run/session/trace IDs in evidence records, not as metric label dimensions.

Measure controller end-to-end operation time, worker queue/dispatch time, adapter round-trip time, stop-receipt-to-inspection time, verified-fix build/check time, cleanup time, timeouts, uncertain mutations, dropped/redacted bytes, and evidence encoding/write time. Report model latency and token/cost information separately when the provider supplies it. A remote client's total duration includes network delay; do not derive an exact network duration by subtracting clocks from different hosts.

Initial proposed acceptance targets on the same declared Linux runner and pinned fixture/toolchain:

| Measurement | Proposed target | Method |
| --- | --- | --- |
| Evidence processing per small operation record | p95 <=1 ms, p99 <=5 ms | At least 1,000 <=1 KiB synthetic records; measure serialization, redaction, and enqueue together with a monotonic clock |
| Local deterministic investigation overhead | <=5% median and <=10% p95 versus optional collection off | At least 30 paired runs after warm-up; same fixture/build/adapter, alternate on/off order; report absolute time and variability |
| Buffered evidence memory | <=1 MiB optional payload per run, plus documented fixed metadata | Stress with large/noisy values and validate actual counters and peak memory; do not hide Python object overhead in the reported total |
| Export/backpressure | No network exporter on required operation path; bounded local persistence | Slow/failing sink test must preserve critical capacity or end with explicit incomplete evidence |

These are hypotheses to measure, not achieved promises. Report hardware, OS, cold/warm state, sample count, debugger overhead separately, and observed failures. The first deterministic benchmark should establish a baseline; tune budgets from evidence rather than weakening correctness to meet a number. A sub-millisecond timer observation is not itself proof of low application overhead.

Keep the required PR test limited to deterministic schema/ordering/loss/redaction checks and one bounded benchmark smoke. Run the 30-pair performance assessment manually or on a separate scheduled lane until variance is understood. It must not lengthen the parallel required-CI critical path beyond the existing <5-minute execution target.

## Incremental delivery and acceptance

1. **Identity and provenance:** add the versioned envelope and run/operation IDs around existing evidence. Test cross-run isolation, repeated operation IDs, stale stops, clock restart, and missing/mismatched source/artifact manifests. Retain existing report fields during a documented migration.
2. **Bounded local log correlation:** attach existing target stdout/stderr with collection sequence, byte budgets, redaction policy, and explicit attribution. Test noisy output without lifecycle loss, missing data, UTF-8 boundaries, and secret fixtures. No tracing dependency or target instrumentation.
3. **Durations and measurements:** add operation counters/durations and a paired benchmark harness. Compare enabled/disabled capture, record distributions, and publish measured overhead with environment details.
4. **Optional external context:** accept explicitly supplied trace/span IDs and bounded log/metric snapshots through a versioned import format. Test unrelated-run contamination, clock skew, PID reuse, duplicate events, and partial traces. A backend connector or tracing SDK requires a separate decision and concrete user need.

Each slice must preserve the language conformance suite and include an example report with claims a reviewer can check against the raw records. Reconnection, distributed replay, live production metrics backends, and automatic instrumentation remain outside this decision.
