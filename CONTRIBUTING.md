# Contributing

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md). Report bugs and ask for
features with the [issue templates](https://github.com/sushantmane/peeky/issues/new/choose).

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

Run `ruff check --fix .` and `ruff format .` before submission. Describe the observed problem, the resulting behavior, and the checks you ran in each pull request. Add a line to `CHANGELOG.md` under **Unreleased** for each change a user would notice. Include protocol or architecture changes in the design documents. Record adversarial review findings and their resolution when changing state, permissions, transport, or execution semantics.

## CI budget

CI starts quality, unit, Python 3.11/3.13 debugger integration, and wheel smoke jobs in parallel. Each job has a four-minute execution limit; the desired overall run is under five minutes excluding queue delays. This is a budget and measured target, not a guarantee: hosted-runner scheduling and package download delays are external. Watch the longest job's duration as tests grow. Split independent fixtures or move optional benchmarks to manually triggered jobs before increasing timeouts. Newer pushes cancel obsolete runs for the same branch or pull request.

Direct runtime and development versions are pinned. Keep the dev extra and `requirements-dev.txt` aligned. Actions are pinned by commit SHA and updated by Dependabot. Transitive pip dependencies are not yet locked; a full lock is a follow-up if reproducibility requires it.

## CI's Linux lanes on your machine

When hosted CI cannot run, run the same Linux lanes in Docker. You need Docker or OrbStack; the debugger needs `SYS_PTRACE`:

```sh
docker build -t peeky-ci-linux scripts/ci_linux
git archive HEAD | docker run --rm -i --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
  peeky-ci-linux bash -c 'mkdir /w && tar -x -C /w && cd /w && scripts/ci_linux/run_lanes.sh'
PEEKY_E2E_DOCKER=1 pytest tests/remote   # the remote SSH lane, on the host
```

It runs quality, unit, integration, C, C++, Rust, Java and wheel smoke against the committed tree (`git archive HEAD`), plus a Rust lane with LLVM 20's lldb-dap, whose `readMemory` qualifies Rust memory views (hosted CI has only LLVM 18), and prints one `PASS`/`FAIL` line per lane. Under a `FAIL` line, it prints what each failing test said, bounded, so a failure seen once can be diagnosed. The tree goes in through stdin rather than a bind mount: OrbStack bind mounts from protected folders such as `~/Documents` can hang. The image uses your machine's architecture (arm64 on Apple Silicon); hosted CI is x86_64.

## Delivery

The manual `Build artifact` workflow checks out the selected revision, reruns checks, builds distributions, smoke-tests the installed wheel, and keeps them as a workflow artifact for seven days. It does not publish to a registry or deploy a worker. Run the normal CI workflow on the revision before sharing an artifact. Production deployment and release automation are later milestones and require their own environment and rollback design.
