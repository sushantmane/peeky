# Peeky for Rust

This page lists what is different for Rust targets, one rule per line. Each rule links to its
details, in the [Rust cookbook](rust-cookbook.md) or the [COOKBOOK](../COOKBOOK.md). Read
[AGENT_GUIDE.md](../AGENT_GUIDE.md) first: it has the steps and rules every language shares.

Peeky debugs Rust with lldb-dap, LLVM's debugger.

## Words these pages use

| Word | Meaning |
| --- | --- |
| MIR | The compiler's mid-level form of the code. Every move, drop and assignment is explicit in it. Peeky reads it to prove that a value is there, and that a change is safe. |
| debug information | What the build records about types, fields and offsets (DWARF). |
| innermost frame | The function the program is stopped in. Its callers are caller frames. |
| unoptimized build | Peeky's default build. Reading a `Vec`'s, `String`'s or reference's contents needs it, and so does every change. |
| raw | LLDB's own summary of a value: its type, address and fields, such as a `Vec`'s `len`. Not its contents. |
| `[memory]` | Peeky read the value from the program's memory itself, at offsets checked against the debug information. |
| `[stale]` | The leftover bytes of a moved or dropped local. Not its value. |
| `[unproven]` | Peeky shows only a pointer, because it can't prove that what it points at is still there. |
| `[settable]` | `--set` can change this value at this stop. |

## Choose the target

