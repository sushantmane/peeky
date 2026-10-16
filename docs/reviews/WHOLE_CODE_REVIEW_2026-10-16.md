# Whole-code review, 2026-10-16

Four independent reviewers read the whole codebase at `727ae42`, not a diff. Each covered one area: the engine (worker, DAP, client, errors), the CLI (cli, probe, evidence), native and Rust toolchains, and Java plus the agent harness. They checked correctness and efficiency, ran real toolchains, and reproduced every finding. In parallel, `scripts/profile_probe.py` split probe time into Peeky's own CPU, child-process CPU and waiting (see [PERFORMANCE.md](../PERFORMANCE.md#where-a-probes-time-goes-2026-10-16)).

The findings are fixed in three PRs. The `PR` column says which one; "this PR" means it is fixed here, with a regression test unless the row says otherwise.

## Efficiency

| Finding | Disposition | PR |
| --- | --- | --- |
| Every operation rehashed the toolchain binaries: clang (125 MB) 32× per C++ probe, cargo 30× per Rust probe; 58 ms per C++ operation | `peeky.digest.sha256_file` reuses a digest while (device, inode, size, mtime, ctime) are unchanged and settled. All checks still run. C++ probe 2.28 s → 0.73 s, C++ operation 58 → 0.8 ms, Rust probe 1.35 → 1.04 s | A |
| `build.prepare` listed each compiler once per command, so hashing grew quadratically with source count (16 C files: 10 s) | Tools are deduplicated by identity, and repeated hashes hit the cache | A |
| The Java adapter bundle (273 files) was walked and rehashed by every check: about 120 ms of JVM pause per hit | The verified bundle is reused while a stat fingerprint of the whole tree is unchanged; any added, removed or written entry triggers full verification. Pause per hit 120 → 9.9 ms | A |
| `run_probe` runs a full `inspect` per selector | Selectors share one inspection; the stop-validation inspection is reused; a selector that runs out of expansion budget is retried on a fresh inspection (`test_selectors_share_one_inspection_unless_budget_runs_out`) | B |
| The Python probe's ~0.7 s exit wait | Inside debugpy (pydevd threads wait on each other with a 0.5 s timeout). Peeky cannot shorten it safely; `--until hits` skips it. Documented | — |
| About 25 ms of imports per CLI command | Measured; low value, not changed | — |

## Correctness

