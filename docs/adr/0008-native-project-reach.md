# ADR 0008: Native projects with their own builds, and Rust crates with dependencies

Status: accepted, with the owner's decisions below. Motivated by the value study ([#441](https://github.com/sushantmane/peeky/issues/441)). Phase 1's checks, and the owner's decisions on them, are under "Verified for phase 1".

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

**The build** uses clang with `-O0 -g -gdwarf-5 -grecord-command-line`, and on Linux `-fdebug-macro` and a build ID. Generated sources come without `#line` (`bison -l`, `flex -L`). C++ uses libc++ with `-D_LIBCPP_HAS_NO_PRAGMA_SYSTEM_HEADER`. The cookbook gives a recipe per build system: `./configure --disable-shared CC=clang CFLAGS='…'`, or CMake's `Debug` type with the flags added.

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

**Checks.** The binary must be an ELF or Mach-O executable with a build ID or UUID. On macOS, Peeky runs dsymutil into its own directory. A unit whose main file is under the root is judged when its producer is clang, its recorded flags give `-O0` and no `-flto`, and its DWARF 5 line table names the unit as file 0 and has an MD5 for every file. Each of those files must match its MD5 now. A unit whose table has no MD5s is refused with what the table shows: "its line table records no MD5s", and where it names `<stdin>`, "… and names `<stdin>`, which clang adds for an inheriting constructor". The likely remedies follow, as likely, not proven: `bison -l` and `flex -L` for a generator's `#line`, and `-D_LIBCPP_HAS_NO_PRAGMA_SYSTEM_HEADER` for libc++'s headers. The table can't tell these causes apart; the phase 3 wrapper can. A function is judged by the unit whose code the binary keeps.

The `-O` level and LTO are read from the driver's own `-###` answer for the recorded words, run with the config's `compiler` in the unit's compilation directory. Where it gives none, the words are read as the driver would, and refused where an `-O` or LTO word may be another option's value. That reading alone doesn't judge a unit: options that change what code computes (`-ffast-math`, `-ffinite-math-only`, `-fno-honor-nans`, `-fno-honor-infinities`, `-funsafe-math-optimizations`, `-fassociative-math`, `-freciprocal-math`, `-ffp-contract=fast`) are refused too, unless Peeky's proofs are shown to hold under them. Whether `-fwrapv` and `-fno-strict-overflow` may stay is decided with binary targets.

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

- **jq-2650:** `jq '.a.[]'`, with the grammar generated by `bison -l`: its actions are probed in `src/parser.c`, their values read or withheld with reasons. A breakpoint in `src/parser.y` is refused, as no judged unit's code is there. Built with bison's `#line` instead, `src/parser.c`'s table records no MD5s: its breakpoints are refused saying so, with `bison -l` named as the likely remedy.
- **valkey-1842:** the replica probe above reads the primary's client's `user` as a null pointer.
- **micropython-13569:** `py/compile.c`'s `compile_try_finally` shows the stack sizes it computes, with the `nlr_push` note below the stop.
- **fmt-3158, fmt-3272:** `ranges-test` and `xchar-test`, filtered. Their test units' line tables record no MD5s and name `<stdin>`, so a probe in them, or in `include/fmt/`'s template code they instantiate, is refused saying so. Units with MD5s (`test-main.cc`) are judged and read.
- **json-4237:** `test-udt_cpp11`, filtered: both units' tables record no MD5s and name `<stdin>`, so a probe in `to_json.hpp`'s overload for enums is refused, saying so.

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

- **A Peeky compiler wrapper,** first: `CC="peeky cc"` and `CXX="peeky c++"`, as ccache is used. At build time it records each input's path and hash, and the exact arguments, in a file kept by unit and build ID. It brings into reach what phase 1 refuses: C++ units without MD5s (fmt, nlohmann/json), GCC's binaries, bison's and flex's `#line` output, and headers of macros alone.
- **C and C++ builds Peeky runs** (option (b)), from a trusted `build` command, as a job. They bring candidate edits and `verify` to project binaries. Built from clean in a fresh copy, they also bring GCC, whose binaries hold no checksums.
- **Native attach,** checked by build ID, for a server a test harness starts: valkey-790's cluster.
- **Shared libraries** the project builds, and reading symbols one unit at a time, if big binaries need it.
- **Rust:** candidate edits in workspaces, and MIR summaries of chosen dependency functions, if dependency calls block most changes.

## Verified for phase 1

