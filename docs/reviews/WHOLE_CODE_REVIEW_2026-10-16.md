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

## Review of the compiled-describe PR (#46, 59947ce)

One blocking issue, three should-fix issues and three nits. All are fixed:
- **Blocking:** Rust scopes read the whole binary's debug info, which is mostly std. That was 4.8 MB of line table on Linux, over the 2 MB cap, so the Rust CI lane failed. On macOS it was 16.7 MB of DIEs, at the 16 MB cap. `RustSession.function_scopes` now dumps only the application units `prepare` already selected (per-unit `--debug-info=0x…` and `--debug-line=0x…`), which takes 0.8 s for the example crate.
- **Should-fix:**
  - Text `describe` prints `function names: unavailable (reason)`.
  - Spans no longer absorb inlined callees. Each line-table row belongs to the innermost function or inlined call, from the row's own file, whose address range (`low_pc`/`high_pc` or a multi-line `DW_AT_ranges`) covers it. So an inlined same-file helper keeps its lines (and is listed), and std code inlined around the caller's rows does not take them.
  - `decl_file` is the first one found along the origin chain, so optimized out-of-line constructors are listed.
- **Nits:**
  - Names are decoded from dwarfdump's octal escapes (`größe`, `zähler`).
  - Rust legacy symbols are demangled into readable names (`peeky_pagination::Pager::pages`, `<peeky_pagination::Pager as core::fmt::Display>::fmt`, `…::{{closure}}`) instead of DWARF's `{impl#0}`.
  - Copies are merged through their declaring DIE (C1/C2 constructors, inlined plus out-of-line copies). Identical entries from several units are listed once. Address-0 and tombstoned copies are skipped.

Regression tests: `test_rows_belong_to_the_innermost_function_from_their_own_file`, `test_rust_symbols_are_demangled_like_lldb_frames` and `test_identical_scopes_from_several_units_are_listed_once`. The reviewer's C++ `always_inline`, C++ -O2 constructor, non-ASCII and Rust impl/closure/trait/`#[inline(always)]` cases were rerun with real toolchains.

Verification of 81123e9 confirmed every fix on macOS and on Linux LLVM 18. Hosted CI could not start (a billing block), so the reviewer ran the Rust, C and C++ lanes in Docker. It found one more should-fix and two nits:
- **Should-fix:** the docs claimed `describe` names match the probe's `function` column. Plain functions and methods match. Rust trait impls and closures keep LLDB's symbol escapes there, and C++ frames add signatures. The cookbook now says so and suggests `LIKE` in SQL.
- **Nit:** a lambda inside a function that exists only inlined (-O1 or higher) lost its function name, because clang parents it under an empty placeholder. The name now comes from the lambda's Itanium local-entity symbol (`_ZZ11apply_twiceiE…`), giving `apply_twice::operator()` at -O2.
- **Nit:** a single very large optimized C++ unit can exceed the 16 MB dump bound. Splitting by unit does not help there, since it is one unit. It fails closed with a visible reason, and the cookbook documents it.



## Review of the session-stepping PR (#47, 3dc8add)

One blocking issue, five should-fix issues and two nits. All are fixed with regression tests (real debugpy, lldb-dap and Java):
- **Blocking:** Python's `validate_stop` never raises, so the "left the program" refusal never fired for Python, and `set` changed locals in a module outside the root. Step stops are now checked before validation for every runtime. A frame counts as the program's own only if it is in a declared source (compiled targets) or under the worker's root (Python). Elsewhere `inspect`, `get` and `set` are refused as request errors and the session is kept (`test_steps_into_code_outside_the_root_read_and_change_nothing`).
- **Should-fix:**
  - **Co-stops.** After `continue` served a co-stopped thread, lldb re-reported the other thread's old breakpoint stop when a step ended. `step` is now refused until a real `continue` has resumed every thread (`test_steps_wait_until_threads_stopped_together_have_resumed`).
  - **Stepping out of the outermost frame.** The debugger's refusal closed the session. Refused requests are now a distinct `RequestFailed`; a refused step is a request error, and nothing moves. The state says when your code is no longer on the stack (`returns_to_program`), and in that case it suggests `continue`, not `step --out`.
  - **Frames without line information.** LLDB's `module`function` frames under the build directory counted as inside the program, so `inspect` closed the session on a false provenance mismatch. Membership is now by declared source, and such paths are never shown as user files.
  - **Older remote workers** silently ignored `continue --to`. The client now checks `version` operations first for remote sessions.
  - **`continue --to` to a line the debugger moves or cannot place** ran the program on. The reply is checked; the session's breakpoints are restored, the stop is kept, and the error is `INVALID_BREAKPOINT`.
