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

## How functions are placed

Every rule about a local's declaration reads its function's entry in `local_declarations`. Those
rules are declared below the stop, still running, and under construction. `_function_at` finds a
frame's entry: the narrowest function whose code holds the frame's address.

- `_function_entries` makes an entry for each function with code whose declaration's file is one
  of the program's sources or headers. That file is `DW_AT_decl_file`, also through
  `DW_AT_abstract_origin` and `DW_AT_specification`.
- The SDK's and the standard library's functions never get an entry.
- A function's first line is its `DW_AT_decl_line`. Its own lines run from there to its last row
  in its file.
- GCC gives a lambda's `operator()` and a class's implicit members `DW_AT_artificial`, but no file
  or line. Clang gives them their class's.
- So `_declared_at` takes such a member's file and line from its class, as clang does. A lambda's
  closure type has the lambda's line.
- A member whose class has no file either gets no entry. A line-table row proves nothing: the
  first row in its code may be an inlined callee's, or a cold fragment's.
- `_class_role` tells a constructor of an unnamed class by its name: clang's
  `(unnamed struct at f.cpp:5:3)`, or GCC's `<constructor>`. GCC also names a closure's
  constructor `<lambda>`.
- A frame named `<constructor>` runs a constructor too (`_CONSTRUCTOR_NAME`).
- GCC's `operator()` and `_FUN` are artificial too, and may return nothing, as a constructor does.
  So they never count as constructors.
- A lambda's call operator is `operator()`, or a generic lambda's `operator()<int>`
  (`_calls_closure`). GCC names its object pointer `__closure`, where clang names it `this`.
- A closure's class has no plain name: clang leaves it unnamed, and GCC may name it
  `<lambda(long int)>`.
- `_local_classes` leaves a lambda's closure out of the classes a function defines. Its body is a
  function of its own, not lines the function runs as a class's member initializers.
- GCC may fold every call of a lambda away, leaving its closure with no `operator()`. Its members
  `<lambda>` and `~<lambda>` still mark it as a closure.
- `describe` leaves out GCC's `__closure`, and the variables GCC adds for a lambda's captures.
  lldb-dap lists none of them, but the one for a captured `this`, which `describe` keeps.
- **A safety net:** a C++ frame on a line of the program's sources or headers withholds every
  local and parameter when its function has no entry (`_unplaced`). The row says `not shown`, and
  that Peeky can't place the function's declarations.
- Before GCC's lambdas and implicit constructors were placed, the net took them. A `#line` that
  names another file for a function's declaration, as bison writes, still takes it there, with
  clang too.
- `_function_entries` also gives a `CodeDie` for every `DW_TAG_subprogram` and
  `DW_TAG_inlined_subroutine` with code, placed or not. Each has its ranges, depth, entry root
  (None where unplaced), enclosing code, call site, and the row at each range's start.
- An inlined call shares its caller's code, so the narrowest entry holding the address can be the
  caller's. `frame_level` finds the code the frame runs, and `_function_at` gives no entry where
  that is unplaced.
- The innermost frame runs the deepest code holding its address. lldb-dap gives an inlined call's
  caller the address where the call's code starts, and a call's caller its return address. One
  before either is in the caller's own code.
- At the start of an inlined call's code, a frame that shows the call's line is the caller: LLDB
  shows such a stop at the call. If the call's own first row is on that line too, either may be
  meant.
- Where the code may be unplaced, the row says Peeky can't tell which function the frame runs.
- GCC gives an inlined call with no code left the same `DW_AT_low_pc` and `DW_AT_high_pc`, and
  dwarfdump writes both as addresses. `_code_ranges` reads that as no code. Read as an offset, it
  held twice its address, and took the frames of `main` after it.
- Without the load address, the frame's line must be one placed function's own code: each of its
  rows must be that function's, as the deepest code there. Else the row is withheld, saying why.
- Frames outside the program's sources, such as the standard library's, aren't judged so. Nor are
  C targets.
- Clang's `cpp` lane and all of `tests/native` never reach the net.

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
| `test_rows_read_through_a_container_a_library_call_is_changing_are_withheld` and `test_rows_read_through_a_container_whose_element_is_destroyed_in_place_are_withheld` | Rows read through a container that something may be changing | macOS and Linux |

## References

- [LLVM's lldb-dap](https://lldb.llvm.org/use/lldbdap.html)
- [lldb-dap's launch configuration](https://github.com/llvm/llvm-project/blob/main/lldb/tools/lldb-dap/README.md)
- [macOS debug symbols](https://lldb.llvm.org/use/symbols.html)
- [Ubuntu's lldb-18 package](https://packages.ubuntu.com/noble/lldb-18)
