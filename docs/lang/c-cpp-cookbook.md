# Peeky Cookbook: C and C++

What is particular to C and C++ in Peeky's recipes. Read the [COOKBOOK](../COOKBOOK.md) for the recipes every language shares: each section here has the number and title of the COOKBOOK section it adds to. The one-page summary of C and C++'s rules is [c-cpp.md](c-cpp.md).

## Contents

- [1.1 What Can I Ask?](#11-what-can-i-ask)
- [2.1 Fields and Indexes](#21-fields-and-indexes)
- [2.2 Every Iteration of a Loop](#22-every-iteration-of-a-loop)
- [4.7 C++ References and Pointers in SQL](#47-c-references-and-pointers-in-sql)
- [5.1 Stop, Look, Change, Finish](#51-stop-look-change-finish)
- [7.1 C and C++](#71-c-and-c)
  - [Quick Start](#quick-start)
  - [Structs and Arrays of Structs](#structs-and-arrays-of-structs)
  - [Building](#building)
  - [Locals Peeky Withholds](#locals-peeky-withholds)
  - [Members](#members)
  - [Strings with a NUL Byte (C++)](#strings-with-a-nul-byte-c)
  - [Changing Text](#changing-text)
  - [Maps and Sets (C++)](#maps-and-sets-c)
  - [Changing Fields and Elements](#changing-fields-and-elements)
  - [What `set` Checks](#what-set-checks)

## 1.1 What Can I Ask?

Adds to [COOKBOOK 1.1](../COOKBOOK.md#11-what-can-i-ask).

- **Types:** Each name shows its type; in C a struct, union or enum that no typedef names is written with its tag, as C code names it (`r (struct ring *)`, `c (enum color)`; a typedef's name stays: `t (box_t)`). C++ writes the standard library's types as code and probe tables do (`parts (std::vector<std::string>)`, `p (const std::string &, lines 6-7)`), and lists none of the compiler's own locals (a range `for`'s `__range1`, `__begin1` and `__end1`).

## 2.1 Fields and Indexes

Adds to [COOKBOOK 2.1](../COOKBOOK.md#21-fields-and-indexes).

- **C and C++ pointers:** In C and C++, follow a pointer with `.` (`stats.total_us`, not `->`); a pointer to a scalar carries no length, so `values[0]` reads what it points at; for other elements, read the array itself where it is declared (in the caller). The same holds for a pointer to an array, a container or a struct: `pa[0]` is what it points at, so `pa[0][1]` is `(*pa)[1]`, while `pa[1]`, which C reads past the pointee, is refused with that selector (#102). A typedef'd pointer (`RowPtr`, `const RowPtr`) works the same; the build's debug information tells it from a typedef'd reference, which indexes its referent. A pointer under a type name it does not resolve (a template's member typedef such as `std::vector<T>::pointer` to a container) is refused for indexing rather than read either way. A NULL or unreadable pointer is refused (`p is NULL`).
- **C++ maps and sets:** On a C++ `std::map` or `std::unordered_map`, `m[1]` is the value for key 1, as in C++, never the debugger's second entry; `m[#0]` is the first entry, and a set has only `s[#0]`, ... ([7.1](#maps-and-sets-c)).

## 2.2 Every Iteration of a Loop

Adds to [COOKBOOK 2.2](../COOKBOOK.md#22-every-iteration-of-a-loop).

- **GCC:** GCC gives `for (;;)` and `do {` no code of their own, so LLDB moves their breakpoint into the body, which stops at each pass (`moved` says so).

## 4.7 C++ References and Pointers in SQL

Adds to [COOKBOOK 4.7](../COOKBOOK.md#47-c-references-and-pointers-in-sql).

**What it answers:** Which arguments did `trim_left` change? A `const std::string &` (or a pointer) is saved as the debugger shows it, its address first: `0x000000016d78b298 "apple"`. It never equals a plain string's `"apple"`, so take the text after the address before comparing.

`examples/cpp/refs/trim.cpp` trims each argument's leading spaces. Capture the reference parameter `s` in `trim_left`, then the result `name` in `main`, one run each:

```sh
peeky probe examples/cpp/refs/trim.cpp --at trim.cpp:6 --arg apple --arg " pear" --arg "fig " \
  --capture s --hits all --label argument
peeky probe examples/cpp/refs/trim.cpp --at trim.cpp:11 --arg apple --arg " pear" --arg "fig " \
  --capture name --hits all --label name
peeky sql "SELECT n.hit, s.value AS argument, n.value AS name,
  substr(s.value, instr(s.value, ' ') + 1) = n.value AS unchanged
  FROM evidence s JOIN evidence n ON n.hit = s.hit AND n.label = 'name' AND n.selector = 'name'
  WHERE s.label = 'argument' AND s.selector = 's'"
```

```
hit  argument                    name     unchanged
1    0x000000016d78b298 "apple"  "apple"  1
2    0x000000016d78b298 " pear"  "pear"   0
3    0x000000016d78b298 "fig "   "fig "   1
```

`substr(value, instr(value, ' ') + 1)` is the text after the address: an address has no space, while the text may have several (`" pear"`), so take only the first. Use it only on a reference's or pointer's value (one that starts with `0x`); on a plain string it would cut the text at its first space. `no_addresses(value)` gives `0x… "apple"`: enough to compare two references, or one across runs, but still not equal to `"apple"`. Group and count such values by it, not by `value` (`GROUP BY no_addresses(value)`): two references to the same text at different addresses are then one group. A text Peeky could not check whole carries a note after it (`"v" (shown up to a NUL at byte 1 of 3)`, libstdc++ with lldb-dap 18; [7.1](#strings-with-a-nul-byte-c)), so it never equals a plain string either.

## 5.1 Stop, Look, Change, Finish

Adds to [COOKBOOK 5.1](../COOKBOOK.md#51-stop-look-change-finish).

- **C and C++ callers:** In C and C++, a caller's local is withheld while its declaration's code is not finished: in `long result = twice(seed);`, or `long multi =` over `twice(seed + 1);`, the value is stored only after `twice` returns (so is a local whose declaration line has no code of its own: an array of structs, a nested struct or a 2-D array initialized over several lines is stored element by element on its elements' lines). So is a local whose declaration statement, read from the source to its `;`, is still running on the caller's line: in `struct trio t = { seed,` / `{ twice(3),` / `twice(4) } };`, the first line's code runs before the calls and `t.p` is stored after them. A `for` or `while` loop's own variable (`for (int i = 0; …)`) is withheld too while the body calls, on one line or several: the header's code also runs after the body, and Peeky cannot prove from the source that the loop has run its initialization (a `goto` into the body, or a call a macro hides in it, skips or delays it). Read it in the loop's body: probe a line there, or `step --out` back to that frame.
- **Headers and other files:** A variable declared in a `for`, `if`, `while` or `switch` header counts until the `;` after the whole statement, so it stays withheld while the body calls, and where the source does not give the end, the local is withheld: brackets that do not balance, or text the compiler may read differently from the scan (between the declaration and its end, a directive such as `#include`, a line splice, `<::`, or a macro of the program's sources and headers that leaves a bracket open or holds a `;` outside its brackets; anywhere in the file, `#line`). So is every local whose declaration is in another file than the caller's line: an initializer can `#include` its rows, or be included itself.
- **How the source is read:** C's trigraphs are read as `-std=c11` reads them (`??/` is a backslash); C++17 has none. Digraphs (`<% %> <: :>`) are brackets, and lines are counted as the compiler's debug information counts them (`\r\n`, a lone `\r` or `\n`; `\n\r` is two).

## 7.1 C and C++

**Common cases**

- **Read** any value by selector: `s.end`, `bins[3].hi`, through a pointer with `.` (`ps.start`), C++ members as `this.head_`, a map by key (`m[1]`). A `std::vector`, `std::array` or `std::map` row shows its first elements (`size=3 ["users", "42", ""]`; not in an optimized build or a program running several threads, nor while a library call changes it), so a capture reads them without a session.
- **Set** a 16- to 64-bit integer local, or such a field or element of a struct or array local the stopped frame owns (`--set s.end=10`), at a statement's first line; rows mark it `[settable]` (an 8-bit `char` is refused).
- **Set a text** in a `char[N]` or `std::string` local (`--set 'name="apple"'`), within its size, never reallocating; not through a `char *` or a reference, nor in a const one. lldb-dap 21 (macOS) writes every case, older ones fewer (**Changing text** below).
- **A field through a pointer** (`r->tail` in `push(struct ring *r)`, `this.count`) cannot be set: set it in the frame that owns the struct, before the call (in C, when the program calls the function once, the refusal names that call: `push has one caller: …`; where that caller only passes its own pointer on, it follows the calls to the frame that owns the struct, or says it cannot).
- **`no value yet`** is a local not initialized at this stop (declared below it, an initializer still running): read it below its declaration (the row says where: `stop after line 35 to read it`). A `note` is a value Peeky cannot vouch for: on a line a loop returns to (a `do` body's first) the next line reads it; below a `setjmp` call, a local declared at or below the stop shows a `note` instead of `no value yet`, and reads plainly below its declaration.
- **Same-named locals:** a plain name is the declaration the code names at the stop; `name @ file:line` selects another.

### Quick Start

What can be read, what can be set, and where:

- **Read** any local, parameter or global by selector: fields and elements (`s.end`, `bins[3].hi`, `grid[1][0]`), through pointers too (`ps.start`), C++ members through `this` (`this.head_`), a `std::string` whole, a `std::vector` by index and a `std::map` by key (`m[1]`, `ages["al"]`).
- **Structs, arrays and most standard containers** (**Standard containers** below) show their members: `bins` reads `[{lo = 0, hi = 2, count = 0}, …]`, `bins[3]` `{lo = 6, hi = 7, count = 0}`, a `std::vector<std::string>` `size=3 ["users", "42", ""]`, in probes, `inspect` and `session get`. Unions and what pointers point at are never read into a preview.
- **Withheld, never guessed:** a local whose storage may still hold leftovers (declared below the stop, an initializer still running, a jump past its declaration) shows `no value yet` and why; one Peeky shows but cannot vouch for (a `do` body's first line, below a `setjmp` call) carries a `note`, and so does every field and element read through it (``read through `q`: …``, in captures, in `session get` children, marked `[note]`, and in the evidence), which `--set` refuses where it refuses the local.
- **Set** integer locals, and integer fields and elements of a struct, class or array local the frame holds itself: `--set s.end=10`, `--set 'bins[3].hi=8'` (16 to 64 bits, in range).
- **Text:** a `char[N]` local or a `std::string` local takes a text in double quotes: `--set 'name="apple"'` (see **Changing text** below).
- **Never** through a pointer or a reference (`ps.end`, `this.count`, a C++ structured binding of a reference, `auto &[at, n] = *owner`, as `n` or `at.x`, or a C `__block` variable, all `[not settable: reference]` as fields; in C, a refusal for a pointer parameter names the function's call when the program's sources have only one: `push has one caller: m.c:15 in main, which passes `&r`, where `r.tail` is settable`, following a pointer passed on to the frame that owns the struct), nor in a union, a packed struct, a bitfield, a const or a `std::` type; floats cannot be set, and a text only as above. A probe whose `--set` the build's debug information rules out like this in every function on its `--at` line is refused before the program runs (the debugger's launch request is not sent, no run saved); parameters are set as other locals (an unoptimized build keeps them on the stack).
- **Where:** at a statement's first instruction, so a breakpoint on its first line (`--at`, `session continue --to LINE`) or a `step` that lands there; not after `step --out`, not on a loop header's later passes.
- **Marks:** rows show `[settable]` on what `set` would change, fields and elements included (a preview's too); the fields of a local the debug information keeps in registers or as a static say `[not settable: register]` or `[not settable: static]`; when a rule leaves nothing settable, `session inspect` says why. A refused change's hint names the one way around it.
- **Later hits:** `--set-hit N` applies a probe's changes at hit N; in a session, change values at as many stops as you like.

The rest of this section shows them at work, then the detail behind them.

### Structs and Arrays of Structs

A struct, class or fixed-size array shows its first members, read from the debugger's own children (#136). In the sixth evaluation's `histo` case ([report](../evaluations/2026-10-20-agent-sixth-round.md); `struct bin bins[4]`, each `{lo, hi, count}`), one capture reads every bin:

```sh
peeky probe histo.c --at histo.c:28 --arg 0 --arg 7 --arg 7 --capture 'bins,bins[3]'
```

```
hit  phase     line  function  selector  type    value
1    observed  28    main      bins      bin[4]  [{lo = 0, hi = 2, count = 0}, {lo = 2, hi = 4, count = 0}, {lo = 4, hi = 6, count = 0}, {lo = 6, hi = 7, count = 0}]
1    observed  28    main      bins[3]   bin     {lo = 6, hi = 7, count = 0}
```

`session get bins` shows each element so, and its selector (`bins[3]: bin = {lo = 6, hi = 7, count = 0} (children: peeky session get 'bins[3]')`); fields are still read and set by name (`bins[3].hi`, `--set 'bins[3].hi=8'`).

- **Previews:** A preview shows 8 members or elements, then `…`, two levels deep (`{…}` below), at most 40 characters of one member. A union is not previewed (`<union>` inside another value: only one of its members holds what was stored); a pointer inside shows its address, never what it points at; a member with no value shows `<no value>`; a C++ base class shows as a member named by its class (`{Base = {b = 1}, d = 2}`); a standard container as below (`v = size=2 [1, 2]`), and any other `std::` value its debugger's summary or `{…}`.
- **No preview:** Types come from the build's debug information, and where it cannot vouch for one there is no preview, only the debugger's `T @ 0x…`: a local withheld at the stop, a variable-length array, an incomplete type (`op[0]` on `struct opaque *`), a map's entry (`m[#0]`, a `std::pair`), and every value of an optimized build.
- **Standard containers** (#181): a `std::vector`, `std::array`, `std::deque` or `std::list` shows its size and first elements (`have: size=3 ["users", "42", ""]`), with the bounds above; elements as their type shows (`size=2 [{x = 1, y = 2}, {x = 3, y = 4}]`, `size=2 [size=2 [1, 2], size=1 [3]]`).
  - **Maps and sets:** a `std::map` or `std::unordered_map` shows its entries in iteration order as `key: value` (`size=3 {1: 10, 2: 20, 5: 50}`, `{"a": {x = 1, y = 2}}`), a set (`std::set`, `std::multiset`, `std::unordered_set`, `std::unordered_multiset`) its elements (`size=2 {1, 3}`); a `std::multimap` keeps its summary (`size=2`). Selectors are as before: `m[1]` reads key 1, `s[#0]` the first element.
  - **Where they come from:** the debugger's children, which its formatter reads from the container's own storage, `size()` of them, each shown as the class template's argument in the build's debug information says (`_Tp`, `_Key`), never by LLDB's type names; a container the debugger lists otherwise (its own members, fewer elements than its size) keeps its summary. A `std::array` LLDB lists by its C array (`ai.__elems_`) shows that array's elements.
  - **References and pointers:** a reference shows its address, then the container (`0x… size=3 [1, 2, 3]`); a pointer its address and LLDB's summary (`0x… size=3`), and `p[0]` the container.
  - **Withheld:** a container not yet constructed is `no value yet`, as any local; an optimized build's shows its summary alone. A moved-from one shows what it holds: libc++ and libstdc++ leave it empty (`size=0 []`), as the program's `size()` says.
  - **A change under way:** a container shows its summary alone (`size=2`), and its elements no preview of their own (a struct element `Noisy @ 0x…`), where a standard-library function, or a constructor, destructor or assignment operator, may be part-way through changing it:
    - in a program running more than one thread: a stop pauses the others wherever they are (Peeky counts the threads lldb-dap lists, those LLDB's `thread list` lists and those that said they started, and withholds where any of them does not answer);
    - in a caller frame below a library function (`session get o.items --frame N` on `main`'s frame while `o.items.clear()` runs an element's destructor, the vector still counting an element already destroyed), or below a constructor, destructor or `operator=` of any class (`slot->~E(); ::new (slot) E(3);` in `main`, stopped in `E::~E()`);
    - for one reached through a pointer or reference (`this.owner[0].items`, a reference parameter, a structured binding of one, `auto &[items, n] = *owner`, a static local), wherever the stack holds such a frame (a `std::thread`'s or `std::function`'s too, or the frame's own destructor), or where Peeky cannot read it whole (1,024 frames or more, or a reply short of its `totalFrames`).
    - In all three, its elements, with everything read through them (`o.items[1].name`, `shared[0].x`, a map's entry), are `not shown`, with why (#187); with several threads, every container's are. Which container the call changes is not told, so every container the frame reads that way is withheld (`o.tags[1]` too); values not read through a container (`o.dropped`) read as before.
- **Characters:** A wide character array shows element by element (`[L'a', L'\0', L'b', L'\0']`, where LLDB's text stops at the first NUL), and a char-typed bitfield as its number (`sc = -3`, not LLDB's `'\x05'`).
- **Details:** Details: NATIVE.md, CPP.md.

```sh
peeky probe examples/c/pagination_bug.c --at 25 --capture 'start,page,items[1]' --set start=0
```

```
hit  phase      line  function  selector  type  value
1    observed   25    main      start     int   3  [settable]
1    observed   25    main      page      int   1  [settable]
1    observed   25    main      items[1]  int   20  [settable]
1    after-set  25    main      start     int   0  [settable]
after-set: 2 rows equal to the hit's observed value left out (`--json` has every row)
set start: 3 -> 0
status: completed  hits: 1  seconds: 0.745
output:
  PAGE:[10, 20, 30]
```

```sh
peeky probe examples/c/pagination_bug.c --at 25 --capture start,page --arg 2     # PAGE:[70, 80]
peeky probe examples/cpp/pagination_bug.cpp --at 28 --capture start
```

### Building

All `.c` files in the directory that don't define their own `main` are built together with the target. A directory holding several programs links only the target's `main`. A directory works as the target too: `peeky probe ring/ --at 16` finds its one program (a checker script beside it doesn't count), and `--at ring.cpp:16` names one when there are several. Headers read from outside the directory, including SDK headers, are hashed and rechecked.

### Locals Peeky Withholds

In short, a C or C++ local is withheld (`no value yet:` and why) when it is declared below the stop, in a running initializer, past a `case`/`goto` label, `this` in a constructor, hidden by a block's `static`/`extern`, or declared on the stop line where control enters it (`declared on line 3, which has not run yet`), not mid-line (after `step --out`). The cases in detail:

A C or C++ local declared below the stop line is not shown as a value: until its declaration runs, its storage holds whatever an earlier call or loop iteration left there, so the row says `no value yet: declared at line 18, below this stop` (the declaration the debugger shows, the one in the stopped function whose block holds the stop, is declared below it: another of the name, in an earlier loop or a lambda, changes nothing; where Peeky cannot tell which is shown, any declared below withholds it. A static or parameter keeps its value).

- **Where the stop cannot be placed:** Where Peeky cannot place the stop before the declaration (the line also runs code after it, as a temporary's cleanup does, or a `goto` label lets control return above it), it is withheld all the same, saying it may not be initialized.
- **`setjmp`:** In a function that calls `setjmp` (or a macro expanding to it), below the call (a stop on the call's own line is not: a `longjmp` returns past the call), a local declared at or below the stop is shown with a `note` instead (a `longjmp` can bring control back past its declaration with it live, or this is the first pass) and cannot be changed; what is declared above the stop, and every other function, is judged as usual.
- **Names a block declares:** Inside a block that declares an outer local's name otherwise (a `static`, `extern` or `typedef`, an enumerator, also one of an enum inside a C struct's body, a function declaration, a C++ `using` declaration, class or structured binding (`auto [t, u]`, also a range-`for`'s or an `if`'s, which clang 18 on Linux does not describe; a range-`for`'s counts in its loop, not in a lambda in its range, nor at the line's first instruction, where the range runs first), or a `thread_local`, which the debugger lists apart as `q @ file:line`; a block opening on the stop line counts), the code names that one: the outer local's row says `not what the code reads` and cannot be changed.
- **How the source is read:** Peeky reads the source as the build's compiler preprocesses it (`-E`, the same flags), so whatever a macro expands to, and only what a conditional keeps, counts; a prototype's parameter names do not. Where it cannot read that (the preprocessor fails, or a system header is included inside the block), the row says `may not be what the code reads`.
- **Block names in optimized builds (`-O1`, `-O2`, #173):** a block's declaration hides the outer local there too, though the outer local lives in a register or as a constant rather than in the frame.
  - On a range-`for`'s line, the stop cannot be told from the line's first instruction, where the range runs with the outer local: the row says `may not be what the code reads` and names the binding.
  - A block's `static` that is never changed is kept as a constant, and lldb-dap lists it among the frame's locals as `t @ file:line`. That row is the static the code names, so it is shown; the outer local's row says the static is `listed separately as` it.
- **On the stop line:** A local declared on the stop line itself is withheld at the line's first instruction, where its declaration has not run yet, so probe the next line to read it; on a line a loop returns to (a `do` body's first line) it is shown with a `note` that it may be the last pass's value, and mid-line (a `for` counter on later passes, after `step --out`) as its storage reads.
- **Part-way through the stop line:** A local declared on the stop line is withheld part-way through that line while the line still has code to run (back from a call with `step --out` in `long r = twice(3) + twice(4);`), except a variable a `for`, `if` or `switch` header initializes from literals alone, as it does each declarator before it in C (`for (int i = 0, j = 1; ...)`; in C++ only the first declarator counts, since constructing one before it may call), which a one-line loop keeps live. A `while`'s is withheld, and so is one with no initializer (`for (int i; ...)`), one whose initializer or an earlier declarator's calls or names anything (`if (long r = twice(3))`, or `int i = INIT`, where `INIT` may be a macro that calls), and one a jump can reach past its declaration (`goto inside;` to a label in the loop's body, a `case` of a `switch` around the loop, or any label once the source has a GNU `asm goto`, whose labels are operands).
- **While its declaration runs:** A local is also withheld while its own declaration is still running at the stop: `struct outer o = {` over lines 17-22 (or an array of structs, or a 2-D array), stopped at line 20, says `its declaration (lines 17-22) is still running at this stop` until its `;` has run. The end comes from the source, read as for caller frames ([5.1](#51-stop-look-change-finish)), and where it cannot be found the local is withheld; a declaration below the stop whose own line has no code is placed by its elements' lines.
- **Header variables:** A variable a `for`, `if`, `while`, `switch` or `catch` header declares has run with its part of the header, so the body reads it.
- **Below a label:** Below a label a jump can reach past a declaration, the local is withheld too (`a jump ... can reach the label on line 21`): a `case` or `default` of the `switch` whose body holds the declaration, or a label that a `goto` above the declaration names, or one outside its block jumps back to (re-entering a block starts its locals anew, C11 6.2.4; any label, with a computed `goto`). A `goto` counts only within its function's own lines, so a namespace's other functions do not.
- **Declared in an `#include`d file:** A local declared in an `#include`d file counts as declared at that `#include`'s line: below the stop it is withheld, and above it, when its file may `#include` more after it (a `case` there can skip it).
- **Outside the function's own lines:** When the frame (stopped, or a caller on its own file's line) runs a line outside its function's own (a class's default member initializer, as when you break on `long b = twice(2);` in `struct Cfg`, also a class defined inside the function, or an `#include`d fragment), the usual rules run from the own line that brings it in (that `#include`), or else from the last own line whose code comes before it; its class, struct and array locals are withheld too, since one of them is being built there, and where Peeky cannot place the stop, everything but parameters is.
- **`this` in C++:** In C++, `this` is withheld while its object is being built: in a constructor, in any frame a constructor called (Peeky reads up to 64 frames to tell; past that it withholds), in a lambda defined inside a declaration or a constructor that may still be running (the note names that declaration), and in a lambda a class's default member initializer defines (`long b = [this] { ... }();`), since a class with no constructor runs its initializers where the object is built. An unnamed struct's constructor (`$_0::$_0()`) counts as a constructor too.

### Members

In C++ and Java methods, read members through `this` (`this.head_`); a bare `head_` that is a member gives an error naming that selector. C++ changes no member through `this` (a pointer); Java does (`this.total=0`). Peeky never reads a member in place of a local: the debugger's list of locals can be incomplete (statics, classes built without local-variable tables), so a missing local does not mean the code uses the member.

### Strings with a NUL Byte (C++)

LLDB shows a libc++ (macOS) `std::string` whole, a NUL as `\0`: `"a\0b"`. Its summary of a libstdc++ (Linux) string is a C string, which ends at the first NUL, and a value LLDB names `std::string` gets `error: summary string parsing error` on clang's Linux builds. So on Linux the worker checks each string against its `_M_string_length` (#128), in `inspect`, `get`, listings, probe rows and the evidence store alike:

- With a debugger that reads memory (lldb-dap 20), it reads the string's bytes and shows them as LLDB shows a libc++ string (`"a\0b"  [memory]`; past 1024 bytes, `"..."...`), for `std::string`, `std::wstring`, `std::u16string` and `std::u32string`, by value, reference (`0x… "a\0b"`) or pointer.
- Without one (lldb-dap 18), a text the debugger cut at a NUL keeps a note: `"v" (shown up to a NUL at byte 1 of 3)`. A string whose summary failed has no value (`no readable value: the string's summary failed ..., and this debugger cannot read memory (lldb-dap 20 can); it holds 3 bytes`); a pointer or reference to one shows `0x… <no text: ...>`.
- A string the build's debug information gives no members (clang leaves `std::wstring` to libstdc++'s own) has no length to check: its text gets `(not checked for a NUL byte: ...)`, or no value where LLDB's summary failed.
- A string under a typedef's name (`typedef std::basic_string<char> Bytes`, `using Name = std::string`, a reference or pointer to one) is checked the same: Peeky resolves the typedef from the build's debug information, or knows the string by its libstdc++ members. LLDB's `error: summary string parsing error` is never shown as a value, for any type.
- On every platform, a literal reads back in C as the same bytes: where LLDB writes `\0` before a digit (`"n\064"`, which C reads as `"n4"`) or `\xff` before a hex digit, Peeky writes `"n\00064"` and `"\377a"`. Long text stays within 2000 characters, cut between characters with `...`.
- Each libstdc++ string costs a debugger request (two or three where its bytes are read), within the inspection's budget: a large page of strings may say `(not checked for a NUL byte: ...)` instead.
- A `std::string` local takes a text in double quotes (`--set 'name="apple"'`), within its capacity: see **Changing text** below. Its NUL bytes count as text there too: a new size is written with the bytes.
- A string is its text: `session get name` shows `name: std::string = "apple"` and no rows under it (the library's own members, `(anonymous struct)` or `_M_dataplus`, are never listed), and `inspect` offers no `(children: ...)` for it. Selecting inside one (`name[0]`, `name.size`) is refused with what to do instead: capture the string and compare its text, in SQL ([4.7](#47-c-references-and-pointers-in-sql)). A pointer to one shows its address and text, and `p[0]` is the string.

### Changing Text

A value in double quotes is a text (JSON's string syntax, so `\"` and `\u00e9` work), which Peeky writes in the storage the local already has, and only there (#137):

- **A `char[N]` array:** the text and a NUL, when both fit in its N bytes (the debug information gives N); the bytes after the NUL stay as they were, and the debugger shows them after `\0` (`set name: "apple :4" -> "apple\0:4"`). A text that does not fit is refused (`name` is a char[16], which holds 15 bytes of text and a NUL), as is one with a NUL in it.
- **A `std::string`:** the text, a NUL and the new size, in its own short buffer (libc++: 22 bytes; libstdc++: 15) or in its heap buffer, within its current capacity, never reallocating: `set name: "apple " -> "apple"`. Peeky reads where the text is held from the debugger's members and checks it against memory; a longer text is refused (`with room for 22 bytes, and the text is 30 bytes`), as is a layout that does not check out (a string not yet constructed). A heap buffer's room is the string's `capacity()`: libc++ keeps the allocation's size in `__cap_` (macOS arm64's layout) or half of it (its standard layout), and where its short buffer lies tells which.
- **Views taken before:** a `std::string_view`, a `c_str()` or `data()` pointer or an iterator the program took from the string before the change keeps the old size and reads a mix of old and new text (`string_view sv = s;` then `s="bye"`: `sv` reads `bye\0o world`). Peeky cannot see those, so every `std::string` change says so (`note` on the change in JSON, and a warning).
- **Debuggers:** lldb-dap 21 (macOS) writes memory, so every case above works. lldb-dap 18 and 20 cannot: Peeky writes an array's text one element at a time, for printable ASCII only, and a libstdc++ string's only in its short buffer and only with lldb-dap 20, which reads memory to tell where the text is (lldb-dap 18 cannot, so its strings are refused); a heap buffer needs lldb-dap 21. Rows say `[settable]` where a change can be made: with lldb-dap 20, a string whose text is in its own buffer.
- **Never** through a pointer or a reference: a `char *` may point at a string literal (read-only) or a buffer of unknown size, and a `const std::string &` at a string another frame owns. Never a `const` or `volatile` one either (`const std::string cs`, `const char tag[8]`, a typedef of one: the program may never change a const object), judged from the build's debug information, which also tells a typedef's array or string (`using Name = std::string` is changed as a `std::string`). Never into a field or element either (`rec.name`): copy it into a local with a candidate edit. All `--set` rules apply (a statement under way, a local not yet initialized, an optimized build).
- **Read back:** every byte written and the new size are read back; anything that does not read back as asked closes the session.

### Maps and Sets (C++)

The debugger lists a `std::map`'s entries as `[0]`, `[1]`, ... in iteration order, but in C++ `m[1]` is the value for key 1. Peeky reads `m[K]` as C++ does, and `m[#N]` is the Nth entry in iteration order (a pair with `.first` and `.second`), so a position is never taken for a key (#119). In `examples/cpp/maps/scores.cpp`, `m` is `std::map<int, int> m{{2, 20}, {1, 10}, {5, 50}}`:

```sh
peeky probe examples/cpp/maps/scores.cpp --at 9 --capture 'm[1],m[#1].first,m[#1].second,ages["al"],s[#0],s[1],m[3]'
```

```
hit  phase     line  function  selector      type       value
1    observed  9     main      m[1]          int        10
1    observed  9     main      m[#1].first   const int  2
1    observed  9     main      m[#1].second  int        20
1    observed  9     main      ages["al"]    int        42
1    observed  9     main      s[#0]         int        1
1    observed  9     main      s[1]                     <[1] not read: s (std::set<int>) is a C++ set, which has no [] in C++, and the debugger lists its elements in iteration order, not by index; select an element by position with s[#N] (`peeky session get s` lists them)>
1    observed  9     main      m[3]                     <[3] not found: m has no key 3 (keys: 1, 2, 5); in C++, m[3] would insert it with a default value>
```

- `m[K]` reads `std::map` and `std::unordered_map` (libc++ and libstdc++) with integer keys (`m[1]`, `m[-1]`; one outside the key type's range is refused) or `std::string` keys (`ages["al"]`; printable ASCII without `"` or `\`), matched exactly as Peeky shows each key (strings with a NUL byte below), and of the key's length. A key whose length cannot be read is refused. That holds through a typedef (`using Index = std::map<int, int>`, or a class's member typedef such as `Holder<T>::S`; the build's debug information names its class), a reference, a pointer (`mp[0][1]`, as C++'s `(*mp)[1]`), a struct (`h.m[1]`) and a vector of maps (`vm[0][4]`). A missing key is `not found`, with the keys there are; nothing is inserted.
- Keys of any other type (`double`, `char`, `bool`, enums, structs, `const char *`) are refused for `m[K]`: read those entries with `m[#N].first` and `.second`.
- A map with a comparator of its own (`std::map<int, int, ByAbs>`), or a hash or equality of its own, reads an exact key (`am[-1]`) but never says a key is missing: C++'s `am[1]` reads the entry whose key `ByAbs` treats as equal (`-1`), so `am[1]` says no key equals 1 exactly and names `am[#N]`. `not found ... would insert it` comes only with `std::less`, `std::greater`, or `std::hash` with `std::equal_to`.
- Sets, multisets and multimaps have no `[]` in C++: `s[1]` and `mm[1]` are refused, and `s[#1]`, `mm[#1].second` read by position. `t[1]` is refused too where `t`'s type is a typedef whose class the debug information does not give and the debugger lists pairs with a const key, as it does a map's entries: it could be a key or a position (`t[#1]` reads by position). So is a type the debug information gives several classes, a map or set among them (`std::map<int, std::set<int>>::mapped_type`, where other maps' `mapped_type` is `int`).
- `session get m` lists `m[1]: int = 10`, `m[2]: int = 20`, ... when the keys compare (each selector reads the value its row shows), and `m[#0]`, `m[#1]`, ... otherwise, with a note saying which. An entry whose key no selector can write (more than six digits, a string with other characters or a NUL) is listed by position, and no two entries share a label.
- A `std::map` with integer keys in the standard order (`std::less`, the default, or `std::greater`) and more than 20 entries is bisected, so any size up to 512 entries reads in a few steps. Any other map is compared entry by entry, and one inspection (512 nodes) holds the entries and keys of a map of about 200; a bigger one is refused before any is read (use `m[#N]`).
- Vectors, deques and arrays index by position as before.

In a session, select a map's entry by key the same way, quoted for the shell, and pass the session's `--name` to every command:

```sh
peeky session start examples/cpp/maps/scores.cpp --at 9 --name maps
peeky session get 'ages["al"]' ages --name maps
peeky session close --name maps
```

```
ages["al"]: int = 42
ages: std::map<std::string, int> = size=2
  ages["al"]: int = 42
  ages["bo"]: int = 31
  note: ages is a C++ map: ages[K] is the value for key K, as in C++ (integer and std::string keys), and ages[#N] its Nth entry in iteration order, a pair with .first and .second
```

### Changing Fields and Elements

`--set` (and `session set`) changes an integer field or array element of a local the stopped frame holds itself, as it does an integer local (#105):

```sh
peeky session set s.end=10        # set s.end: 2 -> 10
peeky session set 's.arr[2]=7'    # an element of an array in a struct
peeky session set ps.end=10       # MUTATION_REJECTED: `ps` is a pointer: Peeky changes only values the frame holds itself ...
```

### What `set` Checks

- **Path:** The path is checked against the build's debug information, not the debugger's type names: fields of structs and classes and elements of fixed-size arrays only, never through a pointer (including `this`), a reference, a base class or an anonymous member, nor inside a union, a packed struct or a `std::` type; the target is an integer of 16 to 64 bits, not const, volatile or a bitfield, and the value must fit it.
- **Through a pointer or a reference:** Peeky does not write through a pointer or a reference, since what it reaches may be freed or moved; change the value where it is owned, before the call that passed it: a probe `--at` the call's line with `--hits N --set-hit N`, or `session start --at` it with `--hit N`, where N counts the call line's own stops, not the called function's, which differ when it is called from several lines (find N with `peeky probe ... --at <call line> --capture <argument> --hits all`); `session continue --to` the call's line reaches only the next call.
- **The function's one caller (C):** In C, when the program's sources call the function once, the refusal names that call instead (`push has one caller: m.c:15 in main, which passes `&r`, where `r.tail` is settable: a probe's `--set-at 15@N r.tail=<value>` ...`).
  - It says settable, and how to change it, only where a stop on the first line of the call's statement can make the change and the call sees it: the call is in the first statement on that line (`r.tail = 1; total += push(&r, 1);` overwrites it first); its statement, read as the compiler preprocesses it (a macro's code where it is used), is `push(ARGS);`, `NAME = push(ARGS);` (`+=` and the like too), a declaration's `int NAME = push(ARGS);`, `return push(ARGS);` or `if (push(ARGS) ...)`, NAME not the root; and ARGS, and the rest of an `if`'s condition, only read: no assignment, `++`, `--` or `&` (but the address of another plain name, `&out`), no call of any kind (`atoi(s)`, `ops[0](r.buf)`, `(*fp)(x)`, `v.fn(x)`, `sizeof(x)`), the root only as the `&r` passed and in reads of its integer fields (`r.head`; not `r.buf`, a pointer into it).
  - Anything else may change the value first (`r = q, push(&r, 1)`, `push(&r, RESET(r))`, `push(&r, ops[0](r.buf))` through the array at `r`'s start), so the eval's `push(&r, atoi(argv[i] + 1))` is named alone.
  - Also, the caller takes no address of the root or its fields elsewhere in its body as preprocessed, a header included inside it too (a pointer `a = &r` may change it), neither with a builtin (any but those known to reach no memory, such as `__builtin_expect`: `__builtin_addressof`, `__builtin_frame_address` and `__builtin_dwarf_cfa` count) nor by naming an array in it without an index (`keep(r.buf)`; `r.buf[0]` and `sizeof r.buf` are fine), but to pass `&r` whole to a function of the same file that only uses its fields through it (`pop(&r, &v)` reading `r->count`, `r->buf[i]`); the root is declared above, and these rules allow the change at that stop (`int total = 1 +` / `push(&r, 1);` is named by its statement's first line, 10, whose stop comes after the call).
  - A call in another source file is changed by a probe `--at` that file's line (`--at other.c:6 --set q.tail=<value> --set-hit N`), as `--set-at` names a line of the probe's own file; one in a header cannot be stopped at.
  - Each source file's calls are its own: a header's function is compiled once in each file that includes it, and its macros may make those copies differ, so a call in a header two files include is two calls, and a function, or a caller, defined in more than one file gets the general advice. No settable claim is made where the owner's function, or a function on the way, has inline assembly (`asm`, `__asm__`), which may change any value it reaches.
  - **Passed on (#202).** Where the one caller passes its own pointer parameter on as is (`fits(lay, …)` in `wrap(const struct layout *lay, …)`), a change there is refused too, so the refusal follows the calls while each function has one caller and passes the same pointer on, to the same type, never pointing it elsewhere (no `lay = …`, `lay++` or `&lay`) and with no inline assembly or address builtin (`__builtin_addressof`), up to the frame that owns the struct: `fits has one caller: wrap.c:40 in wrap, which passes it on (`lay`, its own pointer parameter); wrap has one caller: wrap.c:75 in main, which passes `&lay`, where `lay.width` is settable: a probe's `--set-at 75@N lay.width=<value>` ...`. It says settable only where the rules above hold at the owner's call and each function on the way uses the pointer only for that call and to read integer fields through it (`lay->indent`); else it names the owner's call alone.
  - **Not followed:** where a function on the way has several or unknown callers, may point the pointer elsewhere or has inline assembly, or the caller passes a pointer it keeps or computes (`p`, `&a[i]`, `s->inner`), no frame is named for the change: the refusal says the caller passes it on (`…, and Peeky cannot single out one call of wrap`) and points to `describe`'s `mutable:` line, which lists the fields each function can set. A struct the caller does not hold in its frame (`&g`, a global; a static local) is never changed there: change what sets it with a candidate edit.
  - Otherwise the call is named alone.
  - It counts every mention of the function's name in the preprocessed sources, so a macro used twice is two calls, and any mention that is not a call by name, a prototype or the definition (its address taken) leaves the callers unknown; C++ keeps the general advice, as overloads and member calls make a call's target unclear from the text.
- **The root:** The root must be an automatic or a parameter of the stopped frame that Peeky does not withhold (declared below the stop, an initializer still running, a jump past it), nor shows with a note that its declaration still runs after the stop (a `do` body's first line), in an unoptimized build.
- **Same-named locals:** A local LLDB names with where it is declared, when another of its name is in scope, is selected as named (`'p @ s1.c:38.x=3'`, `'n @ s1.c:15=9'`; two of one name declared on the same line are ambiguous, and `get` and `set` refuse them).
- **A plain name** means the declaration the code names at the stop, as C and C++ scope it: where LLDB lists an inner block's `t` and an outer one as `t @ same.c:22` and `t @ same.c:20`, `t` reads and changes the inner, and the outer before the inner's declaration and after a `while`'s or a `switch`'s own `t`, or a `t` that is a loop's or a `switch`'s whole body without braces (`while (c) long t = 8;`) (whose scope ends with its statement, though the debug information keeps it to the end of the block; scopes are read from the source as the compiler preprocessed it, so a macro's `while (decl)` counts too).
- **When Peeky cannot tell:** Where Peeky cannot tell which one the code names (the source does not say where a scope ends, two are as inner), `t` is refused and the error lists the `t @ file:line` names, which select.
- **One name, different types:** Where the function declares one name with different types (a loop's `Task t` and a later range-`for`'s `const Task &t`, or `s` in sibling blocks), the path is checked against the type of the declaration the debugger shows at the stop, the one whose block holds it, and `describe`'s `mutable:` line names such locals; two declared on one line cannot be told apart, and a change to them is refused. A probe is refused for it before the program runs unless the line's breakpoint location (its lowest address that runs before the function returns, not an exception's landing pad) singles one out; LLDB's later stops on the line, one per other block (#157), are part-way through it, where no change is made, and the refusal says where one of them names another declaration.
- **Caller frames:** A caller frame (`--frame 1`) is judged where its call is, the address before the one it returns to, as LLDB reads it; a frame a signal interrupted (above the system's `_sigtramp` or `__restore_rt`, not a function of the program's named so), at the instruction it stopped at.
- **Arrays:** An element of an array of any length is fetched alone, at any step of the path (`big[700].a=1`, `grid[599][2]=3`).
- **Read back:** The field is read back from the debugger after the write, and only the value asked counts as a change.
- **Marks:** Probe rows and `session get` listings mark the fields and elements `set` would change `[settable]`, judged by the same rules at the stop (`bins[3].hi  [settable]`, never `ps.end`); a refusal names the rule it broke, and its hint the one way around it.

What a change makes true, for a local or a field: its storage holds the new value, and the program's later reads of it see it. What the program copied before keeps the old value: a copy on an earlier line (`int a = s.x;`), and a statement under way at the stop, which may hold what it read in a register. So C and C++ allow a change only at a statement's first instruction, and refuse it anywhere else: `set` says `cannot be changed here` and keeps the session, and `inspect` drops `[settable]` there and says why (`note: nothing here is [settable]: the stop is not at the first instruction of the statement on line 9 ...`; `unsettable` in JSON). Peeky proves the stop is there from the source and the line table:

- the stop is on one of its function's own lines, and that line starts a statement: every bracket around it, up to the function's body, is a block of statements (not a call's arguments, an initializer, a `new long[1]{`, a compound literal; nothing inside a GNU statement expression `({ … })` at all, even a macro's loop there), no directive or bracket-hiding macro lies between it and the previous token, and that token ends a statement (`;`, `{`, `}`, a label, `else`, the `)` of an `if`/`for`/`while`/`switch` header). A brace after something like parameters (`REPEAT(k, 1) {`) is a function's body only at file scope or in a class's or namespace's body; elsewhere it is a block of the function around it;
- the pc is the lowest address of all the function's code on the lines of the statements starting there: clang may put a later line's code first (`t =` / `p.x + g();` loads `p.x` and calls `g` before line 1's store);
- on a function's first line (a one-line function or lambda), the statements are those after its body's `{`, and the pc is the lowest of their rows past the function's entry (`long one(struct P p) { long t =` / `p.x + g();` runs the second line first, so neither line qualifies);
- no call in those statements is one the compiler inlined (`always_inline`, `flatten`; Peeky reads which from the debug information): an inlined body's code carries its own function's lines and runs before the statement's lowest row (`t = inl(&p) + p.y;`).

A breakpoint on a statement's first line (`--at`, `continue --to`) or a `step` that lands there qualifies.

- **Refused:** Back from a call (`step --out`, a return address), on a statement's later line (`+ f();` under `int t = s.a`), on a loop header's later passes, in a K&R-style definition (`long f(a) long a; {`, not supported), and wherever Peeky cannot tell, the change is refused. A `step` onto `} else if (p.y > 1) {` on the else path is refused too: that line's lowest address is the jump that ends the `if` block, so the step stops past it, at the condition (and a breakpoint on that line, placed at the jump, is never hit on the else path, with or without Peeky); change the value before the `if`, or at the first statement inside the `else if` block.
- **Also refused:** Two more stops are refused though nothing is under way there: in an inlined function's own code (a one-line `always_inline` function's line, where its frame stops), and on a body's `{` placed on the line under its signature with a statement after it (`long f(struct P p)` / `{ long t = p.x + 1;`); change the value in the caller, or at the next statement.
- **Function-try-blocks:** A function-try-block (`long f(P p) try { … } catch (…) { … }`) is a function body like any other, its handlers too.
- **Overwrites:** The stop's own line may still overwrite the change (`s = (struct tri){9, 9, 9};`).
