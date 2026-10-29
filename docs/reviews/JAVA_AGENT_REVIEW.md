# Java agent integration adversarial review

Date: 2026-10-14. Scope: the fresh-build Java verification boundary, the controller's pending-breakpoint observations, and the worker's launch provenance. This is an independent AI-assisted engineering review of `feat/java-agent`, not an external security audit or proof of general debugging ability.

## Findings and dispositions

| Finding | Priority | Disposition and evidence |
| --- | --- | --- |
| Same Java sources could be debugged with different main class/application arguments from those used by the checker | P1 | Fixed: the worker returns a semantic target identity and the verifier requires an exact match alongside the full source manifest. Two mocked regressions reject a changed main class or argument list despite identical source bytes. Three additional regressions reject a missing companion source, changed remote companion hash, or locally modified companion even when the selected main source is unchanged. Compiler/platform equivalence across hosts remains outside that semantic identity. |
| A late verified event from another breakpoint, a revoked breakpoint, or a different stopped location could be accepted as evidence | P1 | Fixed: the controller resolves the launched breakpoint ID using the latest matching updates, preserves updates across wait polls, requires verified status, and checks the actual stopped line and supplied source path. Explicit relocation is allowed. Mocked regressions cover unrelated IDs, missing IDs, pending/revoked/removed breakpoints, mismatched locations and successful relocation. |
| Generic build exceptions could turn integrity failures into retryable compiler feedback | P1 | Fixed: only `BuildCommandError` carries nonzero/budget-limited command results as build feedback. Source/artifact/provenance failures remain fatal. Original input/checker integrity is checked even after a failed build. A regression distinguishes compiler failure from a build that overwrites source. |
| A passing checker could mutate the candidate, descriptor, original checker or compiled artifact before acceptance | P1 | Fixed: the Java boundary compares the saved candidate with its compiled staged source, checks both after execution, checks descriptor bytes, and runs build/runtime/original/checker identity gates. Five real-JDK regressions execute deliberate tampering and reject acceptance. |
| A Java candidate might lose its filename/package layout or accidentally use a previous build | P1 | Addressed: candidate files retain their declared relative `.java` name; every attempt copies declared inputs into a new workspace and compiles again. The real end-to-end test rejects invalid Java, then an overfit candidate, then accepts the general candidate. Produced artifacts cannot also be copied inputs. |

## Additional failure paths assessed

A separate bounded AI-assisted review considered the proposed design without tools or source access. It raised ambient JVM-option injection, stale classes after failed compilation, checker timeout, source/artifact check-to-use races, and redefinition leakage. This was a no-tool design review, not a source audit or executed test. The dispositions below map those concerns to inspected code and regression evidence; check-to-use races remain an explicit trusted-code limitation rather than a claimed fix.

- **Premature checker exit:** Java uses the same trusted completion wrapper as Python. `SystemExit(0)` and absent completion do not constitute a passing check. Existing wrapper regressions cover both Python `SystemExit` and direct process exit; Java-specific code does not bypass the wrapper.
- **Ambient JVM options:** build environments inherit only the explicit allowlist and declared entries. `JAVA_TOOL_OPTIONS` and similar ambient flags are not implicitly inherited. The verifier receives the runtime's recorded environment, and the example checker uses that environment for its JVM runs. A build regression covers ambient JVM-option exclusion. This does not make the build hermetic.
- **Checker/build timeouts and output floods:** both use the existing bounded process executor with owned process-group cleanup. A build timeout/output limit produces a failed build result, never verification success. Executor regressions cover deadlines, descendant cleanup, output limits and credential stripping.
- **Hot redefinition contamination:** the public worker and Java adapter do not expose redefinition operations. Candidate verification uses fresh builds and JVM executions instead of the mutated debug session.
- **Failed launch and cleanup:** the DAP event wait detects a failed launch response promptly; the worker closes the prepared runtime. Controller cleanup preserves an active original exception. Repeated mutation after an uncertain transport outcome remains prohibited.

## Validation performed for this review

- `tests/unit/test_agent_java.py`, `tests/unit/test_build.py`, and `tests/unit/test_agent.py`: **47 passed in 1.96 seconds**.
- `tests/java/test_java_agent.py`, using the prepared real Microsoft-core adapter bundle and local JDK 17: **7 passed in 8.26 seconds**. These include real HTTP/debugger observation and mutation, compile-failure feedback, overfit rejection, a general rebuilt candidate checked in fresh JVMs, and five tampering scenarios.
- Ruff check/format passed for the owned test and typed-build-error changes.

The first combined run correctly rejected observation while the new target-identity field had not yet been connected to the worker response. The worker now supplies the field, and the subsequent real Java lane passed. This run used deterministic decision fixtures and made no model calls; it does not establish live-model success rates or hosted-CI elapsed time.

## Remaining trust and scope limits

The source/check/descriptor/artifact comparisons are checks at specific times. They do not atomically bind filesystem reads to loaded executable pages, prevent malicious concurrent replacement between checks, or defend against a target that deliberately escapes its owned process group. The checker, compiler recipe, adapter bundle and application are trusted developer code with local OS permissions. These controls detect the demonstrated integrity failures; they are not a hostile-code sandbox or tamper-proof verification service.

The semantic target identity binds source names, main class, application arguments and compiler options. It does not assert that a remote worker's OS, JDK distribution, shared libraries, dependencies or external services match local verification. Those details remain available only to the extent the build manifest records them; remote environment equivalence must not be inferred from matching source hashes or target identity.

A successful result means the unchanged supplied checker completed for the identified candidate. It does not prove arbitrary Java correctness or that the runtime intervention alone repaired the program. Saved evidence explicitly marks compiled artifacts as not retained; descriptor command paths refer to cleaned temporary workspaces and are not a standalone replay package. Broader Java projects, additional language adapters and statistically useful live-model evaluations require their own acceptance work.
