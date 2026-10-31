# Peeky design

Status: Python, Java, C, C++ and Rust each have an experimental investigation loop, with explicit
limits. The [roadmap](ROADMAP.md) has where each language stands, what a language needs before
Peeky claims it, what is delivered and what comes next. This document specifies the MVP contract.

## Problem and outcome

Agents often edit source only to add print statements. They rebuild, restart, and infer runtime
behavior from logs. Peeky lets an agent inspect execution through a debugger, and run a small
causal experiment. The output is a reproducible diagnosis, a candidate patch, and the results of
an independent check.

- An intervention that improves behavior is evidence, not a permanent repair.
- A patch is accepted only after a fresh run of the configured verifier completes successfully.
  The original source's and verifier's hashes must also be unchanged.

## Boundaries

In scope:

- trusted programs, not hostile ones;
- one session per worker, and one controller;
- local launch, or attaching to a Java VM that waits for a debugger
  ([JAVA_ATTACH.md](JAVA_ATTACH.md));
- loopback HTTP, or SSH forwarding.

Who does what:

- The debugger handles each language's runtime semantics: debugpy for Python. DAP is the adapter
  boundary.
- The model selects experiments.
- Ordinary code enforces IDs, time limits, the order of operations, and verification
  requirements.

Out of scope:

- isolating hostile code;
- arbitrary production mutation;
- multi-user tenancy;
- general automatic edits across files;
- Kubernetes installation, and an outbound gateway;
- reverse debugging and replay;
- attaching to processes that weren't prepared for a debugger.

## Components

```mermaid
flowchart LR
  M[Model decision provider] <-->|source and evidence / decision| C[Investigation controller]
  C <-->|JSON HTTP| W[Single-session worker]
  W <-->|DAP| A[debugpy adapter]
  A <--> P[Python process]
  C --> V[Fresh verification process]
  V --> R[Evidence report and candidate diff]
```

The diagram shows Python. Java, C, C++ and Rust use their own adapters behind the same worker.

| Module | Responsibility | Must not own |
| --- | --- | --- |
| `agent` | Model decisions, investigation budget, experiment sequencing, candidate proposal | Debugger protocol parsing |
| `client` | HTTP request/response transport | Automatic mutation retries |
| `worker` | Session identity, state transitions, input validation, target access, operations | Model reasoning |
| `dap` | Framing, response matching, asynchronous events, child lifecycle | Hypothesis selection |
| `adapters` | Trusted debugger command, initialize/launch configuration, scope and capability normalization | Session identity or model decisions |
| `build` | Fresh copied workspaces, explicit trusted build recipes, source/artifact integrity manifests | Automatic language selection or proof of loaded-image identity |
| verification support | Trusted verifier completion and isolated fresh execution | Editing the verifier |

