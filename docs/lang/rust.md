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
| `[memory]` | Peeky read the value from the program's memory itself, at offsets checked against the debug information. Where the program keeps the local in registers, that is the debugger's copy, and the row also says `[not settable: register]`. |
| `[stale]` | Bytes that aren't a trustworthy value. Either a moved, dropped, partly moved, unassigned or unproven local's leftovers, or a caller's container that may be mid-change. The note says which. |
| `[unproven]` | Peeky shows only a pointer, because it can't prove that what it points at is still there. |
| `[settable]` | `--set` can change this value at this stop. |

## Choose the target

- The target is the crate directory with its `Cargo.lock`:
  `peeky probe limiter --at src/main.rs:10 --capture width`
  ([Rust 7.2](rust-cookbook.md#set-up-the-crate)).
- A plain `cargo new` binary crate works: edition 2021 or 2024, with no dependencies. It can't
  have `src/lib.rs`, `src/bin` or `build.rs`, and has at most one integration test
  ([Rust 7.2](rust-cookbook.md#set-up-the-crate)).
- Anything else is a workspace: a crate with dependencies, a library, `build.rs`, several
  members. Vendor its dependencies, name what Peeky analyses and what runs in a JSON file, then
  run `peeky build . --config rust.json` ([Rust 7.2](rust-cookbook.md#a-rust-workspace)).
- Or give the test's `cargo test` or `cargo run` command after `--test --`, run from the
  workspace's directory: `peeky probe . --at crates/x/src/lib.rs:LINE --capture a --test -- cargo
  test -p x --test it name -- --exact`. Peeky makes the config from it. It analyses the `--at`
  file's package. It runs the test target the command names, or the one that is the `--at` file,
  with its `--features`, in the package Cargo would pick without `-p` (the root's, or
  `default-members`). It builds when no build of it is current; a build refused for its
  provenance stays refused, with its "Build again". The test's filter and the words after
  cargo's `--` go to the test. `--release`, `--doc`, `--workspace`, `--example`,
  `--no-default-features` and a toolchain (`+nightly`) are refused, saying why, and so are a
  runner and the variables that would change Cargo's build or run (`RUSTFLAGS` but lints,
  `CARGO_PROFILE_*`, `RUSTUP_TOOLCHAIN`, …). So is a cargo or rustc other than Peeky's pinned
  toolchain, by its path, `rust-toolchain.toml` or `rustup override`. The test runs in Peeky's
  environment, not yours, and the result says so
  ([ADR 0009](../adr/0009-first-question.md#3-rust-the-config-from-cargo-tests-words)).
- A workspace's analysed packages may be edition 2018 too
  ([ADR 0008](../adr/0008-native-project-reach.md#verified-for-phase-2)).
- A probe of a workspace never builds it. Without a current build it is refused with
  `BUILD_REQUIRED`, which names the exact `peeky build` command. `--build` builds first
  ([Rust 7.2](rust-cookbook.md#build-it-then-probe-it)).
- In a workspace, Peeky stops only in the analysed packages' files. A dependency's frame, or
  another member's, reads as a library's
  ([Rust 7.2](rust-cookbook.md#what-peeky-reads-in-a-workspace)).
- A call into a dependency is code Peeky reads none of. A change the dependency may keep a copy
  of is refused, naming the call, its line and its crate
  ([Rust 7.2](rust-cookbook.md#calls-into-dependencies)).
- If `Cargo.lock` is missing, run `cargo generate-lockfile` once
  ([Rust 7.2](rust-cookbook.md#set-up-the-crate)).
- Name the line with `--at src/main.rs:LINE`. Pass program arguments after `--`, or with repeated
  `--arg` ([2.5](../COOKBOOK.md#25-a-module-imported-by-a-script-with-arguments)).
- A line with no code of its own, such as `continue;` or a lone `}`, may have no breakpoint. Stop
  on the line before it, then `session step` ([Rust 1.1](rust-cookbook.md#lines-with-no-breakpoint)).
- A step can stop inside a macro's definition, at a call the macro makes that the build inlined.
  Peeky reads no value of that frame there, and says why. `session step` goes on. A breakpoint on
  such a line is refused ([Rust 1.1](rust-cookbook.md#stops-inside-a-macros-definition)).
- `describe`, probes and `session inspect` leave out the bindings the compiler makes: a `for`
  loop's `iter`, and a `?`'s `residual` and `val`
  ([Rust 1.1](rust-cookbook.md#see-where-a-local-is-visible)).
- `verify` also runs the crate's integration test, if it has one
  ([6.5](../COOKBOOK.md#65-writing-a-checker)).
- To debug a crate on another host, run a worker there with `--rust-config`. Then add
  `--worker URL` ([REMOTE](../REMOTE.md#c-c-and-rust-on-the-worker-host)). A workspace's worker
  builds it with `peeky build . --worker URL`, or `--build`
  ([REMOTE](../REMOTE.md#a-rust-workspace)).
- Through a worker, `Cargo.toml`, `Cargo.lock` and every file in the config's `sources` and `tests`
  are compared with yours before the build. One that differs is refused by name
  ([REMOTE](../REMOTE.md#source-identity)).
- Peeky keeps up to 400,000 debug-information entries of the crate's own code, in at most 300 MiB
  of memory. A crate using BTreeMap has about 95,000, in about 100 MB: the std code it uses
  counts. Over that, the probe is refused with
  `BUDGET_EXHAUSTED` ([NATIVE_INTERNALS](../NATIVE_INTERNALS.md#reading-a-builds-symbols)).
- If the build fails, the probe is refused before launch with `BUILD_FAILED`. It exits 2, with no
  table and no run saved. A candidate edit that doesn't compile counts too. In `verify`, it is a
  failed verify, which exits 1 ([ERRORS](../ERRORS.md)).

## Read values

- Locals show their Rust types, such as `Vec<u32>`, `&mut limiter::Window` and
  `*const kitty::Friend`, where LLDB writes `limiter::Window *`
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
- A probe reads a caller's local as `1:pass`, by the caller-frame rules above
  ([2.10](../COOKBOOK.md#210-values-in-a-callers-frame)).
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
- A reference reads while the code still uses it, and after its last use until a call or drop
  runs. A call of `std::ptr::eq` or `std::ptr::addr_eq` doesn't count. Past that, it shows only its
  pointer, and its note names the call or drop and its line
  ([Rust 7.2](rust-cookbook.md#when-a-reference-reads)).
- A reference to a crate's struct reads whole only as a reference parameter whose fields are all
  plain scalars. Elsewhere its row shows the address, with a note. Read its fields by name, as
  `entry.name`.
- A raw pointer shows only its address, `[unproven]`. To see which element it points at in
  another run, join the runs on `address(value)` minus a base each run has
  ([Rust 4.8](rust-cookbook.md#48-compare-addresses-across-runs)).
- To see what `ptr::eq(e, l)` compares, capture `e` and `l` at its line. Each shows its address, so
  compare their `address(value)` at each hit in SQL ([4.8](../COOKBOOK.md#48-compare-addresses-across-runs)).
- In `queries diff`, a value whose addresses differ says `addresses differ`: what it points at
  may differ too ([3.1](../COOKBOOK.md#31-two-inputs-side-by-side)).
- A local whose slot may hold leftovers is withheld: the row says `no value yet:` and why. That is
  a local not assigned on some path (`let x;`), or not kept across an `.await`
  ([Rust 7.2](rust-cookbook.md#values-peeky-withholds-or-leaves-raw)).
- An `Option` local of a tuple or crate struct reads from memory, marked `[active-variant]`:
  `Some(Engineer { name: "Björn", shifts: 1, .. })`. `last.__0` is what `Some` holds, and
  `last.__0.name` its field ([Rust 7.2](rust-cookbook.md#options-of-tuples-and-structs)).
- An enum that Peeky can't decode with certainty stays opaque. Its note says why, gives its
  address and says what to read instead
  ([Rust 7.2](rust-cookbook.md#options-of-tuples-and-structs)).
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
- It lists each function's root locals of a type `set` changes too: `movavg::main: k, sum`.
  A stop can still refuse one, as its row says.
- The program's own copy can block a change. In `x = match x { n => … n }`, `let y = x;` …
  `x = y + 1;`, or `u.push(x);` … `x = t;` before `u` is read, the program goes on with the copy,
  `n`, `y` or `u` ([Rust 7.2](rust-cookbook.md#when-the-program-has-its-own-copy)).
- So can a copy a call keeps in a `static` or a `thread_local!` that a later call reads, as
  `local_set(&SLOT, x)` does ([Rust 7.2](rust-cookbook.md#copies-in-a-static-or-a-thread-local)).
- Such a change is refused, unless the program reads the changed part itself again. A reference
  given to a call doesn't count, nor does `{:?}`. Instead, change the value before the copy is
  made, or change the copy ([Rust 7.2](rust-cookbook.md#what-counts-as-reading-it-again)).
- A change no path reads before the value is overwritten or goes out of scope is refused too, as
  on `total = k + n;` itself ([Rust 7.2](rust-cookbook.md#when-the-program-never-reads-it-again)).
- Write a non-ASCII name as rustc lists it, NFC-normalized: `größe.zähler`. Peeky proves where it
  is kept, in memory or a register, as it does for an ASCII name. So `--set ñ=6` and
  `--set größe.zähler=30` change a local and a field that the frame keeps in memory
  ([2.1](../COOKBOOK.md#21-fields-and-indexes)).
- A local kept in a register is `[not settable: register]`. `describe`'s `mutable:` line lists
  them ([Rust 6.6](rust-cookbook.md#change-a-local-kept-in-a-register)). Where the IR doesn't
  show where it is, such as a closure's capture, rows say `[not settable: not proven in memory]`.
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
