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

## Current state (2026-10-15, latest)

**Merged to `main`** (each after an independent adversarial review, with every finding fixed, all nine CI lanes green, and the full real-toolchain suite passing locally; dispositions in `docs/reviews/ERRORS_CORPUS_REVIEW.md` and `RUST_REPAIR_REVIEW.md`):
- **#25 (`10ac271`):** the standalone `peeky` CLI, Java attach (Venice), protocol v1 error codes, the ten-bug corpus, and agent-speed debugging (pause budgets, background jobs). Closed #6.
- **#26 (`99efe66`):**
  - `--set-hit`, `by_hit`, Python scopes in `describe`, and safe negative indexes;
  - Java fixes for array indexing, object fields and a String size cap.
- **#28 (`8ceeefe`):** the real program for #7 (CPython 3.9.6 `ipaddress`, CVE-2024-4032), `--run`/`--arg`, and the live agent's feedback. Closed #7.
- **#30 (`77fcaa9`):** Codex's Rust preparation hardening ported onto `main`; this superseded #24.
- **#29 (`5ad98c5`):**
  - launched JVMs: exits that java-debug drops are inferred safely (pid-authoritative, launch only);
  - a bounded DAP trace;
  - clear errors for a corrupted DAP stream.
- **#32 (`627eb1a`):** optimized native and Rust builds (closes #12 and #13).
  - Opt-in `optimization` (O0/Og/O1/O2) and Rust `opt_level`, both recorded in the target identity.
  - Locals LLDB cannot read are reported as `unavailable` with a reason, never as a value.
  - Optimized targets refuse `set_variable` and show Rust enums as opaque. Probes, sessions, `get` and `describe` carry an `optimized` flag plus a warning that values may be wrong.
- **#33 (`3143129`):** remote workers over SSH tunnels (#8). See [REMOTE.md](REMOTE.md).
  - `--worker <loopback URL>` works for `probe` and `session start`. The token is read only from `$PEEKY_WORKER_TOKEN`.
  - The worker checks the source with `expect_sha256` and refuses before launching on a mismatch.
  - Disconnects return `UNREACHABLE` (retryable, nothing sent) or `TRANSPORT_LOST`, and the session record is kept.
  - Adds `session close --forget` and the worker's `--idle-timeout`.
  - Two-process relay tests cover a mismatch refusal, a dropped tunnel, reconnecting, and a dead local worker.
- **#35 (`1706807`):** the captured-evidence store, ADR 0004 step 4 (the owner's "as easy as heappy" direction).
  - Every probe (local, `--worker` and `--background`) is saved on the client to SQLite and gets a `run_id`.
  - `peeky sql` runs one read-only, bounded SELECT over saved runs.
  - `peeky queries` adds canned questions such as `diff`, `set_effect` and `hits`.
  - `peeky sql --schema` is the manifest, and the new error code is `INVALID_QUERY`.
  - Two review rounds; every finding is fixed (worker probes stay stateless, read-only open with journal recovery, result bounds).
- **Dependabot:** #1, #3, #4 and #5 were merged. #2 (`upload-artifact` v7) was validated by dispatching `artifact.yml`, then fast-forwarded over SSH as `7308955`, because the API token lacks the `workflow` scope.

**Closed issues:** #6, #7, #9, #10, #11, #12, #13.

**Open issues:**
- **#27:** an attached JVM whose `stopped` event is lost (seen once on Venice). The launch case is fixed. 160 targeted stress runs (simple and Venice-like thread churn) did not reproduce it; diagnostics stay in place.
- **#8:** the code is merged. The issue stays open until a recorded run on a real second host is attached (procedure in REMOTE.md). This machine has no VM tooling or local sshd.

**Process notes:**
- Create PRs and issues with `gh api` (REST): `gh pr create`/`gh issue create` GraphQL calls returned 504s, and the app's PR tool needs the project's GitHub link.
- Squash-merge with `gh api -X PUT .../pulls/N/merge -f merge_method=squash -f sha=<head>`.
- Never pipe pytest output through `tail` before checking its exit code.
- Changes to workflow files cannot be merged through the API (no `workflow` scope). Validate them by dispatching the workflow, then push to `main` over SSH.

