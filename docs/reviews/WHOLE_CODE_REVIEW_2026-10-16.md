# Whole-code review, 2026-10-16

Four independent reviewers read the whole codebase at `727ae42`, not a diff. Each covered one area: the engine (worker, DAP, client, errors), the CLI (cli, probe, evidence), native and Rust toolchains, and Java plus the agent harness. They checked correctness and efficiency, ran real toolchains, and reproduced every finding. In parallel, `scripts/profile_probe.py` split probe time into Peeky's own CPU, child-process CPU and waiting (see [PERFORMANCE.md](../PERFORMANCE.md#where-a-probes-time-goes-2026-10-16)).

The findings are fixed in three PRs. The `PR` column says which one; "this PR" means it is fixed here, with a regression test unless the row says otherwise.

## Efficiency

| Finding | Disposition | PR |
| --- | --- | --- |
| Every operation rehashed the toolchain binaries: clang (125 MB) 32× per C++ probe, cargo 30× per Rust probe; 58 ms per C++ operation | `peeky.digest.sha256_file` reuses a digest while (device, inode, size, mtime, ctime) are unchanged and settled. All checks still run. C++ probe 2.28 s → 0.73 s, C++ operation 58 → 0.8 ms, Rust probe 1.35 → 1.04 s | A |
| `build.prepare` listed each compiler once per command, so hashing grew quadratically with source count (16 C files: 10 s) | Tools are deduplicated by identity, and repeated hashes hit the cache | A |
| The Java adapter bundle (273 files) was walked and rehashed by every check: about 120 ms of JVM pause per hit | The verified bundle is reused while a stat fingerprint of the whole tree is unchanged; any added, removed or written entry triggers full verification. Pause per hit 120 → 9.9 ms | A |
| `run_probe` runs a full `inspect` per selector | With the cache, an inspect costs about 1–3 ms (C) and 10 ms (Rust), so the per-selector cost is now small. Reusing one snapshot per phase is in PR B | B |
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
| A debugpy debuggee survives a transport failure (its own process group) | blocking | | B |
| Two `session start` runs with the same name both succeed | blocking | | B |
| lldb-dap per-thread `stopped` events are reported as new stops after `continue` | should-fix | | B |
| Python breakpoint `condition`/`hitCondition`/`logMessage` evaluate code in the debuggee | should-fix | | B |
| The client sends the bearer token through `http_proxy` | should-fix | | B |
| `report` history has no byte bound | should-fix | | B |
| "DAP frame exceeds limit" classified as `BUDGET_EXHAUSTED` | should-fix | | B |
| Workers leak when `session start` is interrupted; workers never exit after idle expiry | should-fix | | B |
| A relative Java `bundle` path resolves against the CWD or state dir | should-fix | | B |
| Remote probe failures misreported; JSON mode can print two documents | should-fix | | B |
| `session get` aborts all selectors when one fails remotely | should-fix | | B |
| `--timeout` validated after the action ran | should-fix | | B |
| Python list indexes ≥ 100 reported as not found | should-fix | | B |
| `describe` on a syntax error reports `INTERNAL_ERROR` | should-fix | | B |
| Non-ASCII auth header and pre-dispatch errors are misreported; `session close`/`list` JSON gaps; log path shows an fd | nit | | B |
| Rust i32 mutation can write a debug-only copy (`.dbg.spill`) that the program never reads | blocking | | C |
| Files included from outside the declared inputs (`#include "/abs"`, `#[path]`, `include!`) are not hashed | should-fix | | C |

## Review of PR A (#38, 4a1195f)

No blocking issues. Five should-fix issues and two nits, all fixed, each with a regression test:
- **Should-fix:** the caller-frame downgrade also covered module mismatches and line mismatches inside the program's own sources. Now only `validate_stop` failures for frames whose source is outside the prepared or declared directories become request errors, and `validate_debugger` always fails closed (`test_module_mismatch_on_any_program_frame_closes_the_session`, `test_only_caller_frames_outside_the_program_are_request_errors`).
- **Should-fix:** native configs on macOS without `sysroot` lost the SDK once the stub was replaced by the real clang. They now use `xcrun --show-sdk-path`, which is recorded in the build commands (`test_macos_builds_without_a_sysroot_use_the_default_sdk`).
- **Should-fix:** `main` detection counted `main` functions that are commented out or behind `#ifdef`. When more than one file matches the quick text check, the compiler's preprocessor decides (`test_mains_that_are_not_compiled_do_not_split_a_program`).
- **Should-fix:** java-debug parses integers with `Long.decode`, so `010` meant 8. Integer literals must have no leading zero (`tests/unit/test_java_values.py`).
- **Should-fix:** adapter setup failed when the output's parent did not exist yet, which is the documented `work/` path in a fresh clone (`test_setup_accepts_an_output_whose_parent_does_not_exist_yet`).
- **Nit:** a source reached through an in-root link raised `KeyError` on attach. Sources are now keyed by their resolved path (`test_declared_sources_behind_an_in_root_link_attach`).
- **Nit:** the attach-race error is reworded so it classifies as `PROVENANCE_CHANGED`.
