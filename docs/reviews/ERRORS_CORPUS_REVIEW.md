# Review: error codes, corpus and agent-evaluation fixes (2026-10-15)

- **Scope:** `feat/java-attach..feat/corpus` at 19328bf: protocol v1 error codes (#6), the ten-bug corpus and outcomes (#7), and the fixes from the live agent evaluation.
- **Reviewer:** an independent code-review agent with read-only access, which checked findings against the committed blobs.
- **Result:** no blocking issues. Two should-fix issues and one nit are all fixed, with regressions, in the agent-speed commit that follows.

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| 1 | should-fix | `_wait` ran `inspect` on every stop just to print the location. On a crash or signal stop, runtime validation raises `RuntimeError`. The worker then closes the session, killing the debuggee and losing the crash output, so `session start`/`wait`/`continue` could destroy a session. | Fixed. The worker reads the top-frame location once per stop with a best-effort `stackTrace` (2 s bound) that never raises and never closes the session, and `_wait` no longer inspects. Regression: `test_location_read_failure_never_closes_the_session`. |
| 2 | should-fix | `verify` built the reported diff from an earlier, separate read of the source than the candidate it verified. An edit in between could make the evidence diff differ from what passed. | Fixed. Both the Python and compiled paths report `candidate_sha256` for the file actually verified. `verify` refuses with `PROVENANCE_CHANGED` unless it equals the hash of the reported edit. Regression: `test_verify_refuses_when_the_source_changes_before_the_candidate_is_built`. |
| 3 | nit | The SDK's fallback error payload for non-JSON bodies lacked `hint`, `retryable` and `session_closed`, so the CLI error handler raised `KeyError`. | Fixed. The fallback has the full shape, and the CLI reads fields defensively. Regression: `test_unexpected_worker_error_bodies_render_cleanly`. |

The reviewer confirmed these areas with no issues:
- The Python literal allow-list cannot be bypassed: calls, names, attributes, f-strings, comprehensions and lambdas are rejected, as are `**`, `*` and `<<` size attacks.
- No retryable code follows an uncertain mutation.
- `no_reproduction` still persists its report.
- All ten corpus checkers reject the original and the overfit and accept the fix, on multiple inputs.
