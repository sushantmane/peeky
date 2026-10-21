# Peeky for C and C++

What is particular to C and C++ targets, one rule per line, each with the [COOKBOOK](../COOKBOOK.md) section that has the details. Read [AGENT_GUIDE.md](../AGENT_GUIDE.md) first: the loop and the rules every language shares are there.

## Targets

- A `.c` or `.cpp` file, or a directory holding one program (`peeky probe ring/ --at 16`; `--at ring.cpp:16` names one when there are several). The directory's other `.c` files that define no `main` are built with it ([7.1](../COOKBOOK.md#71-c-and-c)).
- `describe` lists each function's lines and names with their types (C: `struct ring` for a struct with no typedef; C++: `std::vector<std::string>`, as code writes it), leaving out the compiler's own (a range `for`'s `__range1`) ([1.1](../COOKBOOK.md#11-what-can-i-ask)).
- Program arguments go after `--` or in repeated `--arg` ([2.5](../COOKBOOK.md#25-a-module-imported-by-a-script-with-arguments)).

## Reading values

- Selectors: `s.end`, `bins[3].hi`, through a pointer with `.` (`ps.start`), C++ members as `this.head_`, a map's value by key (`m[1]`) and its first entry as `m[#0]` ([2.1](../COOKBOOK.md#21-fields-and-indexes), [7.1](../COOKBOOK.md#71-c-and-c)).
- Structs, arrays and standard containers show their first members (`{lo = 0, hi = 2}`, `size=3 ["users", "42", ""]`); unions and pointer targets are not read, and a container something may be part-way through changing (a library call, a constructor or destructor, another thread) shows only its size ([7.1](../COOKBOOK.md#71-c-and-c)).
- **`no value yet`** is a local not initialized at this stop: read it below its declaration (the row says where). A **`note`** is a value Peeky cannot vouch for: on a line a loop returns to (a `do` body's first) the next line reads it; below a `setjmp` call, a local declared at or below the stop shows a `note`, and reads plainly below its declaration ([7.1](../COOKBOOK.md#71-c-and-c)).
- **Same-named locals:** a plain name is the declaration the code names at the stop; `name @ file:line` selects another ([7.1](../COOKBOOK.md#71-c-and-c)).
- **Several stops:** a line with several breakpoint locations stops at each, and rows carry `stop`: a macro's code at each location every pass (1, 2, 1, 2); a one-line loop whose body declares a local, or a range `for`, once before the loop, then once per pass (1, 2, 2, 2); a one-line loop whose body declares no local has one location and stops once in all. For one row per pass, put `--at` on a plain statement line in the body; a loop header may stop only once, before its loop ([2.2](../COOKBOOK.md#22-every-iteration-of-a-loop)).
- A step can land on a closing `}`, a loop header, or back on an `if`'s line ([1.1](../COOKBOOK.md#11-what-can-i-ask)).
- A reference or pointer is saved with its address first (`0x000000016d78b298 "apple"`): compare or group such values by `no_addresses(value)`, or take the text after the address ([4.7](../COOKBOOK.md#47-c-references-and-pointers-in-sql)).

## Changing values

- `--set` changes a 16- to 64-bit integer local, or such a field or element of a struct or array local the stopped frame owns (`--set s.end=10`), at a statement's first line; rows mark it `[settable]`, and an 8-bit `char` is refused ([7.1](../COOKBOOK.md#71-c-and-c)).
- A text in double quotes (`--set 'name="apple"'`) goes into a `char[N]` or `std::string` local within its size, never reallocating; not through a `char *` or a reference, nor into a const one. lldb-dap 21 (macOS) writes every case, older ones fewer ([7.1](../COOKBOOK.md#71-c-and-c)).
- Every `std::string` change warns that a `string_view`, `c_str()` or `data()` pointer or iterator taken from it before keeps the old size: Peeky cannot see those ([7.1](../COOKBOOK.md#71-c-and-c)).
- A field through a pointer (`r->tail` in `push(struct ring *r)`, `this.count`), or a structured binding of a reference (`auto &[at, n] = *owner`: `n`, `at.x`), cannot be set: set it in the frame that owns the struct, before the call; in C, when the program calls the function once, the refusal names that call ([7.1](../COOKBOOK.md#71-c-and-c)).

## Returns

- `--capture return` shows integers, `bool`, characters, floats and pointers exactly; a struct, a string or any return in an optimized build is withheld, saying why ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
- `session step --out` shows with the stop what the function returned (`(return): int = 42`), or a withheld row saying why, as `--capture return` does (an exception or `longjmp` that ended the call included); nothing where another stop interrupted the step first, or the program ended. `session get return` reads it again ([2.9](../COOKBOOK.md#29-what-a-function-returned)).

## Builds

- `"optimization": "O2"` in a `--config` debugs the optimized program: locals may be `unavailable`, shown values can be stale, and changes are refused; every result carries `optimized` and a warning ([7.5](../COOKBOOK.md#75-optimized-builds)).
