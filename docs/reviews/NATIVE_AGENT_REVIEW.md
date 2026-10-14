# Native C agent verification adversarial review

Date: 2026-10-14. Scope: shared compiled verification, Java compatibility, C candidate preparation, and integrity/error classification. This independent AI-assisted review covers code and deterministic tests; it does not measure live-model repair success or establish a hostile-code sandbox.

## Findings and dispositions

| Finding | Priority | Disposition |
| --- | --- | --- |
| Header bytes can change the compiled candidate even when its edited C source is unchanged | P1 | Shared verification freezes every declared source/header at investigation creation, includes the full manifest in worker matching, and rechecks it across attempts. Candidate editing remains restricted to declared source files. New mocked regressions reject modified companion headers, missing remote header identities and attempted header edits. |
| A compiler could change or delete staged source, then exit nonzero, causing provenance drift to be treated as retryable syntax feedback | P1 | Fixed in the shared build runner: before cleaning a failed command workspace, compare staged and original inputs against their captured identities. Four real subprocess regressions cover modification/deletion of staged/original source followed by exit 7. These failures are fatal; unchanged compiler failures retain typed feedback. The runtime author independently reviewed this fix and reran the tests. |
| Symbol-generation failures or toolchain setup failures could be mistaken for candidate compiler diagnostics | P1 | Native preparation permits typed feedback only for its fixed compiler/linker command vectors. Other command failures become fatal preparation errors; tool probes and changed executable hashes already remain fatal in the shared builder. Generic regressions verify the controller boundary does not catch ordinary integrity/symbol errors. NativeTarget-specific regressions feed failures from its actual generated compile, link and dsymutil command vectors and confirm only the first two remain retryable; a header edit is rejected before tool lookup. |
| A checker could return success while changing the descriptor, compiled output, staged header or checker itself | P1 | Shared verification retains the existing Java before/after artifact, original/checker, descriptor and saved-candidate gates. Four mocked passing-checker regressions tamper with those inputs and reject acceptance while cleaning the prepared workspace. Real-runtime tampering coverage belongs to the native integration lane. |
| Extracting Java verification could silently change JVM arguments or lifecycle | P2 | The Java wrapper retains its public methods and language marker; JavaSession supplies the same JVM/classpath/main command prefix. The shared engine retains completion-wrapper execution, artifact evidence, error classification and cleanup. Existing Java agent unit regressions run alongside the new shared suite. |

## Validation

New `tests/unit/test_compiled_verification.py` tests exercise the generic acceptance boundary with fake runtime/compiler objects. They establish engine behavior, not native compiler or debugger conformance. They also check fresh workspace identities, preserved nested candidate filenames, cleanup, and unique evidence files.

The initial shared/build/Java unit run passed **50 tests in 1.18 seconds**. After adding the failed-compile source-integrity regressions, build/shared tests passed **39 tests in 1.07 seconds**; a separate reviewer repeated that subset successfully. The final combined native/shared/build/Java-agent unit run passed **101 tests in 1.14 seconds**, including the adapter-specific classification regressions. Ruff check and format check passed for the owned build and test changes. Real C and Java integration results must be considered separately before merge, including the hosted Linux LLVM 18 lane.

## Trust and interpretation

A passing result means the unchanged trusted checker completed for the identified freshly built candidate. Runtime intervention alone does not establish a source fix. The new C wrapper uses the same completion envelope as Java and Python; no alternate bare exit-code acceptance path was introduced.

Filesystem hash checks occur at boundaries and do not prevent malicious concurrent replacement between checks or bind loaded executable memory atomically. Compilers, adapters, checkers and programs retain ordinary local OS permissions. Tool identities are checked per prepared run and recorded in its manifest; the engine does not promise identical toolchains across all attempts or across remote/local machines. SDK headers, shared libraries and external services remain outside hermetic capture. Saved evidence records cleaned build paths with artifacts_retained=false and is not a replayable executable bundle.
