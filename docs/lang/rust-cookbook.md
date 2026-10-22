# Peeky Cookbook: Rust

What is particular to Rust in Peeky's recipes. Read the [COOKBOOK](../COOKBOOK.md) for the recipes every language shares: each section here has the number and title of the COOKBOOK section it adds to. The one-page summary of Rust's rules is [rust.md](rust.md).

## Contents

- [1.1 What Can I Ask?](#11-what-can-i-ask)
- [2.1 Fields and Indexes](#21-fields-and-indexes)
- [6.6 See What a Fix Does Before Making It](#66-see-what-a-fix-does-before-making-it)
- [7.2 Rust (a Cargo crate)](#72-rust-a-cargo-crate)
  - [Quick Start](#quick-start)
  - [Examples and Details](#examples-and-details)

## 1.1 What Can I Ask?

Adds to [COOKBOOK 1.1](../COOKBOOK.md#11-what-can-i-ask).

- **Rust:** In Rust every `let` opens a block, so a local shows the lines after its `let`, where it is visible (#107). Bindings a macro from another crate declares (`println!`'s `args`, `core::fmt::rt::Argument[1]`) are left out: the build's MIR places them in the macro's own file. A macro of your crate's keeps its bindings, `#[collapse_debuginfo(yes)]` too.
- **Rust's names:** types are spelled as the program spells them (`Vec<u32>`, `String`, `Option<i32>`), here and in probe rows, where `debugger_type` keeps LLDB's (`alloc::vec::Vec<unsigned int, alloc::alloc::Global>`).
- **Nothing settable:** for Rust, the line ends with the functions where nothing can change (every local in a register, behind a reference, or a container's contents), and the way around it: a candidate edit, which can also keep a local in memory ([6.6](#66-see-what-a-fix-does-before-making-it)).

## 2.1 Fields and Indexes

Adds to [COOKBOOK 2.1](../COOKBOOK.md#21-fields-and-indexes).

- **Rust:** Rust references and Boxes are never indexed this way (`p[0]` for what a C or C++ pointer points at: [C and C++ 2.1](c-cpp-cookbook.md#21-fields-and-indexes)).

## 6.6 See What a Fix Does Before Making It

Adds to [COOKBOOK 6.6](../COOKBOOK.md#66-see-what-a-fix-does-before-making-it).

- A Rust local kept in a register can be kept in memory by a candidate edit (`std::hint::black_box(&mut x);`) and set in the same probe.
- **A Rust local kept in a register:** a by-value parameter, or a `let` the code never borrows or reassigns, may live only in a register even in a debug build, so the debugger shows a copy and a change would not reach the program. Rows mark it `[not settable: register]` (a struct's fields too, in its preview) and `describe`'s `mutable:` line lists them per function. A probe that sets one is refused before the program runs (the debugger's launch request is not sent, no run saved), as is any `--set` its build rules out in every function on the `--at` line (#138), and any `--set-at` the build rules out on its own line ([2.8](../COOKBOOK.md#28-change-a-value-then-restore-it)). Keep it in memory with the edit, in the same probe: for the limiter's `fn allow(w: &mut Window, now: u64, limit: u32, width: u64)`,

  ```sh
  peeky probe limiter --at src/main.rs:10 --arg 2 --arg 10 --arg 0,1,10,11 --capture width \
    --old 'limit: u32, width: u64) -> bool {' \
    --new 'limit: u32, mut width: u64) -> bool { std::hint::black_box(&mut width);' \
    --hits 3 --set-hit 3 --set width=9
  ```

  This reports `set width: 10 -> 9`, and the output the program printed with the change. Borrowing the parameter itself keeps it in memory; a shadowing `let mut width = width;` would not, since a name is settable only when every binding of it is in memory. For a `let` local, add `std::hint::black_box(&mut x);` after its `let` (declared `let mut`).

## 7.2 Rust (a Cargo crate)

**Common cases**

- **Read** locals with their Rust types; an owned `String`, or a `Vec` of plain elements (integers, `bool`, floats, `char`, or tuples and crate structs of them), and a reference parameter of such elements (`&[T]`, `&str`), from memory (`[memory]`), in an unoptimized build with lldb-dap 20. A `Vec<String>` (`args`) stays raw.
- **String loops:** in `for cell in cells` over a `&[&str]`, capture `cell` (`"Renée"`, and `cell.length` its bytes); `cells` and a `Vec<&str>` stay raw, and their note names their length (`cells.length`, `names.len`).
- **`Vec` and `String` fields read too:** `self.values` behind `&mut self` (innermost frame only; behind `&self`, any frame), `w.values` in `main`, which owns `w`, and `entry.name` of a loop's `entry: &Entry`; a moved struct's stay raw, with a note.
- **A crate with `unsafe` code or a nightly feature** reads no `Vec` or `String` contents, no reference parameter's contents and no such field: each stays raw, with the reason. Scalars (`self.k`, `w.k`) are still read.
- **Set** a 16- to 64-bit integer local kept in memory, or such a field of a struct local the frame owns (`--set w.start=10`); a local kept in a register is `[not settable: register]` (keep it in memory with `black_box`, [6.6](#66-see-what-a-fix-does-before-making-it)).
- **Set a `String` to a text** (`--set 'name="pear"'`) within its capacity, where the MIR proves it initialized and no borrow of it (`&s[..2]`, `s.chars()`) is used after the stop, with lldb-dap 21 (macOS); never a `&str`, nor in a crate with `unsafe` code or a nightly feature.
- **Through `&mut Window`** (`w.start` in the callee), set it in the caller that owns `w`, at the call's line.
- **`describe`** lists the paths that can change (`board.entries`, never a `Vec`'s elements), and types read as the program spells them (`Vec<u32>`). A name bound more than once is listed once per binding, by its selector (`points @ main.rs:73`).
- **A plain `cargo new` crate** works; run `cargo generate-lockfile` once if `Cargo.lock` is missing.

### Quick Start

What can be read, what can be set, and where:

- **Read** integer, float, `bool` and `char` locals with their Rust types (`usize`, and `&mut limiter::Window` for a reference parameter; a field or element's too, from the build's debug information: `self.k  usize`, where LLDB says `unsigned long`, kept in `debugger_type`, as for a field a preview reads from memory), `Option<i32>`, a struct or tuple local (or a reference parameter to one) as derived `Debug` prints it (`Window { start: 0, count: 2 }`), and the contents of `Vec<T>`, `String`, `&[T]` and `&str` (`[memory]`), also as fields (`self.values`); fields stay selectable (`w.start`, `runs[0].__0`).
- **Never guessed:** a value Peeky cannot prove (moved, dropped or not yet assigned, in a caller frame, in an optimized build) keeps its raw fields with a note (`[stale]`, `[unproven]`, `<unverified ...>`). A local whose storage may hold leftovers is withheld (`no value yet:` and why): unassigned on a path (`let x;`) or not kept across an `.await`.
- **Set** integer locals (`i16` to `i64`, `u16` to `u64`, `isize`, `usize`) the program keeps in memory, and integer fields and elements of a struct, tuple or array local the frame holds itself, which the MIR proves initialized: `--set w.start=10 --set w.count=0`.
- **Text:** a `String` local takes a text in double quotes, `--set 'name="pear"'`: Peeky writes its bytes and `len` in the buffer it has, within its capacity (`name`'s buffer holds 16 bytes, and the text is 17 bytes is refused), only where the MIR proves the local initialized at the stop (a moved or dropped `String`'s buffer may be freed). The buffer is on the heap, so this needs a debugger that writes memory (lldb-dap 21; macOS ships it); with lldb-dap 18 or 20 it is refused. A `&str` is refused before the program runs: it borrows text that may be read-only.
- **Borrows:** a `String` takes no text while a borrow of it is used after the stop: a `&str` of it (`let head = &s[..2];`), a reference, an iterator (`s.chars()`), anything built from one or given one (a `Vec<&str>` it was pushed into, a closure that borrows it, a `Box<dyn Fn() + '_>` or `Pin<Box<dyn Future + '_>>` holding one). Such a borrow keeps the old length: it would read a mix of old and new text, or bytes that are not UTF-8. Change it before the borrow is taken or after its last use (a `len()` or a clone keeps no borrow).
- **What may hold a borrow:** a value holds none only where its type provably holds no pointer: numbers, `String`, arrays, tuples and standard containers of those, and a struct, enum or union of your crate without type or lifetime parameters whose fields, read from the debug information, are all such or a `Box` of such (`Vec<Span>` for `struct Span { start: u32, end: u32 }`, a C-like `enum`, `struct Rec { v: u32, next: Option<Box<Rec>> }`). A trait object, an `impl` type, a type parameter or another type of your crate may.
- **Drops:** dropping a value counts as using its borrow where the drop may run code that reads it (a guard whose `Drop` reads its `&str`, a trait object, a closure, a type of your crate), not for a standard container of references (`Vec<&str>`).
- **Unsafe code:** a crate with `unsafe` code or a nightly feature takes no text at all, with the reason (see **Crates with unsafe code** below).
- **Never** through a reference or a `Box` (`w.start` where `w: &mut Window`), nor inside an enum, a `#[repr(packed)]` struct or a standard-library type. Peeky does not write through a reference, since what it reaches may be freed or moved; change the value where it is owned, before the same call: a probe `--at` the call's line with `--hits N --set-hit N`, or `session start --at` it with `--hit N`, where N counts the call line's own stops, not `allow`'s, which differ when `allow` is called from several lines (find N with `peeky probe ... --at <call line> --capture now --hits all`); `session continue --to` the call's line reaches only the next call, and `session step --out` returns part-way through the line, where changes are refused.
- **Registers:** A local kept only in a register (a by-value parameter, or a `let` the code never borrows or reassigns) is never settable: rows mark it `[not settable: register]`, `describe`'s `mutable:` line lists them per function, and a probe that sets one is refused before the program runs, unless another function on the line keeps it in memory (a closure, an inlined copy).
- **Bound twice, inlined:** A name the function binds twice is settable only when both bindings are in memory (`[not settable: another binding in a register (line 4)]`, and the refusal names the `let` to black-box), and a local of an `#[inline(always)]` function never is (`[not settable: inlined]`); `describe`'s `mutable:` line gives each binding the same reason (`bind::shadow: x @ main.rs:6 (line 4)`).
- **Async:** A local an async body keeps across an `.await` is never settable either (`[not settable: async state]`): the poll reaches it in the future's state through a pointer; copy it into a local after the `.await` with a candidate edit and set that.
- **Keeping a local in memory:** A candidate edit adding `std::hint::black_box(&mut x);` after its `let` keeps it in memory (a parameter: declared `mut x` and borrowed first thing; see [6.6](#66-see-what-a-fix-does-before-making-it)).
- **Where:** a line breakpoint, or a step that lands on a line's start, where the MIR shows no statement under way has read the value; never back from a call (`step --out`).
- **Statements under way:** Peeky follows the MIR in the order the code runs: a temporary made before the local took its current value holds an earlier one (`for entry in args`: the iterator's `next()` result, from which `entry` is moved), so `entry` changes at the loop body's first line. A refusal names the line where the statement read the value (`a statement under way at the stop has already read it into a temporary (at line 17)`), with its file where it is another's, a macro's code (`at alloc/src/macros.rs:113` for `format!`), and says where a change works instead: before that statement starts, on an earlier line or at an earlier stop of the line (`--set-hit`). A call from a macro of another crate counts for the line only where the IR puts its code there: `label = format!(…);` on line 4 is line 4's, so at line 5's first stop `label` holds its value and `sum` is `[settable]` (#222). Where every place a line can stop at is inside such a statement (a statement's later line: `s.b];` of `let arr = [s.a,` / `k,` / `s.b];`), the probe is refused before it runs, naming that line.
- **The program's own copies:** where a binding that copied the value is used and the local is then replaced or dropped without being read, the program goes on with the copy and never sees the change, so it is refused (`the program has already copied `x` into `n` … and goes on with that copy`): a `match` arm's binding (`x = match x { n => … n }` puts the old value back), a `while let`'s (`x = v - 1`), `let y = x;` … `x = y + 1;`, a `move` closure called before `x` goes out of scope unread (writing a part, `s.a = t;`, reads none of it). Where the program reads the local again, before or after it uses the copy, it sees the change, and the copy keeps the old value as the program's own (`let a = s.x;`). Change it before the copy is made, or change the copy.
- **What counts as reading it again:** only a read of the changed part itself: using its value (`x + 1`, `let z = s.b;`, passing `s` by value) or printing it as a number with `print!`, `println!`, `eprint!`, `eprintln!` or `format!` (`println!("{}", s.b)`, read where it prints). Reading another field (`x.a` for a change to `x.b`), lending a reference to a call (`ignore(&x)`, `width(&s)`), printing the struct with `{:?}`, `write!`, a panic message, or a `format_args!` passed on or dropped shows nothing, so the change is refused there, even where that call or print does read it. A copy counts only for the part it may have taken: `let seen = r.end;` does not block a change to `o.marks[0]`.
- **Raw fields say why:** a field Peeky leaves as LLDB's summary carries the reason, in probe rows and in `session get` listings of what holds it (`note: self.names shows LLDB's summary, not contents: Peeky does not read the elements of a Vec<String>: …`).
- **Marks:** rows show `[settable]` on what `set` would change, fields and elements included; when a rule leaves nothing settable, `session inspect` says why.
- **Builds:** an unoptimized build (the default) for every change and for owned `Vec`/`String` contents; a plain `cargo new` crate with no dependencies. The target is the crate directory with its `Cargo.lock`: a plain `cargo new` binary crate works (edition 2021 or 2024, no dependencies, no `src/lib.rs`, `src/bin` or `build.rs`, and at most one integration test in `tests/`); run `cargo generate-lockfile` once if `Cargo.lock` is missing.

The rest of this section shows them at work, then the detail behind them.

### Examples and Details

```sh
peeky describe examples/rust
peeky probe examples/rust --at 36 --capture start,some,none,mode,request,request.page --set start=0
```

```
breakpoint lines in src/main.rs: 14-17, 20, 22-25, 27-30, 32-38, 40-41
qualified enum views: Option<i32>

hit  phase      line  function                selector      type                       value
1    observed   36    peeky_pagination::main  start         i32                        3  [settable]
1    observed   36    peeky_pagination::main  some          Option<i32>                Some(3)  [active-variant]
1    observed   36    peeky_pagination::main  none          Option<i32>                None  [active-variant]
1    observed   36    peeky_pagination::main  mode          peeky_pagination::Mode     First
1    observed   36    peeky_pagination::main  request       peeky_pagination::Request  Request { page: 1, page_size: 3, total: 8 }  [memory]
1    observed   36    peeky_pagination::main  request.page  i32                        1  [memory]  [settable]
...
set start: 3 -> 0
output:
  PAGE:[10, 20, 30]
```

- **Decoding:** Values Peeky cannot decode with certainty show as `<unverified ...>` or as opaque enums; they are never guessed (`describe` counts them as "other enum types").
- **Changing values:** Only integer locals (`i16` to `i64`, `u16` to `u64`, `isize`, `usize`) that the program keeps in memory can be changed, and `String` locals to a text (see the quick start); `session inspect` marks them `[settable]`. So can such integer fields and elements of a struct, tuple or array local kept in memory that the build's MIR proves initialized at the stop (`--set s.end=10`, `t.__0=3`, `arr[2]=7`): never through a reference or a `Box`, nor inside an enum, a `#[repr(packed)]` struct or a standard-library type, and only at a line breakpoint, or a step that ends at its line's start, where the MIR shows no statement under way has read the local into a temporary (so never back from `f` in `let t = s.a + f();`, nor after a step back onto `let arr = [s.a,` from its later lines), which also holds for integer locals (optional background: [RUST_AGENT.md](../RUST_AGENT.md#changing-fields-and-elements-105)).
- **Reference parameters:** Reference parameters (`&[T]`, `&str`, `&Vec<T>`, `&String`, shared or `&mut`, for `T` any integer type, `bool`, `f32`, `f64` or `char`) are read from memory at offsets checked against the build's debug information (`describe` lists them). Elements print as Rust's `{:?}` does, floats included (`[0.30000000000000004, 1e16, -0.0, NaN]`), so they can be compared with the program's own output. In `fn median(values: &[i32], label: &str)`, `--capture 'values,values[0],values[-1],values.length,label'` shows `[5, 1, 3]`, `5`, `3`, `3` and `"größe"`. Their type column shows the Rust type (`&mut limiter::Window`), not LLDB's (`limiter::Window *`), which `debugger_type` keeps.
- **Tuples and structs:** Tuples, and structs your crate defines, of those scalars are read too: `runs` (`Vec<(char, usize)>`) shows `[('a', 3), ('b', 1)]`, and `runs[0].__0` and `spans[1].start` select fields (LLDB's names: `__0` for a tuple's first). A tuple or struct local of them, or a reference parameter to one, previews the same way under the same proofs (`request` above: `Request { page: 1, page_size: 3, total: 8 }`), and its fields stay selectable (#106). Optional background: [RUST_AGENT.md](../RUST_AGENT.md#reading-rust-values).
- **Owned `Vec` and `String`:** Owned `Vec<T>` and `String` locals are read as well, in the innermost frame of a crate with no unsafe code, wherever the compiler's own MIR proves them initialized at the stop line: Peeky builds the binary crate with `-Zmir-opt-level=0` and dumps its MIR, so every move, drop and assignment is explicit. A local that may be moved, dropped or not yet assigned around the line (`drop(ordered)`, `consume(v)`, the old `s` in `let s = s + "x"`, a deferred `let label: String;`) keeps its raw fields (`ordered.len`) with a note, since its pointer can reach reused memory and its contents would be a guess (#53). A `&str` or slice local reads where #110 shows its target still standing (`v` of `for v in args[2].split(',')`, whose `args` nothing changes); other reference locals keep their raw fields.
- **`&str` items and containers of them** (#205):
  - a `&&str` item (`cell` of `for cell in cells` over a `&[&str]`, LLDB's `&str *`) reads the text it points at, `[memory]`, under the same proof as a `&str` local: in the innermost frame, where #110 shows it still standing (the `&str` it points at outlives it), in a crate with no unsafe code; `cell.length` is its length in bytes;
  - past that (not used ahead, a call or drop since its last use) it shows only its pointer, `[unproven]`, and its note says so;
  - a `&&str` parameter (`fn show(s: &&str)`, given by `words.iter().map(show)`) reads as a `&str` parameter does, typed `&&str`;
  - a `&[&str]` or `Vec<&str>` shows LLDB's summary, and its note says where its items read and names its length: `cells.length` for a slice or `&str`, `names.len` for a `Vec`, `s.vec.len` for a raw `String`;
  - a `Vec<&str>` past its last use keeps its own fields (`names.len` reads), and the pointers inside it show only their address;
  - every such note in a probe's warnings starts with its selector (`cells: …`).
- **Fields holding a `Vec`, `String`, `&str` or `Box<str>`** read from memory too (#156), reached through plain struct and tuple fields and fixed-size array elements:
  - through a reference parameter: `self.values` in `fn push(&mut self, v: i64)` shows `[3, 6]`, and `self.values[0]`, `self.values[-1]` and `self.values.len` select from it. A `&mut self` reads only in the innermost frame (in a caller, the call under way may be changing it); a `&self` in any frame;
  - in an owned local (or what its `Box` holds), under the local's proof, in the innermost frame only: `w.values` in `main`, where `w.sum` is `[settable]`;
  - through a reference local, where #110 shows what it points at still standing, in the innermost frame only: `entry.name` of `for (entry, points) in entries.iter().zip(&point_cells)`, read on the loop's lines (#206);
  - never through a `RefCell` (its contents change behind `&self`), a `Mutex` or another standard-library type, a pointer, a reference other than the root, or an enum, nor in a moved, dropped or partly moved struct (`[stale]`, #108);
  - a `&str` field of an owned local reads where #110 shows its target standing (a struct with no lifetime holds only `&'static str`);
  - where a field stays raw, its note says why (`` `self` is a `&mut` parameter of a caller frame ``, `` `self.cell` is a standard-library type ``, `the debugger gives no memory address for it`, `Peeky does not read the elements of a Vec<String>`, #183), and `session get self` lists them; an index into it (`self.values[0]`) is refused with the same reason and the length selector to capture instead (`self.values.len`).
- **Crates with unsafe code:** unsafe code can free a buffer that safe code still shows owned, unseen (`ptr::read(&w.values)`, `ptr::drop_in_place(&mut self.values)`, in a closure or another function), so where the crate has any, Peeky reads nothing from memory itself (#156):
  - raw, with the reason ("the crate has unsafe code (in src/main.rs), which can free a buffer without Peeky seeing it"): a reference parameter's contents and a struct preview through a reference, an owned `Vec` or `String` local, a `&str` or slice local, a container field; a Box shows only its pointer (`[unproven]`);
  - no `String` takes a text (`--set 'name="x"'`), with the same reason: a raw pointer, or an address kept as an integer (`s.as_ptr() as usize`), can read its bytes at their old length where no borrow shows it;
  - still read, as LLDB reads them: scalar locals, a struct's scalar fields in the frame or behind a reference parameter (`w.k`), a by-value struct's preview, an enum's variant;
  - what counts: an `unsafe` block, function, impl, trait or extern block, `#[unsafe(...)]`, `#[no_mangle]`, `#[export_name]`, `#[link_section]` or `global_asm!` in any of the crate's files (`tests/` too; `macro_rules!` bodies included, comments and strings not); a nightly feature (`#![feature(...)]`: "the crate enables nightly features"); and a source Peeky cannot read as rustc does (frontmatter, an unclosed literal: "Peeky cannot rule out unsafe code in …"). Optional background: [RUST_AGENT.md](../RUST_AGENT.md#reading-rust-values).
- **Names bound more than once** (#206):
  - `describe` lists each binding by the selector lldb-dap gives it where another of its name is in scope too: `points @ main.rs:73 (Vec<String>, lines 74-85)` and `points @ main.rs:81 (&str *, line 82)`;
  - that selector reads the binding at any stop where it is in scope, also where lldb-dap lists it by its plain name (`points @ main.rs:73` on line 76);
  - a plain name is the one binding in scope at the stop, by the MIR's scopes: an earlier loop's `entry` does not hold back this loop's `entry.name`; where two are in scope, lldb-dap lists only the `@` names;
  - two bindings that share a file name and line (`let x = n + 1; let x = format!("{x}!");`, or files of one name in two directories) share the selector too, which names neither: `describe` lists `x @ main.rs:3 (2 bindings on this line: not selectable by line)`, and the selector is refused with that reason.
- **Long values:** Long values show their first elements with `(first k of N)`, and up to 256 elements stay selectable.
- **Debugger:** This needs a debugger with `readMemory` (lldb-dap from LLVM 20); older debuggers keep the raw fields, with an error that names the length field.
