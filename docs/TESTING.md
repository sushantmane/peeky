# Testing and delivery budget

## Required CI: parallel from the first commit

All five jobs start independently. No build/install job sits ahead of the tests.

| Lane | Scope | Design budget |
| --- | --- | --- |
| quality | Ruff lint and formatting | 60s |
| unit | Framing, failure injection, lifecycle validation, verification bypasses | 90s |
| integration (Python 3.11) | Real debugpy + HTTP + runtime intervention + owned target cleanup | 180s |
| integration (Python 3.13) | Same integration contract on the newer interpreter | 180s |
| wheel smoke | Build wheel/sdist, install clean, import and CLI entry points outside source tree | 90s |
| remote SSH e2e | A second host (Linux container with key-only sshd) reached through a real `ssh -L`: probe, session, tunnel drop and recovery, source-mismatch refusal, worker loss (`tests/remote`, `PEEKY_E2E_DOCKER=1`) | 120s |
| C LLVM 18 conformance | Real C debugging (`tests/native`) on Ubuntu 24.04, with `clang-18`, `lldb-18` and `llvm-18` from the signed Ubuntu repositories. The LLVM major is pinned; distribution patch versions can change, and each run prints them. It acquires its tools explicitly, never relying on a hosted image's toolchain, and starts independently of the Python, Java, package and quality lanes. It reports the acquisition and conformance times | The four-minute timeout; cold hosted timing must be measured before claiming the budget |
| C++ LLVM 18 conformance (`native-cpp`) | Real C++ debugging and agent verification (`tests/cpp`) on Ubuntu 24.04, with LLVM 18 from signed Ubuntu package repositories. It records the installed package revisions, and depends on no C or Java lane | The four-minute timeout; measured hosted runs show whether it is met |
| C++ LLVM 20 (Docker mirror only, `scripts/ci_linux`, `cpp-llvm20`) | `tests/cpp` with lldb-dap 20; `PEEKY_EXPECT_READ_MEMORY=1` makes the tests expect libstdc++ strings read whole from memory | |
| Rust LLVM 20 (Docker mirror only, `scripts/ci_linux`) | Rust memory views with lldb-dap's `readMemory`; `PEEKY_EXPECT_READ_MEMORY=1` fails the lane if values fall back to raw fields | 60s |

Every lane has a four-minute timeout. The goal is <5 minutes of execution wall time for the required checks, ideally <3 minutes. Queue delays are recorded separately and are outside the workflow's control. Cold cache and dependency service availability can affect duration. A timeout is a failure, not evidence that the speed goal was met.

Pip caching uses package/dev requirements as keys. Superseded branch/PR runs are canceled. Actions are pinned to commit SHAs. Permissions are read-only. No live model, API token, user program, networked debugger target, or paid external service is needed for PR CI. Diagnostics remain in job logs; the delivery workflow uploads distributions only.

## Test layers

- Fake-adapter tests exercise malformed/oversized framing, control-event retention under output floods, invalid input, stale sessions, uncertain mutation failure, and path confinement.
- Process tests verify output caps, timeouts, credential stripping and cleanup.
- Verification regressions reject premature exit(0), missing completion, modified checker, no runtime evidence and an overfit pagination patch.
- Real adapter tests run local HTTP and debugpy, observe values, mutate them, continue and verify the candidate with the fixed checker.
- The Java lane (`PEEKY_JAVA_ADAPTER="$PWD/work/java-adapter" pytest tests/java`) includes
  `tests/java/test_java_worker.py`. It uses real HTTP requests, a worker HTTP server, the real Java
  adapter and a separate JVM. Its fixture has a package, a nested class, an integer local, an array
  local, a process ID and a check that the worker token is present. It exercises a runtime change,
  a stale stop identity, a required fresh inspection, a fresh launch's reset, rejection of a
  tampered original or copied source or compiled class, and cleanup after the adapter fails.
  Public breakpoint events and the actual stopped locations show that the wanted breakpoint was
  reached. The test's HTTP worker runs in a thread of the test process; the debugger and the
  target are separate owned processes. That shows real transport and JVM behavior, not a
  deployment on another host or source synchronization across roots. The fixtures hold no
  secrets, and no required test calls a model.
- Packaging smoke imports the wheel in a clean environment outside the checkout and exercises entry-point help. The installed wheel also launches a real debugger and inspects a value; a separate-host SSH fixture remains subsequent acceptance work.

The independent example verifier checks first/middle/last/empty pages, page size edge cases and invalid inputs. Passing these proves those cases, not arbitrary pagination correctness. Malicious native code and deliberate escape from owned process groups are outside the MVP trust contract.

## Local commands

```sh
ruff check .
ruff format --check .
pytest tests/unit --durations=10
pytest tests/integration --durations=10
python -m build
```

C conformance on macOS uses the Command Line Tools. Without `PEEKY_NATIVE_DAP`, the native
integration tests skip, and say so:

```sh
export PEEKY_NATIVE_DAP="$(xcrun --find lldb-dap)"
export PEEKY_NATIVE_CC="$(xcrun --find clang)"
export PEEKY_NATIVE_DWARFDUMP="$(xcrun --find llvm-dwarfdump)"
export PEEKY_NATIVE_DSYMUTIL="$(xcrun --find dsymutil)"
export PEEKY_NATIVE_SYSROOT="$(xcrun --show-sdk-path)"
pytest tests/native --durations=10
```

C++ conformance on macOS uses the Command Line Tools too:

