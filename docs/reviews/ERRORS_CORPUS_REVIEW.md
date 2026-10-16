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

## Review: agent-usability slice (#26, 37d2462)

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| 1 | blocking | `[-k]` trusted LLDB's `size=N` and `indexedVariables`. Both equal the number of children LLDB listed, and LLDB stops listing `std::forward_list`/`std::list` at `target.max-children-count` (default 24). So `fl[-1]` on a 30-element list would return element 23 as "the last element". | Fixed. A length counts only if it is independent of the listing: debugpy's `len()` on a real `list`/`tuple`/`deque`, or a fixed array type (`int[4]`) or JVM array value (`int[4]@12`). Everything else is refused. Regression: `test_declared_lengths_must_be_independent_of_the_listing`. |
| 2 | should-fix | A Python dict with int keys `0..n-1` passed as a sequence (`d[-1]` returned the last key's value). | Fixed by the same rule (`dict` is not a sequence type). Regression: `memo[-1]` in `test_negative_indexes_count_from_the_end_of_fully_visible_sequences`. |
| 3 | nit | `describe` scopes kept the newest 200 and dropped the earliest without saying so. Generator expressions printed `lines N-None`. | Fixed. Scopes are listed in source order, the first 300 are kept, and an `omitted` count is added. Scopes without a source span are skipped, and methods are class-qualified (`IPv6Address.is_private`). |

Found while checking the fix on a real JDK: **any indexed selector on a Java array** (`values[0]`) failed the whole session with `Invalid array range: 0 to 512`. This bug also exists on main. The worker always requested 513 children to detect oversize collections, and java-debug rejects ranges past the end of an array. The worker now requests exactly `indexedVariables` children when the adapter states that count, and refuses more than 512 before making the request. Java array elements now resolve from either end, and a 600-element array fails per selector without ending the session. Regression (real JDK): `tests/java/test_java_arrays.py`.

**Verification review of a7e79ab:** no blocking issues. The reviewer confirmed:
- `count = indexedVariables` returns the same children as before for lldb-dap and debugpy.
- The array-type parsing never yields a wrong length.
- `_python_scopes` handles decorators, async code, PEP 695 and nested classes.

One medium issue, which also existed on main, is fixed. Expanding a Java object (`p.x`) still sent a variables range, which java-debug rejects for anything that isn't an array, ending the session. Java runtimes now send no range for non-indexed containers; the size check after the reply still bounds the result. Regression (real JDK): `test_java_object_fields_expand_without_failing_the_session`. Two cosmetic nits are also fixed: PEP 695 scope names were double-qualified, and the scope-cap message was misleading.

**Review of ccd49cf:** no blocking issues. One medium issue, which also existed on main, is fixed. java-debug sent String values uncut (`maxStringLength` 0 means unlimited), so a String over 4 MB (a local, or now an object field) exceeded the 4 MiB DAP frame limit and ended the session. The Java adapter now caps Strings at 4096 characters; the worker shows 2000. The misleading comment about reply bounds is also corrected. Regression (real JDK, 5 MB String field): `test_multi_megabyte_java_strings_are_bounded_not_fatal`. It fails on the previous bundle and passes on the rebuilt one.

## Review: real program and `--run`/`--arg` (#28, 7c083c2)

**Result:** no blocking issues. Two low-severity issues are fixed.

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| 1 | medium-low | `session continue --to-exit` cleared breakpoints before the worker checked the stop. If the pause guard had already auto-resumed the program, it ran free, the command failed with "stale stop", and the agent was told to wait for a stop that could never come. | Fixed. The worker's `continue` takes `to_exit` and, under one lock, checks the stop before clearing the launch breakpoints. Regression: `test_continue_to_exit_never_clears_breakpoints_for_a_stale_stop`. |
| 2 | low | Background `verify --old-file/--new-file` read the files when the job ran, so rewriting them could make one job verify another candidate. | Fixed. The edit texts are read at submission and stored in the private job spec. Regression: `test_background_verify_fixes_edit_files_at_submission`. |

The reviewer confirmed these areas:
- No argument injection into the probe subprocess.
- debugpy never shell-expands list arguments.
- `entry` cannot escape the root, and only Python targets accept it.
- Stop validation still pins the requested module and line.
- The checker rejects the original, partial, overfit and deleted-entry variants.
- `ipaddress39.py` matches upstream v3.9.6 byte for byte.

The guide now notes `--arg=-v` for values that start with a dash.

## Review: Java exit inference (#29)

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| 1 | blocking | java-debug answers `threads` with an empty list for a **live** JVM when a thread is collected mid-lookup. The reviewer saw this on 25 of 25 attaches to a churning 2000-thread JVM. One empty reply would have reported an exit and detached or killed a live JVM. | Fixed. The check runs for launched JVMs only (`JavaAttachSession.liveness_by_threads = False`). One empty reply is only a hint: exit is inferred when the launched JVM process (from java-debug's `processid` event) is gone, or after a second empty reply at least 2 s later. A real stop or exit that arrives during the check wins. Regressions: unit tests, plus a real-JDK extreme-churn test that fails on the previous commit (false exit at about 6 s) and passes now. |
| 2 | blocking | A slow `threads` reply (many threads, or attach over SSH) raised `ResponseTimeout`, which closed the session. | Fixed. A slow or failed check counts as "still running" and never closes the session. Regression: `test_a_slow_or_failed_check_means_still_running_and_never_closes`. |

The reviewer confirmed:
- The pause guard never reaches the check.
- The synthesized event doesn't replay real events.
- The trace records no arguments, values, output or tokens.

While testing, java-debug itself was found to stall (no breakpoint within 40 s) on a launched JVM creating about 37,000 threads per second, even with the check disabled. This is an adapter limit, and Peeky fails closed with `TIMEOUT`.

**Verification review of d54041d / 95560f9:** two findings, both fixed.
- **Blocking:** the empty-reply count carried over between sessions on a long-lived worker. One empty reply in the next session could then end a live JVM. The count and check time now reset at launch and in `close()`. Regression: `test_empty_replies_never_carry_over_into_the_next_session`.
- **Medium:** a live pid did not block the two-reply rule. With a known pid, which the reviewer confirmed is the JVM itself, an exit is now inferred only if that process is gone. The two-reply rule applies only when no pid was announced. Regression: `test_with_a_known_pid_only_the_jvm_process_being_gone_proves_exit`.

## Review: optimized builds and unavailable locals (#32)

**Result:** no blocking issues for the default `-O0` path. Four medium/low issues for opt-in optimized builds, all fixed.

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| 1 | medium | LLDB zero-fills enum pieces it cannot locate in optimized code without reporting it, so a Rust `Option<i32>` view could show `None`/`Some(0)` labelled `active-variant`. | Fixed. With `opt_level` ≠ 0, Rust enum views are opaque (`<unverified Rust enum value>`). Real Rust regression. |
| 2 | medium | Sessions on optimized builds never said so, and snapshots kept the placeholder text in `value`. | Fixed. Launch, inspect and `get` carry `optimized`, the session text prints a warning, and unavailable entries have `value: null` with a `reason`. Real C session regression. |
| 3 | medium | Interventions on optimized builds could look applied while the program uses other copies of the value. | Fixed. Every `set_variable` in an optimized build is refused (`MUTATION_REJECTED`). |
| 4 | low | Read failures at `-O0` (for example `<error: parent is NULL>`) were called "optimized out", with advice to prefer `-O0`. | Fixed. The wording is neutral ("the debugger could not read a value") with the debugger's reason, unless the build is optimized. |

The reviewer confirmed:
- No real value can match the placeholder pattern; LLDB quotes strings and starts pointers with `0x`.
- Expanded children and session `get` are covered.
- `verify` builds with the same flags as the debugged build.

**Verification review of 84e6491:** no blocking issues; all four earlier fixes were confirmed. Two follow-ups are fixed:
- **Medium:** `peeky-agent` could never succeed on an optimized target, because it requires an intervention. It now rejects optimized configs up front with a clear message.
- **Low:** `describe` still advertised changeable locals and qualified enum views for optimized builds. It now reports `optimized`, `mutable_types: none ...`, and no enum views.

Both have real-build regressions in `tests/native/test_native_optimized.py`.

## Review: remote workers over SSH (#33)

**Result:** no security problems; URL and token handling were confirmed safe. Six issues, all fixed with regressions.

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| 1 | medium | The idle reaper could close a session right after a long operation returned. `last_used` was stamped only at the start, and also on rejected requests. | Fixed. Activity is stamped after the session check and again when the operation ends. |
| 2 | medium | A remote `session close` that failed on the transport deleted the only record of the session, so the close could never be retried. | Fixed. On `UNREACHABLE`/`TRANSPORT_LOST`, remote records are kept. |
| 3 | medium-low | A dead **local** worker was reported as a retryable `UNREACHABLE`. | Fixed. Local sessions check the worker pid (including zombies) and report `DEBUGGER_ERROR` with `session_closed: true`. A transport failure during a local launch reports that the worker was stopped. |
| 4 | medium | Remote programs (including `--set`) ran before the source check. | Fixed. The client sends `expect_sha256`; the worker refuses before launching, so a mismatched program is never executed. The client still double-checks, and discards output too. |
| 5 | low-medium | With older workers, the entry-script check was skipped. | Fixed. Results that ran without a reported hash are discarded. |
| 6 | low | `--worker ""` silently ran locally. | Fixed. `is not None` is checked, and the URL validation then refuses it. |

The docs now recommend `ssh -o ExitOnForwardFailure=yes` and distinguish a closed local tunnel port (`UNREACHABLE`) from a live tunnel to a dead worker (`TRANSPORT_LOST`).

**Verification review of 9640607:** no blocking issues. Two follow-ups are fixed:
- **Medium:** a local launch failure was still classified as `UNREACHABLE`/`TRANSPORT_LOST`, because the wrapped message kept the transport text. It is reworded (the cause is kept via `from exc`) and classifies as `DEBUGGER_ERROR` with `session_closed: true`. `_start` uses the local-worker client from the first wait.
- **Low:** a remote session whose worker is gone for good could not be closed. `session close --forget` drops the record without contacting the worker, and the `UNREACHABLE` hint mentions it.

## PR #35: captured-evidence store (`peeky sql`, `peeky queries`)

**Independent review of 88086e6.** The read-only enforcement held: `VACUUM INTO`, `ATTACH`, every pragma outside the whitelist (including the `pragma_*` functions), `ANALYZE`, `REINDEX` and `load_extension` were refused, and stores under paths containing `?#%` worked. Four findings, all fixed, each with a regression test:
- **Blocking:** the worker's HTTP probe process ran `peeky probe`, so it saved on the worker host and its `evidence` key reached the client. With `--no-save` the client then claimed a saved run that did not exist locally. The worker now passes `--no-save`, and `_probe_remote` drops worker evidence fields and worker save warnings (`test_remote_probes_are_recorded_by_the_client_only`, `test_worker_side_evidence_never_reaches_the_client_result`).
- **Should-fix:** program output over 1 MB made the insert fail, losing the whole run. The length limit now applies only to query connections, and stored output is cut to 1 MB with `output_truncated` set (`test_large_program_output_is_kept_within_the_value_bound`).
- **Should-fix:** queries opened the store read-write and created a missing one. A read-only store failed as `DEBUGGER_ERROR`, and a mistyped `--store` silently returned 0 rows. Queries now open the store read-only directly; a missing, empty or unreadable store is an `Evidence store` `INVALID_REQUEST` (`test_queries_never_create_or_write_the_store`).
- **Should-fix:** total result size was unbounded (rows × columns × 1 MB). Rows are now fetched one at a time under a 16 MB budget (`truncated_by: size`), with SQLite's column limit set to 100 (`test_the_whole_result_and_its_width_are_bounded`).
