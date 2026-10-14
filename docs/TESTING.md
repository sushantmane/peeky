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

Initial local and hosted run results are recorded below after validation. The first hosted run is the initial cold-cache baseline; it does not establish a long-term latency guarantee.

Local bootstrap: Python 3.11 on macOS, 34 tests passed in 2.93 seconds; isolated wheel and sdist build succeeded. Hosted Linux measurements pending the first push.
