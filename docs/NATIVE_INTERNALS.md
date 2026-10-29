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

- Peeky builds C++ with clang or GCC, by what the compiler's `--version` says
  (`native.compiler_family`). It refuses any other compiler before the build.
- Peeky passes clang `--driver-mode=g++`. The resolved `clang++` is often `clang` itself, which
  picks its language from the name it was run by. g++ gets no such option: it knows no
  `--driver-mode`, and is always in C++ mode.
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
- An inlined call shares its caller's code, so the narrowest entry holding the address can be
  the wrong one. In a C++ target, `frame_level` gives the roots of every entry the frame may run.
  `_function_at` takes the one root's entry, and none where there are several, or one unplaced.
- The innermost frame runs the deepest code holding its address. lldb-dap gives an inlined call's
  caller the address where the call's code starts, and a call's caller its return address.
- A frame above the innermost one may run the deepest code at its address, or at the one before
  it. Where those differ, as at the end of an inlined call whose last instruction calls, both
  count.
- At the start of an inlined call's code, a frame that shows the call's line is the caller: LLDB
  shows such a stop at the call. If the call's own first row is on that line too, either may be
  meant.
- Of several roots, the frame's line picks one if only that one's code holds rows the debugger
  shows on it. Else its name picks the one entry named so. An inlined call's start on its own
  call's line picks none.
- Rows a later row at their address replaces count for neither: LLDB shows the last one's line.
  GCC writes the call's line at an inlined call's start, then the callee's.
- Copies of a function at one address (folded) are several roots, placed or not.
- A frame left with several roots, or one unplaced, is withheld. The row says Peeky can't tell
  which function the frame runs, or can't place it.
- C targets keep the narrowest entry, as before.
- GCC gives an inlined call with no code left the same `DW_AT_low_pc` and `DW_AT_high_pc`, and
  dwarfdump writes both as addresses. `_code_ranges` reads that as no code. Read as an offset, it
  held twice its address, and took the frames of `main` after it.
- Without the load address, the frame's line must be one placed function's own code: each of its
  rows must be that function's, as the deepest code there. Else the row is withheld, saying why.
- Frames outside the program's sources, such as the standard library's, aren't judged so. Nor are
  C targets.
- Clang's `cpp` lane, on lldb-dap 18 and 20, and all of `tests/native` never reach the net.

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

## A local declared on the stop line

- `local_declarations` gives each local's `starts`: the first row of its declaration line in the
  block declaring it. A stop there withholds the local (`declared_on_line`).
- A first row with another line's code just before it, by address, is `preceded` instead. That
  code may be a later line's, or another file's.
- A header over two lines runs that code first, then falls into the line with the local set.
- At `-O0`, clang puts a C++ landing pad there, on a closing `}`'s line. It ends in a jump, so
  only a jump reaches the line.
- At the stop, `_check_loop_target` disassembles the function once. `control_sources` lists where
  control comes to each instruction from: the instruction before, unless it never falls through,
  and each jump that names it.
- A call's return is a way in Peeky can't follow, since the call may never return
  (`_Unwind_Resume`). So is any instruction, in a function that jumps through a register.
- The unwinder enters a function at its landing pads, which no disassembly shows.
  `landing_pads` reads them from the binary's exception tables, once per session. On ELF, each
  `.eh_frame` FDE names its LSDA in `.gcc_except_table`.
- On Mach-O, the `__unwind_info` LSDA array, and any `__eh_frame` FDE, name LSDAs in
  `__gcc_except_tab`. Each LSDA's call-site records give the landing pads.
- Every field is read within its record, its augmentation data or its call-site table. A LEB128
  number must fit in 64 bits, and the `__unwind_info` arrays must lie in their section, past its
  header.
- A table that breaks any of these, or a DWARF64 `.eh_frame` record, can't be read. Then no
  landing pad is known, which is never the same as having none.
- `runs_into` is True where every way in from below is another line's code, and no jump comes
  back. It is False where every way in from below is code at or above the line, or where only
  jumps back come. Anything else is None, and so is a landing pad, or tables Peeky can't read.
