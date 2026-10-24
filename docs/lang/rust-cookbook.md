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
  - [Lines with no breakpoint](#lines-with-no-breakpoint)
  - [See where nothing can change, and the way around it](#see-where-nothing-can-change-and-the-way-around-it)
- [2.1 Fields and Indexes](#21-fields-and-indexes)
  - [Read through a reference or a Box](#read-through-a-reference-or-a-box)
- [4.8 Compare Addresses Across Runs](#48-compare-addresses-across-runs)
  - [Find who sits at a pointer's old slot](#find-who-sits-at-a-pointers-old-slot)
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

`describe` also leaves out the bindings that the compiler makes for a `?`: `residual` and `val`.
The debug information lists them as locals, without marking them as the compiler's. In the
build's MIR, each one's span ends right after the `?`: `residual` spans only the `?`, and `val`
the expression it tries, through the `?`. A binding you write spans its name. So a local that you name
`val` or `residual` is still listed. Where Peeky can't tell, the binding is listed.

It leaves out a `for` loop's `iter` too, such as `iter @ main.rs:32`: the binding that holds the
loop's iterator. In the MIR, its span is the loop's iterator expression, such as `1..=weeks`, so
the loop's `{` comes right after it. A local you name `iter` is still listed.

`session inspect` and probes leave out these bindings of the compiler's too, the `?`'s and the
loop's. So a `visible:` list doesn't name them, and a selector can't read them.

A local you name `iter` beside such a loop is still `iter`, as `describe` lists it. lldb-dap lists
it as `iter @ main.rs:2` where the loop's `iter` is in scope too. The plain name selects it all
the same, since the other binding is the compiler's.

### Read types as the program spells them

Types are spelled as the program spells them: `Vec<u32>`, `String`, `Option<i32>`. This holds in
`describe` and in probe rows. A row's `debugger_type` keeps LLDB's spelling, such as
`alloc::vec::Vec<unsigned int, alloc::alloc::Global>`.

References and raw pointers are spelled so too: `&rota::Engineer`, `&mut Vec<u32>`, `&&str`,
`*const kitty::Friend`. LLDB writes them as C would: `rota::Engineer *`
([Types in rows](#types-in-rows)).

### Lines with no breakpoint

A line with no code of its own may have no breakpoint, such as `continue;`, `break;` or a lone
`}`. The compiler gives its jump to a line nearby, such as the `if` above it. So `describe`'s
breakpoint lines may skip it: `38-39, 43-44` for an `if` on lines 38 to 42 with `continue;` on 40.

- An `--at` on such a line moves to another line with code, which the debugger picks, and Peeky
  says so: `values are from line 43`.
- To see whether the `continue` runs, stop on the line before it, the `if`. Then `session step`:
  the next stop shows where the program went.

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

## 4.8 Compare Addresses Across Runs

Read [COOKBOOK 4.8](../COOKBOOK.md#48-compare-addresses-across-runs) first.

### Find who sits at a pointer's old slot

The `kitty` crate settles a trip's shared expenses. `parse` keeps each payer as a raw pointer into
`trip.friends`, which holds the friends in the order they are first named. Then `main` sorts
`trip.friends` by name. `settle` credits the friend whose address equals the payer's
(`ptr::eq`).

So which friend does each payer's pointer find after the sort? One probe reads the pointers, in
`parse`. Another reads each slot's address and name, in `settle`:

```sh
peeky probe kitty --at src/main.rs:58 --capture 'payer @ main.rs:54,cents @ main.rs:49' \
  --hits all -- 'Åsa:4500:Åsa,Bo,Zoë' 'Zoë:1000:Zoë,Chloé,Bo' 'Bo:900:Åsa,Chloé'
peeky probe kitty --at src/main.rs:67 --capture 'f,f.name' --hits 4 \
  -- 'Åsa:4500:Åsa,Bo,Zoë' 'Zoë:1000:Zoë,Chloé,Bo' 'Bo:900:Åsa,Chloé'
```

```
hit  phase     line  function      selector            type                  value
1    observed  58    kitty::parse  payer @ main.rs:54  *const kitty::Friend  0x0000000102ec5800  [unproven]
1    observed  58    kitty::parse  cents @ main.rs:49  i64                   4500  [not settable: register]
2    observed  58    kitty::parse  payer @ main.rs:54  *const kitty::Friend  0x0000000102ec5850  [unproven]
2    observed  58    kitty::parse  cents @ main.rs:49  i64                   1000  [not settable: register]
3    observed  58    kitty::parse  payer @ main.rs:54  *const kitty::Friend  0x0000000102ec5828  [unproven]
3    observed  58    kitty::parse  cents @ main.rs:49  i64                   900  [not settable: register]
...
evidence: saved as run 7; ...
hit  phase     line  function       selector  type            value
1    observed  67    kitty::settle  f         &kitty::Friend  0x0000000102cad800
1    observed  67    kitty::settle  f.name    String          "Bo"  [memory]
2    observed  67    kitty::settle  f         &kitty::Friend  0x0000000102cad828
2    observed  67    kitty::settle  f.name    String          "Chloé"  [memory]
3    observed  67    kitty::settle  f         &kitty::Friend  0x0000000102cad850
3    observed  67    kitty::settle  f.name    String          "Zoë"  [memory]
4    observed  67    kitty::settle  f         &kitty::Friend  0x0000000102cad878
4    observed  67    kitty::settle  f.name    String          "Åsa"  [memory]
...
evidence: saved as run 8; ...
```

The two runs' heaps start at different addresses: `0x102ec5800` and `0x102cad800`. So join them on
each address's offset from its run's lowest:

```sh
peeky sql "WITH a AS (SELECT run_id, hit, phase, selector, value,
    address(value) - MIN(address(value)) OVER (PARTITION BY run_id, selector) AS from_base
  FROM evidence WHERE run_id IN (:payers, :slots) AND phase = 'observed' AND address(value) > 0)
SELECT p.hit AS expense, c.value AS cents, p.from_base, n.value AS credited FROM a p
  JOIN evidence c ON c.run_id = p.run_id AND c.hit = p.hit AND c.phase = p.phase
    AND c.selector = 'cents @ main.rs:49'
  JOIN a f ON f.run_id = :slots AND f.phase = p.phase AND f.selector = 'f'
    AND f.from_base = p.from_base
  JOIN evidence n ON n.run_id = f.run_id AND n.hit = f.hit AND n.phase = f.phase
    AND n.selector = 'f.name'
  WHERE p.run_id = :payers AND p.selector = 'payer @ main.rs:54' ORDER BY p.hit" \
  --param payers=7 --param slots=8
```

```
expense  cents  from_base  credited
1        4500   0          "Bo"
2        1000   80         "Zoë"
3        900    40         "Chloé"
(3 rows)
```

Åsa's 4500 goes to Bo, and Bo's 900 to Chloé: the sort moved them into the payers' old slots.
Zoë's slot didn't move, so her balance is right.

- The base is slot 0 in both runs. The first payer is the first friend added, so slot 0 is the
  lowest of the payers' pointers. At line 67, `f` visits every slot, slot 0 first.
- A `Friend` is 40 bytes, so offset 80 is slot 2.
- Pass the run numbers that `evidence: saved as run N` printed. A `--label` may name several runs.
- Each side keeps `observed` rows. A run with `--set` or `--set-at` also has `after-set` rows.
  Mixed in, they would move the base and pair the wrong hits
  ([COOKBOOK 4.8](../COOKBOOK.md#48-compare-addresses-across-runs)).
- `address(value) > 0` skips values that read negative, such as sentinels and pointers tagged in
  their top bit, so a pointer set to `usize::MAX` never becomes the base.
- Call `address` on a pointer, such as `f` or `e.payer`. On a struct's preview, it reads the first
  hex token, whichever field holds it.
- `[unproven]` on `payer @ main.rs:54` is about what the raw pointer points at. Its address is the
  pointer's own value.
- Read the slots after the sort, as here. At line 96, the sort's comparator sees some friends as
  copies on the stack (`a 0x000000016d67aaf8`), which aren't slots.

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

The refusal ends with the integer locals that may be set at that line instead: `settable here:`.
It leaves out every local the build rules out at each stop of the line, so never the one it
refuses. Where none is left, it says `no root locals`.

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
candidate: In the edited file, locations name the candidate's copy, since the line numbers are the candidate's
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
| `Option` of a tuple or struct, such as `Option<Engineer>` | `Some(Engineer { name: "Björn", shifts: 1, .. })` or `None`, marked `[active-variant]` | a listed enum; plain `T`; memory; safe crate, if `T` holds a `String`; innermost; initialized |
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
  stay opaque, with a note ([Options of tuples and structs](#options-of-tuples-and-structs)).
- **unoptimized:** an unoptimized build. In an optimized build, an enum shows
  `<unverified Rust enum value>`.
- **plain fields:** all its fields are integers, `bool`, floats or `char`
  ([Tuples and structs](#tuples-and-structs) lists the other limits).
- **plain `T`:** an integer, `bool`, float or `char`, or a tuple or crate struct with such a field
  or a `String` one. Its other fields show `not read:`
  ([Containers of structs](#containers-of-structs)).
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
- A reference or raw pointer local or field shows its Rust type too: `f  &kitty::Friend` for a
  `&Friend` loop item, `*const kitty::Friend` for a raw pointer, `&&str` for a `&str` loop item.
  `&[u32; 3]` and `*const [u32; 3]` differ too, though LLDB writes both `unsigned int (*)[3]`.
  `describe` spells them the same way. The name is the build's debug information's. It is taken
  only where LLDB's type has as many pointer levels, and spells what the last one points at, or
  an array there. `debugger_type` keeps LLDB's.
- A raw pointer also has its `[unproven]` mark and its note, and in JSON
  `unproven_pointer: "raw"`. A `Box` keeps LLDB's type, such as `int *`.
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
`char`, or a tuple or crate struct with such a field or a `String` one
([Containers of structs](#containers-of-structs)).

- This needs an unoptimized build, and a debugger with `readMemory`.
- A `&mut` parameter reads only in the innermost frame. In a caller, the call under way may be
  changing what it points at.
- So in a caller frame, a `&mut` reference to a `Vec`, `String` or slice shows its raw fields
  `[stale]`, each saying why. Peeky doesn't check whether the call under way was lent it. In
  `friends.retain_mut(decide)`, `friends.len` may be mid-change. No note offers it as the length. A
  shared reference still reads there.
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

- a struct or tuple local with any other field, such as a `String`, an enum or a reference, with a
  note. In a `Vec` or slice, such elements still read field by field
  ([Containers of structs](#containers-of-structs));
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

#### Containers of structs

A `Vec` or slice of tuples, or of structs your crate defines, reads element by element. Each
element needs at least one integer, `bool`, float, `char` or `String` field. The container needs
its own proof, as above. In the `kitty` crate, `settle(friends: &mut [Friend], …)` gets
`Friend { name: String, paid: i64, owes: i64 }` elements:

```sh
peeky probe kitty --at src/main.rs:72 --capture 'friends,friends[1].name,friends[1].paid' \
  -- 'Åsa:4500:Åsa,Bo,Zoë' 'Zoë:1000:Zoë,Chloé,Bo' 'Bo:900:Åsa,Chloé'
```

```
hit  phase     line  function       selector         type                  value
1    observed  72    kitty::settle  friends          &mut [kitty::Friend]  [Friend { name: "Bo", paid: 0, owes: 0 }, Friend { name: "Chloé", paid: 0, owes: 0 }, Friend { name: "Zoë", paid: 0, owes: 0 }, Friend { name: "Åsa", paid: 0, owes: 0 }]  [memory]
1    observed  72    kitty::settle  friends[1].name  String                "Chloé"  [memory]
1    observed  72    kitty::settle  friends[1].paid  i64                   0  [memory]
```

- A `String` field reads as text, as a `String` local does. `friends[1].name.len` is its length
  in bytes.
- A `String` whose bytes aren't valid UTF-8 shows `not read:` and says so. None of its text shows.
- Each text is a memory read of its own. So Peeky reads the texts of a container's first 16 `String`
  fields. A later one shows `not read:`, with its length.
- Any other field shows `not read:` and why: a raw pointer, a `Vec`, a `&str`, an enum or a nested
  struct. Read it through a reference to the element, such as `e.payer` of `for e in expenses`.
- Where a `for` loop's item that refers to such an element is in scope, the note names it.
  Nothing proves the loop goes over this container, nor which element the item is at a stop. So
  `team[1].leave` says ``if the `for` loop's item `e` is `team[1]` at a stop, read `e.leave`; Peeky
  can't prove it is``.
- The element's value leaves such a field out: `Expense { cents: 4500, .. }` in a struct, and `_`
  in a tuple, as in `(_, 3)`.
- A container of wide elements shows those that fit one 4096-byte read, then `(first 102 of 300)`.

These stay raw, with a note that says why:

- a `Vec<String>`, a `&[&str]`, or another container of standard-library values, such as
  `Range<usize>`;
- a container of enums, of packed structs, or of structs with more than 16 fields or 256 bytes;
- a container of elements with no field Peeky reads, such as `Vec<(Vec<i64>, &str)>`;
- a container with no proof, such as a caller's `&mut` parameter, or an owned `Vec` the line
  moves. A raw slice's `friends.data_ptr` is not read through: select `friends[0]` instead.

#### Options of tuples and structs

An `Option` of a tuple or struct your crate defines reads from memory, as derived `Debug` prints
it. In the eleventh evaluation's `rota` crate, `last: Option<Engineer>` holds a clone of last
week's engineer:

```sh
peeky session start rota --at src/main.rs:39 --hit 13 -- 10 Ana:5,6 Björn:1,2,3,4 Chloé Dev:9
peeky session get last last.__0
```

```
last: Option<rota::Engineer> = Some(Engineer { name: "Björn", shifts: 1, .. })
  last.__0: rota::Engineer = Engineer { name: "Björn", shifts: 1, .. } (children: peeky session get last.__0)
last.__0: rota::Engineer = Engineer { name: "Björn", shifts: 1, .. }
  last.__0.name: String = "Björn" (children: peeky session get last.__0.name)
  last.__0.shifts: u32 = 1
  last.__0.leave: Vec<u32> = <no readable value: Peeky reads the integer, bool, float, char and String fields of what an `Option` holds, and `leave` has type `Vec<u32>`: read it through `x` of `if let Some(x) = &last`>
```

- The row is marked `[active-variant]`. `None` shows as `None`, with no children.
- `last.__0` is what `Some` holds: `__0` is LLDB's name for its field. Select its fields through it:
  `last.__0.name`.
- Its fields read as a container element's do: integers, `bool`, floats, `char` and `String`. Any
  other field shows `not read:`, and the value leaves it out (`..`).
- Peeky never changes it: `--set last.__0.shifts=3` is refused.
- `describe` lists these types on its `qualified enum views:` line.
- A struct with lifetimes reads too, such as `Option<Pick<'a>>`.

How Peeky tells `None` from `Some`:

- The build's debug information gives the layout. `None` has either a tag of its own, or a niche: a
  value that a field inside the payload can never hold.
- `Engineer`'s niche is the capacity of `name`, which never exceeds `isize::MAX`. `None` stores
  2^63 there.
- A niche must be in a field whose type fixes its valid values: a `Vec`'s or `String`'s capacity, a
  `Box`, a reference, a `NonNull`, a `bool` or a `char`.
- A tag that holds neither variant's value is no `Option`. It shows `<unverified Rust enum value>`,
  never a guess.

It needs the same proofs as an owned `Vec` local: the innermost frame of an unoptimized build, a
debugger with `readMemory`, and the MIR's proof that it is initialized at the stop. Its MIR type
picks the layout. If `T` holds a `String`, the crate must have no unsafe code too.

These stay opaque:

- an `Option` inside another value, such as a field `h.p`;
- an `Option` of anything else, such as `Option<u32>`, `Option<String>`, `Option<Box<T>>` or
  `Option<&T>`;
- an `Option` of a tuple with a `String` in it, such as `Option<(String, i64)>`. The MIR names its
  type `std::string::String`, and the debug information `alloc::string::String`;
- any other enum with data, such as `Result` or your own.

An opaque enum shows `<unqualified Rust enum layout>` or `<unverified Rust enum value>`. Its note
says why, where the value is stored and what to read instead:

```
warning: count: Peeky decodes `Option<i32>`, and an `Option` of a tuple or struct your crate defines whose layout the debug information proves; it does not decode this `Option<u32>`. It is stored at 0x000000016fb8ece8; read what `if let Some(x) = &count` binds instead.
```

What it says to do depends on the enum and on why:

- an `Option`: what `if let Some(x) = &count` binds;
- a `Result`: what `if let Ok(x) = &parsed` or `Err(e)` binds;
- any other enum: what a `match` arm on it binds;
- a value the MIR doesn't prove initialized: there may be nothing to borrow. Read it on a line where
  it is assigned and not yet moved or dropped, such as the one after its assignment;
- a caller frame's value: stop at a line of its own function;
- an optimized build: read it in an unoptimized one.

The address is printed as a pointer is, so you can compare a pointer with it. With lldb-dap 18,
which can't read memory, rota's `last` is withheld. Its note says `It is stored at
0x0000fffffffff1c0`. `l` of `if let Some(l) = &last` holds that address too. So `l` points into
`last`, not into `team`. In SQL, `address(note)` reads it.

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
- A closure's captured `&str`, such as `name` in `|f| f.name == *name`, keeps LLDB's summary. Its
  note says Peeky doesn't judge what a capture points at. Read it in the function that makes the
  closure.
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
1    observed  4     cells::longest  cell         &&str    "Ana"  [memory]
1    observed  4     cells::longest  cell.length  usize    3  [memory]
1    observed  4     cells::longest  cells        &[&str]  &[&str] @ 0x16b0faf20
2    observed  4     cells::longest  cell         &&str    "Renée"  [memory]
2    observed  4     cells::longest  cell.length  usize    6  [memory]
2    observed  4     cells::longest  cells        &[&str]  &[&str] @ 0x16b0faf20
...
warning: cells: This value is LLDB's summary (its type and address), not its contents: check contents with `peeky verify`. Its `&str` items read as text where the code binds one (`for cell in cells`: `cell`). Capture cells.length for the length. Peeky reads Rust contents only for String, &str, and containers of integer, bool, float or char elements, or of tuples and structs your crate defines (their integer, bool, float, char and String fields).
```

The details:

- `cell` is a `&&str`, which LLDB types `&str *`. It reads under the same proof as a `&str` local.
  It reads in the innermost frame, in a crate with no unsafe code, where the MIR shows it still
  alive.
- The `&str` that `cell` points at outlives it, so only `cell` itself must be alive.
- Past that point, `cell` shows only its pointer, marked `[unproven]`. Its note says so. Past that
  point means it isn't used ahead, and a call or drop may have come since its last use. The note
  names that call or drop, and its line.
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
- So does a `Vec<&str>` that the line moves, such as `sharers` in `Ok(Expense { payer, cents,
  sharers })`. Its note says the MIR doesn't prove it initialized there, and its fields are marked
  `[stale]`.
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
- Elements may be scalars, or tuples and crate structs, as in containers
  ([Containers of structs](#containers-of-structs)).
- Behind a caller's `&mut` reference parameter, such a field's own fields, such as
  `self.values.len`, show `[stale]`: the call may be changing them.
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
| `[stale]` | Bytes that aren't a trustworthy value: a moved or dropped local's leftovers, or a caller's container that may be mid-change. The note says which. |
| `[unproven]` | Only a pointer: Peeky can't prove that what it points at is still there. |
| `<unverified ...>` | A value Peeky can't decode with certainty. |
| an opaque enum | An enum Peeky can't decode with certainty. Its note says why, where it is stored and what to read instead. `describe` counts them as "other enum types". |

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

`[stale]` marks bytes that aren't a trustworthy value. They are either a local's leftovers, or a
caller's container that may be mid-change. The note says which:

- Leftovers: the local may be moved, dropped, partly moved or not yet assigned, or nothing proves it
  (below).
- Mid-change: a caller's `Vec`, `String` or slice that the call under way may have been lent
  ([Reference parameters](#reference-parameters)). Peeky doesn't check whether it was.

The details:

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
- An owned `Vec` or `String` in a caller frame is different: the call under way may have it lent as
  `&mut`, and Peeky doesn't check whether it does. Its own fields, such as `v.len`, show `[stale]`,
  each saying why. So do those of one held in a struct field, such as `h.v.len`.
  `v.retain_mut(…)` may be part-way through.
- A field or element reassigned in place (`h.b = Box::new(..)`, `w.s = String::from(..)`,
  `arr[i] = ..`) leaves the local whole, if it was whole before. A field moved out and then stored
  again (`let d = h.b; h.b = ..`) still leaves it `[stale]`.
- A data-carrying enum stays opaque, apart from an `Option` that Peeky reads
  ([Options of tuples and structs](#options-of-tuples-and-structs)). One the MIR doesn't prove
  initialized shows `<unverified Rust enum value>`, with a note.
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
  may come between. A call of `std::ptr::eq` or `std::ptr::addr_eq` counts as arithmetic here: it
  only compares two addresses.
- Past that, it shows only its pointer: what it pointed into may be moved, dropped or reallocated.
  To read it, probe a line that still uses it.
- Its note says what may have run since its last use, and on which line. For example: "`first` is
  not used ahead, and a call on line 47 may have run since its last use".
- A line breakpoint stops where its line starts in each lexical block. So a reference reads on the
  line of its last use. A step or a pause can stop inside a line, after a call there.
- Making a reference into a raw pointer is a use of it, and its last one in
  `if ptr::eq(f, e.payer)`. The raw pointer made from it is no use of `f` later.
- Peeky counts a line breakpoint as maybe past a call that the compiler inlined, even at
  opt-level 0. That doesn't matter for `ptr::eq`, so `f.name` reads on that line. It does for
  `if same(f, e.payer)`, where `same` is your own `#[inline(always)]` function. There the note says
  the stop may be past a call on this line.
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
- A raw pointer, `*const T` or `*mut T`, keeps no borrow. It always shows only its address,
  `[unproven]`. Its note says why: "Peeky never follows a raw pointer, because nothing proves that
  what it points at still stands."
- Its row has `unproven_pointer: "raw"`. Where the build's debug information doesn't tell a pointer
  inside a value from a raw pointer, it shows only its address with `unproven_pointer: "unknown"`.
- A `NonNull` or `AtomicPtr` local is judged as before. Where it shows only its pointer, it has the
  generic `[unproven]` note, not this one. The raw pointer inside one is a raw pointer inside a
  value: `o.q.pointer` of a `NonNull` field.
- A raw pointer inside a value shows only its address too: a field, a tuple or array element, at any
  depth. For example, with `let h = Holder { n: 1, p: &*b as *const i64 };`, `h.p` shows `0x…`
  and lists nothing under it, even while `h` is live. Safe code can free what it points at:
  `drop(b)`.
- The value that holds it still reads: `h.n`, and `t.__0` of a `(i64, *const i64)`. An array of
  raw pointers, such as `arr: [*const Pt; 2]`, lists its elements, and each shows its address.
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
| an `Option` of a struct or tuple with a `String` field | an `Option` of a struct or tuple without one |
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
  and `points @ main.rs:81 (&&str, line 82)`.
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

To change a value at a given stop of any line of the `--at` file, use `--set-at LINE@HIT
NAME=VALUE` ([COOKBOOK 2.8](../COOKBOOK.md#28-change-a-value-then-restore-it)). HIT counts that
line's own stops. Repeat it for several stops: in `kitty`, `--set-at 72@1 payer=3 --set-at 72@3
payer=0` credits the first and third expenses to other friends in one run.

`--set-at LINE@all NAME=VALUE` changes it at every stop of the line, up to 100
([COOKBOOK 2.8](../COOKBOOK.md#change-it-at-every-stop-all)). On a line that runs only in some case, that tests a change in that case.
The value must be the stopped frame's own: a field through a `&mut` parameter is refused before
the run, `@all` or not.

`--set-at LINE@HIT NAME=@before` writes back an integer as it was before the probe's change at
the same hit ([COOKBOOK 2.8](../COOKBOOK.md#skip-a-statement-restore-with-before)), only to the
binding its change wrote, as the MIR's scopes tell it. To find where
each function on the restore line begins, Peeky reads the crate's debug information once more
before the run. A restore line in an async fn or async block is refused before the run: each poll
runs on part-way through it, so its calls can't be told apart.

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
- Some calls are inlined even in an unoptimized build: an integer's `clone`, and your own
  `#[inline(always)]` functions. Their code may come before the line's first instruction of its
  own, where the line's breakpoint stops.
- So at a breakpoint on `let y = x.clone();`, `x` is refused: the inlined `clone` may already have
  read it. Peeky reads where each inlined call is made from, and its addresses, from the build's
  debug information.
- In safe code, only what such a call is given, or what may point into it, is refused. In
  `let y = z.clone() + x;`, `z` is refused, but `x` can change: the line reads it after the stop.
- In a crate with unsafe code, such a call may read a local through a pointer it isn't given,
  such as one a `static mut` holds. So every local is refused there. Only the standard library's
  `ptr::eq` and `ptr::addr_eq` are exempt.
- Inlined code just before the stop whose call Peeky can't place refuses every local too. That is
  a call made from a macro's definition, as `$v.clone()` in `mclone!(x)`, or from another file.
  It is so too where the build's IR and debug information name different inlined calls.
- A `session step` from the line before stops at the inlined call's first instruction, before it
  runs. There, `x` can change. A macro's code is the exception: LLDB shows that stop on the
  macro's definition, so change the value on an earlier line instead.
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
- A copy that the statement stores through a pointer counts too. `vec![x, 1]` stores `x`'s copy
  in its Box through a raw pointer. So at `+ other();` under `let t = total(vec![x, 1])`, `x` is
  refused. The note says the statement reads it at the first line, and may hold a copy.
- What a function of the crate's returns is never said to hold a copy for sure. `total` may
  return anything, so the note says the statement may hold one. Only the standard library's own
  step that makes `vec!`'s Vec of its Box passes on what the Box holds.
- What a call returns may be a copy of the local, where the call is given a pointer into it:
  `let y = x.clone();`, `x.to_string()`, `ptr::read(&x)` or `format!("{}", x)`. Such a binding
  blocks a change after it ([When the program has its own copy](#when-the-program-has-its-own-copy)).
- Only the standard library's measures and comparisons are proven to return no copy: `len`,
  `is_empty`, `contains`, `starts_with`, `eq`, `lt`, `cmp`, `partial_cmp` and the like. Each must
  be a method of a standard type, and return a `bool`, `usize` or `Ordering` the local can't hold.
- A crate type of its own named `String` or `Vec` is not the standard one. Its `String::len` may
  return a copy, so it is counted as one.
- A call given only a `&` may still store a copy, where what it points at holds a cell, a lock, an
  atomic or a channel, however deep. After `send(&tx, x)`, with `tx` an `mpsc::Sender`, the
  channel holds `x`, and `rx.recv()` reads it.
- So after such a call, `x` is refused, naming what may hold the copy:
  ``the program's own `rx` (at lines 54 and 55) may hold a copy of `x` that it goes on with``.
- Your own types count by their fields, as the build's debug information shows them. So
  `bump(&c, x)`, with `c = Counter::new()` and a `Cell` field in `Counter`, refuses `x` after it.
  A struct of numbers and `Vec`s, lent by `&`, leaves `x` settable
  ([Copies behind a cell or a channel](#copies-behind-a-cell-or-a-channel)).

Peeky follows the MIR in the order the code runs. A temporary made before the local took its
current value holds an older value, so it doesn't block a change. For example, in
`for entry in args`, `entry` is moved out of the iterator's `next()` result. Still, `entry` can
change at the loop body's first line.

#### Reads through a pointer Peeky can't trace

Peeky doesn't follow what a reference variable points at, such as a `for` loop's `e`. So a read
through one may have read any local, and a call given one may return a copy of any local. That
holds for raw pointers too, whether or not the source has `unsafe`. A standard-library macro can
bring unsafe code that reads through one.

- The standard library's `ptr::eq` and `ptr::addr_eq` read only the two addresses. So a call of
  one holds a copy only of what its pointers hold, never of what they point at.
- That is so only where Peeky proves the call is the standard library's. The crate must have no
  item of its own named like the root the call is under (`mod std`, `use x as core`), nor its own
  function of that name.
- A breakpoint on `if ptr::eq(e, l) {` stops inside the inlined `ptr::eq`. There, `week` and
  `pick` are `[settable]`.

A refusal says that the statement has read the local only where the MIR shows that read on every
path to every place the stop can be. A read through an aggregate that also holds other pointers
doesn't count, nor a read of another field than the one a change makes. Nor does a read the
program has overwritten since, as `s.a = 3;` does for `s.a`, or written through a pointer that may
point into it. Where the function lends the local with `&mut` or a raw pointer, a later call or
drop may change it, so nothing read before counts either. What a call returns never counts, nor
what it was given, since the callee may do anything with it.

- It names your binding, as in ``has already copied its value into `n` ``, only where `n` gave
  the copy at every such place.
- Where each place holds a copy another way, it says only `the program already holds a copy of it`.
- Where your binding only may hold a copy, it says so, apart from the statement, which may not use
  it: ``the program's own `y` (at line 34) may hold a copy of it that it goes on with``.
- Otherwise it says what the statement does, and that it may hold a copy:
  - ``reads it into a temporary (at line 12)``: the read may be on another path, or past the stop;
  - ``reads another part of it into a temporary (at line 12)``, for a change to one field;
  - ``uses `week` (at line 32), which may hold the program's own copy of it``, after
    `let week = weeks;` on some path;
  - ``uses a pointer that may point into it (at line 13)``: a read through it, or a call given
    it, which may read through it or not;
  - ``uses `p` and `q` (at line 23), raw pointers Peeky can't trace that may point at it``. Each
    name is called what it is: a reference, a raw pointer, a closure, or a value holding a
    pointer.

Peeky doesn't rule out a local by its type. A `&Engineer` can't point into a `u32` local in safe
code. But proving that needs every field of the crate's own types, since a struct may hold a
reference to the local.

Rows withheld at such a stop say why, as `session inspect` does:

- `[not settable: a statement under way may hold a copy (line 23)]`;
- `[not settable: a statement under way read it (line 17)]`;
- ``[not settable: copied into `n` (line 35)]``;
- `[not settable: the program may hold a copy (line 34)]`, for your binding's copy;
- `[not settable: part-way through line 12]`, after a step back from a call;
- ``[not settable: line 20's inlined `clone` may have read it]``, at a breakpoint past an inlined
  call, with `(unsafe code)` where the crate has unsafe code;
- `[not settable: code inlined before the stop may have read it]`, after a macro's inlined code.

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

A call given `&mut x` after the copy, as `let y = x; keep(&mut x);`, may change `x`. Then `y` may
no longer hold a copy of its value, and the refusal says only that it may:
``the program's own `y` (at line 34) may hold a copy of `x` that it goes on with``.

A copy stored through a pointer counts too. After `let p = slot.as_mut_ptr();` and `*p = x;`,
`slot` may hold a copy of `x`, and a later `*p` reads it. Peeky doesn't follow which element `p`
points at, so the refusal says that `slot` may hold a copy. Where a later write through a pointer
may overwrite the copy, as `b[0] = 7;` does after `let mut b = Box::new([x, 1]);`, the refusal
says only that the program may hold one.

A call may store a copy too, through a `&mut` or raw pointer it is given, of another argument:

- `v.push(x)`: `v` may hold a copy of `x`;
- `ptr::write(p, x)` with `p = &raw mut slot`: `slot` may hold one;
- `mem::replace(&mut slot, x)`, and `copy_nonoverlapping`, in the same way;
- `v.extend_from_slice(&a)` after `let a = [x];`, or `v.extend(a.iter())`: what it is given
  points at a copy;
- `m.entry(k).or_insert(x)` and `v.iter_mut().for_each(|r| *r = x)`: an `Entry` or `IterMut`
  holds a `&mut` its type doesn't show. A view that only reads, as `a.chunks_exact(1)` or
  `m.iter()`, writes nothing, so it leaves `a` and `m` out;
- a struct holding `&mut v`, passed by value or as `&mut h`: `v` may hold a copy. Passed as `&h`,
  it writes nothing through that `&mut`, so it leaves `v` out. Through `&h`, a raw pointer or a
  `Cell` it holds may still be written. Only the standard library's cells, locks and atomics
  count, not a type of your own named `NotCell` or `Atomicity`;
- a value holding a cell, a lock or a channel, passed as `&c`
  ([Copies behind a cell or a channel](#copies-behind-a-cell-or-a-channel)).

A read through a pointer made from the binding is a use of it. So after
`let r = v.last_mut().unwrap();` and `*r = x;`, a later `*r` uses `v`'s copy.

A value a call returns, given a pointer into the local, may be a copy too. After
`let y = x.clone();`, `s.push_str(&x.to_string());` or `let y = unsafe { ptr::read(&x) };`, the
refusal says ``the program's own `y` (at line 11) may hold a copy of `x` that it goes on with``.

- Peeky can't see what a function of the crate's computes, so its result may be a copy. In
  `let mean = w.push(v);`, `mean` may hold a value computed from `w.sum`.
- So on the next line, `w.sum` is refused. On the loop's last pass, a change there is never seen,
  since the program prints the old `mean`.
- A result that only points, as a `&str`, an iterator or `Option<&u32>`, points into the local.
  It holds no copy, but a call given it may return one: `v.iter().sum()`.
- A result that holds a value beside a reference may hold a copy: `(u32, &'static ())`, or a
  struct with a `u32` and a `&u32` field.
- `Box::new`, `Box::pin`, `Rc::new` and `Arc::new` return only what they are given. So a Box of a
  closure that borrows `s` holds no copy of `s`.
- So do the standard library's iterator adapters. `let it = s.chars().rev();` only borrows `s`, so
  a change to `s` there is refused for the borrow. A `map` closure that holds a copy of `x` holds
  it; one that borrows `x` doesn't.
- Where Peeky can't tell whether a result holds a copy or only points into the local, the refusal
  says both: ``may hold a copy of `x` that it goes on with, unless `c` only points into it``.
- A crate module named `std`, `core` or `alloc` makes paths under it the crate's own, not the
  standard library's. It leaves the other roots the standard library's: beside a `mod alloc {}`,
  `a.len()` (`core::slice`) still measures.
- The standard library's measures and comparisons return none: `x.len()`, `x.eq(&5)`,
  `x.cmp(&5)`. That is so only where Peeky proves they are the standard library's own, and the
  local can't hold what they return.
- `let b = std::mem::take(&mut a);` leaves `a` its default value, so the refusal names `b`, not
  `a`.

Where no local of the function stands for what the pointer points at, the pointer itself does.
That is so for an argument `p`, or one `Box::into_raw` returned. After `*p = x;`, the refusal says
``the program's own `p` (at line 49) may point at a copy of `x` that it goes on with``.

To change the value, change it before the copy is made, or change the copy.

#### Copies behind a cell or a channel

A callee given only a `&` can still store a value in a cell, a lock, an atomic or a channel.
Peeky decides whether it can from everything the type holds, however deep:

- The standard library's types count by what each is known to hold:
  - `Cell`, `RefCell`, `UnsafeCell`, `OnceCell`, `OnceLock`, `Mutex`, `RwLock` and the atomics
    take a value;
  - a channel's `Sender` or `SyncSender` takes one, and its `Receiver` reads it;
  - `Rc`, `Arc`, `Weak`, `LocalKey`, `LazyCell`, `LazyLock`, `Option`, `Vec` and the other
    containers and views count by what they hold;
  - `Condvar` and `Barrier` change, but hold no value of yours;
  - `PhantomData<T>` holds nothing, whatever `T` is.
- A `Cell` or `UnsafeCell` of a value with only one state keeps nothing: `Cell<()>`,
  `Cell<PhantomData<u32>>`, a cell of a struct with no fields, or of an enum with one fieldless
  variant. Whatever a call stores there, the program sees the same value.
- Such a cell has only one state itself, so `Cell<Cell<()>>` keeps nothing either.
- A `RefCell`, a lock, a once-cell and a channel keep state of their own, even of `()`. A
  `RefCell<()>` keeps its borrow flag, which a leaked `RefMut` leaves set. A `Mutex<()>` may be
  poisoned, and a `Sender<()>` adds a message that `rx` counts.
- An array counts by its length: `[Cell<u32>; 0]` holds nothing. A slice, or an array whose
  length isn't a plain number, counts by its element.
- A function pointer or item holds nothing, whatever its signature names:
  `fn(&Cell<u32>) -> u32` stores no value.
- A standard type on none of these lists, a trait object, or a dependency's type may take a
  value. A refusal then says only that a binding may hold a copy.
- Your own structs, enums and unions count by their fields and variants, as the build's debug
  information shows them. A closure counts by what it captures.
- A generic type counts by each instance: `Gen<u32>` holds no cell, even where the program also
  has a `Gen<Cell<u32>>`. A type argument left at its declared default (`struct Gen<T = u32>`)
  doesn't show in the type's name, so Peeky reads the default from your source.
- A default may name an earlier parameter: `Pair<u32>` of `struct Pair<T, U = T>` is
  `Pair<u32, u32>`, and `Lst<u32>` of `struct Lst<T, U = Vec<T>>` is `Lst<u32, Vec<u32>>`.
- Where it can't find that default, it can't tell which instance the value is. The refusal says
  so: ``(… its type may hold: which instance of its type it is can't be told)``.
- What shares the value the call reaches counts too: `rx` beside `tx` of `mpsc::channel()`, or
  `b` of `let b = Rc::clone(&a);` for `put(&a, x)`. So does a struct or closure built of one,
  as `h` of `let h = Hold { c: a.clone() };`.
- What a cell may hold is compared with the value's whole type, its path and type arguments
  included. Only the very type is "a copy": a `Cell<Vec<u32>>` holds no copy of a
  `Vec<bool>`, nor a `Cell<a::Id>` of a `b::Id`.
- A cell may hold the very type inside what it holds by value: a `Cell<Vec<u32>>` may hold a
  copy of a `u32`.
- Otherwise the cell holds what was computed from the value, never a copy. That is so for a
  `Cell<bool>` given `x > 5`, and a `Cell<u32>` given `p.a` of `p: Pt { a: u32, b: bool }`.
- The refusal then says ``may hold state derived from `x` ``, and advises a change before the
  program uses it. It says so too where a type isn't known.

Such a copy sits in a standard type's fields, which Peeky doesn't change. So the refusal says it
may be out of Peeky's reach, and advises only a change before the call:

```
error [MUTATION_REJECTED]: `x` cannot be changed at src/main.rs:56, at any stop of this build: in hidden::sent, at every place line 56 can stop at, the program's own `rx` (at lines 54 and 55) may hold a copy of `x` that it goes on with, which may be out of Peeky's reach (behind a cell, a lock, a channel or a pointer its type holds), so a change there may never be seen: …
hint: Change it before the program copies it (a stop on an earlier line, or `--set-at` there).
```

A `--set x=100` at line 55, before `send(&tx, x)` runs, reaches the program: `rx.recv()` returns
100.

A call can also keep a copy in a `static` or a `thread_local!` that a later call reads. Peeky
doesn't follow that, so `x` stays `[settable]` there. Look for such state before you trust a
change.

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
  A field inside a standard-library type, such as a `Vec`'s `len`, never gets this mark. `set`
  refuses it for its type first.
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

- `--set-at <call line>@N` in a probe at any line of the file, such as
  `--set-at 30@3 w.start=10`;
- a probe `--at` the call's line, with `--hits N --set-hit N`;
- `session start --at` the call's line, with `--hit N`.

N counts the call line's own stops, not `allow`'s. They differ when `allow` is called from several
lines. To find N, run `peeky probe ... --at <call line> --capture now --hits all`.

These don't reach the stop you need:

- `session continue --to` the call's line reaches only the next call.
- `session step --out` returns part-way through the line, where changes are refused.

The refusal gives this call advice only where the build's MIR proves the reference is the
function's parameter at the stop, still holding what the call passed. Otherwise it names no call:

- A local binding was not passed into this function as a parameter: `let r = &mut x;`, or a
  `let p` that shadows a parameter `p`. If what it points at is also a root local in some
  frame, change it there, where its row is `[settable]`. Otherwise edit the source with
  `--old`/`--new`.
- A `for` loop's item over a slice or `Vec`, such as `e` in `for e in expenses`, points at an
  element. Peeky never changes a slice's or `Vec`'s elements, so the refusal says to edit the
  source. The crate must not declare its own `Iterator` trait, or its own `std`.
- Where the MIR doesn't prove either, the refusal says the reference isn't proven a parameter.
  Examples are a closure's capture, and a local that reborrows a parameter (`let q = &*p;`).
  So is a parameter that the function may point elsewhere: `p = &GLOBAL;`, `&mut p` taken
  anywhere in it, or any `asm!` in it, which may write its operands.

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
