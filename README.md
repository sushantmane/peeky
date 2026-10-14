# Peeky

**Let agents peek inside running programs.**

Peeky gives a coding agent a controlled debugging loop: choose a breakpoint, inspect live state, test a hypothesis by changing a variable, and validate a candidate fix in a fresh process. Runtime experiments produce evidence; a separate verifier decides whether the source change passes the supplied checks.

This private repository is an MVP foundation, migrated from a working prototype and revised after two adversarial reviews. It is not a production debugger service or a sandbox for hostile code.

## Current Python alpha

- Python programs through debugpy and the Debug Adapter Protocol (DAP).
- One controller and one session per worker.
- JSON HTTP client/worker interface with session and stop identities.
- Local launch, breakpoints, bounded inspection, variable changes, continuation, and evidence reports.
- A bounded model-driven investigation using Codex CLI structured decisions.
- A candidate source patch checked by a trusted verifier in a fresh process.
- Loopback transport; SSH forwarding for remote access.

The minimum product scope is **Python, Java, C, C++, and Rust**, delivered in that order through incremental releases. Only Python is implemented today. See the [five-language plan](docs/LANGUAGES.md). Kubernetes deployment, multi-tenant workers, attach, and production mutation policy remain later milestones. The initial CI support contract is Linux with Python 3.11 and 3.13; macOS is used for local development but is not yet in the CI matrix.

## Start

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest tests/unit tests/integration
```

Python 3.11+ is required. Run commands from the repository root.

For a live investigation, install and sign in to Codex CLI, then choose a model available to your CLI account:

```sh
peeky-agent --model YOUR_MODEL --output runs/first-investigation
```

The bundled pagination program uses one-based page numbers incorrectly. The agent receives the bug report and source, chooses its own breakpoint and runtime intervention, and proposes a correction. The output directory contains the decision/evidence report, candidate source, and a diff after verification. Original source and verifier files are retained. A live run consumes model usage and sends selected source and runtime evidence to that model. PR CI uses deterministic model fixtures and never calls a paid model.

## Separate worker and client

```sh
export DEBUG_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
peeky-worker --root "$PWD" --port 8765
```

In a second terminal, set the same secret and use the SDK:

```python
import os
from peeky.client import Client

client = Client('http://127.0.0.1:8765', os.environ['DEBUG_TOKEN'])
launch = client.call('launch', program='examples/pagination_bug.py',
                     breakpoints=[{'line': 8}])
session = {'session_id': launch['session_id']}
stop = client.call('wait', **session, timeout=2)
while stop['state'] == 'running':
    stop = client.call('wait', **session, timeout=2)
if stop['state'] == 'stopped':
    print(client.call('inspect', **session, stop_id=stop['stop_id']))
print(client.call('report', **session))
client.call('close', **session)
```

For remote use, run the worker on the target and forward its loopback port:

```sh
ssh -N -L 8765:127.0.0.1:8765 user@target
peeky-agent --url http://127.0.0.1:8765 --model YOUR_MODEL --output runs/remote
```

Keep matching source on the client and worker. The runner checks source identity. Verification currently executes on the client, so its dependencies must match the target environment. SSH topology is documented; CI exercises the network interface locally, not a live cluster.

## Design and delivery

- [Design](docs/DESIGN.md): boundaries, lifecycle, protocol, failures and extension points.
- [MVP plan](docs/ROADMAP.md): incremental slices with acceptance criteria.
- [Testing and CI budget](docs/TESTING.md): parallel lanes and measured timings.
- [Threat model](docs/THREAT_MODEL.md): trusted-code assumptions and remote constraints.
- [Adversarial review](docs/reviews/INITIAL_REVIEW.md): findings and dispositions.
- [Decision record](docs/adr/0001-mvp-boundaries.md): why Python/DAP/SSH comes first.
- [Contributing](CONTRIBUTING.md): local checks and change process.

Required CI is designed as parallel quality, unit, integration, and packaging lanes, each with a four-minute cutoff. The target is below five minutes from jobs starting to required checks completing; hosted-runner queue time is measured separately. A cutoff is not evidence of speed: actual run measurements belong in the testing document.

The current GitHub plan does not allow enforced branch protection for this private repository: CI runs, but merge gating must be observed manually. See the testing document for the API result and measurements.

Delivery builds wheels and source archives as private GitHub Actions artifacts. No PyPI publishing, public release, or deployment is configured.
