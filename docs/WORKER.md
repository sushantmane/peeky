# The HTTP worker

`peeky-worker` runs the debugger next to the program, and answers authenticated JSON requests over
HTTP. The `peeky` CLI starts and calls a worker for you. This page is for code that calls a worker
itself.

- To run a worker on another host, see [REMOTE.md](REMOTE.md).
- Every operation, with its inputs and results, is in [DESIGN.md](DESIGN.md#http-contract).

## What the C and C++ worker does

A native worker runs a fixed C or C++ target through lldb-dap:

- It builds the declared sources in a fresh workspace, with debug information
  ([what it checks](lang/c-cpp-cookbook.md#what-peeky-checks-about-a-build)).
- It checks the source's, the binary's and the symbols' identity, and verifies the loaded
  executable and each stopped location.
- It changes integers and texts, within Peeky's rules
  ([C and C++ 7.1](lang/c-cpp-cookbook.md#71-c-and-c)). Rust uses the same debugger family.
- The [investigation loop](NATIVE_AGENT.md) rebuilds a candidate fix and verifies it
  independently. A change at runtime is an experiment, not a fix to the source.

It doesn't attach to a running program, evaluate expressions, change a pointer, or deploy a remote
debug server. It writes memory only to set a text
([C and C++ 7.1](lang/c-cpp-cookbook.md#change-a-text)).

## Start a C or C++ worker

The worker takes the same config file as the CLI's `--config`
([C and C++ 7.1](lang/c-cpp-cookbook.md#name-the-tools-in-a-config)). Save it as
`work/native-target.json`, set a random worker token, and start the worker on the bundled example:

```sh
export DEBUG_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
peeky-worker --root "$PWD/examples/c" --native-config "$PWD/work/native-target.json" --port 8765
```

- The config is trusted startup input. HTTP requests choose only a program it already declares,
  and the debugger operations Peeky supports.
- A request can't supply a compiler command, an LLDB initialization command, or a launch
  configuration of its own.
- `--native-config` and `--java-config` are mutually exclusive.
- A config field that Peeky doesn't know is refused, and `sources` is required.

## Inspect and change a C value

`examples/c/branch_bug.c` has an integer `value`, a pointer to it, an integer array, and a branch.
With the `Client` API, launch `branch_bug.c` with a breakpoint on the line marked `FIRST`, wait,
inspect, set `value` to `5000`, and continue. The program prints:

```text
RESULT=success value=5000 array=4 pointer=5000
```

- The original source and the compiled executable don't change during that experiment.
- A pointer or an array shows its type and its debugger reference. `expand` lists its children
  ([below](#read-a-values-children-expand-and-preview)).
- Changing a pointer is refused.

## Use the current session, stop and handles

- Stopped operations take the current `session_id` and `stop_id`, and a scope handle from the
  latest inspection.
- Scope handles are opaque. Another inspection, a stop transition, `continue` or `close`
  invalidates them.
- LLDB may reuse a raw reference for a different frame. That never makes an old worker handle
  valid again.

## Read breakpoints and stops as evidence

- LLDB may first return a breakpoint as pending. Use the verified breakpoint events and the actual
  stopped frame and location as evidence. The worker keeps those events bounded.
- Registers aren't treated as local variables.
- The worker turns off LLDB's init files, shell expansion of the program's arguments, and LLDB's
  automatic variable summaries. That removes hidden configuration and behavior. It isn't isolation
  from hostile code.

## Configure a C++ target

A native target's config, passed with `--native-config`, sets `"language": "cpp"` for C++. Its
sources are `.cpp`, `.cc` or `.cxx` files, and it names an explicit C++ compiler. This is a trusted
config for the C++ fixture, with the worker root set to `examples/cpp`:

```json
{
  "language": "cpp",
  "sources": ["pagination_bug.cpp"],
  "headers": ["pagination.hpp"],
  "compiler": "/usr/bin/clang++-18",
  "adapter": "/usr/bin/lldb-dap-18",
  "dwarfdump": "/usr/bin/llvm-dwarfdump-18"
}
```

- Start the worker with `peeky-worker --native-config config.json`. Set `DEBUG_TOKEN` first, and
  pass the intended `--root`.
- On macOS, also set `dsymutil` and the SDK's `sysroot`. Take the compiler's and debugger's paths
  from `xcrun --find`.
- The compiler must be clang
  ([C and C++ 7.1](lang/c-cpp-cookbook.md#which-c-builds-peeky-supports)).
- A C++ target gets the same source and build identity checks as C, and the same temporary build
  lifecycle ([what Peeky checks](lang/c-cpp-cookbook.md#what-peeky-checks-about-a-build)).

## Read a value's children: `expand` and `preview`

Both operations are read-only.

- **`expand`** takes a variable's opaque `variablesReference` as `handle`, with the current session
  and stop. It lists up to `count` children, at most 64, from `start`. Instead of `start` and
  `count`, `index` reads one element of an array of any length.
- With `previews: true`, `expand` previews the values it lists.
- **`inspect`** takes `previews` too: `true` (the default), `false`, or a list of the roots to
  preview, up to 32 names.
- **`preview`** gives one value's preview by its `handle`, or nothing. With `pointee: true`, it
  previews what a pointer points at.
- A probe asks `inspect` to preview only the roots it captures (`previews: [names]`). A selector
  walks to a field without previews on its way, and `preview` previews the value it reaches.
- Collections are bounded to 512 entries, with limits on depth and on inspection nodes
  ([DESIGN.md](DESIGN.md#bounded-child-inspection)).
- Each expansion rechecks the source and build, the frame, the scope and the child's ancestry.
- Any expansion revokes the mutable scope handles. To change a scalar after one, inspect again.
- A fresh inspection invalidates earlier child handles too.

## What the worker adds to C and C++ values

| Field | On | Meaning |
| --- | --- | --- |
| `whole_string_summaries` | a stop | `true` when LLDB's summary of a `std::string` is all of it: libc++, known by `std::__1` in the debug information. libstdc++ (`std::__cxx11`) gets no such field |
| `string_length` | a variable | A libstdc++ string's length, checked against its `_M_string_length`. A map key matches only a string shown whole and of its length, or one read from memory |
| `view: memory` | a variable | Peeky read the string's text from memory |
| `text: true` | a variable | A standard string shown as its text, or as Peeky's reason it has none. It gets no handle, as Rust's text views get none |
| `aliased_type` | a variable | The class behind a typedef's name, such as a map under another name |
| `pointee_aliased_type` | a variable | The same, for what a pointer points at: `IntSet *` |
