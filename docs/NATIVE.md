# Experimental native C worker

The native slice runs a fixed C target through LLDB-DAP and the existing authenticated HTTP worker. It builds declared sources in a fresh workspace with debug information, checks source/binary/symbol provenance, verifies the loaded executable and stopped location, and permits qualified integer-local intervention. C++ and Rust are planned separate qualification steps using the same debugger family.

This slice does not yet provide native agent-directed candidate rebuilding and independent verification. A successful runtime intervention is not a permanent source fix. Attach, arbitrary expression evaluation, pointer mutation, memory writes, remote debug-server deployment and optimized builds are outside the current support boundary.

## Local macOS setup

Use the installed Xcode Command Line Tools. The executable may be available through `xcrun` even when it is absent from `PATH`:

```sh
xcrun --find lldb-dap
xcrun --find clang
xcrun --find llvm-dwarfdump
xcrun --find dsymutil
xcrun --show-sdk-path
```

A trusted configuration file can contain the resolved absolute paths:

```json
{
  "sources": ["branch_bug.c"],
  "headers": [],
  "arguments": [],
  "compiler": "/Library/Developer/CommandLineTools/usr/bin/clang",
  "adapter": "/Library/Developer/CommandLineTools/usr/bin/lldb-dap",
  "dwarfdump": "/Library/Developer/CommandLineTools/usr/bin/llvm-dwarfdump",
  "dsymutil": "/Library/Developer/CommandLineTools/usr/bin/dsymutil",
  "sysroot": "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
}
```

Use paths discovered on your machine; the paths above are the development machine's toolchain. The explicit SDK path matters: its plain Clang invocation could not find `stdio.h`, while the SDK-qualified invocation succeeded. On Linux, use the pinned distribution tools and omit `dsymutil` and `sysroot` unless required by the configured toolchain.

Save the configuration as `work/native-target.json`, set a random worker token, and launch against the bundled example:

```sh
export DEBUG_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
peeky-worker --root "$PWD/examples/c" --native-config "$PWD/work/native-target.json" --port 8765
```

The configuration is trusted startup input. HTTP requests choose only an already declared program and supported debugger operations; they cannot supply compiler commands, LLDB initialization commands, or arbitrary launch configuration. `--native-config` and `--java-config` are mutually exclusive.

## Inspect and intervene

The example has an integer `value`, a pointer to it, an integer array, and a branch. Use the line marked `FIRST` as the breakpoint. The existing `Client` API can launch `branch_bug.c`, wait, inspect, set `value` to `5000`, and continue. The resulting output is:

```text
RESULT=success value=5000 array=4 pointer=5000
```

The original source and compiled executable do not change during that runtime experiment. Pointer and array variables expose their type and debugger reference; the HTTP API does not yet expand children. Pointer assignment is rejected. Integer assignment is qualified separately from the LLDB backend's broader mutation capabilities.

Use the current `session_id` and `stop_id` for stopped operations, and a scope handle returned by the latest inspection. Worker scope handles are opaque and invalidate after another inspection, a stop transition, continuation or close. An LLDB raw reference reused for a different frame must not silently authorize an old worker handle.

LLDB may initially return a pending breakpoint; use verified breakpoint events and the actual stopped frame/location as evidence. The worker keeps those events bounded. Registers are not treated as local variables. LLDB init-file sourcing is explicitly disabled using both the LLVM 18 and newer extension keys. The native launch disables shell argument expansion and automatic variable summaries. This reduces hidden configuration and behavior; it is not hostile-code isolation.

## Integrity and lifecycle

The fresh-build manifest binds declared sources and headers, compile flags, tool identities and output files. macOS also retains and hashes the generated dSYM; Linux uses the executable's embedded DWARF for the initial configuration. Stop validation checks the prepared source location, and native module validation checks the expected loaded executable identity. Missing debug information and changed original/copied source, executable or symbols fail closed.

The target, compiler, debugger and symbol tools still execute with the user's OS permissions. System headers/libraries and dynamic-loader behavior are not fully hermetic. Do not interpret these checks as a hostile-process attestation or a general guarantee about every loaded library. The initial target is tiny unoptimized code, and symbol/debugger versions remain part of qualification.

## Tests and parallel CI

On macOS:

```sh
export PEEKY_NATIVE_DAP="$(xcrun --find lldb-dap)"
export PEEKY_NATIVE_CC="$(xcrun --find clang)"
export PEEKY_NATIVE_DWARFDUMP="$(xcrun --find llvm-dwarfdump)"
export PEEKY_NATIVE_DSYMUTIL="$(xcrun --find dsymutil)"
export PEEKY_NATIVE_SYSROOT="$(xcrun --show-sdk-path)"
pytest tests/native --durations=10
```

Without `PEEKY_NATIVE_DAP`, local native integration tests skip explicitly. The dedicated required CI lane sets it and uses Ubuntu 24.04 with `clang-18`, `lldb-18`, and `llvm-18` from the signed Ubuntu repositories. The LLVM major is pinned; distribution patch versions can change and are printed in each run. Dependencies are acquired explicitly rather than assuming a hosted image's incidental toolchain. The lane starts independently of Python, Java, package and quality lanes, has a four-minute cutoff, and reports acquisition and conformance elapsed times. Cold hosted timing must be measured before claiming the budget is achieved.

The real HTTP regressions cover scalar/pointer/array inspection, integer mutation and output, unchanged source/binary, stale scopes/stops, source/binary/symbol tampering, absent debug information, stopped-target cleanup and adapter-death cleanup. Argument tests preserve whitespace, empty values, wildcards, dollar signs and single quotes without shell expansion. A benign hostile-home `.lldbinit` marker is first exercised as a positive control, then confirmed not to run under the worker's initialization settings. No required test calls a model.

Primary references: [LLVM LLDB-DAP](https://lldb.llvm.org/use/lldbdap.html), [LLVM launch configuration](https://github.com/llvm/llvm-project/blob/main/lldb/tools/lldb-dap/README.md), [macOS debug symbols](https://lldb.llvm.org/use/symbols.html), [Ubuntu lldb-18 package](https://packages.ubuntu.com/noble/lldb-18).