Checked in October 2026. Each claim says how. **Tested**: `tests/native/test_native_units.py` checks it in the native lanes, with clang 18, GCC 13 and llvm-dwarfdump 18 on Linux, and Apple clang 21 on macOS. The same tests passed by hand with llvm-dwarfdump 20. **By hand**: seen once on the toolchains named, with no test kept. `native_units` reads these facts, and `tests/projects/` rebuilds and measures the study's projects.

**The recorded command line is complete and exact.**

- clang records every `-D`, `-U`, `-I`, `-isystem`, `-include` and `-std` as given (tested; clang 20.1.2 by hand).
- Each argument is one word. The driver writes `-Iinc` as `-I inc`, and escapes a space or backslash with a backslash. So a define holding a space, quote or tab reads back exactly (tested).
- Linux clang appends the words to `DW_AT_producer`, after the line `--version` prints. Apple's writes them to `DW_AT_APPLE_flags` (tested), and adds its own `-Xclang -clang-vendor-feature=…` options. Each of Apple clang 21's is known by name (tested).
- Response files, `--config` files and `CCC_OVERRIDE_OPTIONS` are recorded as the arguments they add. `CPATH` and `C_INCLUDE_PATH` are not recorded (tested).
- `-Wp,-D…`, `-Xpreprocessor` and `-Xclang -D…` are recorded as written, so the allowlist refuses them (tested).
- The driver records an alias as what it means: `--optimize=2` as `-O2`, `-O` as `-O1` (by hand, Apple clang 21). `native_units.optimization` takes the last `-O` and LTO option from the driver's own `-###` answer, which needs the unit's inputs where it was compiled (tested). Without it, an `-O` or LTO word after an option not known to take no value is refused: clang reads `-O2 -fmodules-user-build-path -O0` as `-O2` (tested). Both refuse any `-mllvm` option, and any `-Xclang` one but warnings, `-O`, LTO and the exact options Apple clang 21 adds.
- GCC 13 records only code generation options: no `-D`, `-I` or `-include` (tested).

**Checksums.**

