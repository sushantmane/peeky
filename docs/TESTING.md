# Testing and delivery budget

## Required CI: parallel from the first commit

The CI workflow, `.github/workflows/ci.yml`, runs eleven jobs. They all start independently: no build
or install job sits ahead of the tests.

| Lane | Scope | Design budget |
| --- | --- | --- |
| quality | Ruff lint and formatting | 60s |
| unit | Framing, failure injection, lifecycle validation, verification bypasses | 90s |
| integration (Python 3.11) | Real debugpy + HTTP + runtime intervention + owned target cleanup | 180s |
| integration (Python 3.13) | Same integration contract on the newer interpreter | 180s |
| Java headless conformance (`java`) | `tests/java` on JDK 17 (Temurin), with an adapter that `scripts/setup_java_adapter.py` builds | — |
| C LLVM 18 conformance (`native-c`) | `tests/native`: real C debugging, on Ubuntu 24.04, with clang-18, lldb-18 and llvm-18 from signed Ubuntu repositories. The LLVM major is pinned; distribution patch versions can change, and each run prints them. It acquires its tools explicitly, never relying on a hosted image's toolchain, and starts independently of the Python, Java, package and quality lanes. It reports the acquisition and conformance times. Cold hosted timing must be measured before claiming the budget | — |
| C++ LLVM 18 conformance (`native-cpp`) | `tests/cpp`: real C++ debugging and agent verification, with the same LLVM 18 packages and `clang++-18`. Cases whose expectations depend on the compiler also run with the runner's g++. It records the installed package revisions, and depends on no C or Java lane. Measured hosted runs show whether it meets its budget | — |
| C++ GCC 13 conformance (`native-cpp-gcc`) | Every C++ case, built by g++ 13 with LLVM 18's lldb-dap: `pytest -m cpp tests/cpp tests/native`. The `cpp` marker selects all of `tests/cpp`, and the C++ cases of `tests/native`. It acquires g++-13 with the LLVM 18 packages | — |
| Rust 1.96 LLVM 18 conformance (`rust-probe`) | `tests/rust`: raw Rust debugging and the experimental Rust worker, with the pinned Rust 1.96.0 toolchain and LLVM 18 | — |
| wheel smoke | Build wheel/sdist, install clean, import and CLI entry points outside source tree | 90s |
| remote SSH e2e | A second host (Linux container with key-only sshd) reached through a real `ssh -L`: probe, session, tunnel drop and recovery, source-mismatch refusal, worker loss; Java: a launched JVM and an attached one; C, C++ and Rust: a probe with a change, a session, and a changed source and header or module refused (`tests/remote`, `PEEKY_E2E_DOCKER=1`) | 120s |
| Rust LLVM 20 (Docker mirror only, `scripts/ci_linux`) | Rust memory views with lldb-dap's `readMemory`; `PEEKY_EXPECT_READ_MEMORY=1` fails the lane if values fall back to raw fields | 60s |

A dash means that no design budget is recorded. The job's timeout still applies.

**Timeouts and goals:**

- Every hosted CI job has a four-minute timeout, except three. Remote SSH e2e and C++ LLVM 18
  conformance have six minutes, and C++ GCC 13 conformance has ten. The C++ jobs run each
  compiler-aware case with g++ as well, so they take longest.
- The Docker mirror's lanes have no timeout.
- The goal is under 5 minutes of execution wall time for the required checks, and ideally under 3.
- Queue delays are recorded separately. They are outside the workflow's control.
- A cold cache, and the availability of dependency services, can affect the duration.
- A timeout is a failure, not evidence that the speed goal was met.

**How the workflow runs:**

- Pip caching uses the package and dev requirements as keys.
- Superseded branch and PR runs are canceled.
- Every job runs on every change: no path filter skips a language's lane.
- Actions are pinned to commit SHAs. Permissions are read-only.
- PR CI needs no live model, API token, user program or paid external service.
- PR CI needs no external or persistent networked debugger target. The remote lane creates an
  ephemeral one, in a container.
- Diagnostics stay in the job logs. The delivery workflow uploads distributions only.

### The Docker mirror

`scripts/ci_linux` runs the same checks and test suites as `ci.yml`'s jobs, once each, in its
Ubuntu image. It skips remote SSH e2e: run that one on the host, with `PEEKY_E2E_DOCKER=1`.
Hosted CI runs the integration tests on Python 3.11 and 3.13. The mirror runs them once, with the
image's `python3`. [CONTRIBUTING.md](../CONTRIBUTING.md) has the commands. The mirror adds lanes
that hosted CI doesn't have:

- `cpp-llvm20` and `rust-llvm20`: C++ and Rust with LLVM 20's lldb-dap, whose `readMemory` qualifies
  Rust memory views, and libstdc++ strings with a NUL byte. In `cpp-llvm20`,
  `PEEKY_EXPECT_READ_MEMORY=1` makes the C++ tests expect libstdc++ strings read whole from memory;
- `c-gcc-llvm20`: C built by GCC, with LLVM 20's lldb-dap. It runs the `tests/native` files whose
  cases depend on what GCC builds: prologues, loop headers, `.incbin`, previews, static storage,
  initializers and a block's `extern`, and the caller chains. The rest expect clang's lines and
  text. The `c` lane runs these files' GCC cases with lldb-dap 18;
