# Rust MVP qualification plan

Status: proposed next slice; no Rust runtime support is claimed. Keep the first target a dependency-free, single-package Cargo binary with a committed lockfile and explicitly declared source and test files. C/C++ conformance does not establish Rust support.

## Evidence and first gate

Local inspection on 2026-10-14 found Rust/Cargo 1.96.0, host `aarch64-apple-darwin`, rustc commit `ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96`, and compiler LLVM 22.1.2. Apple LLDB DAP is available at `/Library/Developer/CommandLineTools/usr/bin/lldb-dap`. This inventory is not a qualified combination. Linux CI currently uses LLVM 18, so compiler/debugger/formatter compatibility must be proved there before implementation is advertised.

First run a scratch probe with a pinned Rust release and the CI adapter: Cargo build, source breakpoint, struct fields, enum variants, `Option<i32>` Some/None, a mutable `i32`, continuation, close and actual source/module identity. Record raw DAP shapes, debug-info versions and formatter identities. If that pair fails, select and pin a compatible pair explicitly; do not silently replace system LLDB or infer compatibility from local macOS.

## Narrow runtime boundary

Add `RustTarget` and `RustSession` rather than extending Clang-specific `NativeTarget.prepare`. Reuse native ELF/Mach-O identity parsing, line mapping and guarded module checks through a small shared helper when a second implementation requires it. Do not copy the entire native session or introduce arbitrary command configuration.

Trusted startup configuration identifies Cargo, rustc, LLDB DAP, dwarfdump, optional macOS symbol tooling, package/binary name, declared inputs and application arguments. Use real versioned `sysroot/bin/cargo` and `sysroot/bin/rustc`, not rustup dispatcher paths: hashing the dispatcher does not identify the selected compiler. Record rustc commit, host, Cargo version and actual executable hashes. Set `RUSTC` explicitly; do not let Cargo select a compiler from ambient PATH.

The existing shared verification boundary remains:

- `verification_inputs`: `Cargo.toml`, `Cargo.lock`, declared `.rs` sources and immutable independent Cargo integration-test files.
- `verification_sources`: only approved application `.rs` files; manifests, lockfile and tests are not model-editable.
- `prepare(program, root, edit=None)`: fresh copy and build, with original/staged identities checked even after compiler failure.
- Session `verification_command`: the single prepared application executable; fixed arguments and controlled environment remain separate.
- Session provenance, `source_path`, `validate_stop`, `validate_debugger`, and cleanup have the same worker contract. Reuse the read-only expansion API and its opaque, inspection-bound handles.

Initially accept one explicit binary target and a small explicit test target. Reject dependencies (including path/dev/build dependencies), workspaces, build scripts, proc macros, custom runners, custom profiles and unsupported Cargo manifest target discovery. Validate the supported manifest subset with TOML parsing; never implement policy by searching substrings. No dependency downloads or arbitrary shell commands.

## Cargo build and independent tests

Use fixed argv for `cargo build --offline --locked --bin <configured-name>` and an explicit fresh target directory. Disable incremental compilation, force optimization level zero/full debug info, DWARF4, no stripping and an explicitly qualified split-debug-info mode. Supply those settings from the worker-controlled recipe; reject conflicting manifest settings. Configure the linker explicitly where necessary and record its executable identity.

Cargo searches ancestor configuration as well as CARGO_HOME. Give it a fresh controlled CARGO_HOME, clear ambient Cargo/Rust wrappers and flags, and prevent untrusted `.cargo/config` or `.cargo/config.toml` in the staging directory or its ancestry from influencing commands. An isolated CARGO_HOME alone is insufficient. A conflicting ancestor config must fail preparation unless an independently verified isolation mechanism excludes it. Do not copy application `.cargo` directories in the first slice. Account for Rust/Cargo dynamic libraries and the standard library as toolchain dependencies; executable hashes alone are not hermeticity.

The compile/test split needs an explicit small extension to verification, not placing test execution in `BuildRecipe.build_commands`:

1. Prepare the application and compile the configured tests with `cargo test --no-run --offline --locked --test <name>`. Parse bounded Cargo JSON messages to identify expected test executables, constrain every path to the fresh target directory, and hash application/test executables plus symbols. Reject missing, duplicate, unexpected or escaping artifact identities.
2. Run a fixed `cargo test --offline --locked --test <name> -- --test-threads=1` verification stage with the same compiler, environment and target directory. Record completion/exit status and check all prepared identities before and after. Prove this does not rebuild or substitute any executable; unexpected rebuild/artifact changes are fatal. Suppress doctests and implicit target selection through explicit target arguments.
3. Run the existing unchanged Python checker against the prepared descriptor. It independently executes application cases; passing Cargo tests alone cannot bypass checker completion.

Add only a trusted session verification-test hook/result to the shared engine if required. Preserve Java/C/C++ behavior when the hook is absent. A failed test assertion is verification failure, distinct from compilation failure. Cargo exit code alone cannot distinguish syntax errors, manifest/config failures and test failures: use the known stage and bounded structured diagnostics. Retry candidate compiler/linker diagnostics only; missing tools, failed probes, malformed messages, failed symbol extraction and provenance drift remain fatal. Ordinary baseline test failure must still permit the investigation to begin.

Retain manifests, diagnostics, candidate hashes and test/checker evidence, but remove fresh builds on every success/failure/cancellation path. Cargo, rustc, linker, test processes and debugger/debuggee must stay within owned bounded process groups. Test the cleanup of spawned descendants.

## Symbols and formatter policy

