# How Peeky reads C and C++ values

This page is for maintainers. It explains how Peeky's C and C++ rules work, and which tests check
them. The rules themselves are in [c-cpp.md](lang/c-cpp.md) and the
[C and C++ cookbook](lang/c-cpp-cookbook.md). The worker's fields are in [WORKER.md](WORKER.md).

## Building and checking a build

- Peeky reads files a compile used outside the workspace from its `-MD` dependency file, by
  clang's and GCC's escaping. An entry that could be read two ways fails the build's preparation.
  Such an entry has:
  - a backslash or a tab in a name;
  - an undoubled `$`;
  - a name with a newline, or an empty step (`//`);
  - or a header that clang may have printed with `/` for a backslash.
- The tools' escapes, dwarfdump's and clang's octal, are read back. Their output's lines end at
  `\n` alone, so a source's name may hold any character.
- The worker turns off LLDB's init files with both the LLVM 18 key and the newer one.
- The first target was tiny, unoptimized code. Symbol tool and debugger versions remain part of
  what is qualified.

## Building C++

- Peeky passes clang `--driver-mode=g++`. The resolved `clang++` is often `clang` itself, which
  picks its language from the name it was run by.
- Debugger display types, and the standard library's member names, differ between platforms.
  Tests check the values, and never assume that libc++ and libstdc++ share a layout.

## Changing a value

- Peeky changes integers only, a narrower set than LLDB's backend can change.
- The write is lldb-dap's `setVariable` on the field. Then the frame's scopes are listed again and
  the field is read back. Only the value asked for counts as a change.
- `Statements.begins` decides whether a line starts a statement.
- `Statements.statement_span` gives the lines of the statements that start there. The stopped
  instruction's address must be the lowest of every line-table row the function has on them.
- `Statements.opening_statements` gives the statements after a body's `{` on a function's first
  line.
- `_prologue_windows` gives where each function's prologue runs: from its entry to its first row
  marked `prologue_end`. A GCC build marks none, so its window ends at the function's second row,
  as GDB reads it. A stop in a window withholds the frame's parameters and locals, and refuses
  changes. These are kept: a `static`, one that a location list places, and a parameter at or
  above GCC's frame base (`DW_OP_call_frame_cfa`), which the caller passed on the stack.
- Without the load address (a module with no `addressRange`), no stop is placed by its address.
  Then a frame on a line of any window withholds the same way, marked or not.
- An inlined call shows in the debug information as a `DW_TAG_inlined_subroutine` with its
  `DW_AT_call_line` on the statement's lines, or from another file.

## Preview bounds and types

- `native_previews` sets the bounds: 8 members or elements per level, two levels, 40 characters
  per member, 240 in all.
- Each level is one `variables` request. It counts against a preview budget of its own, per
  inspection or expansion: `PREVIEW_FETCHES` (256) within `PREVIEW_SECONDS` (3 s). So previews
  never take the value-view budget that libstdc++ strings use.
- A preview's type is the local's declared type (`local_types`, as `--set` reads it), walked along
  the selector's path in `fields.type_graph`. The graph keeps a record's base classes and a
  bitfield's width. It also keeps what a pointer or reference points at, one level down, as a
  graph of its own merged in. A change never walks through a pointer or reference.

## Maps and sets

- LLDB lists the entries of every associative container as `[0]`, `[1]`, … in iteration order, in
  libc++ and libstdc++ alike. Only the entry's type, such as `std::pair<const int, int>`, tells
  them from a vector's elements.
- Reading LLDB's `[1]` as key 1 would give a wrong value with no note. For
  `std::map<int, int> m{{2, 20}, {1, 10}, {5, 50}}`, `m[1].second` would read `20`. So a selector
  on these containers never takes a position for a key.
- `typedef_kinds` records the class that the debug information gives a typedef.
- A class's member typedef, such as `Holder<std::string>::S`, is spelled differently by lldb-dap
  and by the debug information. Peeky keeps the class that every instance's typedef names, or the
  map or set template that they all instantiate (`std::map`).
- `std::less` is the default order, and LLDB leaves it out of the type's name.

## Strings in libstdc++

- LLDB's libstdc++ summary is the `_M_dataplus._M_p` C string. Its
  `LibStdcppStringSummaryProvider` sets `SetBinaryZeroIsTerminator(true)`, so `"a\0b"` reads `"a"`.
  For libc++, `SetBinaryZeroIsTerminator(false)` in LLDB's `LibCxx.cpp` prints every byte, from
  the size.
- For a value LLDB names `std::string`, LLDB uses the summary string `${var._M_dataplus._M_p}`
  instead. On clang's Linux builds it fails, because clang leaves `_Alloc_hider` incomplete and
  expects libstdc++'s own debug information.
