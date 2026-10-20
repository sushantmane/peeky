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

## Maps and sets (#119)

LLDB lists the entries of `std::map`, `std::unordered_map`, `std::set`, `std::multimap` and the other associative containers as `[0]`, `[1]`, ... in iteration order, in libc++ (macOS) and libstdc++ (Linux) alike, with only the entry's type (`std::pair<const int, int>`) to tell them from a vector's elements. In C++, `m[1]` on a map is the value for key 1, so reading the debugger's `[1]` showed a wrong value with no note: for `std::map<int, int> m{{2, 20}, {1, 10}, {5, 50}}`, `m[1].second` read `20`. Selectors on these containers now never take a position for a key:

- `m[K]` on a map with unique keys (`std::map`, `std::unordered_map`) is the value for key K, as C++'s `m[K]` is: the `second` of the one entry whose `first` the debugger shows as exactly K. Integer keys (`short` to `long long`, signed or unsigned, under their LLDB or GCC names) match the selector's integer, which must be in the key type's range; `std::string` keys match a quoted selector (`m["al"]` or `m['al']`) of printable ASCII without `"` or `\`, against the debugger's `"al"`. A missing key is `not found`, with the keys there are (in C++, `m[K]` would insert it). Keys of any other type (floating point, `char`, `bool`, enums, classes, `const char *`, which C++ compares by address) and selectors of the other kind are refused, with how to select the entry instead.
- A `std::string` key matches only a string of its length. LLDB prints a libc++ string from its size, a NUL as `\0` (`SetBinaryZeroIsTerminator(false)` in LLDB's `LibCxx.cpp`), so its summary is the whole string; libstdc++'s summary is the `_M_dataplus._M_p` C string, which ends at the first NUL (`"a\0b"` shows as `"a"`). The worker says which a build has (`whole_string_summaries`, from `std::__1` and `std::__cxx11` in the debug information); otherwise a key whose summary matches must also have a `_M_string_length` of its length, and is refused where that cannot be read (at the inspection depth limit).
- Absence is claimed (`not found ... would insert it`) only where C++'s `m[K]` could find no other key: a `std::map` with `std::less` or `std::greater`, a `std::unordered_map` with `std::hash` and `std::equal_to`. With a comparison of the map's own (`ByAbs`, a case-insensitive `NoCase`, `LowerHash`/`LowerEq`), an exact key reads, and otherwise the error says no key equals K exactly and that the map may treat another as equal; bisection runs only under `std::less` and `std::greater`.
- `m[#N]` is the Nth entry in iteration order (a map's pair, with `.first` and `.second`, or a set's element). It is accepted only on these containers; elsewhere `[N]` indexes.
- Sets, multisets and multimaps (also unordered) have no `[]` in C++, so `s[N]` and `mm[N]` are refused, naming `s[#N]`.
- `session get m` lists a map's entries as `m[K]: V = value` when its keys compare (the selector reads what the row shows), and every other container's as `m[#N]`, with a note saying which. A key no selector can write (over six digits, a string that is not plain) is listed by position, and two entries never share a label. A pointer's entries are `p[0][K]` (#102), and `p[1]` is refused with `p[0][1]`, the value for key 1.
- The container is told by its type as LLDB shows it (`std::map<…>`, `std::__1::map<…>`, `std::__debug::map<…>`, `std::pmr::map<…>`, through `const` and `&`), or, for a typedef, by the class the build's debug information gives it (`typedef_kinds` records it; the worker adds `aliased_type`), also behind a reference (`const Index &`) or a pointer (`IntSet *`, `pointee_aliased_type`). A class's member typedef (`Holder<std::string>::S`, which lldb-dap and the debug information spell differently) keeps the class every instance's typedef names, or the map or set template they all instantiate (`std::map`); where they disagree and one is a map or set, the type is `?`, and `t[N]` is refused (`t[#N]` reads by position). A typedef the debug information does not resolve, whose children the debugger lists as pairs with a const key, may be a map or a vector of such pairs: there `t[N]` is refused and `t[#N]` reads by position.
- A `std::map` with integer keys and the standard order (`std::less`, which LLDB leaves out of the type's name, or `std::greater`) and more than 20 entries is bisected, reading about log2(size) entries; its size is the summary's `size=N`, checked against the listing. Any other map is read entry by entry, one node per entry and one per key, within the inspection's 512 nodes: a map too big for that is refused before any entry is read. A listing shorter than the summary's size never says a key is absent.

Known gaps: floating-point, `char` and other keys read only by position; a typedef the debug information does not resolve at all (no class for its name) is told from a vector only by its entries, so a set behind one reads `s[N]` by position as a vector would; a libstdc++ string key at the inspection depth limit is refused; LLDB's own display of a libstdc++ string with a NUL (`session get sso[#0].first` shows `"a"` for `"a\0b"`) is unchanged, outside map keys; `std::flat_map` and `std::flat_set` (C++23) are recognized by name but not qualified with a real build. Rust's `HashMap` and `BTreeMap` are not affected: Peeky does not load Rust's LLDB formatters, so LLDB shows their raw fields and `m[1]` finds no element.