- `cpp-gcc-llvm20`: every C++ case built by GCC, with LLVM 20's lldb-dap, as `cpp-gcc` (the
  `native-cpp-gcc` job) runs them with lldb-dap 18;
- `gdb-dap`: GDB's DAP, which the CUDA qualification drives (`scripts/cuda/qualify.py`).

It prints one PASS or FAIL line per lane, and exits non-zero on a failure. Under a FAIL line, it
prints what each failing test said, bounded: its header, its frames' `file:line`, its `E` lines and
the start of its output. A lane that isn't pytest shows its last 40 lines. To keep each failed
lane's whole log, mount a directory and set `PEEKY_LANE_LOGS` to it.

C++ cases whose lines, frame names or notes depend on the compiler take the `cxx` fixture
(`tests/conftest.py`). Each runs once per compiler, with each compiler's expectations, as C's
`["clang", "gcc"]` cases do:

- a lane whose `PEEKY_NATIVE_CXX` is clang runs both, with a g++ on the path for the GCC side;
- a lane whose `PEEKY_NATIVE_CXX` is g++ runs only the GCC side;
- macOS, whose `g++` is clang, runs only the clang side.

Other C++ cases build with `PEEKY_NATIVE_CXX`. Where their expectations differ, they ask
`native_compilers.cxx_is_gcc()`.

## Test layers

- **Fake-adapter tests** exercise malformed and oversized framing, keeping control events under
  output floods, invalid input, stale sessions, uncertain mutation failure, and path confinement.
- **Process tests** verify output caps, timeouts, credential stripping and cleanup.
- **Verification regressions** reject a premature `exit(0)`, missing completion, a modified
  checker, no runtime evidence, and an overfit pagination patch.
- **Real adapter tests** run local HTTP and debugpy. They observe values, change them, continue,
  and verify the candidate with the fixed checker.
- **The Java lane** (`PEEKY_JAVA_ADAPTER="$PWD/work/java-adapter" pytest tests/java`) includes
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
- **Packaging smoke** imports the wheel in a clean environment, outside the checkout, and exercises
  each entry point's help. The installed wheel also launches a real debugger and inspects a value.
- **The separate-host test** is its own lane, remote SSH e2e ([REMOTE.md](REMOTE.md)).

The independent example verifier checks the first, middle, last and empty pages, page-size edge
cases, and invalid inputs. Passing them proves those cases, not that pagination is correct in
general. Malicious native code, and deliberate escape from owned process groups, are outside the
MVP's trust contract.

## Local commands

```sh
ruff check .
ruff format --check .
pytest tests/unit --durations=10
pytest tests/integration --durations=10
python -m build
```

- Each test has a 45-second timeout.
- Unit and integration paths can run separately, matching the parallel CI lanes.
- The unit tests need SQLite 3.35 or later: some build an older store with `ALTER TABLE … DROP
  COLUMN`, and one uses a `MATERIALIZED` CTE. Peeky itself needs SQLite 3.30 or later.

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

## Live model evaluation

Live model evaluation is manual only. Record:

- the selected model and the input bug;
- the decisions, observations and interventions;
- the source and check hashes;
- the candidate, and the verification's completion.

Evaluate several distinct bugs before calling the MVP reliable in general. Never use stored model
answers as evidence of a fresh model run.

## Artifact delivery

`Build artifact` is a manually triggered workflow. It validates, builds, and uploads wheel
and sdist artifacts, which are kept for seven days.

- It does not publish to PyPI, create a release, or deploy a worker.
- Later release automation should use a release checklist, and explicit decisions about the
  environment and registry.

## Merge enforcement limitation

- GitHub enforces required checks on `main` only for a public repository or a paid plan (HTTP 403).
- CI runs on pushes to `main` and on PRs, but its checks are not yet enforced merge gates.
- Until they are, use PRs, and check by hand that all checks are green.

## Measurements

Each run's job durations are on GitHub. The native, Rust and remote jobs also write their toolchain
acquisition and test times to the run's summary. The Docker mirror prints each lane's time on its
PASS or FAIL line.

The [first hosted CI run](https://github.com/sushantmane/peeky/actions/runs/38526357448), on
2026-10-14 at commit `75dd4cd`, had five jobs: quality, unit, integration on Python 3.11 and 3.13,
and wheel smoke. It took 22 seconds from the workflow's creation to the last job's completion.

Two later runs, with each job's execution time, excluding queue time:

### C investigation slice (2026-10-14)

PR #20, run 38542794719: all seven independent hosted jobs passed, at source head `4cca330`.

| C LLVM 18 | Java (longest) | quality | unit | Python 3.11 / 3.13 | wheel smoke |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 50 s | 68 s | 10 s | 21 s | 20 / 19 s | 19 s |

### C++ investigation slice (2026-10-14)

PR #21, run 38549552255: all eight independent jobs passed, at source head `18e00a5`.

| C++ LLVM 18 | C | Java (critical lane) | quality | unit | Python | wheel |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 37 s | 50 s | 70 s | 12 s | 21 s | 21 / 20 s | 21 s |