| Finding | Severity | Disposition | PR |
| --- | --- | --- | --- |
| C/C++ auto-detection linked every sibling source, so `examples/c/pagination_bug.c` failed (duplicate `main`) | should-fix | Only the target's `main` is linked with the files that define none; an ambiguous directory is an actionable error | A |
| C++ builds ran the resolved clang (the C driver), so libc++ programs failed to link | blocking | `--driver-mode=g++` for C++ compile and link | A |
| macOS `/usr/bin` clang, dsymutil and dwarfdump are one xcrun stub, so provenance hashed the stub | should-fix | `build.real_tool` resolves stubs through `xcrun --find`; the dsymutil drift guard is kept | A |
| Java `set_variable` silently wrapped, truncated or inverted values (int 3000000000 → -1294967296, boolean 1 → false) | should-fix | `JavaSession.validate_variable` checks the declared type: integer ranges, true/false, finite float/double; other types are refused | A |
| Java attach hashed the files again after parsing them, so a rebuild in between became the baseline | should-fix | Digests come from exactly the bytes that were parsed; a change before the baseline is refused | A |
| A Java breakpoint on a line without code failed the whole launch as `DEBUGGER_ERROR`; inspecting a caller frame outside the program closed the session | should-fix | `check_breakpoint_lines` gives `INVALID_BREAKPOINT` listing nearby lines; a non-top frame outside the program is a request error and the session survives (all runtimes) | A |
| `peeky-agent` aborted on any worker refusal and lost the decision | should-fix | Refusals that change nothing (`INVALID_REQUEST`, `INVALID_BREAKPOINT`, `MUTATION_REJECTED`, `NOT_FOUND`, `UNSUPPORTED`) are step errors; every other failure is recorded with its decision, then raised | A |
| `setup_java_adapter.py` wrote an unresolved bundle path (`/tmp` vs `/private/tmp`) | nit | The output parent is resolved | A |
| Java attach `timeout_ms` above 15 s had no effect | nit | Capped at 15000 | A |
| A debugpy debuggee survives a transport failure (its own process group) | blocking | The DAP `process` event's pid is recorded. On close, a debuggee that left the adapter's group is killed, but only if its ancestry runs through that group. `disconnect` is still sent after a read-side failure (`test_a_failed_transport_still_ends_the_launched_program`) | B |
| Two `session start` runs with the same name both succeed | blocking | The name is reserved with `O_EXCL` before anything is spawned (`test_two_starts_with_one_name_cannot_both_succeed`) | B |
| lldb-dap per-thread `stopped` events are reported as new stops after `continue` | should-fix | When all threads continue, events received before the request are consumed; breakpoint updates among them are kept (`test_threads_stopping_together_are_one_stop_not_stale_new_ones`, real lldb) | B |
| Python breakpoint `condition`/`hitCondition`/`logMessage` evaluate code in the debuggee | should-fix | Only `{"line"}` is accepted, and the capabilities no longer advertise these (`test_python_breakpoints_never_carry_code`) | B |
| The client sends the bearer token through `http_proxy` | should-fix | Worker calls use an opener with no proxies (`test_worker_calls_never_use_an_http_proxy`) | B |
| `report` history has no byte bound | should-fix | Entries over 256 KB keep only size and digest; the oldest go once the history passes 4 MB (`test_history_is_bounded_in_bytes`) | B |
| "DAP frame exceeds limit" classified as `BUDGET_EXHAUSTED` | should-fix | Protocol-stream failures are `DEBUGGER_ERROR` with a session-closed hint | B |
| Workers leak when `session start` is interrupted; workers never exit after idle expiry | should-fix | Until its record is saved, `session start` stops the worker on any failure or interrupt and frees the name. Workers close their debugger on SIGTERM, and session workers run with `--exit-when-idle` (`test_an_interrupted_start_…`, `test_workers_close_their_program_on_sigterm`, `test_session_workers_exit_after_their_idle_timeout`) | B |
| A relative Java `bundle` path resolves against the CWD or state dir | should-fix | Resolved against the configuration file for `java` as well as `java-attach` | B |
| Remote probe failures misreported; JSON mode can print two documents | should-fix | `_probe_remote` gives partial replies the full result shape and keeps their error | B |
| `session get` aborts all selectors when one fails remotely | should-fix | Per-selector worker errors (`BUDGET_EXHAUSTED`, `NOT_FOUND`, `STALE_HANDLE`) become row errors | B |
| `--timeout` validated after the action ran | should-fix | `--timeout` and `--max-pause` are checked first | B |
| Python list indexes ≥ 100 reported as not found | should-fix | Reported as beyond the debugger's visible window | B |
| `describe` on a syntax error reports `INTERNAL_ERROR` | should-fix | `INVALID_REQUEST` naming the file and line | B |
| Non-ASCII auth header and pre-dispatch errors are misreported; `session close`/`list` JSON gaps; log path shows an fd | nit | 401 for any bad header; body errors are `INVALID_REQUEST` with the session untouched; `close`, `close --forget` and `list` emit JSON; the log path is printed | B |
| Rust i32 mutation can write a debug-only copy (`.dbg.spill`) that the program never reads | blocking | Builds also emit LLVM IR. A change is allowed only if every declaration of that local in the stopped function (matched by its symbol hash) is a real memory slot; otherwise `MUTATION_REJECTED` explains why (`test_locals_the_program_keeps_in_registers_cannot_be_changed`, real cargo: `MARK:1007` stays unchanged) | C |
| Files included from outside the declared inputs (`#include "/abs"`, `#[path]`, `include!`) are not hashed | should-fix | Rust: every file in rustc's dependency files must be a declared source, or the build fails. C/C++: `-MD` lists every file read; headers outside the workspace (absolute includes, SDK and system headers) are hashed and rechecked before every operation, at about 2 ms for an `<iostream>` program's 824 headers (`test_sources_from_outside_the_crate_fail_the_build`, `test_headers_read_from_outside_the_workspace_are_provenance`) | C |

## Review of PR A (#38, 4a1195f)

No blocking issues. Five should-fix issues and two nits, all fixed, each with a regression test:
- **Should-fix:** the caller-frame downgrade also covered module mismatches and line mismatches inside the program's own sources. Now only `validate_stop` failures for frames whose source is outside the prepared or declared directories become request errors, and `validate_debugger` always fails closed (`test_module_mismatch_on_any_program_frame_closes_the_session`, `test_only_caller_frames_outside_the_program_are_request_errors`).
- **Should-fix:** native configs on macOS without `sysroot` lost the SDK once the stub was replaced by the real clang. They now use `xcrun --show-sdk-path`, which is recorded in the build commands (`test_macos_builds_without_a_sysroot_use_the_default_sdk`).
- **Should-fix:** `main` detection counted `main` functions that are commented out or behind `#ifdef`. When more than one file matches the quick text check, the compiler's preprocessor decides (`test_mains_that_are_not_compiled_do_not_split_a_program`).
- **Should-fix:** java-debug parses integers with `Long.decode`, so `010` meant 8. Integer literals must have no leading zero (`tests/unit/test_java_values.py`).
- **Should-fix:** adapter setup failed when the output's parent did not exist yet, which is the documented `work/` path in a fresh clone (`test_setup_accepts_an_output_whose_parent_does_not_exist_yet`).
- **Nit:** a source reached through an in-root link raised `KeyError` on attach. Sources are now keyed by their resolved path (`test_declared_sources_behind_an_in_root_link_attach`).
- **Nit:** the attach-race error is reworded so it classifies as `PROVENANCE_CHANGED`.