- GCC 13 writes no MD5s into DWARF 5 line tables, with or without the assembler's line support (tested). Decision 1 stands.
- clang's table holds an MD5 for every file, or for none: one file without one drops them all. In clang 18, 20 and Apple's, three things leave a file without one:
  - a `#line` naming a file, as bison and flex write (tested). jq's `src/parser.c` and `src/lexer.c` have them. Generated with `bison -l` and `flex -L`, they compile with MD5s (by hand, bison 3.8.2 and flex 2.6.4 in jq's image).
  - `#pragma GCC system_header` (tested), in every libstdc++ and libc++ header. libc++ leaves it out under `-D_LIBCPP_HAS_NO_PRAGMA_SYSTEM_HEADER`; GCC 13's libstdc++ can't (by hand, libc++ 18 on Ubuntu and Apple's SDK).
  - a C++ inheriting constructor (`using Base::Base;`), as fmt's `format_error` has (tested). clang then names a file `<stdin>` with no MD5.
- dsymutil keeps the MD5s and `DW_AT_APPLE_flags` (tested).
- A DWARF 5 table's directory 0 is the compilation directory, and its file 0 the unit's own file. With `-fdebug-compilation-dir=rel`, both are relative, so no file has a path on this machine (tested). A table not naming its unit as file 0 isn't judged.
- A table names a header only if code or a type comes from it. A header of macros alone (`config.h`) is named only with `-fdebug-macro` (tested on clang 18; clang 20 by hand). Apple clang 21 crashes with `-fdebug-macro -gdwarf-5` (by hand).

**The study's projects** were rebuilt in their images with clang 18 at `-O0 -g -gdwarf-5 -grecord-command-line -fdebug-macro`, and judged by `tests/projects/measure.py`. The C++ ones use libc++ with the pragma left out; with libstdc++, none of their units has MD5s. Each build took under 25 s on 4 CPUs. Units are counted one by one, by offset: valkey-server's 173 have 172 names, as hiredis and valkey each have a `sds.c`. (The image's own GCC build, with LTO, has 209, 36 of them `<artificial>`.)

| Binary | Units judged | Not judged | Line tables | Debug information | Entries | Kept |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| jq-2650 `jq` | 34 of 36 | `parser.c`, `lexer.c`: no MD5s (bison's and flex's `#line`, by hand) | 5.7 MB | 5.4 MB | 21,000 | 22 MB |
| valkey-1842 `valkey-server` | 106 of 173 | 65 of hiredis and jemalloc: no recorded command; 2 at `-Os` | 20.7 MB | 57.6 MB | 217,000 | 205 MB |
| micropython-13569 `micropython` | 215 of 218 | `py/vm.c`, `py/gc.c` at `-O3`; `py/nlraarch64.c` at `-Os` | 23.1 MB | 26.5 MB | 121,000 | 110 MB |
| fmt-3158 `ranges-test` | 1 of 5 (`test-main.cc`) | 4: no MD5s, `<stdin>` named | 8.4 MB | 37.7 MB | 122,000 | 120 MB |
| fmt-3272 `xchar-test` | 3 of 6 | 3, `xchar-test.cc` among them: no MD5s, `<stdin>` named | 11.3 MB | 45.3 MB | 146,000 | 143 MB |
| json-4237 `test-udt_cpp11` | 0 of 2 | both: no MD5s, `<stdin>` named | 5.5 MB | 36.8 MB | 93,000 | 93 MB |

The driver's own `-###` read every recorded command's optimization. All fit the bounds: 64 MiB of line tables, 256 MiB of debug information, 400,000 entries, 300 MiB kept. valkey keeps the most, 68% of `MAX_KEPT`. `-fdebug-macro` adds 2 MB to its line tables.

**The owner's decisions on these findings.**

- C: the recipes add `bison -l` and `flex -L`, and on Linux `-fdebug-macro`. jq's grammar is probed in `src/parser.c`, and a breakpoint in `src/parser.y` is refused. On macOS, a header of macros alone can't be checked, so the text rules withhold there.
- C++: a unit with an MD5 for every file is judged, as libc++ with `-D_LIBCPP_HAS_NO_PRAGMA_SYSTEM_HEADER` and no inheriting constructor gives. Any other is refused with what its table shows, and the likely remedies as likely. `-gembed-source` isn't used: it embeds most files' text but not all, and made a test binary six times larger.
- Phase 3 starts with a compiler wrapper that records each input's hash, which brings fmt, nlohmann/json, GCC, bison's output and headers of macros alone into reach.

## Not yet verified

- `-Zunpretty=expanded` works through the wrapper on rustc 1.96, and a warm workspace build fits an agent's wait.

## Consequences

- 12 of the 13 out-of-reach bugs become probeable; valkey-790 waits for attach.
- Peeky's claim changes, from "the program Peeky built from these files" to "the binary whose debug information matches these files". Line tables don't cover a file an `.incbin` reads, a header no unit names, link options or libraries loaded at run time. The docs say so.
- Agents must rebuild with Peeky's flags first. Every refusal names them, and `describe` says which units fall short.
- Phase 1 runs no project code but the program. Phase 2 runs build scripts and macros, from trusted configs only.

## Decisions on the open questions

1. **GCC binaries:** refused in phase 1, with the flags to rebuild with clang. If the check under "Not yet verified" finds that GCC writes the checksums after all, GCC binaries are read like clang's.
2. **Dependencies are taken as sound, as std is.** The docs say so: a value can be wrong only if a dependency's own unsafe code breaks Rust's rules.
3. **A separate `peeky build`, plus `--build` on probes and sessions.** `--build` runs the same build first, with the build's own deadline, so one command still works. Without either, a probe never builds.
4. **Optimized units: by opt-in** (`"optimized": true`), read with today's `optimized` warning, as Peeky's own optimized builds are.
5. **`BUILD_REQUIRED`: yes.** It is a new code and a protocol bump, since an agent acts on it differently from other refusals.
6. **Dependency sources: `cargo vendor` in phase 2.** The refusal of a missing vendor directory gives the exact command. The user's own Cargo cache can follow later.
7. **`returns_twice`: yes**, as a trusted config's claim, documented as one. The broad note stays the default.
8. **Project lanes run nightly and on demand,** not on every PR. Every PR keeps small fixtures of each new shape.

## Open questions for the owner (answered above)

1. GCC binaries: refuse them in phase 1 for lack of checksums, or read them with a provenance note on every row?
2. Take dependencies as sound, as std is? Without that, no memory read is possible in a crate with dependencies.
3. Is a separate `peeky build` acceptable, or must a probe build within its deadline?
4. Should optimized project units be readable by opt-in in phase 1, or refused?
5. Is `BUILD_REQUIRED` worth a new code and a protocol bump?
6. Dependency sources: `cargo vendor` only, or also the user's Cargo cache, which brings its configuration?
7. May a config name functions that return twice (`"returns_twice": ["nlr_push"]`), narrowing the note? It is a trusted claim.
8. The project lanes need the study's images and minutes each: nightly, or on every PR?
