# Peeky

**Let agents peek inside running programs.**

Peeky gives a coding agent a controlled debugging loop: choose a breakpoint, inspect live state, test a hypothesis by changing a variable, and validate a candidate fix in a fresh process. Runtime experiments produce evidence; a separate verifier decides whether the source change passes the supplied checks.

This private repository is an MVP foundation, migrated from a working prototype and revised after two adversarial reviews. It is not a production debugger service or a sandbox for hostile code.

## Current Python, Java, C, C++ and Rust investigations

- Python programs through debugpy and the Debug Adapter Protocol (DAP).
- One controller and one session per worker.
- JSON HTTP client/worker interface with session and stop identities.
- Local launch, breakpoints, bounded inspection, variable changes, continuation, and evidence reports.
- A bounded model-driven investigation using Codex CLI structured decisions.
- A candidate source patch checked by a trusted verifier in a fresh process.
- Loopback transport; SSH forwarding for remote access.

The minimum product scope is **Python, Java, C, C++, and Rust**, delivered in that order through incremental releases. All five (Python, Java, C, C++ and Rust) now have experimental investigation loops with independent candidate verification on narrow fixtures; none is general language support. See the [five-language plan](docs/LANGUAGES.md). Kubernetes deployment, multi-tenant workers, attach, and production mutation policy remain later milestones. CI covers Linux with Python 3.11/3.13 and a separate Java 17 lane; macOS is used for local development but is not yet in the CI matrix.

The experimental [Java worker](docs/JAVA_WORKER.md) supports fixed standalone targets: clean compilation, generated source mappings, HTTP inspection/scalar mutation, provenance checks and failure cleanup. The [Java investigation loop](docs/JAVA_AGENT.md) adds fresh candidate rebuilds, independent fresh-JVM checks, and retryable compiler diagnostics. A runnable example is in `examples/java`.

The experimental [C worker](docs/NATIVE.md) adds fresh Clang builds and LLDB runtime inspection with symbol/module provenance and restricted integer mutation. The [C investigation loop](docs/NATIVE_AGENT.md) adds fresh candidate builds, frozen companion headers and independent executable checks. The experimental [C++ slice](docs/CPP.md) qualifies C++17 classes, references and basic array contents with bounded read-only expansion and fresh candidate verification. An experimental [Rust worker](docs/RUST_GATE.md) inspects one dependency-free Cargo binary (structs, fieldless enums, compiler-validated `Option<i32>` active variants) and mutates root i32 locals. The [Rust investigation loop](docs/RUST_AGENT.md) verifies candidates with both the compiled Cargo integration test and an independent checker.

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
- [Runtime performance](docs/PERFORMANCE.md): repeatable HTTP/debugger benchmark and raw latency samples.
- [Code, logs, and metrics](docs/adr/0002-correlated-evidence.md): incremental evidence correlation design.
- [Stateless probes](docs/adr/0004-stateless-probes.md): planned one-request probe mode and query interface alongside sessions.
- [Threat model](docs/THREAT_MODEL.md): trusted-code assumptions and remote constraints.
- [Adversarial review](docs/reviews/INITIAL_REVIEW.md): findings and dispositions.
- [Decision record](docs/adr/0001-mvp-boundaries.md): why Python/DAP/SSH comes first.
- [Contributing](CONTRIBUTING.md): local checks and change process.

Required CI is designed as parallel quality, unit, integration, and packaging lanes, each with a four-minute cutoff. The target is below five minutes from jobs starting to required checks completing; hosted-runner queue time is measured separately. A cutoff is not evidence of speed: actual run measurements belong in the testing document.

The current GitHub plan does not allow enforced branch protection for this private repository: CI runs, but merge gating must be observed manually. See the testing document for the API result and measurements.

Delivery builds wheels and source archives as private GitHub Actions artifacts. No PyPI publishing, public release, or deployment is configured.

The [mixed-runtime direction](docs/MIXED_RUNTIME.md) covers future inference-engine debugging across Python, C++ and CUDA; this is planned capability, not current GPU support.
