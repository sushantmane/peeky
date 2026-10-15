# Autonomous execution checkpoint

Updated: 2026-10-15. Repository: https://github.com/sushantmane/peeky (private).

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

Rust worker and full repair loop are in draft PR #23 (`feat/rust-worker`, baseline `9985257`). Copilot owns the original checkout. Codex review fixes are isolated on `fix/rust-review`, based on that commit, to avoid overlapping edits. Do not reset or switch the original checkout.

Delivered on the pending branch: application-unit DWARF extraction, active `Option<i32>` views, guarded HTTP worker, fixed offline/locked Cargo compilation, direct prepared integration-test execution, and independent checker conjunction. The real repair sequence rejects invalid syntax and a compiled-test-passing overfit before accepting the general fix. This remains unmerged pending exact-head CI and review.

The isolated follow-up fixes bind Cargo policy to staged inputs, validate pinned versions before compilation, enforce declared dsymutil resolution, recheck helper identities on failed builds, and distinguish qualified source diagnostics from infrastructure failure. Ignored/measured tests cannot pass. A shared 30-second Rust launch budget covers preparation, symbols and DAP handshake, with separately bounded cleanup; this is not arbitrary client-disconnect cancellation.

Independent adversarial review and dispositions: `reviews/RUST_REPAIR_REVIEW.md`; earlier findings: `reviews/RUST_WORKER_FOLLOWUP.md`. Local isolated validation explicitly uses `PYTHONPATH=$PWD/src` because the shared venv editable install points at the original checkout: 441 unit tests passed in 9.03 seconds; all six real Rust tests passed in 20.72 seconds (general repair sequence 8.37 seconds); Ruff check and format passed. These are local timings, not hosted CI evidence.

Next: validate all nine parallel CI lanes on the fix commit, integrate the isolated follow-up into PR #23 without overwriting active Copilot work, then verify all checks on PR #23's final head before merging. Language support alone does not complete the MVP: lifecycle, corpus, separate-host and evidence-correlation acceptance remain.

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