**Local Java adapter bundle:** `outputs/peeky/work/java-adapter-strcap` (with the String cap). `~/.peeky/venice/attach-*.json` point to it. Rebuild with `scripts/setup_java_adapter.py --output <absolute path>`.

## Previous slices


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
- **Venice production-code breakpoints:** verified. A probe in the client library (`ControllerClient.createNewStore`) and one in the controller's HTTP handler (`CreateStore`, on a Jetty thread) each captured both invocations, and the tests PASSED. The recipe is in JAVA_ATTACH.md.
  - Fixed while doing this: `describe` without `--at` on a multi-source target now describes every source instead of raising a traceback.
  - Every unexpected CLI exception is now a structured error.
  - Missing paths now return `INVALID_REQUEST` with a path hint.
- **Next:**
  - one PR from `feat/corpus` covering the whole stack;
  - SSH separate host (#8; needs a second host);
  - a user-selected non-synthetic program.

Known local-only limitation (pre-existing on main): test_native_adapter_death_reaps_stopped_target fails with Apple LLDB on macOS and passes on hosted Linux.

## Next slices

The original slices are delivered: #6, #7, #9, #10, #11, #12 and #13 are closed, and the code for #8 is merged. Remaining:
1. **#8:** attach a recorded run on a real second host (see REMOTE.md).
2. **#27:** if the lost `stopped` event on an attached JVM recurs, capture the DAP trace and fix it from that evidence.
3. **ADR 0004, step 5:** a prepared-build cache, only if measured probe latency requires it. Step 4 shipped in #35; the SQL-shaped probe form was deliberately not implemented.
4. Implement the bounded evidence correlation design (code, logs, metrics).

Read AGENTS.md, DESIGN.md, LANGUAGES.md, and ROADMAP.md for acceptance criteria. Runtime intervention is evidence only; never claim a fix without fresh verification and matching source/build identity. Preserve a checkpoint when usage runs low; report shipped work and actual limitations.

Mixed-runtime inference engines (Python/C++/CUDA) are now an explicit design direction in MIXED_RUNTIME.md, not shipped support. Preserve backend session ownership, correlated evidence and clock domains; qualify extension-module coexistence and GPU hardware separately after host-language foundations.

C++ microbaseline:10 local Apple LLVM21 samples, inspect median62.63ms/p9574.91ms, class expansion123.11/124.07ms. Full inspect+class+two array expansion sequence437.95/450.80ms. HTTP/DAP/provenance included; build/model excluded. See PERFORMANCE.md and performance/cpp-2026-10-14.json. These are measurements, not SLOs.

## 12:20 UTC continuation checkpoint

Repository clean on entry; only unrelated Dependabot PRs open. Ordinary usage allowed but weekly window99% used; no reset or purchased credits consumed. Completed a bounded parser compatibility check before starting any larger implementation: the retained local Rust scratch DWARF4 dump (1,754,438 bytes) parses under the unchanged2MiB limit in0.433s and yields declared src/main.rs lines. This is parsing evidence only, not a fresh source/build provenance validation or a worker qualification. Raw result remains ignored at work/rust-line-parser-check.json. Linux4,808,912-byte dump still requires a reviewed Rust-specific bound or declared-source extraction. No implementation edits or background provider jobs started. Resume from RustTarget/Option/test-verification plan when sufficient ordinary usage is available.

## Owner direction: stateless probes (2026-10-14)

The owner prefers stateless operation where possible and wants heappy-level ease of use. Both modes must be supported: the existing stateful sessions and one-request stateless probes over the same guarded engine, with a `describe` manifest, canned probes and SQL over captured evidence. Recorded as [ADR 0004](adr/0004-stateless-probes.md). This is direction only, not started. Until it lands, new worker features should be expressible as declared probe steps.