- **Nits:**
  - `continue --to-exit` finishes a step that a breakpoint interrupted (LLDB completes it first), with at most three extra resumes.
  - The `breakpoints` operation records the session's breakpoints only after the debugger accepts them.
  - `inspect` offers `session get` only for selectable names.

Verification of the fixes confirmed all six repros. It found two regressions that the fixes had introduced, both now fixed:
- **Should-fix:** java-debug verifies a line in a class that hasn't loaded yet (a nested class) only once the class loads, so the new placement check refused valid Java `continue --to` lines. Runtimes that prove lines against their own tables (`check_breakpoint_lines`) now have only a moved line refused (`test_java_sessions_run_to_a_line_but_refuse_steps` now targets the not-yet-loaded `Inner`).
- **Should-fix:** `co_served`, `stop_outside` and `stop_returns` carried over into the next session on a shared worker. They now reset at launch (`test_a_new_session_on_a_worker_starts_with_no_stop_state`).

## Review of the Java-stepping PR (#51, a7f267a)

One blocking issue, two should-fix issues and one nit. All are fixed with real-JVM regression tests:
- **Blocking:** java-debug suspends each thread separately (`allThreadsStopped: false`). `step` moved the event cursor past another thread's pending breakpoint stop, which left that thread suspended indefinitely, even past the attach pause budget. Now the cursor moves only for all-threads stops, and `step` is refused while another thread's stop is pending; `continue` serves that stop first (`test_java_steps_wait_for_threads_stopped_separately`, five runs in a row).
- **Should-fix:** `skipSynthetics` made lambda bodies (synthetic `lambda$main$0`) unreachable by `step --into`. It is now off: `--into` on `xs.forEach(x -> sink(x))` reaches the lambda body, then `sink` (`test_java_step_into_reaches_lambda_bodies`).
- **Should-fix:** java-debug applies class filters only to step into and step out. A plain step at the end of a method that JDK code called can stop in JDK code, where reads are refused. The docs now say so.
- **Nit:** the launcher accepted `"1"`, `1.5`, `-1`, `0` and out-of-range `threadId` values. Only a positive integer is accepted now (adapter test).

Verification of 17ff75f confirmed all four fixes on launched and attached JVMs (two threads with `--max-pause`, lambdas, JDK callers, raw `threadId` values). It found one regression in LLDB sessions, now fixed:
- **Should-fix:** `pending_stop_elsewhere` also applied to all-threads stops. Stepping over a call during which another thread hit the breakpoint left the stepping thread's own `step` event pending, so every later `step` was refused. The check now applies only when threads stop separately (java-debug); for all-threads stops `co_stopped`/`co_served` already cover the other threads. `test_a_step_interrupted_by_another_threads_hit_can_still_step` (real lldb-dap, three runs) fails without the fix.

Two older problems found in passing, not caused by this PR: a Java breakpoint on a line owned by two classes fails at start with `DEBUGGER_ERROR` instead of `INVALID_BREAKPOINT`; and debugpy reports one stop when two threads reach a breakpoint together.

## Review of the Rust container-view PR (#52, 5f9faf2)

