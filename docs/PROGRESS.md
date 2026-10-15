# Autonomous execution checkpoint

Updated: 2026-10-14. Repository: https://github.com/sushantmane/peeky (private).

## Direction and authorization

The user authorized autonomous implementation and parallel agents. The user also authorized delegating to their Claude and Copilot subscriptions. Both CLIs are installed. On 2026-10-14, Claude Sonnet successfully completed a bounded no-tool design review after user login. The configured Fable default requested extra credits; use the explicit Sonnet override without changing account configuration or purchasing credits. Copilot completed a bounded Java integration review in 37.91 seconds using existing GitHub authentication; do not pass --no-auto-login, which prevented credential use. Use bounded independent reviews or isolated implementation tasks under the same correctness/CI gates. Do not treat CLI installation or auth-status output alone as a successful model invocation. Correctness comes first; optimize measured performance next. Minimum MVP languages: Python, Java, C, C++, Rust. Connect code, logs, and metrics incrementally without delaying debugger support. Keep CI independent and parallel, targeting less than five minutes of execution. Subscription usage for authorized agent work is allowed. Do not change account plans, purchase extra credits, consume reset credits, publish packages, or deploy production services.

An hourly thread heartbeat (`continue-peeky-mvp`) checks for available usage and resumes unfinished work. It cannot bypass usage limits and depends on app availability. Check active agents, Git status, and open PRs before editing; never duplicate an active implementation.

## Delivered

- Private repository, reviewed Python agent loop and private artifact delivery.
- PR #15 merged (`27a5c14`): bounded DAP writes, stale-stop regressions, latency baseline and evidence correlation ADR. Five CI lanes completed in 21 seconds.
- PR #16 merged (`59b6508`): adapter/build foundations and experimental headless Java. 84 tests; six independent hosted lanes completed in 26 seconds.
- PR #17 merged (`8584bd1`): Java HTTP worker, source/class provenance and mutation coverage. All 121 tests passed locally; six CI lanes completed in 44 seconds.
- PR #18 merged (`a40c323`): Java full investigation and fresh candidate verification, 143 tests. All six parallel hosted checks passed; critical job elapsed52 seconds (run38533697141). Independent review in reviews/JAVA_AGENT_REVIEW.md.
- PR #19 merged (`b26c216`): guarded C LLDB worker and scope ownership, 221 collected tests. All seven hosted lanes passed; C34 seconds, longest lane71 seconds (run38538891500). Mixed-runtime inference direction captured in MIXED_RUNTIME.md.
- Private branch protection is unavailable on the current GitHub plan; manually verify all required checks before merging.

## Active slice

Branch `feat/rust-worker` (pushed to origin; **no PR yet**: the Copilot app's PR tool reported the project's GitHub account must be re-linked, and it was not bypassed). CI was run with `workflow_dispatch` on the branch head. Commits on top of merged #22 (`4bb493d`):

- `5ef785f` application-unit Rust symbols and active `Option<i32>` views (RUST_PLAN steps 1–2).
- `09820c6` LLDB `$variant$<discr_value>` binding (from LLDB 18 `ParseRustVariantPart`).
- `48e7429` experimental `RustTarget`/`RustSession` and `--rust-config` HTTP worker (step 3).
- `2e587fa` adversarial-review hardening (see review below).
- `bd2ade2` ADR 0004: stateless probes alongside sessions (owner direction).

Delivered behavior: symbols come only from compile units whose recorded `comp_dir` equals the fresh workspace and whose name binds the declared crate root. Each table is dumped by exact offset into the unchanged 2 MiB parser (Linux 4,808,912-byte full dump avoided; C/C++ limits untouched). Enum layouts are read from the same unit with base types resolved through referenced DIEs. Only `Option<i32>` is shown as `Some(n)`/`None`, reading only the active payload; other data-carrying enums are opaque, and raw `$variants$` storage is never expandable. The worker builds offline/locked with real sysroot cargo/rustc, a checked owned parent, controlled env and hashed `target/debug/deps`. Mutation is limited to root `int` (i32) locals, and symbol-file scripts are disabled. **Not delivered:** Cargo compiled-test execution, independent candidate repair verification (step 4), an end-to-end Rust investigation (step 5), and non-i32 Option payloads.

