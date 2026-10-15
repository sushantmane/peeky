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

Every lane has a four-minute timeout. The goal is <5 minutes of execution wall time for the required checks, ideally <3 minutes. Queue delays are recorded separately and are outside the workflow's control. Cold cache and dependency service availability can affect duration. A timeout is a failure, not evidence that the speed goal was met.

Pip caching uses package/dev requirements as keys. Superseded branch/PR runs are canceled. Actions are pinned to commit SHAs. Permissions are read-only. No live model, API token, user program, networked debugger target, or paid external service is needed for PR CI. Diagnostics remain in job logs; the delivery workflow uploads distributions only.

## Test layers

- Fake-adapter tests exercise malformed/oversized framing, control-event retention under output floods, invalid input, stale sessions, uncertain mutation failure, and path confinement.
- Process tests verify output caps, timeouts, credential stripping and cleanup.
- Verification regressions reject premature exit(0), missing completion, modified checker, no runtime evidence and an overfit pagination patch.
- Real adapter tests run local HTTP and debugpy, observe values, mutate them, continue and verify the candidate with the fixed checker.
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
