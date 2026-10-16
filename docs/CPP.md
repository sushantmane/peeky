# Experimental C++ conformance

Peeky's fixed native target accepts `language: "cpp"` and compiles `.cpp`, `.cc`, or `.cxx` sources with an explicit C++ compiler. The fixture qualifies C++17 builds with debug information, a class object, a reference to that object, `std::array<int, 128>`, and an ordinary local integer. It uses the same source/build identity guards and temporary build lifecycle as C.

`examples/cpp/pagination_bug.cpp` intentionally calculates the first item with `page * page_size`. At the `OBSERVE` line, the default first page has `start == 3`. Changing this local integer to `0` tests the pagination hypothesis without changing the source or rebuilding the running executable. The deterministic agent test then rejects a compilation error and an overfit `start = 0` patch before accepting `(request.page - 1) * request.page_size` against an independent checker. That checker starts fresh processes for 12 valid cases and 10 invalid argument cases. Numeric inputs are parsed with `strtol` and bounded before arithmetic.

The agent test uses separate identical controller/worker source directories on the same host and the authenticated HTTP interface. Decision fixtures replace the model call in required CI. This demonstrates path independence, not separate-host deployment.

A trusted configuration for the fixture, with the worker root set to `examples/cpp`, is:

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

Pass the saved configuration to `peeky-worker --native-config config.json` or `peeky-agent --native-config config.json --program pagination_bug.cpp --check verify_pagination.py`. Supply the existing `DEBUG_TOKEN` requirement and the intended `--root`. On macOS also configure `dsymutil` and the SDK `sysroot`, with compiler/debugger paths returned by `xcrun --find`.

Local conformance runs use:

```sh
PEEKY_NATIVE_DAP="$(xcrun --find lldb-dap)" \
PEEKY_NATIVE_CXX="$(xcrun --find clang++)" \
PEEKY_NATIVE_DWARFDUMP="$(xcrun --find llvm-dwarfdump)" \
PEEKY_NATIVE_DSYMUTIL="$(xcrun --find dsymutil)" \
PEEKY_NATIVE_SYSROOT="$(xcrun --show-sdk-path)" \
pytest tests/cpp --durations=10
```

The independent Ubuntu 24.04 CI lane acquires LLVM 18 from signed Ubuntu package repositories, records installed package revisions, and has a four-minute timeout. It has no dependency on the C or Java lanes. The budget is a target; measured hosted results determine whether it is met.

This slice does not qualify arbitrary STL visualizers, overloaded operators, expression evaluation, pointer/reference reassignment, class mutation, or C++ build systems. Optimized builds are opt-in, with locals reported as `unavailable` when they have no value (see NATIVE.md). Mutations remain restricted to supported integer locals. Debugger display types and standard-library storage member names differ between platforms; conformance must check actual values without assuming libc++ and libstdc++ have identical layouts.

Read-only `expand` accepts a variable's opaque `variablesReference` as `handle`, together with the current session/stop, `start`, and `count` (at most 64). Collections are bounded internally to 512 entries, with depth and inspection-node limits. Expansion revalidates the source/build, frame, scope, and child ancestry. Any expansion revokes mutable scope handles; inspect again before mutating a scalar. A fresh inspection invalidates earlier child handles as well.

On the locally tested Apple LLDB 21, both the `Pagination` object and `Pagination &` reference expand to `page`, `page_size`, and `total`. The `std::array` exposes a raw `__elems_` storage child, which expands to integer elements. The test accepts the corresponding single raw storage member on other standard libraries or direct indexed elements, while requiring the first three values to be 10, 20, and 30. Hosted LLVM 18 qualification remains a separate CI result rather than an assumption about its display shape.