## Review of PR B (#39, 981c6c8)

One blocking and two should-fix issues, all fixed with regression tests:
- **Blocking:** the cursor fix consumed the `stopped` events of the other threads that hit the breakpoint in the same all-threads stop. Those were real hits, and resuming steps each thread over the breakpoint, so a probe reported "Only 1 of 4 hits" when there were four. While every thread is stopped and breakpoints remain, `continue` now serves each co-stopped thread as the next stop before resuming anything. `continue --to-exit`, and continues after the breakpoints are cleared, resume at once. `test_threads_stopping_together_are_one_stop_not_stale_new_ones` now requires exactly 4 hits with ids 0–3, across three real-lldb runs per test.
- **Should-fix:** a `session start` killed with SIGKILL left a "starting" record that `close --forget` could not remove. `close --forget` no longer reads the record. A new start takes over a claim whose starter is dead, or an empty one older than 30 s, under a lock. A command on an abandoned claim is `NO_SESSION` (`test_an_interrupted_start_never_blocks_its_name`).
- **Should-fix:** with a shared inspection, `items[-1]` could see a budget-starved page and report "more than 512 elements". That message now also triggers the one fresh-inspection retry (`test_a_shared_inspection_never_makes_a_readable_value_an_error`, which fails without the fix).

## Review of PR C (#40, 8987f88)

One blocking issue and one nit, both fixed with regression tests:
- **Blocking:** `#[inline(always)]` functions are inlined even at opt-level 0. LLVM renames their slots (`marker.dbg.spill.i`), and the end-anchored spill pattern missed the rename, so a change inside an inlined callee was reported as applied while the program printed the old value. Now any name containing `.dbg.spill` is a copy, and any declaration whose location is `inlinedAt` another function is never provably in memory (`test_locals_of_always_inlined_functions_cannot_be_changed`, real cargo and lldb-dap; the IR unit test covers both forms; both fail on the previous parser).
- **Nit:** rustc writes `# env-dep:NAME=value` notes into `.d` files, and a value containing `": "` was read as a prerequisite. Comment lines are skipped (a literal `#` in a path is escaped as `\#`).

## Review of the cookbook PR (#43, 99faeda)

Three should-fix issues and three nits, all fixed:
- **Should-fix:** selectors with `[...]` were unquoted in the cookbook and README, so zsh (the macOS default) globbed them and the command never ran. Every such selector is now quoted, and section 2.1 says why.
- **Should-fix:** section 3.1 built a command in `$P`, which zsh does not split into words. It now uses a shell function.
- **Should-fix:** run records showed the requested `--arg` list, not what the program received. The launch reply now carries `program_args` (per-run arguments, else the configured ones), and `target_identity.arguments` matches it. `runs.args` records that list, falls back to the requested one when launch failed, and is NULL for an attached JVM, whose arguments are unknown (`test_runs_record_the_arguments_the_program_received`, plus real Java, C and Rust assertions in the per-run argument tests).
- **Nits:** the troubleshooting row now says values come from the line named in the warning (Python moves breakpoints back, LLDB forward); the gdb table no longer maps `finish` to `continue`; the `--arg` help no longer says "Python:".

## Review of the evaluation-fixes PR (#44, 57fea56)

One blocking issue, three should-fix issues and three nits. All are fixed, each with a regression test (real debugpy where it matters):
- **Blocking:** debugpy 1.8 lists the first 500 entries of a dict, then a child named `Unable to handle:`, a marker the partial-mapping guard did not recognise. So `big['k502']` in a 505-entry dict was reported as not found. Now that marker, the older markers, or a `len()` larger than the number of listed entries all mean "only part of a large mapping" (`test_mapping_keys_never_read_the_wrong_entry`, `test_keys_missing_from_a_partial_mapping_are_not_called_absent`).
- **Should-fix:** debugpy never gives two children the same name. A later key with the same repr appears as `"'a' (id: N)"`, so the ambiguity check could not fire, and `w['a']` read the entry of an object whose `__repr__` returns `'a'`. A key is now accepted only after every page of entries has been seen, and any `<repr> (id: …)` sibling makes it ambiguous (`test_a_key_whose_repr_is_shared_is_ambiguous_on_any_page`, plus the real-debugpy case).
- **Should-fix:** `session get` now lists children, and one child name or type over 2000 characters (a dict keyed by a long string) raised a session-closing error. Over-long child identities are now shown as a non-expandable, non-selectable placeholder (`<3002-character name>`); root locals still fail closed.
- **Should-fix:** the `inspect` hint ignored `--name`, so following it read another session. It now includes `--name` when the session is not `default`.
- **Nits:**
  - A child listing that hits the node budget is retried once on a fresh inspection, as values already are.
  - `--capture` splits on commas outside quoted keys, so `d['a,b']` works locally and through the worker's probe subprocess.
  - Key escapes are validated by pattern (no `\d`, `\777` or unknown `\N{}`) instead of process-global warning filters, which HTTP handler threads could race on. 20k random keys round-trip.
  - Unreadable children show `<no readable value: reason>` instead of `None`.