Checks on `bd2ade2` (run 38666345665): all nine lanes passed; Rust 43 s, C 51 s, Java 53 s (longest), C++ 39 s, unit 21 s, Python 17/19 s, quality 9 s, wheel 21 s. Linux Rust tests 8.73 s total: raw probe 3.44 s (symbols 0.242 s), HTTP worker flow 1.88 s, drift 1.31 s, compile failure 0.54 s. Local macOS (Apple LLVM 21): 399 unit tests in 5.17 s; `tests/rust` 4 passed in 4.40 s; symbols 0.063 s versus 0.433 s for the former full-dump parse. Earlier heads `5ef785f` (run 38664786468) and `48e7429` (run 38665951115) also passed all nine lanes.

Review: an independent adversarial review of `5ef785f` found 2 blocking and 5 should-fix issues. Dispositions: inactive payload swap → each `value` child must carry its own variant type; label-trusted base types → resolved via referenced DIEs (name/encoding/size); truncated/unterminated DIE trees → explicit NULL closure and EOF check; positional `$variant$N` → bound to discriminant values; offset-width indentation → depth-based; `comp_dir` resolve aliases → exact string; per-call timeouts → one shared 30 s deadline. All have regressions. A follow-up review of `48e7429` and the fixes was started but stalled without reporting. Rerun it and record its findings before merging.

Next: open the PR once the account is re-linked (or manually), record the follow-up review, merge only after verifying every lane on the final head. Then RUST_PLAN step 4: `cargo test --no-run` with bounded JSON artifact parsing, exact prepared test execution with pre/post identity checks, unchanged Python checker, and the invalid/overfit/general candidate sequence (step 5).

## Next slices

1. Close remaining lifecycle acceptance gaps in #6; extract adapter and build/verification boundaries (#9).
2. Deliver Java debugger plus clean candidate compilation/verification (#10).
3. Native adapter and C (#11), then C++ (#12), Rust (#13), each with real debugger and deterministic end-to-end agent tests.
4. Expand bug corpus (#7), prove separate-host SSH (#8), and implement the bounded evidence correlation design.

Read AGENTS.md, DESIGN.md, LANGUAGES.md, and ROADMAP.md for acceptance criteria. Runtime intervention is evidence only; never claim a fix without fresh verification and matching source/build identity. Preserve a checkpoint when usage runs low; report shipped work and actual limitations.

Mixed-runtime inference engines (Python/C++/CUDA) are now an explicit design direction in MIXED_RUNTIME.md, not shipped support. Preserve backend session ownership, correlated evidence and clock domains; qualify extension-module coexistence and GPU hardware separately after host-language foundations.

C++ microbaseline:10 local Apple LLVM21 samples, inspect median62.63ms/p9574.91ms, class expansion123.11/124.07ms. Full inspect+class+two array expansion sequence437.95/450.80ms. HTTP/DAP/provenance included; build/model excluded. See PERFORMANCE.md and performance/cpp-2026-10-14.json. These are measurements, not SLOs.

## 12:20 UTC continuation checkpoint

Repository clean on entry; only unrelated Dependabot PRs open. Ordinary usage allowed but weekly window99% used; no reset or purchased credits consumed. Completed a bounded parser compatibility check before starting any larger implementation: the retained local Rust scratch DWARF4 dump (1,754,438 bytes) parses under the unchanged2MiB limit in0.433s and yields declared src/main.rs lines. This is parsing evidence only, not a fresh source/build provenance validation or a worker qualification. Raw result remains ignored at work/rust-line-parser-check.json. Linux4,808,912-byte dump still requires a reviewed Rust-specific bound or declared-source extraction. No implementation edits or background provider jobs started. Resume from RustTarget/Option/test-verification plan when sufficient ordinary usage is available.

## Owner direction: stateless probes (2026-10-14)

The owner prefers stateless operation where possible and wants heappy-level ease of use. Both modes must be supported: the existing stateful sessions and one-request stateless probes over the same guarded engine, with a `describe` manifest, canned probes and SQL over captured evidence. Recorded as [ADR 0004](adr/0004-stateless-probes.md). This is direction only, not started. Until it lands, new worker features should be expressible as declared probe steps.
