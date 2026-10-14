# Contributing

Use Python 3.11 or newer. From the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest tests/unit
pytest tests/integration
python -m build
```

Develop one small milestone at a time. Add a regression test for changed debugger behavior; tests must terminate their subprocesses even on failure. Keep unit tests independent of real debugger processes and model APIs. Integration tests belong in `tests/integration` and use real debugpy with deterministic target programs. Hosted CI must never need model credentials or external model calls. Exercise model behavior through a fake provider in unit tests; opt-in live evaluations are separate from merge checks.

Run `ruff check --fix .` and `ruff format .` before submission. Describe the observed problem, the resulting behavior, and the checks you ran in each pull request. Include protocol or architecture changes in the design documents. Record adversarial review findings and their resolution when changing state, permissions, transport, or execution semantics.

## CI budget

CI starts quality, unit, Python 3.11/3.13 debugger integration, and wheel smoke jobs in parallel. Each job has a four-minute execution limit; the desired overall run is under five minutes excluding queue delays. This is a budget and measured target, not a guarantee: hosted-runner scheduling and package download delays are external. Watch the longest job's duration as tests grow. Split independent fixtures or move optional benchmarks to manually triggered jobs before increasing timeouts. Newer pushes cancel obsolete runs for the same branch or pull request.

Direct runtime and development versions are pinned. Keep the dev extra and `requirements-dev.txt` aligned. Actions are pinned by commit SHA and updated by Dependabot. Transitive pip dependencies are not yet locked; a full lock is a follow-up if reproducibility requires it.

## Delivery

The manual `Build private artifact` workflow checks out the selected revision, reruns checks, builds distributions, smoke-tests the installed wheel, and retains them as a private repository artifact for seven days. It does not publish to a registry or deploy a worker. Run the normal CI workflow on the revision before sharing an artifact. Production deployment and release automation are later milestones and require their own environment and rollback design.
