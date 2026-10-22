# How Peeky proves Rust values

This page is for maintainers. It explains how Peeky proves what it reads and changes in a Rust
program. The rules themselves, with examples, are in [rust.md](lang/rust.md) and the
[Rust cookbook](lang/rust-cookbook.md#72-rust-a-cargo-crate). The worker is in
[RUST_GATE.md](RUST_GATE.md). The investigation loop is at the [end](#the-investigation-loop).

## The MIR build

Unoptimized builds compile the binary crate through a small rustc wrapper, set as
`RUSTC_WORKSPACE_WRAPPER`. Peeky writes it into its own build directory, and rechecks its bytes
before every command. For that crate only, the wrapper adds:

- `-Zmir-opt-level=0`;
- a `PreCodegen` MIR dump into `target/peeky-mir`;
- legacy symbol mangling.

Around it:

- `RUSTC_BOOTSTRAP` is set to that crate's name only, so no other crate gets unstable features.
- The test target, and anything else Cargo compiles, gets no flags. Cargo reuses the one binary
  unit for both commands.
- The dumps are a hashed build artifact.
- Optimized builds use no wrapper, and read no owned locals.

## The owned-local proof

`rust_mir` parses the dumps, and runs a definite-initialization dataflow over each body.

- It maps the stop line to MIR blocks through the LLVM IR's debug locations. Those already
  attribute macro-expanded code to the line that invokes the macro.
- It reads an owned `Vec<T>` or `String` local only if the local is initialized everywhere code of
  that line can be. That means in every such block, and in any block codegen merged into it.
  `T` is an integer type, `bool`, `f32`, `f64` or `char`.
- "Everywhere" means before each statement on the line, and in code from another file, such as an
  external macro's. The dataflow follows moves, drops and assignments statement by statement.
- A block whose code on the line no statement explains must keep the local throughout.
- The stop must be in the function's own file.
- The MIR body must be identified positively. Exactly one body of that name has a span on the
  function's `fn` line, and it owns every line of its code that any body has a span on.

A second dataflow follows, for every local, only whether it was assigned since its storage began.

- A `StorageLive` ends that: a loop's next pass, or a poll resuming the body. A move or a drop
  doesn't.
- Across a suspension, only what the coroutine's state saves stays.
- A stop where some path leaves a local unassigned withholds it.

Anything Peeky doesn't understand proves nothing:

- an unparsed statement;
- a coroutine;
- a projected or doubly bound name;
- a body that is ambiguous for its IR function, macro-generated twins included;
- a block of unknown origin;
- a stop in another file, such as a macro defined there.

A crate using `#[collapse_debuginfo]` gets the whole-block rule only. Unsafe code that duplicates
ownership, such as `ptr::read`, is outside the proof.

## Reading values from memory

The rules are in the cookbook's
[What Peeky reads](lang/rust-cookbook.md#what-peeky-reads). How they are proven:

- Reference parameters are read at offsets checked against the build's debug information, when the
  debugger supports `readMemory`: lldb-dap from LLVM 20.
- A parameter named in Unicode reads as its ASCII twin does. The LLVM IR quotes its name in hex,
  so its proofs match the hex spelling.
- A `let w: &Vec<i64>;` that the function never assigns is named in the IR, but never stored. So
  it counts as binding the name again.
- A reference parameter takes its type from the build's IR: `&mut limiter::Window`, not LLDB's
  `limiter::Window *`. The IR declares each instantiation's own types, never a type parameter.
- Locals and parameters take their types from the build's MIR.
- A tuple or struct preview reads at the offsets the build's debug information gives.
- On stable Rust, a crate can't declare an identifier `__0` other than by a nightly feature.
  `macro_rules!` can't join identifiers. A procedural macro needs a crate that Peeky's builds
  refuse: a dependency, a library target or a build script. Peeky's builds allow nightly features,
  and `${concat(__, "0")}` declares `__0` with no such token. Every file the build reads is a
  declared input, and all of them are scanned. That includes `Cargo.toml` and `Cargo.lock`, read
  as plain text, which `include!` can read as Rust.
- Of a name bound more than once, only the bindings in scope at the stop count. The MIR's scopes
  pick the binding, and so the type that a field read uses.

### Fields holding a `Vec`, `String`, `&str` or `Box<str>`

- Such a field reads under the proof of the value that holds it. The build's debug information
  decides the path, never LLDB's names.
- A shared reference parameter's field reads in every frame, because nothing can change its target
  during the call.
- The field's DWARF type picks the layout: `alloc::vec::Vec<i64, alloc::alloc::Global>`, which LLDB
  names `Vec<long>`.
- A `RefCell`'s or `Mutex`'s contents change behind a shared reference, so
  `self.cell.value.value` stays raw.
- An owned `Vec` or `String` local's note gives its own reason, rather than always "may be moved,
  dropped or not yet assigned".
- `session get self` lists the fields that stay raw, and why. An index into one is refused with
  that reason, and the length selector to capture: `self.values.len`, `self.name.vec.len`.

## When a reference reads

The rules are in the cookbook's
[When a reference reads](lang/rust-cookbook.md#when-a-reference-reads). How they are proven:

- While the code still uses a reference, the borrow checker keeps its target.
- Peeky tells which lexical block a line breakpoint stopped in from the stop's column and the
  build's LLVM IR.
- For a macro's stops, it tells the blocks apart by the lexical block that the stop's address is
  in. That is the build's DWARF, matched to the MIR's scopes and the LLVM IR's blocks by the names
  each declares.
- An `#[inline(always)]` function is inlined by LLVM even at opt-level 0, so no IR of its own
  places the stop. Its frame is judged at every statement of the stop line, and after each of its
  calls. A frame whose code can't be told apart from another's, such as methods a macro generates,
  shows every local that borrows as only its pointer, except a reference parameter as below.
- A function is too big or unusual to analyze when its dataflow runs past its budget.
- A reference parameter that is never reassigned stops reading when the function drops what it
  points at: `*h = new`, or `*c.borrow_mut() = new` through a shared `&RefCell`.

### The Drop window

Between the drop and the store of `*v = new`, `v` shows only its address. That holds also when the
store goes through:

- a reborrow;
- a struct, a `Cell` or a closure that holds it: `Ctx { v: &mut *v }`;
- a call that returns it: `id_mut(v)`;
- a closure's capture of it.

### Frozen owners

In a crate with no unsafe code, a shared reference made only from shared borrows of variables that
nothing changes always reads. An example is `v` of `for v in args[2].split(',')`, past its last
use. The borrows may pass through:

- reborrows and copies;
- calls whose borrowing operands are such borrows: `index`, `deref`, `split`, `into_iter`;
- `Iterator::next`, on an iterator that only `next` borrows `&mut`. Its items never borrow the
  iterator.

Limits on that:

- A crate that names anything else `Iterator` gets none of this.
- A type that a function's header names short, such as `&Vec<String>`, must be the standard
  library's. It must not be one the crate declares, imports under that name, or names in a
  `macro_rules!` body.

Each such variable:

- has a scope that encloses the reference's;
- is assigned once, and never moved, partly assigned, or borrowed `&mut` or raw;
- is initialized at the stop;
- has a type with no interior mutability and no references of its own (`Vec<String>`, `String`,
  integers, and arrays and tuples of them), or is a shared reference argument to such a type.

These read too, unless written through a `&mut` to them:

- a reference into what a shared reference parameter points at, `&items[0]`, because the caller's
  borrow freezes it;
- a reference to a variable of an enclosing scope that is initialized at the stop: `&p`;
- a parameter, never reassigned, that holds references: `&mut self`, `p: &mut Pt`, `v: View<'_>`,
  `(&Pt, i64)`. The caller lent them for the call;
- a literal, `let s = "text"`, and a fn pointer.

### Async bodies

- The `.await`'s column is also where the awaited future is dropped if a panic unwinds. A reference
  handed to the awaited future is past its last use while it is polled. So lines with an `.await`
  hide more than the same code without `async`.
- The 140-line async program showed up to four stops on every line.
- lldb-dap lists the function's own variables, such as a parameter's future field, before a
  lexical block's.
- lldb-dap lists a row only where the stop is inside its binding's lexical block. So places of the
  line outside the blocks of the frame's rows don't count. Where the stop's address tells its
  lexical block apart, places outside that block don't count either.
- After a call or a drop on the line, only the edge it returns on counts.

## Withheld and stale values

The rules are in the cookbook's
[Values Peeky withholds or leaves raw](lang/rust-cookbook.md#values-peeky-withholds-or-leaves-raw).
How they are proven:

- A local that may hold leftovers is found by the second dataflow above, as rustc's own
  initialization check would find it.
- Any struct counts as possibly owning memory through a pointer, because the MIR names no fields.
- A tuple or array of only primitives and references doesn't count, unless a generic function's
  type parameter has a primitive's name.

## Crates with unsafe code

The rules are in the cookbook's
[Crates with unsafe code](lang/rust-cookbook.md#crates-with-unsafe-code). Why the rule is the
whole crate's:

Unsafe code can free what a value owns where the MIR still shows the value initialized. That is
sound as long as the value isn't used while it dangles. Examples:

- `ptr::drop_in_place(&mut self.values)` before a `ptr::write`;
- `ptr::read(&w.values)`, with the copy dropped before `mem::forget(w)`;
- the same in a closure, or in an `unsafe fn` that the function calls;
- through a pointer stored for later: an out-parameter, `slot.replace(&mut w.values)`;
- after the value is moved or rebound;
- in a caller: `look(w: &W)` after its caller freed `w.values`.

No analysis of the stopped function sees all of these. An owned `Vec` or `String` local's read
would read freed memory in these cases too. So where the crate's own sources have unsafe code
anywhere, Peeky reads nothing from memory itself. References are still judged by when they read,
as above.

- Before the 2024 edition, `#[no_mangle]`, `#[export_name]` and `#[link_section]` are unsafe without
  saying so. `global_asm!` needs no `unsafe`, but can define `free`.
- In a crate with no unsafe code, safe Rust guarantees that a reachable `Vec` or `String` owns live
  memory. A raw pointer, such as `v.as_ptr()`, can free nothing without unsafe code.
- The standard library is taken as sound. Peeky's builds have no other dependencies.
- A reference parameter's contents stay raw, because a caller may have freed the buffer of what it
  lends: `look(v: &Vec<i64>)` reading only `v.capacity()`.
- A Box's contents are never read. A Box can be freed through a second Box made by
  `Box::from_raw`, while the first is later forgotten.
- What LLDB reads itself stays. A reference parameter's target is valid for the whole call, as Rust
  requires of a reference argument, so its scalar fields read. A by-value struct's preview is its
  own storage, in the frame.

How the check reads the sources:

- The sources include an integration test's in `tests/`. A test runs in its own binary, but what
  Peeky read from rustc's dependency file to leave such files out wasn't exact for every file name,
  such as `a$$b.rs` or one with a U+2028.
- A shebang line, such as `#!/usr/bin/env -S echo "`, is skipped. Read as code, its `"` would open
  a string that hides the code below it.
- Brackets that don't pair up, once comments and literals are blanked, mean a literal was misread.
  Every source rustc accepts pairs them.
- A nightly feature counts because Peeky's MIR builds set `RUSTC_BOOTSTRAP`. Then
  `#![feature(custom_mir)]` frees what a `&Vec` points at with `Drop(*v, ...)`, and no `unsafe`
  token.

## Changing a value

The rules are in the cookbook's [What `--set` can change](lang/rust-cookbook.md#what---set-can-change)
and the sections after it. How they are proven:

### No statement under way has read it

A change is allowed at a line breakpoint, or after a step that ends at its line's lowest address in
the function, where a breakpoint on it stops. From the line's first code in the stop's lexical
block, the build's MIR must show no temporary that may hold a value read from the local, and is
used again. Such a temporary is:

- a copy of the local, or of a field;
- a read through a pointer that may point into it;
- what a call given such a pointer returns. `format_args!`'s calls don't count, because they keep
  the pointer, unless the crate itself names something `core` or `std` that could pass for them.

- The line's first MIR place may emit no code, as `&k` in `let t = (&k,` / `s.a,` / `s.b);`. So the
  stop's column places a breakpoint on its line.
- `let arr = [s.a,` / `k,` / `s.b];` builds its array on line 1, after reading `s.a`.
- `OwnedLocals.copied_everywhere` finds lines where every place a line can stop at is inside such a
  statement. Places that emit no code, such as `StorageDead`, are left aside.

### The order the code runs

`Body.copies_at` reads the MIR in the order the code runs.

- An assignment to the local, or the start or end of its storage, leaves no temporary holding its
  new value. So a temporary made before doesn't count.
- A call's result counts only on its return edge.
- `Body.direct_copies_at` gives the direct copies, from the blocks reachable from `bb0` or a
  coroutine's resumption. That is a copy or move of the local or a part, what is computed from one,
  a closure or aggregate holding one, and a read through a pointer to it. It isn't what a call
  given a pointer returns, such as `x.len()`.
- A temporary that read the local blocks the change wherever it is used again. One taken from a
  binding's copy is judged as that binding is: `a + x` still reads `x`.
- Copies are judged by the part they may have taken: `direct_copies_at(…, projection)`. An
  aggregate's `[settable]` gate leaves them out (`mid_statement(members=True)`), and judges each
  member alone.

### The program's own bindings

A binding of the program's own blocks the change where, on some path, the program uses it and then
assigns or ends the local without reading the changed part (`Body.copy_undoes`). Only direct copies
count for bindings.

- Only a read that surely covers part of that part counts: `_root_access`, with the selector's MIR
  projection. `fields.member_indexes` maps fields, because rustc's DWARF lists them in declaration
  order, as the MIR numbers them.
- A read is a `copy` or `move` of a place that overlaps the part, or the standard library's
  printing of a borrowed primitive.
- A print counts at the call that formats it (`Body._format_reads`): a call to `std::io::_print`,
  `std::io::_eprint` or `alloc::fmt::format`, by its full MIR path. `format` alone counts only where
  no function of the crate's own is so named, because the MIR then prints the standard library's.
- The call's span must be exactly in this toolchain's standard-library or `alloc` `macros.rs`
  (`rust.std_macro_files`). That is the sysroot's `lib/rustlib/src/rust/library/…`, or
  `/rustc/<commit-hash>/library/…` with the hash from the build's own `rustc -vV`. It is never a
  file under the crate or its build, as a crate's own `src/library/std/src/macros.rs` is.
- The print is traced back to the borrow through `Arguments::new`, the `Argument` array and
  `Argument::new_display::<i64>`, through locals assigned once. It never counts for a type parameter
  named like a primitive, nor where the body's type parameters aren't known.
- `write!` isn't a read, because a `write_fmt` of the crate's own may format nothing. A panic
  message isn't one either, because a hook may never format it.
- A borrow given to anything else is unknown, and the path goes on. A write to another part reads
  nothing, and one that covers the part assigns it.

### How the change is written

- The change is the debugger's `setVariable` on the field. Then the frame's scopes are listed
  again, and the field is read back.
- `describe`'s `mutable:` line comes from `fields.settable_parts`: a walk through structs and
  fixed-size arrays, at any depth, whose every leaf `settable_member` judges. `&str` is a record
  named `&str` in the DWARF, which the walk never enters.
- Types are shown as the program spells them, by `rust.rust_spelling`: `Vec<u32>`, `String`,
  `Option<i32>`, without `alloc::alloc::Global`. The spelling comes from the DWARF name of the type
  at the row's path, where LLDB's spelling matches it. LLDB's stays in `debugger_type`.

## Changing a String's text

The rules are in the cookbook's
[Set a String to a text](lang/rust-cookbook.md#set-a-string-to-a-text). How they are proven:

- Peeky writes the bytes with the debugger's `writeMemory`, and `len` with `setVariable` on
  `vec.len`.
- `OwnedLocals.borrow_live` decides whether a local that may hold a pointer into the `String` is
  used after the stop.
- `Body.lent_from` takes every borrow of the `String` (`&s`, `&mut s`, `&raw const s`), and every
  value built from one: what `deref`, `index`, `as_str` and `chars` return, a copy, a struct, a
  closure.
- It also takes what a call given a borrow may store it into: what another `&mut` or raw pointer it
  is given points at, or a shared one to a type that may hide a `Cell`.
- It keeps every local whose type doesn't provably hold no pointer (`_pointer_free`, from
  `rust.free_types`). A type is free unless it reaches a reference, a raw pointer, a trait object
  or an unknown type. So a type that holds itself through a `Box` is judged by the rest.
- A trait object never counts as free: the MIR doesn't print its lifetime.
- A pointer copied out of a local, such as `vec![…]` filling its `Box`, points into what that local
  owns.
- `Body.live_ahead` says whether one is used from the stop's places. A drop counts as a use where
  the local may still hold a value there (`Body.maybe_initialized`), and its drop may run code that
  reads the borrow (`_quiet_drop`: not a standard container or view of references).
- A crate with unsafe code takes no text: `RustSession.unsafe_code`, from `rust.unsafe_source`. An
  address kept as an integer (`s.as_ptr() as usize`, `addr()`, a `transmute`) holds a pointer that
  no type shows, and only `unsafe` code can read through it. The crate has no dependencies, so its
  sources are all the code but the standard library's.

## Limits and qualification

- dwarfdump's octal escapes and the LLVM IR's hex escapes read back. rustc's MIR and Cargo's
  messages are split at `\n` alone. So a source's name may hold any character.
- Edition 2024 was qualified by running the whole Rust suite, 56 tests with every owned-local proof,
  with its example crate switched to 2024.
- Test processes share OS permissions with the checker, and are trusted code.
- No live-model Rust evaluation has been recorded.

## The investigation loop

The Rust controller uses the same observation, intervention and candidate-verification flow as Java, C and C++. A fixed Rust worker builds and debugs one dependency-free Cargo binary through LLDB-DAP (see [RUST_GATE.md](RUST_GATE.md)). `RustVerification` rebuilds each candidate in a fresh workspace. Only declared application `.rs` sources are editable; `Cargo.toml`, `Cargo.lock` and the integration-test file are frozen inputs included in every identity check.

### Two independent gates

A Rust candidate passes only when both of these pass on the same fresh build:

1. **Compiled Cargo test.** Preparation runs `cargo build --offline --locked --bin <name>`, then `cargo test --no-run --offline --locked --test <name>` with the same controlled environment. The test executable is selected from the hashed `target/debug/deps` directory: exactly one, matching the declared test name, with no unexpected artifacts. Verification **executes that hashed binary directly** with `--test-threads=1`. Cargo is never invoked at verification time, so nothing can be rebuilt or substituted. Cargo's uplifted `target/debug/<bin>`, which the test runs through `CARGO_BIN_EXE_*`, is a hashed artifact and must be byte-identical to the debugged executable before and after the run. The test runs with a 16,000-byte output limit, so any output beyond the retained capture is an explicit failure rather than silent truncation. The final non-empty line must be the harness's single `test result: ok.` summary, with at least one passing test and zero failed, ignored, measured or filtered tests; the exit code must be zero and the run must not time out. Anything else is a test failure, and an earlier forged summary cannot satisfy this check.
2. **Independent checker.** The unchanged supplied `examples/rust/verify_pagination.py` runs against the prepared descriptor (12 page cases and 10 invalid-argument cases) and must complete through the trusted wrapper. A passing Cargo test can never replace it.

Baseline test failure is recorded and does not stop the investigation; the checker must fail on the original. A failed test assertion or checker case is a verification failure that returns to the decision loop. Candidate compile errors are retryable `build` results only when Cargo's JSON messages (`--message-format=json-diagnostic-short`) show every rustc `error` with a primary span in a declared application source. Span-less errors such as linker or driver failures, errors elsewhere, and malformed or possibly truncated messages are fatal. A test-target compile failure, toolchain/probe failure, unexpected artifact, executable drift or source/test/checker change is fatal.

### Required deterministic proof

```sh
PEEKY_RUST_CARGO="$(rustup which --toolchain 1.96.0 cargo)" \
PEEKY_RUST_RUSTC="$(rustup which --toolchain 1.96.0 rustc)" \
PEEKY_RUST_DAP="$(xcrun --find lldb-dap)" \
PEEKY_RUST_DWARFDUMP=/usr/bin/dwarfdump \
PEEKY_RUST_LINKER=/usr/bin/clang \
PEEKY_RUST_DSYMUTIL=/usr/bin/dsymutil \
pytest tests/rust
```

On Linux, use the LLVM 18 paths from the Rust CI lane and omit `PEEKY_RUST_DSYMUTIL`. The main regression invokes the real `peeky-agent --rust-config` entry point with a deterministic decision provider and a real HTTP worker. It spawns no model. The observed sequence is:

1. Pause at the observation line: `start = 3`, `some = Some(3)`, `none = None`.
2. Change the live `i32` to `0`; continuation prints `PAGE:[10, 20, 30]` without a rebuild.
3. Propose invalid Rust syntax; the compiler error returns as a retryable `build` result.
4. Propose `start = 0`. The **Cargo test passes** (it only checks page 1), but the independent checker rejects later pages, so the candidate fails.
5. Propose `(request.page - 1) * request.page_size`. The compiled test and all checker cases pass on a fresh build.

The report keeps the original source, test, manifest, lockfile and checker unchanged. It records the candidate hash, build manifest, test executable hash, libtest summary and checker completion. Temporary workspaces are removed (`artifacts_retained: false`). A second regression changes the frozen test file mid-investigation, and verification fails fatally without accepting a fix.

Local macOS run (Apple LLVM 21, Rust 1.96.0): the full investigation took 5.05 s, and the frozen-test drift regression took 2.34 s. Linux timings are recorded in TESTING.md from the hosted lane.