- LLDB applies its libstdc++ formatters to a typedef's canonical class. So without Peeky's check,
  `Bytes b` would read `"b"` for `"b\0y"`, and `Name tn` would read the parsing error, with no
  note.
- The worker's `string_value` hook, `peeky.cxx_strings`, reads each `std::basic_string`'s members
  through the debugger's own reference, at any depth. That is one value-view read, within the
  inspection's view budget. It compares the summary's length, counted as LLDB escapes it, with
  `_M_string_length`.
- Under any other name, a string is known by its members. It is a value LLDB shows as a literal,
  or as a failed summary, other than C's `char *` or `char[N]`. Its `_M_dataplus` is a
  `std::basic_string<C>::_Alloc_hider`, which names the character type, beside a
  `_M_string_length`.
- With `readMemory`, the hook reads the data pointer and the length from the string's first two
  words, and checks the length against `_M_string_length`. The text takes one 32-byte request for
  a string in its own 16-byte buffer, and a second for one on the heap.
- A literal is kept within the worker's 2000 characters, and cut between characters, for two
  reasons. A value with a note once ran to 5041 characters. A long libc++ text was cut inside an
  escape, with no mark.
- `tidy` respells only the debugger's own text, and only once. LLDB's escapes are unambiguous:
  `\0` is always one NUL, and LLDB writes no other octal escape. Reading memory again on macOS
  would cost a request per string, just to fix a spelling.
- A literal read from memory is already spelled as C reads it. Rust's `\0` is never followed by
  more digits, so Rust literals stay as LLDB shows them.
- `NativeSession.string_value` marks a standard string `text` when the value shown is its text, or
  Peeky's reason it has none. It does this by the type's name, or a typedef's class, in libc++ and
  libstdc++, and by the members in libstdc++.
- Map keys need no member. The worker's `string_length`, or `view: memory`, carries the length
  that `_string_bytes` read from `_M_string_length`.

## Locals reached through a pointer

clang gives a structured binding of a reference no storage of its own. It describes each name as a
variable of the member's type, at a location read through the hidden reference. For
`auto &[at, dropped] = *owner;`, that is `DW_OP_fbreg -16, DW_OP_deref, DW_OP_plus_uconst 0x18`.

- Its storage class says it is the frame's own (`automatic`). A change would then write the
  caller's `o.dropped`, through the reference. So `fields.local_types` stores a variable whose
  location reads through a pointer as `through`.
- clang types `auto &[items, dropped] = *owner` as the vector itself, at a location read through
  the hidden reference (`DW_OP_fbreg -24, DW_OP_deref`).
- `fields.referred_locals` counts any local whose location is not one frame offset as reached
  through a pointer. That is a structured binding of a reference, a class passed by value through
  a hidden pointer, and a named return value.

## Function names

LLDB's demangled C++ signatures run to about 100 characters, which made a probe table about 200
columns wide.

- `names.short_function` gives a table's short name.
- `function_labels` gives a table's column. That is the short name, or, for frames that would read
  alike, the signature as `display_function` shows it, else the whole name.
- `display_function` writes session text and frame listings.
- `names.shortened_note` writes the note of a table that shortens names. Its example is the SQL
  function `short_function(function)`'s value.
- `captures.instance` tells a Rust function's closures and a generic's instances apart.

## Block spans

clang's lexical blocks are right, but their code isn't contiguous.

- The landing pads that destroy a block's objects, when an exception leaves it, lie inside the
  block's `DW_AT_ranges`. They carry the function's closing line. LLDB places that line's
  breakpoint locations there, among the function's own.
- A span from a block's lowest line to its highest was too wide. In a function ending at line 35,
  a `while` body's local read `colon (size_t, lines 17-35)`, and an `if` block's
  `name (lines 22-35)`.
- `_block_spans` splits a block's lines only where a line between two of them runs code, none of
  which is the block's (`_runs`).

## Previews and containers

- lldb-dap 18 names an anonymous member `<null>`. lldb-dap 20 and later name it `""`.
- The lldb-dap builds tested, 18, 20 and Xcode's, list no vtable pointer.
- `fields.type_graph` records the template arguments (`DW_TAG_template_type_parameter`: `_Tp`,
  `_Key`) of each supported preview container, and walks them. Those are the containers
  `fields.CXX_CONTAINER` names: `std::vector`, `std::array`, `std::deque`, `std::list`, `std::map`,
  `std::unordered_map`, `std::set`, `std::multiset`, `std::unordered_set` and
  `std::unordered_multiset`. It leaves out `std::multimap`, `std::forward_list`, the adaptors,
  `std::pair` and `std::tuple`. A container's storage is behind pointers, so a plain walk never
  reaches its elements.
- The formatters of libc++ and libstdc++, in every lldb-dap tested, read a container's children
  from its own storage.
