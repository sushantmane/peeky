# Experimental native C worker

The native slice runs a fixed C target through LLDB-DAP and the existing authenticated HTTP worker. It builds declared sources in a fresh workspace with debug information, checks source/binary/symbol provenance, verifies the loaded executable and stopped location, and permits qualified integer-local intervention. C++ and Rust are planned separate qualification steps using the same debugger family.

The [C investigation loop](NATIVE_AGENT.md) now provides candidate rebuilding and independent verification through the shared compiled-verification boundary. A successful runtime intervention is not a permanent source fix. Attach, arbitrary expression evaluation, pointer mutation, memory writes and remote debug-server deployment are outside the current support boundary.

**Optimized builds** (opt-in, #12/#13):
- **Enabling:** set `"optimization": "Og" | "O1" | "O2"` in the native target config; the default stays `O0`. The level is part of `compiler_options` and `target_identity`.
- **Missing values:** LLDB reports a local with no value at a line as text, such as `<error: variable not available>` or `<error: could not evaluate DW_OP_entry_value: ...>`. Peeky marks those locals `unavailable`: they get no value, an explanatory error quoting the debugger's reason, and a warning. Changing them is refused with `MUTATION_REJECTED`.
- **Stale values:** optimized code can also show stale values that look normal. For example, an inlined `-O2` function reported `b=2, t=8` where the live values were 0 and 3. Peeky cannot detect this, so prefer `O0` for debugging.
- **Optimized-build rules:** every probe result, every session snapshot, every `session get` row and `describe` carry `optimized` and a warning.
  - Changing any local is refused (`MUTATION_REJECTED`), because the program may keep using other copies of the value.
  - Rust enum views become opaque, because LLDB zero-fills enum pieces it cannot locate.
  - In unoptimized builds, a value the debugger can't read (for example, behind a NULL pointer) is described neutrally with the debugger's reason, not as "optimized out". Rust is the same: set `"opt_level": "1" | "2"` (default `"0"`). At `opt-level 2`, `start` read `1` where the program's value is `3`, with no unavailable marker.
- **Scope:** this applies to C, C++ and Rust (all LLDB). Other adapters never reinterpret value text.

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

Integer fields and array elements of a local change too (#105): `--set s.end=10`, `s.in.lo=-3`, `s.arr[2]=7`, `grid[1][0]=3`. The stopped (innermost) frame must hold the object itself: the root is an automatic or a parameter in the frame's memory (from the build's debug information; not a static or a register value), not withheld by the rules for locals that may hold leftovers (declared below the stop, an initializer still running, a jump past its declaration), in an unoptimized build. The path, read from the debug information rather than the debugger's type names (a typedef can hide a pointer, and a bitfield shows as its declared type), may select only fields of structs and classes and elements of fixed-size arrays: never through a pointer (`ps->end`, `this->count`), a reference (a C++ `&` local or member), a base class or an anonymous member, and never inside a union, a packed struct (a misaligned field), or a `std::` type. The target must be an integer of 16 to 64 bits that is not const, volatile or a bitfield (`char`, `bool`, enums and floats are refused), and the new value in its range. The write is lldb-dap's `setVariable` on the field; the frame's scopes are then listed again and the field read back, and only the value asked counts as a change. A caller's frame is never changed: a call it is making may hold a copy of the old value (`p.x + f()` loads `p.x` before calling `f`). Nor is the stopped frame anywhere but at a statement's first instruction, for a local or a field (#105 reviews): the stop must be on one of its function's own lines; that line must start a statement (`Statements.begins`: every bracket around it up to the function's body is a block of statements, so not a call's arguments, a `for` header, an initializer, `new T[n]{`, a compound literal, a class body or a GNU statement expression `({`; no directive or bracket-hiding macro between it and the previous token; and that token ends a statement); and the pc must be the lowest address of every line-table row the function has on the lines of the statements starting there (`Statements.statement_span`), since clang may emit a later line's code first. On the function's first line, the statements are those after its body's `{` (`Statements.opening_statements`), and the pc must be the lowest of their rows past the function's entry, so a return address in a one-line function or lambda never counts, nor the store of `long t =` / `p.x + g();`. A brace after something like parameters is a function's body only at file scope or in a class's, namespace's or `extern "C"` body (`REPEAT(k, 1) {` in a function is a block of it), and any `({` around the line refuses. No call in the statements may be inlined (`DW_TAG_inlined_subroutine` with its `DW_AT_call_line` there, or from another file): its code carries the callee's lines and runs before the statement's lowest row. A K&R-style definition is refused as one. Unknown counts as part-way. What a change makes true: the storage holds the new value and the program's later reads see it; a copy the program made on an earlier line keeps the old value, and the stop's own line may overwrite the change. A local LLDB names with its declaration (`p @ s1.c:38`, when shadowed) is selected so, an integer one too (`'n @ s1.c:15=9'`); two of one name declared on the same line are ambiguous and refused, by `get` too. An element of an array of any length is fetched alone (`start`/`count`, the worker's `expand` with `index`), at any step of the path.

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
