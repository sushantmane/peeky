# The investigation loop (`peeky-agent`)

`peeky-agent` is an optional reference harness. A controller drives a Peeky worker through a
fixed set of actions, and a model, through Codex CLI, picks each one. The model's actions and the
controller's rules are in [DESIGN.md](DESIGN.md#the-agent-loop-peeky-agent). This page covers the
loop for C, C++, Java and Rust.

Every language follows the same flow:

1. Observe a live value at a stop.
2. Change it, to test a hypothesis, and see what the program prints.
3. Propose an edit to the source.
4. Accept the edit only when it passes an independent checker on a fresh build.

A change at runtime is evidence for a hypothesis. Only a source edit that passes the unchanged
checker counts as a repair.

## How a candidate is verified

- A worker builds and debugs an isolated copy of the declared inputs: a C or C++ program through
  lldb-dap, a Java program through java-debug, and one dependency-free Cargo binary for Rust
  ([RUST_GATE.md](RUST_GATE.md)).
- Each candidate is rebuilt in a fresh workspace. `NativeVerification` does it for C and C++, and
  `RustVerification` for Rust. Java compiles it into another fresh workspace, and runs fresh JVMs.
- Only the candidate source can change:
  - C: the candidate `.c` file. Declared companion headers stay frozen, and are part of the
    source identity checks.
  - Rust: the declared application `.rs` sources. `Cargo.toml`, `Cargo.lock` and the
    integration-test file are frozen inputs, part of every identity check.
- The original buggy source must fail the checker before the loop can claim a repair.
- A compile error in the candidate, or a failed check, goes back to the model for another decision.
  An integrity, source-binding or infrastructure failure is fatal.
- The report keeps the original source and checker unchanged, with C's header and Rust's test,
  manifest and lockfile. It records the candidate's bytes and hash, the build manifest, the
  executable's identity (Rust: the test executable's hash and the libtest summary), and the
  checker's completion.
- Temporary executables and their workspaces are removed. The evidence says
  `artifacts_retained: false`. Recorded commands identify the run that happened. They aren't
  promised to stay runnable afterwards.
- Java saves the candidate under its package-relative `.java` file name, in the investigation's
  output. Each candidate attempt writes its own build-evidence JSON file.

Regressions reject:

- finishing before any runtime evidence;
- a change, during checking, to the original or staged source, the original or staged header,
  the saved candidate, the checker, the descriptor, or the executable or class file.

Each tamper test writes an execution marker after its change. So an unrelated earlier setup
failure can't pass for a successful rejection.

## The checker contract

