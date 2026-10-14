# Rust foundation design review

Date: 2026-10-14. Status: proposed production gates, not qualified Rust support. This independent AI-assisted review reads the Rust plan, existing build/worker boundaries and official Cargo documentation. The raw DAP probe and hosted compatibility lane are separate evidence and must be evaluated before a RustTarget is implemented or advertised.

## Proposed minimum build boundary

Use a dedicated fixed RustTarget for one dependency-free package, one named binary and explicitly named integration-test targets. TOML validation should accept a documented small schema rather than blacklist a few keys. Freeze Cargo.toml, Cargo.lock, all declared application sources and test files; only designated application .rs files are candidate-editable. Disable automatic target discovery and build scripts. Reject dependencies in every form, workspace inheritance, custom profiles, features outside the supported subset, arbitrary target specifications and executable hooks. Unsupported configuration is a fatal setup error, not model repair feedback. Cargo's [manifest reference](https://doc.rust-lang.org/cargo/reference/manifest.html) describes these independent configuration surfaces.

The actual compiler and linker are part of the trusted toolchain. Resolve a configured versioned Cargo/rustc pair, record executable hashes and version/host/commit output, and pass the actual rustc and linker paths explicitly. A rustup dispatcher hash does not identify the selected compiler. Prefer a controlled minimal PATH and an environment constructed from approved keys; remove ambient Rust/Cargo wrappers, encoded/plain flags, target/runner/profile variables and dynamic-loader/Python overrides. Use private HOME, CARGO_HOME, target and temporary directories. An explicit offline, locked invocation should fail on missing or stale lock metadata instead of repairing it.

## Cargo ancestry is a required gate

Cargo searches the invocation directory and its parents for both configuration filename variants; configuration arrays merge, and command-line overrides merge rather than disabling discovery. Fresh CARGO_HOME therefore does not isolate configuration. These are documented [Cargo configuration rules](https://doc.rust-lang.org/cargo/reference/config.html).

Proposed conservative implementation: stage outside the repository in an owned temporary directory, resolve its actual path, and reject either .cargo/config or .cargo/config.toml anywhere from the Cargo working directory through the filesystem root before invoking Cargo. Check both names even when one takes precedence. Reject symlinked staging paths/config entries and unexpected ancestor Cargo manifests that could participate in workspace discovery. Do not copy project .cargo content. Recheck the ancestry condition and owned configuration identity before every Cargo stage. A fresh empty home plus these checks is a practical trusted-code guard; it is not protection against an attacker racing filesystem changes. Do not delete or modify a user's ancestor configuration to make a build pass.

Acceptance tests should place sentinel wrappers/runners/forced environment values in an ancestor config, in CARGO_HOME, and in ambient environment variables. Establish a positive control that the sentinel would run under ordinary Cargo, then require the controlled path to reject the ancestor or avoid the ambient override before any compiler/test runs. Also test the extensionless config filename, symlink ancestors and conflicting encoded flags. This gate must apply to metadata/probes that invoke Cargo as well as build/test commands.

## Compile and test completion

Keep compilation and test execution as separate bounded stages. Use the fixed named binary build and named test compile-only command. Cargo's [test command](https://doc.rust-lang.org/cargo/commands/cargo-test.html) supports no-run test compilation and target selection; it also builds selected targets before executing tests. A second cargo test invocation is therefore not inherently proof that an earlier hashed artifact was the executable tested.

Prefer identifying the libtest executables from bounded Cargo JSON, hashing them and running those exact paths with fixed harness arguments. Require the ordinary test harness, explicit expected test targets, no filters and a controlled documented runtime environment. Qualify this direct-run behavior with the fixture first. Alternatively, retain cargo test only with explicit detection and rejection of any recompile or artifact substitution. Do not simply reuse the success bit from the compile stage as test success. A failing assertion is verification failure; compiler diagnostics are build feedback; infrastructure/provenance failures remain fatal.

Cargo [JSON messages](https://doc.rust-lang.org/cargo/reference/external-tools.html) expose compiler-artifact and build-finished records. Validate complete bounded records, target/package identity, executable uniqueness and paths confined to the owned target directory. Reject escaping paths, duplicate unexpected artifacts, absent completion and malformed/truncated output. Hash the binary, selected tests and separate symbol artifacts; retain source/tool identities even after failed compilation, using the existing failed-build provenance guard. The trusted independent Python checker remains required after Cargo tests; no Cargo-only success bypass is proposed.

Apply wall-clock/output limits and owned process-group cleanup to Cargo, rustc/linker, test executables, checker and debugger. Test spawned descendants, panic, timeout and cancellation. Record per-stage results separately so a model cannot treat a test assertion failure as a successful build-and-check result.

## Symbol and debugger provenance

Do not assume application DWARF flags rewrite linked standard-library debug data. Measure the actual fixture's line-dump size and versions under each pinned toolchain. Keep declared-source path/line resolution, ELF build ID or Mach-O UUID, symbol-file path/hash and stopped-module evidence. A large Rust dump must cause an explicit failure or use a separately reviewed bounded extraction method; silent truncation cannot support line provenance.

Use raw fields for the first qualification. Rust formatter scripts execute inside the debugger; if needed later, pin and hash their complete import closure, isolate Python search paths, disable bytecode writes, and retain disabled ambient initialization. Record raw versus formatter-assisted observations. Exact reported Rust scalar types and Some/None/enum shapes must be captured before permitting mutation or claiming container support. C integer type rules are not Rust qualification.

## Evidence status and next gate

The existing C/C++ worker, bounded child handles and compiled-verification tests establish reusable mechanisms only. This review has not executed a Rust build or debugger session and grants no Rust runtime approval. Required evidence remains the pinned compiler/LLVM pair's raw source breakpoint, module/symbol identity, struct/enum/Some/None shapes, exact scalar mutation, continued output and process cleanup; then the ancestry isolation and compile/test failure regressions above. Hosted Linux and local macOS results must be reported independently.

## Preflight helper implementation review

The subsequently added rust_manifest helper was independently inspected as a preflight component only. Its explicit TOML/lockfile schema excludes dependency tables, automatic targets, build scripts and test harness overrides; declared files are confined and reject symlinks. Ancestor checks cover both configuration filenames, dangling config links and ancestor Cargo manifests. The helper resolves its input directory; ownership and creation of the staging directory and isolated CARGO_HOME remain preparation responsibilities.

One concrete gap was found and fixed: the package-version pattern accepted numeric components larger than the [SemVer implementation's u64 fields](https://docs.rs/semver/latest/semver/struct.Version.html). Validation now bounds both total version length and each component. An independent test file adds overflow, target execution-hook, nested-source symlink and ancestor configuration regressions. `tests/unit/test_rust_manifest.py` plus `tests/unit/test_rust_manifest_review.py`: **45 passed in 0.05 seconds**; Ruff check passed for the independent test file. These tests establish preflight rejection behavior, not full Cargo environmental isolation.