The table lists the core modules. The [module map](#module-map) lists every module, grouped by
area.

The model provider is Codex CLI. The controller takes an injected decision callable, which
deterministic tests replace. Extract a provider protocol when a second provider is added. Don't
build a registry before it has users.

## Module map

Each module in `src/peeky`, by what it owns.

**CLI and client**

- `cli`: the `peeky` command. It parses arguments, and runs probes, sessions, `--background` jobs
  and remote clients.
- `probe`: a stateless probe. One request launches, stops, captures, changes and reports.
- `client`: the HTTP SDK (`Client`), the check of a worker's `hello`, and the token a client reads.
- `agent`: `peeky-agent`, the model-driven investigation loop, through Codex CLI.
- `verification`: runs a trusted checker on a Python candidate, in a fresh process. It is not a
  sandbox.
- `__init__`: the version, and `--version`.

**Worker and transport**

- `worker`: `peeky-worker`. `DebugWorker` holds one session's state, checks input and runs every
  operation. `make_server` is the HTTP server.
- `dap`: the Debug Adapter Protocol transport. It frames messages, matches answers, queues events
  and owns the adapter's process.
- `sources`: the sources a target reads, with their hashes. A remote client and the worker compare
  them before a launch.
- `processes`: the process table, to end every process a session started. It also keeps the token
  out of each environment.

**Python**

- `adapters`: debugpy's launch and scopes (`PythonAdapter`), and a Python candidate's copy
  (`PythonCandidate`).
- `python_interpreter`: which interpreter runs a program, and a helper that reads its code with
  that interpreter.
- `python_scopes`: which module globals a stopped function reads, and the lines and scopes
  `describe` lists.
- `python_passes`: which locals a loop's pass hasn't assigned yet.

**Java**

- `java`: a standalone target (`JavaTarget`), its fresh build, and its session (`JavaSession`).
- `java_attach`: attaching to a JVM that waits for a debugger.
- `java_metadata`: a bounded reader of a class's debug metadata.
- `java_bytecode`: one decoder of a method's bytecode, for loop headers and a local's later reads.
- `java_verification`: Java's use of the shared fresh-build check.

**C and C++**

- `native`: a target (`NativeTarget`), symbol evidence from llvm-dwarfdump, and the LLDB session
  (`NativeSession`). Rust's session builds on it.
- `c_source`: where a declaration statement ends, from the source text alone.
- `native_previews`: previews of structs, classes and fixed-size arrays.
- `char_arrays`: a character array's text, up to its first NUL.
- `cxx_strings`: a libstdc++ `std::string`, shown as LLDB shows a libc++ one.
- `hashed_nodes`: a libstdc++ unordered container's entries, read from its own nodes.
- `assembler`: refuses inline assembly that can read a file, before a build.
- `native_verification`: C's and C++'s use of the shared fresh-build check.

**Rust**

- `rust`: a Cargo target (`RustTarget`), and its session (`RustSession`, built on
  `NativeSession`).
- `rust_manifest`: the strict input contract for a dependency-free crate.
- `rust_symbols`: symbol evidence from the crate's own compile units.
- `rust_mir`: which owned locals are initialized at a stop, from the compiler's MIR.
- `rust_ir`: which locals live in memory, from the compiler's LLVM IR.
- `rust_lexer`: where comments, strings and char literals are, for the checks that read source text.
- `rust_chars`: a generated table of the characters `{:?}` escapes. Regenerate it; don't edit it.

**Evidence store**

- `evidence`: the SQLite store of captured runs. `peeky sql` and `peeky queries` read it.
- `stored_values`: a stored value's elements and text, as SQL functions.

**Shared**

- `errors`: error codes, `classify`, and `PROTOCOL_VERSION`. It removes the token from an error's
  text.
- `build`: trusted build recipes, fresh copied workspaces, and integrity manifests.
- `compiled_verification`: a fresh compiled build, and a trusted checker's run, for Java, C, C++
  and Rust.
- `loop_headers`: whether a breakpoint on a loop header stops only before its loop, in C, C++, Rust
  and Java.
- `fields`: which field or element of a C, C++ or Rust local `--set` may change.
- `text_sets`: changing a string's text in place, in C, C++ and Rust.
- `names`: function names as people read them.
- `identifiers`: the names a selector can write.
- `textlines`: lines and quoted names, as tools and compilers write them.
- `digest`: a file's SHA-256, reused while the file is provably unchanged.

Where to start reading:

- Any language: `cli.main`, then `probe.run_probe` for a probe. A session's operations start at
  `DebugWorker.dispatch` in `worker`.
- Python: `adapters.PythonAdapter`.
- Java: `java.JavaTarget`, then `java.JavaSession`. For attach, `java_attach`.
- C and C++: `native.NativeTarget`, then `native.NativeSession`.
  [NATIVE_INTERNALS.md](NATIVE_INTERNALS.md) explains the symbol evidence.
- Rust: `rust.RustTarget` and `rust.RustSession`, then `rust_mir` for what a stop may show.

## Session model

```mermaid
stateDiagram-v2
  [*] --> Idle
  Idle --> Running: launch and configure
  Running --> Stopped: consume stopped event
  Stopped --> Running: continue / invalidate stop ID
  Running --> Terminated: consume termination
  Stopped --> Invalid: uncertain debugger failure
  Running --> Invalid: adapter failure
  Invalid --> Idle: close owned processes
  Stopped --> Idle: close
  Running --> Idle: close
  Terminated --> Idle: close
```

**Identities:**

- A new launch creates a random session ID, and a source fingerprint.
- Each consumed stop creates a new stop ID. Frame IDs are valid only at that stop.
- The worker issues opaque scope handles, and translates them to adapter references.
- Every new inspection revokes all earlier scope handles, even within the same stop. This stops a
  reused raw adapter scope ID from selecting the wrong frame.
- Inspection and mutation need the current session and stop. Reports need the session's identity.
- Continue invalidates all frame and scope references.

**Failures and timing:**

- A request timeout can mean that the operation already ran. Close or invalidate the session.
  Never retry a mutation transparently.
- A worker serializes operations. `wait` polls in short, bounded steps, which limits how long
  polling delays another request. Other adapter operations keep their own request deadlines.
- The CLI worker expires inactive sessions. A graceful disconnect is followed by ending the
  processes the session started, on supported POSIX hosts. An attached JVM is only detached
  ([ERRORS.md](ERRORS.md#how-peeky-ends-a-sessions-processes)).
- Lifecycle events are kept apart from output buffers, so noisy stdout can't push out a stop or
  termination event.

State changes are consumed as events: the controller calls `wait` to observe asynchronous
transitions. A future event stream may replace polling, without changing what the identities mean.

## HTTP contract

Requests are JSON objects with `Authorization: Bearer <token>`. Each also names its client's
protocol in `X-Peeky-Protocol: 13`. The worker refuses any request but `version` and `hello` that
names another protocol, or none, before it runs it ([ERRORS.md](ERRORS.md#protocol-version)).

- The worker binds only to `127.0.0.1`. It refuses a request whose `Host` isn't 127.0.0.1, or
  that has an `Origin` header: a web page's ([THREAT_MODEL.md](THREAT_MODEL.md)).
- The token is 32 to 512 characters.
- The token authorizes control of that worker. It is not a per-target policy.
- Request sizes are bounded: at most 4 MiB, enough for a remote client's source hashes.
- Each read of a request waits at most 10 s. When the request line or headers stall, the worker
  closes the connection, before any token check. A stalled body is an `INVALID_REQUEST`.
- Errors are JSON with a stable `code`, a `hint`, `retryable` and `session_closed`. Every response
  carries the protocol version. [ERRORS.md](ERRORS.md) lists the codes.
- The HTTP status and the code tell invalid requests apart from uncertain adapter failures.

`POST /version` returns the protocol version, the sources a remote client hashes, the operations
and the error codes:

```
$ curl -s -i -X POST -H 'Authorization: Bearer …' -H 'X-Peeky-Protocol: 13' -d '{}' \
    http://127.0.0.1:18766/version
HTTP/1.0 200 OK
...
X-Peeky-Protocol: 13
...
{"protocol": 13, "sources": {"kind": "python"}, "root": "/srv/app", "operations": ["breakpoints", "close", "continue", "expand", "hello", "inspect", "launch", "preview", "probe", "report", "set_variable", "shutdown", "step", "version", "wait"], "error_codes": ["BUDGET_EXHAUSTED", "BUILD_FAILED", "BUILD_REQUIRED", "BUSY", "DEBUGGER_ERROR", "INSPECT_REQUIRED", "INTERNAL_ERROR", "INVALID_BREAKPOINT", "INVALID_QUERY", "INVALID_REQUEST", "JOB_LOST", "MUTATION_REJECTED", "NOT_FOUND", "NO_SESSION", "NO_STOP", "PROGRAM_CRASHED", "PROVENANCE_CHANGED", "SESSION_ACTIVE", "STALE_HANDLE", "STALE_STOP", "TIMEOUT", "TRANSPORT_LOST", "UNAUTHORIZED", "UNREACHABLE", "UNSUPPORTED"]}
```

`version` is answered whatever `X-Peeky-Protocol` a request names, or none. Any other request
must name the worker's protocol: otherwise it is refused with `INVALID_REQUEST` before it runs.

Without the token, the reply is `401`:

```
{"error": {"code": "UNAUTHORIZED", "message": "Missing or wrong bearer token", "hint": "Set PEEKY_WORKER_TOKEN to the DEBUG_TOKEN the worker was started with.", "retryable": false, "session_closed": false}, "protocol": 13}
```

`POST /hello` needs no token. It takes `{"nonce": HEX}`, 32 random bytes from the client. The
answer is `{"protocol": 13, "proof": HEX}`: HMAC-SHA256, keyed by the token, of the label
`peeky worker hello v1` and a NUL byte, then the nonce. A client checks the proof before it sends
the token or any request ([THREAT_MODEL.md](THREAT_MODEL.md#assets-and-boundaries)).

| Operation | Required input | Result |
| --- | --- | --- |
| launch | worker-relative program, breakpoint list (at most 100); optional `args`, `entry` (Python's `--run` script) or `module`, which is `pytest` (its `--run-module`, protocol 13), `edit` (a candidate), `sets` and `entry_lines` (a probe's changes and restores), `max_pause`; remote clients: `expect_sha256` and `expect_inputs` | session ID, source hash, capabilities, verified locations; `inputs_checked` and `inputs_sha256`; `target_identity` (for Python, the interpreter that runs the program); for Java, `java` ([below](#the-launch-replys-java)) |
| wait | session ID, bounded timeout | stopped, terminated, or still running; at a `calls` step's own end, `call_change` (`left`, `deeper` or `other`) where its stack, read whole up to 256 frames, differs in function or depth; `macro_stop` (Rust) where the debugger shows the stop on a line with no code of its own in its function that is in a macro's definition, or may be one (Peeky can't read the source as rustc does; the text says which), why `inspect` reads none of that frame; where the function's DWARF blocks are known, only once the stop's address proves it; `crash` where a C or C++ program crashed (SIGABRT, SIGSEGV, SIGBUS, SIGILL or SIGFPE, from a fault or `abort`, that its signal masks show it neither handles nor ignores; not another signal, or a C++ throw or catch filter's stop): the debugger's `description`, `outside_program`, the nearest frame of the program's own code among the first 64 (`frame`, `function`, `location`), checked against the build as `inspect` checks a frame, and `uncaught_exception` where `__cxa_throw` called `std::terminate`, and `kind`, `exceptionInfo`'s `exceptionId` (`signal`, `exception`, or a filter's name, `cpp_throw`); `signal_stop` with the same fields at any other `exception` or `signal` stop; `inspect` reads nothing in a frame outside the program's sources at any `exception` or `signal` stop |
| inspect | session ID, stop ID; optional `frame` (index into `frames`, 0 the innermost; 10 frames are listed, or up to `frame`, at most 64) or `frame_id`; optional `names` (up to 32 names also listed past the 50-entry bound; a shadowed local's as LLDB names it, `s @ main.rs:81`); optional `globals` (Python: `true` lists the module's globals; a list of names lists them only if the function provably reads one of those names from them, with `globals_proof`); optional `previews` and `extents` (C and C++: `true`, `false` or up to 32 root names), and `depth` (`true` adds the stack's `depth`) | stack and bounded locals (`frame`, `innermost` for a caller, `globals_available`); Python values carry `identity`; `no_longer_visible`: after a `calls` step that stayed in the function at its depth, names listed at an earlier stop and not here, with `same_call` where the debugger's steps prove it (Java); not Python |
| expand | session ID, stop ID, opaque child handle, start/count, or `index`; or `key` (a map's key as the debugger shows one, for entries Peeky read from an unordered container's nodes); optional `previews` | bounded read-only children; for `key`, the entry with that key, if any, with the `key`, `compared` (how many keys) and `keys` (the first 20) |
| preview | session ID, stop ID, child handle; optional `pointee` | the value's preview, as `inspect` shows it, or none |
| set_variable | session ID, stop ID, name, a literal value, and a scope `reference` (a root local of the innermost frame) or an expansion `handle` (a field or element of one); optional `restorable` and `target`, for `@before` restores | before/after; `restore` where `restorable` |
| breakpoints | session ID, program, complete replacement breakpoint list | verified locations |
| continue | session ID, stop ID; optional `to_line` or `to_exit` | running, and `resumed`: false where another thread's breakpoint stop from the same all-threads stop is served next, and nothing was resumed |
| step | session ID, stop ID; `kind`: `over`, `in` or `out`; optional `calls` (the step's own end is compared with this stop) | running |
| report | session ID | bounded output and operation history |
| close | session ID | idle; owned process teardown |
| probe | one stateless request: `at`, `capture`, optional `set` and the probe's other options ([ADR 0004](adr/0004-stateless-probes.md)) | the probe's result |
| version | none | protocol version, the sources a client hashes, the worker's root, operations, error codes |
| hello | `nonce`; no token | `protocol` and `proof` only |
| shutdown | none | the worker closes its session and stops |

- Client-visible IDs mean nothing across sessions or stops.
- Check DAP capabilities before adding an optional operation.
- When a breakpoint's line is relocated, return the actual line to the agent. Never treat it
  silently as the requested line.

### The launch reply's `java`

A Java launch's reply has `java`, which says which `java` runs what. A probe's result and
`session start --json` repeat it. It is optional: a worker before it sends none, and a client
shows it only where it is there.

- `program`: the `java` the program runs on. A launched target has it; an attached one doesn't,
  since the JVM runs on the JDK that started it.
- `adapter`: the `java` the adapter runs on, the bundle's own.
- Each has `path`, `version` and `version_from`. `path` is a real path, with links followed.
- `version` is the JDK's `JAVA_VERSION`, from its `release` file (`version_from`: `release`).
  Where the `java` isn't a JDK's own `bin/java`, a launched target's is what the build's
  `java -version` printed (`version_from`: `-version`).
- `version` is `null` where neither says, such as an attached target's adapter on macOS's
  `/usr/bin/java`.

```json
"java": {"program": {"path": "/usr/lib/jvm/java-11-openjdk-arm64/bin/java", "version": "11.0.32.1", "version_from": "release"},
         "adapter": {"path": "/usr/lib/jvm/java-17-openjdk-arm64/bin/java", "version": "17.0.20.1", "version_from": "release"}}
```

## The agent loop (`peeky-agent`)

The model receives the numbered source, a plain-language issue, the baseline run's and checks'
failures, and earlier observations. It returns one structured action:

1. `observe(line)` launches a fresh process, and inspects a chosen stop.
2. `intervene(variable, value)` changes an observed local, removes breakpoints, resumes, and returns
   the runtime outcome.
3. `finish(old, new, reason)` proposes one exact replacement in the source.

The controller:

- rejects a `finish` before a runtime intervention;
- writes a separate candidate, and never overwrites the original silently;
- verifies the candidate in a fresh process. A failed check goes back to the model for another
  decision;
- bounds the total number of decisions, and each call's and run's deadlines;
- treats a transport failure as fatal to the active session;
- turns a model failure into a failed report, never a made-up diagnosis.

This vocabulary is small on purpose:

- The worker also supports bounded read-only expansion, and stepping. The agent's vocabulary
  selects neither yet.
- Multiple hypotheses remain a later addition, with their own contract tests.
- Conditional breakpoints, hit conditions and logpoints are refused for every language. They are
  expressions the debuggee would evaluate, and Peeky never runs agent-supplied code in the program.

## Verification and evidence

The configured verifier gets the candidate's path in `argv[1]`.

- It must fail on the original.
- It must return normally after checking the candidate. An explicit `sys.exit(0)` is rejected on
  purpose.
- A trusted wrapper requires explicit completion, instead of taking every zero exit as success.
  Catching a premature `SystemExit` prevents that exit-zero bypass.
- Hash checks gate acceptance. They are not only notes in the report.

This is not adversarially secure verification. The target and the verifier run with local OS
permissions. Malicious native code, or deliberate process tampering, is outside the trusted-code
contract. Strong isolation needs a separate environment and verifier service, later.

Evidence includes:

- source and check hashes;
- the actual breakpoint, and the observed local state;
- the requested intervention, and the returned value;
- the runtime output;
- the decision's reason, the proposed edit, and the checker's completion.

A claim that a bug is fixed in general is only as broad as the supplied verifier. Model claims must
never replace test output.

Reports may include application data. They stay local, unless the user sends them, or uses the
model runner, which sends selected evidence to the configured model. CI uses synthetic fixtures
only, and keeps test diagnostics in job logs.

## Remote development

The same worker runs on the target host, and the reasoning client stays outside
([REMOTE.md](REMOTE.md)).

- SSH forwards loopback traffic, and supplies transport confidentiality and authentication.
- The bearer token remains an additional worker credential.
- Source identity must match before line-based debugging.
- The root directory constrains which entry point is chosen, not what files the program can reach.
- Evaluating variable assignments, and even rendering objects, can run user code.
- Strip the worker's control credential from the environments of debugger and verification
  children. Keep the normal runtime dependencies that trusted programs need.

For Kubernetes, later:

- Add a per-workload worker with explicit deployment permissions, then an authenticated outbound
  relay.
- Define reconnect semantics, and what data may leave, before claiming secure production support.
- Native debugger access may need capabilities, or a shared process namespace. A sidecar alone
  doesn't make a process attachable.

## Delivery strategy

- Each roadmap slice includes a runnable example and tests.
- Required CI is deterministic and parallel: style, unit, real-debugpy integration on two Python
  versions, and an installed-wheel smoke test. Each language adds its own lane
  ([TESTING.md](TESTING.md)).
- Real-model evaluations are manual, and report cost and latency separately. They never block a
  PR.
- Wheel and source artifacts are built on manual delivery.
- Public distribution and cluster deployment need separate product decisions.

## Language expansion

Java, C, C++ and Rust runtime debugging, and fresh candidate verification, are connected to the
worker and controller. C++ adds qualified class, reference and array inspection, and candidate
verification. Rust has a worker for one dependency-free Cargo binary
([RUST_AGENT.md](RUST_AGENT.md#the-cargo-build)), and an investigation loop
([AGENT_LOOP.md](AGENT_LOOP.md#rusts-two-gates)).
[ROADMAP.md](ROADMAP.md#what-a-language-needs-before-peeky-claims-it) has the full acceptance criteria per language.

How the languages plug in:

- Launch and scope configuration live behind trusted startup adapter objects. HTTP clients can't
  supply adapter commands.
- `JavaTarget` prepares fresh copies of the inputs, compiles debug classes, and generates source
  mappings straight from class metadata.
- `JavaSession` checks the build, bundle and mapping identities before guarded operations. It
  validates selected stopped frames, and cleans its workspace after the debugger's teardown.
- The worker keeps ownership of sessions and stops, and exposes the debugger's later
  breakpoint-verification events.
- Java, C, C++ and Rust candidate acceptance use the shared `CompiledVerification` lifecycle, with
  thin language wrappers, fresh prepared builds and the unchanged trusted checker.

[Java 7.3](lang/java-cookbook.md#what-peeky-checks-about-a-java-build) has the restrictions, and the
limits of provenance for trusted development.

## Open questions

- How should compiled candidate verification extend the source-patch controller? (Answered for
  Java, C, C++ and Rust by `CompiledVerification`, above.)
- Should a future agent receive stable evidence IDs, beside opaque worker scope handles?
- What needs to stay inside customer infrastructure before remote production use?
- Which regression corpus gives evidence beyond synthetic examples?
- Is latency improved more by batching observations, or by keeping one model session?

## Bounded child inspection

Inspection returns opaque child handles in `variablesReference`, distinct from writable scope
references.

- Expansion revalidates the stopped frame, source and module. It resolves a unique scope, and the
  exact ancestry of names and types, afresh.
- Reinspection, mutation attempts, stop transitions, continuation and close revoke child handles.
- Expansion revokes earlier writable scope handles, because resolving children can change the
  backend's scope context. Inspect again before a mutation.

Limits:

- A page holds at most 64 values. Depth is at most four.
- An inspection has a 512-node budget, which includes the initial locals.
- A collection beyond 512 entries is rejected. Backend paging can't silently get around these
  limits. Two reads go past them, one entry at a time: `index`, an element of a longer array, and
  `key`, an entry Peeky read from a GCC build's unordered container. Such a container, read from
  its own nodes, may also be paged past 512.
- Values and identity strings are bounded.
- Child handles never authorize mutation.

These reads don't call arbitrary evaluate commands. But a trusted debugger's formatting may have
runtime side effects. The qualified fixture doesn't imply general support for C++ projects or
formatters.
