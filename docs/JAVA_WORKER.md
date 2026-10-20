# Experimental Java worker

The Java worker slice connects the existing authenticated HTTP API to a fixed, trusted Java target. Each launch copies declared sources into a new workspace, compiles with debug information and annotation processing disabled, derives source/class/line mappings from class-file metadata, and starts the prepared Microsoft-core adapter. The controller cannot supply arbitrary adapter commands or build recipes over HTTP.

This remains experimental Java support. Runtime inspection and scalar intervention are connected to the worker; Java agent-directed source repair and independent verifier execution are separate acceptance work. A runtime intervention is never a verified permanent fix.

## Setup and fixed target

Build the local adapter using JDK 17 and Python 3.11+:

```sh
python scripts/setup_java_adapter.py --output "$PWD/work/java-adapter"
```

The script builds java-debug from its pinned, checksummed source, plus Peeky's launcher, and applies Peeky's source patches to java-debug before compiling. It records them in the bundle's `adapter.json` (`patches`). Today there is one patch, `eventhub-log-guard`: without it, java-debug's event thread could die and stop reporting every stop and exit (#27, see [JAVA_ATTACH.md](JAVA_ATTACH.md#root-cause-java-debugs-event-thread-died)). Peeky refuses a bundle built without a required patch (`UNSUPPORTED`, with the rebuild command), so rebuild older bundles into a new directory.

The bundled example can then be started with `peeky-worker --root examples/java --java-config examples/java/target.json`; set DEBUG_TOKEN first as shown below. Its branch is on line 6. At that stop, change `timeoutMs` from `0` to `5000` and continue to observe `ready` instead of `timed out`.

Create a trusted startup JSON file, for example `work/java-target.json`:

```json
{
  "bundle": "java-adapter",
  "sources": ["demo/BranchBug.java"],
  "main_class": "demo.BranchBug",
  "arguments": []
}
```

`bundle` resolves relative to the configuration file. Source paths resolve relative to the worker root. Application arguments support spaces, backslashes, single quotes, and empty strings. Embedded double quotes and CR/LF are rejected because the JDI launcher does not provide shell-style escaping for those values. All required `.java` files must be declared; arbitrary Maven/Gradle projects and external classpaths are not part of this slice. The startup configuration is trusted local input, not a request from an agent.

The current metadata reader accepts non-preview class files through Java 21; Java 17 is tested in CI. Use standard package-relative source layouts (source-root prefixes are supported). Arguments containing whitespace, single quotes and backslashes are supported; embedded double quotes, CR/LF and NUL are rejected because this launcher uses the JDI connector's quoted string contract.

```sh
export DEBUG_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
peeky-worker --root /absolute/path/to/java-sources --java-config "$PWD/work/java-target.json" --port 8765
```

Use the existing HTTP client in another process, with the same token:

```python
import os
from peeky.client import Client

client = Client("http://127.0.0.1:8765", os.environ["DEBUG_TOKEN"])
launch = client.call("launch", program="demo/BranchBug.java", breakpoints=[{"line": 10}])
session = {"session_id": launch["session_id"]}
stop = client.call("wait", **session, timeout=2)
while stop["state"] == "running":
    stop = client.call("wait", **session, timeout=2)
if stop["state"] == "stopped":
    snapshot = client.call("inspect", **session, stop_id=stop["stop_id"])
    print(snapshot)
client.call("close", **session)
```

Choose a real executable source line for the supplied program; line 10 above is illustrative. Breakpoints may start pending while the class loads. `wait` and `report` expose bounded breakpoint events, including the adapter's eventual verified location. Inspect returns the actual stopped source path/line, validated against prepared compiler metadata. Source paths in the stack point to the worker's fresh workspace.

## What is checked

The build manifest identifies declared original and copied source bytes, compiled artifacts, tool binaries/version probes, and the controlled environment. The Java session additionally identifies the adapter bundle, generated source map, main class and arguments. Launch, inspection, mutation, breakpoint changes and continuation check relevant identities before executing. Integrity failures invalidate the session and clean up owned execution and its workspace.

The runtime permits unconditional line breakpoints, stack/local inspection, supported numeric/boolean scalar assignment, and continuation. Arrays expose their type, length metadata and debugger reference; the HTTP API does not yet provide child expansion. Automatic Java `toString` and logical-structure evaluation are disabled. Sessions can step over, into and out. The launcher fixes the step filters, so `--into` and `--out` skip JDK classes; a step over at the end of a method that JDK code called stops outside the sources, where nothing is read. Arbitrary expression evaluation, conditional/log breakpoints and hot code replacement are not exposed.

These identities bind the known local build and launch inputs. They are not a cryptographic attestation of every class loaded into a hostile JVM or a sandbox boundary. The target, compiler and debugger execute as the worker user. Remote absolute-path rebinding, arbitrary class loaders and production Kubernetes support remain unimplemented.

## Test coverage

Run the real Java lane after setup:

```sh
PEEKY_JAVA_ADAPTER="$PWD/work/java-adapter" pytest tests/java
```

`tests/java/test_java_worker.py` uses real HTTP requests, a worker HTTP server, the real Java adapter and a separate JVM. The fixture includes a package, nested class, integer local, array local, process ID, and a worker-token presence check. It exercises runtime intervention, stale stop identity, required fresh inspection, fresh launch reset, tampered original/copied source and compiled class rejection, and adapter failure cleanup. Public breakpoint events and actual stopped locations establish that the desired breakpoint was reached.

The HTTP test worker runs in a background thread within the test process; debugger and target are separate owned processes. This establishes real transport and JVM behavior, not a remote-host deployment or different-root source synchronization claim. Synthetic fixtures contain no application secrets, and required tests make no model calls.