```sh
PEEKY_NATIVE_DAP="$(xcrun --find lldb-dap)" \
PEEKY_NATIVE_CXX="$(xcrun --find clang++)" \
PEEKY_NATIVE_DWARFDUMP="$(xcrun --find llvm-dwarfdump)" \
PEEKY_NATIVE_DSYMUTIL="$(xcrun --find dsymutil)" \
PEEKY_NATIVE_SYSROOT="$(xcrun --show-sdk-path)" \
pytest tests/cpp --durations=10
```

Tests have a 45-second per-test timeout. Unit and integration paths can run separately, matching the parallel CI lanes.

## Live model evaluation

Manual only. Record selected model, input bug, decisions, observations, interventions, source/check hashes, candidate and verification completion. Evaluate multiple distinct bugs before calling the MVP generally reliable. Never use stored model answers as evidence of a fresh model run.

## Private delivery

`Private build artifact` is a manually triggered workflow which validates, builds, and uploads wheel/sdist artifacts for seven days. It does not publish to PyPI, create a public release, or deploy a worker. Later release automation should use a release checklist and explicit environment/registry decisions.

## Measurements

The first hosted run is a cold-cache baseline; it does not establish a long-term latency guarantee.

Local bootstrap: Python 3.11 on macOS, **36 tests passed in 2.94 seconds**; isolated wheel and sdist build succeeded.

[First hosted CI run](https://github.com/sushantmane/peeky/actions/runs/38526357448), commit `75dd4cd`, completed successfully in **22 seconds** from workflow creation to last job completion (2026-10-14). Jobs began within 3–4 seconds and ran concurrently; execution span was 19 seconds.

| Job | Observed duration |
| --- | ---: |
| quality | 12s |
| unit | 16s |
| integration Python 3.11 | 17s |
| integration Python 3.13 | 18s |
| wheel smoke, including real debugger | 19s |

These are GitHub API job start/completion timestamps, including per-job setup and teardown. This run meets the <5-minute target with substantial room; measure again as coverage grows.


Private delivery was also exercised successfully: [artifact run](https://github.com/sushantmane/peeky/actions/runs/38526401333) built and uploaded the private distributions, with a 29-second job.

The [bounded-transport PR run](https://github.com/sushantmane/peeky/actions/runs/38528024457), commit `7e3a9d3`, passed all five concurrent jobs with a 21-second execution span: quality 11s, unit 18s, integration 19s each, wheel 21s. Local validation was 42 tests in 5.14s.

### Merge enforcement limitation

The attempt to require all five checks on `main` returned GitHub HTTP 403: private-repository branch protection requires a plan upgrade. The repository remains private. CI runs on main pushes and PRs, but checks are not enforced merge gates under the current plan. Until protection is available, use PRs and verify all five green checks manually. No plan or visibility change was made.

## C investigation slice (2026-10-14)

PR #20, run 38542794719: all seven independent hosted jobs passed. C LLVM 18 conformance took 50 seconds; the longest job was Java at 68 seconds. Quality 10 seconds, unit 21 seconds, Python integrations 20/19 seconds, wheel smoke 19 seconds. These are job execution durations, excluding queue time. The final source head was 4cca330; merged as 07d1835.

## C++ investigation slice (2026-10-14)

PR #21, run38549552255: all eight independent jobs passed. C++ LLVM18 took37s, C50s, Java70s (critical lane), quality12s, unit21s, Python21/20s, wheel21s. Job execution excludes queue time. Source head18e00a5 merged as8890113.

## Rust engineering gate (2026-10-14)

PR #22, run38556716292, final source770c60a: nine independent checks passed. Rust41s, C40s, C++31s, Java72s, Python16/18s, unit19s, quality10s, wheel23s. Job durations exclude queue time. Rust rawprobe2.03s, not an HTTP worker or repair benchmark. Initial Linux symbol capture failed with the2MiB ceiling; engineering capture explicitly permits16MiB and measured4,808,912 bytes. Five regressions keep production parsing capped at2MiB.

## Rust worker conformance (2026-10-14)

Branch `feat/rust-worker`, head `bd2ade2`, run 38666345665 (`workflow_dispatch`): nine independent checks passed. Rust 43 s, C 51 s, Java 53 s, C++ 39 s, unit 21 s, Python 17/19 s, quality 9 s, wheel 21 s; job durations exclude queue time. The Rust lane now runs the raw probe (3.44 s, application symbols 0.242 s) plus real HTTP worker conformance: inspection/intervention 1.88 s, source drift 1.31 s, compile failure 0.54 s. No Rust test is skipped in CI. Local macOS runs set `PEEKY_RUST_DSYMUTIL` as well.

## Rust verification and review fixes (2026-10-14)

PR #23 head `9d1e338`, run 38684077815 (`pull_request`): nine independent checks passed. Rust 50 s, Java 58 s (longest), C 51 s, C++ 33 s, unit 29 s, Python 20/25 s, quality 13 s, wheel 20 s. Linux Rust tests: 6 passed in 20.08 s, none skipped. Full deterministic investigation 8.78 s, frozen-test drift 3.44 s, HTTP worker flow 2.31 s, source drift 1.62 s, raw probe 0.94 s, compile failure 0.57 s. The previous head `9985257` (run 38683005986) also passed all nine lanes. Locally on macOS, 436 unit tests run in about 9.3 s; the suite now includes deadline and timeout regressions that sleep deliberately.
