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
- A `std::string` key matches only a string of its length. LLDB prints a libc++ string from its size, a NUL as `\0` (`SetBinaryZeroIsTerminator(false)` in LLDB's `LibCxx.cpp`), so its summary is the whole string; libstdc++'s summary is the `_M_dataplus._M_p` C string, which ends at the first NUL (`"a\0b"` shows as `"a"`). The worker says which a build has (`whole_string_summaries`, from `std::__1` and `std::__cxx11` in the debug information); for libstdc++ it checks every string against its `_M_string_length` (see Strings, #128), and a key matches only a string shown whole and of its length (`string_length`), or read from memory.
- Absence is claimed (`not found ... would insert it`) only where C++'s `m[K]` could find no other key: a `std::map` with `std::less` or `std::greater`, a `std::unordered_map` with `std::hash` and `std::equal_to`. With a comparison of the map's own (`ByAbs`, a case-insensitive `NoCase`, `LowerHash`/`LowerEq`), an exact key reads, and otherwise the error says no key equals K exactly and that the map may treat another as equal; bisection runs only under `std::less` and `std::greater`.
- `m[#N]` is the Nth entry in iteration order (a map's pair, with `.first` and `.second`, or a set's element). It is accepted only on these containers; elsewhere `[N]` indexes.
- Sets, multisets and multimaps (also unordered) have no `[]` in C++, so `s[N]` and `mm[N]` are refused, naming `s[#N]`.
- `session get m` lists a map's entries as `m[K]: V = value` when its keys compare (the selector reads what the row shows), and every other container's as `m[#N]`, with a note saying which. A key no selector can write (over six digits, a string that is not plain) is listed by position, and two entries never share a label. A pointer's entries are `p[0][K]` (#102), and `p[1]` is refused with `p[0][1]`, the value for key 1.
- The container is told by its type as LLDB shows it (`std::map<…>`, `std::__1::map<…>`, `std::__debug::map<…>`, `std::pmr::map<…>`, through `const` and `&`), or, for a typedef, by the class the build's debug information gives it (`typedef_kinds` records it; the worker adds `aliased_type`), also behind a reference (`const Index &`) or a pointer (`IntSet *`, `pointee_aliased_type`). A class's member typedef (`Holder<std::string>::S`, which lldb-dap and the debug information spell differently) keeps the class every instance's typedef names, or the map or set template they all instantiate (`std::map`); where they disagree and one is a map or set, the type is `?`, and `t[N]` is refused (`t[#N]` reads by position). A typedef the debug information does not resolve, whose children the debugger lists as pairs with a const key, may be a map or a vector of such pairs: there `t[N]` is refused and `t[#N]` reads by position.
- A `std::map` with integer keys and the standard order (`std::less`, which LLDB leaves out of the type's name, or `std::greater`) and more than 20 entries is bisected, reading about log2(size) entries; its size is the summary's `size=N`, checked against the listing. Any other map is read entry by entry, one node per entry and one per key, within the inspection's 512 nodes: a map too big for that is refused before any entry is read. A listing shorter than the summary's size never says a key is absent.

Known gaps: floating-point, `char` and other keys read only by position; a typedef the debug information does not resolve at all (no class for its name) is told from a vector only by its entries, so a set behind one reads `s[N]` by position as a vector would; `std::flat_map` and `std::flat_set` (C++23) are recognized by name but not qualified with a real build. Rust's `HashMap` and `BTreeMap` are not affected: Peeky does not load Rust's LLDB formatters, so LLDB shows their raw fields and `m[1]` finds no element.

## Strings (#128)

LLDB's summary of a libstdc++ `std::string` is the `_M_dataplus._M_p` C string (`LibStdcppStringSummaryProvider` sets `SetBinaryZeroIsTerminator(true)`), so `"a\0b"` showed as `"a"`, with no note, wherever the value appeared. A value LLDB names `std::string` gets the summary string `${var._M_dataplus._M_p}` instead, which fails on clang's Linux builds (it leaves `_Alloc_hider` incomplete, expecting libstdc++'s own debug information): `error: summary string parsing error` was shown as the value. LLDB prints a libc++ string from its size, every byte (a NUL as `\0`), so on macOS nothing changes.

For libstdc++ (`whole_string_summaries` false), the worker's `string_value` hook (`peeky.cxx_strings`) reads each `std::basic_string`'s members through the debugger's own reference (any depth; one value-view read, within the inspection's view budget) and compares the summary's length, counted as LLDB escapes it, with `_M_string_length`. A string is known by its type's name (`std::string`, `std::basic_string<char>`, `std::__cxx11::basic_string<char, …>`, by value, reference or pointer), by the class a typedef of it names in the build's debug information (`typedef std::basic_string<char> Bytes`, `using Name = std::string`, `const Name &`, `Bytes *`, a template's member typedef; see `aliased_type`, #119), and, under any other name, by its members: a value LLDB shows as a literal (or as a failed summary), other than C's own `char *`/`char[N]`, whose `_M_dataplus` is a `std::basic_string<C>::_Alloc_hider` (which names the character type) beside a `_M_string_length`. LLDB applies its libstdc++ formatters to a typedef's canonical class, so `Bytes b` showed `"b"` for `"b\0y"`, and `Name tn` the parsing error, with no note (review of #131):

- Equal (or a `char` summary cut at LLDB's 1024 bytes, marked `...`, with no NUL before): the debugger's text stays, marked `string_length` for map keys (#119).
- Otherwise, with `readMemory` (lldb-dap 20): the data pointer and length are read from the string's first two words (the length checked against `_M_string_length`), then up to 1024 characters (one 32-byte request for a string in its own 16-byte buffer, a second for one on the heap), shown as LLDB's libc++ summary shows them (`\0`, `\t`, `\"`, `\U0000001b` for a control character, `\xff` for a byte that starts no UTF-8 sequence, `"..."...` past the bound), with `view: memory`. `std::wstring` (`L"…"`, UTF-32), `std::u16string` (`u"…"`) and `std::u32string` (`U"…"`) too.
- Without it (lldb-dap 18): `"v" (shown up to a NUL at byte 1 of 3)`; a failed summary has no value, with its length in the reason (`0x… <no text: …>` after a pointer's or reference's address).
- No `_M_string_length` in view (clang leaves `std::wstring`'s members to libstdc++'s debug information; libstdc++'s old ABI): `(not checked for a NUL byte: …)`, or no value where the summary failed.

Every string literal is then spelled so that C reads it back as the same bytes (`tidy`): LLDB writes a NUL as `\0` and an invalid byte as `\xff` even before a digit, so the bytes n, NUL, `6`, `4` showed `"n\064"`, which C reads as `"n4"`, and `\xff` then `a` showed `"\xffa"`, one character to C. There, and only there, the three-digit octal form is used: `"n\00064"`, `"x\0005"`, `"\377a"`; everywhere else the text is LLDB's libc++ spelling, unchanged (`"a\0b"`, `"\08"`). This applies to LLDB's own libc++ text on macOS too: re-reading memory there would cost a request per string to fix a spelling, while LLDB's escapes are unambiguous to Peeky, which counts them as LLDB writes them (`\0` is always one NUL; LLDB never writes other octal escapes) and respells only the debugger's own text, once (a literal Peeky read from memory is spelled as C reads it already, and Rust's `\0` is never followed by more digits, so Rust literals stay as LLDB shows them). A literal is kept within the worker's 2000 characters (with its address and note), cut between characters with LLDB's `...`: a noted value was 5041 characters, and a long libc++ text was cut inside an escape with no mark.

LLDB's `error: summary string parsing error` (also `error: no target`, `error: no ScriptInterpreter`) is never shown as a value, for any C or C++ type, string or not, nor saved as one: a value with only that text is unavailable (`LLDB's summary of this value failed (...); select its members`), and after a pointer's address it reads `0x… <no text: …>`.

Cost on libstdc++: one `variables` request per string (its members), and, where the summary is cut or failed and the debugger reads memory, one or two `readMemory` requests. LLDB's text gives no sign of a NUL it dropped, so every string is checked; the requests count against the inspection's view budget (128 per inspection or expansion, 10 s), so a page of a large `std::vector<std::string>` can end with strings marked `(not checked for a NUL byte: …)` rather than vouched for. libc++ (macOS) costs nothing.

`--set` changes only integer locals, so strings are refused as before. Qualified by real builds in `tests/cpp/test_cpp_strings.py` with lldb-dap 18 and 20 (the Docker `cpp-llvm20` lane sets `PEEKY_EXPECT_READ_MEMORY`). Known gaps: with lldb-dap 18, a `std::string` value (and one under a typedef of that name) has no text at all (its summary fails), and an incomplete `std::wstring` has none with either; a string inside another value's own summary would not be rewritten (vectors, maps and structs show none); a `const char *` stays a C string, as C has it; `std::string_view` is not covered.
