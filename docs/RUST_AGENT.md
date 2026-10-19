# Experimental Rust investigation loop

The Rust controller uses the same observation, intervention and candidate-verification flow as Java, C and C++. A fixed Rust worker builds and debugs one dependency-free Cargo binary through LLDB-DAP (see [RUST_GATE.md](RUST_GATE.md)). `RustVerification` rebuilds each candidate in a fresh workspace. Only declared application `.rs` sources are editable; `Cargo.toml`, `Cargo.lock` and the integration-test file are frozen inputs included in every identity check.

## Two independent gates

A Rust candidate passes only when both of these pass on the same fresh build:

1. **Compiled Cargo test.** Preparation runs `cargo build --offline --locked --bin <name>`, then `cargo test --no-run --offline --locked --test <name>` with the same controlled environment. The test executable is selected from the hashed `target/debug/deps` directory: exactly one, matching the declared test name, with no unexpected artifacts. Verification **executes that hashed binary directly** with `--test-threads=1`. Cargo is never invoked at verification time, so nothing can be rebuilt or substituted. Cargo's uplifted `target/debug/<bin>`, which the test runs through `CARGO_BIN_EXE_*`, is a hashed artifact and must be byte-identical to the debugged executable before and after the run. The test runs with a 16,000-byte output limit, so any output beyond the retained capture is an explicit failure rather than silent truncation. The final non-empty line must be the harness's single `test result: ok.` summary, with at least one passing test and zero failed, ignored, measured or filtered tests; the exit code must be zero and the run must not time out. Anything else is a test failure, and an earlier forged summary cannot satisfy this check.
2. **Independent checker.** The unchanged supplied `examples/rust/verify_pagination.py` runs against the prepared descriptor (12 page cases and 10 invalid-argument cases) and must complete through the trusted wrapper. A passing Cargo test can never replace it.

Baseline test failure is recorded and does not stop the investigation; the checker must fail on the original. A failed test assertion or checker case is a verification failure that returns to the decision loop. Candidate compile errors are retryable `build` results only when Cargo's JSON messages (`--message-format=json-diagnostic-short`) show every rustc `error` with a primary span in a declared application source. Span-less errors such as linker or driver failures, errors elsewhere, and malformed or possibly truncated messages are fatal. A test-target compile failure, toolchain/probe failure, unexpected artifact, executable drift or source/test/checker change is fatal.

## The MIR behind owned-local views (#53)

Unoptimized builds also compile the binary crate through a small rustc wrapper (`RUSTC_WORKSPACE_WRAPPER`). It is written into Peeky's own build directory, and its bytes are rechecked before every command. For that crate only, the wrapper adds:
- `-Zmir-opt-level=0`;
- a `PreCodegen` MIR dump into `target/peeky-mir`;
- legacy symbol mangling.

`RUSTC_BOOTSTRAP` is set to that crate's name only, so no other crate gets unstable features. The test target and anything else Cargo compiles get no flags, and Cargo reuses the one binary unit for both commands. The dumps are a hashed build artifact.

`rust_mir` uses them as follows:
- It parses the dumps and runs a definite-initialization dataflow over each body.
- It maps the stop line to MIR blocks through the LLVM IR's debug locations, which already attribute macro-expanded code to the invoking line.
- It reads an owned `Vec<T>` (`T` an integer type, `bool`, `f32`, `f64` or `char`) or `String` local only if, in every such block (and any block codegen merged into it), the local is initialized at each place code of that line can be: before each statement on the line, or from another file (external macro code), following moves, drops and assignments statement by statement. A block whose code of the line no statement explains must keep the local throughout. The stop must be in the function's own file, and the MIR body must be identified positively: exactly one same-named body with a span on the function's `fn` line, owning every line of its code that any body has a span on.

Anything not understood proves nothing: an unparsed statement, a coroutine, a projected or doubly bound name, a body that is ambiguous for its IR function (macro-generated twins included), a block of unknown origin, or a stop in another file (a macro defined there). A crate using `#[collapse_debuginfo]` gets the whole-block rule only. Optimized builds use no wrapper and read no owned locals. Unsafe code that duplicates ownership (`ptr::read`) is outside the proof.

## Required deterministic proof

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

## Reading Rust values

Rust reference parameters (`&[T]`, `&str`, `&Vec<T>`, `&String`, shared or `&mut`, where `T` is any integer type, `bool`, `f32`, `f64` or `char`) are read from the program's memory, at offsets checked against the build's debug information, when the debugger supports `readMemory` (lldb-dap from LLVM 20). Rows show `[memory]`; selectors index them (`values[0]`, `values[-1]`) and read their length (`values.length` for slices and `&str`, `list.len` and `list.capacity` through `&Vec`/`&String`). Text is shown whole. `&mut` parameters are read only in the innermost frame. Elements show as Rust's `{:?}` prints them (`[0.1, -0.0, 1e16, NaN]`, `[true, false]`), and each element row carries its exact type (`u64`, not LLDB's `unsigned long`). Scalars do too, with LLDB's name in `debugger_type`. Locals and parameters take theirs from the build's MIR (`usize`, `f64`, `char`). A field takes the one primitive LLDB's name can be (`double` is `f64`); only `long` and `unsigned long` (i64/isize, u64/usize) keep LLDB's name there. `f64`/`f32` values print as Rust's `{:?}` (`10.0`), and `u8`/`i8` values as integers (`65`, not LLDB's `'A'`). Owned `Vec<T>`/`String` locals are read too, in the innermost frame, where the build's MIR proves them initialized at the stop line. Where they may be moved, dropped or not yet assigned (`drop(v)`, `consume(v)`, `let s = s + "x"`, a `let label: String;` before its assignment, a move on that very line), they keep their raw fields (`ordered.len`) with a note, because their pointer can reach freed memory (#53). Reference locals (`let view: &[i32]`) keep their raw fields. Optimized builds give no proof, so parameters there keep their raw fields or show as optimized out. Long values show their first elements or characters with `(first k of N)`; up to 256 elements stay selectable.

## Limits

One dependency-free binary and at most one integration-test target, edition 2021 or 2024. A plain `cargo new` crate qualifies: targets may be explicit (`[[bin]]`, `[[test]]`, discovery switched off) or Cargo's defaults (`src/main.rs`, `tests/*.rs`), provided nothing else Cargo would discover exists (`src/lib.rs`, `src/bin`, `build.rs`, test directories). `[dependencies]` tables must be empty, and package metadata fields (`description`, `license`, ...) are allowed. Without an integration test, `verify` relies on the checker alone. Edition 2024 was qualified by running the whole Rust suite (56 tests, every owned-local proof included) with its example crate switched to 2024. No workspaces, build scripts, dependencies, doctests or unit-test harness of the binary itself. libtest output beyond the 16,000-byte capture is a failure, not a pass. Test processes share OS permissions with the checker and are trusted code. No live-model Rust evaluation has been recorded.