- **Threads:** the program runs one thread only where every source agrees.
  - The sources are two `threads` replies, LLDB's `thread list`, and every `thread` event that
    said a thread started since launch.
  - lldb-dap answers its first `threads` request with the threads the program had at launch.
  - Peeky asks `thread list` through lldb-dap's command escape.
  - Two started threads mean several threads, for good.
  - The thread IDs of all of them together must be one. A source that doesn't answer, or a thread
    without an ID, leaves it unsure, and unsure withholds.
- A row's withholding check runs before anything else about it, a name or type too long to show
  whole included.
- Rows read through a container are withheld for a reason. `main`'s `o.items[1].name` would read
  the freed string of an element that `clear()` had destroyed (`"\U00000003\0\0…"`), with no note.
  `items[1].name` would read it too, through the reference parameter of a function that the
  destructor calls. `shared[0].x` would read a moved-from `-1` while a worker thread relocated it.

## What LLDB misreads in GCC's C++

C++ built by GCC is still refused. A test-only hatch, `PEEKY_TEST_CPP_GCC=1`, builds it for the
tests (`tests/cpp_gcc_survey.py`). These rules hold for any build, and clang's builds pass them.

- **Unordered containers.** LLDB's libstdc++ formatter (`gnu_libstdcpp.py`) takes an unordered
  container's element type from its allocator's first template argument. GCC gives
  `std::allocator<T>` none, so the formatter counts the container as empty and lists nothing.
  LLDB 18's formatter reads a typedef's own type, which has no template arguments, so clang's
  `using Ids = std::unordered_set<long>` reads empty too.
  - `NativeSession.unlisted_container` compares the formatter's `size=N` with the container's own
    `_M_h._M_element_count`. The worker reads that with an `evaluate` in the `hover` context.
    lldb-dap reads a hover as a variable path, as `frame variable` does, past any formatter. It
    never runs a hover as an expression in the program.
  - The path comes from the debugger's names on the row's way from its root local: `h.us`,
    `vs[1]`, `mus[0].second`, `p->_M_h…`. An element of what a pointer points at is `pv[0][1]`.
    A name no path holds leaves the count unread: a located name, a base class, an anonymous
    member.
  - In an optimized build, a function that declares the root's name twice leaves it unread too.
    An inner block's local of that name may be in scope there, but not listed.
  - Where the counts differ, or the count is unread, the row is withheld as `not listed`, with no
    handle. A preview shows `<not listed>` in its place. The probe refuses its keys and
    positions: `um[1] not read: um is not listed: …`.
  - A right count doesn't prove the entries right. The provider decodes each node's entry as
    its allocator's first template argument, `skip_size` bytes into the node: the size of
    `_M_nxt`.
  - So a non-empty one is listed only where `unordered_proof` holds. The type the provider takes
    must be the hashtable's own `_Value`. And the entry must sit where the provider reads.
  - The entry follows `_Hash_node_base`, and the provider skips the size of `_M_nxt`'s type. So
    `_M_nxt` must be a pointer at offset 0 of `_Hash_node_base`, and `_Hash_node_base` exactly
    that pointer's size. `fields.type_graph` keeps a pointer's `DW_AT_byte_size`, which GCC
    gives; else it is 8 bytes, a 64-bit target's. `_Value`'s alignment must divide that size.
  - `WeirdAlloc<double, int>` (`value_type` `int`) would read `7` as a `double`. A `long double`,
    16-byte aligned, sits 16 bytes in: clang's read `size=1 {0}`.
  - Which argument is the allocator depends on LLDB's version, which `lldb_major` reads once
    with LLDB's `version` command. LLDB 18 and 19 take the 5th of a map and the 4th of a set. A
    set is any value whose type's name holds `set`, so `std::unordered_map<int, Asset>` decodes
    wrongly there. LLDB 20 to 22 take the last argument.
  - An unknown version must pass both rules. `fields.type_graph` walks every template argument
    of these containers and of `std::_Hashtable`, so the allocator and `_Value` are there to
    compare.
  - libc++'s formatters are LLDB's own C++ ones, and its members differ. So a libc++ build, known
    by its `std::__1` namespace, is not checked.
- **A string's own buffer.** LLDB 18 and 20 read GCC's `_M_local_buf`, a `char[16]`, as
  `char[0]`. LLDB says `DW_TAG_member '_M_local_buf' refers to type … which extends beyond the
  bounds of …`, though GCC's DWARF is valid.
  - `native.local_buffer` takes the buffer's length from `fields.type_graph`. It does so only
    where the string's members are libstdc++'s, at offsets 0, 8 and 16, and their sizes add up.
  - The pointer, the length and the capacity are 8 bytes each. The buffer must fit the union,
    and the union the string, by their byte sizes. Any size that is missing or doesn't fit
    gives no length, so nothing is written there.
  - With `writeMemory` (lldb-dap 21), the text is written there and read back.
  - Without it, Peeky would set the buffer's characters one by one, and LLDB lists none of them.
    So the refusal says that the debugger misreads the build's string buffer.