- False makes the row a start. True shows the local. None, or a disassembly Peeky can't read,
  shows it with a note, and refuses a change.

## A caller's local whose declaration line has no code

- `declared_unfinished` withholds a caller frame's local whose declaration line has no row of
  its own. GCC's C++ does this to `struct trio t = { seed,`, and clang to an array of structs.
- `_stored_before` lifts that. The frame's line must be below the statement's end, from
  `Statements.end`.
- Each row of the statement's lines, from the function's `order`, must end before the call.
  A row ends at the function's next row, or at the end of its code there. The call is the
  return address less one.
- `NativeSession._resume` gives that return address only where it is proven a physical call's,
  in code laid out in source order. The build must be `-O0`. And no unit may optimize a function
  on its own: `c_source.optimizes` finds an `optimize` attribute or pragma in a unit's `-E` text.
- The frame must be above the innermost one, and no signal interrupted it. No inlined call's
  code may hold its address or the one before. LLDB gives the caller of an inlined call the
  address where that call's code starts.
- Nor may it be a frame LLDB rebuilt for a tail call's caller, which has no frame of its own
  left. LLDB rebuilds one only from a call site marked as a tail call. So the build's debug
  information must mark none (`has_tail_calls`), and the frame's `presentationHint` must not
  be lldb-dap 20's "subtle".
- A row after the call, such as a loop's code placed there, keeps it withheld. So does anything
  unknown.
- A loop around both runs the declaration again before the call on each pass. A jump past the
  declaration is judged after it, by `declared_skipped`.

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
- LLDB names an unnamed class, such as a lambda, by the first typedef it meets for it. A map
  ordered by a lambda is `std::map<int, long, type>` with libc++ (`__type_identity<…>::type`), or
  `std::map<int, long, key_compare>` with libstdc++. `NativeSession._unnamed_arguments` gives a
  root local's row the compiler's name from its record's `DW_AT_name`, argument by argument. It
  takes one only where LLDB writes a bare identifier, and the record has a lambda's or an unnamed
  class's name at that place: `(lambda at a.cpp:4:18)`, `main()::<lambda(int, int)>`.
  `debugger_type` keeps LLDB's. `_order` reads neither as `std::less`, so `m[K]` reads only an
  exact key.

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

## Character arrays

- LLDB's summary of a `char[N]` (`signed`, `unsigned`, or a `char8_t` typedef) reads its first N
  bytes, at most `target.max-string-summary-length` (1024), and adds `...` past that.
  `CopyStringDataToBufferSP` drops the trailing NULs (`rtrim('\0')`).
- It escapes a NUL as `\0`, a byte that starts no UTF-8 character as `\xNN`, and a control
  character as `\U000000NN`. Each escape stands for one byte, or one character's UTF-8.
- Peeky's lldb-dap sources no init file (`sourceInitFile: false`), so these settings and
  `escape-non-printables` keep LLDB's defaults.
- LLDB's summary formats whatever part of its read succeeded: `GetPointeeData` keeps a partial
  read, and the DAP value doesn't say how much was read. So an array that runs into an
  unreadable page shows the bytes before it, as if NULs followed.
- `peeky.char_arrays` reads the bytes back from that summary. A character LLDB would have
  escaped, shown as itself, means the text isn't its summary: the value stays LLDB's.
- Those bytes are proven: LLDB read them. A NUL among them is the first one.
- `proven_rest` reads the bytes after them up to the window, `min(N, 1024)`. With `readMemory`,
  that is one read of the whole window from the variable's `memoryReference`. Every byte must
  come back, and its start must match the summary's bytes.
- Otherwise it reads one page of elements, at most 64, from where the summary's bytes end. Each
  must be named `[i]` in order and read as a char literal. An unreadable one ends them: lldb-dap
  shows `error: read memory from … failed`.
- These reads share the inspection's value-view budget, or a preview's. A spent budget leaves
  the bytes `not read`.
