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

PR #23 merged as `2995958` (Rust worker, active Option views, compiled-test plus checker verification, five review rounds, no blocking issues on the final head `825c075`, all nine lanes green).

Branch `feat/stateless-probe` (pushed; the Copilot PR tool is blocked until the project's GitHub account is re-linked, so open the PR manually). It implements the owner's direction (ADR 0004 and ADR 0005): **Peeky is a standalone tool that models, harnesses and humans call; it never calls a model, and there is no mode switch.** One `peeky` CLI provides `describe` (manifest), `probe` (one stateless question), `session start|wait|inspect|get|set|continue|report|close|list` (step by step across shell commands via a local worker) and `verify` (fresh build plus independent checker). Targets are auto-detected with toolchain discovery. There is a stateless HTTP `probe` plus `Client.probe`, docs/AGENT_GUIDE.md and skills/peeky-debug. 506 unit and integration tests pass locally, along with the real Rust CLI and probe tests.

Java on Venice end-to-end tests (owner request) is **delivered experimentally** on branch `feat/java-attach`, stacked on `feat/stateless-probe`; both are pushed with CI green, and PRs must be opened manually until the account is re-linked. `JavaAttachTarget` attaches over loopback to a JVM started with `gradlew … --debug-jvm`, maps only the declared sources' compiled classes, rechecks hashed sources, classes and adapter on every operation including `wait` and `report`, labels provenance `attached`, refuses edit verification and detaches without killing the JVM. Verified on Venice `ba78edda8`: `peeky probe` at `TestEmptyPush.java:85` captured `storeName`, `sendControlMessageDirectly` and `controllerClient` for both data-provider runs in 28.5 s; Gradle reported both tests PASSED. Root cause of the first failed attempts: thousands of DAP `thread` events evicted lifecycle events. Only lifecycle events are now kept in bounded history.

Venice on Apple Silicon without Rosetta: grpc-java ships only x86_64 macOS code generators. `scripts/venice/native-grpc.sh` builds v1.59.1 natively from a pinned commit with SHA-256 checks and exact patch counts, and writes a Gradle init script. Venice `assemble` plus integration-test classes then succeed (2m23s) with no change to the Venice repo. Recipe in docs/JAVA_ATTACH.md.

Reviews: the CLI and attach review found 3 blocking and 7 should-fix issues, all fixed in `3bd1e38`/`d41bdf7` with regressions (path confinement in verify, `no-stop` probes, provenance on wait/report, hard-capped HTTP probe subprocess, token-authenticated worker shutdown, private validated session records, selector truncation, bounded attach discovery, detection precedence, pinned Venice helper, narrowed docs). A verification pass found five more (HTTP probe child isolation, stop validation, cleanup inside the cap, complete JSON results, full attach entry bounds); these are fixed in `27937b7`, and the final review reported **no blocking issues**. Evidence on `27937b7`: all nine CI lanes passed (run 38691568364, longest 71 s); 562 local tests passed (unit, integration, Java including attach, Rust); and the Venice probe again captured both hits, with both tests PASSED.

Next: open one PR from `feat/java-attach` (it contains `feat/stateless-probe`) and merge after reconfirming lanes on its head. Then: a live-model session where an agent drives `peeky` against a Venice bug using docs/AGENT_GUIDE.md; production-class breakpoints in Venice (add `services/*/build/classes/java/main` roots); ADR 0004 steps 4–5 (query grammar, evidence store); lifecycle #6, corpus #7, SSH #8.

Stacked on `feat/java-attach` (2026-10-15):
- **`feat/error-codes` (#6):** protocol version 1, machine-readable error codes with hints, retryable and session_closed flags ([ERRORS.md](ERRORS.md)), and lifecycle regressions. CI green, run 38694056033.
- **`feat/corpus` (#7):** the ten-bug corpus and distinct outcomes. A live agent evaluation ([evaluations/2026-10-15-agent-cli.md](evaluations/2026-10-15-agent-cli.md)) had three independent agents each reach verified general fixes. Its findings are fixed:
  - Python `--set` is now limited to literals; it previously evaluated expressions inside the debuggee.
  - `verify` cleans up its scratch copy, shows the failure reason and diff, and explains `--old` mismatches.
  - Probes warn about relocated breakpoints and short runs.
  - All failures now use one error object.
  - With every real toolchain lane enabled, 644 tests pass locally.
- **Review:** the independent review of error codes plus corpus found no blocking issues. Its two should-fix issues and one nit are fixed ([reviews/ERRORS_CORPUS_REVIEW.md](reviews/ERRORS_CORPUS_REVIEW.md)).
- **Agent speed ([ADR 0006](adr/0006-agent-speed.md), owner priority):**
  - Probes report `pauses_ms`, and on attached JVMs they end at the last hit.
  - Sessions take `--max-pause` (10 s by default for attached JVMs). The worker auto-resumes overdue stops, even unobserved ones.
  - `probe` and `verify` take `--background`, collected with `peeky job wait` (exit 3 while running).
  - `describe` reports the timing contract.
  - Verified on Venice's timed test: the background probe took 19.2 s, and the slow-agent and absent-agent sessions both auto-resumed. All runs PASSED.
  - Open issue: one Venice session never received a `stopped` event (see JAVA_ATTACH.md). Diagnostics were added.
- **Agent-speed review:** no blocking issues. Three should-fix issues are fixed: background errors are replayed on stderr, a relative state dir now works, and crash stops are never auto-resumed (checked with real LLDB). Four nits are fixed too. See the same review file.
- **Next:**
  - one PR from `feat/corpus` covering the whole stack;
  - SSH separate host (#8; needs a second host);
  - a user-selected non-synthetic program.

Known local-only limitation (pre-existing on main): test_native_adapter_death_reaps_stopped_target fails with Apple LLDB on macOS and passes on hosted Linux.

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