A checker is trusted Python code ([COOKBOOK 6.5](COOKBOOK.md#65-writing-a-checker)). Its
`sys.argv[1]` is the path of a temporary JSON descriptor:

| Field | C | Java |
| --- | --- | --- |
| `schema` | `1` | `1` |
| `language` | `"c"` | `"java"` |
| `command` | `[prepared_binary_path]` | an argument vector, such as `[java, "-cp", classes_directory, main_class]` |
| `arguments` | the baseline application arguments | the configured baseline application arguments |
| `environment` | the controlled environment for child runs | the same |
| identity | the source identity and the build evidence | the prepared source identity, the source manifest, the build manifest and the artifact manifest ID |

- Run each independent case as `[*command, *case_arguments]`, with `env=environment`.
- Assert your own expected outputs, then return normally.
- An exit code alone isn't enough: the trusted wrapper requires the checker's explicit completion.
- The descriptor and the compiled workspace are temporary.
- A passing checker proves only its own assertions.

## Rust's two gates

A Rust candidate passes only when both of these pass, on the same fresh build.

**1. The compiled Cargo test.**

- Preparation runs `cargo build --offline --locked --bin <name>`, then
  `cargo test --no-run --offline --locked --test <name>`, with the same controlled environment.
- The test executable comes from the hashed `target/debug/deps` directory: exactly one, matching
  the declared test name, with no unexpected artifacts.
- Verification runs that hashed binary directly, with `--test-threads=1`. Cargo never runs at
  verification time, so nothing can be rebuilt or substituted.
- Cargo's uplifted `target/debug/<bin>`, which the test runs through `CARGO_BIN_EXE_*`, is a hashed
  artifact. It must be byte-identical to the debugged executable, before and after the run.
- The test's output is limited to 16,000 bytes. Output past the retained capture is a failure,
  not a silent cut.
- The last non-empty line must be the harness's single `test result: ok.` summary. It needs at
  least one passing test, and no failed, ignored, measured or filtered test.
- The exit code must be zero, and the run must not time out.
- Anything else is a test failure. An earlier, forged summary can't pass this check.

**2. The independent checker.** The unchanged `examples/rust/verify_pagination.py` runs against the
prepared descriptor, with 12 page cases and 10 invalid-argument cases. It must complete through the
trusted wrapper. A passing Cargo test never replaces it.

How failures count:

- The baseline's test failure is recorded, and doesn't stop the investigation. The checker must
  fail on the original.
- A failed test assertion, or a failed checker case, fails verification and returns to the
  decision loop.
- A candidate's compile errors are retryable `build` results only when Cargo's JSON messages
  (`--message-format=json-diagnostic-short`) show every rustc `error` with a primary span in a
  declared application source.
- These are fatal:
  - span-less errors, such as linker or driver failures;
  - errors elsewhere, and malformed or possibly cut messages;
  - a test target's compile failure, or a toolchain or probe failure;
  - an unexpected artifact, or executable drift;
  - a change to the source, the test or the checker.

## The fixtures

Each language's fixture is a one-based pagination bug: page 1 starts at the second page. The fix
is `(page - 1) * page_size`, spelled in each language's way.

### C: `pagination_bug.c`

- `examples/c/pagination_bug.c` uses one-based page numbers, but calculates
  `start = page * page_size`.
- Its companion `pagination.h` defines the item multiplier and the largest fixture size.
- The independent `verify_pagination.py` checks 12 combinations of page number, page size and input
  length, against its own expected results.

### C++: `pagination_bug.cpp`

- `examples/cpp/pagination_bug.cpp` calculates the first item with `page * page_size`, on purpose.
- At its `OBSERVE` line, the first page by default has `start == 3`. Changing this local integer to
  `0` tests the hypothesis without changing the source or rebuilding the running executable.
- The checker starts fresh processes for 12 valid cases and 10 invalid argument cases. It parses
  numeric inputs with `strtol`, and bounds them before arithmetic.
- The fix is `(request.page - 1) * request.page_size`.

### Java: `PaginationBug.java`

- `examples/java/demo/PaginationBug.java` computes `start = page * pageSize`.
- A useful experiment pauses after that assignment, observes `start = 3`, changes it to `0`, and
  continues to the expected first-page output.
- The checker runs 12 independent page, size and length cases in fresh JVMs.
- The fix is `start = (page - 1) * pageSize`.

### Rust: `examples/rust`

- At the observation line: `start = 3`, `some = Some(3)`, `none = None`.
- The checker has 12 page cases and 10 invalid-argument cases.
- The fix is `(request.page - 1) * request.page_size`.

## Run the deterministic proof

Required CI injects the decisions: it never calls a model, and uses no model credentials. Each
test still runs the real debugging, build and checking path, with a real HTTP worker. Every
language goes through the same sequence:

| Step | C, C++ and Java | Rust |
| --- | --- | --- |
| 1 | Pause after the buggy assignment, and see `start = 3` | Pause at the observation line: `start = 3`, `some = Some(3)`, `none = None` |
| 2 | Change the live integer to `0`. Continuing prints `PAGE:[10, 20, 30]`, with no source change and no rebuild for the experiment | Change the live `i32` to `0`. Continuing prints `PAGE:[10, 20, 30]`, with no rebuild |
| 3 | Propose invalid syntax. The compile failure returns its diagnostics for another decision | Propose invalid Rust syntax. The compile error returns as a retryable `build` result |
| 4 | Propose `start = 0`. It fixes the first page, but independent later-page cases reject this overfit candidate | Propose `start = 0`. The Cargo test passes, because it checks only page 1. The independent checker rejects later pages, so the candidate fails |
| 5 | Propose the fix. A fresh build passes all 12 supplied cases | Propose the fix. The compiled test and every checker case pass on a fresh build |

**C** runs the actual `peeky-agent` entry point. Set the native tools that the worker tests use:

```sh
PEEKY_NATIVE_DAP="$(xcrun --find lldb-dap)" \
PEEKY_NATIVE_CC="$(xcrun --find clang)" \
PEEKY_NATIVE_DWARFDUMP="$(xcrun --find llvm-dwarfdump)" \
PEEKY_NATIVE_DSYMUTIL="$(xcrun --find dsymutil)" \
PEEKY_NATIVE_SYSROOT="$(xcrun --show-sdk-path)" \
pytest tests/native/test_native_agent.py
```

- On Linux, use the LLVM paths that the native CI lane sets, and leave out the macOS-only tools.
- The worker and the controller have separate copies of the source root, on the same host, with
  matching declared source and header bytes.

**C++** runs `tests/cpp/test_cpp_agent.py`, in the C++ lane ([TESTING.md](TESTING.md)). It uses
separate, identical controller and worker source directories on one host, and the authenticated
HTTP interface. Decision fixtures replace the model call. This shows path independence, not a
deployment on a separate host.

**Java** needs the adapter bundle:

```sh
python scripts/setup_java_adapter.py --output "$PWD/work/java-adapter"
PEEKY_JAVA_ADAPTER="$PWD/work/java-adapter" pytest tests/java/test_java_agent.py
```

**Rust** runs the real `peeky-agent --rust-config` entry point:

```sh
PEEKY_RUST_CARGO="$(rustup which --toolchain 1.96.0 cargo)" \
PEEKY_RUST_RUSTC="$(rustup which --toolchain 1.96.0 rustc)" \
PEEKY_RUST_DAP="$(xcrun --find lldb-dap)" \
PEEKY_RUST_DWARFDUMP=/usr/bin/dwarfdump \
PEEKY_RUST_LINKER=/usr/bin/clang \
PEEKY_RUST_DSYMUTIL=/usr/bin/dsymutil \
pytest tests/rust
```

- On Linux, use the LLVM 18 paths from the Rust CI lane, and leave out `PEEKY_RUST_DSYMUTIL`.
- A second regression changes the frozen test file mid-investigation. Verification then fails
  fatally, and accepts no fix.
- A local macOS run (Apple LLVM 21, Rust 1.96.0): the full investigation took 5.05 s, and the
  frozen-test drift regression 2.34 s. Linux timings, from the hosted lane, are in
  [TESTING.md](TESTING.md).

## Run with a live model

You need Codex CLI, installed and signed in, and a model that your account can use. Use a new
output directory for each run.

**C.** Prepare a native target config like the one in
[C and C++ 7.1](lang/c-cpp-cookbook.md#name-the-tools-in-a-config), with these declared files:

```json
{
  "sources": ["pagination_bug.c"],
  "headers": ["pagination.h"],
  "arguments": []
}
```

Add the trusted compiler, adapter and symbol-tool paths for your machine: the snippet shows only
the fixture's fields. Save it as `work/native-pagination.json`, then run:

```sh
peeky-agent \
  --root "$PWD/examples/c" \
  --native-config "$PWD/work/native-pagination.json" \
  --program pagination_bug.c \
  --check verify_pagination.py \
  --model YOUR_MODEL \
  --output "$PWD/work/native-agent-run"
```

**C++.** Save a C++ config ([WORKER.md](WORKER.md#configure-a-c-target)), then run
`peeky-agent --native-config config.json --program pagination_bug.cpp --check verify_pagination.py`.

**Java.** Create `work/java-agent-target.json`:

```json
{
  "bundle": "java-adapter",
  "sources": ["demo/PaginationBug.java"],
  "main_class": "demo.PaginationBug",
  "arguments": []
}
```

Then run:

```sh
peeky-agent \
  --root "$PWD/examples/java" \
  --java-config "$PWD/work/java-agent-target.json" \
  --program demo/PaginationBug.java \
  --check verify_pagination.py \
  --model YOUR_MODEL \
  --output "$PWD/work/java-agent-run"
```

For every language:

- Without `--url`, the command starts the configured worker locally.
- `--url`, with the worker's `DEBUG_TOKEN`, selects an existing worker. The declared sources'
  identities must match, and for C, the frozen headers' too.
- The candidate's build and the checker run on the controller's host. That host needs the declared
  inputs, and a suitable native or Java toolchain.
- A live run sends selected source and runtime evidence to the model, and uses model credits.
  Required CI uses injected decisions only.

## What the loop doesn't prove

- The runtime change supports a diagnosis. The candidate's checks support a repair claim limited to
  the supplied assertions. Build and check evidence is broader than a successful experiment on a
  live variable, but it still proves only those assertions.
- The checker, the compiled code, the compiler, the JVM and the debugger share the worker's OS
  permissions. This is trusted-code verification, not a sandbox against a malicious compiler or
  JVM, nor against an adversary with those permissions.
- Rust's test processes share OS permissions with the checker, and are trusted code.
- The regressions prove different source-root paths over real HTTP on one host. They don't prove a
  deployment on another host, or on Kubernetes.
- Out of scope:
  - arbitrary build systems and project builds;
  - external dependencies, and third-party classpaths;
  - attaching to production remotely;
  - Java hot code replacement;
  - generating a verifier automatically.
- No live-model Rust evaluation has been recorded.