- The worker passes `string_value` and `preview_value` a `proven()` callback. The bytes are read
  again only where it holds: no note, no taint, an unoptimized build, and every pointer or
  reference on the way judged live (`bytes_reachable`, `nodes_reachable`). A prefix match alone
  proves nothing of a value that may be leftovers.
- `then N NUL bytes` needs every byte of the array read, all NULs after the text. A NUL with
  bytes after it not all read, or not all NULs, is `then a NUL and N bytes not shown`. No NUL
  among the bytes read is `then N bytes not read`.
- `NativeSession.string_value` matches the array by LLDB's type name. `buffer` spells every
  byte read, and ends `...` where the array holds more.
- `stored_values.text` checks the note's count against the row's type. A run saved before the
  note proves a NUL only where LLDB's literal shows one, and no NUL only where it shows N bytes.

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

GCC describes a structured binding of a pair or tuple (`auto [t, u] = p;`) another way. Each name
is a variable that holds a reference to `std::tuple_element<0, E>::type`, the element. lldb-dap
shows that reference's address as the variable's value.

- `fields.element_reference` tells such a variable by that type. `local_types` stores it as
  `through`, wherever it is kept, so a change is refused before the program runs.
- Apple clang 21 types it as a plain reference (`long &&`), not marked artificial. Clang 18
  marks it artificial, and lldb-dap doesn't list it.
- Either way, `Statements.binds` reads the declaration's line: the name must be in a binding's
  brackets, and declared no other way. A reference the code declares with GCC's type
  (`std::tuple_element<0, P>::type &r`) fails that check, so it stays a reference. A GCC
  candidate whose line Peeky can't read is withheld.
- `Statements.binding` says whether the binding is by reference, and what it binds.
  `NativeSession._bound_alive` follows one by reference only where it proves what that refers to
  is still alive. Anything else is `not shown`, with why, since it may be gone.
  - An object of the frame, or a member of one (`_local_object`): a local or by-value parameter in
    scope there, reached through no reference, pointer or union (`_member_object`).
  - An element of a local `std::vector`, `std::map`, `std::set` or `std::multiset`, by a range
    `for` over it or one subscript of a vector (`_held_element`). `_plain_container` reads its
    template arguments: `std::allocator`, `std::less` for an order, and elements of base types
    (a map's key and value, or a `std::pair` or `std::array` of them). `type_graph` walks every
    template argument of such a container for this. `Statements.leaves_elements` reads each
    translation unit as the compiler preprocessed it.
  - In the binding's scope, the container may only call a member that gives a value
    (`_VALUES`). In its own scope, nothing may keep a pointer, iterator or reference into it: a
    call's result is dropped (`m.insert(…);`), an element is only assigned (`v[1] = x;`), and
    another binding over it keeps its names. No lambda or local class may name it.
  - `allocation_replaced` reads the build's debug information for a global `operator new` or
    `operator delete` of the program's own, in any form but the placement ones the headers
    define. One that is there, or a library the launch preloads (`_PRELOADS`), withholds
    it (`_allocation_seen`). So does a Linux host's `/etc/ld.so.preload` that names a library,
    or that can't be read whole. `system_preload` reads it on the worker's host, in
    `launch_args`, ignoring blanks and `#` comments.
  - A call's result is never proven (`_kept_result`). The function called may keep a pointer
    into it, as the named return value optimization builds it where the caller keeps it.
  - `Statements._names_kept` follows each of the binding's names, which must be of base types
    (`_scalar_bound`): never an enumeration or a pointer.
  - `_operand` reads the expression around each use. Every token must be a name of a base type
    (the binding's, or a local's), a number literal, a cast to a base type, a parenthesis, or a
    built-in operator. With every operand of a base type, no overload can be called. A literal
    must match `_NUMBER_LITERAL`, with the language's own suffixes only.
  - `_only_read` then reads what takes that expression. A new value goes anywhere. The name alone
    may be a statement, an assignment's target or source of a base type, a condition, returned,
    or an argument every function of the call's name takes by value (`call_arguments`).
  - `bound_element` then checks the address the reference holds (`_bound_place`). It must be in
    the vector's elements (its own `_M_start` and `_M_finish`, or libc++'s `__begin_` and
    `__end_`). Or it must be in one of the map's entries as the debugger lists them.
  - In an optimized build, only an object of the frame is read.
