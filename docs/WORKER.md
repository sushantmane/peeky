# The HTTP worker

`peeky-worker` runs the debugger next to the program, and answers authenticated JSON requests over
HTTP. The `peeky` CLI starts and calls a worker for you. This page is for code that calls a worker
itself.

- To run a worker on another host, see [REMOTE.md](REMOTE.md).
- Every operation, with its inputs and results, is in [DESIGN.md](DESIGN.md#http-contract).

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
  lifecycle ([NATIVE.md](NATIVE.md)).

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
