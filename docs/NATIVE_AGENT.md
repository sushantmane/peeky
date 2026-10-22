# Experimental C investigation loop

The C controller now uses the same observation, intervention and candidate-verification flow as Java. A fixed native worker builds and debugs an isolated C program through LLDB-DAP; `NativeVerification` independently rebuilds candidate source and runs a trusted supplied checker against fresh processes. Only the candidate `.c` source is editable in this slice. Declared companion headers remain frozen and are included in source identity checks.

The bundled `examples/c/pagination_bug.c` uses one-based page numbers but calculates `start = page * page_size`. Its companion `pagination.h` defines the item multiplier and maximum fixture size. The independent `verify_pagination.py` checks 12 combinations of page number, page size and input length using its own expected results.

## Required deterministic proof

Set the same native tool environment used by the worker tests, then run:

```sh
PEEKY_NATIVE_DAP="$(xcrun --find lldb-dap)" \
PEEKY_NATIVE_CC="$(xcrun --find clang)" \
PEEKY_NATIVE_DWARFDUMP="$(xcrun --find llvm-dwarfdump)" \
PEEKY_NATIVE_DSYMUTIL="$(xcrun --find dsymutil)" \
PEEKY_NATIVE_SYSROOT="$(xcrun --show-sdk-path)" \
pytest tests/native/test_native_agent.py
```

On Linux, use the LLVM paths configured by the native CI lane and omit macOS-only tools. The main regression invokes the actual `peeky-agent` CLI entry point with a deterministic decision provider and real HTTP worker. It does not spawn a model or consume model credentials. The worker and controller have separate source-root copies with matching declared source/header bytes on the same host.

The observed decision sequence is:

1. Pause after the buggy assignment and inspect `start = 3`.
2. Change the live integer to `0`; continuation prints `PAGE:[10, 20, 30]` without source modification or a rebuild for the intervention.
3. Propose invalid C syntax; compiler failure returns diagnostics for another decision.
4. Propose `start = 0`; independent later-page cases reject the overfit candidate.
5. Propose `(page - 1) * page_size`; a fresh build passes all 12 supplied cases.

The final report must retain unchanged original source, header and checker identities. It records the saved candidate source, candidate hash, build manifest, executable identity and checker completion. Temporary executables and their workspaces are removed; evidence declares `artifacts_retained: false`. Retained descriptor commands identify the run that happened and are not promised to remain executable afterward.

Negative tests reject finishing before runtime evidence, plus changes during verification to the original source, staged source, original header, staged header, saved candidate, checker, descriptor or executable. Each tamper test writes an execution marker after its mutation, preventing an unrelated earlier setup failure from masquerading as successful rejection. Integrity, source-binding and infrastructure failures are fatal; genuine candidate compilation errors and failed independent assertions can return to the decision loop.

## Optional live-model execution

Prepare a native target configuration like the one in [C and C++ 7.1](lang/c-cpp-cookbook.md#name-the-tools-in-a-config), changing its declared files to:

```json
{
  "sources": ["pagination_bug.c"],
  "headers": ["pagination.h"],
  "arguments": []
}
```

Add the trusted compiler/adapter/symbol-tool paths required for your machine; the snippet above shows only the fixture-specific fields. Save it as `work/native-pagination.json` and run with an installed, authenticated Codex CLI and a model available to your account:

```sh
peeky-agent \
  --root "$PWD/examples/c" \
  --native-config "$PWD/work/native-pagination.json" \
  --program pagination_bug.c \
  --check verify_pagination.py \
  --model YOUR_MODEL \
  --output "$PWD/work/native-agent-run"
```

Use a new output directory. Without `--url`, the command starts the configured worker locally. An existing worker can be selected by URL and its token; source and frozen-header manifests must match. The supplied checker executes on the controller host, which therefore needs the declared inputs and native toolchain. The regression proves different source-root paths over real HTTP on one host, not a remote-host or Kubernetes deployment.

Live model runs send selected code and runtime evidence to the model and consume model usage. Required CI uses injected decisions only.

## The C++ fixture

`examples/cpp/pagination_bug.cpp` calculates the first item with `page * page_size`, on purpose.
At its `OBSERVE` line, the first page by default has `start == 3`. Changing this local integer to
`0` tests the hypothesis without changing the source or rebuilding the running executable.

The deterministic agent test, `tests/cpp/test_cpp_agent.py`, then rejects a compile error and an
overfit `start = 0` patch. It accepts `(request.page - 1) * request.page_size` against an
independent checker.

- The checker starts fresh processes for 12 valid cases and 10 invalid argument cases.
- It parses numeric inputs with `strtol`, and bounds them before arithmetic.
- The test uses separate, identical controller and worker source directories on one host, and the
  authenticated HTTP interface.
- Decision fixtures replace the model call in required CI.
- This shows path independence, not a deployment on a separate host.

To run it with a model, save a C++ config ([WORKER.md](WORKER.md#configure-a-c-target)), then run
`peeky-agent --native-config config.json --program pagination_bug.cpp --check verify_pagination.py`.

## Checker contract and limits

The trusted Python checker receives a temporary JSON descriptor path as `sys.argv[1]`. For C, it has `schema: 1`, `language: "c"`, `command: [prepared_binary_path]`, separate baseline arguments, the controlled environment, source identity and build evidence. Execute each independent case using `[*command, *case_arguments]` with the descriptor's environment. Return normally after assertions; the verification wrapper requires explicit checker completion in addition to process status.

The runtime intervention supports a diagnosis; the independently compiled candidate's checks support a repair claim limited to those supplied assertions. The checker, compiled code and debugger share OS permissions and are not an adversarial execution sandbox. C++/Rust qualification, arbitrary build systems, external dependencies and production remote attachment remain separate work.
