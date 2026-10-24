# Rust internals: how Peeky builds, reads and proves Rust values

This page is for maintainers. It explains how Peeky builds a Rust target, reads its values, and
proves what it reads and changes. The rules themselves, with examples, are in
[rust.md](lang/rust.md) and the [Rust cookbook](lang/rust-cookbook.md#72-rust-a-cargo-crate). The
investigation loop is in [AGENT_LOOP.md](AGENT_LOOP.md#rusts-two-gates). The first engineering
gate's record is in the [archive](archive/RUST_GATE.md).

## The Cargo build

`peeky.rust.RustTarget`, the worker's `--rust-config`, prepares one declared Cargo binary.

**The crate's shape.** `rust_manifest` checks it before any tool runs:

- one binary target: a `[[bin]]` table, or Cargo's default `src/main.rs`;
- at most one integration test, explicit or discovered in `tests/*.rs`, which can't be edited;
- no build script, library or other binaries. A discovery switch (`build`, `autolib`,
  `autobins`, `autotests`) may be left out only where Cargo would find nothing more;
- no dependencies, workspaces or profiles;
- a matching version 4 lockfile.

A separate check refuses implicit Cargo configuration, and parent manifests above the crate. That
check alone isn't isolation: the build below adds the rest.

**The tools.** The config names real `cargo` and `rustc` from the toolchain's own directory.
rustup's proxies, and tools from mixed directories, are refused. The adapter, dwarfdump and the
linker are absolute paths. The pinned rustc and cargo versions are read from the tools' own output
before any Cargo command runs.

**The build.**

- It runs in a fresh workspace, under a parent directory Peeky owns. That parent's ancestry is
  checked for Cargo configuration.
- `CARGO_HOME` and `CARGO_TARGET_DIR` are relative, inside the workspace. `RUSTC` is explicit.
- The rustflags ask for DWARF 4. Incremental builds are off, and the opt-level is 0 unless the
  config asks for more ([Optimized builds](#optimized-builds)).
- Symbols are packed on macOS and unsplit on Linux, and Linux gets an explicit build ID.
- The command is fixed: `cargo build --offline --locked --bin <name>`.
- Builds also emit LLVM IR (`--emit=link,llvm-ir`). A change is allowed only if every declaration
  of that local in the stopped function is a real memory slot, not a `.dbg.spill` copy. The
  function is the one whose symbol hash ends LLDB's frame name.

**Before each toolchain probe and each Cargo command:**

- the staged manifest and lockfile are checked again against the first check's result;
- the workspace's ancestry is checked again;
- every tool probed so far, and the original and staged sources, are hashed again;
- on macOS, `PATH` must still find the `dsymutil` it found at the start. The declared tool must be
  named `dsymutil`, and its directory comes first on `PATH`.

An edit to a test, the manifest or the lockfile is refused before any tool runs.

**What was built.**

- `target/debug/deps` is a directory artifact: every file in it is hashed, and checked again.
- It may hold exactly one hashed application executable, its `.d` file, and on macOS its dSYM.
- Every file in rustc's dependency files, for the application and test crates, must be a declared
  source or test. So a `#[path]` module, `include!` or `include_str!` can't bring in bytes that no
  check covers.
- Module identity uses the native worker's UUID and build-ID check.

**Compiler errors.** Only a complete Cargo JSON failure is feedback a candidate can retry:

- Cargo's ordinary stderr footer;
- exactly one final `build-finished` failure;
- only well-formed `compiler-message` errors: an object message, a list of span objects, not an
  internal compiler error, with every primary span in a declared source.

Every message is read before deciding. These are fatal instead:

- a Cargo infrastructure failure, a timeout, or output over its bound;
- a test target that doesn't compile;
- a change in any tool probed so far.

**Time and cleanup.**

- Each launch has one 40 s deadline, below the client's 45 s HTTP timeout. Cleanup after it has
  its own bounded grace.
- Preparation, for Rust, C, C++ and Java, gets only the time that remains. So does every DAP launch
  step: initialize, the launch write, the `initialized` event, breakpoints, `configurationDone`
  and the launch response.
- A compile failure, and `close`, remove the workspace and its parent. A removal that fails is
  raised, or attached to the original error, never ignored.

## The MIR build

Every build compiles the binary crate through a small rustc wrapper, set as
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
- Optimized builds run the wrapper too, and their MIR is parsed: their bindings' types decide
  what a `Box` is. But no owned-local proof shows a container's contents there
  ([Optimized builds](#optimized-builds)).

## The owned-local proof

`rust_mir` parses the dumps, and runs a definite-initialization dataflow over each body.

- It maps the stop line to MIR blocks through the LLVM IR's debug locations. Those already
  attribute macro-expanded code to the line that invokes the macro.
- It reads an owned `Vec<T>` or `String` local only if the local is initialized everywhere code of
  that line can be. That means in every such block, and in any block codegen merged into it.
  `T` is an integer type, `bool`, `f32`, `f64` or `char`.
- "Everywhere" means before each statement on the line, and in code from another file, such as an
  external macro's. The dataflow follows moves, drops and assignments statement by statement.
- A call from another file can return into the next MIR block. That block's start counts as the
  line's, unless the IR block holding the call has every instruction located, none on this line,
  and doesn't end in an `invoke`. An instruction with no location, at line 0, or with an
  `inlinedAt` chain that doesn't resolve keeps the place. So `label = format!(…);` on line 4 is
  line 4's, not line 5's.
- The IR's files are told apart by directory and name: `src/a/mod.rs` is never `src/b/mod.rs`.
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
  during the call. The exception is where the MIR shows its target dropped and not yet replaced
  ([The Drop window](#the-drop-window)).
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
- A raw pointer keeps no borrow, so nothing proves what it points at. One inside a value shows only
  its address, however live the value is (#255). The build's DWARF tells it from a reference:
  rustc names pointer types `*const T`, `*mut T`, `&T`, `&mut T` and `alloc::boxed::Box<T, A>`.
  LLDB names them all `T *`.
- The address inside a fat pointer has no DWARF name. The fat pointer's own name says which it is:
  `*const str` or `&str`.
- Where the DWARF doesn't say, as on a path Peeky can't walk, the local's MIR type decides. A
  pointer shows only its address unless that type is made only of primitives, and references,
  tuples and arrays of them, such as `(i64, &i64)`. A struct's name says nothing of its fields.
  Its note then says that the build doesn't tell it from a raw pointer, not that it is one.
- A raw pointer local is judged by its MIR type, before #110. An array of them isn't collapsed:
  each element is judged as a raw pointer inside a value.

### Past its last use

- Between a reference's last use and the stop, a call or drop may move, drop or free its target. So
  on every path back from the stop to its last use, only branches, assertions and whole assignments
  to locals nothing borrows may run.
- A call of `std::ptr::eq` or `std::ptr::addr_eq` on two raw pointer locals may run too: it reads
  the two addresses, and nothing else (#259). The dump names `core::ptr::eq` `std::ptr::eq`, and
  `addr_eq` alone where no other item has that name.
- A crate's own `mod std` is dumped `std::ptr::eq` too, even one a macro declares:
  `make_mod!(std)`. So these names count only where all of these hold:
  - no function in the crate's MIR dump has such a name, with any prefix, such as
    `fn std::ptr::eq` or `fn inner::addr_eq` (the standard library's bodies are never in it);
  - no source declares, imports or renames a `std` or `core` of its own: `mod std`,
    `use x as core`, `extern crate core as std`, or a `macro_rules!` body that names one;
  - no macro is passed `std` or `core` other than as a path's start, as in `make_mod!(std)`;
  - the code names `addr_eq` only after `ptr::`, so it declares none.
- Making the reference a raw pointer (`&raw const (*f)`) is a use of it. A call given that raw
  pointer is no use of it: the raw pointer keeps no borrow.
- Where the proof fails, the reason it failed becomes the note. That is a value not yet assigned,
  the Drop window below, or the calls, drops and writes on the paths back to the last use, with
  their lines. The walk back stops after 16 of them.
- Where a call or drop is among them, only calls and drops are named: they can free the target.

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
  the pointer, unless the crate itself names something `core` or `std` that could pass for them;
- what a store through a pointer of such a value writes into (#311). `vec![x, 1]` fills its Box by
  `((*_10).1: …) = [move _7, const 1_u32]`, then makes the Vec of it. Each local the store may
  write into holds it: the pointer's aliases (`_aliases`, the Box and the Vec) and what they borrow
  (`Body._stored_into`). A binding among them holds the program's own copy; a pointer binding
  (`p` of `*p = x;`) only points at one. `_pointers_to` follows `p = slot.as_mut_ptr()` too.
- what a call may write through a pointer it is given, of what else it is given (#311 review). An
  argument counts where a callee may write through it (`_may_write`): a `&mut` or raw pointer,
  `NonNull`, a cell, an aggregate holding one, or a type not known. So
  `Vec::push(move _6, move _7)` with `_6 = &mut v` makes `v` hold `_7`'s copy, as do `ptr::write`,
  `mem::replace`, `mem::swap` and a `copy_nonoverlapping` statement. `Box::new` given a closure
  holding `&s` stores no copy of `s`: nothing else is given.
- A source counts where it holds a direct copy or points at one (the flow's `kept`, review round
  2). That is a borrow of such a holder (`&a` after `let a = [x];`), a cast or aggregate of such a
  pointer, and a pointer a call returns from one or from a pointer into the local (`a.iter()`).
- What a call returns is a direct copy where the call is given a pointer into the local or into
  what holds a copy (#320: `x.clone()`, `x.to_string()`, `ptr::read(&x)`, `format!("{}", x)`).
  Only a result type holding nothing but pointers holds no copy (`_pointer_only`, review): a
  reference, a raw pointer, `NonNull`, a tuple, array or `Option` of these, or a standard view of
  `_POINTER_VIEWS` (`slice::Iter`, `str::Chars`, `fmt::Arguments`). `(u32, &'static ())` and a
  struct `Hybrid<'_> { v: u32, r: &u32 }` may hold a copy.
- An adapter of `_TRANSPARENT` holds only the iterators it wraps (review round 2): `Rev`, `Fuse`,
  `Enumerate`, `Cycle`, `Cloned`, `Copied`, `Zip` and `Chain`. So `Rev<Chars<'_>>` holds no copy.
  `Skip`, `Take`, `StepBy`, `Peekable` and those holding a closure (`Map`) may.
- A standard path counts only under a root the crate gives nothing of its own
  (`OwnedLocals._std_crates`, set as `Body.std_paths`, review round 2). A `mod alloc` of the
  crate's makes `alloc::str::Chars` its own, and leaves `std::` paths the standard library's.
- Each proof needs only the root its path is under (review round 3). `core::slice::<impl
  [u32]>::len` needs `core`, `std::mem::take` needs `std`, and an inherent `Vec::len` needs the
  root of the type its argument points at. So a `mod alloc {}` leaves `a.len()` a measure.
- A result whose type shows a pointer (a reference, a lifetime, a closure, a trait object) may
  also point into the local (the flow's `aims`), as a cast, copy or aggregate of one does. A read
  through it may be a copy. A call given one may store a copy into what holds no pointer
  (`out.push_str(&name)`). Into what may hold one it may have stored the pointer itself, as
  #164's `v.push(&s[2..])` does, so that isn't counted.
- `Body._only_wraps` proves a call the standard library's that returns only what it is given
  (`_WRAPPERS`: `Box::new`, `Box::pin`, `Rc::new`, `Arc::new`). What it returns holds a copy only
  where what it was given did. So #164's `Box::new(move || check(t))`, with `t = &s[2..]`, holds
  none.
- So does an iterator adapter call (`_ADAPTER_CALL`: `<std::str::Chars<'_> as Iterator>::rev`,
  `map`, `filter`, `skip`, review round 2). `Iterator` must be the standard trait
  (`_std_iterator`), on a type under a trusted standard root. A `map` given a `move` closure that
  holds a copy holds it; one given a closure that borrows the local holds none.
- A binding that may hold a copy and may point into the local (`Body.pointing_copies_at`) is said
  to hold a copy "unless it only points into it" (`copy_hazards(reads="pointing")`). The advice
  then doesn't offer to change the copy.
- A cast to a type naming a function (`as Box<dyn Fn() -> usize>`) is no call: only a statement
  with a `return:` edge is one.
- `Body._only_measures` proves a call the standard library's that only measures or compares, so
  its result is no copy. It is a method of `_MEASURES` (`len`, `is_empty`, `contains`,
  `starts_with`) of a standard type, slice or `str`, or `eq`, `lt`, `cmp`, `partial_cmp` and the
  like of `PartialEq`, `PartialOrd` or `Ord`.
- Each argument must point at a primitive or a standard type (`_standard_only`), and the inherent
  method's type must be what the first argument points at. So the method is the standard
  library's own.
- A dump names an inherent method by its type's short name (`String::len`), as it would a type of
  the crate's own of that name. So the name must be one the crate gives no item of its own
  (`OwnedLocals._trusted_names`, set as `Body.trusted_names`): none it declares, imports under
  that name, passes bare to a macro, or names in a `macro_rules!` body (`_named_own`).
- A comparison needs its trait's root trusted, where the dump names one, and no `PartialEq`,
  `PartialOrd` or `Ord` of the crate's own (`OwnedLocals._std_traits`, set as
  `Body.std_traits`). `_std_compares` (`ptr::eq`) and `_std_transfers` (`vec!`'s Box) return
  the roots they trust.
- What a measure or comparison returns must be a `bool`, `usize` or `Ordering` the local's type
  can't hold (`_may_hold_type`): a `Vec<usize>`'s `len()` may be a copy of a `usize`.
- Where the proven standard `mem::take` or `mem::replace` is given a pointer surely to a local
  (`_sure_pointee`: `&mut a`, reborrowed, each assigned once and never borrowed), that local holds
  no copy after it. Its result holds what it held.
- A call stores only into what the pointers it is given were made from (`_made_from`: `v` of
  `&mut v`), since #320. It no longer stores into every alias `_stored_into` links to them, such as
  a pointer `format_args!` puts in the same tuple. A store statement still writes into them all.
- A callee may write through an argument where its type may (`Body._may_write_through`, review
  round 3). That is a `&mut` or raw pointer, a cell, a type parameter, or a mutable standard view
  (`_MUTABLE_VIEWS`: `Entry`, the `IterMut` family, `RefMut`, `Drain`, `ChunksMut`). Or something
  it was made from shows one (`_made_from`: a closure or a crate's struct capturing `&mut v`). A
  view that only reads, as `ChunksExact` or `btree_map::Iter`, writes nothing.
- What a callee reaches only through a shared `&` it writes only if it may take a value through
  one: a raw pointer, a cell, or what holds one (review round 4, and #321 below). `_may_write`'s `shared`, and `_unshared` within a type: `&&mut Vec<u32>`
  writes nothing. `_reached` walks what an argument was made from, noting where it passed a
  shared borrow or a shared reference type. So `look(&h, x)` with `h = Hold { p: &mut v }`
  writes nothing, while `keep(h, x)`, `poke(&mut h, x)`, and `&h` with a `*mut` or `&Cell`
  field may.
- A type counts by everything it holds, however deep (#321, `_may_write`, `_type_flags`). Each
  type it names gives flags: "interior" (a `&` to it may take a value), "owned" (by value or
  through a `&mut`, it may) and "shares" (another value may reach what it holds).
- A type's name is parsed into a tree (`_parse_type`), which `_Judge` walks, noting where it is
  reached through a shared `&` and behind a pointer others may hold (review). A function
  pointer or item (`for<'a> fn(&'a Cell<u32>) -> u32 {read}`) holds nothing, its signature's
  types and `&mut`s included.
- A standard type counts by its class (`_std_class`, `_STD_NAMED`), by the module its path names
  second and its last segment. That reads the dump's paths (`std::sync::Mutex`) and the debug
  information's (`std::sync::poison::mutex::Mutex`) alike. Its root must be trusted
  (`Body.std_paths`).
- The classes: "cell" (`Cell`, `RefCell`, `UnsafeCell`, `OnceCell`, `OnceLock`, `Mutex`, `RwLock`,
  the atomics), "channel" (`Sender`, `SyncSender`), "pointer" (`NonNull`), "receiving"
  (`Receiver`, which reads what a sender stored), "shared" (`Rc`, `Arc`, `Weak`, `LocalKey`),
  "lazy" (`LazyCell`, `LazyLock`), "inert" and "plain".
- "inert" types hold no value of the program's, and their type arguments aren't held: `Condvar`,
  `Barrier`, `Once`, `PhantomData`.
- A plain cell (`_PLAIN_CELLS`: `Cell`, `UnsafeCell`, `SyncUnsafeCell`) of values with a single
  state (`_Judge.single`, `Walk.single`) keeps nothing: `Cell<()>`, `Cell<PhantomData<u32>>`, a
  cell of a struct with no fields (review round 2).
- Such a cell has a single state itself (`Cell<Cell<()>>`), and so does an enum of one variant
  whose fields have one, without a discriminant (review round 3).
- A `RefCell` keeps its borrow flag, which a leaked `RefMut` leaves set (review round 3). A lock,
  a once-cell and a channel keep state of their own too: a poisoned `Mutex<()>`, a set
  `OnceCell<()>`, a `Sender<()>`'s message count.
- The parser tells a slice (`("slice", T)`) from an array (`("array", T, length)`). An array of
  length 0 holds nothing; a length that isn't a number counts as its element (review round 2).
  The debug information gives the length as a subrange's count.
- "plain" is every other type of `_PLAIN_MODULES` (`vec`, `collections`, `slice`, `iter`, `fmt`,
  `path`, `time` and the like): it holds what its arguments hold, which the word scan reads as
  types of their own. `ChunksExact` and `btree_map::Iter` are plain (review round 3).
- A standard type in no class, a trait object, an `impl` type, a coroutine, a raw pointer, or a
  dependency's type may hold anything. The may-analysis only ever says "may hold" of it.
- rustc 1.96 trims a signature's unique paths (`fn f(_1: &Cell<u32>, _2: &Vec<u32>)`).
  `_STD_BARE` gives those short names' classes. Each counts only where the crate gives that name no
  item of its own (`OwnedLocals._trusted_bare`, by `_named_own`).
- The crate's own structs, enums and unions count by what the build's debug information shows
  they hold (`rust_symbols.interior_types`, read with the application units' symbols). It walks
  every field, variant and element, and gives `OwnedLocals.interior` per type path and last
  segment.
- Each type's entry is a `CrateType`. A field whose type is one of the instance's own type
  arguments is left to the argument: `uses` notes how the type holds each (by value, through a
  `&`, not at all as in `PhantomData<T>`).
- `instances` keeps each instance by its name (`_key`): its flags, what may be stored in it, its
  fields' types, and whether it has a single state.
- `_key` names a canonical tree (`_Judge.canon`, `Walk.tree`). It keeps a crate type's path from
  the crate's root, its type arguments and an array's length. A standard type is named by
  module and last segment, only where those name one type (`_STD_KEYS`). The default allocator
  and hasher are left out.
- At a use, `_Judge.crate_flags` takes the instance by name where it can, else what it holds
  besides its arguments, and each argument as the type holds it.
- rustc leaves an argument that is its default out of the type's name (`&Gen` for `Gen<u32>` of
  `struct Gen<T = u32>`). Its default comes from the crate's sources
  (`OwnedLocals._declared_defaults`, set as `TypeLayouts.defaults`).
- Each parameter's name is kept with its default. A default naming an earlier parameter gets that
  argument (`_substituted`): `Pair<u32>` of `struct Pair<T, U = T>` is `Pair<u32, u32>` (review
  round 2). A projection (`T::Item`) can't be resolved, so it gives no argument.
- Where a default can't be found, and the instances' arguments differ, the type may hold
  anything ("unsure"). The refusal says which instance it is can't be told.
- `payloads` gives the trees of what a callee given a `&` may store, and `parts` the types of
  the fields (`("arg", n)` for argument `n`, `("payloads", n)` for what may be stored in it).
- `_copy_level` compares whole trees (review round 2). It says "copy" only where a payload, or a
  value it holds by value (`_Judge.components`), is the local's very type, whole. Anything else
  gives "derived", which is always true (review round 3: no "a copy of part of").
- `components` follows tuples, arrays, crate fields, and the standard types that own their
  arguments (`_STD_OWNING`). It doesn't follow references or iterators. It is used only on what
  may be stored, never on the local's own type.
- `OwnedLocals.holder_kinds` gives each holder's level. The refusal says "a copy" only where each
  holder's is "copy"; else "may hold state derived from", advising a change before it's used. A
  type not known gives "derived".
- A closure counts by what it captures, from the aggregate that builds it
  (`Body.closure_captures`). Where the debug information wasn't read, every type of the crate's may
  hold anything. These make `TypeLayouts`, set as `Body.layouts` when the body is matched.
- So a crate's `NotCell<'_>` or `Atomicity<'_>` holding `&mut v` takes nothing through a `&`
  (review round 5), while `Counter { c: Cell<u32> }` does.
- A field moved or copied out (`_5 = move (_6.1: Receiver<u32>)`) reaches that field of `_6`
  only (`_reached`). So `look(&rx, x)` writes nothing, though `mpsc::channel()`'s tuple holds a
  `Sender` beside `rx`.
- A call given a `&` to what may take a value also stores into what shares it (`Body._sharers`).
  That is a local moved or copied out of what the argument was made from, returned by a call
  given one or a pointer to one, or an aggregate, closure or cast built of one, whose type
  `_may_share`. So `send(&tx, x)` stores into `rx`, and `put(&a, x)` into `b` of
  `let b = Rc::clone(&a);` and `h` of `let h = Hold { c: a.clone() };`.
- A pointer to such a value is no sharer: its uses are the value's (`_pointers_to`). Nor is a
  cell moved out by value, which holds what it held then.
- A call's argument made only of constants carries no value (`f(0)` is `Fn::call(&f, (0,))`).
- A binding whose type may keep the copy behind a cell, a lock, a channel or a pointer
  (`OwnedLocals.hidden_holders`) may be out of a change's reach. The refusal says so ("which may
  be out of Peeky's reach"), and the advice offers no copy to change.
- `_pointers_to` follows what is made from a borrow through any value that may hold a pointer:
  `Option<&mut u32>` of `v.last_mut()`, then `r` of its `unwrap()`. So `copy_undoes` sees a
  later `*r` as a use of `v`.
- Where no local the pointers were made from stands for what they point at (`_pointees`), each
  pointer binding among them does. That is an argument `p`, or `Box::into_raw`'s pointer, whose Box
  is moved out of. `copy_undoes` judges it as a binding, and the refusal says it "may point at a
  copy" (`OwnedLocals.pointer_bindings`).

A pointer that Peeky doesn't follow may point into any local: a read through a reference variable,
or a call given one. A raw pointer counts as any other, since the source alone doesn't show unsafe
code: a standard-library macro brings its own (`#[allow_internal_unsafe]`).

- A call the dump names `std::ptr::eq`, `core::ptr::eq` or `addr_eq` (`_ADDRESS_COMPARISON`) reads
  only the two addresses. Where `OwnedLocals._std_compares` proves it the standard library's, as
  for a reference's last use above, its result holds only what its operands hold
  (`_copy_verdict`).
- A refusal states a read only where `copy_hazards(reads="explicit")` finds one at every place.
  That is `Body.read_copies_at`, a must-analysis met by intersection over every way into a block.
  It counts a `copy` or `move` of an overlapping place (`_reads_part`), and a read through a pointer
  surely made from a borrow of the whole local. A temporary computed from either counts too, but
  not a call's result (below).
- A write that may overlap the changed part ends what it knows: `_written_place` and `_apart`. A
  write through a pointer ends it too, unless the pointer surely points apart from the part: a
  borrow of another local, or of a part `_apart` from it, a copy of one, or an argument. A pointer
  loaded from memory (`(*((*_8).0: &mut u32))`) is never known to point apart. Nor is a pointer
  local that is itself borrowed anywhere (`Body._borrowed`): `q = &mut p; *q = &raw mut x;`
  retargets `p`, and `&p` may do so through a `Cell`.
- Where a writable borrow of a place that may overlap the part is taken anywhere in the body
  (`Body._exposed`), a call, a drop or a `copy_nonoverlapping` ends what it knows, on every edge.
  `keep(&mut x)` may change `x`, so `let y = x;` before it no longer surely holds a copy of `x`.
- A temporary computed from a binding's copy doesn't count as read: the statement read the copy.
  It counts only as the binding's (#192), through `copy_undoes` (`reads="taken"`, or `"copied"`
  where it may). A refusal names the bindings it was surely computed from (`reads="sources"`),
  which stays true after the binding is assigned again. Where it may be, `copy_sources` walks back.
- It names them only where every place has such a copy, from bindings common to all. Where each
  place holds a sure copy, some another way, it says "the program already holds a copy of it".
- A binding that may hold a copy is named apart from the statement under way, which may not use
  it: "the program's own `y` (at line 34) may hold a copy of it that it goes on with".
- It leaves out a pointer an aggregate or a call holds, a local a writable borrow is taken of, and
  a coroutine's body. `rust._copy_reason` decides the wording.
- A store through a pointer surely into a local, or into what it owns, of a value surely holding a
  read, makes that local hold it (`Body._read_store`). Such a pointer is a borrow of the local, a
  Box's own pointer (`_BOX_POINTER`, `vec!`'s), or a copy or reborrow of one. A Box written
  through itself (`ShallowInitBox`) counts too.
- A store through a pointer that may point into what holds a read ends that holder: one of
  `_stored_into`, or any, through a pointer loaded from memory. So does a drop through one. Only a
  local whose storage is borrowed, or whose type may hold a pointer, can be reached
  (`_addressable`).
- A call's result holds no sure copy: the callee may return anything (#311 review). What a call is
  given holds none after it, nor does a holder it is given an alias of that `_may_write` allows
  (`_read_clobber`). The one exception is `std::boxed::box_assume_init_into_vec_unsafe`, the step
  `vec!` makes its Vec with (`_STD_TRANSFER`). Its result holds what its Box held, where
  `OwnedLocals._std_transfers` proves the path the standard library's, as `_std_compares` does.
- Otherwise it hedges, by what each other reading finds:
  - `reads="value"`: reads by value, on some path, of what may overlap the changed part;
  - `reads="whole"`: the same for the whole local, so another part's read;
  - `reads="traced"`: also pointers the flow shows may point into the local;
  - `reads="any"` alone: a pointer Peeky doesn't follow. `untraced_reads` names those variables,
    each by its kind.

A rule by type isn't used, though a `*const Engineer` can't point into a `u32` in safe code. It
holds only for a pointee of the crate's own type that holds no pointer, which only the debug
information shows. Standard types can view a local as another type: `Cell::from_mut`,
`slice::from_ref`.

- The line's first MIR place may emit no code, as `&k` in `let t = (&k,` / `s.a,` / `s.b);`. So the
  stop's column places a breakpoint on its line.
- `let arr = [s.a,` / `k,` / `s.b];` builds its array on line 1, after reading `s.a`.
- `OwnedLocals.copied_everywhere` finds lines where every place a line can stop at is inside such a
  statement. Places that emit no code, such as `StorageDead`, are left aside.

### Code inlined before the stop

LLVM inlines `#[inline(always)]` calls even at opt-level 0, such as an integer's `clone`. Their
rows carry the callee's lines, so their code may run below the line's first row of its own, where
the breakpoint stops (#324).

- `RustSession._inlined_below` finds them. They are the DWARF's inlined calls made from the stop's
  line (`CodeBlock.call`: the call's file, line, column and callee's name), with code below the
  stop's pc.
- It also reads the code just before the pc (`_window`): back to the last row of another line
  that is a statement's, outside inlined code and macro definitions (`macro_lines`). An inlined
  call there that isn't a statement's is a doubt (`_unplaced`). Its site may be a macro's
  definition, another file, or not given. A doubt withholds every local (#324 review).
- `OwnedLocals.inlined_reads` matches each column, and name, to the line's MIR calls whose span
  holds it (`_covers`, `_callee`). In safe code, a local given to one, or what may point into it
  (`Body.lent_from`), is withheld. The borrow rules make a pointer into a local in its own frame,
  which `lent_from` follows.
- In unsafe code, a call may read a local through a pointer it isn't given, as a `static mut`
  holds. So every inlined call but the standard library's `ptr::eq` withholds every local.
  That `ptr::eq` must be proven under the root the dump names it by (`_trusts`,
  `_comparison_root`, #320 review round 3).
- Where the DWARF's inlined calls made from the line aren't the IR's (`IrFunction.inlined`), or a
  column holds no call, every local is withheld. Where the pc or the DWARF blocks are not known,
  every call the IR shows inlined on the line counts, and a line that may invoke a macro is not
  understood.
- A step that ends at the line's lowest address, the inlined code included, stops before that
  code (`_line_start`). LLDB stops there after a step from the line before, except before a
  macro's code, which it shows on the macro's definition (below).

### Stops LLDB shows at an inlined call's site

At a call the build inlined, LLDB shows the caller's frame on the line the call is made from, the
DWARF's `DW_AT_call_line`. That line may have no row of its own in the line table, which
`RustSession.lines` is read from (#326).

- `validate_stop` accepts such a line only where the frame's pc is in the code of a call inlined
  into its function from that line (`_inlined_site`, `CodeBlock.call` and `ranges`). A caller
  frame's pc - 1 counts too, since its return address may end that code. A macro's definition
  (`macro_definitions`) counts by the source alone only where the function's blocks aren't known.
  Any other line with no row still fails closed.
- The pc needs the load bias, which `validate_debugger` reads with the module. Until then
  `validate_stop` lets such a line pass, and `validate_debugger` checks it before anything of the
  frame is read.
- In a macro's definition (`$v.clone()` of `macro_rules! mclone`), the debug information names
  no invocation. So rules keyed by the line would judge the definition as a line of the function.
  `macro_stop` finds such a stop: no row of the line in the frame's function (`_own_rows`).
- The worker then reads no value of that frame: `inspect` raises `macro_stop_note` as
  `UnplacedStop`, a request error, and the state carries it as `macro_stop`. A step or continue
  goes on.
- The state is built before anything validates the stop. So `macro_stop_state` gives the note
  only where the pc is proven in the inlined call's code, the load bias read first
  (`validate_debugger`). Without the function's blocks, the source alone tells, as `inspect` does.
- Where the source isn't read as rustc reads it, `macro_stop` gives `()`: the line may be a
  macro's, and the note says Peeky can't tell.
- A probe that stops there (its breakpoint told by the frame's place) withholds each selector's
  row with that reason and goes on. A change due there is refused (`_unplaced_rows`).
- A macro statement's own rows on the definition's lines are judged by their line, as before.
- A line of the function's own with no row, as `bump(&mut x);` of an `#[inline(always)] fn bump`,
  starts at the lowest inlined call made from it (`_line_start`, `_site_lows`).
- `unplaceable_line` refuses a breakpoint on a line with no row where an inlined call is made.
  LLDB would place it on the call's code, and report the callee's line (`clone.rs:615`). The
  worker asks before it places a breakpoint (`refuse_unplaceable`).

### The order the code runs

`Body.copies_at` reads the MIR in the order the code runs.

- An assignment to the local, or the start or end of its storage, leaves no temporary holding its
  new value. So a temporary made before doesn't count.
- A call's result counts only on its return edge.
- `Body.direct_copies_at` gives the direct copies, from the blocks reachable from `bb0` or a
  coroutine's resumption. That is a copy or move of the local or a part, what is computed from one,
  a closure or aggregate holding one, and a read through a pointer to it. It is also what a call
  given a pointer into it returns (#320), but not what a standard measure returns, as `x.len()`.
- Every reading of `copy_hazards` judges by `copy_undoes` the same direct copies: those the "any"
  reading doesn't hold as temporaries. So a value `format!` made from a binding's copy is never
  said to read the local.
- A temporary that read the local blocks the change wherever it is used again. One taken from a
  binding's copy is judged as that binding is: `a + x` still reads `x`.
- Copies are judged by the part they may have taken: `direct_copies_at(…, projection)`. An
  aggregate's `[settable]` gate leaves them out (`mid_statement(members=True)`), and judges each
  member alone.

### The program's own bindings

A binding of the program's own blocks the change where, on some path, the program assigns or ends
the local without reading the changed part, and uses the binding before or after that
(`Body.copy_undoes`). Only direct copies count for bindings.

- A use after the local is assigned or ends counts too (#330): `u.push(y);`, then `y = t;`, then
  `u` printed. So does a Vec a struct was pushed into, used after the struct ends. The copy is all
  that is left of the old value.
- The frame's exit ends every local and argument (`Body._frame_exit`): a `return`, a call that
  never returns (`panic!`'s, `process::exit`), or `resume`, `abort` or `terminate` after one. An
  argument has no `StorageDead`, nor has a local on a panic's path.
- A path a call, a drop or a check takes only when it panics (`Block.unwinds`, its cleanup edge
  beside another) ends nothing. Every call may panic, so such an exit is no sign the program
  goes on with the copy.
- A drop of the local that may run code reads it (`Body._drop_reads`): its type's own `Drop` may
  print the changed field. A `_quiet_drop` type's, and a drop of a field surely apart, don't.

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

### Values no path reads again

Where no path from the stop reads the changed part before it is overwritten or ends, a change is
never seen (#330), whether or not a copy is kept. The rule is in the cookbook's
[When the program never reads it again](lang/rust-cookbook.md#when-the-program-never-reads-it-again).

- `Body.unread_ends` walks every path from each place, as `copy_undoes` does. It returns where the
  part ends: "write", "scope" (`StorageDead`), "return", or "stop", where the frame exits otherwise
  (a panic, an exit). It returns None where a statement may read it first.
- Each path the program takes must reach one of them. Where a loop has no way out to one, it adds
  "loop". A path a call or a check takes only when it panics is read through for reads, but
  reaches no end of its own.
- `Body._unread_access` judges each statement strictly, unlike `_root_access`. A borrow, a
  dereference, a `discriminant`, a call given the local, and a drop that may run code are reads. A
  `copy` or `move` of a place `_apart` from the part isn't, nor is a drop `_quiet_drop` allows.
- `OwnedLocals.unread_after` adds what the walk can't see. A pointer into the local, of `lent_from`
  or `_pointers_to`, that is live ahead counts as a read, as for `borrow_live`. So does a crate with
  unsafe code, or a coroutine's body: then it says nothing.
- `mid_statement` gives this reason only where no copy hazard is found, so a copy's reason, with
  its advice, comes first. Before the run, `RustSession._unread_everywhere` refuses it where every
  place of the line that emits code is so (`coded`).
- The wording, from `rust._unread_where`: "it is overwritten at line 7 first", "it goes out of
  scope at line 14 first", "the function returns at line 9 first", joined with "or". A line is
  never said to come first where another path panics, exits or loops forever: "…, unless the
  program panics or exits before that", or "the program panics or exits first".

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

## Line tables

`peeky.rust_symbols` is the boundary for symbols. `RustTarget` and the worker use it, and so does
the engineering probe.

- One bounded dwarfdump index, `--debug-info --recurse-depth=0`, lists the compile units.
- A unit is the application's only if both are true:
  - its `DW_AT_comp_dir` is the fresh workspace's canonical path, with aliases not resolved;
  - its name is `<entrypoint>/@/<crate>.<hash>-cgu.<n>`.
- A unit with only one of the two is ambiguous, and preparation fails.
- Each selected unit must be DWARF 4, `DW_LANG_Rust` and produced by rustc, with its own line
  table. There are at most 16.
- All the dwarfdump calls share one time budget. The worker gives it at most 10 s, or less if the
  launch's deadline is nearer. `application_symbols` accepts at most 30 s: its default, for direct
  callers such as the engineering probe.
- Each table is dumped by its exact `DW_AT_stmt_list` offset, and its header's offset must match.
  The native worker's `parse_lines` then reads it, fail-closed.
- For Rust, dwarfdump's output and the line parser are each bounded at 16 MiB. C and C++ keep
  their own 2 MiB limit.
- The standard library's tables are never read. Fewer lines can only refuse a breakpoint or a
  stop: a standard-library line can't be taken for a declared file's.

## Enum views

The selected unit's DIE tree gives the layout the compiler emitted for each enum that carries
data. Reading it is bounded:

- depth comes from indentation;
- every child list must end with an explicit `NULL`;
- truncated and unknown rows are refused;
- every entry must be inside the unit's offset range.

**`core::option::Option<i32>` is qualified through LLDB's view,** with exactly this layout:

- one artificial discriminant, with distinct 32-bit `DW_AT_discr_value`s for `None` and `Some`;
- a `None` with no fields;
- a single `__0` payload.

The discriminant's and the payload's types are resolved through the DIEs they reference: a
four-byte `DW_ATE_unsigned` `u32` and a `DW_ATE_signed` `i32`. dwarfdump's labels are never used.
Other payloads, apart from the tuples and structs below, are unqualified. So are `Result`, user
enums, types outside a plain namespace, and conflicting definitions.

**Reading one.** `active_variant` accepts only LLDB's exact raw shape:

1. `$variants$`;
2. then `$variant$<DW_AT_discr_value>`, in DWARF order, typed for that variant;
3. then `$discr$`, and a `value` of the same variant's type.

- Every discriminant view must agree, and be a compiled value.
- Only the active payload is read. The result is `Some(<i32>)` or `None`, with no expansion handle.
- An inactive payload is never fetched.
- An unqualified enum is opaque: its storage isn't read. A shape that doesn't match is
  `<unverified Rust enum value>`, never a guess.
- The session refuses to expand the raw `$variants$` and `$discr$` storage of an enum missing from
  the application's layouts.

**An `Option` of a tuple or crate struct is read from memory** (`_composite_option_layout`,
`option_view`). LLDB's view isn't used. Apple's LLDB names the `None` of `Option<Engineer>`
`$variant$0`, though its value is 0x8000000000000000. The layout must have:

- one artificial discriminant member, a `u8` to `u64` base type, and two variants, `None` and
  `Some`, each a struct of the `Option`'s own size;
- a `None` with no fields, and a `Some` with one `__0` of the payload type its name gives;
- a payload of the crate's own, or a tuple, that `_composite` reads, as a container's element.

Either both variants have a `DW_AT_discr_value`, and the tag lies beside the payload. Or only
`None` has one: a niche. Then `_niche_range` walks the payload's members, through nested structs,
to the one field that the discriminant spans exactly. Its type must fix its valid values:

- `core::num::niche_types::UsizeNoHighBit`'s `__0`, a `Vec`'s or `String`'s capacity: 0 to
  `isize::MAX`;
- a `core::ptr::non_null::NonNull<T>`'s `pointer`, a reference (`&T`) or a `Box`: never 0;
- a `bool`: 0 or 1;
- a `char`: 0 to 0x10FFFF.

`None`'s value must lie outside them. Anything else, such as a raw pointer, an integer or an
enum inside, leaves the `Option` unqualified.

**Reading one.** The session reads it as an owned `Vec`: a local or parameter of the innermost
frame of an unoptimized build, which the MIR proves initialized at the stop. The MIR's type picks
the layout (`option_name`), as for a struct preview. One whose payload holds a `String` needs a
crate with no unsafe code.

- The tag equal to `None`'s value is `None`. One in the niche's valid range, or equal to `Some`'s
  value, is `Some`.
- Any other tag is `<unverified Rust enum value>`. So is a payload that doesn't read, such as a
  `String` longer than its capacity.
- `Some`'s payload is its one child, `__0`, read as a container's element. Its children are the
  payload's fields. Nothing is settable through them.
- One without these proofs is withheld as `<unverified Rust enum value>`, with why.

**Every opaque enum says why** (`enum_withheld`): the worker passes `opaque_reason` on its row,
which becomes the row's note. It gives the address LLDB stores it at, from `memoryReference` and
the `T @ 0x…` value, which must agree. It is printed as a pointer is, so a pointer into it can
be compared. And it says what to do instead (`binding_advice`): read what an `if let` on it
binds (`Some(x)`, `Ok(x)` or `Err(e)`), or a `match` arm. A reason that leaves no value to
borrow gives its own advice. Where the MIR doesn't prove it initialized, read it on a line after
its assignment. In a caller frame, stop in its own function. In an optimized build, build
unoptimized.

`option_name` drops a MIR type's lifetimes first (`Pick<'_>` is `Pick`): the debug information
never names them.

## View reads

`RustSession` uses the native session's stop and module checks. It also sets
`settings set target.load-script-from-symbol-file false`, and reads back each value it writes.
What it can change is in [Changing a value](#changing-a-value).

- A view has a `view` label. A leaf view has `variablesReference: 0`.
- A decoded view with children gets a worker-owned expansion handle, for at most 512 children. It
  never reaches the debugger, and exposes no raw storage.
- Only a struct or tuple preview's fields can change through such a handle. The handle keeps where
  the debugger lists the value, and a field marked settable changes under the usual member rules.
- A container's contents (a `Vec`'s, a `String`'s, a `Box`'s) and an enum's variant never change
  through a view.
- A request's view reads share one budget: 128 fetches, 10 s, 1,024 items and 1 MiB. One absolute
  deadline covers each read's write and its response.
- Running out is `budget-exhausted`, apart from a shape Peeky can't verify. That includes a read
  whose reply misses the deadline. A write that doesn't complete is fatal.
- After any view read, a change's handles are bound to scopes fetched again, and checked.
- A debugger field whose name starts with `$` is refused, at every level.

## Optimized builds

The config's `opt_level` is `"0"` by default, or `"1"` or `"2"`.

- It sets `CARGO_PROFILE_DEV_OPT_LEVEL`, and the target identity's profile records it.
- The [MIR build](#the-mir-build) still runs, and its MIR is parsed. But no owned-local proof
  shows a container's contents. A `Vec` or a `String` stays raw, with the reason "Peeky reads a
  container's contents only in an unoptimized build".
- A local with no value is reported as `unavailable`.
- Every result carries `optimized` and a warning, because optimized code can show wrong values
  that LLDB doesn't flag. In a real run, `start` read `1` at opt-level 2, and `3` unoptimized.
  See [COOKBOOK 7.5](COOKBOOK.md#75-optimized-builds).

## Limits and qualification

- dwarfdump's octal escapes and the LLVM IR's hex escapes read back. rustc's MIR and Cargo's
  messages are split at `\n` alone. So a source's name may hold any character.
- Edition 2024 was qualified by running the whole Rust suite, 56 tests with every owned-local proof,
  with its example crate switched to 2024.
- Test processes share OS permissions with the checker, and are trusted code.
- No live-model Rust evaluation has been recorded.
- Not delivered yet: any `Option` payload other than `i32`, a tuple or a crate struct, and any
  other data-carrying enum.
- A crate can be debugged on a separate host, through a worker started with `--rust-config`
  ([REMOTE.md](REMOTE.md#c-c-and-rust-on-the-worker-host)).
- `tests/rust/test_rust_worker.py` runs the worker on a real Cargo build and LLDB. It covers:
  - launch over HTTP, `Some(3)` and `None`, struct expansion and a fieldless enum;
  - refused changes: an `Option`, a value out of range, an expression;
  - `start=0` giving the corrected first page, the report and cleanup;
  - a changed source failing closed, and a compile failure or a test edit leaving no workspace.
- The raw-DAP engineering probe, `scripts/probe_rust.py` (`tests/rust/test_rust_probe.py`), is
  evidence for qualification only. It takes the `PEEKY_RUST_*` tool paths, or the same paths as
  named arguments. CI resolves the tools with `rustup which --toolchain 1.96.0`, and uses LLVM 18.
  The probe:
  - records the tools' and inputs' hashes, and requires Rust and Cargo 1.96.0;
  - builds a fresh copy of the crate, offline;
  - imports no Rust formatter, turns LLDB's source-init files off, and loads no scripts from symbol
    files. The worker's own initialize request also sends both `sourceInitFile` flags as false;
  - checks the stopped source and line, and the loaded module's identity;
  - changes a scalar, and shows that the output changes without a rebuild;
  - checks that the inputs' hashes are unchanged, and keeps bounded events and cleanup.
