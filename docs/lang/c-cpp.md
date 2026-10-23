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

- unordered containers show no entries, unless LLDB's count is the container's own;
- a lambda's captures can't be read: they are `not found` in the lambda;
- a one-line function's parameters can show `no value yet` at a stop on its line;
- a `const` local aggregate GCC builds at compile time is in read-only data, and `not found`;
- GDB's DAP isn't supported.

## Choose the target

- Give a `.c` or `.cpp` file, or a directory that holds one program: `peeky probe ring/ --at 16`.
  If the directory holds several programs, name the file in `--at`: `--at ring.cpp:16`
  ([C and C++ 7.1](c-cpp-cookbook.md#build-a-program-from-several-files)).
- The directory's other `.c` files that define no `main` are built with the target
  ([C and C++ 7.1](c-cpp-cookbook.md#build-a-program-from-several-files)).
- `describe` lists each function's lines, and its names with their types. C writes a struct with
  no typedef as `struct ring`. C++ writes types as code does: `std::vector<std::string>`. The
  compiler's own locals, such as a range `for`'s `__range1`, are left out
  ([C and C++ 1.1](c-cpp-cookbook.md#11-what-can-i-ask)).
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
- Write a name as Clang keeps it, non-ASCII too: `größe.zähler`, `--set ñ=3`. `größe` in NFC and
  in NFD are two different locals, each selected by its own spelling. When two names are equal
  under NFKC, `inspect` marks each row with how it is written
  ([2.1](../COOKBOOK.md#21-fields-and-indexes)).
- Structs, arrays and standard containers show their first members: `{lo = 0, hi = 2}`,
  `size=3 ["users", "42", ""]`. Unions and what a pointer points to are not read
  ([C and C++ 7.1](c-cpp-cookbook.md#read-structs-arrays-and-standard-containers)).
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
- A binding by reference, such as `auto &[t, u]`, is read only where it names a local of the frame,
  or a member of one. Otherwise its row is `not shown`, because what it refers to may be gone
  ([C and C++ 7.1](c-cpp-cookbook.md#what-is-different-in-a-gcc-build)).
- A pointer captured with an array at the same stop says which element it points at, where Peeky
  proves it: `[points at level[6]]`, or `[level + 96: one past its end]`. The array must be one,
  not a pointer such as a parameter
  ([C and C++ 2.1](c-cpp-cookbook.md#which-element-a-pointer-points-at)).
- Where the array is a pointer, such as a parameter, a pointer says how far it is from it by
  address alone: `[level + 6 by address; same array not proven]`. It names no element
  ([C and C++ 2.1](c-cpp-cookbook.md#how-far-a-pointer-is-from-a-pointer-parameter)).
- `--json` rows and the store give `points_into` and its element's index, or `offset_from` and
  its offset, in columns of their own. Pointer and array rows give `element_size`, the bytes of
  one element.
- `address(value)` reads that address as an integer. To see which element a pointer points at in
  another run, join the runs on it minus a base each run has
  ([4.8](../COOKBOOK.md#48-compare-addresses-across-runs)).
- `queries diff` compares the elements a pointer holds where both runs captured the array.
  ``another element of `level`: index 6 in run a, 4 in run b`` is a real difference. The same
  element is `addresses only` ([C and C++ 4.8](c-cpp-cookbook.md#which-element-in-each-run)).
  It compares offsets from the same base pointer the same way, and names no element there.
- Otherwise `diff` never calls a pointer's other address `addresses only`. It says
  `addresses differ: it may point at another element too`, right after the real differences
  ([3.1](../COOKBOOK.md#31-two-inputs-side-by-side)). Where the text after the address reads the
  same in both runs, as a `const std::string &` that reads `"Maya"`, it is `addresses only`.

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

## Builds

- A probe whose build fails is refused before launch: `BUILD_FAILED`, exit 2, no table, and no run
  saved. That includes a candidate edit that doesn't compile. `verify` calls it a failed verify,
  exit 1 ([ERRORS](../ERRORS.md)).
- To debug the optimized program, put `"optimization": "O2"` in a `--config`. Locals may then be
  `unavailable`, shown values can be stale, and changes are refused. Every result carries
  `optimized` and a warning ([7.5](../COOKBOOK.md#75-optimized-builds)).