- **LLDB's errors in the output.** LLDB 18 prints those errors on its own stderr, which lldb-dap
  sends as `stderr` output events. `DAP` keeps an event apart where it is `stderr` and every
  line matches `NativeSession.debugger_diagnostics`: `error: <module> 0x…: DW_TAG_…`. Rust's
  sessions inherit the pattern, as they run on lldb-dap too.
  - This holds for the launch Peeky makes, with no `stdio` redirection. lldb-dap then runs the
    program on a terminal, and sends both its streams as `stdout`. A program line stays output
    there, whatever it says.
  - That is no DAP rule: lldb-dap sends a program's stderr as `stderr` where it isn't a
    terminal. So the warning says that a program line of exactly that shape would be moved too.
- **A bitfield's structured binding.** GCC describes `auto [lo, hi] = bits` with no location and a
  1-byte type named `__unknown__`, which LLDB shows as `char`. `NativeSession.unlocated` says so
  on the row and on a refused change. It replaces the advice to stop after the declaration, which
  no stop can follow.

## Which tests check what

C's real HTTP regressions cover:

- scalar, pointer and array inspection;
- integer changes, and the program's output after them;
- an unchanged source and binary;
- stale scopes and stops;
- tampering with the source, the binary or the symbols;
- absent debug information;
- cleanup of a stopped target, and after the adapter dies.

Argument tests keep whitespace, empty values, wildcards, dollar signs and single quotes, without
shell expansion. A harmless `.lldbinit` marker in a hostile home directory runs first as a
positive control. Then the tests confirm that it doesn't run under the worker's settings. No
required test calls a model.


| Test | What it checks | Where |
| --- | --- | --- |
| `tests/cpp/test_cpp_worker.py` | C++17 builds with debug information: a class object, a reference to it, `std::array<int, 128>` and a local integer. The `Pagination` object and the `Pagination &` reference expand to `page`, `page_size` and `total`. The `std::array` lists a raw `__elems_` child, which expands to integers. The test also accepts one raw storage member, or indexed elements, on other standard libraries, and requires the first three values to be 10, 20 and 30 | Apple LLDB 21, locally. Hosted LLVM 18 is a separate CI result, not an assumption about its display shape |
| `tests/cpp/test_cpp_strings.py` | `--set` on a `std::string` | lldb-dap 18 and 20. The Docker `cpp-llvm20` lane sets `PEEKY_EXPECT_READ_MEMORY` |
| `tests/cpp/test_cpp_bindings.py`, and `test_a_c_local_reached_through_a_pointer_is_never_changed` | Locals reached through a pointer are never changed | The C test: macOS |
| `tests/cpp/test_cpp_eval6.py` | A `stock.cpp`; strings by value, reference, pointer and typedef, in a struct, a vector and a map; overloads on one line | macOS (libc++); Linux (libstdc++, lldb-dap 18 and 20) |
| `tests/cpp/test_cpp_previews.py`, with C's in `tests/native/test_native_previews.py` | Struct and class previews | macOS (libc++); Linux (libstdc++, lldb-dap 18 and 20) |
| `tests/cpp/test_cpp_containers.py` | Vectors of strings, structs, `bool` and vectors; a moved-from vector; `std::array`; maps; sets; `std::deque`; `std::list`; a struct holding a vector; a reference; a pointer; a caller frame; a destructor during `clear()`; a structured binding of a reference; an element destroyed in place; a second thread, also one held inside `emplace_back`'s relocation; a loop's next pass; an optimized build | macOS (libc++); Linux (libstdc++, lldb-dap 18 and 20) |
| `tests/cpp/test_cpp_gcc_values.py` | Unordered containers, a string's text, LLDB's errors in the output and a bitfield's binding, built by clang and by g++ (under the test-only hatch) | Linux (libstdc++, lldb-dap 18 and 20) |
| `test_rows_read_through_a_container_a_library_call_is_changing_are_withheld` and `test_rows_read_through_a_container_whose_element_is_destroyed_in_place_are_withheld` | Rows read through a container that something may be changing | macOS and Linux |

## References

- [LLVM's lldb-dap](https://lldb.llvm.org/use/lldbdap.html)
- [lldb-dap's launch configuration](https://github.com/llvm/llvm-project/blob/main/lldb/tools/lldb-dap/README.md)
- [macOS debug symbols](https://lldb.llvm.org/use/symbols.html)
- [Ubuntu's lldb-18 package](https://packages.ubuntu.com/noble/lldb-18)