Verification of cf8cb1f found one regression and one nit, both fixed:
- **Should-fix:** reading every page before accepting a key made ordinary reads fail once a busy frame used up the 512-node window (`big['k3']` with 12 locals, `outer['a']['k1']`). It also still read an impostor when the real key was past debugpy's 500-entry cap. Now a key matches only an entry that debugpy marks evaluable. debugpy gives a dict entry an `evaluateName` only when its key's repr evaluates back to the key; the worker passes that on as a boolean, never the expression. Reads stop at the match again, and an `(id: N)` sibling still makes a key ambiguous (`test_keys_read_in_busy_frames_and_never_from_an_impostor`, real debugpy; `test_a_key_shown_by_an_impostor_object_is_refused`). One limit remains: a `str` subclass whose own `__repr__` mimics another string passes debugpy's `isinstance` check.
- **Nit:** the over-long-identity placeholder skipped the "no value" check and qualified value views. It now applies the first, and shows values that need a view as not shown (`test_over_long_child_identities_keep_value_rules`).

A final check of f555349 confirmed that debugpy carries `evaluateName` through attributes, method frames, list, tuple and deque elements, nested and subclassed dicts, dataclasses, slots and module globals. It found one should-fix and three nits, all fixed:
- **Should-fix:** the `session get` listing still offered `imp['a']` next to the impostor's value. Listings and reads now share one rule. A key must be evaluable. An index must be evaluable whenever its siblings are, which catches `{N(): …, 1: …}` where `N.__repr__` returns `1`. Debuggers that mark nothing evaluable (LLDB, java-debug) keep reading indexes. A plain-named evaluable entry is the real key, so the `(id: N)` check and its misleading message are gone.
- **Nits:** entries debugpy files under "function variables" or "class variables" are named as unreachable instead of reported as missing, and they no longer trip the `len()` partial check (`test_impostor_entries_get_no_selector_and_are_never_read`, `test_entries_debugpy_groups_by_value_type_are_explained`, plus the real-debugpy listing check).


## Review of the evidence-diff PR (#45, ce54aa0)

One blocking issue, three should-fix issues and four nits. All are fixed with regression tests:
- **Blocking:** address masking (`0x` + 8–16 hex digits) also hid NULL, so a list cut short (NULL against a live pointer) was ranked as "addresses only" below later rows. Masking now needs at least 9 significant hex digits and never applies to all-ones values, so NULL, `-1`/`MAP_FAILED` and 8-digit data such as `0xdeadbeef` stay real differences (`test_a_null_pointer_is_a_real_divergence`, plus the masking cases).
- **Should-fix:** "only in run a/b" was decided from captured rows, so a run that never stopped turned every selector into one empty `only in` row. It is now decided from each run's requested selectors (`runs.capture`, via `json_each`), so such a run shows `missing in run b` for each hit. `diff`, `compare` and `outputs` refuse a missing run, or a store with only one run, with a clear `INVALID_QUERY` (`test_a_run_that_never_stopped_is_missing_hits_not_selectors`, `test_comparisons_need_two_runs_that_exist`).
- **Should-fix:** `--hits all` with more than 20 selectors exceeded the 2000-row probe budget and failed mid-run. Its cap is now the largest hit count that fits the budget, and the cap warning names it.
- **Should-fix:** `--hits all --until hits` (the attached-JVM default) could only end at the deadline. That combination is refused up front (`test_hits_all_fits_the_row_budget_and_needs_the_program_to_exit`).
- **Nits:**
  - After-set rows from a run with an intervention, compared with a run without one, are noted `intervention only in run b` and ranked after real differences.
  - The text schema lists `no_addresses`.
  - Repeated selectors are de-duplicated.
  - Progress ends with "program finished after N hit(s)", and the docs describe that.

Verification of a63c113 confirmed every fix and found one more should-fix and one nit, both fixed:
- **Should-fix:** SQLite checks the query deadline only between VM steps, never inside a Python function. So a query calling `no_addresses` 20 times per row on hex-dense text ran 81 s against a 1 s deadline. The function now checks the deadline itself, and masking is a single regex with no Python callback per token (`test_no_addresses_cannot_outrun_the_query_deadline` stops within the bound).
- **Nit:** the `set_hit` error names the `--hits all` cap.
