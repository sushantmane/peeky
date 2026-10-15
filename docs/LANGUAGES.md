# Five-language MVP

Python, Java, C, C++, and Rust are the minimum product scope. The existing Python release is the foundation/alpha; it does not complete the multi-language MVP. Ship each language incrementally and report support honestly.

Current evidence: Python has runtime experiments and candidate verification. Java has fresh compilation, generated source maps, guarded HTTP runtime operations and real failure-cleanup tests; Java also has a deterministic full investigation loop, fresh candidate compilation and independent verification. Separate-host path binding and a live-model Java evaluation remain open. C has an experimental LLDB worker and a deterministic full investigation loop with fresh candidate builds, frozen headers and independent executable checks. C++ adds explicit C++17 builds, bounded class/reference/array expansion and deterministic candidate repair. Rust has an experimental HTTP worker for one dependency-free Cargo binary with application-unit symbols, compiler-validated `Option<i32>` views and root i32 mutation; Cargo test execution and candidate repair remain.

## Priority and rationale

| Order | Language | Backend direction | First acceptance target |
| --- | --- | --- | --- |
| 1 | Python | Existing debugpy DAP | Harden existing lifecycle and independent verification |
| 2 | Java | Java DAP adapter backed by JDI/JDWP | Launch a small JVM app, inspect/mutate primitive locals, verify a compiled source fix |
| 3 | C | Shared native DAP via LLDB | Symbolized debug binary, local/pointer inspection, primitive mutation, source rebuild and test |
| 4 | C++ | Same native backend + C++ fixtures | Classes/references and basic standard-library values, primitive mutation, rebuild and test |
| 5 | Rust | Same native backend + Rust fixtures/formatters | Struct/enum/Option inspection, supported primitive mutation, Cargo build and test |

Java comes next because it fits the owner's JVM and heappy work and forces the architecture to handle a different managed runtime early. C establishes the simplest native baseline. C++ and Rust reuse that adapter while getting their own compatibility and regression tests. Sharing an adapter never means automatically claiming language support.

Do not wait for all ten Python corpus bugs or a Kubernetes gateway before starting Java. First close the common worker lifecycle blockers and define the adapter/build contracts; then deliver the second runtime. Broader corpus work and SSH validation can progress with the language slices.

## Contract changes required before Java

Debugger configuration and build preparation have now been extracted. The controller now supports Python verification and a Java compiled-verification strategy behind two narrow boundaries:

- **Debugger adapter:** adapter process/transport, launch configuration, capability discovery, stop/stack/variable normalization, and teardown. Keep session IDs, stop IDs, operation budgets and audit ownership in the worker.
- **Build and verification recipe:** explicit argument-vector commands, working directory, selected toolchain, source/workspace identity, artifact identity, independent verification command and completion. Keep original/check integrity and report acceptance in the controller.

Avoid claiming stable interface names before the Java integration validates them. Python remains the reference adapter and must pass the same conformance suite after extraction.

Compiled programs must identify both source revision/hash and the actual class/binary artifacts plus debug symbols. A source hash alone cannot establish that a binary was built from those bytes. Require a clean, explicit build recipe that records the source/workspace fingerprint, toolchain version, build flags, artifacts and debug-symbol identities in one manifest. Include a negative test pairing a stale binary with updated source. Fail clearly on missing symbols, mismatched artifacts, missing toolchains and unsupported operations. Initial builds/rebuilds for actual fixes are expected; adding a breakpoint or inspecting/changing an available live value should not require rebuilding source.

## Definition of language support

Each language has to pass real-adapter tests on the initial Linux platform:

1. Launch a deterministic fixture and verify the requested/actual breakpoint location.
2. Inspect stack and locals, including a representative language-specific type.
3. Change a supported primitive local, continue, and observe a different result without source edits/rebuilds during that runtime experiment.
4. Reject stale IDs and handle timeout, adapter failure and close without leaking owned execution.
5. Write a candidate source change in an isolated workspace, build when necessary, run the unchanged independent verifier in a fresh process, and report evidence.
6. Exercise the same worker API across a process/network boundary. Bind source paths across different absolute client/target roots, check actual breakpoint locations and loaded artifact identity, and reject mismatches before mutation. Document runtime setup and remote verification location.
7. Run a provider-injected end-to-end agent test for that language: choose observation, intervene, propose source change, build candidate, verify. Worker-only tests cannot establish autonomous language support. CI uses deterministic decisions; a manual live-model example complements them.

No universal arbitrary expression evaluation, pointer rewriting, Rust ownership preservation, Java class-schema hot swap, or instruction-pointer jump guarantee. Distinguish inspect, primitive mutation, evaluation, attach, conditional breakpoints and logpoints in capabilities. Unsupported behavior must return an explicit result, not silently fall back to editing source.

Start with unoptimized builds containing debug information. Optimized-out locals are reported as unavailable. Rust complex-value mutation and pretty printing need their own adapter/version tests; support for a scalar in Rust is not support for all Rust values.

## Java integration decision

Microsoft's Java debugger implements DAP and is normally integrated with the Java language tooling. First spike its headless startup, packaging and launch dependencies; do not assume the editor plugin is a standalone DAP executable. Pin a working adapter/JDK combination and document acquisition/licensing. Prefer a small reusable launcher over scraping jdb. If headless DAP packaging is unsuitable, record an ADR comparing a small JDI-backed bridge before committing to a new protocol implementation.

## Native integration decision

Start by evaluating LLVM's lldb-dap for C, C++, and Rust. Pin a known adapter/compiler combination for CI, including Rust formatter configuration where needed. Use the shared conformance suite and distinct language fixtures. Keep GDB/MI as a compatibility option driven by a concrete unsupported target, rather than implementing two native backends immediately.

## CI growth plan

Retain the independent quality/unit/package lanes. Add independent Java, C, C++, and Rust integration lanes as their support lands; no cross-language build dependency chain. Cache Maven/Gradle and Cargo artifacts/toolchains where appropriate, and keep fixtures tiny. Shared controller/protocol changes run every supported language lane; do not use path filters that hide shared-contract regressions.

The <5-minute execution target applies to the critical path, not summed runner minutes. Per-job cutoffs stay four minutes; cold-cache adapter/toolchain downloads must be measured. If acquisition dominates, use versioned cached adapter bundles or an intentionally maintained runner image. Do not move essential conformance checks out of required CI merely to claim a fast pipeline. Manual live-model evaluations remain separate.

## Sources

- [Microsoft Java debug server](https://github.com/microsoft/java-debug)
- [LLVM lldb-dap documentation](https://lldb.llvm.org/use/lldbdap.html)
- [DAP overview and capabilities](https://microsoft.github.io/debug-adapter-protocol/overview.html)
