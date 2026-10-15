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

## Follow-up review: agent-speed slice (19328bf..7e4aeba)

**Result:** no blocking issues. Three should-fix issues and four nits are all fixed, with regressions.

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| 1 | should-fix | `job wait` replayed only stdout. A failed background command's error message, which goes to stderr, was lost: the agent got exit 2 and no text. | Fixed. `job wait` replays both streams. Regression: `test_failed_background_jobs_replay_their_error_on_stderr`. |
| 2 | should-fix | With a relative `PEEKY_STATE_DIR`, the detached child, which runs with another cwd, could not find its job, so every job became `JOB_LOST`. | Fixed. State paths are made absolute (without resolving symlinks, so the symlink check still applies). Regression: `test_background_jobs_work_with_a_relative_state_dir`. |
| 3 | should-fix | The pause guard auto-resumed crash stops. On macOS, LLDB re-faulted every interval, the crash could not be inspected, and the reason was not recorded. | Fixed. Only `reason: breakpoint` stops are auto-resumed. Auto-resume entries and the audit record the reason and location, and `wait`/`inspect` report `reason`. Regression (real LLDB): `test_pause_guard_resumes_breakpoints_but_never_crash_stops`. |
| 4 | nit | Job files `lock`, `out` and `log` were created 0644. | Fixed. They are created 0600 (checked in the stderr regression). |
| 5 | nit | `ended: "hits"` was reported for a timed-out `until=hits` probe. | Fixed. `ended` is `hits` only when all hits were captured, `exit` when the program terminated, and otherwise null. Regression: `test_timed_out_probes_do_not_claim_to_have_ended_at_their_hits`. |
| 6 | nit | `pauses_ms` started after `wait` returned, so it missed the location read. | Fixed. Measured from the stop event's receive time. |
| 7 | nit | A poll in the microseconds between the child opening and locking its lock file could report `JOB_LOST`. | Fixed. The child writes a `started` marker only while holding the lock, and a free lock means lost only once that marker exists (or after 30 s without it). Regression: `test_a_job_not_yet_holding_its_lock_is_running_not_lost`. |

The reviewer confirmed these areas:
- Lock coverage between the guard and client operations (a resumed stop fails closed with `STALE_STOP`).
- Event ordering and `pending_stop`.
- No guard runs for in-process probes.
- Guard failure closes or detaches.
- `until=hits` leaves no orphan debuggee.
- `candidate_sha256` encoding is consistent.

## Final review: f4a9d12 (multi-source describe, catch-all errors)

**Result:** no blocking issues. One should-fix issue, fixed.

The issue: `describe` on a fresh-build Java target (`main_class`) raised `AttributeError`, because `JavaSession` had no `lines`. This bug predated f4a9d12, but the commit's new catch-all reported it as `INVALID_REQUEST` ("fix your arguments"). An agent would then retry a correct request, paying for a javac build each time.

The fix:
- `JavaSession.lines` is now built from the prepared class line tables.
- Unexpected exception types (not `ValueError`, `KeyError`, `TypeError`, `OSError`, `RuntimeError` or `TimeoutError`) now return `INTERNAL_ERROR`, which tells the agent not to retry or change its request.
- Regressions: `test_cli_describe_lists_fresh_build_java_lines` (real JDK) and classification cases.

The reviewer confirmed these areas:
- Path confinement of the default source.
- Unchanged behavior for auto-detected targets.
- SIGTERM still unwinds through cleanup.
- Job exit codes.
- Worker-side `FileNotFoundError` keeps `session_closed: true`.
