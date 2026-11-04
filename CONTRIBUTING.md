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

Run `ruff check --fix .` and `ruff format .` before submission. Describe the observed problem, the resulting behavior, and the checks you ran in each pull request. Add a line to `CHANGELOG.md` under **Unreleased** for each change a user would notice. Include protocol or architecture changes in the design documents. When changing state, permissions, transport, or execution semantics, record the adversarial review's findings and how each was resolved in the pull request.

## Sign off your commits (DCO)

Peeky takes contributions under the [Developer Certificate of Origin 1.1](DCO) (DCO), as the Linux kernel and many Apache-2.0 projects do. There is no contributor licence agreement to sign. Each commit in a pull request from an outside contributor must be signed off by its author, with a line at the end of its message:

```text
Signed-off-by: Jane Doe <jane@example.com>
```

**What it means.** Signing off certifies the DCO's four statements, in [DCO](DCO): that you wrote the change, or otherwise have the right to submit it under the project's licence, Apache-2.0; and that you understand the contribution, and your sign-off with your name and email, are public and kept for good. You keep your copyright. As the Apache License's section 5 says, what you contribute is licensed under the Apache License 2.0.

**How.** Commit with `-s` (`--signoff`), and git adds the line from your `user.name` and `user.email`:

```sh
git commit -s -m "Explain the change"
```

The email must be the commit's author email, so commit as the identity you sign off with. Sign off with your name, not an anonymous one. A commit made in GitHub's web editor is signed off if the repository requires sign-off for web-based commits.

**A missing sign-off.** The `DCO` check fails, and names each commit without one. Add the sign-off, and force-push the branch:

```sh
git commit --amend --signoff --no-edit    # the last commit
git rebase --signoff origin/main          # every commit since main (use your pull request's base)
git push --force-with-lease
```

The check, `.github/workflows/dco.yml`, runs on each pull request. It checks every commit except merges, and accepts a `Signed-off-by:` line whose email is the commit author's, in any case. It skips pull requests from the maintainer, and from apps the maintainer installed, such as Dependabot: the DCO applies to outside contributions.

## Security

Report a vulnerability privately, as [SECURITY.md](SECURITY.md) describes. Don't put its details in a public issue or pull request.

## CI budget

CI builds its image first, then starts its lane jobs in parallel; remote SSH e2e runs beside the image build. [TESTING.md](docs/TESTING.md#required-ci-the-linux-lanes-on-github-actions) lists the jobs, with their timeouts. A run's wall time is the image build plus the slowest lane job, unless remote SSH e2e alone takes longer: watch the slowest job as tests grow, and split its lanes into two jobs before raising its timeout. Each job's runner minutes count against the account's Actions allowance while the repository is private. Newer pushes cancel obsolete runs for the same branch or pull request.

Direct runtime and development versions are pinned. Keep the dev extra and `requirements-dev.txt` aligned. Actions are pinned by commit SHA and updated by Dependabot. Transitive pip dependencies are not yet locked; a full lock is a follow-up if reproducibility requires it.

## CI's Linux lanes on your machine

CI's lane jobs run `scripts/ci_linux/docker_lanes.sh`, and so can you: it builds CI's image, then runs every lane, or the ones `PEEKY_LANES` names. [TESTING.md](docs/TESTING.md#the-docker-mirror) says what each lane runs. Remote SSH e2e runs on the host, with `PEEKY_E2E_DOCKER=1`. You need Docker or OrbStack; the debugger needs `SYS_PTRACE`:

```sh
scripts/ci_linux/docker_lanes.sh                       # every lane
PEEKY_LANES="unit rust" scripts/ci_linux/docker_lanes.sh  # only these lanes
PEEKY_E2E_DOCKER=1 pytest tests/remote                 # the remote SSH lane, on the host
```

It runs against the committed tree (`git archive HEAD`). It prints one `PASS`/`FAIL` line per lane, with what each failing test said; with `PEEKY_LANE_LOGS_DIR` set to a directory, it keeps each failed lane's whole log there. The tree goes in through stdin rather than a bind mount: OrbStack bind mounts from protected folders such as `~/Documents` can hang. The image uses your machine's architecture (arm64 on Apple Silicon); CI's runners are x86_64.

`scripts/ci_linux/run_tests.sh` runs chosen tests with one lane's environment. On Apple Silicon, an image built for x86_64 runs under emulation (OrbStack's Rosetta). There the debugger can't stop a program, so only cases that don't run one mean anything, such as what a binary's startup code is:

```sh
docker build --platform linux/amd64 -t peeky-ci-linux-amd64 scripts/ci_linux
git archive HEAD | docker run --platform linux/amd64 --rm -i peeky-ci-linux-amd64 bash -c \
  'mkdir /w && tar -x -C /w && cd /w && scripts/ci_linux/run_tests.sh c \
   tests/native/test_native_binary.py -k "normal_build or frame_dummy or adds_to_init or describe"'
```

## Delivery

The manual `Build artifact` workflow checks out the selected revision, reruns checks, builds distributions, smoke-tests the installed wheel, and keeps them as a workflow artifact for seven days. It does not publish to a registry or deploy a worker. Run the normal CI workflow on the revision before sharing an artifact. Production deployment and release automation are later milestones and require their own environment and rollback design.
