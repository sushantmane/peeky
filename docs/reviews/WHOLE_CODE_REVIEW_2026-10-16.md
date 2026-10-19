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

Verification of ac6cda3 confirmed both fixes, in the Linux image (dash, GDB 15.1) and on macOS. It also ran the whole script with GDB standing in for cuda-gdb and a C version of the scale program; every run stopped and exited. It found two more issues, both fixed:
- **Medium (pre-existing):** GDB's DAP forwards the program's stdout on its own thread, so the last lines can arrive after `exited`. `program_output_seen` read the output at once, and under CPU load it failed 17 of 40 times while `continued_to_exit` passed. The output is now read until `SUM:` appears or 5 s pass (`settled_output`), and that reading also goes into the report.
- **Medium:** `shutil.which("tools/nvcc")` returns a relative path, but builds run in a scratch directory, so a relative tool looked like a failed build (exit 1) rather than a missing tool. Found tools are now made absolute.

Tests: `test_relative_tool_paths_are_made_absolute`, `test_output_that_arrives_after_the_exit_event_is_still_seen`, `test_output_that_never_arrives_ends_at_the_deadline`.

## Review of candidate probes (#68, c84f613)

The review confirmed several areas with real lldb-dap:
- **Provenance binding:** the worker hashes the staged bytes, and the CLI recomputes the expected hash. CRLF stays consistent through universal newlines on every side; symlinks resolve the same way in the worker and the adapters.
- **Remote and version skew:** older workers refuse the `edit` field, and the edit files are byte-exact.
- **Migration:** concurrent writers are safe, because the version is rechecked under `BEGIN IMMEDIATE`.
- **`BUILD_FAILED`:** it changes no existing flow. `verify` still handles its own build stage.
- **Flags and sessions:** the flag interactions and exit codes are right, and sessions have no edit flags, so they drop nothing silently.

