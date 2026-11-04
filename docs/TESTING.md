# Testing and delivery budget

## Required CI on GitHub Actions

The CI workflow, `.github/workflows/ci.yml`, runs on every pull request, on each push to `main`,
and by hand. Its Linux jobs run on GitHub's hosted Ubuntu 24.04 runners (x86_64), and, once the
repository is public, its [macOS lanes](#the-macos-lanes-once-the-repository-is-public) on its
Apple-silicon macOS 26 runners:

- **CI image:** one job builds the image in `scripts/ci_linux` into the Actions cache. Each lane
  job loads it from there, so the image is built again only when its Dockerfile changes.
- **Lane jobs:** each runs some lanes of `scripts/ci_linux/run_lanes.sh` in that image, through
  `scripts/ci_linux/docker_lanes.sh` with `PEEKY_LANES` naming them. A laptop runs the same
  script ([The Docker mirror](#the-docker-mirror)).
- **Remote SSH e2e** runs on the runner itself, with `PEEKY_E2E_DOCKER=1`: a second host (a Linux
  container with key-only sshd) reached through a real `ssh -L`. It covers a probe, a session, a
  tunnel drop and recovery, a source-mismatch refusal and worker loss; for Java, a launched JVM
  and an attached one; for C, C++ and Rust, a probe with a change, a session, and a changed
  source and header or module refused ([REMOTE.md](REMOTE.md)).

| Job | Lanes | Timeout |
| --- | --- | --- |
| CI image | builds the image into the Actions cache | 30 min |
| lanes (quality unit wheel gdb-dap java) | Ruff lint and format; the unit tests; the wheel smoke test; GDB's DAP; `tests/java` on JDK 17 | 30 min |
| lanes (integration) | real debugpy, HTTP, runtime changes and cleanup, on Ubuntu's Python 3.12 | 30 min |
| lanes (py311), lanes (py313) | the unit and integration tests on CPython 3.11 and on 3.13 | 45 min |
| lanes (c c-gcc-llvm20) | `tests/native` with clang 18 and lldb-dap 18; C built by GCC with lldb-dap 20 | 45 min |
| lanes (cpp cpp-llvm20) | `tests/cpp` with clang++ 18, with lldb-dap 18 and with lldb-dap 20 | 45 min |
| lanes (cpp-gcc cpp-gcc-llvm20) | every C++ case built by g++ 13, with lldb-dap 18 and with lldb-dap 20 | 45 min |
| lanes (rust) | `tests/rust` with the pinned Rust 1.96.0 and lldb-dap 18 | 60 min |
| lanes (rust-llvm20) | `tests/rust` with lldb-dap 20, whose `readMemory` qualifies Rust memory views | 60 min |
| remote SSH e2e | `tests/remote`, on the runner | 30 min |

**How the workflow runs:**

- A timeout is a failure.
- Superseded branch and PR runs are canceled.
- Every job runs on every change: no path filter skips a language's lane.
- Each lane job's summary lists its lanes' PASS or FAIL lines, with their times. A failed lane's
  whole log is kept as an artifact for seven days.
- Actions are pinned to commit SHAs. Permissions are read-only, and checkouts keep no credentials.
- PR CI needs no live model, API token, user program or paid external service.
- PR CI needs no external or persistent networked debugger target. The remote lane creates an
  ephemeral one, in a container.
- **Runner minutes:** the repository is private, so each job's minutes count against the
  account's Actions allowance (2,000 minutes a month on GitHub Free), then its budget. A run of
  every job is estimated at two to three runner-hours, from the lanes' local times; measure it on
  the first runs. When the allowance is spent and no budget covers more, GitHub doesn't start the
  jobs, and each says that the spending limit needs to be increased. Standard runners are free
  for public repositories. A private repository's macOS minutes count ten times, so the macOS
  lanes are skipped until the repository is public.

### The macOS lanes, once the repository is public

The `macos` job runs five lanes in parallel on `macos-26`, GitHub's Apple-silicon runner image
(Xcode 26.6), with Python 3.12. It runs only while the repository is public:

```yaml
if: ${{ !github.event.repository.private }}
```

On a private repository each macOS minute counts as ten against the Actions allowance; on a
public one, standard runners are free. While the repository is private, the job shows as
skipped: run its lanes on a Mac instead ([Local commands](#local-commands)).

| Job | Runs | Timeout |
| --- | --- | --- |
| macOS (unit integration) | `tests/unit` and `tests/integration` | 30 min |
| macOS (c) | the C cases of `tests/native` (`-m "not cpp"`), built by Apple clang | 30 min |
| macOS (cpp) | every C++ case (`-m cpp tests/cpp tests/native`), built by Apple clang | 30 min |
| macOS (rust) | `tests/rust` with the pinned Rust 1.96.0, linked by Apple clang | 60 min |
| macOS (java) | `tests/java` on JDK 17, with the adapter `scripts/setup_java_adapter.py` builds | 20 min |

- `xcrun` finds each tool: lldb-dap, clang, clang++, llvm-dwarfdump and dsymutil, and the macOS
  SDK. The C, C++ and Rust lanes set the `PEEKY_NATIVE_*` variables to them, and the Rust lane its
  `PEEKY_RUST_*` too, with rustup's 1.96.0 `cargo` and `rustc`.
- `PEEKY_EXPECT_READ_MEMORY=1`: Xcode's lldb-dap reads memory, so the tests expect values read
  whole, as in the LLVM 20 lanes.
- The image's default JDK is 21; the Java lane uses its JDK 17, as Linux CI does.
- Homebrew is taken off `PATH`, and the job checks that no GCC is left: the image's Homebrew g++
  would add GCC cases, which are qualified on Linux only.
- Not on macOS: CPython 3.11 and 3.13, GCC, LLVM 18 and 20, GDB's DAP, and remote SSH e2e, whose
  second host is a Docker container.
- On an Apple-silicon Mac, with the five lanes running at once, their tests took 10 minutes
  (unit and integration), 11 (C), 12 (C++), 26 (Rust) and 4 (Java). GitHub's runners (3-core M1)
  are slower: the timeouts are about two and a half times those. Check them against the first
  hosted runs.

### The Docker mirror

`scripts/ci_linux` holds CI's image and lanes, so a laptop runs what CI's lane jobs run:
`scripts/ci_linux/docker_lanes.sh` builds the image, then runs every lane, or the ones
`PEEKY_LANES` names. [CONTRIBUTING.md](../CONTRIBUTING.md) has the commands. Remote SSH e2e runs
on the host instead, with `PEEKY_E2E_DOCKER=1`. The image uses the machine's architecture (arm64
on Apple Silicon); CI's runners are x86_64. Besides Python, Java, C, C++ and Rust with LLVM 18,
the lanes include:

- `py311` and `py313`: the unit and integration tests on CPython 3.11 and 3.13, which uv installs
  in the image beside Ubuntu's 3.12, each in its own environment;
- `cpp-llvm20` and `rust-llvm20`: C++ and Rust with LLVM 20's lldb-dap, whose `readMemory` qualifies
  Rust memory views, and libstdc++ strings with a NUL byte. In `cpp-llvm20`,
  `PEEKY_EXPECT_READ_MEMORY=1` makes the C++ tests expect libstdc++ strings read whole from memory;
- `c-gcc-llvm20`: C built by GCC, with LLVM 20's lldb-dap. It runs the `tests/native` files whose
  cases depend on what GCC builds: prologues, loop headers, `.incbin`, previews, static storage,
  initializers and a block's `extern`, and the caller chains. The rest expect clang's lines and
  text. The `c` lane runs these files' GCC cases with lldb-dap 18;
- `cpp-gcc-llvm20`: every C++ case built by GCC, with LLVM 20's lldb-dap, as `cpp-gcc` runs them
  with lldb-dap 18;
- `gdb-dap`: GDB's DAP, which the CUDA qualification drives (`scripts/cuda/qualify.py`).

It prints one PASS or FAIL line per lane, and exits non-zero on a failure. Under a FAIL line, it
prints what each failing test said, bounded: its header, its frames' `file:line`, its `E` lines and
the start of its output. A lane that isn't pytest shows its last 40 lines. To keep each failed
lane's whole log, set `PEEKY_LANE_LOGS_DIR` to a directory for `docker_lanes.sh` (it mounts the
directory and sets `PEEKY_LANE_LOGS` in the container).

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

C conformance on macOS uses the Command Line Tools, or Xcode's toolchain, whichever
`xcode-select -p` names. Without `PEEKY_NATIVE_DAP`, the native integration tests skip, and say so:

```sh
export PEEKY_NATIVE_DAP="$(xcrun --find lldb-dap)"
export PEEKY_NATIVE_CC="$(xcrun --find clang)"
export PEEKY_NATIVE_DWARFDUMP="$(xcrun --find llvm-dwarfdump)"
export PEEKY_NATIVE_DSYMUTIL="$(xcrun --find dsymutil)"
export PEEKY_NATIVE_SYSROOT="$(xcrun --show-sdk-path)"
pytest tests/native --durations=10
```

Peeky builds with clang at DWARF 5. `PEEKY_NATIVE_DWARF=4` builds at DWARF 4 instead, for a run
of the native or C++ tests that checks its reading. `tests/native/test_native_dwarf_versions.py`
compares the two in every clang lane.

C++ conformance on macOS uses them too:

```sh
PEEKY_NATIVE_DAP="$(xcrun --find lldb-dap)" \
PEEKY_NATIVE_CXX="$(xcrun --find clang++)" \
PEEKY_NATIVE_DWARFDUMP="$(xcrun --find llvm-dwarfdump)" \
PEEKY_NATIVE_DSYMUTIL="$(xcrun --find dsymutil)" \
PEEKY_NATIVE_SYSROOT="$(xcrun --show-sdk-path)" \
pytest tests/cpp --durations=10
```

The [macOS lanes](#the-macos-lanes-once-the-repository-is-public) run on a Mac with Python 3.12,
the five variables above exported, and these:

```sh
export PEEKY_NATIVE_CC="$(xcrun --find clang)" PEEKY_NATIVE_CXX="$(xcrun --find clang++)"
export PEEKY_EXPECT_READ_MEMORY=1
pytest tests/unit tests/integration          # unit integration
pytest -m "not cpp" tests/native             # c
pytest -m cpp tests/cpp tests/native         # cpp
# rust: Rust 1.96.0 (rustup toolchain install 1.96.0), linked by clang
PEEKY_RUST_CARGO="$(rustup which --toolchain 1.96.0 cargo)" \
PEEKY_RUST_RUSTC="$(rustup which --toolchain 1.96.0 rustc)" \
PEEKY_RUST_DAP="$PEEKY_NATIVE_DAP" PEEKY_RUST_DWARFDUMP="$PEEKY_NATIVE_DWARFDUMP" \
PEEKY_RUST_LINKER="$PEEKY_NATIVE_CC" PEEKY_RUST_DSYMUTIL="$PEEKY_NATIVE_DSYMUTIL" \
pytest tests/rust
# java: a JDK 17 first on PATH
python scripts/setup_java_adapter.py --output work/java-adapter
PEEKY_JAVA_ADAPTER="$PWD/work/java-adapter" pytest tests/java
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

## Releases

`Release`, `.github/workflows/release.yml`, runs when a tag `v*` is pushed. Its **build** job
builds the wheel and sdist once, with no OIDC token, and fails before anything is published
unless:

- the tagged commit is on `main`;
- the tag is `v` and the built version, which is `X.Y.Z`;
- `CHANGELOG.md` has the section `## X.Y.Z (YYYY-MM-DD)`, which becomes the release notes;
- the wheel, installed in a clean environment, says `peeky X.Y.Z` to `peeky --version`, and passes
  `scripts/smoke_wheel.py`.

Then **publish to PyPI** uploads those files by trusted publishing, in the `pypi` environment;
it is the only job that can ask for an OIDC token. Last, **GitHub release** creates the release
for the tag, with the files attached. It runs no test suite: release a commit whose CI is
green. [CONTRIBUTING.md](../CONTRIBUTING.md#releasing) has the steps, and the one-time setup.

## Merge enforcement limitation

- GitHub enforces required checks on `main` only for a public repository or a paid plan (HTTP 403).
- CI runs on pushes to `main` and on PRs, but its checks are not yet enforced merge gates.
- Until they are, use PRs, and check by hand that all checks are green.

## Measurements

Each run's job durations are on GitHub. Each lane job's summary lists its lanes' times, and remote
SSH e2e writes its own. The Docker mirror prints each lane's time on its PASS or FAIL line.

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
