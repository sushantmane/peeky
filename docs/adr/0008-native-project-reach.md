# ADR 0008: Native projects with their own builds, and Rust crates with dependencies

Status: proposed. Motivated by the value study ([#441](https://github.com/sushantmane/peeky/issues/441)). Phase 1 starts once the items under "Not yet verified" are checked.

## Context

The [value study](../../experiments/value-study/REPORT.md#why-peeky-wasnt-used-and-what-that-says) found 13 of its 29 bugs out of Peeky's reach. Seven are C and C++: jq (autotools), valkey twice and MicroPython (Make), fmt twice and nlohmann/json (CMake). Six are Rust crates with dependencies: tokio twice, axum, bat, ripgrep and coreutils.

**C and C++.** `NativeTarget.prepare` copies a declared list of sources to a fresh workspace. It runs each unit's `-E`, then `-c -MD`, with `FLAGS` (`-std=c11 -g -gdwarf-4 -O0 -fno-omit-frame-pointer`), and links them into `program`. A real project needs its generated headers, include paths, defines, libraries and link line. Its default build is optimized: valkey at `-O3` with LTO, fmt's Release, MicroPython's `-Os`. The proofs rest on three groups of facts:

1. **The binary's debug information and code:** `local_declarations`, `declared_on_line` with `runs_into`, `landing_pads`, prologue windows, `_function_entries`, static locals, the type graph. Any binary with readable debug information gives these. But `_LineParser` accepts DWARF 4 only, and about a dozen rules run only where `target.optimization == "O0"`, which Peeky knows because it chose the flag.
2. **The source text** (`c_source`): statement ends (`declared_running`, `declared_unfinished`), a static's scope, jumps past a declaration, structured bindings, setjmp lines and `optimizes`. Some rules read a unit as Peeky's own compile preprocesses it (`preprocess_argv`). Others read the raw source with every program header's macros (`macro_hazards`). Where either can't be read, these rules already withhold.
3. **The build process:** `-MD` lists every file a compile read (`external_inputs`). `c_refusal` rules out `.incbin`. `build.prepare` and `assert_current` hash inputs, tools and outputs. `-Wl,--build-id=sha1` identifies the loaded module.

**Rust.** `rust_manifest` refuses dependencies, a library, `build.rs`, workspaces and profiles. The build has a fresh `CARGO_HOME` and runs `--offline --locked`. More deeply, the MIR proofs assume the only other code is the standard library's ([Global state](../RUST_AGENT.md#global-state)):

- `_escapes` counts a call as keeping what it is given only for std's `thread::spawn` and the like (`_keeping_call`), or the crate's own functions. So `tokio::spawn(async move { … })` would keep nothing.
- `global_state` takes it that other code names none of the crate's statics. A dependency can keep a crate's trait object in its own state, as `log` keeps a logger.
- `unsafe_source` scans the crate's own text, but a dependency's macro can write `unsafe` code into it. `without_numbered_structs` and `_crate_traits` assume no procedural macros.
- "The standard library is taken as sound. Peeky's builds have no other dependencies."

**Time.** The study's builds took 0.3 to 1.4 minutes in its images (`build_minutes` in each `bug.yaml`). A probe's launch has one 40 s deadline, under the client's 45 s timeout.

## Options for C and C++

| | (a) Rebuild from `compile_commands.json` | (b) Run the project's build, debug flags forced | (c) Launch the project's binary, verified |
| --- | --- | --- | --- |
| Keeps | All three groups, in rebuilt units | As (c). Peeky also knows the build is fresh, and can rebuild an edit | Group 1, once Peeky reads DWARF 5 and recorded flags. Group 2 where a unit's command is known |
| Loses | The link, which the file lacks: Make keeps none Peeky could trust. Also sub-builds' libraries (valkey's `deps/`) and linker scripts. LTO objects don't mix with Peeky's | Nothing forces flags: Makefiles ignore `CFLAGS` or put `-O3` after it. So the binary must be checked as in (c) | Group 3, replaced by per-file checksums |
| Withheld | Little | As (c) | Units not built by clang at `-O0` with DWARF 5 and recorded flags. Text rules where a unit's command can't be re-run |
| Provenance | As today, but the link is Peeky's guess | `make`'s timestamps can keep a stale object | clang's DWARF 5 line tables hold each file's MD5. Checked before launch and every read: a stale binary is refused by file name |
| Build time | Every unit again, per probe unless cached | The project's build, past a probe's deadline: a separate step | None; symbol reading, to be measured |
| Security | Only the compiler, but with the project's flags: refuse those that load code (`-fplugin=`, `-Xclang -load`, `-B`, `-specs=`) | Runs the project's build code: the program's trust, from a trusted config only | The program, as today, and `clang -E` with allowlisted flags |

In each, a worker takes files, binaries and commands only from its own config, and the study's images are the tests. (c) is phase 1, and (b) phase 3. (a) isn't planned: its link would be guessed.

## Options for Rust

| | (a) Treat dependencies like std | (b) Analyse their MIR too | (c) Withhold what needs their code |
| --- | --- | --- | --- |
| Keeps | Every proof, by assumption | Proofs across calls into dependencies | LLDB's reads, the MIR's initialization dataflow, reference liveness, memory reads in crates without unsafe code |
| Loses | Soundness. Peeky's trust in std is knowledge of named functions (`_KEEPING_CALLS`, `_WRAPPERS`, `_MEASURES`, `_std_class`). Only its other half carries over: that the code is sound | Nothing in principle. But Peeky's analyses work within one crate, and many dependencies have unsafe code, which ends memory reads crate-wide | Values and changes whose proof crosses a dependency call |
| Withheld | Nothing, wrongly | Fewer | Each such value, with a note naming the call, its line and its crate |
| Provenance | As (c) | As (c), plus every crate's MIR hashed | Cargo.lock's checksums for dependencies; analysed sources hashed |
| Build time | Dependencies built once, then cached | A MIR dump of every crate (tokio's tree alone is a few dozen), every build | As (a); the analysed crates every build |

In (c), a call into a dependency counts as a body Peeky can't read. It may write through, keep or pass on anything it is given, touch any global state, run any crate code ever handed to a dependency, and never return. In each option, Cargo runs the project's and the dependencies' build scripts and procedural macros, as today's Rust builds never do. A worker builds only from its `--rust-config`, and the six Rust images are the tests. (c) is chosen; (a) would guess, and (b) costs much for little.

## Decision

Phase 1 debugs a binary the project built with clang at `-O0`, verified from its DWARF 5. Phase 2 adds Rust workspaces with dependencies, built by Peeky in a separate step, with dependency calls as unknown code. Phase 3 adds builds Peeky runs for C and C++, candidate edits and attach.

### Phase 1: a binary the project built (C and C++)

**The build** uses clang with `-O0 -g -gdwarf-5 -grecord-command-line`, and on Linux a build ID. The cookbook gives a recipe per build system: `./configure --disable-shared CC=clang CFLAGS='…'`, or CMake's `Debug` type with the flags added.

**Shape.** A config names `binary` instead of `sources`; with both, it is refused:

```json
{
  "language": "c",
  "binary": "jq",
  "arguments": [".a.[]", "input.json"],
  "compiler": "/usr/bin/clang-18",
  "adapter": "/usr/bin/lldb-dap-18",
  "dwarfdump": "/usr/bin/llvm-dwarfdump-18",
  "compile_commands": "compile_commands.json",
  "optimized": false
}
```

- `binary` is relative to the target directory, the root for source paths.
- `compiler` runs only `-E` and `-M`. Its `--version` must match the unit's `DW_AT_producer`.
- `compile_commands` is optional. Its entries must agree with the recorded commands.
- Without a config: `peeky probe . --binary ./jq --at src/execute.c:<line> --capture <name> -- '.a.[]' input.json`.
- `peeky describe . --binary ./jq` lists each unit as judged, or not and why: `modules/oniguruma/src/regcomp.c: built with -O2 (its recorded flags)`.

**Checks.** The binary must be an ELF or Mach-O executable with a build ID or UUID. On macOS, Peeky runs dsymutil into its own directory. A unit whose main file is under the root is judged when its producer is clang, its recorded flags give `-O0` and no `-flto`, and its DWARF 5 line table has an MD5 for every file. Each of those files must match its MD5 now. A function is judged by the unit whose code the binary keeps.

**Read and withheld.**

- **Judged units** follow today's rules. The session's `optimization` is the highest of any unit read, so every `-O0` rule stays exact.
- **A unit's text** comes from its recorded command line, run with the config's `compiler`, and `-M` lists the headers whose macros count. Only allowlisted flags pass: `-I`, `-D`, `-U`, `-include`, `-isystem`, `-std`, and `-f` and `-m` options that change predefined macros. Each file both the text and the line table name must match its MD5.
- **Where a unit's text can't be read,** its text rules withhold, saying why: "Peeky can't read src/acl.c as compiled: its command uses -fplugin=…". `NativeSession._resume` then proves nothing, as it needs every unit's `optimizes`.
- **Frames in units not judged** are `not shown`: "this frame runs src/lvm.c, built with -O2 (its recorded flags)". Breakpoints there are refused. With `"optimized": true`, they read as Peeky's optimized builds do: with `optimized` and its warning, and no change.
- **The program's own assembly** can return twice, as MicroPython's `nlr_push` (`py/nlraarch64.c`) does, but Peeky finds such calls by name (`_NONLOCAL_JUMPS`). So where a unit is assembly, or its text holds `asm` or can't be read, any call may return twice. Locals at or below the stop then carry today's setjmp note.
- **C frames at an address several functions share** are withheld, as C++'s are. Identical code folding is a link option, which no debug information records.

**Errors.**

- `UNSUPPORTED`: not an executable (a libtool wrapper script), no debug information, split DWARF, DWARF 4 ("records no checksum of each file: rebuild with -gdwarf-5"), no build ID, no unit judged, or a breakpoint in a unit not judged. Each names the file and the flag to add.
- `PROVENANCE_CHANGED`: a file differs from its recorded MD5, named, before launch; or a file or the binary changed during the run.
- `BUDGET_EXHAUSTED`: today's [symbol bounds](../NATIVE_INTERNALS.md#reading-a-builds-symbols). `INVALID_REQUEST`: `binary` outside the root, or with `sources`.

**The worker.** `--native-config` takes `binary`; a request can't name one. The files compared with the client's are those the judged units name under the root, within Python's bounds (4096 files, 16 MiB).

**Servers.** A probe takes `--until hits` and `--background` ([ADR 0006](0006-agent-speed.md)). So for valkey-1842, Peeky launches the replica, the agent starts a primary and sends `ACL LOAD`, and `peeky job wait` returns the hit.

**Docs:** `lang/c-cpp.md` and its cookbook, `NATIVE_INTERNALS.md`, `ERRORS.md`, `WORKER.md`, `REMOTE.md`, and one command per project type in `AGENT_GUIDE.md`.

**Acceptance.** First, every C and C++ test runs with Peeky's own builds at `-gdwarf-5`, as GCC's C++ was qualified. Then a Linux lane rebuilds the study's images (`experiments/value-study/bugs/*/Dockerfile`) with clang:

- **jq-2650:** `jq '.a.[]'`. Lines of `src/parser.y`, reached through bison's `#line`, are placed, and their values read or withheld with reasons.
- **valkey-1842:** the replica probe above reads the primary's client's `user` as a null pointer.
- **micropython-13569:** `py/compile.c`'s `compile_try_finally` shows the stack sizes it computes, with the `nlr_push` note below the stop.
- **fmt-3158, fmt-3272:** `ranges-test` and `xchar-test`, filtered, in template code in `include/fmt/`.
- **json-4237:** `test-udt_cpp11`, filtered, in `to_json.hpp`'s overload for enums.

Each also checks refusals: an edit without a rebuild, by name; the default optimized build, unless opted in; GCC and `-gdwarf-4` builds. Every value read must match the program's own print of it. Probe times and symbol sizes are recorded against `MAX_DIES`, `MAX_DEBUG_INFO` and `MAX_LINE_TEXT`.

### Phase 2: Rust workspaces with dependencies

**Shape.**

```json
{
  "workspace": ".",
  "analyse": ["tokio"],
  "run": {"package": "tokio", "test": "sync_broadcast"},
  "features": ["full"],
  "vendor": "vendor",
  "cargo": "…", "rustc": "…", "linker": "…", "adapter": "…", "dwarfdump": "…"
}
```

- `analyse`: the packages Peeky builds with its MIR wrapper and proves values in. Breakpoints go only in their files.
- `run`: `{"bin": "rg"}`, `{"package": "axum", "lib": true}` for unit tests, or a `test`. A test's filter follows `--`: `-- --exact <name> --test-threads=1`.
- `vendor`: from `cargo vendor`, passed as `--config` source replacement. The workspace's `.cargo/config.toml` is a hashed input; any above it stays refused.

**Build.** `peeky build . --config rust.json [--background]` runs `cargo build` or `cargo test --no-run`, `--offline --locked`, into a private target directory in Peeky's state directory.

- Dependency artifacts are reused. Each vendored package's checksum must equal Cargo.lock's, and Cargo checks each file against it.
- The analysed packages are compiled every time, as Peeky first removes their fingerprints. The wrapper stamps their MIR with the build's nonce, and records their rustc commands, so `opt-level=0` is checked whatever the profiles say. `--emit=llvm-ir` moves into the wrapper, for them only.
- Their dependency files' entries are hashed inputs, and `OUT_DIR` files hashed artifacts.
- A probe uses the build while its manifest holds (`assert_current`), and never builds: [ADR 0004](0004-stateless-probes.md)'s step 5 for these targets.

**Dependency calls** are unknown code, as in option (c). A note names one: "a call to `tokio::spawn` (dependency `tokio`) on line 41 is given a pointer into `x`; Peeky reads no dependency's code, so it may keep a copy".

- Dependencies are taken as sound, as std is (question 2). Otherwise every memory read in such a crate is withheld.
- Only the analysed crates' units are read, picked by name and `DW_AT_comp_dir` as `application_symbols` does now. Dependency generics built into them count against `MAX_DIES`.
- A dependency's frame has no MIR Peeky read, so none of its values is proven.

**Macros.** The source scans (`unsafe_source`, `without_numbered_structs`, `_crate_traits`, trusted names) read each analysed crate as rustc expands it (`-Zunpretty=expanded`). A glob `use` of a dependency makes bare standard names untrusted, as it shadows the prelude.

**Errors.** `BUILD_FAILED` as today. A new `BUILD_REQUIRED`: no current build, naming `peeky build`. `PROVENANCE_CHANGED`: an analysed source changed since the build, or a vendored checksum differs from Cargo.lock. `UNSUPPORTED`: a git dependency without vendored source, `[patch]`, or an analysed package outside editions 2021 and 2024.

**Trust and the worker.** Build scripts and procedural macros are the project's code, as the program is; `THREAT_MODEL.md` says so. A worker builds only from its `--rust-config`, as a job. It compares every `Cargo.toml`, `Cargo.lock` and the analysed sources with the client's.

**Docs:** `lang/rust.md` and its cookbook, `RUST_AGENT.md`, `ERRORS.md`, `THREAT_MODEL.md`, `REMOTE.md`, `WORKER.md`.

**Acceptance,** on the six images:

- **tokio-4867, tokio-6752:** a library's integration tests. tokio has unsafe code, so memory reads stay withheld with today's note; scalars read.
- **axum-1934:** unit tests of a crate that forbids unsafe code, so memory reads are exercised.
- **bat-3108:** a binary with `build.rs`; clap's types stay raw.
- **ripgrep-2209:** `rg -U --replace`, with the path dependency `grep-printer` analysed.
- **coreutils-6690:** the multicall binary, with `uu_cp` analysed.

Each also checks a change refused with a note naming a dependency call, and one allowed where only std is called. No value may contradict the program. Build times with a cold and a warm dependency cache, and analysed entries against `MAX_DIES`, are recorded.

### Phase 3, and later

- **C and C++ builds Peeky runs** (option (b)), from a trusted `build` command, as a job. They bring candidate edits and `verify` to project binaries. Built from clean in a fresh copy, they also bring GCC, whose binaries hold no checksums.
- **Native attach,** checked by build ID, for a server a test harness starts: valkey-790's cluster.
- **Shared libraries** the project builds, and reading symbols one unit at a time, if big binaries need it.
- **Rust:** candidate edits in workspaces, and MIR summaries of chosen dependency functions, if dependency calls block most changes.

## Not yet verified

- clang's recorded command line holds every `-D`, `-I` and `-include`, unambiguously: clang 18 and 20, and Apple clang (`DW_AT_APPLE_flags`).
- GCC 13 writes no MD5s into DWARF 5 line tables, as reported.
- The five C and C++ projects build this way, and fit the symbol bounds.
- `-Zunpretty=expanded` works through the wrapper on rustc 1.96, and a warm workspace build fits an agent's wait.

## Consequences

- 12 of the 13 out-of-reach bugs become probeable; valkey-790 waits for attach.
- Peeky's claim changes, from "the program Peeky built from these files" to "the binary whose debug information matches these files". Line tables don't cover a file an `.incbin` reads, a header no unit names, link options or libraries loaded at run time. The docs say so.
- Agents must rebuild with Peeky's flags first. Every refusal names them, and `describe` says which units fall short.
- Phase 1 runs no project code but the program. Phase 2 runs build scripts and macros, from trusted configs only.

## Open questions for the owner

1. GCC binaries: refuse them in phase 1 for lack of checksums, or read them with a provenance note on every row?
2. Take dependencies as sound, as std is? Without that, no memory read is possible in a crate with dependencies.
3. Is a separate `peeky build` acceptable, or must a probe build within its deadline?
4. Should optimized project units be readable by opt-in in phase 1, or refused?
5. Is `BUILD_REQUIRED` worth a new code and a protocol bump?
6. Dependency sources: `cargo vendor` only, or also the user's Cargo cache, which brings its configuration?
7. May a config name functions that return twice (`"returns_twice": ["nlr_push"]`), narrowing the note? It is a trusted claim.
8. The project lanes need the study's images and minutes each: nightly, or on every PR?