It reported one high and one medium issue, both fixed:
- **High:** when `--old` ends with a newline and `--new` does not, the candidate joins the next original line onto the edit's last line. `candidate_line` still mapped that line to `line + delta`, so the breakpoint stopped before the edited code ran. It reported `start = 0` for a candidate that computes 3, with no warning. That line is now refused before anything is built, with the fix in the message: end `--new` with a newline, or probe another line.

  Tests: `test_a_line_joined_onto_the_edit_is_refused`, `test_the_cli_refuses_a_line_the_candidate_joins_onto_the_edit` (the reviewer's command).
- **Medium:** candidate values could not be told apart from real ones in the evidence. The `evidence`/`last_run` views and `queries values` mixed them with real runs, at the candidate's line numbers. Now:
  - the views have a `candidate` column; the 1→2 migration recreates them;
  - `queries runs` marks `(candidate)`;
  - `queries values` leaves candidate runs out;
  - `queries diff` adds an `(edit)` row when the runs built different edits. Pairing is by hit and selector, so moved lines still compare.

  Because canned queries now use the new column, a reader upgrades a writable schema 1 store before opening it read-only, as it already did for hot-journal recovery. A read-only schema 1 store is refused, unchanged, with a clear message.

  Tests: `test_candidate_runs_are_marked_and_kept_apart`, `test_schema_1_stores_are_upgraded_in_place_keeping_their_runs`, `test_a_read_only_schema_1_store_is_refused_unchanged`.

The reviewer also noted that any save by this version upgrades the shared default store, after which older Peeky installs refuse it. That is how schema versions already worked (they ask to move the store aside); it is noted here rather than changed.

Verification of 0952752 confirmed both fixes. It brute-forced `candidate_line` over about 3.5M mapped lines: every line before or after an edit landed at exactly its shifted offset. That covered `--old` starting with a newline, edits starting mid-line, empty `--new`, edits at the start and end of a file, and files without a trailing newline. It also confirmed that migrations run inside `BEGIN IMMEDIATE` and roll back whole, that concurrent upgrades happen exactly once, and that hot-journal recovery comes before the version check. One medium issue, fixed:
- **Medium:** stores already upgraded by c84f613 had `user_version` 2 with the old views. 0952752 changed the views but kept version 2, so those stores were never rebuilt and `queries values` failed with `no such column: candidate`. This included this machine's own store. The schema is now version 3:
  - 1→2 adds `runs.edit`;
  - 2→3 rebuilds the views with `DROP VIEW IF EXISTS`, which also lets a store with a hand-dropped view upgrade instead of being refused as unusable.

  This machine's store was repaired by its next read.

  Test: `test_older_stores_are_upgraded_in_place_keeping_their_runs` (schema 1, early schema 2, and schema 1 without views).

## Review of owned Rust locals (#71, cce72eb)

The review reproduced, with real builds, that several cases are correctly refused or correctly read:
- **Refused:** `move` closure captures, `match` and `if let` moves, moves inside nested blocks, moves later in a loop body, the moved binding of a shadowed `String`.
- **Read:** `let … else`, `break` values, and a local moved on one branch and reassigned on it.
- **Mapped correctly:** a closure on the same line as its enclosing function.

It found two false proofs, both blocking and both reproduced with `probe`. Both are fixed:
- **rustc merges MIR blocks during codegen.**
  - **What happens:** a block is appended to its predecessor's IR block, with no label of its own, when the predecessor has exactly that one successor and the block has exactly that one predecessor. That is the case after a `goto`, or after a call that cannot unwind (any `extern "C"` function).
  - **Why it was a false proof:** the proof checked only the labeled head block, so a move or free in the merged block went unseen. A `Vec` moved into an `extern "C" fn` that frees it read as `[7, 7, 7]` from the reused allocation. One moved by `let w = v` after `getpid()` read as `[1, 2, 3]`, while the live `w` was shown raw.
  - **The fix:** each labeled block is now expanded through its merge chain, and every block in the chain must be free of moves, drops and storage ends. A function with any reachable MIR block that is neither labeled nor in such a chain proves nothing.

  Tests: `test_blocks_merged_into_a_labeled_block_are_checked_too`, `test_an_unlabeled_block_codegen_could_not_have_merged_proves_nothing`, and the real-build `test_review_false_proofs_stay_refused[freed-after-a-call-that-cannot-unwind, moved-after-it]`.
- **IR functions were matched to MIR bodies by elimination.**
  - **What happened:** an `#[inline(always)]` call left its line without code, which excluded `A::new`'s own body. `B::new`, whose only calls come from `vec!`, then remained as the single candidate. The proof ran on B's dataflow, and A's moved and freed `v` read as `[7, 7, 7]`.
  - **The fix:** bodies are now identified positively. Every line the IR function has code on must lie within the body's own span range (its signature to its closing brace), and the function's `DISubprogram` line must not come after it. The call-line test is gone.

  Tests: `test_a_body_is_identified_by_its_own_lines_not_by_elimination`, `test_review_false_proofs_stay_refused[same-named-method]`.

All three real-build regressions fail on cce72eb and pass with the fix.

Verification of 2bf24a3 confirmed both fixes: the three regressions fail on cce72eb and pass now. It checked rustc's block merging for every terminator and unwind kind, `start`/`bb0`, nested and generic functions, inlined callees, and storage reuse, and found no further false proof from these. It found one more blocking issue and one false negative, both fixed:
- **Blocking:** the proof was given only the stop line, never its file. A breakpoint inside a `macro_rules!` defined in another crate file stops in the calling function's frame at the other file's line. The proof then checked that line number in the function's own file: a `drop($v)` in `macros.rs` read `[7, 7, 7]`. `proven()` now takes the frame's source path and proves nothing unless it is the function's own file, unambiguously among the declared sources.

  Tests: `test_a_stop_in_another_file_is_never_checked_against_this_files_line`, and the real-build `test_a_stop_in_a_macro_from_another_file_is_never_proven_with_this_files_lines` (it fails on 2bf24a3).
- **False negative:** a function that expands a same-file macro defined above it got no proof. rustc keeps local macros' own lines in debug info, and those spans put the body's range above its `fn` line. Identification now requires:
  - a span on the function's `DISubprogram` line;
  - every IR line of the function either between that line and the body's last span, or on a line the body has a span on.

  Tests: `test_spans_of_a_macro_defined_above_do_not_hide_the_body`, and the real-build `test_a_macro_defined_above_the_function_does_not_hide_its_proofs`.

The second verification (02a5ad1) confirmed both fixes. It also confirmed that one same-file macro used twice around `drop(v)` is refused at the drop and read where `v` is live, that adjacent `impl` methods named `new` resolve correctly, and that rustfmt-style signatures work. It found one more blocking issue and one false negative. Both are fixed:
- **Blocking (older than 02a5ad1):** functions or closures that one local macro generates twice share their `fn` line, return place and closing brace, all from the macro definition. Matching still worked partly by elimination:
  - A's own body failed the line check, on a call-site line that had only `println!` code.
  - B's body then remained as the single candidate.
  - Twin `run` methods and twin closures in `a::work`/`b::work` read a moved and freed `v` as `[7, 7, 7]`.

  A body is now chosen only positively: exactly one body of that name, file and parent with a span on the function's `DISubprogram` line, so twins are refused. The IR-line containment check is gone, since it no longer decides anything.

  Tests: `test_twins_a_macro_generates_are_refused_not_told_apart`, and the real-build `test_functions_a_macro_generates_twice_are_never_proven_with_each_other[methods, closures]` (both fail on 02a5ad1).
- **False negative:** a local wrapper macro around `println!` (`say!`) defined above a function put code on the macro's line, where the body has no span, so the whole function lost its proofs. Removing the containment check fixes it.

  Test: `test_a_wrapper_macro_around_println_keeps_the_functions_proofs` (fails on 02a5ad1).

The third verification (f3c54d7) confirmed the twin fix. It checked these cases and found them correct:
- trait default methods, and `impl Trait for X` against an inherent method of the same name;
- derived methods, nested `fn`s, and closures in `const`s, `static`s, impl methods and other closures;
- generic instances;
- async bodies (refused).

It found one more blocking case, now fixed:
- **Blocking:** with a multi-line signature, the only span a body has on its `fn` line is its cleanup `resume`. So of two macro twins, only the one with an unwind path has a span on the shared `fn` line, and it was the unique candidate for the other twin. A moved `Vec` read as `[7, 7, 7]`. A candidate is now also refused when any line the function has code on is a span line of another body in the file but not of the candidate. Here, the other twin's own lines give it away. Lines no body has a span on (an external macro inside a local wrapper) still don't matter. Bodies are told apart by identity, never by equality, so identical twins are not confused.

  Tests: `test_a_twin_whose_fn_line_matches_alone_is_refused_by_the_other_twins_lines`, and the real-build `test_functions_a_macro_generates_twice_are_never_proven_with_each_other[only-one-twin-has-cleanup]` (fails on f3c54d7).
- **Known false negative, not changed:** a closure three levels deep (`{closure#0}` inside `{closure#0}` inside `{closure#0}`) shares its name and parent with the middle closure, which constructs it on its own definition line. Both are candidates, so it is never proven.

**Statement-level positions (ae76546).** The whole-block rule refused live locals often: `let w = v; let n = w.len();` never read `w` on the second line, because its block also holds `w`'s assignment. A local is now checked at every place code of the stop line can be:
- before each code-emitting statement on the line, or from another file (external macro code takes the invoking line), following moves, drops and assignments statement by statement;
- at bb0's start on the function's own line;
- at a block's start when a checked caller's call result is stored there.

Any other block with code of the line keeps the whole-block rule, and `#[collapse_debuginfo]` anywhere falls back to it entirely.

The review agents were unavailable for this round ("model not supported", three attempts), so it was verified by hand with real builds on lldb-dap 21:
- **Never read:**
  - a multi-line call that moves its argument, at the call line and at its closing line;
  - a `match` scrutinee moved into its arm;
  - `let Some(k) = Some(c) else`, for `c`;
  - a `Vec` moved into a closure call on the same line as a live one;
  - three statements on one line (`let s = …; let t = s; …`), at each of the line's three breakpoint locations;
  - mid-line stops after `step --out`, from `consume(a)` and from the closure, both after the move.
- **Read:** `k`, `e` and `t` where they are live.
- **Scope-end `drop`:** rustc puts it in the parent scope, so the dropped local is not visible at all, before the drop or after stepping out of `drop_in_place`.
- **Defense in depth:** a stop at a call's or drop's return address is still on the line, so the state after such a terminator on the stop line must also hold the local.

Tests: `test_a_local_assigned_earlier_in_the_block_is_proven_at_a_later_line`, `test_a_block_holding_only_a_callers_result_store_is_checked_at_its_start`, `test_a_stop_after_a_drop_on_the_line_is_never_proven`, and the real-build `test_a_local_assigned_on_the_previous_line_is_read`.

## Review of Rust container views for every element type (#73, 9a4d486)

No issues. The review checked these on real builds and found them sound:
- **Decoding:** width and sign per type; `Vec<bool>` from bytes `[2, 1, 0]` becomes `<unverified>`; signalling, negative and payload NaNs print `NaN`; the alignment check is inert for one-byte elements.
- **Type names:** LLDB builds container names from the element's template argument, so only the scalar pairs `u64`/`usize` and `i64`/`isize` share a name. Rust `char` (`char32_t`) does not collide with `i8` (`char`).
- **Parameters:** `&[usize]` and `&[u64]` in one function stay exact, and `&Vec<T>` is decided by the IR's declared type.
- **Owned locals:** their exact type comes from MIR. A generic `Vec<T>`, or a by-value `Vec<u64>` parameter, falls back to the identical-layout label `u64 or usize`, which is correct.
- **Floats:** the reviewer's own differential run against rustc 1.96 covered 121,018 cases (every power of two ±2 ULP, ±50 ULP around `1e-4`, `1e16` and the maximum values, every power of ten ±3 ULP, 20,000 random subnormals per type, both signs) with 0 mismatches. 256 f64 elements take 20–32 ms per view.
- **Evidence:** `inf`, `NaN` and `true` are not numeric in the `num` column.

## Review of the third-evaluation fixes (#80, ddc6d9f)

One critical finding: a false proof in the owned-local change. Everything else in scope (float text, evidence queries, failed-probe output, NO_STOP, CLI wording, exit codes) was reviewed without issues. The float parser was checked against a port of APFloat's `toString` and real LLDB output over about 385,000 bit patterns; about 3% return None (raw), and none were wrong.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 1 | `may_invoke_macro` could call a line macro-free while rustc put `vec!` code on it, in three ways. (a) Sources were read with universal newlines, so a lone `\r` in a comment shifted every later line by one against rustc, which counts `\n` only. (b) The continuation scan skipped `/* */` without nesting. (c) Identifiers had to be ASCII. With `foreign=False`, the merged block holding `from_elem` was then passed over. The reviewer built each case with Peeky's flags and showed a read before `vec!` ran, and (with an `extern "C"` call that drops the `Vec`) a read of freed memory. `main` refused all of them. | Critical | **Fixed.** Sources are decoded from bytes and split on `\n` alone, as rustc counts lines. Any `/*` or non-ASCII character on the line, or where a path continues, counts as "may invoke a macro". Tests: `test_a_path_continued_past_a_block_comment_or_non_ascii_text_may_invoke` (nested comment, non-ASCII path segment, both on a continuation line), `test_a_lone_carriage_return_is_not_a_line_break`, and the real-build `test_a_line_where_vec_runs_is_never_read_even_after_a_lone_carriage_return`, which fails on `ddc6d9f`. The reviewer's other suggestion was to keep the whole-block rule for any merged block with code from another file. That was not adopted: it would undo the loop-head fix. The macro-line test now errs toward "may invoke" on anything it cannot read as plain ASCII code. |

### Second round (by hand)

The review agents failed twice with "400 The requested model is not supported", so this round was done by hand with real builds (rustc 1.96, lldb-dap). It used an adversarial program covering `for`, `?`, ranges, `+=` on a user type, closures, `HashMap`, derives, local macros wrapping `vec!`, `concat!`, `line!`, `env!`, `format!`, `assert!`, `debug_assert_eq!`, a `vec!` path split over two lines, a multi-line `vec!`, raw strings holding `*/`, the char `'!'`, `async fn` and `drop`.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 2 | The macro-line rule rests on a premise about rustc, not on Peeky's code: another crate's macro code lands only on the line where the invocation starts. | High (it is what makes the rule sound) | **Qualified per toolchain.** `tests/rust/test_rust_macro_lines.py` builds one program twice, with `-Ccollapse-macro-debuginfo=yes` and `=no`. The instructions are identical, so every instruction placed in `main.rs` collapsed but in another file uncollapsed is collapsed macro code. Every such line must pass `may_invoke_macro`. Here that was 10,460 paired instructions and 13 collapsed lines, including the split path, which collapses onto the line where it starts. The program also holds finding 1's nested-comment and non-ASCII cases, and the test fails on `ddc6d9f` at exactly those lines. |
| 3 | `ir_functions` dropped a label printed `bbN: ; No predecessors!`: `debug_assert!` branches on a constant, and codegen leaves the dead arm without predecessors. That block's code was attributed to the block before it, and the MIR block counted as uncovered, so nothing in the function was ever proven. | Medium (false negative) | **Fixed.** Such labels are kept, without predecessors. Test: `test_a_block_codegen_left_without_predecessors_keeps_its_own_label`. The adversarial function is now proven where its locals are live; at the probe, its dropped `Vec` is still never read. |
| 4 | A Rust build with a few errors and warnings writes more than 16 KB of Cargo JSON. `execute` kept 16 KB per stream, so the diagnostics looked possibly truncated, and a plain type error became `DEBUGGER_ERROR: Rust toolchain or Cargo infrastructure failed`. When the error was classified, `BUILD_FAILED` showed only Cargo's footer ("due to 3 previous errors"). | High (agents get no compiler errors) | **Fixed.** Build commands keep all of their output up to the existing 1 MiB kill limit. `BUILD_FAILED` lists rustc's rendered errors from Cargo's JSON, followed by the footer, without the `Compiling` line and its temporary path. Tests: `test_many_diagnostics_are_still_a_compile_failure`, `test_a_cargo_build_failure_shows_rustcs_errors_not_only_cargos_footer`, and the real-build `test_a_candidate_with_many_compile_errors_is_build_failed_with_rustcs_words`, which fails on `ddc6d9f`. |
| 5 | Any Rust program using `HashMap` failed every probe with `DEBUGGER_ERROR: Native symbol command failed or exceeded its budget`. The application unit's own DWARF is 4.4 MB of monomorphized types, against a 2 MiB cap. | High | **Fixed.** The Rust application unit's dumps and parsers allow 16 MiB, the existing ceiling of `_metadata`. Dumping and parsing 4.4 MB takes tens of milliseconds, well inside the symbol deadline. C and C++ keep 2 MiB: an STL-heavy C++ program (`map`, `unordered_map`, `regex`, streams) is well within it. Test: the real-build `test_a_program_using_std_collections_is_debuggable`, which fails on `ddc6d9f`. |

### Third round (989d240)

The review agent was available again. It checked these and found them sound:
- the `No predecessors!` labels: a dead block is no longer credited to its neighbour, and adding a block that never runs only makes the must-init check stricter;
- `_compiler_errors` on malformed JSON;
- leftover 16,000-byte assumptions on the build path.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 6 | Once builds kept up to 1 MiB per stream, that output went unbounded into `verify` results. `BuildCommandError`'s message was the repr of the whole result, and `candidate()` copied it into `diagnostic` (printed in full as `reason:`), next to the raw streams. From there it reached `--json`, the evidence and every later agent prompt. | Medium | **Fixed.** `BuildCommandError` names the command and its outcome only (`Build command failed: /x/cargo: exit code 101`). A compiled candidate's build failure keeps each stream's last 16,000 characters, as before. Its `diagnostic` is the bounded `BUILD_FAILED` message, with rustc's errors first. Test: `test_a_long_build_failure_stays_bounded_with_the_compilers_errors_first`. |

### Verification of ba50845

Nothing that copies a `BuildCommandError`'s result or message can still carry unbounded output, and nothing depended on the old message. A pre-existing mislabel was reported and fixed: a compile that hit its per-command timeout printed `reason: checker timed out`, because the build result's `timed_out` overrode the diagnostic. The verify text renderer (`_verify_text`, now a module function) uses the checker label only outside the build stage. Test: `test_a_build_timeout_is_not_reported_as_the_checkers`.

## Review of withheld C/C++ locals (#81, f2c0fe1)

These were checked and found sound: the per-table line cache (reset per table; file ids are fixed before any row), the keys of a withheld output, `_root_declared_here` against `_resolve`, other runtimes, shadowed `x @ file:line` names, lambdas and init-captures, Apple blocks, and declarations in other files or with non-automatic storage.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 1 | A live constructor parameter was withheld. Candidates came from functions whose line *span* covered the stop line, and that span dropped lines above the function's declaration. A local class's constructor runs its default member initializers there (`int w = helper(3);` on line 5), so `main`, which defines the class and declares another `v` on line 12, was the only candidate: `v` showed "declared at line 12". | High | **Fixed.** Candidates come only from functions that own code on the stop line, using the exact line set from the line table with no declaration filter. Tests: `test_only_functions_owning_the_line_count_and_jumps_disable_withholding`, and the real C++ build `test_a_constructor_parameter_is_never_withheld_for_an_enclosing_functions_local`, which fails on `f2c0fe1`. |
| 2 | A backward `goto` (the `again:` retry idiom), or a `longjmp` to a `setjmp` above the declaration, re-runs lines above a declaration while the local is still live (C11 6.2.4p6). It was withheld as leftovers. | Medium | **Fixed.** A function with a `DW_TAG_label`, or with `setjmp` in its source lines, never withholds. Tests: `test_a_function_with_a_label_or_setjmp_can_jump_back`, and the real build `test_a_backward_goto_keeps_the_local_live_above_its_declaration` (`x` is 7, then 17), which fails on `f2c0fe1`. |

### Second round (64a9bd3)

Both fixes held. The reviewer ran the pending-local tests on macOS clang and on Linux clang-18 (CI's compiler); the `goto` test also passed with gcc. These were checked and found right: lambdas sharing a line with their parent (the union of their locals only withholds less), same-file inlined callees, switch fallthrough, one-line functions, macro-defined functions, GCC nested functions, VLAs (a location list under clang; GCC's is withheld above the declaration, correctly) and labels kept at `-O1`/`-O2`/`-Og`.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 3 | Clang runs a struct's default member initializer (`int weight = default_weight();`) in the caller that brace-initializes it, on the struct's line. Once the caller owned that line (fix 1), a stop there withheld all of its live locals (`i`, `total`). | High (regression from fix 1) | **Fixed by a stronger rule.** A local declared below is withheld only when all of that function's code on the stop line comes before any code on the declaration's line, by address in the build's line table. Code borrowed from another entity sits inside a later line's code, so it never qualifies. A declaration without code on its line proves nothing. Tests: `test_a_local_is_withheld_only_when_the_lines_code_comes_before_its_declaration`, and the real C++ build `test_a_member_initializer_run_by_its_caller_withholds_nothing`, which fails on `64a9bd3`. |
| 4 | GCC runs an `__attribute__((cleanup))` at its variable's declaration line when the block ends, so `session step` lands there with later locals live. | Medium (GCC only; pre-existing) | **Fixed by the same rule.** The declaration line then also has code after the later declarations' code. The unit case reproduces GCC's layout. CI's compiler is clang, so no real-build lane exercises it. |
| 5 | `setjmp` behind a macro from a header (`#define TRY if (setjmp(env) == 0)`) escaped the per-function text check. After `longjmp`, a volatile local declared below is live. | Medium | **Fixed.** When the executable names `setjmp` (any variant), `getcontext` or `swapcontext`, nothing is withheld. The text check is gone. Tests: `test_nonlocal_jumps_are_found_in_the_executable`, and the real build `test_setjmp_behind_a_macro_withholds_nothing` (40, 41), which fails on `64a9bd3`. |

### Third round (18e6875)

All three earlier fixes held on macOS clang, Linux clang-18 and gcc. These were checked and found right: `for` increments and back-edges, GCC's switch dispatch after the body, range-for, declarations in `if`/`while`, multi-line declarations, statement expressions, macros, lambdas, clang's cleanup attribute and address-taken locals.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 6 | Clang runs a header struct's member initializers in the caller on the header's line (`geo.hpp:2` in `main`). A function only owned rows of its own file, so `main` was not an owner there, and `origin_sum`'s `x` (declared on `geo.hpp:5`) withheld `main`'s live `x = 41`. | High | **Fixed.** Each row is attributed to the innermost program function running it, in any declared file, keyed by (file, line). `main` is then an owner, and its `x`, declared in `main.cpp`, stops any withholding at `geo.hpp:2`. `describe` keeps its own-file attribution. Tests: `test_a_function_running_code_on_another_files_line_is_among_its_owners`, and the real C++ build `test_a_header_structs_initializer_run_in_main_withholds_nothing` (`step` from `main.cpp:5`), which fails on `18e6875`. |
| 7 | `__builtin_setjmp`/`__builtin_longjmp` leave no symbol in the executable. | Medium | **Fixed.** Nothing is withheld when a declared source or header names `setjmp`, `getcontext` or `swapcontext`; the executable is still checked too. Test: `test_sources_naming_a_nonlocal_jump_are_found`. |
| 8 | The "declared on this line" note called a live `for` counter leftovers at a step stop on its header (the increment, `i = 1`). | Medium (note only) | **Fixed.** The note needs the line's code to be contiguous: a `for` header's code wraps its body's, so a stop there may be on a later pass. Tests: the unit case, and the real build `test_a_live_for_counter_at_a_step_stop_on_its_header_has_no_note`, which fails on `18e6875`. |

### Fourth round (4947c18)

All round-3 fixes held on macOS clang, Linux clang-18 and gcc. `describe` output is unchanged against `main` (STL-heavy program and the reproducers). `local_declarations` takes 0.11 s on 10.9 MB of debug info, and a probe takes 1.5 s against 1.6–1.8 s on `main`.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 9 | A header outside the workspace (`/usr/local/include`) hiding `__builtin_setjmp` in a TRY macro: the declared sources never name it, and it leaves no symbol. | Medium | **Fixed.** The build's external inputs (the compiler's dependency files, already hashed) are searched for `__builtin_setjmp`. Only the builtin is searched for there: the SDK's `setjmp.h` names `setjmp`, and calls to that are found in the executable. Each file is now read once, not once per name. Test: `test_an_external_header_hiding_the_builtin_is_found`. |
| 10 | The "declared on this line" note called live locals leftovers at mid-line stops: a one-line loop (`for (int i…) { int sq = i * i; … }`, where lldb stops at the body's start on each pass) and `step --out` into `int a = 3, b = twice(a);`. | Medium (note only) | **Removed.** Only the stop's instruction pointer against the line's first address could tell those cases apart, and lldb-dap versions report load addresses in different forms. A local declared on the stop line is now shown as its storage reads, and the docs say that at a breakpoint there its declaration has not run yet. The `for` header regression now checks that the counter is shown (`test_a_live_for_counter_at_a_step_stop_on_its_header_is_shown`). |

## Review of exact integers in the evidence store (#82, 7eb08cf)

Checked and found sound:
- capture order through the rebuild, including gaps left by pruning;
- a v1 store chaining through 2, 3 and 4;
- views and indexes across the rename (no view references `captures` at rename time, so neither the SQLite version nor `legacy_alter_table` matters);
- concurrent upgrades;
- pruning after the rename;
- `_number` on `-0`, `+5`, `007`, `1e5`, `0x10`, padding, NaN/inf and values past 64 bits;
- no canned query depending on `num`'s storage class.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 1 | Migration copied `num` as is, and NUMERIC turned a REAL that had already lost digits above 2**53 into an INTEGER that looked exact (`9007199254740992` for `value` `9007199254740993`). The same `value` then had two `num`s across old and new runs. | Medium | **Fixed.** The migration recomputes `num` from `value` through `_number`, registered on the migrating connection as `peeky_number`. Test: `test_a_version_3_store_upgrades_num_in_capture_order`. |
| 2 | With exact integers, `SUM(num)` fails on overflow past 64 bits (a sentinel like `LLONG_MAX` on two hits), and `num / k` truncates. | Medium | **Documented.** These are SQL integer semantics, and exact integers are the point of the change. The schema text, AGENT_GUIDE and COOKBOOK say to use `TOTAL(num)` and `num * 1.0 / k`, and `SUM` is no longer recommended. |
| 3 | A writable v3 store in a read-only directory said "is unusable … move it aside" on its first read: SQLite cannot create its journal. | Low | **Fixed.** The error says the store needs a one-time upgrade and the directory is not writable. Test: `test_an_upgrade_blocked_by_a_read_only_directory_says_so`. |

## Review of several edits per candidate (#83, 0266426)

No issues. The reviewer checked by argument and by fuzzing:
- **Uniqueness:** the combined `old` is unique, because it starts with the first edit's unique `old`. Its match is where that edit starts. Over 41k random texts of `a`, `b` and newlines, which give many overlapping matches, combining always equaled applying each edit.
- **Line mapping:** `candidate_line` mapped about 137k lines through one to three edits in random order correctly. Every refusal was a joined line or a line inside an edit that changes the line count.
- **Other cases:**
  - adjacent edits;
  - a `new` that creates another copy of an `old` (the worker re-checks the original);
  - an older remote worker given a list (it answers 400, and a single pair is still sent as an object);
  - Python verify;
  - malformed `Probe.edit` shapes;
  - the edit being dropped from echoed probe JSON.

One test was updated for the reworded mixed `--old`/`--old-file` error.

## Review of Rust scalar type names (#84, b3e1b6a)

No local got a wrong type. These were checked on real builds:
- shadowed `x @ main.rs:N`, each resolving to its own binding;
- closure upvars and generic `T` locals, which keep LLDB's name;
- type aliases, `i128`, `char` and `bool`;
- inlined frames;
- every consumer of the `type` field (`settable`, `validate_variable`, `set`, `INTEGER_RANGES` and container views all use the raw LLDB variable).

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 1 | `u8`/`i8` locals were relabeled while their values stayed C character literals (`level: i8 = '\xfd'`, which is -3). `num` was then NULL for them, and elements read from memory print numbers. | Medium | **Fixed.** In a Rust program, LLDB's `unsigned char`, `char` and `signed char` can only be `u8`/`i8`, so their literals print as Rust integers (`65`, `-3`). The parser accepts exactly LLDB's forms (`'A'`, `'\0'`, `'\xHH'`, `'\e'`, the named escapes, octal); anything else stays raw. Tests: `test_lldb_byte_literals_show_as_rust_integers` (all 256 values, both signs), and real builds checking `t_i8 = -1` and `letter: u8 = 65`. |
| 2 | Parameters never got Rust types: MIR types arguments only in the function header, so a frame showed `scale: unsigned long` next to `total: usize`. | Medium | **Fixed.** `parse_body` reads argument types from the header, split at top-level commas (generics and tuples kept whole, `->` closing nothing). Struct fields, and anything else MIR does not type, take the one primitive their LLDB name can be (`double` is `f64`); only `long`/`unsigned long` stay ambiguous. Tests: `test_arguments_take_their_types_from_the_header`, and the real build `test_scalar_parameters_show_their_rust_types_and_bytes_their_values`. |

## Review of settable locals up front (#85, c95284f)

Checked and found sound:
- shadowed names (all duplicates are named `x @ file:line`, and a non-identifier name is never marked settable);
- duplicate names across scopes (`_resolve` and the `--set` scope search take the same first match);
- re-raised refusals keeping their codes (pointer, struct, out-of-range value, declared below);
- the snapshot being re-read for each name.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 1 | The guide said Java rows carry `settable`, but Java had no `settable()`, so its rows never did and its refusals listed nothing. | Medium | **Fixed by implementing it.** `JavaSession.settable` mirrors `validate_variable`: a root local of type byte, short, int, long, boolean, float or double. The worker now also requires the adapter's `supportsSetVariable`, so nothing is marked settable on an adapter that cannot write. The MUTATION_REJECTED hint points to the markers and the list. Test: the real Java build `test_rows_say_which_java_locals_set_can_change` (`counter`/`args` not settable and `settable here: none`; `i`/`times` settable). |
| 2 | (verification of 2654eb4) Java marked names no selector can reach as `[settable]`, including java-debug's method return-value entry after a step (`->compute()`, or `⎯►compute()` on macOS) and identifiers with `$` or non-ASCII letters, which every set path then refuses. | Medium | **Fixed.** `JavaSession.settable` also requires a selector name, as `_writable_integer` does for native targets. Test: `test_java_settable_locals_are_scalars_a_selector_can_name`. java-attach, `char`, `this`, boxed types, lambda frames and lower frames were checked and are consistent, and the capability requirement changes nothing for C, C++, Rust or Python. |

## Review of Java objects and lists (#87, 63c0a1b)

No preview showed a wrong field value. Also checked:
- **Threads:** only the stopped thread is suspended (java-debug's default). An `ArrayList` preview reads `size`, then the elements, so a concurrent change can show a mixed state. Separate field reads always could.
- **Other targets:** Rust and LLDB requests are unchanged, and a String value cannot look like an array value.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 1 | Whether a value got a preview depended on its siblings: up to 16 preview slots per inspection, handed out in listing order. The same unchanged object read `Cls@N {…}` at one hit and `Cls@N` at the next, which `hits` and `diff` report as changes. | Medium | **Redesigned.** The worker no longer previews. The probe and `session get` preview only the value a selector captured, through that value's own expansions. A preview cut short by the node budget is read again on a fresh inspection, or left out. Test: the real build `test_java_previews_depend_only_on_the_value_and_lists_read_within_bounds` (`slots[30]` is the same at all three hits while the earlier slots fill), which fails on `63c0a1b`. |
| 2 | Previews touched more objects, and JDWP numbers objects in the order first touched, so the same object had different `@N` across runs and `queries diff` ranked them as real divergences. | Medium | **Fixed.** `no_addresses` masks Java object numbers in a value java-debug wrote (one starting with `Type@N` or `T[n]@N`), never inside a String's quoted text, so these rows are `addresses only`. Test: `test_java_object_numbers_differ_between_runs_like_addresses`. |
| 3 | `list[i]` failed for any `ArrayList` whose backing array held more than 512 slots (`new ArrayList<>(1000)`), and that error triggered a pointless re-inspection. | Medium | **Fixed** as a clear limit: `the list's backing array holds more than 512 slots, Peeky's inspection bound`, which does not trigger a retry. Plain Java arrays have the same bound. Covered by the real-build test (`roomy[0]`). |
| 4 | When earlier selectors had used the node budget, `_list_element` reported a page cut short as "not in the list's backing array". | Medium | **Fixed.** A truncated page, or no nodes left, raises the budget-classified error, so the capture retries on a fresh inspection. Covered by the real-build test (`counts[400]` then `many[127]`). |
| 5 | Previews ran on every expansion, including `_resolve`'s intermediate pages: about 30% more pause, often without previewing the captured value itself. | Medium | **Fixed by the redesign (#1).** Only captured values are previewed: one expansion for an object or array, two for an `ArrayList`. |
| 6 | (verification of 739faac) A preview whose own page was cut short by the node budget (earlier selectors had used it up) showed as a complete object: a five-field `P` read `P@N {a=1, b=2}`. | Medium | **Fixed.** The preview's fetch raises the budget error when the page is `truncated` and shorter than requested, so the capture reads it again on a fresh inspection. Test: the real build `test_a_java_preview_after_selectors_that_used_the_budget_is_complete`, which fails on `739faac`. |
| 7 | (verification of 739faac) java-debug writes String text between quotes without escaping it, so a field like `"say "hi" @3"` made `no_addresses` pair the wrong quotes and mask (or keep) the wrong `@N`. | Medium | **Fixed.** A preview renders String fields as Java literals (`"say \"hi\""`), cut inside the quotes, so its quoted spans always pair. Test: `test_java_preview_strings_are_literals_so_ids_mask_correctly`. |
| 8 | (verification of ef2c3b8) java-debug writes a char bare, so a `char` holding `"` looked like a String's opening quote and previewed as the empty String `""`. | Medium | **Fixed.** `_brief` decides by the field's type: a `char` is a Java char literal (`'"'`, `'\''`, `'\n'`), and only a value starting with `"` that is not a char is a String. A String is cut before it is escaped, so an escape is never split. Control characters and lone surrogates are `\uXXXX`. Masking's quoted spans now include char literals. Tests: `test_java_preview_chars_are_literals_never_strings`, and the real build `test_a_java_preview_after_selectors_that_used_the_budget_is_complete` (fields `q='"'` and `s="say \"hi\" @3"`, which checks java-debug's real `char`/`String` types). |
| 9 | (verification of ef2c3b8) Java masking was chosen by the value's shape, so a Python (or C, C++, Rust) value such as a `Node@1` repr had its `@N` masked, and a real divergence ranked as `addresses only`. | Medium | **Fixed.** `no_addresses(text[, language])` masks Java object numbers only with language `java` or `java-attach`. `diff` and `compare` pass each run's `runs.language`; one argument masks only `0x` addresses, as before #87. Test: `test_java_object_numbers_differ_between_runs_like_addresses` (Python `Node@1`/`Node@2` is a real difference). |
| 10 | (verification of ef2c3b8) A `Class` value (`Class (Foo)@12`) and a lambda's hidden class (`Main$$Lambda/0x…@46`) kept their object numbers. | Low | **Fixed.** A number after `)` is masked, and in a Java run any value that is not a String is masked outside its literals, so hidden-class names are too. Same test (`Class (Foo)@12` vs `@13` is `addresses only`). |
| 11 | (verification of 5472e36) A field value is cut after 40 characters, so with a long class name `Name@998` fit in one run and `Name@1003` was cut to `Name@10…` in the next. The same field then differed by more than its object number, and `diff` reported a real difference. | Low | **Fixed.** An object's number is never cut: whether a value is cut depends only on the text before `@N` (`LongName…@1003`), and masking accepts a number after the cut's `…`. Test: `test_a_cut_java_field_keeps_its_object_number_whole`, which fails on `5472e36`. The same review found no issue with char literals, masking only in Java runs, `_QUOTED` pairing, NULL languages in old stores, or the language subquery's cost (200k captures: 8.75 s vs 8.72 s). |

## Review of Java field and element `--set` (#88, 8f951a0)

Checked and found sound: stale, view and non-Java handles never reach `setVariable`; the parent is found again from frame and scope by name and type; root-local sets in C, C++, Rust and Python are unchanged; the HTTP probe and background jobs pass dotted names through; `session set` errors keep their codes through the HTTP client. Shadowed instance fields cannot be confused: java-debug names both `x (Sub)` and `x (Base)`, so a plain `x` is never listed for them.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 1 | java-debug writes an object's field through `referenceType().fieldByName(name)`, which follows Java's hiding rules, while its listing leaves static fields out. When a subclass's static `x` hides an inherited instance `x`, the listing shows the instance field as plain `x` (`settable`), and the write went to the static. A new value equal to the instance field's passed the read-back, so the static changed silently. An attached JVM also kept the static change after a refused read-back. | Medium | **Fixed in Peeky's Java launcher.** Before java-debug sees a `setVariable` on an object, `PeekyJavaAdapter.checkFieldTarget` requires the name to mean exactly one instance field, which `fieldByName` must resolve to. Names with `(` are refused. The bundle reports `peekyFieldSet`, and an older bundle changes only locals, saying to rebuild. Test: the real build `test_a_field_a_static_field_hides_is_never_written[5, 1]`, where the session continues and prints `AFTER 1 100`. Remaining limit: the row's `settable` cannot see the hiding static, which the listing omits, so it can say `[settable]` for that field. The write is then refused with a message that names the reason. |
| 2 | A missing parent in a dotted target (`request.limits.max`) produced an error that lists the visible names, and the names decided the code: `deadline` made it `TIMEOUT` (whose hint says the session was discarded) and `breakpoint` made it `INVALID_BREAKPOINT`. | Medium | **Fixed.** A parent that cannot be resolved is `… was not found in the inspected values: …`, which the NOT_FOUND rule matches before any rule that names could. Test: `test_a_missing_intervention_parent_is_not_found_whatever_the_names`. |
| 3 | (own) A C++ `session set` hint offered "in Java, changed as this.size_". | Low | **Fixed:** the hint says "it can be read as", as before; the NOT_FOUND hint says Java fields can be changed. |
| 4 | (verification of b73e949) Fixes 1 and 2 confirmed against real java-debug, in launched and attached JVMs: the attached JVM exits with `substatic=100` after `sub.x=5` is refused, and `this.f`, `this.grid[1][-1]`, an enum's field and an inherited unhidden field are applied. The launcher's reflection and JDI calls run on java-debug's single request thread, the one its own handler uses. But the other `apply_set` refusals (`[5] is out of range`, `has no fields or elements`, the depth limit, no value here) still let words in the selector decide the code: `this.deadline.x` was `TIMEOUT`, and `this.breakpoints[5]` was `INVALID_BREAKPOINT`. | Medium | **Fixed by giving refusals their code explicitly.** `errors.Refused(code, message)` is classified by its code, with the hint of the rule that describes it, and its text is never pattern-matched. Every `apply_set` refusal, wrapped worker errors (classified by the worker's own message) and the map-entry refusals use it. Test: `test_member_refusals_are_classified_by_rule_never_by_the_selectors_words`, 3 of its 5 cases failing on `b73e949`. |
| 5 | (verification of b73e949) With a bundle older than the guard, `session set this.f=1` was `INVALID_REQUEST`, while a probe hitting the same condition was `MUTATION_REJECTED`. | Low | **Fixed:** the worker's "predates field and element changes" message is MUTATION_REJECTED. Test: `test_a_bundle_without_member_sets_is_a_mutation_refusal`. |
| 6 | (verification of b73e949) A field hidden by a static field still shows `settable: true`, since the listing never shows the static. | Low | **Kept, documented in finding 1:** the marker is advisory, the write is refused cleanly before anything changes, and the message names the reason. |
| 7 | (verification of 1c3ce11) `_refusal` took the first rule with a code's name, so every `INVALID_REQUEST` refusal (`this.grid[5]` out of range) carried the evidence store's hint. | Medium | **Fixed:** `INVALID_REQUEST` takes the generic "Fix the request arguments" hint. Test: `test_the_workers_own_errors_keep_their_codes_in_an_intervention` (checks the hint). |
| 8 | (verification of 1c3ce11) Every failure to read the parent became `NOT_FOUND`, including a node budget (`Item[600]`, "exceeds bounded expansion limit") and a stale handle, which re-inspecting can cure. Through `session set`, `_Remote` had already turned those codes into plain errors. | Medium | **Fixed at the sources.** `apply_set` wraps the worker so its errors are classified by the worker's own message, before any selector or name is added. `_Remote` keeps a row error's code (`Refused`). The probe's own bounded-window errors are `Refused("BUDGET_EXHAUSTED")`, still row errors and still retried. Only the remaining "the path names nothing here" failures are `NOT_FOUND`. Tests: `test_the_workers_own_errors_keep_their_codes_in_an_intervention` (budget, stale, invalid request) and `test_a_remote_row_error_keeps_its_code`, all failing on `1c3ce11`. |

The last verification (f91cd34) found nothing wrong.
- **Real java-debug:** `Item[600]` gives `BUDGET_EXHAUSTED` through a probe and through `session set` (with the session kept); an out-of-range index gives `INVALID_REQUEST` with the generic hint; the earlier cases are unchanged.
- **Unchanged reads:** capture rows, `_BUDGET` retries and `session get` rows are identical to `1c3ce11`'s, checked with arrays and lists of 600, negative indexes and missing names.

## Review of plain Cargo crates (#89, 4040162)

Checked and found sound: no accepted crate makes Cargo build or run anything unhashed.
- **Refused before any build:** auto-discovered examples and benches are never built by `--bin`/`--test`; `build.rs`, `src/lib.rs` and `src/bin` are refused as files, directories or symlinks; tables such as `[lib]`, `[[example]]`, `[features]`, `[profile]`, `[patch]`, `[lints]` and `[target.*]` are refused.
- **Re-checked on the staged copy:** `validate_workspace` validates the staged copy again, so files added after preflight are caught.
- **Without a test:** the artifact allowlist, uplift digest and dependency checks hold.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 1 | The "LLDB's summary, not the contents" note was attached to every Rust container without a view, including reference locals of types Peeky reads as parameters (`&str`, `&[i32]`, `&Vec<i32>` shown as `Vec<int> *`). Indexing `&Vec<i32>` said integer Vecs are never read. | Medium (regression) | **Fixed:** the note and the element-type refusal apply only when the referent type (without ` *`) is never read. Test: the real build `test_a_cargo_new_crate_is_probed_and_chars_print_as_rust_does` (`s`, `sl`, `r` have no note; `r[0]` keeps the parameter/owned-local reason). |
| 2 | A `[[bin]]` at another path beside `src/main.rs`, with `autobins` left out, made Cargo infer `src/main.rs` as a second binary. Validation accepted it, and the build failed with "Unexpected Cargo build artifact". | Medium (fails closed) | **Fixed:** refused before the build, naming the second binary and the two fixes. Test: `test_a_bin_elsewhere_beside_src_main_rs_is_refused_up_front`. |
| 3 | `lldb_char` decides by Python's Unicode tables (14.0 or 15.1), but Rust 1.96 uses Unicode 17.0. U+1171E is `Mn` for Python and `Mc` (not Grapheme_Extend) for Rust, so Peeky escaped a character Rust prints as is. The reviewer compared all 1,112,064 scalar values against Rust's `{:?}` with Python 3.11 and 3.13; this was the only mismatch. | Medium (one code point) | **Fixed:** U+1171E stays raw (`_UNICODE_CHANGED`). Test: the real build prints `'\u{1171e}'` through the program and Peeky's value is not the escaped form. |
| 4 | Test discovery counted hidden files, which Cargo skips: macOS `._real.rs` and Emacs lock symlinks `.#real.rs`. | Medium (fails closed) | **Fixed:** hidden entries are skipped, as Cargo does. Test: `test_hidden_files_in_tests_are_not_test_targets`. |
| 5 | (Linux lanes) LLVM's lldb-dap writes a char as `U+0x00000062 U+0x00000062 U'b'`, while Apple's writes `U+0062 …`. The new `Vec<String>` refusal reason also replaced the freed-memory wording an older test asserted. | Medium | **Fixed:** both forms are parsed (`test_rust_chars_print_as_rusts_debug_does_on_either_lldb`). The test now asserts the accurate reason: String elements are never read. |

The verification (3a4fde1) found nothing wrong.
- **Fixes on real builds:** all four hold.
  - `cargo metadata` confirms that a `[[bin]]` named after the package, at another path, stops Cargo inferring `src/main.rs`, and Peeky accepts it. A `[[bin]]` with another name is refused, and so is `my-app` against a bin named `my_app`.
  - `lldb_char` matches Rust 1.96's `{:?}` for all 1,112,064 scalar values under Python 3.11 and 3.13, in both LLDB forms.
  - Cargo and Peeky agree on hidden test entries.
- **Notes:** no qualified type lost a note it should have.
- **Linux lanes:** `rust` and `rust-llvm20` (lldb-dap 18 and 20) pass the char test with LLVM's `U+0x…` form.

## Review of the fourth evaluation's fixes (#94, 32f20f7)

Checked and found sound:
- **Jobs:** a background probe's text, stderr and exit code match the foreground exactly, including with `--` arguments; older jobs replay what they recorded.
- **`object_ref`:** has no false positive on quoted strings, numbers or non-Java text.
- **`(args)`/`(sets)` rows:** NULL arguments compare as expected.
- **Real Java test:** the `BalanceBug` test passes against real java-debug.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 1 | A background job's added `--json` went at the end of argv. For `verify ... -- target`, that is after the bare `--`, so it read as a second target, and the job failed in argument parsing where `main` succeeded. | Medium (regression) | **Fixed:** `--json` goes right after the subcommand. Test: `test_a_background_verify_with_its_target_after_a_double_dash`. |
| 2 | When `--timeout` ran out while the checker was running, a correct fix was reported as `passed: False` ("checker timed out"), which is how a hanging candidate looks. | Medium | **Fixed:** a checker cut short by the deadline (not by its own 20 s) raises `TIMEOUT`. So does a toolchain probe cut short by it, which was `DEBUGGER_ERROR`. Test: `test_a_timeout_during_the_checker_is_timeout_not_a_failed_fix`. |
| 3 | `--timeout` did not bound the Rust Cargo test run (always 30 s): `--timeout 8` took 21 s. | Medium | **Fixed:** `run_tests` takes the deadline and raises `TIMEOUT` when it is what stopped the tests. Test: the real build `test_verify_timeout_bounds_the_cargo_tests_too` (a 60 s test, `--timeout 15`, done in 15 s). |
| 4 | `session get` on an `ArrayList` whose backing array has over 512 slots (any list of 367 or more elements, or `new ArrayList<>(1000)`) showed only an error and lost its `size`. | Low–Medium (regression) | **Fixed:** such a list lists its fields, as before. Test: `test_a_java_list_whose_backing_array_is_too_big_lists_its_fields`. |

The verification (3272874) found nothing wrong. All five repros behave:
- the `--` background verify passes, with `--json` at either end;
- a checker cut short by `--timeout` is `TIMEOUT`;
- Java `--timeout` from 0.02 to 2 s is `TIMEOUT` at every cut point (toolchain checks, build, preparation, checker), and a wrong fix still fails normally;
- a 20 s Rust test stops at `--timeout 8` after 8.0 s, with no scratch directories left behind;
- lists of 400 and 1000 slots fall back to their fields, and the session keeps working.

Callers without a deadline keep the old 20 s and 30 s limits.

## Review of Rust char elements (#95, bb49268)

Checked and found sound:
- **Rendering:** `rust_char_debug` matches real Rust 1.96 `{:?}` for all 1,112,064 scalar values. The surrogate gap splits the table correctly, and `"` prints as `'"'`.
- **Element types:** none of the tables derived from `ELEMENT_TYPES` changes harmfully. `Vec<i8>` (LLDB's `Vec<char, …>`) and `Vec<char>` (DWARF's) read correctly side by side, including `&mut` forms.
- **Containers:** `.len`, `.capacity`, `a[-1]` and the `(first k of N)` cut behave as for other elements, and spare capacity is never read.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 1 | The table generator ran the pinned `cargo` without `RUSTC`, so it built with whatever `rustc` PATH picked (the rustup default), while stamping the table with cargo's version. A default other than 1.96 would silently produce a wrong table that the version test still accepts. | Medium | **Fixed:** `generate(cargo, rustc)` builds with `RUSTC` set and refuses if cargo's and rustc's versions differ. The Rust lanes now regenerate the table and compare it with the shipped one (`test_the_char_table_is_what_the_pinned_rust_prints`). |

The verification (56d6bcd) found nothing wrong.
- **Pinned toolchain:** with a failing `rustc` first on PATH, the generator and the test still build with the pinned 1.96.0 and produce the same 909 ranges; the fake is never called.
- **Hygiene and speed:** the test builds only in its own temporary directory, leaves nothing behind (also on timeout), and takes 0.1–0.3 s.