Force application DWARF4 deliberately, then inspect actual output. The present line parser rejects other versions and limits a full dump to 2 MiB. Rust's linked standard library and macro-generated mappings may exceed that limit or contain other versions. Do not silently truncate or globally loosen parsing. First measure the fixture; if needed, add a bounded declared-source extraction strategy that still proves each selected path/line and rejects ambiguous mappings. Hash all separate symbol files and retain current executable build-ID/UUID and loaded-module checks.

Rust formatters are executable Python inside LLDB. Default to raw supported fields; if `Option`/enum qualification requires formatters, permit only a pinned, audited bundle matched to the chosen compiler. The installed rust-lldb wrapper imports `lldb_lookup.py` and sources `lldb_commands`; `lldb_lookup` also imports providers and Rust type helpers. A hash of just the entry file is insufficient.

Copy the full approved dependency closure to an owned directory, record every file hash, disable bytecode writes, exclude project/ambient Python import paths, and verify hashes before launch and inspection. Generate only fixed worker-owned initialization commands with correctly quoted controlled paths; never expose command hooks over HTTP. Keep both LLDB init-file flags disabled and explicitly qualify automatic symbol-file script loading. No arbitrary project formatters or commands. Record formatter version/hash in the session manifest and report whether raw or formatted inspection was used. Do not claim formatter evaluation is side-effect-free or hostile-code isolation.

## Mutation and representative fixture

Qualify exact debugger-reported `i32` and possibly `u32` root locals separately. Apply numeric range checks only after actual LLVM/Rust type-shape evidence; do not reinterpret C `int` matching as Rust support. Reject references, pointers, aggregate fields, enum discriminants, `Option`, unsigned/width variants not qualified, expression-valued names and expanded children. A successful mutation does not promise preservation of Rust ownership or borrowing invariants.

Use a bounded pagination example with a request struct, an enum for mode, Some/None values and `let mut start: i32`. Inputs are small validated integers so overflow/panic/unsafe code does not obscure the experiment. The sequence observes the incorrect first-page offset, mutates the root scalar, then proposes an invalid compile, an overfit constant and the general formula. Immutable Cargo integration tests plus the independent Python checker cover multiple pages, partial/empty results and input bounds. Test files and manifest/lock bytes must remain unchanged across every attempt.

## Acceptance and adversarial priorities

- Fresh build and independent test/checker completion, preserving `.rs` filename; each candidate has distinct build evidence.
- Struct, enum and Some/None inspection with documented exact toolchain behavior, bounded paging/depth and no arbitrary formatter claim.
- Explicit scalar mutation and changed output without source rebuild during the experiment.
- Stale/cross-frame/read-only-child handles cannot authorize mutation; changed symbols/module/source/formatter files fail before reads or writes.
- Missing/stale lockfile, unsupported dependency/build script/config, altered source/test/compiler during failed compilation, malformed Cargo artifacts and a forged success/missing test completion fail appropriately.
- Panic, timeout, adapter death, linker/test descendants and close leave no owned processes or temporary builds.
- Independent pinned Linux Rust CI lane, no model calls or network dependency resolution; measure cold setup and total time against the existing five-minute critical-path target. Local macOS evidence is reported separately.

## References

Cargo's [test command](https://doc.rust-lang.org/cargo/commands/cargo-test.html) documents explicit test selection and compile-only testing. Its [configuration rules](https://doc.rust-lang.org/cargo/reference/config.html) motivate controlling ancestry and environment. Rust's [codegen options](https://doc.rust-lang.org/rustc/codegen-options/index.html) define debug-info controls. The upstream [rust-lldb wrapper](https://github.com/rust-lang/rust/blob/main/src/etc/rust-lldb) and [lookup module](https://github.com/rust-lang/rust/blob/main/src/etc/lldb_lookup.py) show why formatter initialization needs a version-pinned dependency closure; `main` links are explanatory, not the proposed runtime pin.

## First-gate findings (macOS, 2026-10-14)

The engineering probe's Rust 1.96.0/Apple LLDB 21 pagination fixture now demonstrates a real source stop, module/symbol identity checks, raw struct/fieldless-enum inspection and root scalar intervention. Rust `i32` appears as DAP type `int` on this exact pair; mutation from 3 to 0 produced the expected first-page output. This is fixture/toolchain evidence, not general Rust runtime support.

macOS requires packed debug information for this build: Cargo puts the dSYM beside the hashed executable under `target/debug/deps`. Using split-debuginfo off followed by dsymutil lost application symbols because Rust's temporary codegen object had already been deleted. The successful probe launches the hashed executable with its adjacent dSYM and verifies both UUID and symbol path. Full line metadata was approximately 1.75 MiB, all DWARF4; the existing 2 MiB limit remains a material qualification constraint. Linux symbol behavior must be established independently.

Raw `Option<i32>` is not yet a usable worker-level semantic view. LLDB exposes both variant branches and a raw `$discr$` field; inactive payload fields can also appear. The Some payload path is `Option → $variants$ → $variant$1 → value → __0`, beyond the worker's current depth-four expansion limit. The engineering probe's deeper raw traversal does not qualify the public worker API. A pinned formatter or narrowly validated active-variant normalization remains a required next gate. Do not label inactive fields as a meaningful Some payload, increase generic depth without reviewing bounds, or interpret the observed layout as a stable Rust ABI.

An independent persistent scratch probe retained valid packed symbols and a verified source breakpoint but repeatedly timed out without a source stop. The timeout persisted with automatic symbol-file script loading disabled and after substituting the successful probe's fixture source; its cause remains unresolved. Adapter cleanup completed and recorded debuggee PIDs were gone. The separate clean-temporary-directory probe succeeded, so preserve this as a narrow setup qualification limitation rather than claiming the compiler/debugger pair always succeeds or that source-path resolution caused the timeout.
