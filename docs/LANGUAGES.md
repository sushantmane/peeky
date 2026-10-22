# Five-language MVP

The minimum product scope is Python, Java, C, C++ and Rust. The Python release is the foundation,
or alpha. It does not complete the multi-language MVP. Ship each language incrementally, and report
its support honestly.

## Where each language stands

What each language can read and change today is on its page: [Python](lang/python.md),
[Java](lang/java.md), [C and C++](lang/c-cpp.md), [Rust](lang/rust.md). This table lists what each
has as a whole:

| Language | What exists | Design notes |
| --- | --- | --- |
| Python | Runtime experiments and candidate verification. | [DESIGN.md](DESIGN.md) |
| Java | Fresh compilation, generated source maps, guarded HTTP runtime operations and real failure-cleanup tests. A deterministic full investigation loop, with fresh candidate compilation and independent verification. | [worker](JAVA_WORKER.md), [loop](JAVA_AGENT.md) |
| C | An experimental LLDB worker. A deterministic full investigation loop, with fresh candidate builds, frozen headers and independent executable checks. | [worker](WORKER.md), [loop](NATIVE_AGENT.md) |
| C++ | Explicit C++17 builds, bounded expansion of classes, references and arrays, and deterministic candidate repair. | [internals](NATIVE_INTERNALS.md), [worker](WORKER.md#configure-a-c-target), [loop](NATIVE_AGENT.md#the-c-fixture) |
| Rust | An experimental HTTP worker for one dependency-free Cargo binary, with application-unit symbols and compiler-validated `Option<i32>` views. A deterministic investigation loop: the hashed compiled Cargo test and an independent checker must both pass on a fresh build. | [worker](RUST_GATE.md), [loop](RUST_AGENT.md) |

Still open for Java: binding source paths on a separate host.

## Priority and rationale

| Order | Language | Backend direction | First acceptance target |
| --- | --- | --- | --- |
| 1 | Python | Existing debugpy DAP | Harden existing lifecycle and independent verification |
| 2 | Java | Java DAP adapter backed by JDI/JDWP | Launch a small JVM app, inspect/mutate primitive locals, verify a compiled source fix |
| 3 | C | Shared native DAP via LLDB | Symbolized debug binary, local/pointer inspection, primitive mutation, source rebuild and test |
| 4 | C++ | Same native backend + C++ fixtures | Classes/references and basic standard-library values, primitive mutation, rebuild and test |
| 5 | Rust | Same native backend + Rust fixtures/formatters | Struct/enum/Option inspection, supported primitive mutation, Cargo build and test |

Why this order:

- **Java comes second.** It fits the owner's JVM and heappy work. It also forces the architecture
  to handle a different, managed runtime early.
- **C sets the simplest native baseline.**
- **C++ and Rust reuse C's adapter,** but get their own compatibility and regression tests.
  Sharing an adapter never means claiming a language's support automatically.

Don't wait for all ten Python corpus bugs, or for a Kubernetes gateway, before starting Java. First
close the common worker lifecycle blockers, and define the adapter and build contracts. Then
deliver the second runtime. Broader corpus work and SSH validation can progress alongside the
language slices.

## Contract changes required before Java

Debugger configuration and build preparation are extracted. The controller supports Python
verification and a Java compiled-verification strategy, behind two narrow boundaries:

- **Debugger adapter:** the adapter's process and transport, launch configuration, capability
  discovery, normalizing stops, stacks and variables, and teardown. The worker keeps session IDs,
  stop IDs, operation budgets and audit ownership.
- **Build and verification recipe:** explicit argument-vector commands, the working directory and
  the selected toolchain. It also covers source, workspace and artifact identity, the independent
  verification command, and completion. The controller keeps the integrity of the originals and
  the checks, and reports acceptance.

Don't claim stable interface names before the Java integration validates them. Python remains the
reference adapter, and must pass the same conformance suite after the extraction.

### Build provenance for compiled programs

A compiled program must identify its source revision or hash, and the actual class or binary
artifacts with their debug symbols. A source hash alone can't show that a binary was built from
those bytes. So:

- Require a clean, explicit build recipe. It records in one manifest the source and workspace
  fingerprint, the toolchain version, the build flags, the artifacts and the debug symbols'
  identities.
- Include a negative test that pairs a stale binary with updated source.
- Fail clearly on missing symbols, mismatched artifacts, missing toolchains and unsupported
  operations.

Initial builds, and rebuilds for actual fixes, are expected. Adding a breakpoint, or reading or
changing an available live value, should not need a rebuild.

## Definition of language support

Each language has to pass real-adapter tests on the initial Linux platform:

1. Launch a deterministic fixture, and verify the requested and actual breakpoint location.
2. Inspect the stack and locals, including a representative language-specific type.
3. Change a supported primitive local, continue, and observe a different result. The runtime
   experiment must need no source edit or rebuild.
4. Reject stale IDs. Handle a timeout, an adapter failure and a close without leaking owned
   execution.
5. Write a candidate source change in an isolated workspace, and build it when necessary. Run the
   unchanged independent verifier in a fresh process, and report the evidence.
6. Exercise the same worker API across a process or network boundary:
   - bind source paths across different absolute client and target roots;
   - check actual breakpoint locations and the loaded artifact's identity;
   - reject mismatches before any change;
   - document the runtime setup and where remote verification runs.
7. Run a provider-injected end-to-end agent test for that language: choose an observation,
   intervene, propose a source change, build the candidate, and verify it. Worker-only tests can't
   establish autonomous language support. CI uses deterministic decisions; a manual live-model
   example complements them.

### What no language promises

No language guarantees these:

- arbitrary expression evaluation;
- rewriting pointers;
- preserving Rust ownership;
- hot-swapping a Java class's schema;
- jumping the instruction pointer.

Capabilities tell apart inspection, primitive mutation, evaluation, attach, conditional
breakpoints and logpoints. Unsupported behavior must return an explicit result. It must never fall
back to editing source silently.

### Builds and values

- Start with unoptimized builds that contain debug information.
- Optimized-out locals are reported as unavailable.
- Changing complex Rust values, and pretty-printing them, need their own adapter and version
  tests. Support for a Rust scalar is not support for all Rust values.

## Java integration decision

Microsoft's Java debugger implements DAP, and is normally integrated with the Java language
tooling.

- First spike its headless startup, packaging and launch dependencies. Don't assume that the
  editor plugin is a standalone DAP executable.
- Pin a working adapter and JDK combination, and document how to get it and its licensing.
- Prefer a small reusable launcher to scraping jdb.
- If headless DAP packaging is unsuitable, record an ADR that compares a small JDI-backed bridge,
  before committing to a new protocol implementation.

## Native integration decision

- Start by evaluating LLVM's lldb-dap for C, C++ and Rust.
- Pin a known adapter and compiler combination for CI, including Rust formatter configuration
  where needed.
- Use the shared conformance suite, and distinct fixtures per language.
- Keep GDB/MI as a compatibility option, driven by a concrete unsupported target. Don't implement
  two native backends at once.

## CI growth plan

- Keep the independent quality, unit and package lanes.
- Add independent Java, C, C++ and Rust integration lanes as their support lands. No lane depends
  on another language's build.
- Cache Maven, Gradle and Cargo artifacts and toolchains where it helps, and keep fixtures tiny.
- Shared controller and protocol changes run every supported language's lane. Don't use path
  filters that hide regressions in the shared contract.

Timing:

- The under-5-minute target applies to the critical path, not to the summed runner minutes.
- Each job's cutoff stays at four minutes.
- Measure cold-cache downloads of adapters and toolchains. If they dominate, use versioned, cached
  adapter bundles, or a runner image maintained on purpose.
- Don't move essential conformance checks out of required CI just to claim a fast pipeline.
- Manual live-model evaluations stay separate.

## Sources

- [Microsoft Java debug server](https://github.com/microsoft/java-debug)
- [LLVM lldb-dap documentation](https://lldb.llvm.org/use/lldbdap.html)
- [DAP overview and capabilities](https://microsoft.github.io/debug-adapter-protocol/overview.html)
