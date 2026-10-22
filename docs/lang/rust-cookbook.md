# Peeky Cookbook: Rust

This cookbook covers what is different for Rust. Read the [COOKBOOK](../COOKBOOK.md) first: it
has the recipes every language shares. Each section here has the number and title of the COOKBOOK
section it adds to. For a one-page summary of the Rust rules, see [rust.md](rust.md).

Peeky debugs Rust with lldb-dap, LLVM's debugger. Words such as MIR, raw, `[memory]` and `[stale]`
are explained in [rust.md](rust.md#words-these-pages-use).

## Contents

- [1.1 What Can I Ask?](#11-what-can-i-ask)
  - [See where a local is visible](#see-where-a-local-is-visible)
  - [Read types as the program spells them](#read-types-as-the-program-spells-them)
  - [See where nothing can change, and the way around it](#see-where-nothing-can-change-and-the-way-around-it)
- [2.1 Fields and Indexes](#21-fields-and-indexes)
  - [Read through a reference or a Box](#read-through-a-reference-or-a-box)
- [6.6 See What a Fix Does Before Making It](#66-see-what-a-fix-does-before-making-it)
  - [Change a local kept in a register](#change-a-local-kept-in-a-register)
- [7.2 Rust (a Cargo crate)](#72-rust-a-cargo-crate)
  - [Set up the crate](#set-up-the-crate)
  - [A first probe](#a-first-probe)
  - [What Peeky reads](#what-peeky-reads)
  - [Values Peeky withholds or leaves raw](#values-peeky-withholds-or-leaves-raw)
  - [Crates with unsafe code](#crates-with-unsafe-code)
  - [Names bound more than once](#names-bound-more-than-once)
  - [What `--set` can change](#what---set-can-change)
  - [Where a change is allowed](#where-a-change-is-allowed)
  - [When the program has its own copy](#when-the-program-has-its-own-copy)
  - [Locals that can't change, and what to do](#locals-that-cant-change-and-what-to-do)
  - [Change a value behind a reference](#change-a-value-behind-a-reference)
  - [Set a String to a text](#set-a-string-to-a-text)

## 1.1 What Can I Ask?

Read [COOKBOOK 1.1](../COOKBOOK.md#11-what-can-i-ask) first.

### See where a local is visible

In Rust, every `let` opens a block. So `describe` gives a local the lines after its `let`, where
it is visible. For [examples/rust](../../examples/rust):

```sh
peeky describe examples/rust
```

```
...
names in peeky_pagination::main (src/main.rs:22-41): args (Vec<String>, lines 24-41), end (i32, lines 37-41), mode (peeky_pagination::Mode, lines 33-41), ...
...
```

`describe` leaves out bindings that a macro from another crate declares, such as `println!`'s
`args` and `core::fmt::rt::Argument[1]`. The build's MIR places them in the macro's own file.

A macro of your own crate keeps its bindings, even with `#[collapse_debuginfo(yes)]`.

### Read types as the program spells them

Types are spelled as the program spells them: `Vec<u32>`, `String`, `Option<i32>`. This holds in
`describe` and in probe rows. A row's `debugger_type` keeps LLDB's spelling, such as
`alloc::vec::Vec<unsigned int, alloc::alloc::Global>`.

### See where nothing can change, and the way around it

For Rust, `describe`'s `mutable:` line ends with the functions where nothing can change. Nothing
can change there when every local is in a register, behind a reference, or a container's
contents. The line then names the way around it: a candidate edit. A candidate edit can also keep
a local in memory ([6.6](#66-see-what-a-fix-does-before-making-it)).

```
mutable: ... A Vec's, String's or Box's contents (`v[0]`, `self.values[1]`) and what a reference points at are never settable; nor is anything in peeky_pagination::bounded::{{closure}}, peeky_pagination::main::{{closure}}: change those with a candidate edit, the program's own code (`--old`/`--new`; `std::hint::black_box(&mut x)` after a `let` keeps a local in memory: rust-cookbook 6.6)
```

## 2.1 Fields and Indexes

Read [COOKBOOK 2.1](../COOKBOOK.md#21-fields-and-indexes) first.

### Read through a reference or a Box

In C and C++, `p[0]` reads what a pointer points at
([C and C++ 2.1](c-cpp-cookbook.md#21-fields-and-indexes)). Peeky never indexes a Rust reference
or `Box` this way. So for `w: &mut Window`, `w[0]` doesn't read the `Window`. Select its fields
directly instead: `w.start`.

A slice is different: an index selects an element, as in `values[0]` for `values: &[i32]`.

## 6.6 See What a Fix Does Before Making It

Read [COOKBOOK 6.6](../COOKBOOK.md#66-see-what-a-fix-does-before-making-it) first.

### Change a local kept in a register

A by-value parameter, or a `let` that the code never borrows or reassigns, may live only in a
register. This can happen even in a debug build. The debugger then shows a copy, so a change
wouldn't reach the program.

Rows mark such a local `[not settable: register]`. In a struct's preview, its fields are marked
too. `describe`'s `mutable:` line lists them per function.

For example, this `limiter` crate's `src/main.rs` accepts at most `limit` events per window:

```rust
use std::env;

#[derive(Debug, Clone, Copy)]
struct Window {
    start: u64,
    count: u32,
}

fn allow(w: &mut Window, now: u64, limit: u32, width: u64) -> bool {
    if now - w.start > width { // ALLOW
        w.start = now;
        w.count = 0;
    }
    if w.count < limit {
        w.count += 1;
        true
    } else {
        false
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let limit: u32 = args[1].parse().unwrap();
    let width: u64 = args[2].parse().unwrap();
    let times: Vec<u64> = args[3].split(',').map(|t| t.parse().unwrap()).collect();
    let mut w = Window { start: times[0], count: 0 };
    let mut accepted = Vec::new();
    for &now in &times {
        if allow(&mut w, now, limit, width) {
            accepted.push(now.to_string());
        }
    }
    println!("ACCEPTED:{}", accepted.join(","));
}
```

`width` is a by-value parameter kept in a register:

```sh
peeky probe limiter --at src/main.rs:10 --arg 2 --arg 10 --arg 0,1,10,11 --capture width,w
```

```
hit  phase     line  function        selector  type                  value
1    observed  10    limiter::allow  width     u64                   10  [not settable: register]
1    observed  10    limiter::allow  w         &mut limiter::Window  Window { start: 0, count: 0 }  [memory]
...
```

A probe that sets it is refused before the program runs. The debugger's launch request is not
sent, and no run is saved:

```sh
peeky probe limiter --at src/main.rs:10 --arg 2 --arg 10 --arg 0,1,10,11 --capture width \
  --hits 3 --set-hit 3 --set width=9
```

```
Error [MUTATION_REJECTED]: `width` cannot be changed at src/main.rs:10, at any stop of this build: in limiter::allow, `width` is a parameter kept in a register (the debugger only shows a copy of it, so a change would not affect the program). Refused before the program runs: nothing ran. A candidate edit keeps it in memory, in the same probe (rust-cookbook 6.6): `mut width` in the signature and `std::hint::black_box(&mut width);` as the function's first statement, by `--old`/`--new`, with this `--set`; settable here: no root locals (fields where rows mark them [settable])
hint: Keep it in memory with a candidate edit (`std::hint::black_box(&mut x);` after its `let`, by `--old`/`--new`) and `--set` it in that same probe.
```

The exception is when another function on the line keeps it in memory, such as a closure or an
inlined copy.

Peeky refuses these the same way, before the program runs:

- any `--set` that the build rules out in every function on the `--at` line;
- any `--set-at` that the build rules out on its own line
  ([2.8](../COOKBOOK.md#28-change-a-value-then-restore-it)).

To change it, keep it in memory with a candidate edit, in the same probe. Declare the parameter
`mut`, and borrow it with `std::hint::black_box` as the function's first statement:

```sh
peeky probe limiter --at src/main.rs:10 --arg 2 --arg 10 --arg 0,1,10,11 --capture width \
  --old 'limit: u32, width: u64) -> bool {' \
  --new 'limit: u32, mut width: u64) -> bool { std::hint::black_box(&mut width);' \
  --hits 3 --set-hit 3 --set width=9
```

```
hit  phase      line  function        selector  type  value
1    observed   10    limiter::allow  width     u64   10  [settable]
2    observed   10    limiter::allow  width     u64   10  [settable]
3    observed   10    limiter::allow  width     u64   10  [settable]
3    after-set  10    limiter::allow  width     u64   9  [settable]
set width: 10 -> 9
...
output:
  ACCEPTED:0,1,10,11
candidate: values are from a fresh build with this edit; your files are unchanged
...
```

The probe reports `set width: 10 -> 9`, and what the program printed with the change.

- **A parameter:** borrow the parameter itself. A shadowing `let mut width = width;` would not
  work. A name is settable only when every binding of it is in memory.
- **A `let` local:** declare it `let mut x`, and add `std::hint::black_box(&mut x);` after its
  `let`.

## 7.2 Rust (a Cargo crate)

### Set up the crate

The target is the crate directory with its `Cargo.lock`. A plain `cargo new` binary crate works:

- edition 2021 or 2024;
- no dependencies: `[dependencies]`, `[dev-dependencies]` and `[build-dependencies]` stay empty;
- no `src/lib.rs`, `src/bin` or `build.rs`;
- at most one integration test, in `tests/`.

If `Cargo.lock` is missing, run `cargo generate-lockfile` once.

In detail:

- Targets may be explicit (`[[bin]]`, `[[test]]`, with discovery switched off) or Cargo's
  defaults (`src/main.rs`, `tests/*.rs`). Nothing else that Cargo would discover may exist:
  `src/lib.rs`, `src/bin`, `build.rs`, or test directories.
- Package metadata, such as `description` or `license`, is allowed.
- No workspace, build script, doctest, or unit-test harness of the binary itself.
- Without an integration test, `verify` relies on the checker alone.
- A source's name may hold any character: `#[path = "x\u{2028}y.rs"]`. It must end in `.rs`. A
  `#[path]` module named otherwise isn't part of the build: `couldn't read src/module`.
- Lines are counted as rustc counts them, at `\n` alone.
- A crate whose `asm!`, `global_asm!` or `naked_asm!` can read a file is refused before anything
  runs, with `UNSUPPORTED`. That is `.incbin`, `.include`, or an assembler macro that could spell
  one: rustc's dependency file doesn't list what the assembler reads. Where a template may come
  from elsewhere, such as a `macro_rules!` argument, a renamed asm macro or `include_str!`, such a
  directive anywhere in the crate counts.

Some tasks need a certain build or debugger:

| Task | Build and debugger it needs |
| --- | --- |
| Read the contents of a `Vec`, a `String` or a reference parameter | an unoptimized build, the default, and lldb-dap 20 or later, which has `readMemory` |
| Change any value | an unoptimized build |
| Set a `String` to a text | an unoptimized build, and lldb-dap 21, which macOS ships |

An older debugger keeps contents raw, with an error that names the length field. With lldb-dap 18
or 20, setting a text is refused.

These are not the only conditions. [What Peeky reads](#what-peeky-reads) and
[What `--set` can change](#what---set-can-change) list the others.

### A first probe

[examples/rust](../../examples/rust) pages through a list. Stop at line 36, read some values, and
set `start` to 0:

```sh
peeky probe examples/rust --at 36 --capture start,some,none,mode,request,request.page --set start=0
```

```
hit  phase      line  function                selector      type                       value
1    observed   36    peeky_pagination::main  start         i32                        3  [settable]
1    observed   36    peeky_pagination::main  some          Option<i32>                Some(3)  [active-variant]
1    observed   36    peeky_pagination::main  none          Option<i32>                None  [active-variant]
1    observed   36    peeky_pagination::main  mode          peeky_pagination::Mode     First
1    observed   36    peeky_pagination::main  request       peeky_pagination::Request  Request { page: 1, page_size: 3, total: 8 }  [memory]
1    observed   36    peeky_pagination::main  request.page  i32                        1  [memory]
...
set start: 3 -> 0
...
output:
  PAGE:[10, 20, 30]
```

- `start` is an `i32` local kept in memory, so it is `[settable]`.
- `request` shows as derived `Debug` prints it, read from memory.
- `request.page` is not `[settable]` here. The program has already copied it into `start`, on
  line 35. See [When the program has its own copy](#when-the-program-has-its-own-copy).

### What Peeky reads

Peeky reads a value only when it can prove the value is there. Otherwise it shows the raw value
with a note, or withholds it
([Values Peeky withholds or leaves raw](#values-peeky-withholds-or-leaves-raw)).

The table's last column names the conditions a value needs. Each condition is defined below it.

| Value | What a row shows | Only with |
| --- | --- | --- |
| Integer, float, `bool` or `char` local or parameter | its value, with its Rust type: `usize` | no condition, but it can be withheld |
| `Option<i32>` | `Some(3)` or `None`, marked `[active-variant]` | a listed enum; unoptimized |
| Struct or tuple local, or a by-value parameter | as derived `Debug` prints it, marked `[memory]` | plain fields; memory; innermost; initialized |
| Reference parameter to a struct or tuple | as derived `Debug` prints it, marked `[memory]` | plain fields; memory; safe crate; live; not rebound; `&mut` innermost |
| Owned `Vec<T>` or `String` local | its contents, marked `[memory]` | plain `T`; memory; safe crate; innermost; initialized |
| `&[T]`, `&str`, `&Vec<T>` or `&String` parameter | its contents, marked `[memory]` | plain `T`; memory; safe crate; not rebound; `&mut` innermost |
| `&str` or slice local | its contents | memory; safe crate; innermost; alive |
| Field holding a `Vec`, `String`, `&str` or `Box<str>` | its contents, marked `[memory]` | memory; safe crate; [what holds it](#fields-holding-a-vec-string-str-or-boxstr) |
| `Vec<String>`, such as `args` | raw when shown, with a note | it can still be withheld |
| `&[&str]` or `Vec<&str>` | raw when shown, with a note naming the length selector | it can still be withheld |

The conditions:

- **can be withheld:** a local that may be unassigned shows `no value yet:`. In an optimized
  build, a value may be unavailable or stale ([7.5](../COOKBOOK.md#75-optimized-builds)).
- **a listed enum:** `describe` lists its type on the `qualified enum views:` line. Other enums
  stay opaque.
- **unoptimized:** an unoptimized build. In an optimized build, an enum shows
  `<unverified Rust enum value>`.
- **plain fields:** all its fields are integers, `bool`, floats or `char`
  ([Tuples and structs](#tuples-and-structs) lists the other limits).
- **plain `T`:** an integer, `bool`, float or `char`, or a tuple or crate struct with plain
  fields.
- **memory:** an unoptimized build, and a debugger with `readMemory`, such as lldb-dap 20 or later.
- **safe crate:** the crate has no unsafe code
  ([Crates with unsafe code](#crates-with-unsafe-code)).
- **innermost:** the stop is in the innermost frame.
- **live:** the build's IR proves the reference parameter live.
- **not rebound:** the function doesn't bind the parameter's name again. If it does, the parameter
  shows only its pointer in the whole function.
- **`&mut` innermost:** a `&mut` parameter reads only in the innermost frame. A shared one reads
  in any frame.
- **initialized:** the MIR proves the local initialized at the stop line.
- **alive:** the MIR shows that what it points at is still alive.

Fields stay selectable: `w.start`, `runs[0].__0`.

#### Types in rows

- A reference parameter shows its Rust type, such as `&mut limiter::Window`. LLDB's type,
  `limiter::Window *`, is kept in `debugger_type`.
- A field's or element's type comes from the build's debug information: `self.k  usize`, where
  LLDB says `unsigned long`. `debugger_type` keeps LLDB's. This holds for a field that a preview
  reads from memory too.
- Locals and parameters take their types from the build's MIR: `usize`, `f64`, `char`. A scalar
  keeps LLDB's name in `debugger_type`. In a generic function, a reference parameter shows the
  instantiation's own type.
- Any other value takes the one Rust primitive that LLDB's name can be: `double` is `f64`. Only
  `long` and `unsigned long` keep LLDB's name there, since each can be two Rust types: `i64` or
  `isize`, `u64` or `usize`.
- `f64` and `f32` values print as Rust's `{:?}` does: `10.0`. `u8` and `i8` values print as
  integers: `65`, not LLDB's `'A'`.
- A reference or raw pointer to a `u8`, `i8` or `char` (LLDB's `unsigned char *`) shows only its
  address, never LLDB's C-string reading of what follows it. What it points at is its child.

#### Reference parameters

In a crate with no unsafe code, Peeky reads reference parameters from memory: `&[T]`, `&str`,
`&Vec<T>` and `&String`, shared or `&mut`. `T` is any integer type, `bool`, `f32`, `f64` or
`char`, or a tuple or crate struct of them.

- This needs an unoptimized build, and a debugger with `readMemory`.
- A `&mut` parameter reads only in the innermost frame. In a caller, the call under way may be
  changing what it points at.
- A parameter that its function binds again, such as `let w = …;`, shows only its pointer in the
  whole function. The plain name could mean either binding. A `let w: &Vec<i64>;` that the
  function never assigns counts too.
- A parameter named in Unicode, such as `größe`, reads as its ASCII twin does.
- Peeky checks the offsets it reads against the build's debug information.
- Rows show `[memory]`. Selectors index them (`values[0]`, `values[-1]`) and read their length:
  `values.length` for a slice or a `&str`, and `list.len` and `list.capacity` through a `&Vec` or a
  `&String`. Text is shown whole.
- `describe` lists these types, on its `read from memory` line.
- Elements print as Rust's `{:?}` prints them, floats included:
  `[0.30000000000000004, 1e16, -0.0, NaN]`. So you can compare them with the program's own output.

For `fn median(values: &[i32], label: &str)`,
`--capture 'values,values[0],values[-1],values.length,label'` shows `[5, 1, 3]`, `5`, `3`, `3` and
`"größe"`.

#### Tuples and structs

Tuples, and structs your crate defines, read too when their fields are all integers, `bool`,
floats or `char`:

- `runs`, a `Vec<(char, usize)>`, shows `[('a', 3), ('b', 1)]`.
- `runs[0].__0` and `spans[1].start` select fields. `__0` is LLDB's name for a tuple's first field.
- A tuple or struct local of them previews the same way. So does a by-value parameter, or a
  reference parameter to one. `request` above shows `Request { page: 1, page_size: 3, total: 8 }`.
- Its fields stay selectable.
- They print as a derived `Debug` would, even for a type with its own `Debug`: `('a', 3)`,
  `Interval { start: 1, end: 10, open: false }`, `Point(1, 2)`, `(7,)`, and a unit struct as
  `Marker`.
- An element's fields are named as LLDB names them (`points[1].__1`), with their Rust types. They
  are read-only.
- The build's debug information must show plain fields at aligned offsets. The compiler's own type
  picks the layout, because LLDB's names can collide: LLDB names `Pair<i8>` `Pair<char>`. LLDB's
  name must still spell the type: `Pair<char32_t>` is never a `Pair<i8>`.

A preview needs the same proofs as a container:

- an owned local or a by-value parameter: in the innermost frame, where the MIR proves it
  initialized at the stop line;
- a reference parameter: once the build's IR proves it live, and a `&mut` one only in the
  innermost frame. In a crate with unsafe code, it stays raw.

These keep LLDB's summary instead:

- a struct with any other field, such as a `String`, an enum or a reference, with a note;
- a nested struct, such as `Rect { a: Span, .. }`;
- a `#[repr(packed)]` struct, even where its fields fall aligned;
- a standard-library struct, such as `Range<usize>`, which prints as `1..5`, with a note;
- two structs that share a path, such as one declared in a function's body and one in a module of
  the same name. A container of either is opaque;
- a local that may be moved, dropped or not yet assigned, a caller frame's local, and a reference
  local;
- a tuple struct, in a crate whose sources have an identifier `__0` or enable a nightly feature.
  The debug information of `struct Num { __0: u32 }` is that of `struct Num(u32)`;
- a preview longer than 1000 characters;
- any preview in an optimized build, or with a debugger without `readMemory`.

In a generic function, its closures and its impl's methods, a type that names a type parameter is
never taken for the frame's own. That holds whatever the parameter's name: `fn show<i8>(p: Pair<i8>)`
may be called with chars. Such a local or `Vec` stays raw, and a scalar keeps LLDB's type name. A
reference parameter still previews, by the instantiation's own type. A const parameter is a value,
not a type, so a function generic only over consts (`fn sum<const N: usize>`, `fn ch<const C: char>`)
keeps its previews and Rust types.

How Peeky proves these values: [RUST_AGENT.md](../RUST_AGENT.md#reading-values-from-memory), for
maintainers.

#### Owned `Vec` and `String` locals

Peeky reads an owned `Vec<T>` or `String` local only when all of these hold:

- the build is unoptimized, and the debugger has `readMemory`
  ([Set up the crate](#set-up-the-crate));
- the stop is in the innermost frame;
- the crate has no unsafe code ([Crates with unsafe code](#crates-with-unsafe-code));
- the compiler's MIR proves the local initialized at the stop line.

To prove it, Peeky builds the binary crate with `-Zmir-opt-level=0` and dumps its MIR. There,
every move, drop and assignment is explicit.

If the MIR's initialization proof fails, the local keeps its raw fields, such as `ordered.len`,
with a note. Its pointer can reach reused memory, so its contents would be a guess. The proof
fails where the local may be moved or dropped around the line:

- `drop(ordered)` or `consume(v)`;
- the old `s` in `let s = s + "x"`.

A local that isn't assigned yet on some path, such as a deferred `let label: String;`, is
withheld instead: `no value yet:`.
[Values Peeky withholds or leaves raw](#values-peeky-withholds-or-leaves-raw) shows both.

#### `&str` and slice locals

- In a crate with no unsafe code, a `&str` or slice local reads where the MIR shows its target
  still alive. The stop must be in the innermost frame of an unoptimized build.
- Peeky counts the target alive while the code still uses the local. After its last use, it
  counts it alive until a call or drop runs.
- A shared borrow of variables that nothing changes reads even later. For example, `v` of
  `for v in args[2].split(',')` reads, because nothing changes `args`.
- Past that, the local shows only its pointer, and says so. To read it, probe a line that still
  uses it ([When a reference reads](#when-a-reference-reads)).
- Other reference locals keep their raw fields.

#### `&str` loop items, and containers of `&str`

In `for cell in cells` over a `&[&str]`, capture `cell`. It reads as text, marked `[memory]`.
`cell.length` is its length in bytes. For this `cells` crate:

```rust
fn longest(cells: &[&str]) -> usize {
    let mut best = 0;
    for cell in cells {
        let n = cell.chars().count();
        if n > best {
            best = n;
        }
    }
    best
}

fn main() {
    let names: Vec<&str> = vec!["Ana", "Renée", "Bo"];
    println!("{}", longest(&names));
}
```

```sh
peeky probe cells --at src/main.rs:4 --capture cell,cell.length,cells --hits 2
```

```
hit  phase     line  function        selector     type     value
1    observed  4     cells::longest  cell         &str *   "Ana"  [memory]
1    observed  4     cells::longest  cell.length  usize    3  [memory]
1    observed  4     cells::longest  cells        &[&str]  &[&str] @ 0x16b0faf20
2    observed  4     cells::longest  cell         &str *   "Renée"  [memory]
2    observed  4     cells::longest  cell.length  usize    6  [memory]
2    observed  4     cells::longest  cells        &[&str]  &[&str] @ 0x16b0faf20
...
warning: cells: This value is LLDB's summary (its type and address), not its contents: check contents with `peeky verify`. Its `&str` items read as text where the code binds one (`for cell in cells`: `cell`). Capture cells.length for the length. Peeky reads Rust contents only for String, &str, and containers of integer, bool, float or char elements, or of tuples of them and structs of them your crate defines.
```

The details:

- `cell` is a `&&str`, which LLDB types `&str *`. It reads under the same proof as a `&str` local.
  It reads in the innermost frame, in a crate with no unsafe code, where the MIR shows it still
  alive.
- The `&str` that `cell` points at outlives it, so only `cell` itself must be alive.
- Past that point, `cell` shows only its pointer, marked `[unproven]`. Its note says so. Past that
  point means it isn't used ahead, or a call or drop came since its last use.
- A `&&str` parameter reads as a `&str` parameter does, typed `&&str`. An example is
  `fn show(s: &&str)`, called by `words.iter().map(show)`.
- A `&[&str]` or `Vec<&str>` shows LLDB's summary. Its note says where its items read, and names
  its length selector:

  | Raw value | Length selector |
  | --- | --- |
  | a slice, or a `&str` | `cells.length` |
  | a `Vec` | `names.len` |
  | a raw `String` | `s.vec.len` |

- A `Vec<&str>` past its last use keeps its own fields, so `names.len` reads. The pointers inside
  it show only their address.
- In a probe's warnings, every such note starts with its selector: `cells: …`.

#### Fields holding a `Vec`, `String`, `&str` or `Box<str>`

In a crate with no unsafe code, these fields read from memory too. This needs an unoptimized
build, and a debugger with `readMemory`. Peeky reaches them only through plain struct and tuple
fields, and fixed-size array elements. For this `movavg` crate:

```rust
struct Window {
    values: Vec<i64>,
    sum: i64,
}

impl Window {
    fn push(&mut self, v: i64) {
        self.sum += v;
        self.values.push(v);
    }
}

fn main() {
    let mut w = Window { values: Vec::new(), sum: 0 };
    for v in [3, 6, 9] {
        w.push(v);
    }
    let avg = w.sum / w.values.len() as i64;
    println!("{avg}");
}
```

```sh
peeky probe movavg --at src/main.rs:8 --hits 3 \
  --capture 'self.values,self.values[0],self.values[-1],self.values.len'
```

```
hit  phase     line  function              selector         type      value
...
3    observed  8     movavg::Window::push  self.values      Vec<i64>  [3, 6]  [memory]
3    observed  8     movavg::Window::push  self.values[0]   i64       3  [memory]
3    observed  8     movavg::Window::push  self.values[-1]  i64       6  [memory]
3    observed  8     movavg::Window::push  self.values.len  usize     2  [memory]
```

```sh
peeky probe movavg --at src/main.rs:18 --capture w.values,w.sum
```

```
hit  phase     line  function      selector  type      value
1    observed  18    movavg::main  w.values  Vec<i64>  [3, 6, 9]  [memory]
1    observed  18    movavg::main  w.sum     i64       18  [settable]
```

Where such a field reads depends on what holds it:

- **Through a `&mut` reference parameter,** such as `self.values` in `fn push(&mut self, v: i64)`:
  once the build's IR proves the parameter live, past its declaration lines, and in the innermost
  frame only. In a caller, the call under way may be changing it.
- **Through a shared reference parameter,** such as `self.values` behind `&self`: once the IR
  proves the parameter live, in any frame. The exception is where the MIR shows its target dropped
  and not yet replaced.
- **In an owned local, or what its `Box` holds,** such as `w.values` in `main`: in the innermost
  frame only. The MIR must prove the local initialized.
- **Through a reference local,** such as `entry.name` of
  `for (entry, points) in entries.iter().zip(&point_cells)`: in the innermost frame only, where the
  MIR shows what it points at still alive. Here it reads on the loop's lines.

A `&str` field of an owned local reads where the MIR shows its target still alive. A struct with
no lifetime holds only `&'static str`. What a `&str` field behind a reference parameter points at
outlives the call.

- The field's own type, from the debug information, picks the layout. So a generic's instances
  read as themselves: `w.items` in `fn peek<T>(w: &Wrap<T>)` is `[1, 2]` for `T = u8`, and `['a']`
  for `T = char`.
- Elements may be scalars, or tuples and crate structs of them, as in containers.
- The first such field of a session reads the debug information of the crate's own code once. That
  takes tens of milliseconds for a small crate, and field changes share it.

Such a field never reads, and stays raw:

- through a `RefCell`, whose contents change behind `&self`;
- through a `Mutex` or another standard-library type;
- through a pointer, or a reference other than the root;
- inside an enum, such as `Option<Vec<i64>>`, a union, or a packed struct;
- in a moved, dropped or partly moved struct, which shows `[stale]`.

Where a field stays raw, its note says why:

- `` `self` is a `&mut` parameter of a caller frame ``;
- `` `self.cell` is a standard-library type (RefCell<…>) ``;
- `` `w` is a local of a caller frame ``;
- `the debugger gives no memory address for it`, with lldb-dap 18;
- `Peeky does not read the elements of a Vec<String>`.

`session get self` lists those fields. An index into one, such as `self.values[0]`, is refused
with the same reason. The refusal names the length selector to capture instead:
`self.values.len`.

#### Long values

A long value shows its first elements, with `(first k of N)`. Up to 256 elements stay selectable.

### Values Peeky withholds or leaves raw

Peeky never guesses a value. When it can't prove one, it either withholds the local or shows the
raw value with a note.

**It withholds a local that may be unassigned.** The MIR shows that some path to the stop leaves
the local unassigned since its storage began. So its slot may hold leftovers. The row says
`no value yet:` and why, with no value, and `--set` refuses the local. This happens:

- before a deferred `let x;` is assigned on every path;
- on a loop's next pass, before the local's `let`;
- for an `if let` binding, while its scrutinee's call runs;
- in an async body, for a local that the future doesn't keep across an `.await`. A poll that
  resumes after the `.await` never assigns it, so its slot holds what earlier calls left:
  `tmp = 1` where the first poll set 105.

Peeky checks this in the innermost frame of an unoptimized build, and in a caller frame inside the
call it makes. It checks a local of a type that doesn't borrow: a scalar, an enum, a struct, a
`Vec`. The check is the one rustc's own initialization check makes.

**It shows other values it can't prove as raw, with a note.** These include:

- a container whose MIR initialization proof fails;
- a value that may be moved or dropped;
- a value in a caller frame, or in an optimized build.

A moved or dropped value is not withheld: it keeps its leftover bytes, with a note. `session get`
marks them `[stale]`.

| You see | Meaning |
| --- | --- |
| `no value yet:` and why | Withheld: the local may be unassigned, so its slot may hold leftovers. |
| `[stale]` | The leftover bytes of a moved or dropped local. |
| `[unproven]` | Only a pointer: Peeky can't prove that what it points at is still there. |
| `<unverified ...>` | A value Peeky can't decode with certainty. |
| an opaque enum | An enum Peeky can't decode with certainty. `describe` counts them as "other enum types". |

For example, in this `moved` crate, `ordered` moves into `total` on line 8. `label` is assigned
only on line 9:

```rust
fn total(v: Vec<i32>) -> i32 {
    v.iter().sum()
}

fn main() {
    let ordered = vec![3, 1, 2];
    let label: String;
    let sum = total(ordered);
    label = format!("sum {sum}");
    println!("{label}");
}
```

```sh
peeky probe moved --at src/main.rs:8 --capture ordered,label
```

```
hit  phase     line  function     selector  type      value
1    observed  8     moved::main  ordered   Vec<i32>  alloc::vec::Vec<int, alloc::alloc::Global> @ 0x16fc46ef8  [note]
1    observed  8     moved::main  label     String    <no value yet: not assigned on every path to here (a `let` without a value, or a loop's next pass before its `let`): its slot may hold what an earlier call or pass left; read it once it is assigned>
...
warning: ordered: This Rust Vec shows LLDB's summary (its type and address), not its contents: the build's MIR does not prove `ordered` initialized at this line: it may be moved, dropped or not yet assigned around it (#53). Capture ordered.len for the length, and check contents with `peeky verify`.
```

On line 8, the MIR doesn't prove `ordered` initialized, because the line moves it. So it keeps
its raw fields, with a note. `label` isn't assigned yet, so it is withheld.

After the move, on line 9, `ordered`'s fields are its leftover bytes:

```
$ peeky session start moved --at src/main.rs:9
session default (rust): state: stopped at moved/src/main.rs:9 in moved::main, hit 1, stop 1 of 3 (paused 0.0 s)
warning: Line 9 stops up to 3 times per pass in moved::main (3 breakpoint locations): --hits, --set-hit and --set-at's @HIT count stops, not passes (as do session hit numbers); each hit's stop (1 to 3, in address order) says which location.
$ peeky session get ordered
ordered: Vec<i32> = alloc::vec::Vec<int, alloc::alloc::Global> @ 0x16ce96ef8  [stale]
  ordered.buf: alloc::raw_vec::RawVec<i32> = alloc::raw_vec::RawVec<int, alloc::alloc::Global> @ 0x16ce96ef8  [stale] (children: peeky session get ordered.buf)
  ordered.len: usize = 3  [stale]
  note: This Rust Vec shows LLDB's summary (its type and address), not its contents: the build's MIR does not prove `ordered` initialized at this line: it may be moved, dropped or not yet assigned around it (#53). Capture ordered.len for the length, and check contents with `peeky verify`.
$ peeky session close
closed default
cleanup: terminated pids 17296, 17297
```

A raw field carries the reason. It shows in probe rows, and in `session get` listings of what
holds it:

```
note: self.names shows LLDB's summary, not contents: Peeky does not read the elements of a Vec<String>: …
```

#### Moved, dropped and partly moved values

- A local that may own memory through a pointer inside it is judged by the MIR's proof. That is a
  struct, tuple, array or enum that holds a `Box`, `Vec`, `String` or `Rc`. Any struct counts, but
  a tuple or array of only primitives and references doesn't.
- Where the MIR doesn't prove it initialized at the stop, it is `[stale]`. That happens after
  `drop(h)`, `consume(h)`, `let g = h;` or a partial move, `let d = h.b;`. It happens wherever
  nothing is proven too: in an optimized build, a closure's or async block's capture, or a macro's
  twin functions.
- A `[stale]` value's fields show the bytes left in its own storage, each `[stale]` with a note:
  `h.n`, `h.v.len`. Every pointer inside it shows only its address, `[unproven]`. So `h.b.x` is
  refused, and `session get h` lists `h.b` as not expandable.
- In a caller frame (`--frame N`), its fields read unmarked where the MIR proves it initialized on
  the call's line, such as when it is lent as `&mut h`. Its pointers still show only their address:
  the callee may be part-way through replacing a `Box` it was lent. Where the call may move it, as
  `consume(h)` does, it is `[stale]`.
- A field or element reassigned in place (`h.b = Box::new(..)`, `w.s = String::from(..)`,
  `arr[i] = ..`) leaves the local whole, if it was whole before. A field moved out and then stored
  again (`let d = h.b; h.b = ..`) still leaves it `[stale]`.
- A data-carrying enum other than `Option<i32>` stays opaque.
- In an optimized build, parameters keep their raw fields, or show as optimized out.

#### What a `Box` shows

- A `Box<T>` local or parameter, or an array of them, shows its contents (`b.x`). It needs the
  same proof as an owned local, in a crate with no unsafe code.
- Where it may be moved or dropped, it shows only its pointer, `[unproven]`, with a note: the
  allocation may be freed and reused.
- A Box partly moved out of shows only its pointer too: `let d = bx.data;`, also
  `bx.inner.data` of a Box in a Box.
- So does a Box that a closure captures, and a pointer the MIR can't vouch for. That is a generic
  `T`, `Self` or `impl Trait` value, an `async fn`'s parameter (its future holds it twice), or a
  `#[no_mangle]` function's parameter.
- An optimized build proves nothing. There every Box, and every pointer the MIR can't vouch for,
  shows only its pointer.

#### When a reference reads

- In the innermost frame of an unoptimized build, a reference reads while the code still uses it.
  That includes a `&str`, and a tuple, `Vec` or closure that holds a reference.
- After its last use, it reads until a call or a drop runs. Only arithmetic, copies and branches
  may come between.
- Past that, it shows only its pointer: what it pointed into may be moved, dropped or reallocated.
  To read it, probe a line that still uses it.
- A line breakpoint stops where its line starts in each lexical block. So a reference reads on the
  line of its last use. A step or a pause can stop inside a line, after a call there.
- A macro that declares variables of its own, such as `println!` or `assert_eq!`, adds a stop in
  each of its lexical blocks, all at the macro's column. A reference not used later reads at the
  stops before the macro's first call, and shows only its address at those after it.
- Where Peeky can't tell the blocks apart by the stop's address, it judges every stop of the line
  as any of them. That happens for:
  - a block whose code the compiler left out;
  - an `.await`'s block;
  - two blocks that declare the same names on one line;
  - the function's own line;
  - an `#[inline(always)]` or caller frame.
- A raw pointer, also a `NonNull` or an `AtomicPtr`, keeps no borrow. It always shows only its
  address, `[unproven]`, with a note.
- That holds inside a value too: a field, a tuple or array element, at any depth. For example, with
  `let h = Holder { n: 1, p: &*b as *const i64 };`, `h.p` shows `0x…` and lists nothing under it,
  even while `h` is live. Safe code can free what it points at: `drop(b)`.
- The value that holds it still reads: `h.n`, and `t.__0` of a `(i64, *const i64)`.
- A reference or a `Box` inside a value still reads. LLDB names `&i64`, `Box<i64>` and
  `*const i64` all `long *`, so Peeky tells them apart by the field's Rust type in the build's
  debug information.
- The standard library's types hold raw pointers too. An `Rc`'s `ptr.pointer` and a slice
  iterator's `ptr.pointer` show only their address.
- Between a drop and the store of `*v = new`, `v` shows only its address. That is a stop inside
  the old value's `Drop`, then `step --out`.
- In some places every local that borrows shows only its pointer. A reference parameter that is
  never reassigned still reads there, unless the function drops what it points at. Those places
  are:
  - a stop inside a macro from another file;
  - a line the build's IR doesn't explain;
  - a function too big or unusual to analyze;
  - methods that a macro generates.
- In a crate using `#[collapse_debuginfo]`, a line that invokes a macro is judged whole.

#### Async bodies

- An async body is judged as a function is. A local that the future keeps across an `.await`
  reads while it is used ahead. One it doesn't keep shows only its pointer after the `.await`.
- An `async fn`'s parameter shows twice: as the future's field, and as the poll's copy. A reference
  parameter reads in both where they hold the same pointer. A slice or `&str` parameter needs a
  debugger that reads memory.
- A scalar parameter's poll copy is withheld, and names the other row. Where the body may have
  reassigned the parameter (`mut n`) before the stop, the field is withheld instead: it holds only
  what the caller passed.
- On a line that both the first poll and a resumed one run, such as the `.await` itself or a loop
  around it, a local that the future doesn't keep is withheld in the first poll too.
- Lines with an `.await` hide more references than the same code without `async`. In a 140-line
  async program, 145 of 899 reference rows were hidden, 103 of them on lines that hide them in its
  synchronous twin too.

How Peeky proves all this: [RUST_AGENT.md](../RUST_AGENT.md#when-a-reference-reads), for
maintainers.

### Crates with unsafe code

If a crate has any unsafe code, Peeky reads nothing from memory itself. A nightly feature counts
too, as does a source Peeky can't read ([what counts](#what-counts-as-unsafe-code)).

Unsafe code can free a buffer that safe code still shows as owned, and Peeky wouldn't see it.
Examples are `ptr::read(&w.values)` and `ptr::drop_in_place(&mut self.values)`, even in a closure
or another function.

For example, this `rawbuf` crate reads through a raw pointer once:

```rust
fn main() {
    let mut values = vec![1, 2, 3];
    let first = unsafe { *values.as_ptr() };
    let count = values.len();
    values.push(first);
    println!("{count} {values:?}");
}
```

```sh
peeky probe rawbuf --at src/main.rs:5 --capture values,values.len,first,count
```

```
hit  phase     line  function      selector    type      value
1    observed  5     rawbuf::main  values      Vec<i32>  alloc::vec::Vec<int, alloc::alloc::Global> @ 0x16fa4ef50  [note]
1    observed  5     rawbuf::main  values.len  usize     3
1    observed  5     rawbuf::main  first       i32       1  [not settable: register]
1    observed  5     rawbuf::main  count       usize     3  [settable]
...
warning: values: This Rust Vec shows LLDB's summary (its type and address), not its contents: the crate has unsafe code (in src/main.rs), which can free a buffer without Peeky seeing it. Capture values.len for the length, and check contents with `peeky verify`.
```

| Stays raw, with the reason | Still read, as LLDB reads them |
| --- | --- |
| a reference parameter's contents | scalar locals |
| a struct preview through a reference | a struct's scalar fields, in the frame or behind a reference parameter: `w.k`, `self.k` |
| an owned `Vec` or `String` local | a by-value struct's preview |
| a `&str` or slice local | an enum's variant |
| a container field | |
| a `Box`, which shows only its pointer, marked `[unproven]` | |

What is still read keeps its other conditions from [What Peeky reads](#what-peeky-reads). For
example, a by-value struct's preview still needs plain fields, the innermost frame and the MIR's
proof.

No `String` takes a text either, as in `--set 'name="x"'`. The refusal gives the same reason. A
raw pointer, or an address kept as an integer (`s.as_ptr() as usize`), could read its bytes at
their old length where no borrow shows it.

#### What counts as unsafe code

The check is broad on purpose: anything that might be unsafe code counts. Peeky checks every file
the build reads: the Rust sources, those in `tests/` too, and `Cargo.toml` and `Cargo.lock`.

- **In a Rust source,** comments, strings and char literals don't count. Nor does a shebang line.
- **In any other file,** such as `Cargo.toml`, everything counts, comments and strings included.
  A macro could `include!` that file as Rust. So a package named `unsafe-demo` counts.

**Unsafe code.** The reason given is:

```
the crate has unsafe code (in src/main.rs), which can free a buffer without Peeky seeing it
```

These count:

- the word `unsafe` anywhere: an `unsafe` block, function, impl, trait or extern block, or
  `#[unsafe(...)]`;
- `#unsafe` too, because a macro may drop the `#`. Only the raw identifier `r#unsafe` doesn't
  count;
- the words `no_mangle`, `export_name`, `link_section`, `global_asm` and `naked_asm`, anywhere.
  For example, `use std::arch::global_asm as g;` counts, and so does a variable named `no_mangle`;
- `asm!`, also written `asm !`;
- code in `macro_rules!` bodies.

These don't count: `unsafe_code`, as in `#![forbid(unsafe_code)]`, and a function named `asm`.

**A nightly feature.** The reason given is:

```
the crate enables nightly features (in src/main.rs), which can free a buffer without Peeky seeing it
```

This counts:

- an inner attribute, `#![...]`, with `feature(` anywhere inside it. Examples are
  `#![feature(x)]` and `#![cfg_attr(all(), feature(x))]`;
- the same with spaces or comments between `#`, `!` and `[`;
- an inner attribute that never closes, which runs to the end of the file.

These don't count: `#[cfg(feature = "x")]`, a function named `feature`, and `"feature(x)"` in a
string.

**A Rust source Peeky can't read as rustc does.** The reason given is:

```
Peeky cannot rule out unsafe code in src/main.rs (…), and unsafe code can free a buffer without Peeky seeing it
```

In the brackets, Peeky says which of these it found:

- `` it opens with a `---` frontmatter block, which Peeky does not read ``. Frontmatter is a block
  between `---` lines at the top of a file. Cargo scripts, a nightly feature, keep their manifest
  there.
- `` a comment follows its `#!`, which Peeky does not judge ``.
- `a comment, string or char literal does not end`.
- `its brackets do not pair up`.

If a word sets off the check where there is no unsafe code, rename it. Peeky can't tell such a word
from unsafe code.

#### What safe code can still defeat

Peeky trusts safe Rust, and the standard library, to keep what they reach alive. A compiler
soundness bug that lets safe code outlive a borrow defeats this. rustc's issue #25860, implied
bounds on function pointers as `cve-rs` uses them, gives a struct a `&[i64]` into a `Vec` dropped
since. That struct then reads freed memory with no note. Peeky doesn't look for such code.

How the check works: [RUST_AGENT.md](../RUST_AGENT.md#crates-with-unsafe-code), for maintainers.

### Names bound more than once

When two bindings of a name are in scope, `describe` normally lists each by the selector lldb-dap
gives it. The exception is two bindings on the same file name and line: they share one entry,
which no selector can name ([below](#two-bindings-on-one-line)). For this `twice` crate:

```rust
fn main() {
    let x = 5;
    let y = x + 1;
    let x = y * 2;
    let z = x + y;
    println!("{z}");
}
```

```sh
peeky describe twice
peeky probe twice --at src/main.rs:5 --capture 'x,x @ main.rs:2,x @ main.rs:4'
```

```
names in twice::main (src/main.rs:1-7): x @ main.rs:2 (i32, lines 3-6), x @ main.rs:4 (i32, lines 5-6), y (i32, lines 4-6), z (i32, line 6)
...
hit  phase     line  function     selector       type  value
1    observed  5     twice::main  x                    <not found; visible: x @ main.rs:2, x @ main.rs:4, y>
1    observed  5     twice::main  x @ main.rs:2  i32   5  [not settable: register]
1    observed  5     twice::main  x @ main.rs:4  i32   12  [not settable: register]
```

- Each entry gives the binding's type and lines: `points @ main.rs:73 (Vec<String>, lines 74-85)`
  and `points @ main.rs:81 (&str *, line 82)`.
- The selector reads the binding at any stop where it is in scope, unless it names two bindings.
  That includes stops where lldb-dap lists it by its plain name, such as `points @ main.rs:73` on
  line 76.
- Select a field after it: `s @ main.rs:81.a`.
- A plain name means the one binding in scope at the stop, by the MIR's scopes. So an earlier
  loop's `entry` doesn't hold back this loop's `entry.name`.
- Where two bindings are in scope, lldb-dap lists only the `@` names. So plain `x` is not found
  above.

#### Two bindings on one line

Two bindings on the same file name and line share one selector, which names neither. Examples are
`let x = n + 1; let x = format!("{x}!");`, and files of one name in two directories.

- `describe` lists them as one entry:
  `x @ main.rs:3 (2 bindings on this line: not selectable by line)`.
- The selector is refused with that reason.

### What `--set` can change

`--set` changes only these:

- an integer local that the program keeps in memory: `i16` to `i64`, `u16` to `u64`, `isize` or
  `usize`. Not `bool`, `char`, floats or 8-bit integers. The new value must be in the type's range;
- such an integer field or element of a struct, tuple or array local that the frame holds
  itself: `--set s.end=10`, `o.span.start=1`, `o.marks[1]=2`, `t.__0=3`, `arr[2]=7`. Its new value
  must be in range too. An element of an array of any length is fetched alone, at any step of the
  path;
- a shadowed local, by the name LLDB gives it with where it is declared: `s @ main.rs:81.a`, as
  after `let mut w = w;`, or `x @ main.rs:18=500`;
- a `String` local, to a text. A text has its own conditions: capacity, initialization, borrows,
  the debugger and the unsafe gate ([Set a String to a text](#set-a-string-to-a-text)).

Every change also needs all of these:

- an unoptimized build;
- the innermost frame. A caller's frame is never changed, because a call it is making may hold a
  copy of the old value;
- a stop where changes are allowed ([Where a change is allowed](#where-a-change-is-allowed));
- no copy of the program's own that blocks it
  ([When the program has its own copy](#when-the-program-has-its-own-copy));
- a local that isn't in a register
  ([Locals that can't change](#locals-that-cant-change-and-what-to-do)).

For a field or element, the MIR must also prove the local initialized at the stop. A change to a
moved, dropped or not yet assigned local (`drop(h)`, `let later: Span;` before its assignment)
would be lost, or overwritten by the assignment. Several can change at once:
`--set w.start=10 --set w.count=0`.

Peeky never changes these:

- anything through a reference, a raw pointer or a `Box`
  ([Change a value behind a reference](#change-a-value-behind-a-reference));
- anything inside an enum (its variants and niches), a union, a `#[repr(packed)]` struct (whose
  fields may be misaligned), or a standard-library type such as `Vec`, `String`, `Range` or
  `Option`;
- what a container holds (a `Vec`'s or `String`'s contents, a `Box`'s target) and an enum's
  variant. The refusal says `inside a value Peeky decodes itself`.

The path is read from the build's debug information, not LLDB's type names. It may select only
fields of structs and tuples your crate defines, and elements of fixed-size arrays. A refused
`--set` says which rule it broke:
`` `pk` is a packed struct (Packed): its fields may be misaligned ``.

What a change makes true: the value's storage holds the new value, and the program's later reads
of it see it. Copies that the program made itself keep the old value: `let a = s.a;` above the
stop, or a `for` loop's iterator over `0..s.n`. The stop's own line may overwrite the change:
`s = Tri { a: 9, ..s };`.

Every change is read back from the debugger. If it isn't the value asked for, the change is
reported as not made (`MUTATION_REJECTED`). If something else was stored, the session is closed.

- A struct or tuple local shown as a preview from memory (`Span { start: 1, end: 2 } [memory]`) is
  its own storage. So its fields change the same way, under the same rules. Peeky writes through
  the debugger's own variable, not the preview, then reads the preview again. It must show the new
  value too, or the session is closed.

`session inspect` marks a local `[settable]` when `set` would change it. Probe rows and
`session get` mark fields and elements too. When a rule leaves nothing settable, `session inspect`
says why.

`describe`'s `mutable:` line lists, per function, the paths that can change: `board.entries`,
`grid.cells[*].n`, with `[*]` for any element. Past its cap, it says `… and N more`. It never lists
a `Vec`'s elements, nor a reference's or slice's parts. It also names the functions where nothing
can change, with the candidate edit that can.

How Peeky proves where a change is allowed: [RUST_AGENT.md](../RUST_AGENT.md#changing-a-value), for
maintainers.

### Where a change is allowed

A change is allowed only at a line breakpoint, or after a step that lands on a line's start.
There, the MIR must show that no statement under way has read the value into a temporary. This
holds for integer locals too.

- So a change is never allowed back from a call, after `step --out`. For example, it is refused
  back from `f` in `let t = s.a + f();`.
- Nor after a step back onto `let arr = [s.a,` from its later lines.
- A refusal names the line where the statement read the value:
  `a statement under way at the stop has already read it into a temporary (at line 17)`. For a
  binding of the program's own, it names the binding: ``into `n` ``.
- A breakpoint's place on its line is told by the stop's column. With no column, the change is
  refused.
- A breakpoint on `+ f();` under `let t = s.a` is refused: the MIR shows it past the read. So is
  any stop that the MIR doesn't explain.
- A line in another file, a macro's code, is named with its file:
  `(at alloc/src/macros.rs:113)` for `format!`.
- The refusal says where a change works instead: before that statement starts. That is a stop
  on an earlier line, or an earlier stop of the same line (a probe's `--set-hit`).
- A macro's call from another crate counts for a line only where the IR puts its code there.
  After `let label: String;` and `label = format!(…);` on line 4, the first stop of line 5 is
  past the assignment: `label` reads `"sum 6"` and `sum` is `[settable]`.
- Some lines can only stop inside such a statement. An example is a statement's later line:
  `s.b];` of `let arr = [s.a,` / `k,` / `s.b];`. A probe there is refused before it runs, naming
  that line.

Peeky follows the MIR in the order the code runs. A temporary made before the local took its
current value holds an older value, so it doesn't block a change. For example, in
`for entry in args`, `entry` is moved out of the iterator's `next()` result. Still, `entry` can
change at the loop body's first line.

To get a stop where values can change, use `session continue --to LINE`, or a `session step` that
lands on a statement's first line.

### When the program has its own copy

The program may have copied the value into a binding of its own, and go on with that copy. Then
it never sees the change, so Peeky refuses it. That is the case when the copy is used, and the
local is then replaced or dropped without being read.

In [examples/rust](../../examples/rust), line 35 computes `start` from `request.page`:

```sh
peeky probe examples/rust --at 36 --capture request.page --set request.page=2
```

```
Error [MUTATION_REJECTED]: `request.page` cannot be changed at src/main.rs:36, at any stop of this build: in peeky_pagination::main, at every place line 36 can stop at, the program has already copied `request` into `start` (at line 35) and goes on with that copy, so a change there may never be seen: Peeky proves no read of the changed part first (a reference lent to a call, or `{:?}`, is not one). Refused before the program runs: nothing ran. Change it before the program copies it (`--set-at` an earlier line), or change the copy instead; settable here: start (root locals; fields too where rows mark them [settable])
hint: Change it before the program copies it (a stop on an earlier line, or `--set-at` there), or change the copy the message names instead.
```

These bindings copy a value:

- a `match` arm's binding: `x = match x { n => … n }` puts the old value back;
- a `while let`'s binding: `x = v - 1`;
- a plain copy: `let y = x;` … `x = y + 1;`;
- a `move` closure called before `x` goes out of scope unread. Writing a part, as in `s.a = t;`,
  reads none of it.

If the program reads the local again, before or after it uses the copy, it sees the change. The
copy keeps the old value as the program's own: `let a = s.x;`.

To change the value, change it before the copy is made, or change the copy.

#### What counts as reading it again

Only a read of the changed part itself counts:

- using its value: `x + 1`, `let z = s.b;`, or passing `s` by value;
- printing it as a number with `{}` in the standard library's `print!`, `println!`, `eprint!`,
  `eprintln!` or `format!`. For `println!("{}", s.b)`, the read is where it prints.

A print counts only where Peeky proves, from the MIR, that it is the standard library's:

- the call is to `std::io::_print`, `std::io::_eprint`, `alloc::fmt::format` or
  `std::fmt::format`;
- or it is to a bare `format`, as the MIR may name that function. That counts only if the crate
  defines no function named `format`;
- and its code comes from this toolchain's own `macros.rs`, not from a file in the crate.

These don't count, so the change is refused there, even where the call or print does read it:

- reading another field: `x.a`, for a change to `x.b`;
- lending a reference to a call: `ignore(&x)`, `width(&s)`;
- printing the struct with `{:?}`;
- `write!`, a panic message, or a `format_args!` passed on or dropped.

A copy counts only for the part it may have taken. `let seen = r.end;` doesn't block a change to
`o.marks[0]`.

### Locals that can't change, and what to do

Rows mark these locals `[not settable: …]`, with the reason:

- **`[not settable: register]`:** a by-value parameter, or a `let` the code never borrows or
  reassigns, lives only in a register. Keep it in memory with a candidate edit
  ([6.6](#66-see-what-a-fix-does-before-making-it)).
- **`[not settable: another binding in a register (line 4)]`:** the function binds the name twice.
  It is settable only when both bindings are in memory. Black-box the `let` that the refusal names.
- **`[not settable: inlined]`:** it is a local of an `#[inline(always)]` function. Change the value
  in the caller.
- **`[not settable: async state]`:** an async body keeps it across an `.await`. The poll reaches it
  in the future's state through a pointer. Copy it into a local after the `.await` with a candidate
  edit, and set that.

- A probe that sets one is refused before the program runs. The exception is when another
  function on the line keeps it in memory, such as a closure or an inlined copy.
- `describe`'s `mutable:` line lists them per function. Each binding of a name bound twice gets the
  same reason there: `bind::shadow: x @ main.rs:6 (line 4)`.
- To keep a local in memory, add `std::hint::black_box(&mut x);` after its `let`. For a parameter,
  declare it `mut x` and borrow it first thing ([6.6](#66-see-what-a-fix-does-before-making-it)).

### Change a value behind a reference

Peeky never writes through a reference or a `Box`. What it reaches may be freed or moved. Change
the value where it is owned, before the call that passes it.

In `limiter` ([6.6](#66-see-what-a-fix-does-before-making-it)), `allow` gets `w: &mut Window`. So
`w.start` can't change in `allow`. Set it in `main`, which owns `w`, at the call's line 30:

```sh
peeky probe limiter --at src/main.rs:30 --arg 2 --arg 10 --arg 0,1,10,11 --capture w \
  --hits 3 --set-hit 3 --set w.start=10 --set w.count=0
```

```
hit  phase      line  function       selector  type             value
1    observed   30    limiter::main  w         limiter::Window  Window { start: 0, count: 0 }  [memory]
2    observed   30    limiter::main  w         limiter::Window  Window { start: 0, count: 1 }  [memory]
3    observed   30    limiter::main  w         limiter::Window  Window { start: 0, count: 2 }  [memory]
3    after-set  30    limiter::main  w         limiter::Window  Window { start: 10, count: 0 }  [memory]
set w.start: 0 -> 10
set w.count: 2 -> 0
...
output:
  ACCEPTED:0,1,10,11
```

To stop at the right call, use one of these:

- a probe `--at` the call's line, with `--hits N --set-hit N`;
- `session start --at` the call's line, with `--hit N`.

N counts the call line's own stops, not `allow`'s. They differ when `allow` is called from several
lines. To find N, run `peeky probe ... --at <call line> --capture now --hits all`.

These don't reach the stop you need:

- `session continue --to` the call's line reaches only the next call.
- `session step --out` returns part-way through the line, where changes are refused.

### Set a String to a text

A `String` local takes a text in double quotes: `--set 'name="pear"'`. Peeky writes the bytes and
`len` into the buffer the `String` already has. It never reallocates. For this `label` crate:

```rust
fn main() {
    let mut name = String::with_capacity(16);
    name.push_str("apple");
    let count = 2;
    let head = &name[..count];
    let size = head.len();
    println!("{name} {head} {size}");
}
```

```sh
peeky probe label --at src/main.rs:4 --capture name --set 'name="pear"'
```

```
hit  phase      line  function     selector  type    value
1    observed   4     label::main  name      String  "apple"  [memory]  [settable]
1    after-set  4     label::main  name      String  "pear"  [memory]  [settable]
set name: "apple" -> "pear"
...
output:
  pear pe 2
```

The text must fit the `String`'s capacity. Here `name` holds 16 bytes:

```sh
peeky probe label --at src/main.rs:4 --capture name --set 'name="seventeen bytes!!"'
```

```
hit  phase     line  function     selector  type    value
1    observed  4     label::main  name      String  "apple"  [memory]  [settable]
status: failed  hits: 1 before it failed  seconds: 1.285
error [MUTATION_REJECTED]: `name` cannot be changed: the text does not fit: `name`'s buffer holds 16 bytes, and the text is 17 bytes. Peeky never reallocates a String; a longer text needs an edit to the source; settable here: name
hint: Pass a shorter text (the message says how much fits), or change the source with a candidate edit (`--old`/`--new`).
...
```

These rules apply too:

- It needs what every change needs ([What `--set` can change](#what---set-can-change)).
- The MIR must prove the local initialized at the stop. A moved or dropped `String`'s buffer may be
  freed.
- The buffer is on the heap, so the debugger must write memory. lldb-dap 21 does, and macOS ships
  it. With lldb-dap 18 or 20, the change is refused.
- A `&str` is refused before the program runs. It borrows text that may be read-only. So are a
  `&String` and any local other than a `String`. A `String` field or element is never changed.
- Peeky reads the `String`'s pointer, `len` and `cap` at the offsets the debug information gives.
  A text longer than `cap` is refused, and nothing is written. After the write, Peeky reads the
  three words and the bytes back. Any difference closes the session.
- A crate with unsafe code or a nightly feature takes no text at all, with the reason
  ([Crates with unsafe code](#crates-with-unsafe-code)).

#### Borrows that block a text

A `String` takes no text while a borrow of it is used after the stop. A borrow is any of these:

- a `&str` of it: `let head = &s[..2];`;
- a reference, or an iterator such as `s.chars()`;
- anything built from a borrow, or given one. Examples are a `Vec<&str>` it was pushed into, a
  closure that borrows it, and a `Box<dyn Fn() + '_>` or `Pin<Box<dyn Future + '_>>` holding one.

Such a borrow keeps the old length. It would read a mix of old and new text, or bytes that are not
UTF-8. In `label`, `head` borrows `name` on line 5, and is used on lines 6 and 7. So a stop on
line 6 refuses the text:

```sh
peeky probe label --at src/main.rs:6 --capture name --set 'name="pear"'
```

```
hit  phase     line  function     selector  type    value
1    observed  6     label::main  name      String  "apple"  [memory]
status: failed  hits: 1 before it failed  seconds: 1.244
error [MUTATION_REJECTED]: `name` cannot be changed to a text at this stop: change it before a borrow of it is taken, or after the borrow's last use. A borrow of it (a `&str`, a reference or an iterator) is still used after the stop, and a borrow keeps the old length: it would read old and new text mixed, or bytes that are not UTF-8; settable here: none
hint: Observe the value instead, or change the source with a candidate edit (`--old`/`--new`) and verify it; rows marked [settable] can be changed here.
...
```

Change it before the borrow is taken, or after its last use. A `len()` or a clone keeps no borrow.

#### What may hold a borrow

A value holds no borrow only where its type provably holds no pointer. These types hold none:

- numbers and `String`;
- arrays, tuples and standard containers of those;
- a struct, enum or union of your crate, without type or lifetime parameters, whose fields are all
  such types or `Box`es of them. Peeky reads the fields from the debug information.

Examples that hold none are `Vec<Span>` for `struct Span { start: u32, end: u32 }`, a C-like
`enum`, and `struct Rec { v: u32, next: Option<Box<Rec>> }`.

These may hold one: a trait object, an `impl` type, a type parameter, or another type of your
crate.

#### Drops

Dropping a value counts as using its borrow where the drop may run code that reads it. Examples are
a guard whose `Drop` reads its `&str`, a trait object, a closure, and a type of your crate. A
standard container of references, such as `Vec<&str>`, doesn't count. A `Box` dropped before the
stop drops nothing at its scope's end.
