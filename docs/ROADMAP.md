# Roadmap

Peeky's minimum product, the MVP, covers five languages: Python, Java, C, C++ and Rust. Python was
the first alpha. The other languages followed, one slice at a time.

The tracked MVP issues, #6 to #13, are delivered and closed. Java, C, C++ and Rust on a separate
host followed, in [#248][i248] and [#252][i252]. This page lists what each slice delivered, with links, then what comes next and what
stays deferred.
[LANGUAGES.md](LANGUAGES.md) has the language priorities and acceptance criteria.

## What is delivered

| Slice | Issue | Closed | Read about it |
|---|---|---|---|
| 0. Repository foundation | none | — | [DESIGN](DESIGN.md), [TESTING](TESTING.md) |
| 1. Reliable worker | [#6][i6] | 2026-10-15 | [ERRORS](ERRORS.md), [DESIGN](DESIGN.md) |
| 2. Reproducible investigation | [#7][i7] | 2026-10-15 | [Python](lang/python.md) |
| 3. Remote developer workflow | [#8][i8] | 2026-10-16 | [REMOTE](REMOTE.md) |
| P1. Debugger and build boundaries | [#9][i9] | 2026-10-15 | [DESIGN](DESIGN.md) |
| P2. Java | [#10][i10] | 2026-10-15 | [Java](lang/java.md) |
| P3. A shared native adapter, and C | [#11][i11] | 2026-10-15 | [C and C++](lang/c-cpp.md) |
| P4. C++ | [#12][i12] | 2026-10-16 | [C and C++](lang/c-cpp.md) |
| P5. Rust | [#13][i13] | 2026-10-16 | [Rust](lang/rust.md) |
| Stateless probes | none | — | [ADR 0004][adr4], [COOKBOOK](COOKBOOK.md) |

[i6]: https://github.com/sushantmane/peeky/issues/6
[i7]: https://github.com/sushantmane/peeky/issues/7
[i8]: https://github.com/sushantmane/peeky/issues/8
[i9]: https://github.com/sushantmane/peeky/issues/9
[i10]: https://github.com/sushantmane/peeky/issues/10
[i11]: https://github.com/sushantmane/peeky/issues/11
[i12]: https://github.com/sushantmane/peeky/issues/12
[i13]: https://github.com/sushantmane/peeky/issues/13
[i248]: https://github.com/sushantmane/peeky/issues/248
[i249]: https://github.com/sushantmane/peeky/issues/249
[i252]: https://github.com/sushantmane/peeky/issues/252
[i167]: https://github.com/sushantmane/peeky/issues/167
[adr4]: adr/0004-stateless-probes.md

All eight issues are in the MVP milestone. #6, #7 and #8 are its foundation. The language issues
extend it. Kubernetes stays deferred.

### Slice 0: repository foundation

- **Scope:**
  - the package, under `src/peeky`
  - a private GitHub repository and a [contributing guide](../CONTRIBUTING.md)
  - the [design](DESIGN.md) and the [threat model](THREAT_MODEL.md)
  - an adversarial review record: [INITIAL_REVIEW](reviews/INITIAL_REVIEW.md)
  - parallel, deterministic CI, and private wheel delivery
- **Accepted when:**
  - the installed package imports, and the worker and agent help work
  - the unit tests and the integration tests on real debugpy pass
  - the wheel is tested outside the source tree
  - all required CI jobs start independently
  - the first hosted run's timings are recorded

### Slice 1: a reliable worker (#6)

- **Scope:**
  - launch, break, inspect, change a value, continue and close
  - session and stop identity
  - bounded framing and output, with control events kept separate
  - cleanup after an uncertain failure
  - removing the worker's credential from child processes
  - the source fingerprint
- **Exit criteria:**
  - Deterministic transports and real debugpy cover a matrix of state transitions and failures.
  - No change succeeds on a retry after its reply was lost.
  - No owned debuggee remains after close, in the supported Linux configuration.
- **Delivered** in the bootstrap, then in [#15][p15] and [#25][p25]:
  - deadlines on DAP pipe writes, and cleanup of a stalled pipe
  - rejecting old handles at a second stop
  - responses to a failed launch
  - exception stops, and close and wait called at the same time
  - consistent, machine-readable error codes and a protocol version: [ERRORS](ERRORS.md)
- **Accepted:** no retryable code follows an uncertain change, and owned processes are reaped. Each
  documented state transition and error has a regression test.
- **Review:** [ERRORS_CORPUS_REVIEW](reviews/ERRORS_CORPUS_REVIEW.md) found nothing blocking. All
  its findings are fixed.
- **Not cited:** the slice also listed broader tests of monotonic event ordering. The note that
  closed #6 doesn't cite one.

### Slice 2: reproducible investigation (#7)

- **Scope:**
  - a bounded investigation loop, with the model provider injected
  - evidence reports and standalone candidate edits
  - completion by a trusted verifier, in a clean process
  - acceptance only if the original source and verifier hashes are unchanged
  - reports of the budget and of failures
- **Exit criteria:**
  - Each successful example includes a passing checker and the exact source diff.
  - Wrong or overfit candidates are rejected, and so are premature exits.
  - A manual evaluation with a live model records the model, success rate, actions and runtime. It
    records cost or usage where available.
  - Reports are kept after every failure mode.
- **Delivered:**
  - Ten varied bugs in `examples/corpus/`. Each has an independent checker, a general fix and an
    overfit fix. The test is `tests/integration/test_corpus.py`.
  - Distinct outcomes: `no_reproduction`, `insufficient_evidence`, `no_fix`, `budget_exhausted`
    and `verified`.
  - A [live evaluation](evaluations/2026-10-15-agent-cli.md) with three agents. All three reached
    verified general fixes, and its ten usability findings are fixed.
  - A program chosen by the user, not synthetic: CPython 3.9.6's `ipaddress`, with the real
    CVE-2024-4032 bug, in `examples/real/ipaddress_mapped` ([#28][p28]). It runs through an
    entry script with `--run`.
  - On that program, a [live agent](evaluations/2026-10-15-real-program.md) verified a fix
    equivalent to upstream's. The checker rejected its partial fix.
  - Usability follow-ups ([#26][p26]): `--set-hit`, Python scopes in `describe`, safe negative
    indexes and `by_hit`.

### Slice 3: a remote developer workflow (#8)

- **Scope:**
  - document and run a client and worker on separate machines, joined by an SSH tunnel
  - check that the source matches
  - explicit behaviour on disconnect
  - configuring the target's interpreter
  - CLI ergonomics, and portable artifacts
- **Exit criteria:**
  - Automated network tests with two processes, plus one recorded run on a separate host.
  - No cluster permissions are required.
  - Where verification runs, and what it depends on, are explicit.
- **Delivered:**
  - [#33][p33]: `--worker`, source matching and the transport behaviour.
  - [#42][p42]: the run on a real second host, with a Python program, and its CI lane.
    [REMOTE.md](REMOTE.md) has the recorded run. `tests/remote/test_ssh_e2e.py` repeats it in the
    CI job "remote SSH e2e".
  - [#248][i248]: Java on the second host, for a launched JVM and an attached one, in the same CI
    job ([REMOTE.md](REMOTE.md#java-on-the-worker-host)).
  - [#252][i252]: C, C++ and Rust on the second host, in the same CI job
    ([REMOTE.md](REMOTE.md#c-c-and-rust-on-the-worker-host)). A remote location in a compared
    source names your checkout; any other path is marked as the worker host's (protocol 3).
- [#249][i249]: before launching, the worker compares the target's sources with the client's,
  not only the breakpoint's file (protocol 2). [REMOTE.md](REMOTE.md#source-identity) lists
  which files count, and what isn't compared.

### Language slices P1 to P5

| Slice | Scope | Delivered in |
|---|---|---|
| P1 (#9) | Extract the adapter and the build and verification boundaries | [#16][p16] |
| | Keep Python's conformance while doing it | |
| P2 (#10) | Java, through a headless adapter | [#16][p16], [#17][p17], |
| | Launch, inspect, change primitives, and verify compiled fixes | [#18][p18] |
| P3 (#11) | A shared native adapter, with C as the first fixture | [#19][p19], [#20][p20] |
| P4 (#12) | C++ fixtures, and verification of rebuilt candidates | [#21][p21], [#32][p32] |
| P5 (#13) | Rust fixtures and formatters | [#22][p22], [#23][p23], |
| | Cargo verification and capability reports | [#30][p30], [#32][p32] |

- The plan was to close common lifecycle blockers before adding Java. The full Python corpus was
  not a prerequisite.
- Build and workspace recipes for compiled-language verification belong in these slices, not in
  the deferred list.
- **Rule:** each language slice gets an adversarial review, conformance tests on the real adapter
  and its own parallel CI lane. Only then does Peeky claim support.
- **C++ built by GCC** ([#167][i167]) followed the same rule. A survey ran every C++ test with g++.
  Then each wrong value it found was fixed or withheld, and the tests became compiler-aware. The
  `cpp-gcc` and `cpp-gcc-llvm20` lanes run every C++ case with g++. Its known gaps are in the
  [cookbook](lang/c-cpp-cookbook.md#what-is-different-in-a-gcc-build).

### Stateless probes (ADR 0004)

- **Direction:** two modes over one guarded engine.
  - Stateful sessions: `peeky session`.
  - Stateless probes, one request each: `peeky probe`.
- **Goal:** as easy as heappy: one command per question, a `describe` manifest, canned queries and
  SQL over captured evidence.
- **Delivered:** steps 1 to 4 of [ADR 0004][adr4], under one interface
  ([ADR 0005](adr/0005-tool-not-agent.md)). Session mode is unchanged.
  - [#25][p25]: `peeky probe`, `peeky describe`, `peeky session` and `peeky verify`.
  - [#35][p35]: `peeky sql` and `peeky queries`, over the saved evidence.
  - [#68][p68]: probing a candidate fix, `probe --old/--new`, on a fresh build.
  - [#83][p83]: several edits per `verify` and per candidate probe.
- **Remaining:** step 5, a prepared-build cache. Build it only if measured probe latency needs it.
- **Design guideline:** keep worker operations composable and declarative. A new worker feature
  should be expressible as a declared probe step: a location, a selector and a bounded change. It
  shouldn't depend on long-lived state held by the client.

## What comes next

### CUDA (#61)

[#61](https://github.com/sushantmane/peeky/issues/61) asks for CUDA kernel debugging, Compute
Sanitizer, Nsight profiling and support for ML workloads.

- **Plan:** [ADR 0007](adr/0007-cuda-gdb-dap.md), proposed. Peeky would drive `cuda-gdb -i=dap`
  through its existing DAP client and worker.
- **Phase 0, next:** on a real GPU host, run `scripts/cuda/qualify.py`. It qualifies
  `cuda-gdb -i=dap`: GDB 14.2 or later, inside cuda-gdb 12.9 or later. Then debug a known kernel
  bug end to end.
- **Phase 1** starts only after the qualification passes on at least one real GPU. Its acceptance
  test must reproduce a known kernel bug end to end, as #61 requires.
- **Rule:** CPU-only CI does not claim CUDA support. Without a real NVIDIA GPU, GPU tests report
  "not qualified on this host". They never pass, and never go missing silently.

### C++ built by GCC: reading what is withheld

GCC's C++ builds withhold what LLDB misreads ([gaps](lang/c-cpp-cookbook.md#what-is-different-in-a-gcc-build)).
Reading it would take about 2 weeks:

- read more of an unordered container's entries from its own nodes: floating-point numbers,
  characters, enums and nested containers;
- read a lambda's captures through `__closure`;
- set a text in a string's own buffer, which LLDB reads as `char[0]`;
- tell when a caller's aggregate is stored, from its elements' lines.

GDB's DAP reads much of this right, but Peeky's native layer is built on lldb-dap. A GDB backend
would take about 4 to 6 weeks, and is deferred.

### Mixed-runtime inference engines

Inference engines combine Python orchestration, C++ extensions and CUDA kernels.
[MIXED_RUNTIME.md](MIXED_RUNTIME.md) is the design requirement for debugging them. It is not
implemented support.

- Support for each host language alone does not establish mixed-stack or GPU debugging.
- Its qualification steps are in [MIXED_RUNTIME.md](MIXED_RUNTIME.md#incremental-qualification).

### Kubernetes

Kubernetes was deferred from the MVP ([ADR 0001](adr/0001-mvp-boundaries.md)). It needs identity,
deployment permissions, relay and reconnect, and a data policy. The [design](DESIGN.md) sets the
order:

1. A worker per workload, with explicit deployment permissions.
2. Then an authenticated outbound relay.
3. Define reconnect semantics and allowed data egress before claiming secure production support.

### Other accepted directions

- **Correlated evidence:** debugger evidence joined with code, logs and metrics
  ([ADR 0002](adr/0002-correlated-evidence.md)). Its implementation is proposed, step by step.
- **The prepared-build cache:** step 5 of ADR 0004, above.
- **Smaller follow-ups** are in [PROGRESS](PROGRESS.md#next-slices).

## What stays deferred

- Broader autonomous patching across several files. `peeky verify` takes several edits, all in
  one file ([#83][p83]).
- Kubernetes workers and an outbound relay, above.
- Multi-tenant authorization. A worker has one controller and one session.
- Production data policies, and a production mutation policy.
- Attach for languages other than Java.
- A GDB backend, for GCC's builds. GDB's DAP isn't supported.

[p15]: https://github.com/sushantmane/peeky/pull/15
[p16]: https://github.com/sushantmane/peeky/pull/16
[p17]: https://github.com/sushantmane/peeky/pull/17
[p18]: https://github.com/sushantmane/peeky/pull/18
[p19]: https://github.com/sushantmane/peeky/pull/19
[p20]: https://github.com/sushantmane/peeky/pull/20
[p21]: https://github.com/sushantmane/peeky/pull/21
[p22]: https://github.com/sushantmane/peeky/pull/22
[p23]: https://github.com/sushantmane/peeky/pull/23
[p25]: https://github.com/sushantmane/peeky/pull/25
[p26]: https://github.com/sushantmane/peeky/pull/26
[p28]: https://github.com/sushantmane/peeky/pull/28
[p30]: https://github.com/sushantmane/peeky/pull/30
[p32]: https://github.com/sushantmane/peeky/pull/32
[p33]: https://github.com/sushantmane/peeky/pull/33
[p35]: https://github.com/sushantmane/peeky/pull/35
[p42]: https://github.com/sushantmane/peeky/pull/42
[p68]: https://github.com/sushantmane/peeky/pull/68
[p83]: https://github.com/sushantmane/peeky/pull/83