- The target is the crate directory with its `Cargo.lock`:
  `peeky probe limiter --at src/main.rs:10 --capture width`
  ([Rust 7.2](rust-cookbook.md#set-up-the-crate)).
- A plain `cargo new` binary crate works: edition 2021 or 2024, with no dependencies. It can't
  have `src/lib.rs`, `src/bin` or `build.rs`, and has at most one integration test
  ([Rust 7.2](rust-cookbook.md#set-up-the-crate)).
- If `Cargo.lock` is missing, run `cargo generate-lockfile` once
  ([Rust 7.2](rust-cookbook.md#set-up-the-crate)).
- Name the line with `--at src/main.rs:LINE`. Pass program arguments after `--`, or with repeated
  `--arg` ([2.5](../COOKBOOK.md#25-a-module-imported-by-a-script-with-arguments)).
- `verify` also runs the crate's integration test, if it has one
  ([6.5](../COOKBOOK.md#65-writing-a-checker)).
- If the build fails, the probe is refused before launch with `BUILD_FAILED`. It exits 2, with no
  table and no run saved. A candidate edit that doesn't compile counts too. In `verify`, it is a
  failed verify, which exits 1 ([ERRORS](../ERRORS.md)).

## Read values

- Locals show their Rust types, such as `Vec<u32>` and `&mut limiter::Window`
  ([Rust 1.1](rust-cookbook.md#read-types-as-the-program-spells-them)).
- These read from memory, marked `[memory]`: an owned `String`, a `Vec` of plain elements, and
  a reference parameter of them (`&[T]`, `&str`)
  ([Rust 7.2](rust-cookbook.md#what-peeky-reads)).
- Plain elements are integers, `bool`, floats and `char`, and tuples and crate structs of them
  ([Rust 7.2](rust-cookbook.md#tuples-and-structs)).
- A `Vec` or slice of tuples or crate structs reads element by element, if each has such a field
  or a `String`. `friends: &mut [Friend]` shows `[Friend { name: "Bo", paid: 0, owes: 0 }, …]`, and
  `friends[1].name` reads `"Chloé"` ([Rust 7.2](rust-cookbook.md#containers-of-structs)).
- An element's other fields, such as a raw pointer or a `Vec`, show `not read:` and why. The
  element leaves them out: `Expense { cents: 4500, .. }`
  ([Rust 7.2](rust-cookbook.md#containers-of-structs)).
- A container of enums, of `String`s or of standard-library structs stays raw, with why
  ([Rust 7.2](rust-cookbook.md#containers-of-structs)).
- A tuple or struct local whose fields are all integers, `bool`, floats or `char` reads from
  memory, marked `[memory]`. It shows as derived `Debug` prints it:
  `Request { page: 1, page_size: 3, total: 8 }`. Its fields stay selectable
  ([Rust 7.2](rust-cookbook.md#tuples-and-structs)).
- So does a by-value parameter of such a type, or a reference parameter to one
  ([Rust 7.2](rust-cookbook.md#tuples-and-structs)).
- An owned local or a by-value parameter shows this way only in the innermost frame, where the MIR
  proves it initialized at the stop line ([Rust 7.2](rust-cookbook.md#tuples-and-structs)).
- A reference parameter shows this way only once the build's IR proves it live. A `&mut` one shows
  it only in the innermost frame ([Rust 7.2](rust-cookbook.md#tuples-and-structs)).
- These keep LLDB's summary: a struct local with any other field, a nested struct, a
  `#[repr(packed)]` struct, a standard-library struct such as `Range<usize>`, and any preview in an
  optimized build or with a debugger without `readMemory`
  ([Rust 7.2](rust-cookbook.md#tuples-and-structs)).
- Contents read only in an unoptimized build, with lldb-dap 20 or later
  ([Rust 7.2](rust-cookbook.md#set-up-the-crate)).
- An owned `Vec` or `String` local reads only in the innermost frame, where the MIR proves it
  initialized ([Rust 7.2](rust-cookbook.md#owned-vec-and-string-locals)).
- A `&mut` reference parameter reads only in the innermost frame. A shared one reads in any frame
  ([Rust 7.2](rust-cookbook.md#reference-parameters)).
- In a caller frame, a `&mut` `Vec`, `String` or slice shows its fields `[stale]`: the call under
  way may be changing them ([Rust 7.2](rust-cookbook.md#reference-parameters)). So does an owned
  `Vec` or `String` ([Rust 7.2](rust-cookbook.md#moved-dropped-and-partly-moved-values)).
- A reference parameter that its function binds again shows only its pointer
  ([Rust 7.2](rust-cookbook.md#reference-parameters)).
- A `Vec<String>` stays raw ([Rust 7.2](rust-cookbook.md#what-peeky-reads)).
- A `HashMap` or `BTreeMap` shows LLDB's raw fields. Peeky loads none of LLDB's Rust formatters,
  so `m[1]` finds no element.
- A `&str` loop item reads as text where the code still uses it. In `for cell in cells` over a
  `&[&str]`, `cell` reads `"Renée"`
  ([Rust 7.2](rust-cookbook.md#str-loop-items-and-containers-of-str)).
- The `&[&str]` or `Vec<&str>` itself stays raw. Its note names its length selector:
  `cells.length`, `names.len` ([Rust 7.2](rust-cookbook.md#str-loop-items-and-containers-of-str)).
- A closure's captured `&str` keeps LLDB's summary, with a note. Read it in the function that makes
  the closure ([Rust 7.2](rust-cookbook.md#str-and-slice-locals)).
- `Vec` and `String` fields read too
  ([Rust 7.2](rust-cookbook.md#fields-holding-a-vec-string-str-or-boxstr)):
  - `self.values` behind `&mut self`, in the innermost frame only;
  - `self.values` behind `&self`, in any frame;
  - `w.values` in `main`, which owns `w`;
  - `entry.name` of a loop's `entry: &Entry`, where the code still uses it.
- A moved struct's fields stay raw, with a note
  ([Rust 7.2](rust-cookbook.md#fields-holding-a-vec-string-str-or-boxstr)).
- **The crate-wide `unsafe` gate:** a crate with any `unsafe` code or a nightly feature gets no
  contents read. No `Vec` or `String` contents, no reference parameter's contents, and no such
  field. All of these stay raw, with the reason. Scalars are still read
  ([Rust 7.2](rust-cookbook.md#crates-with-unsafe-code)).
- The gate's check is broad. `no_mangle` counts anywhere in the code, even as a variable's name.
  `Cargo.toml` is checked too, as plain text
  ([Rust 7.2](rust-cookbook.md#what-counts-as-unsafe-code)).
- Peeky never guesses. A value it can't prove keeps its raw fields, with a note: `[stale]`,
  `[unproven]` or `<unverified ...>`. That includes a moved or dropped value, one in a caller
  frame, and one in an optimized build
  ([Rust 7.2](rust-cookbook.md#values-peeky-withholds-or-leaves-raw)).
- A local whose slot may hold leftovers is withheld: the row says `no value yet:` and why. That is
  a local not assigned on some path (`let x;`), or not kept across an `.await`
  ([Rust 7.2](rust-cookbook.md#values-peeky-withholds-or-leaves-raw)).
- An enum that Peeky can't decode with certainty stays opaque
  ([Rust 7.2](rust-cookbook.md#values-peeky-withholds-or-leaves-raw)).
- A tuple's fields are `.__0`, `.__1` and so on: `runs[0].__0`
  ([Rust 7.2](rust-cookbook.md#tuples-and-structs)).
- `describe` normally lists a name bound more than once as `x @ main.rs:6`, one entry per
  binding. That selector reads the binding wherever it is in scope: `s @ main.rs:81.a`
  ([Rust 7.2](rust-cookbook.md#names-bound-more-than-once)).
- The exception is two bindings on the same file name and line. They share one entry, and no
  selector can name either: `x @ main.rs:3 (2 bindings on this line: not selectable by line)`
  ([Rust 7.2](rust-cookbook.md#two-bindings-on-one-line)).
- A one-line `for` loop stops at three locations. Over three passes, the stops are 1, 2, 3, 3, 3,
  so rows carry a `stop` number. For one row per pass, put `--at` on a plain statement line in the
  body ([2.2](../COOKBOOK.md#22-every-iteration-of-a-loop)).

## Change values

- `--set` changes an integer local of 16 to 64 bits that the program keeps in memory
  ([Rust 7.2](rust-cookbook.md#what---set-can-change)).
- It also changes such a field or element of a struct, tuple or array local that the frame owns:
  `--set w.start=10`. For these, the MIR must prove the local initialized
  ([Rust 7.2](rust-cookbook.md#what---set-can-change)).
- Every change needs an unoptimized build and the innermost frame. A caller's frame is never
  changed ([Rust 7.2](rust-cookbook.md#what---set-can-change)).
- A change is allowed only at a line breakpoint, or after a step that lands on a line's start.
  No statement under way may have read the value
  ([Rust 7.2](rust-cookbook.md#where-a-change-is-allowed)).
- `describe`'s `mutable:` line lists, per function, the fields and elements `set` may change:
  `board.entries`, `grid.cells[*].n` ([Rust 7.2](rust-cookbook.md#what---set-can-change)).
- The program's own copy can block a change. In `x = match x { n => … n }` or `let y = x;` …
  `x = y + 1;`, the program goes on with the copy, `n` or `y`
  ([Rust 7.2](rust-cookbook.md#when-the-program-has-its-own-copy)).
- Such a change is refused, unless the program reads the changed part itself again. A reference
  given to a call doesn't count, nor does `{:?}`. Instead, change the value before the copy is
  made, or change the copy ([Rust 7.2](rust-cookbook.md#what-counts-as-reading-it-again)).
- Write a non-ASCII name as rustc lists it, NFC-normalized: `größe.zähler`. Peeky proves where it
  is kept, in memory or a register, as it does for an ASCII name. So `--set ñ=6` and
  `--set größe.zähler=30` change a local and a field that the frame keeps in memory
  ([2.1](../COOKBOOK.md#21-fields-and-indexes)).
- A local kept in a register is `[not settable: register]`. `describe`'s `mutable:` line lists
  them ([Rust 6.6](rust-cookbook.md#change-a-local-kept-in-a-register)).
- To change one, add `std::hint::black_box(&mut x);` after its `let` with a candidate edit. That
  keeps it in memory, and the same probe can set it
  ([Rust 6.6](rust-cookbook.md#change-a-local-kept-in-a-register)).
- A `String` local takes a text in double quotes: `--set 'name="pear"'`. The text must fit its
  capacity, and the MIR must prove the `String` initialized
  ([Rust 7.2](rust-cookbook.md#set-a-string-to-a-text)).
- It takes no text while a borrow of it is used after the stop, such as `&s[..2]` or `s.chars()`
  ([Rust 7.2](rust-cookbook.md#borrows-that-block-a-text)).
- Setting a text needs lldb-dap 21, which macOS ships
  ([Rust 7.2](rust-cookbook.md#set-a-string-to-a-text)).
- A `&str` never takes a text. Nor does any `String` in a crate with `unsafe` code or a nightly
  feature ([Rust 7.2](rust-cookbook.md#set-a-string-to-a-text)).
- Peeky never changes a value through a reference or a `Box`. For `w.start` where
  `w: &mut Window`, set it in the caller that owns `w`, at the call's line
  ([Rust 7.2](rust-cookbook.md#change-a-value-behind-a-reference)).

## See what a function returned

- `--capture return` shows integers, `bool` and floats exactly
  ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
- It withholds an enum or `Option`, a struct, a string, and any return in an optimized build. The
  row says why ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
- A line can stop again in the same call before the call returns: on a loop's next pass, or at
  each of `format!`'s blocks. Each of those hits gets the call's return where the step out's watch
  proves the stop is in the same call. Otherwise the earlier hit's return is withheld
  ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
- `session step --out` shows what the function returned, with the stop: `(return): u32 = 42`. Or
  it shows a withheld row saying why, as `--capture return` does. That includes a call that a panic
  ended ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
- It shows nothing if another stop interrupted the step first, or if the program ended
  ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
- `session get return` reads the return again
  ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
