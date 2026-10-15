# Experimental Rust investigation loop

The Rust controller uses the same observation, intervention and candidate-verification flow as Java, C and C++. A fixed Rust worker builds and debugs one dependency-free Cargo binary through LLDB-DAP (see [RUST_GATE.md](RUST_GATE.md)). `RustVerification` rebuilds each candidate in a fresh workspace. Only declared application `.rs` sources are editable; `Cargo.toml`, `Cargo.lock` and the integration-test file are frozen inputs included in every identity check.

## Two independent gates

A Rust candidate passes only when both of these pass on the same fresh build:

1. **Compiled Cargo test.** Preparation runs `cargo build --offline --locked --bin <name>`, then `cargo test --no-run --offline --locked --test <name>` with the same controlled environment. The test executable is selected from the hashed `target/debug/deps` directory: exactly one, matching the declared test name, with no unexpected artifacts. Verification **executes that hashed binary directly** with `--test-threads=1`. Cargo is never invoked at verification time, so nothing can be rebuilt or substituted. Cargo's uplifted `target/debug/<bin>`, which the test runs through `CARGO_BIN_EXE_*`, is a hashed artifact and must be byte-identical to the debugged executable before and after the run. The test runs with a 16,000-byte output limit, so any output beyond the retained capture is an explicit failure rather than silent truncation. The final non-empty line must be the harness's single `test result: ok.` summary, with at least one passing test and zero failed, ignored, measured or filtered tests; the exit code must be zero and the run must not time out. Anything else is a test failure, and an earlier forged summary cannot satisfy this check.
2. **Independent checker.** The unchanged supplied `examples/rust/verify_pagination.py` runs against the prepared descriptor (12 page cases and 10 invalid-argument cases) and must complete through the trusted wrapper. A passing Cargo test can never replace it.

Baseline test failure is recorded and does not stop the investigation; the checker must fail on the original. A failed test assertion or checker case is a verification failure that returns to the decision loop. Candidate compile errors are retryable `build` results only when Cargo's JSON messages (`--message-format=json-diagnostic-short`) show every rustc `error` with a primary span in a declared application source. Span-less errors such as linker or driver failures, errors elsewhere, and malformed or possibly truncated messages are fatal. A test-target compile failure, toolchain/probe failure, unexpected artifact, executable drift or source/test/checker change is fatal.

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

## Limits

One dependency-free binary and one integration-test target. No workspaces, build scripts, dependencies, doctests or unit-test harness of the binary itself. libtest output beyond the 16,000-byte capture is a failure, not a pass. Test processes share OS permissions with the checker and are trusted code. No live-model Rust evaluation has been recorded.
