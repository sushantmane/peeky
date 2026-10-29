# Peeky for C and C++

This page lists what is different for C and C++ targets, one rule per line. Each rule links to
its details, in the [C and C++ cookbook](c-cpp-cookbook.md) or the [COOKBOOK](../COOKBOOK.md).
Read [AGENT_GUIDE.md](../AGENT_GUIDE.md) first: it has the steps and rules every language shares.

Peeky builds C and C++ with clang or GCC, and debugs both with lldb-dap, LLVM's debugger. It uses
clang unless `PEEKY_NATIVE_CC` or `PEEKY_NATIVE_CXX`, or a target's `compiler`, names another
compiler. A C++ compiler that is neither clang nor GCC is refused before anything runs
([C and C++ 7.1](c-cpp-cookbook.md#which-c-builds-peeky-supports)).

**GCC's C++ builds have known gaps.** Peeky withholds what LLDB misreads, and the row says why
([C and C++ 7.1](c-cpp-cookbook.md#what-is-different-in-a-gcc-build)):

- unordered containers are read from their own nodes, except entries that hold floating-point
  numbers, characters, enums, pointers or other containers;
- a one-line function's parameters can show `no value yet` at a stop on its line;
- GDB's DAP isn't supported.

## Choose the target

- Give a `.c` or `.cpp` file, or a directory that holds one program: `peeky probe ring/ --at 16`.
  If the directory holds several programs, name the file in `--at`: `--at ring.cpp:16`
  ([C and C++ 7.1](c-cpp-cookbook.md#build-a-program-from-several-files)).
- The directory's other `.c` files that define no `main` are built with the target
  ([C and C++ 7.1](c-cpp-cookbook.md#build-a-program-from-several-files)).
- `describe` lists each function's lines, and its names with their types. C writes a struct with
  no typedef as `struct ring`. Rows write LLDB's name without the tag: `found` is
  `struct water[96]` in `describe` and `water[96]` in rows. C++ writes types as code does:
  `std::vector<std::string>`. The compiler's own locals, such as a range `for`'s `__range1`, are
  left out ([C and C++ 1.1](c-cpp-cookbook.md#11-what-can-i-ask)).
- Pass program arguments after `--`, or with repeated `--arg`
  ([2.5](../COOKBOOK.md#25-a-module-imported-by-a-script-with-arguments)).
- To pin the compiler and debugger, name them in a `--config`
  ([C and C++ 7.1](c-cpp-cookbook.md#name-the-tools-in-a-config)).
- Peeky launches the program. It doesn't attach to a running one, evaluate expressions, or deploy
  a remote debug server ([WORKER](../WORKER.md#what-the-c-and-c-worker-does)).
- To debug on another host, run a worker there with `--native-config`. Then add `--worker URL`
  ([REMOTE](../REMOTE.md#c-c-and-rust-on-the-worker-host)).
- Through a worker, every file in the config's `sources` and `headers` is compared with yours
  before the build. One that differs is refused by name ([REMOTE](../REMOTE.md#source-identity)).

## Read values

- Selectors look like C: `s.end`, `bins[3].hi`. Follow a pointer with `.`, not `->`: `ps.start`
  ([C and C++ 2.1](c-cpp-cookbook.md#21-fields-and-indexes)).
- Read C++ members through `this`: `this.head_`. Read a map's value by its key, `m[1]`, and its
  first entry as `m[#0]` ([C and C++ 2.1](c-cpp-cookbook.md#21-fields-and-indexes),
  [C and C++ 7.1](c-cpp-cookbook.md#71-c-and-c)).
- Read a lambda's capture by its name in a GCC build, and as `this.count` in a clang build. None
  can be changed ([C and C++ 7.1](c-cpp-cookbook.md#read-a-lambdas-captures)).
- Write a name as Clang keeps it, non-ASCII too: `größe.zähler`, `--set ñ=3`. `größe` in NFC and
  in NFD are two different locals, each selected by its own spelling. When two names are equal
  under NFKC, `inspect` marks each row with how it is written
  ([2.1](../COOKBOOK.md#21-fields-and-indexes)).
- Structs, arrays and standard containers show their first members: `{lo = 0, hi = 2}`,
  `size=3 ["users", "42", ""]`. Unions and what a pointer points to are not read
  ([C and C++ 7.1](c-cpp-cookbook.md#read-structs-arrays-and-standard-containers)).
- An array shows its declared length, whatever the program wrote. Past what it filled, elements
  are leftovers: capture the count, and read only below it
  ([C and C++ 7.1](c-cpp-cookbook.md#what-a-preview-shows)).
- A container that something may be changing right now shows only its size. Its elements are
  `not shown`, with the reason. That something can be a library call, a constructor or destructor,
  or another thread
  ([C and C++ 7.1](c-cpp-cookbook.md#read-structs-arrays-and-standard-containers)).
- **`no value yet`** means the local isn't initialized at this stop. Read it below its
  declaration; the row says where
  ([C and C++ 7.1](c-cpp-cookbook.md#why-a-local-shows-no-value-yet)).
- **A GCC build's one-line function** can stop at its first instruction, before it stores its
  parameters. There, its parameters and locals show `no value yet`, and none can be changed. Read
  the arguments at the call, or put the body on a line of its own. The note says what to do
  ([C and C++ 7.1](c-cpp-cookbook.md#at-a-functions-start-before-it-stores-its-parameters)).
- **A `note`** marks a value Peeky can't vouch for. Don't trust it, or anything read through it.
  On a line a loop returns to, such as a `do` body's first line, read it on the next line. Below a
  `setjmp` call, a local declared at or below the stop shows a `note`; read it below its
  declaration ([C and C++ 7.1](c-cpp-cookbook.md#why-a-local-shows-no-value-yet)).
- **A `static` local** reads where the code at the stop names it: in its block, below its
  declaration. In C++, it is `no value yet` until its initializer has run. It is never changed
  ([C and C++ 7.1](c-cpp-cookbook.md#read-a-static-local)).
- **A caller's local:** a probe reads it as `1:pass`. It is withheld while its declaration's
  call runs, as is a `for` loop's own variable while its body calls
  ([C and C++ 5.1](c-cpp-cookbook.md#read-a-callers-local),
  [2.10](../COOKBOOK.md#210-values-in-a-callers-frame)).
- **Same-named locals:** a plain name means the declaration the code names at the stop. To select
  another one, write `name @ file:line`
  ([C and C++ 7.1](c-cpp-cookbook.md#same-named-locals-which-one-a-name-means)).
- **Several stops:** a line with several breakpoint locations stops at each one. Rows carry their
  `stop` number ([2.2](../COOKBOOK.md#22-every-iteration-of-a-loop)):
  - a macro's code stops at each location on every pass: 1, 2, 1, 2;
  - a one-line loop whose body declares a local, or a range `for`, stops once before the loop,
    then once per pass: 1, 2, 2, 2;
  - a one-line loop whose body declares no local has one location, and stops once in all.
- To get one row per pass, put `--at` on a plain statement line in the loop's body. A loop header
  may stop only once, before its loop ([2.2](../COOKBOOK.md#22-every-iteration-of-a-loop)).
- A step can land on a closing `}`, on a loop header, or back on an `if`'s line
  ([1.1](../COOKBOOK.md#11-what-can-i-ask)). Past a block's end, `get` of its locals says that
  the stop is outside their scope ([5.2](../COOKBOOK.md#52-stepping-and-running-to-a-line)).
- A reference or pointer is saved with its address first: `0x000000016d78b298 "apple"`. To compare
  or group such values, use `no_addresses(value)`, or take the text after the address
  ([C and C++ 4.7](c-cpp-cookbook.md#47-c-references-and-pointers-in-sql)).
- A structured binding of a pair or tuple shows its element, never an address: `t = 10` in
  `auto [t, u] = p;`. GCC and newer clang hold each name as a reference. The row's `type` is the
  binding's, and `debugger_type` is LLDB's reference
  ([C and C++ 7.1](c-cpp-cookbook.md#what-is-different-in-a-gcc-build)).
- A binding by reference, such as `auto &[t, u]`, is read only where Peeky proves that what it
  refers to is alive. That is a local of the frame or a member of one, or an element of a local
  vector, map or set of numbers the code leaves in place. A call's result is never read.
  Otherwise its row is `not shown`, because what it refers to may be gone
  ([C and C++ 7.1](c-cpp-cookbook.md#what-is-different-in-a-gcc-build)).
- A pointer captured with an array at the same stop says which element's address it holds, where
  Peeky proves it: `[at level[6]'s address]`, or `[at the address one past level's end]`. The
  array must be one, not a pointer such as a parameter. Where several captured arrays share the
  address (a union's), it says none
  ([C and C++ 2.1](c-cpp-cookbook.md#which-elements-address-a-pointer-holds)).
- That is an address fact. Another object can have the same address, so it never says which
  object the pointer designates.
- A pointer one past an array's end, such as `end = queue + 4`, is never read through: nothing
  there belongs to the array. Its row says `[at the address one past queue's end]`, even read
  alone, and `end[0]` says why it isn't read
  ([C and C++ 2.1](c-cpp-cookbook.md#a-pointer-one-past-an-arrays-end)).
- Peeky finds that array in the locals of each listed frame, and in the arrays they hold.
- What any other pointer points at is read. Where Peeky can't show it is a live object, it is
  marked `[unproven]`, with a note that says why: into a call that returned, a heap block, or past
  an array. A live heap block and a freed one read alike
  ([C and C++ 2.1](c-cpp-cookbook.md#when-what-a-pointer-points-at-is-unproven)).
- It is proven live inside a local, an array, a struct or a base class of a listed frame, or a
  global, of its type. A `char *` may also point into a string literal or `main`'s arguments. A
  union's member is never proven, only the union. The pointer's own row is its address, unmarked, but a `char *`'s
  text is marked. `queries diff` ranks such values as qualified.
- Where the array is a pointer, such as a parameter, a pointer says how far it is from it by
  address alone: `[level + 6 by address; same array not proven]`. It names no element. At the
  same address, it says ``[same address as `level`]``
  ([C and C++ 2.1](c-cpp-cookbook.md#how-far-a-pointer-is-from-a-pointer-parameter)).
- A value read straight through a pointer, such as `p[0]` or `node.qty`, names the other captured
  pointers of its type at that address: ``[read through `p`, which holds the same address as `top`]``.
  Peeky can't know which elements the program still counts, so it never calls a value left over
  ([C and C++ 2.1](c-cpp-cookbook.md#a-value-read-at-another-pointers-address)).
- `--json` rows and the store give `at_element_of` and its element's index, or `offset_from` and
  its offset, in columns of their own. Pointer and array rows give `element_size`, the bytes of
  one element. `--json` rows also give `read_through`.
- `address(value)` reads that address as an integer. To see which element's address a pointer holds
  in another run, join the runs on it minus a base each run has
  ([4.8](../COOKBOOK.md#48-compare-addresses-across-runs)).
- `queries diff` compares which element's address a pointer holds where both runs captured the
  array. ``the address of `level[6]` in run a, of `level[4]` in run b`` ranks first among the
  addresses that differ: the pointer, or only the array, may have moved. A note names its hit
  where it comes before the first real difference. The same element's is `addresses only`
  ([C and C++ 4.8](c-cpp-cookbook.md#which-element-in-each-run)).
- Offsets from a base pointer are shown too, but rank with the addresses that differ: two
  pointers can point into separate blocks.
- Otherwise `diff` never calls a pointer's other address `addresses only`. It says
  `addresses differ: it may point at another element too`, right after the real differences
  ([3.1](../COOKBOOK.md#31-two-inputs-side-by-side)). Where the text after the address reads the
  same in both runs, as a `const std::string &` that reads `"Maya"`, it is `addresses only`.

## Read unordered maps and lambdas in a GCC build

- An unordered map or set is read from its own nodes, as `[memory]`: `on_hand  Stock  size=4
  {"HX-88": 0, "GS-5": 0, …}`. LLDB alone would count it as empty
  ([C and C++ 7.1](c-cpp-cookbook.md#standard-containers)).
- That holds for entries of integers, bools and strings, alone or in pairs and structs, up to 256
  entries. Otherwise the row is `not listed`, with why. Then print the entries from the program,
  or try an edit.
- Read one entry by key, `on_hand["GS-5"]`, or by position, `on_hand[#0]`. A preview shows the
  first few entries. A `preview:` line under the table says how to read the rest
  ([C and C++ 7.1](c-cpp-cookbook.md#read-a-c-map-or-set-by-key)).
- Inside a lambda, read a capture by its name: `count`, not `this.count`. `session inspect` lists
  the captures under `[Captures]`. A capture by reference shows what it refers to
  ([C and C++ 7.1](c-cpp-cookbook.md#read-a-lambdas-captures)).
- A capture by value is the lambda's copy, made when the lambda was. The outer variable may have
  changed since. Read it in its own frame, such as with `session get --frame 1`.
- A map ordered by a lambda shows the compiler's name for it:
  `std::map<int, long, main()::<lambda(int, int)>>`. LLDB's `key_compare` stays in
  `debugger_type` ([C and C++ 7.1](c-cpp-cookbook.md#maps-that-compare-keys-their-own-way)).
- With lldb-dap 18, LLDB prints an error about `std::string`'s `_M_local_buf`. Peeky moves it out
  of the output, with a warning: whole on a build's first probe, then short. The output stays
  `output so far`, since lldb-dap 18 crashes as it ends
  ([C and C++ 7.1](c-cpp-cookbook.md#71-c-and-c)).

## Change values

- `--set` changes a 16- to 64-bit integer local, at a statement's first line. It also changes such
  a field or element of a struct or array local that the stopped frame owns: `--set s.end=10`.
  Rows mark what it can change `[settable]`. An 8-bit `char` is refused
  ([C and C++ 7.1](c-cpp-cookbook.md#71-c-and-c)).
- A text in double quotes goes into a `char[N]` or `std::string` local, within its size:
  `--set 'name="apple"'`. Peeky never reallocates. It never writes through a `char *` or a
  reference, nor into a const local ([C and C++ 7.1](c-cpp-cookbook.md#change-a-text)).
- lldb-dap 21 (macOS) can write every case of text. Older versions write fewer
  ([C and C++ 7.1](c-cpp-cookbook.md#change-a-text)).
- Every `std::string` change warns about views taken before it. A `string_view`, a `c_str()` or
  `data()` pointer, or an iterator keeps the old size, and Peeky can't see them
  ([C and C++ 7.1](c-cpp-cookbook.md#change-a-text)).
- A field through a pointer can't be set: `r->tail` in `push(struct ring *r)`, or `this.count`.
  Nor can a structured binding of a reference: `n` and `at.x` in `auto &[at, n] = *owner`. Nor can
  one of a pair or tuple, which is read through a reference. To change one, set it in the frame
  that owns the struct, before the call
  ([C and C++ 7.1](c-cpp-cookbook.md#change-a-field-behind-a-pointer)).
- In C, when the program's sources call the function only once, the refusal names that call. It
  follows a pointer that a caller only passes on, such as `fits(lay, …)` in `wrap`. It names the
  frame that owns the struct, and its call: `main` at `wrap(&lay, …)`. If it can't, it says so
  and points to `describe`'s `mutable:` line
  ([C and C++ 7.1](c-cpp-cookbook.md#change-a-field-behind-a-pointer)).

## See what a function returned

- `--capture return` shows integers, `bool`, characters, floats and pointers exactly. A struct, a
  string, or any return in an optimized build is withheld, with the reason
  ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
- A line can stop again in the same call before the call returns, such as a loop's next pass or a
  macro's block. Each of those hits gets the call's return only where the step out's watch proves
  that the stop is in the same call. Otherwise the earlier hit's return is withheld
  ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
- `session step --out` shows what the function returned with the stop: `(return): int = 42`. Or
  it shows a withheld row with the reason, as `--capture return` does. That includes a call that
  an exception or `longjmp` ended ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
- It shows nothing when another stop interrupted the step first, or when the program ended.
  `session get return` reads the value again ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
- A `void` function returns no value: `(return): void (no value: the function returns nothing)`.
  A probe's row has the type `void` ([2.9](../COOKBOOK.md#29-what-a-function-returned)).

## Builds

- A probe whose build fails is refused before launch: `BUILD_FAILED`, exit 2, no table, and no run
  saved. That includes a candidate edit that doesn't compile. `verify` calls it a failed verify,
  exit 1 ([ERRORS](../ERRORS.md)).
- To debug the optimized program, put `"optimization": "O2"` in a `--config`. Locals may then be
  `unavailable`, shown values can be stale, and changes are refused. Every result carries
  `optimized` and a warning ([7.5](../COOKBOOK.md#75-optimized-builds)).