The review confirmed the memory-reference semantics (lldb-dap 20, 21 and main: the variable's own load address), `view_read`'s bounds, the worker-owned handles and the layout checks. It reported two blocking findings, two should-fix and one nit. All are fixed:
- **Blocking:** memory views showed freed, moved-from and uninitialized storage as contents. A dropped `Vec` read as a later `Vec`'s elements; a slice that outlived its owner, a `String` moved into its shadow and a deferred `let label: String;` behaved the same way. Nothing in DWARF or memory shows a local's liveness at opt-level 0.
  - Containers are now read only for **reference parameters** proven by the build's LLVM IR (`rust_ir.ir_locals`). The parameter must have an `arg:` declaration, a single non-inlined slot of its own, and a declared type of `&[i32]`, `&mut [i32]`, `&str`, `&mut str`, `&Vec<i32>`, `&String` or their `&mut` forms. The frame must be past the parameter's declaration lines: rustc stores parameters in the entry block before any statement, as the IR shows.
  - A reference parameter's referent outlives the call. The borrow checker rejects assigning it anything shorter-lived, even by direct reassignment, through `&mut` or in a closure (checked with rustc), so borrowed and reassigned parameters qualify too.
  - `&mut` parameters are read only in the innermost frame. From an outer frame, a callee such as `retain` or `sort_by`, calling back into user code, may be midway through changing the referent.
  - Further checks:
    - a thin reference's slot must hold the pointer the debugger shows;
    - a slice must sit at the address the debugger shows;
    - `length * size` must not exceed `isize::MAX`.
  - Owned locals and reference locals stay raw (`ordered.len` works); #53 tracks a compiler-derived liveness proof. Tests:
    - `test_rust_reference_parameters_are_read_from_memory_and_locals_never_are` (real toolchain: dropped, moved, shadowed and outliving locals stay raw);
    - `test_reference_parameters_are_proven_only_from_their_entry_spill`;
    - `test_rust_sessions_read_containers_only_past_a_proven_parameters_declaration`;
    - `test_only_proven_reference_parameters_are_read_from_memory`;
    - the thin-reference cross-check tests.
- **Blocking:** the worker's 2000-character value limit cut long views mid-number and dropped the `(first N of M)` marker. Values are now built within 1000 characters, keeping only whole elements or characters, and always end with their marker (`test_long_containers_keep_whole_elements_and_their_marker_within_the_value_limit`).
- **Should-fix:** `ordered.len` stopped working on debuggers with `readMemory`, and the length of a large container could not be read at all. Views now carry read-only `length` (slices, `&str`) or `len` and `capacity` (`&Vec`, `&String`) children from the same header read. Text views no longer treat their byte length as an element count, so `label[0]` says text is shown whole instead of "beyond the visible window".
- **Should-fix:** the docs and the error message said readMemory arrived in LLVM 19; it arrived in LLVM 20. Both now say LLVM 20, and say that text is not indexed.
- **Nit:** fewer than the documented 512 elements were selectable within the 512-node budget. Views now decode 256 elements, which stay selectable alongside the locals, and the docs say 256.

Verification of 6b94246 found no blocking or should-fix issues. The reviewer re-ran all four liveness repros (they stay raw) and these parameter variants:
- borrowed and reassigned parameters are read, and show their current value;
- a shadowed parameter is refused;
- in an outer frame during `retain` and `sort_by` callbacks, `&mut` stays raw and `&String` is read;
- a stop on the `fn` line and `step --into` both work;
- a one-line `fn` stays raw.

The reviewer probed the soundness argument through async fns, pattern parameters, generics, `impl Trait`, closures, inlined copies, `mem::swap` and writes through `&mut`, and found no hole in safe Rust. It also checked the display limits (Vec, multi-byte and escaped text, `i32::MIN`) and the enum views. It found two problems in the follow-up commit beb5230, both fixed:
- **Should-fix:** the LLVM 20 Docker image could not be built, because `python3-lldb-18` and `-20` conflict. The image now unpacks `lldb-20` and llvm-20's own Python tree from verified downloads, so lldb-dap-20 starts without errors and lldb-18 is untouched. The `rust-llvm20` lane sets `PEEKY_EXPECT_READ_MEMORY=1`, so it fails if values fall back to raw fields.
- **Nit:** AGENT_GUIDE said optimized builds show containers opaque. Optimized IR has no parameter proof, so those parameters keep their raw fields or show as optimized out. The sentence now says so.

## Review of the second-evaluation fixes PR (#60, b70ae63)

The review confirmed several things:
- `receiver` is never set for C, Python or Rust.
- Cargo crates are still detected first.
- The `--` rewrite applies only to `probe` and `session start`, and survives `--background` replays.
- JSON rows keep raw frame names.
- The multi-function warning holds for failures and `--hits all`.
- The cookbook transcripts match.

It reported one blocking issue, three should-fix and two nits, all fixed:
- **Blocking:** implicit `this` read a member whenever no local had the name. The debugger's local list can be incomplete, and in three real cases Peeky showed the member's value under the local's name:
  - an attached Java class without a local-variable table, where the setter parameter `total` was missing;
  - a C++ function-local static, which lldb-dap lists outside Locals;
  - a scope truncated at 50 variables.

  Members are no longer read implicitly. A bare member name's error names the selector (`select it as this.head_ if the code means the member`), and `this.head_` reads it. `test_a_static_that_shadows_a_member_is_never_read_as_the_member` uses real lldb-dap, and the unit test covers nested selectors and shadowing.
- **Should-fix:** nested implicit selectors were reworded as "not a member". This is gone with implicit reads: the error now names the first segment's selector.
- **Should-fix:** `session set head_=2` (and `probe --set`) sent agents to "dotted paths". Both now say the member can be read as `this.head_` and that only the function's own locals can be changed, and the `NOT_FOUND` hint covers reads and changes.
- **Should-fix:** `describe proj --at ring/ring.cpp` printed a command that failed. A C/C++ program in a subdirectory is now refused with the command that works (`peeky probe …/proj/ring --at ring.cpp:<line>`).
- **Nit:** a symlinked candidate outside the directory was taken by `--at <line>`. Only files whose real parent is the directory count, and the several-programs message suggests `--at <file>:<line>`.
- **Nit:** `display_function` raised on impossible `$u…$` escapes. They stay as written, here and in `describe`'s symbol demangler (`peeky.names.unicode_escape`).

## Review of the session-hits PR (#62, bd241d8)

The review confirmed several things:
- LLDB threads that stop together are numbered one by one, and Java's separate thread stops are numbered too.
- Steps onto a breakpoint line are counted correctly.
- No stale number leaks into later states.
- The two-run queries keep their parameters and NULL semantics.

It reported two blocking issues, two should-fix and two nits, all fixed in 1240cdc:
- **Blocking:** `continue --count N` counted every stop and resumed a crash stop, which lost it. `--count` and `--hit` now count only numbered breakpoint stops. Anything else (a crash, a finished step, the exit) ends them there, shown with a note and the last hit reached, and nothing is resumed past it. Test: `test_counting_stops_at_a_crash_and_never_resumes_it`.
- **Blocking:** hit numbers were wrong in three reproducible cases, all now handled:
  - **Other threads' stops at a served `continue --to` line** compare against that line until the next real resume.
  - **A `--to` line where the debugger had placed the session's breakpoint** counts. Own lines now include the placed lines, plus `breakpoint` events for the session's breakpoint ids.
  - **A stop whose line could not be read while a `--to` line could apply** ends numbering for the session (`numbered: false`), never a guess.

  Tests in `tests/unit/test_session_hits.py`.
- **Should-fix:** `--hit` against an older worker silently returned the first stop. States now carry `numbered`. A worker without it is named ("the worker predates hit numbers: update Peeky on the worker host"), and `--count` counts its breakpoint stops by reason.
- **Should-fix:** reaching the deadline before the target exited 0 with no note. Now the note reads `reached K of N stops before --timeout (…); last hit H`, and the exit status is 3, as is any run that fell short.
- **Nit:** the `(output)` row could show two identical 200-character prefixes. It now shows each output from just before the first differing character, and the note gives the offset (`output differs at character 213`).
- **Nit:** the cookbook's `--hit` note now gives the last hit reached.

The verification of 1240cdc confirmed seven of the eight repros. It found one remaining blocking issue, now fixed:
- **Blocking:** java-debug stops each thread separately, so another thread's stop at a `continue --to` line was served after the next real `continue` and numbered. The same happened to a co-stop at that line after a crash stop. Classification no longer depends on when a stop is served: the session's only breakpoints are its own lines and its `--to` lines. A breakpoint stop at an own line (requested or placed) is a hit, and one at a `--to` line that is not an own line never is. Without any `--to`, every breakpoint stop is the session's own. Anything else ends numbering.

  Tests:
  - `test_threads_stopped_separately_at_a_to_line_are_never_numbered`, a real JVM with two threads (3 of 3 runs);
  - the updated unit tests, including a crash followed by a co-stop.

The final check confirmed the Java, crash-then-co-stop and LLDB repros, plus moved breakpoints in Python, C and Rust, and found no regressions. One should-fix remained, now fixed:
- **Should-fix:** before any `--to`, a stop at a known line Peeky did not place was numbered. An example is a Python program's own `breakpoint()` call, which debugpy reports with reason `breakpoint`. It shifted every later hit away from the probe's numbering. The "no `--to` used" rule now applies only to a stop whose line could not be read; a known line that is neither own nor `--to` ends numbering. Tests:
  - `test_a_programs_own_breakpoint_call_ends_hit_numbering` (real debugpy);
  - `test_a_stop_at_a_line_peeky_did_not_place_ends_numbering`.

## Review of the verify-output PR (#63, d37996a)

The review confirmed that the pass/fail decision is untouched (`clean` is computed before `failures`) and that nothing consumes `descriptor` or `build_manifest` from the CLI's verify JSON. `job wait` replays `--details`, and the evidence store records probes only. It reported three should-fix issues and two nits, all fixed in 92934be:
- **Should-fix:** the program under test could add a failed-test name by printing `test x ... FAILED` into a panic message. Names now come only from libtest's own result lines before its `failures:` section, line-anchored, and never more than the summary's failed count. Control characters in messages are escaped for the terminal.
- **Should-fix:** failed `#[should_panic]` tests (`test t - should panic ... FAILED`) were not named. They are now.
- **Should-fix:** zero tests ran but the text said "no test summary". It now says `0 tests ran`; "no test summary" is kept for when there is none.
- **Nit:** overflowing test output, and a nonzero exit despite an `ok` summary, are now explained.
- **Nit:** `--keep` keeps the edited file and, for compiled programs, its build evidence beside it. The help and text now say so.

Tests: `test_failed_test_names_come_only_from_libtest_result_lines` and `test_compiled_test_text_says_why_when_nothing_ran_or_output_overflowed`.

The verification of 92934be confirmed all five fixes and found three more should-fix issues in the displayed failure list (never the verdict), all fixed:
- A program writing straight into libtest's stdout (a test using `.status()`) could still put a name among the result lines. Names now come only from libtest's final `failures:` list, which libtest prints after every test has finished. They are escaped for the terminal like messages.
- One test's escaped output could supply another's message. Message blocks are now matched on their own line (`\n---- name stdout ----\n`).
- A `should_panic` message ran into libtest's footer. Messages now end before the final list.

Test: `test_failed_test_names_come_only_from_libtests_final_list`.

## Review of the Java lambda-line PR (#65, 5fd50f5)

The review confirmed the main-method cases, attach paths, source-map stability, parsing and launch safety. It reported one blocking issue, one should-fix and one nit, all fixed:
- **Blocking:** java-debug does not bind "the enclosing method". It keeps the first method in class-file order that has the line, and javac writes `<clinit>` after the lambdas. So for a static initializer or an enum constant the lambda is what stops, and the warning was backwards. The metadata now copies that rule. Each line several methods share records the method that stops (`stops`) and those that never stop there (`never`), whatever their kind: lambdas, static initializers, nested lambdas, several constructors sharing a field initializer, record accessors. The warning names both.
- **Should-fix:** lines shared only by nested lambdas got no warning, though the inner one never stops. This is covered by the same rule.
- **Nit:** a method reference compiled to a lambda (`int[]::new`) has no line of its own. The warning now says so and points to `session step --into`.

Test: `test_a_shared_line_warns_which_method_stops_and_which_never_does` (real JVM: `main`, the static initializer's lambda, nested lambdas, and a lambda's own line).

Verification of 009148e had no blocking or should-fix findings. Every warning named the method that actually stopped, for the static field, `static {}`, enum constants, nested lambdas and `int[]::new` (javac). ECJ, which lists `<clinit>` first, was also right, and so were ordinary code (field initializers across constructors, record and enum headers, bridges, inner and anonymous classes). The cookbook example now names `needsRestock` as the method that stops.

## Review of the integer-mutation PR (#64, 2d3c830)

The review confirmed every type name and range against lldb-dap (LLDB 18, 20 and 21; macOS and Linux). Rust's integer names are identical across those versions. Minimum and maximum values reach the program exactly, and the excluded types (`char`, `int8_t`, `__int128`, `bool`, `const`, enums) are refused. It reported one blocking issue, three should-fix and three nits, all fixed:
- **Blocking:** structured bindings of bitfields (clang 19 and later) are computed values. Upstream LLDB answered `set hi=17` with `set hi: 2 -> 2` and success, and Apple LLDB's refusal closed the session. Every native write is now read back:
  - **Exactly the requested value:** done.
  - **Unchanged:** `MUTATION_REJECTED` ("did not change … its value is computed"), and the session is kept.
  - **Anything else:** the session is closed.
  - **A `setVariable` the debugger refuses:** `MUTATION_REJECTED`, and the session is kept, since nothing changed.
  - **No memory location** on adapters that report memory references: never `[settable]` and refused before writing. In practice this catches nothing: lldb-dap 20 and 21 give even bitfield bindings a `memoryReference`, set to their value. So `[settable]` is documented as type-based, and the read-back is what guarantees no false success on every adapter.

  Test: `test_writes_that_cannot_reach_the_program_are_never_reported_as_done` (real lldb-dap, clang 21).
- **Should-fix:** optimized builds marked `[settable]`. Now nothing is marked when every change would be refused.
- **Should-fix:** a typedef can redefine `int64_t` as `float`, and the lossy write was reported as success. The read-back now closes the session (`stored 16777216 in 'lossy', not 16777217`).
- **Should-fix:** the agent guide, README and RUST_GATE stated the old rules. All three are updated.
- **Nit:** the refusal message now names the accepted types (`time_t`, `off_t` and similar typedefs are not accepted). The `[settable]` text says it applies to C, C++ and Rust, and the hint lists Rust's exact types.
- **Nit (pre-existing):** `+5` and an unsigned `-0` made LLDB fail and close the session. Literals are now sent in canonical decimal (`str(int(value))`).

Verification of 7b7f7d0 found no blocking issues. The bitfield bindings and the typedef-float case behaved as intended on lldb-dap 18, 20 and 21, on Linux and macOS: refused or unchanged writes keep the session, and lossy ones close it. Setting a value to what it already is, Rust values read back through `mutation_value`, and failed writes in debugpy and java-debug all checked out. Two remaining items are fixed:
- **Should-fix (low):** the comment and this review doc credited the memory-location check with catching bitfield bindings. lldb-dap 20 gives them a `memoryReference` too. Both now say the read-back is the guarantee everywhere.
- **Nit:** a refusal now carries the debugger's reason from DAP's `body.error.format` (`Cannot change the value of a constant`) instead of "Debugger request failed".

## Review of CUDA phase 0 (#67, c4159aa)

No GPU was available, so the reviewer ran `qualify.py` in the Linux CI image with fake `nvcc`, `compute-sanitizer` and `cuda-gdb` wrappers, and `tests/gdb` five times against GDB 15.1. It confirmed the launch sequence, including 15.1's harmless `notStopped` refusal of `configurationDone`. It reported one high and one medium issue, both fixed:
- **High:** a debugger that timed out, died, or refused `launch` produced a passing report. DAP checks were added only after each step succeeded, and `passed` covered only the checks that had been recorded. With a working build and sanitizer, a cuda-gdb whose kernel stop never came printed only PASS lines and exited 0. Now:
  - every DAP check starts as False (`DAP_CHECKS`), and so does `debugger_started`;
  - a refused `launch` ends the launch at once, rather than waiting for a stop that cannot come;
  - continuing to exit tolerates warps that report stops after the breakpoint is cleared, up to 20 times, so a slow drain is not taken for a failure.

  Test: `test_a_broken_debugger_fails_the_report_even_when_everything_else_passes` (fake tools: every other check passes, every DAP check is present and False, exit 1).
- **Medium:** an explicit `--cuda-gdb`, `--nvcc` or `--compute-sanitizer` path was never checked. A bad `--cuda-gdb` raised before the report was written, and the others gave exit 1 instead of 2. Every path now goes through `shutil.which`, which also checks that it is executable, and the report lists the resolved tools. The debugger also starts inside the recorded steps.

  Tests: `test_explicit_tool_paths_must_be_executable`, `test_a_debugger_that_cannot_start_is_recorded_not_raised`.
