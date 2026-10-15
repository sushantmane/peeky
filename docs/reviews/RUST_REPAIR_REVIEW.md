# Rust repair verification review

Date: 2026-10-15. Initial immutable review target: `9985257`, parent `10a1136`. Reviewed in the isolated Rust review checkout; baseline test evidence below uses an archive of the commit, not concurrently edited source. This review covers compiled-test and checker acceptance. The worker/symbol review remains in RUST_WORKER_FOLLOWUP.md.

## Findings and acceptance boundaries

- **Two gates remain required.** The shared verifier calls the trusted compiled-test hook and the independent checker, then combines their explicit boolean pass results. Test failure does not skip the checker. The original-checker requirement uses the checker's unchanged exit code, so failed baseline Cargo tests cannot conceal a checker that passes the original.
- **Execution uses prepared paths.** Preparation builds the named binary and compiles the named frozen integration-test target. Verification executes the selected test executable directly with fixed harness arguments; Cargo is not called during checking. The complete deps directory and uplifted binary are hashed artifacts. The uplifted program used by the supplied test must match the debugged binary before and after testing.
- **Freshness and failures are distinct.** Every candidate receives a fresh workspace. Application compile failures are retryable; test-target compile failures remain fatal. Original/test/checker identities are protected, descriptor and candidate integrity checks remain in the shared path, and cleanup occurs on successful and failed checks.
- **Overfit rejection is exercised.** The real integration test source includes a syntax-error candidate, a constant candidate that passes the small compiled test but fails the independent checker, and the general fix. This reviewer inspected that sequence; the local real-runtime timings in RUST_AGENT.md were not independently reproduced here.

### P2 — ignored tests are currently accepted as a clean test result

At baseline `src/peeky/rust.py:108`, libtest_summary parses ignored/measured counts but does not require them to be zero. A summary with one passing test and one ignored test is accepted. The supplied tests are immutable, so this is not a candidate edit of the test suite, but it weakens the stated clean-run gate. Requested disposition: explicitly reject ignored/measured tests or document that intentional ignored tests are permitted. Add direct summary regressions either way.

### Coverage required for executable identity gates

The baseline integration suite demonstrates frozen test-source drift, but lacks a direct regression exercising run_tests against changed/missing compiled test artifacts and changed uplifted targets before or during execution. Requested narrow regressions should prove failures occur before invoking a substituted executable and after test-time artifact changes. These complement the shared verifier conjunction tests rather than duplicating the full end-to-end loop.

The earlier helper-tool findings also apply to this repair slice: configured dsymutil selection must match the executed helper, and failed Cargo preparation must check all previously probed tool identities before returning retryable build feedback. Their follow-up fixes require independent confirmation before repair approval.

## Validation performed

Immutable `9985257` snapshot: tests/unit/test_compiled_verification.py and tests/unit/test_rust_worker.py — **56 passed in 0.09 seconds**. This includes checker/test conjunction, libtest malformed/absent/multiple-summary rejection, artifact selection and Rust worker boundary tests. It does not establish final-head CI or independently reproduce the real Rust repair loop.

## Trust limits

A parsed libtest summary is trusted test-harness evidence, not a tamper-proof completion channel. Arbitrary native code could forge output or interfere with same-permission processes; those attacks remain outside the documented trusted-code model. The separate unchanged checker still requires normal completion through the existing wrapper. Filesystem hashes are boundary checks rather than atomic loaded-memory identity, and matching tool/source evidence does not make SDK, runtime libraries or remote/local environments hermetic. Claims should remain limited to the supplied tests/checker and tested toolchain pairs.

## Follow-up acceptance regressions and launch budget review

Added independent tests/unit/test_rust_verification_guards.py using real prepared artifact manifests and a controlled test executor. Ten tests pass: changed/deleted test executable, uplifted target or application prevents execution; changes to each artifact during an otherwise passing test fail the post-run gate; the positive case checks the exact prepared test path and fixed argv without Cargo. The tests exercise the real RustSession.run_tests and build identity checks, rather than replacing those guards with mocks.

Independently reviewed the subsequent absolute launch-deadline changes across worker, DAP, symbols and native UUID extraction. The worker carries one deadline through preparation and launch; DAP caps write-lock waits, writes, response waits and event waits to it. Cleanup clears the launch deadline and retains its own bounded grace period. Successful launch clears the temporary deadline. The launch-deadline and new verification-guard subset passed **14 tests in 3.59 seconds**; Ruff check passed for the owned test file. This source review found no additional issue in the deadline API itself; final preparation callback/error-classification fixes were still being completed at that checkpoint.

## Preparation and clean-test follow-up dispositions

Source-confirmed fixes now reject ignored/measured tests. The build runner rechecks all previously recorded tool identities before each command and before returning a failed-command result. Rust preparation validates staged Cargo inputs/ancestry before every tool invocation, validates the pinned probe versions before compilation, and verifies that PATH resolves dsymutil to the declared executable. These changes resolve the earlier helper provenance findings.

Retryable Cargo failure now requires complete structured diagnostics with one final build-finished failure and a source-bound primary error; timeouts, output overflow, ICEs, non-source errors, malformed spans and contradictory completion are fatal. Review found an early-return issue that would have ignored a later ICE; the author corrected it by scanning all errors before classification. The final malformed-message-shape check is also fixed: a compiler-message whose message is not an object rejects retry classification rather than being skipped.

Combined isolated-checkout run of preparation guards, verification guards, launch deadlines, shared verification, Rust worker and build tests: **111 passed in 5.12 seconds**. Every isolated command used PYTHONPATH pointing at that checkout's src; peeky.__file__ was explicitly confirmed to resolve there, avoiding the venv's editable installation in the original checkout. The ten independently authored verification-guard tests and this review file are the only owned changes in the isolated tree.

Final disposition: no remaining source-review blocker in the reviewed repair/preparation changes. Required full integration and hosted CI checks remain merge gates and must refer to the final committed head.

Final follow-up review confirmed that DAP zero-timeout cached-event reads retain their existing behavior when no launch deadline is active; an expired active launch deadline still fails. Preparation also rechecks original/staged source manifests before each command, alongside tool hashes. The final isolated DAP/launch/preparation/verification-guard subset passed **41 tests in 5.95 seconds** after those changes. No new source-review blocker was found.
