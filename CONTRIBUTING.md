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

## Security

Report a vulnerability privately, as [SECURITY.md](SECURITY.md) describes. Don't put its details in a public issue or pull request.

## CI budget

CI builds its image first, then starts its lane jobs in parallel; remote SSH e2e and, once the repository is public, the macOS lanes run beside the image build. [TESTING.md](docs/TESTING.md#required-ci-on-github-actions) lists the jobs, with their timeouts. A run's wall time is the image build plus the slowest lane job, unless remote SSH e2e or a macOS lane alone takes longer: watch the slowest job as tests grow, and split its lanes into two jobs before raising its timeout. Each job's runner minutes count against the account's Actions allowance while the repository is private; macOS minutes count ten times, so the macOS lanes run only once the repository is public. Until then, run them on a Mac ([TESTING.md](docs/TESTING.md#local-commands)). Newer pushes cancel obsolete runs for the same branch or pull request.

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

The manual `Build artifact` workflow checks out the selected revision, reruns checks, builds distributions, smoke-tests the installed wheel, and keeps them as a workflow artifact for seven days. It does not publish to a registry or deploy a worker. Run the normal CI workflow on the revision before sharing an artifact. Production deployment of workers is a later milestone, and needs its own environment and rollback design.

## Releasing

A release is a tag on `main`. Pushing a tag `vX.Y.Z` runs the `Release` workflow, `.github/workflows/release.yml`. Its three jobs run one after another, and each starts only if the one before it passed:

1. **build** builds the wheel and sdist once, with Python 3.12 and `build` 1.6.1, and with no OIDC token. It fails, before anything is published, unless:
   - the tagged commit is on `main`;
   - the tag is `v` followed by the built version, which is `X.Y.Z` (`tests/unit/test_release.py` checks `__version__` has that form);
   - `CHANGELOG.md` has the heading `## X.Y.Z (YYYY-MM-DD)`, with the release date; its section becomes the release notes;
   - the wheel, installed into a clean environment, says `peeky X.Y.Z` to `peeky --version`, and passes `scripts/smoke_wheel.py`, which debugs a real program.
2. **publish to PyPI** uploads those files by trusted publishing, in the `pypi` environment. Only this job can ask for an OIDC token, and it runs no code from the repository. PyPI accepts the workflow's short-lived token; no PyPI token is stored anywhere.
3. **GitHub release** creates the release for the tag, with the wheel and sdist attached and the changelog's section as its notes.

To cut a release:

1. Check `main`: CI must be green.
2. In a pull request, bump the version and the changelog, then merge it:
   - set `__version__` in `src/peeky/__init__.py`, the one place the version is kept;
   - in `CHANGELOG.md`, rename **Unreleased** to `## X.Y.Z (YYYY-MM-DD)`, with the release date, and start a new, empty **Unreleased** above it.
3. Tag the merge commit, and push the tag:

   ```sh
   git switch main && git pull --ff-only
   git tag -a v0.2.0 -m "Peeky 0.2.0"
   git push origin v0.2.0
   ```

4. Watch the `Release` run in the Actions tab, and approve the `pypi` deployment if the environment asks for a reviewer.

If **build** fails, nothing was published: delete the tag (`git push origin :refs/tags/v0.2.0 && git tag -d v0.2.0`), fix the cause in a pull request, and tag again. If **publish to PyPI** fails because PyPI refused the workflow (no pending or trusted publisher matches the fields below), fix the publisher on pypi.org, and re-run the failed jobs: the run keeps the built files. Once a version is on PyPI, it can't be uploaded again, even after deleting it: if a published version is broken, yank it on pypi.org, and release the next version. If only **GitHub release** fails, re-run that job.

Version 0.1.0 already has a tag and a GitHub release, made by hand. The first release by this workflow must be a new version.

### One-time setup, by the owner

Do this once, before the first tag.

**On pypi.org,** add a pending publisher. It lets this workflow create the project `peeky-debug` (`pyproject.toml`'s `[project] name`) with its first upload:

1. Sign in to [pypi.org](https://pypi.org), with two-factor authentication, and open **Your account → Publishing** (<https://pypi.org/manage/account/publishing/>).
2. Under **Add a new pending publisher**, choose **GitHub**, and fill in:
   - **PyPI Project Name:** `peeky-debug`;
   - **Owner:** `sushantmane`;
   - **Repository name:** `peeky`;
   - **Workflow name:** `release.yml`;
   - **Environment name:** `pypi`.
3. Select **Add**. The first upload creates the project, and the pending publisher becomes its trusted publisher. The project then lists it under **Manage → Publishing**.

**On GitHub,** under the repository's **Settings → Environments**, create the environment `pypi`. The workflow would otherwise create it with no protection. Recommended:

- **Required reviewers:** the owner, so that every upload waits for approval;
- **Deployment branches and tags:** **Selected branches and tags**, with the tag rule `v*`, so that only a version tag can publish;
- a tag ruleset on `v*` (**Settings → Rules → Rulesets**), so that only the owner can create, move or delete version tags.
