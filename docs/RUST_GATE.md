# Rust engineering qualification

This slice adds a dependency-free Cargo fixture, strict input preflight and a bounded raw-DAP compatibility probe. It does **not** add RustTarget, HTTP Rust workers, source-repair verification, Cargo test execution or general Rust support.

The fixture uses Rust/Cargo 1.96.0, edition2021, a request struct, mode enum, Some/None and a mutable i32 pagination offset. The probe builds a fresh copy offline, verifies the actual stopped source and loaded native module/symbol identity, inspects raw values, changes the scalar and checks changed output without rebuilding. It records tool/input hashes, bounded events and cleanup. No Rust Python formatters or project initialization scripts are loaded.

Local Apple LLVM21 qualification passed: the i32 local is displayed as `int`; struct fields and enum variant are readable. Raw Option storage includes a discriminant and union storage; the inactive None payload is not interpreted. The Some payload is deeper than the current worker expansion depth limit, so raw-DAP success does not yet qualify Option inspection through the worker. This is a narrow engineering observation, not a portable Rust formatter contract. macOS requires Cargo's packed dSYM and corresponding hashed binary under target/debug/deps; split-debug-info off discarded object files required for application symbols in this experiment. Linux LLVM18 is a separate CI gate.

The manifest validator accepts only one explicit binary and one immutable integration-test target, with autodiscovery/build scripts disabled, no dependencies/workspaces/profiles and a matching version4 lockfile. A separate ancestry check rejects implicit Cargo configuration and parent manifests. Production preparation must still combine these checks with a fresh CARGO_HOME, controlled environment, executable/artifact identities and repeated integrity checks; preflight alone is not isolation.

Run the optional probe using absolute real versioned cargo/rustc executables and explicit LLDB, dwarfdump and linker paths through PEEKY_RUST_* environment variables, then `pytest tests/rust`. The script accepts the same paths as named arguments. CI resolves tools using `rustup which --toolchain 1.96.0` and tests with LLVM18. The existing Python/Java/C/C++ lanes remain independent. No model calls run in required CI.

Next: implement the reviewed Rust runtime and compiled-test verification boundary in RUST_PLAN.md, including metadata/provenance failure tests and exact artifact test execution. The intentionally failing pagination Cargo test is an input for that future verification stage; it is not executed or claimed passing by this probe.

## Hosted gate result, 2026-10-14

PR #22 merged as4bb493d after all nine lanes passed (run38556716292, source770c60a). Linux Rust1.96.0/LLVM18 raw probe passed in2.03 seconds (pytest2.19s), full Rust job41s; longest job72s. Actual line dump4,808,912 bytes, all reported DWARF4, declared source present, start displayed as int; loaded-module validation and build cleanup passed. Production line parsing remains limited to2MiB and cannot yet accept this dump. Choose a separately reviewed bounded declared-source extraction or explicit Rust-specific parser budget before adding RustTarget; do not silently change the C/C++ default. Raw Option semantic/depth gaps remain as documented.