- `NativeSession.bound_element` gives the worker the element in the variable's place. For a
  number, a character or a pointer, that is the debugger's one child under the reference, with
  nothing listed under it. For a struct, a string or a container, `element_text` keeps the text
  after the address, and the members stay listed.
- `display_type` gives the binding's type, and `debugger_type` keeps LLDB's reference.
- Where `not_yet_declared` would show it with a note, as at a loop's return to its line, it is
  withheld. Until its declaration runs, the reference may point anywhere. In an optimized build,
  so is any stop on its declaration's lines. One the debugger lists no element under is `not
  shown`.

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

These rules hold for any build, and clang's builds pass them. The user-facing list of GCC's gaps is
in the [cookbook](lang/c-cpp-cookbook.md#what-is-different-in-a-gcc-build).

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
    handle, unless Peeky reads its nodes (below). A preview shows `<not listed>` in its place.
    The probe refuses its keys and positions: `um[1] not read: um is not listed: …`.
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
- **Reading the nodes.** Where LLDB's listing isn't proven, `NativeSession._node_listing` reads
  the entries itself (`hashed_nodes`). The worker then shows the row as a value view:
  `view: memory`, with worker-owned handles.
  - `unlisted_container` reads the count alone, and keeps its path and value on the
    `Withholding` (`counted`). The worker asks `listed_container` for the nodes only once
    nothing else withholds the row. So a row with a note (`no value yet`, leftovers, one
    read through a value with a note) is never read from its nodes.
  - Every C or C++ pointer, and every C++ reference, on the row's way must be judged to
    refer to a live object (`DebugWorker.nodes_reachable`). `NativeSession.indirections`
    names them by the debug information: `T &`, `T &&`, a typedef of one, a member.
  - A pointer is judged by #339's `pointee_unproven`. A reference is judged the same way
    (`referent_proven`): the address the debugger shows for it, and what `referent_of`
    says it refers to.
  - One the rule doesn't judge counts as unproven, as does a root local the frame reaches
    through a pointer, which no variable stands for. A heap block may hold a container's
    bytes, and so may a returned call's dead frame.
  - lldb-dap 18 gives no address for a local the debugger shows with a summary, so Peeky
    can't place a container local there. A pointer or reference to one stays unproven, and
    nothing is read through it. lldb-dap 20 gives each local's `memoryReference`.
  - A preview reads nested containers' nodes under the same rule: none below a note, or
    through such a pointer.
  - A GCC lambda's capture is read through `__closure`. Where `__closure`, or what a capture
    by reference refers to, isn't proven live, its note withholds the nodes too. Its count
    is the capture's own, not that of a global it shadows.
  - Nothing is read in an optimized build, nor where a unit's preprocessed text may optimize
    a function (`c_source.optimizes`). That is any `optimize(` or `__optimize__(` outside
    comments and literals, in any attribute form (`[[using gnu: optimize("O2")]]`), also
    behind a macro, and any pragma that names `optimize`.
  - Such a function may keep a value it stores in a register while the count and the chain
    are unchanged. Any function on the stack may be one. A function named `optimize`,
    called, counts too: the detector would rather withhold.
  - The nodes form one chain from `_M_h._M_before_begin._M_nxt`, in iteration order. Each node
    is a `std::__detail::_Hash_node<_Value, cache>`. The hashtable's `_Traits`
    (`_Hashtable_traits`) say whether it caches the hash code.
  - No local's type reaches the node type but through a pointer. So the session adds every
    `_Hash_node` the debug information defines to `fields.type_graph` (`hashed_nodes.node_types`).
  - `hashed_nodes.layout` proves where a node holds its entry. The node types of that `_Value`
    and cache, one per unit, must all place it alike. Each holds one `_Hash_node_base` at
    offset 0, an 8-byte pointer `_M_nxt`.
  - It also holds one `_Hash_node_value_base` of `_Value`, whose `_M_storage` is an
    `__aligned_buffer` of `_Value`, at least its size. The entry sits there: past the link,
    aligned for `_Value`, inside the node.
  - The chain must end in a null `_M_nxt` after exactly `_M_element_count` nodes, at most 256.
    Every node must be aligned.
  - With `readMemory` (lldb-dap 20), `hashed_nodes.Memory` reads each node, 4,096 bytes a
    request, so nodes close together cost one request. Without it (lldb-dap 18), `Words` reads
    8-byte words through hover paths.
  - The word 8 bytes into the first node is `us._M_h._M_before_begin._M_nxt[1]._M_nxt`. LLDB
    reads `[1]` past a pointer, and `_M_nxt` of what is there, as `frame variable` does.
    Nothing is evaluated in the program. `Words` reads nothing outside the nodes.
  - `Memory` reads more: each request takes 4,096 bytes from a node's address, and a string's
    text comes from where its data pointer points. Only bytes at the nodes' own addresses,
    and at a string's text, are decoded.
  - The entries must hold integers, `bool`s and libstdc++ strings, in `std::pair`s and the
    program's structs. A struct has no base class, bitfield or anonymous member.
  - A string's text is read from its own buffer, or where its data pointer points. Its length
    must fit that buffer, as `local_buffer` proves it. Up to 1,024 bytes are read, as for any
    string read from memory.
  - The entries come back listed as the debugger lists a container it reads right: `[0]`, a
    pair's `first` and `second`, a struct's members, `T @ 0x…` for a struct. So
    `native_previews` shows them as any other container: `size=1 {1: {x = 5, y = -6}}`.
  - Nothing is read where a call may be changing the container (`containers_settled`).
  - The reads have their own budget: 4,096 requests and 4 MiB per inspection or expansion
    (`NODE_READS`), within the value view's 10 s. A hover's text is cached for the stop. With
    lldb-dap 18, 333 entries in three containers took about 1,000 hovers and 0.25 s.
  - Otherwise the row stays `not listed`, with the note it had.
- **A string's own buffer.** LLDB 18 and 20 read GCC's `_M_local_buf`, a `char[16]`, as
  `char[0]`. LLDB says `DW_TAG_member '_M_local_buf' refers to type … which extends beyond the
  bounds of …`, though GCC's DWARF is valid.
  - `native.local_buffer` takes the buffer's length from `fields.type_graph`. It does so only
    where the string's members are libstdc++'s, at offsets 0, 8 and 16, and their sizes add up.
  - The pointer, the length and the capacity are 8 bytes each. The buffer must fit the union,
    and the union the string, by their byte sizes. Any size that is missing or doesn't fit
    gives no length, so nothing is written there.
  - With `writeMemory` (lldb-dap 21), the text is written there and read back.
  - lldb-dap 20 reads memory but has no `writeMemory`. LLDB lists none of the buffer's
    characters, so Peeky can't set them one by one. The worker's `memory_command` writes the
    text and its NUL with LLDB's `memory write`, through lldb-dap's command escape.
  - That write is made only where `_M_dataplus._M_p` is the buffer's address and the text fits.
    Then `_M_string_length` is set, and the pointer, the length and the bytes are read back.
  - LLDB prints nothing but the command when the write succeeds. Anything else is a refusal
    where the bytes still read as before; otherwise the session is closed.
  - lldb-dap 18 reads no memory, so it can't tell where the text is. The change is refused, as
    in clang's build.
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
- **Static storage.** GCC places a `const` local aggregate it can build at compile time in
  read-only data (`DW_OP_addr`). lldb-dap lists no static storage among a frame's locals, a
  `static` local's neither. Peeky reads them as [Static locals](#static-locals) says.
- **A const pointer.** GCC types `this` as `Widget *const`. `fields.settable_member` refuses a
  change through a pointer under a `const` as a pointer's, not as a const object's
  (`_pointer_under`).
- **A lambda's captures.** GCC passes them through `__closure`, and describes each one as an
  artificial variable read through it. lldb-dap lists neither, so the worker reads them itself
  (`DebugWorker.read_captures`).
  - `_gcc_capture_members` matches each variable to the member of the lambda's object at its
    offset: `DW_OP_fbreg -24, DW_OP_deref, DW_OP_plus_uconst 0x8` is the member at 8. The
    member's type must be the variable's, or the variable's without its `const`.
  - lldb-dap lists the members by GCC's names (`__bonus`). The offset, not the name, tells which
    member holds a capture.
  - The worker asks for `__closure` as a hover, which reads the frame's variable and runs
    nothing. It lists what it points at, and shows each capture by its own name in a `Captures`
    scope. An expansion finds the member again the same way (`walk_capture`).
  - What is read through `__closure` is judged as through any pointer (#339): its type is the
    lambda's object. A capture by reference is judged as a pointer to what it refers to
    (`capture_referent`), and shown as that value, as a pair's binding is (`bound_element`). A
    pointer captured by reference shows its value; its text is judged as a pointer local's
    (`captured_pointer`).
  - `capture_plan` says how each capture reads at the stop. Where the lambda also declares
    its name, `_capture_scope` decides by scope. A stop whose code is outside the declaration's
    block (its DWARF ranges) names the capture, whatever the text says: a macro's declaration
    ends where its line doesn't. Else it reads the source as C++ does
    (`Statements.scope_lines`): a stop above the declaration, or past its scope, names the
    capture, and one past its statement in its scope names the local. Anything else, such as
    a stop in the block on the declaration's own line, withholds the capture, saying so.
  - `closure_members` says which read failed: the hover of `__closure` or the listing of
    what it points at (no answer in time, an error, malformed, past 512 members).
  - A capture the worker reads replaces any row of its name lldb-dap lists: a local whose scope
    hasn't begun, whose storage is no live object. `_typed` types a capture's root by the
    capture's type there (`_capture_root`).
  - `[*this]`: GCC gives `this` no location, and the copy is the member `__this`, of the class
    `this` points at (`_copied_this`). The worker reads it as `this`, in place of lldb-dap's row
    with no value. `[this]`'s `this` lldb-dap lists itself.
  - A capture with no row says why in `closure_unread`, which `probe._capture_hint` gives.
  - A `static` of a capture's name gets no `Statics` row where the worker reads the capture in
    its place: the worker passes the capture's name to `static_rows` with the frame's locals.
  - `not_yet_declared` withholds a capture where `_under_construction` would withhold clang's
    `this`. No declaration rule applies to it: the object holds it from before the call.
  - A capture's local storage is `through` in `fields.local_types`, so a `--set` is refused
    before the run. At a stop, the scope's handle and its children's refuse it too
    (`capture_refusal`).
  - `captures_unread` reads none in an optimized build, where a `mutable` lambda may keep a
    capture in a register, nor in a prologue window, before the lambda stores `__closure`.
  - `_lambda_scope` follows every function around a lambda, up to `LAMBDA_NESTING` (256).
    Past that, one may still be running, so it never counts as finished.

## Static locals

lldb-dap lists a frame's `static` locals in its "Globals" scope, with its unit's globals, never
among its locals. Each has `$__lldb_extensions.declaration`: its file and line.

- `_static_locals` gives each function entry its `statics`: the variables of its bodies at one
  `DW_OP_addr`, with their file, line, address, type and block's code. Clang declares a block's
  `static` in the function's scope, so its block is the function's.
- `NativeSession.static_scope` lists "Globals" for a C or C++ frame whose function has one. The
  worker reads it as the `Statics` scope (`static_storage`).
- `_static_named` picks the declaration the code at the stop names. `_static_scope` reads where
  the stop stands to each, from the source as the compiler preprocesses it.
- It reads `Statements.scope_lines`, and `block_storage` for a later declaration that hides it.
  The stop must be below the declaration, in its scope, and in its block's code where the debug
  information gives one. Of several, the innermost counts.
- Copies of one function (inlined, or cloned) may each declare it again, even at another
  address. GCC declares it in an abstract block, whose code is each copy's block's.
- `_static_placed` takes one declaration per origin, file, line and type. Its placement is the
  copy whose block holds the stop. Copies at several addresses that hold it are unsure.
- `_static_entry` matches it to lldb-dap's row by name, file and line. The name may be plain,
  `calls @ f.c:9`, or a symbol's `Acc::run()::calls`. An address lldb-dap gives must be the
  declaration's, slid.
- A symbol's name must be the frame's function, as LLDB names it: `long tick<int>(int)::calls`,
  never another instance's, at the same file and line. Else its address must prove it.
- In an optimized build, a row carries a note. Stores and register updates may be reordered,
  so the code may use a value other than what the declared address holds.
- A row keeps lldb-dap's name; `selected_as` gives the plain one. `_static_declared` gives the
  row's `name @ file:line` to `fields.local_types` lookups, for its type.
- In C++, `_static_guard` finds the `_ZZ…` symbol at its address (`object_symbols`). Its guard
  `_ZGV…`'s first byte is read with LLDB's `memory read`. Zero means its initializer hasn't run:
  `no value yet`.
- No guard, or a symbol of another mangling (GCC's `cs.0`), needs no wait. A guard Peeky can't
  read, or no symbol, is `not shown`.
- Rows are never settable: `settable` says no, and `never_settable_row` says `static`. The
  worker refuses `set_variable` on the scope's reference. A member is refused by
  `validate_member` (`_not_framed`), or as const. A probe's `--set` of one is refused before
  launch.
- Why one isn't read is in `static_notes`, which `probe._static_hint` shows.

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
| `tests/native/test_native_statics.py`, `tests/cpp/test_cpp_statics.py` | `static` locals, and GCC's `const` aggregate in read-only data: read where the code names them, a C++ one once its guard says it is initialized, and never changed | macOS (Apple clang); Linux (clang 18, gcc and g++, lldb-dap 18 and 20) |
| `tests/cpp/test_cpp_gcc_values.py` | Unordered containers, also read from their nodes (types, strings, a call under way), a string's text, LLDB's errors in the output, a bitfield's binding, a pair's or tuple's bindings (by reference too: into a local container, or of a call's result), and a lambda's captures, built by clang and by g++ | Linux (libstdc++, lldb-dap 18 and 20); the clang side on macOS too |
| `tests/unit/test_hashed_nodes.py` | Reading an unordered container's nodes: the layout proof, the chain's length, the types read, words through variable paths, and the worker's value view | Unit |
| `tests/cpp/test_cpp_landing_pads.py` | A local declared on the stop line, after clang's landing pad, is withheld; built by clang and by g++ | macOS (Apple clang); Linux (clang 18 and g++, lldb-dap 18 and 20) |
| The `cxx` fixture (`tests/conftest.py`) | Cases whose lines, names or notes depend on the compiler run once per compiler, with per-compiler expectations. A lane whose `PEEKY_NATIVE_CXX` is g++ runs only their GCC side | Linux; macOS runs the clang side only |
| `test_rows_read_through_a_container_a_library_call_is_changing_are_withheld` and `test_rows_read_through_a_container_whose_element_is_destroyed_in_place_are_withheld` | Rows read through a container that something may be changing | macOS and Linux |

## References

- [LLVM's lldb-dap](https://lldb.llvm.org/use/lldbdap.html)
- [lldb-dap's launch configuration](https://github.com/llvm/llvm-project/blob/main/lldb/tools/lldb-dap/README.md)
- [macOS debug symbols](https://lldb.llvm.org/use/symbols.html)
- [Ubuntu's lldb-18 package](https://packages.ubuntu.com/noble/lldb-18)
