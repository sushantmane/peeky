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
  "optimized": false
}
```

- `binary` is relative to the target directory, the root for source paths.
- `compiler` runs only `--version`, and the replay of a unit's `-E` and `-M`. Its `--version` must match the unit's `DW_AT_producer`. The replay passes only allowlisted flags (below), with `--no-default-config`. Its environment drops `CCC_*`, `CL`, `_CL_`, `CPATH`, `C_INCLUDE_PATH`, `CPLUS_INCLUDE_PATH`, `OBJC_INCLUDE_PATH` and `SDKROOT`, unless the unit's recorded words hold them. It is never run on a unit's recorded words as they are, not even with `-###`: those can make it run another program or read other files.
- `compile_commands.json` is not read. The words each unit records are the truth of how it was built, and a second account could only disagree with them. A config naming `compile_commands` is refused, as any unknown key is.
- Without a config: `peeky probe . --binary ./jq --at src/execute.c:<line> --capture <name> -- '.a.[]' input.json`.
- `peeky describe . --binary ./jq` lists each unit as judged, or not and why: `modules/oniguruma/src/regcomp.c: built with -O2 (its recorded flags)`.

**Checks.** The binary must be an ELF or Mach-O executable with a build ID or UUID. On macOS, Peeky runs dsymutil into its own directory. A unit whose main file is under the root is judged when its producer is clang, its recorded flags give `-O0` and no `-flto`, and its DWARF 5 line table names the unit as file 0 and has an MD5 for every file. Each of those files must match its MD5 now. A unit whose table has no MD5s is refused with what the table shows: "its line table records no MD5s", and where it names `<stdin>`, "… and names `<stdin>`, which clang adds for an inheriting constructor". The likely remedies follow, as likely, not proven: `bison -l` and `flex -L` for a generator's `#line`, and `-D_LIBCPP_HAS_NO_PRAGMA_SYSTEM_HEADER` for libc++'s headers. The table can't tell these causes apart; the phase 3 wrapper can. A function is judged by the unit whose code the binary keeps.

The `-O` level and LTO are read from the recorded words alone (`native_units.optimization`). Each option's number of values must be known: none, or one. After an option whose number isn't known, any `-O` or LTO word may be its value, however many words come between, so the unit isn't judged. Apple's clang builds `-O2 -fdepscan-prefix-map /old -O0` at -O2, as clang builds `-O2 -resource-dir -O0`. So is one passing options with `-mllvm`, an unknown `-Xclang` or `-Xarch_…`, or naming a response or configuration file. That reading alone doesn't judge a unit: options that change what code computes (`-ffast-math`, `-ffinite-math-only`, `-fno-honor-nans`, `-fno-honor-infinities`, `-funsafe-math-optimizations`, `-fassociative-math`, `-freciprocal-math`, `-ffp-contract=fast`) are refused too, unless Peeky's proofs are shown to hold under them. So are the options implying some of them (`-ffp-model=fast` or `aggressive`, `-fapprox-func`, `-fno-signed-zeros`, `-mrecip`, denormals flushed), and sanitizers (`-fsanitize=…`), whose instrumentation Peeky's proofs aren't checked under. On macOS, the dSYM is bound to the binary by the linker's timestamps, as LLDB and dsymutil bind them. Each object the debug map names must have the time it records, in seconds, and be no newer than the binary. That catches an object rebuilt after the link, even in the same second, where the filesystem keeps parts of a second. Where it keeps whole seconds, an object written in the binary's second is refused: which came first can't be told. It doesn't catch timestamps forged on purpose, which, like other tampering by the same user, is outside the threat model. Phase 3's wrapper gives a binding by hash. `-fwrapv` and `-fno-strict-overflow` stay: they define what signed overflow does, and no rule of Peeky's assumes it undefined.

**Read and withheld.**

- **Judged units** follow today's rules. The session's `optimization` is the highest of any unit read, so every `-O0` rule stays exact.
- **A unit's text** comes from its recorded command line, run with the config's `compiler`, and `-M` lists the headers whose macros count. Only allowlisted flags pass: those choosing what the preprocessor reads or defines (`-I`, `-D`, `-U`, `-include`, `-isystem`, `-isysroot`, `-std`, `-O`, `-x`, `--target`, and `-f` and `-m` options that change predefined macros). Options that define nothing are left out, each checked against clang's own `-dM -E`. Any other leaves the unit's text unread, naming it. Each file both the text and the line table name must match its MD5: every file the replay reads must be one the line table names. `-march=native` and its kin leave the text unread: they name the build host's CPU. So do units built from one file with different commands, as Peeky reads a unit's text by its file's name.
- **A probe** (`__has_include`, `__has_include_next`) may have been answered otherwise at the build: its environment may have added a header path, or a file may since be gone. One that finds a file now is proven: clang lists the file among the replay's dependencies, which the line table must name, so the build found it too. One that finds none leaves the unit's text unread, naming it: "its preprocessing asks whether <stdatomic.h> exists (__has_include_next, in /usr/lib/llvm-18/lib/clang/18/include/stdatomic.h:22), and finds none": clang's own header asks for the system's, which glibc lacks. Apple's headers ask for private ones. So `#embed` leaves the text unread, too, and a recorded `-D` that names a probe or may paste one together (`#`, `##`, or any three characters of a probe's name). A probe the source itself pastes together from pieces is outside what Peeky checks, as reserved names are: it needs a deliberate construction, and a file whose presence changed since the build.
- **Where a unit's text can't be read,** its text rules withhold, saying why: "Peeky can't read src/acl.c as compiled: its command uses -fplugin=…". `NativeSession._resume` then proves nothing, as it needs every unit's `optimizes`.
- **Frames in units not judged** are `not shown`: "this frame runs src/lvm.c, built with -O2 (its recorded flags)". Breakpoints there are refused. With `"optimized": true`, they read as Peeky's optimized builds do: with `optimized` and its warning, and no change.
- **The program's own assembly** can return twice, as MicroPython's `nlr_push` (`py/nlraarch64.c`) does, but Peeky finds such calls by name (`_NONLOCAL_JUMPS`). So where a unit is assembly, or its text holds `asm` or can't be read, any call may return twice. The whole text counts, headers outside the program too. An asm label counts where it gives a symbol of the returns-twice family; glibc's and Apple's headers label hundreds of declarations with others. The family is one set everywhere: setjmp's, `savectx`, `vfork`, `getcontext` and `swapcontext`. So it may where the binary's own code holds what no unit covers: an assembly file built without `-g` leaves no unit. Peeky reads the binary's symbol table (`native_binary.code_symbols`). A function symbol outside every unit's ranges, or code with no symbol between them, counts, named in the note: "nlr_push is code no unit's debug information covers (assembly built without -g?)". Only the toolchain's startup code and the linker's stubs are left out, each checked on clang 18 and Apple's clang 21 builds. By name only where C and C++ reserve the name for the implementation: `_start`, `_init`, `_fini`, `__libc_csu_*`, AArch64's `__aarch64_*` atomics. A program defining such a name is outside what Peeky checks. Other startup names count unless their bytes are the toolchain's: `frame_dummy` must be crtbegin.o's, byte for byte, but each relocation's immediate field, which must point where the object says: at the binary's symbol of that name, its GOT slot or PLT stub, or into the matched code. So must `.init`'s and `.fini`'s head and tail be crti.o's and crtn.o's; anything else there counts. The compiler finds those objects (`-print-file-name`). A binary with no symbol table counts too. Locals at or below the stop then carry today's setjmp note. A config's `returns_twice` replaces all of this with its trusted claim.
- **C frames at an address several functions share** are withheld, as C++'s are. Identical code folding is a link option, which no debug information records.

**Errors.**

- `UNSUPPORTED`: not an executable (a libtool wrapper script), no debug information, split DWARF, DWARF 4 ("records no checksum of each file: rebuild with -gdwarf-5"), no build ID, no unit judged, or a breakpoint in a unit not judged. Each names the file and the flag to add.
- `PROVENANCE_CHANGED`: a file differs from its recorded MD5, named, before launch; or a file or the binary changed during the run.
- `BUDGET_EXHAUSTED`: today's [symbol bounds](../NATIVE_INTERNALS.md#reading-a-builds-symbols). `INVALID_REQUEST`: `binary` outside the root, or with `sources`.

**The worker.** `--native-config` takes `binary`; a request can't name one. The files compared with the client's are those the judged units name under the root, within Python's bounds (4096 files, 16 MiB). A client asks for them (`inputs`; `version` stays cheap). The worker lists them once per binary, one listing at a time, kept by the binary's size, time and inode. It counts them against the bounds before reading any, and hashes no file: on macOS, the debug map's objects are checked by their stat identities. It compares them once its preparation has judged the units, before the debugger starts. A client names the binary with `--binary`, and is refused if the worker's is another. Sessions run on a binary, locally and through the worker.

**Shared libraries.** A frame in a shared library the binary loads is never read, even one built from sources under the root: Peeky reads only the binary's own units. It is `UNSUPPORTED`, naming the library, and the session is kept. On macOS the frame's `moduleId` names its module; on Linux, where LLVM 18's lldb-dap gives none, the one module whose loaded executable segments hold the frame's address.

**Servers.** A probe takes `--until hits` and `--background` ([ADR 0006](0006-agent-speed.md)). So for valkey-1842, Peeky launches the replica, the agent starts a primary and sends `ACL LOAD`, and `peeky job wait` returns the hit.

**Docs:** `lang/c-cpp.md` and its cookbook, `NATIVE_INTERNALS.md`, `ERRORS.md`, `WORKER.md`, `REMOTE.md`, and one command per project type in `AGENT_GUIDE.md`.

**Follow-ups, after the acceptance PR.** A probe that finds no file withholds its unit's text: the build may have found one. With `-fdebug-macro`, the build's own record can prove it found none. Every file the line table names must be one the replay read, and the macros the build recorded must be the replay's. That brings clang's `stdatomic.h`, which asks for the system's, back into reach. A probe that changes only code, with no include or macro, stays unproven (owner's decision on #460).

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
- `vendor`: from `cargo vendor`, passed as `--config` source replacement. The workspace's `.cargo/config.toml` is a hashed input. Configuration above it, `~/.cargo/config.toml` included, is accepted under the same allowlist; each file's path and SHA-256 are recorded, so a change is `PROVENANCE_CHANGED`. A manifest above the workspace stays refused.

**Build.** `peeky build . --config rust.json [--background]` runs `cargo build` or `cargo test --no-run`, `--offline --locked`, into a private target directory in Peeky's state directory.

- Dependency artifacts are reused. Each vendored copy's `.cargo-checksum.json` must give Cargo.lock's checksum and list every file of the copy, none missing and none more; Peeky hashes each file and compares. This proves the copy is what `cargo vendor` wrote, unless its checksum file was edited to match. It doesn't authenticate the crate against crates.io beyond Cargo.lock's checksum.
- The analysed packages are compiled every time, as Peeky first removes their fingerprints. The wrapper stamps their MIR with the build's nonce, and records their rustc commands, so `opt-level=0` is checked whatever the profiles say. `--emit=llvm-ir` moves into the wrapper, for them only.
- **Analysed crates are compiled with `RUSTC_BOOTSTRAP=<crate>`** (owner's decision). The program is built from the MIR the proofs read. That takes two nightly flags: `-Zmir-opt-level=0`, which changes the code rustc generates (on 1.96, another temporary and other local numbers), and forced legacy mangling, since `RUSTC_BOOTSTRAP` alone makes v0 the default. Before that compile, the wrapper runs Cargo's command unchanged, in Cargo's own environment, writing only metadata into its own directory. So the crate builds on stable Rust as the user's `cargo build` would: what stable rustc refuses, a `#![feature]` among it, fails `peeky build` in rustc's words. There is no second, stock build.
  - What differs from a stock build: the MIR optimization level and the mangling, in the analysed crates alone; and the number of jobs, which Peeky fixes (`cargo build -j N`, N the CPUs this process may use) so that a build script's `NUM_JOBS` is known. That `-j` overrides the configuration's `[build] jobs`. Peeky reads the program it built, so readings are of that program.
  - The expansion and the compile run in the same environment, Cargo's with its jobserver's descriptors passed on, and `RUSTC_BOOTSTRAP=<crate>`. The stable check has Cargo's environment as it is.
  - Who sees `RUSTC_BOOTSTRAP`: build scripts never do. Cargo runs them in its own environment, which has none, and their compiles and rustc probes pass through the wrapper unchanged. A procedural macro's own compile doesn't either. But a procedural macro runs inside the rustc that compiles the analysed crate, so it could read the variable from that process's environment. So could `env!` or `option_env!` in the crate itself.
  - So the build is refused, naming the crate and the file, where an analysed crate's sources, a local procedural macro's, or a vendored procedural macro's mention `RUSTC_BOOTSTRAP` (a plain scan of the `.rs` files their compiles read), or where rustc's dependency file records a read of it (`env!`, `option_env!`, a macro's tracked read). Otherwise procedural macros and the crate's code are taken not to change on `RUSTC_BOOTSTRAP` (owner's decision), as dependencies are taken as sound.
- Their dependency files' entries are hashed inputs, and `OUT_DIR` files hashed artifacts.
- So is what a build script or procedural macro may read: each local build script's `rerun-if-changed` paths, or, with none, every file of its package, as Cargo's default has it. A directory it names is recorded with its own identity (a directory, or a link and what it names; missing or unreadable told apart from empty) and entry by entry, as Cargo scans it, hidden and `target` directories and links too; a link that leaves the root is refused. A file, directory or link, not followed, that changed while Cargo ran (by its status before and after, change time included) is `PROVENANCE_CHANGED`. The status before is of everything under the workspace, hidden directories included, less a `target` directory at its top, since a script's inputs are known only once it ran. Each entry is kept once, by its status not followed (a link's target besides, or that it dangles), `.git` included. So an input missing after the build is recorded as missing only where that record proves it missing before: the deepest directory on its way was listed whole, and nothing on the way failed to list or read. That directory, and any on the way under it, must also be unchanged after the build: a file the script made, read and removed while Cargo ran changes them, and is `PROVENANCE_CHANGED`. An input that is, or passes through, a link to nothing, before the build or after it, is refused (owner's decision): Peeky can't tell what the script read through it. Elsewhere, Peeky can't tell whether the script read it, and refuses, saying why: outside the record, a path it couldn't read, or the record's limit of 200,000 entries (files and directories). Output past 16 MiB, or more than 65,536 files, is refused. Each build script's `rerun-if-env-changed` variables are recorded with the values Cargo gives it, the dependencies' too: the host for `TARGET` and `HOST`, `debug` for `PROFILE`, the build's `-j` for `NUM_JOBS` (Peeky passes one), the unit graph's profile for `OPT_LEVEL` and `DEBUG` and its features for `CARGO_FEATURE_*` and `CARGO_CFG_FEATURE`, the build's `rustc --print cfg` for `CARGO_CFG_*`, the package's manifest and place for `CARGO_PKG_*` and `CARGO_MANIFEST_*`, Peeky's own `CARGO_ENCODED_RUSTFLAGS`, else what Peeky gives Cargo, which Cargo passes on. Each is recorded by package and variable, with how Peeky knows it. A probe compares again only what Peeky gives Cargo, and `NUM_JOBS`; Cargo's own values follow from the tools, the configuration and the manifests, which the build's other records hold. One Cargo sets otherwise, whose value Peeky can't compute (`OUT_DIR`, `DEP_*`, `CARGO`, `CARGO_MAKEFLAGS`, `CARGO_PKG_README`, which a file's presence decides, `CARGO_CFG_DEBUG_ASSERTIONS`), is refused, since Peeky can't tell a later build's. So is any other `CARGO*` variable, but those Cargo was seen to pass on unchanged (`CARGO_HOME`, `CARGO_TARGET_DIR`, `CARGO_INCREMENTAL`, `CARGO_NET_OFFLINE`, `CARGO_TERM_COLOR`, `CARGO_PROFILE_*`, all Peeky's) or never to set for a build script (`CARGO_PRIMARY_PACKAGE`, `CARGO_CRATE_NAME`). A local procedural macro adds every file of its package and of each analysed package. One that reads files elsewhere isn't followed: Peeky can't see what it reads. Dependencies' build scripts and macros are taken as sound, as the rest of a dependency is.
- A probe uses the build while its manifest holds (`assert_current`), and never builds: [ADR 0004](0004-stateless-probes.md)'s step 5 for these targets. It holds a lease on that build while it runs, so no later build removes its files; a build keeps the two newest builds and any leased one.

**Dependency calls** are unknown code, as in option (c). A note names one: "a call to `tokio::spawn` (dependency `tokio`) on line 41 is given a pointer into `x`; Peeky reads no dependency's code, so it may keep a copy".

- Dependencies are taken as sound, as std is (question 2). Otherwise every memory read in such a crate is withheld.
- Only the analysed crates' units are read, picked by name and `DW_AT_comp_dir` as `application_symbols` does now. Dependency generics built into them count against `MAX_DIES`.
- A dependency's frame has no MIR Peeky read, so none of its values is proven. A session reads it, and another member's, as a library's frame: refused, naming the frame.
- The MIR names other crates by their roots: the wrapper compiles the analysed crates with `-Ztrim-diagnostic-paths=false`, which leaves the generated code as it is. The build records every crate's name and each name a crate calls a dependency by. Another analysed crate counts too, proven apart: Peeky's proofs work within one crate.
- Whose code a call runs: a dependency's trait's method is the crate's code where the crate's own `impl` defines it and names the trait in full in its header, and the dependency's where it is the trait's default or a blanket `impl` of the dependency's. Peeky resolves no `use` line: where the crate's `impl` names the trait otherwise, the note says Peeky can't tell (owner's decision). The crate's own trait's method on a type parameter or a trait object may be another crate's code: the program's binary may implement a public trait. So may one on a type parameter in `&`, `Box` or `Pin`: these are `#[fundamental]`, so the binary may write `impl util::Sink for Box<AppType>`. The crate's `impl` for such a wrapper of its own type (`impl smallnum::Keep for Box<Mine>`) is matched with the wrapper kept; where Peeky can't tell which type it is for, the note says so, never that it is the dependency's code. A function of the crate's is read: the types it is given don't make it a dependency's. A call through a function pointer, or a closure whose body Peeky read none of, may run a dependency's code, and the note says that.
- What counts as given: the value, a copy of it or of what is computed from it, or a pointer into it, as Peeky's copy flow traces them, by a call that may have run before the stop. A call the crate's own code makes counts where that code may hand what it is given on to a dependency's (a helper, a closure the standard library's `map` runs, a `Drop`): ``a call to `forward` on line 54 is given a pointer into `x`, and may hand it on to `smallnum::keep` (dependency `smallnum`)``. So does a call or drop that may run a dependency's type's code (`std::mem::drop::<smallnum::Holder>`), and one through a trait object or a type parameter, which a dependency's type may be. Such a call keeps what it is given for `_escapes` (today's rules treated `tokio::spawn` as keeping nothing), and may touch any global state.

**Macros.** The source scans (`unsafe_source`, `without_numbered_structs`, `_crate_traits`, trusted names) read each analysed crate as rustc expands it (`-Zunpretty=expanded`). A glob `use` of a dependency makes bare standard names untrusted, as it shadows the prelude.

- With full paths, the MIR roots the standard library's (`std::cell::Cell`), so a bare name is never taken for std's in a workspace's build.
- A dependency's macros write the dependency's own code, taken as sound as std's `thread_local!` is: `pin_project_lite::pin_project!` writes `unsafe` into axum, which forbids its own. A local procedural macro writes the project's. So the unsafe scan reads the expansion only where the build has one, and the note names it; otherwise it reads the crate's own text, as for a crate alone.
- What the build generated and an analysed crate reads (a build script's `OUT_DIR` file it `include!`s) is the project's code: the unsafe scan reads each such file, and names it.

**Errors.** `BUILD_FAILED` as today. A new `BUILD_REQUIRED`, retryable: no current build, or a source or manifest changed since it, found before launch (the normal edit cycle; owner's decision). It names the file and the exact `peeky build` command. `PROVENANCE_CHANGED`: a change during a build or a run, or to what identifies the build: Cargo's configuration, the lockfile, a vendored copy (its checksum or a file), a tool, or the build's own output. `UNSUPPORTED`: a git dependency, vendored or not (Cargo.lock records no checksum for a git source), `[patch]`, or an analysed package of edition 2015. Editions 2018, 2021 and 2024 are analysed; how 2018 was qualified is under "Verified for phase 2".

**Trust and the worker.** Build scripts and procedural macros are the project's code, as the program is; `THREAT_MODEL.md` says so. A worker builds only from its `--rust-config`, as a job. It compares every `Cargo.toml`, `Cargo.lock` and the analysed sources with the client's.

**Docs:** `lang/rust.md` and its cookbook, `RUST_AGENT.md`, `ERRORS.md`, `THREAT_MODEL.md`, `REMOTE.md`, `WORKER.md`.

**Acceptance,** on the six images:

- **tokio-4867, tokio-6752:** a library's integration tests. tokio has unsafe code, so memory reads stay withheld with today's note; scalars read.
  - tokio-4867, checked in part 5: tokio is edition 2018, in a virtual workspace at resolver 1, with `features: ["full"]` and the test `sync_broadcast`. The build took 17.3 s cold and 10.6 s warm, on 4 CPUs. tokio's unit holds 165,928 debug entries, and the test crate's 18,787, against `MAX_DIES`' 400,000.
  - In the test `send_two_recv`, `n` read 2, which the test asserts. `n = 5` was made, and the test failed with `left: 5, right: 2`: a change no dependency was given.
  - In tokio's own `Tail::notify_rx`, `self.rx_cnt` read 2 (what `send` returned as `n`), `self.pos` 1 and `self.closed` false. The code read there holds no container, so the unsafe-code note isn't exercised.
  - In `send_recv_bounded`, a change of `rx.next` after `task::spawn(rx.recv())` was refused, naming `tokio_test::task::spawn` (dependency `tokio_test`) on line 99.
  - A line of the generic `Sender::send2`, which only the test crate compiles, ends with `DEBUGGER_ERROR` here. Part 4 (#463) refuses it before launch, naming the function.
  - Its `Cargo.lock` is version 2. `cargo update --workspace` keeps that version, so the check added `version = 3` by hand; with no git sources nothing else differs. The refusal says so now (#472).
- **axum-1934:** unit tests of a crate that forbids unsafe code, so memory reads are exercised.
- **bat-3108:** a binary with `build.rs`; clap's types stay raw.
- **ripgrep-2209:** `rg -U --replace`, with the path dependency `grep-printer` analysed. Both are edition 2018, and the root package makes the workspace resolver 1.
  - Checked in part 5: the build took 9.1 s cold and 3.9 s warm, on 4 CPUs. grep-printer's unit holds 7,130 debug entries.
  - The issue's command, with `--stats`, reproduced the bug. In `Stats::add_matches`, `n` read 1; in `add_matched_lines`, 3; in `add_bytes_searched`, 683. rg printed `1 matches`, `3 matched lines` and `683 bytes searched`.
  - In `trim_line_terminator`, under `rg -r X pipc`, `line.end` read 287, the bytes of the file's first three lines, and `end` 286. In `trim_ascii_prefix`, under `--trim`, `count` read 4, the spaces rg trimmed.
  - In `UserColorSpec::from_str`, under `--colors match:fg:nocolor`, `pieces.len` read 3 from memory: grep-printer has no unsafe code.
  - Two changes needed a local in memory, so the check edited `add_matches` and built again. `added = 5` was made, and rg printed `5 matches`.
  - With `added` given to `grep_matcher::Match::new` first, its change was refused, naming that call (dependency `grep_matcher`). rg printed the dependency's copy, `1 matches`.
  - The replacement code the bug is in (`Replacer::replace_all`) is generic, and only `rg`'s crate compiles it. A stop there ends with `DEBUGGER_ERROR` here; part 4 (#463) refuses it, naming the function.
  - `trim_line_terminator` and `trim_ascii_prefix` declare their parameters over several lines, as rustfmt writes a long signature. Such a function isn't matched to its MIR, so its changes are refused, saying Peeky can't tell (#466). That isn't an edition matter.
  - Its `Cargo.lock` is version 2 too, marked `version = 3` by hand (#472).
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
- clang++ records `--driver-mode=g++` before the options it was given. It is an option of one word, as the driver reads it (tested). Only the modes `gcc`, `g++` and `cpp` are read: `cl`'s builds `/clang:-O2` at -O2 (tested).
- The driver records an alias as what it means: `--optimize=2` as `-O2`, `-O` as `-O1` (by hand, Apple clang 21). `native_units.optimization` takes the last `-O` and LTO option from the words alone. Each option it knows takes no value or one, and those taking none are each checked against the driver. Any `-O` or LTO word after another option is refused: clang reads `-O2 -fmodules-user-build-path -O0` and `-O2 -resource-dir -O0` as `-O2`, and Apple's `-O2 -fdepscan-prefix-map /old -O0` too (tested). The study's projects record no such option before an `-O`. It refuses any `-mllvm` option, and any `-Xclang` one but warnings, `-O`, LTO and the exact options Apple clang 21 adds.
- GCC 13 records only code generation options: no `-D`, `-I` or `-include` (tested).
- The options a unit's replay leaves out each predefine what clang predefines without them, in C and C++ (tested). `-fno-unwind-tables` doesn't: it drops `__GCC_HAVE_DWARF2_CFI_ASM`, so it is passed. Apple's driver reads its SDK's `SDKSettings.json`, which no line table names, and nothing else outside the headers (tested).

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

All fit the bounds: 64 MiB of line tables, 256 MiB of debug information, 400,000 entries, 300 MiB kept. valkey keeps the most, 68% of `MAX_KEPT`. `-fdebug-macro` adds 2 MB to its line tables.

**The owner's decisions on these findings.**

- C: the recipes add `bison -l` and `flex -L`, and on Linux `-fdebug-macro`. jq's grammar is probed in `src/parser.c`, and a breakpoint in `src/parser.y` is refused. On macOS, a header of macros alone can't be checked, so the text rules withhold there. Nor can a header of declarations alone, which no unit's line table names without `-fdebug-macro` (found implementing it): on macOS, most units' text isn't read.
- C++: a unit with an MD5 for every file is judged, as libc++ with `-D_LIBCPP_HAS_NO_PRAGMA_SYSTEM_HEADER` and no inheriting constructor gives. Any other is refused with what its table shows, and the likely remedies as likely. `-gembed-source` isn't used: it embeds most files' text but not all, and made a test binary six times larger.
- Phase 3 starts with a compiler wrapper that records each input's hash, which brings fmt, nlohmann/json, GCC, bison's output and headers of macros alone into reach.

## Not yet verified

Nothing for phases 1 and 2: phase 1's items are verified above, and phase 2's below.

### Verified for phase 2

Measured with Rust 1.96.0 on macOS and in the study's images (aarch64 Linux, 4 CPUs), with a prototype of the wrapper. `tests/rust/test_rust_workspace_shape.py` keeps the first and fourth points checked.

- **Expansion works, through `RUSTC_BOOTSTRAP` for one crate.** `-Zunpretty=expanded` is a nightly flag. `RUSTC_BOOTSTRAP` set to the crate's name allows it for that crate's compile alone, as it allows the MIR dump's flags today. Another crate's name is refused. No stable flag expands a crate.
- **The wrapper runs it as a second rustc process,** before the compile, with `-o` and without `--emit` or `--out-dir`. Its messages stay out of Cargo's: they go to a file. It took 0.01 to 0.21 s per crate, for 4 KB to 3.4 MB of text (tokio). The text starts with `#![feature(prelude_import)]` and the prelude's `use`, which the scans must skip.
- **Build scripts run rustc through the wrapper too:** tokio's `autocfg` probes did, with `CARGO_PKG_NAME` set to tokio. So the wrapper acts only on an analysed target's own compile, by its crate name and source file from Cargo's metadata.
- **Cargo checks each file of a vendored copy as it compiles the package,** and refuses a changed file by name. It doesn't check again while it reuses the compiled package, and it accepts a checksum file that lists no files. So Peeky hashes every file of each copy itself, before and after Cargo runs and at every probe. The copy's checksum file must list them all.
- **`cargo metadata` doesn't describe the build:** without the configured features it leaves out an optional path dependency that a feature enables, and with every feature it reads ones the build doesn't. Cargo's unit graph of the build's own command describes it: `--unit-graph`, an unstable flag that `RUSTC_BOOTSTRAP=1` allows for that one command, which compiles nothing. It gives each unit's package, features, target and profile. Peeky reads the members with `cargo metadata --no-deps`, which resolves no dependency, and everything else from the graph. Every path package the graph compiles must be inside the root, and each analysed package a member it compiles. A graph or metadata that isn't what Cargo 1.96 writes, field by field, is refused, naming the Cargo that wrote it.
- **Profiles:** environment variables override a profile's own settings, in Cargo.toml and in Cargo's configuration, but not a setting for one package (`[profile.dev.package.x]`). Peeky sets opt-level, debug, split-debuginfo, strip, lto and incremental for the dev and test profiles. It refuses an analysed package whose unit in the graph differs, or aborts on a panic.
- **Configuration:** where `.cargo/config` and `.cargo/config.toml` both exist, Cargo reads only `config`, and so does Peeky. A `[target.'cfg(...)']` table applies only where its expression holds for the host: `rustc --print cfg`, with the flags the build has. `true` and `false` are literals, as in Cargo. One Peeky can't evaluate is refused, saying so.
- **Rustflags:** Peeky builds with its own flags (`CARGO_ENCODED_RUSTFLAGS`), so no configuration's `rustflags` apply. It keeps the project's own where each is a `--cfg` or `--check-cfg` flag, chosen as Cargo chooses them: every applicable `target.<triple>` and `target.'cfg(...)'` table's, joined, where any has `rustflags`, else `build.rustflags`; only the chosen ones are checked, so a `target-cpu=native` in an ignored `build.rustflags` is no refusal. They are recorded in the build's provenance. Any other flag is refused, naming the file and key. Where the chosen key is set in more than one configuration file, Cargo merges them (the deeper file's string wins, arrays join); Peeky doesn't model that, and refuses, naming both files. `build.target-dir` gives way to Peeky's own target directory, for the metadata, the unit graph and the build.
- **Races:** before Cargo runs, Peeky records what Cargo reads: each configuration file, the lockfile, the root manifest and every manifest a member pattern can name (hidden and `target` directories included, those `workspace.exclude` leaves out not, as Cargo excludes them), and each vendored file. What must be absent is recorded as absent: a manifest in any directory above the workspace, and configuration or credentials in Cargo's home (Peeky's own); one that appears is a change. A record is a file's kind, where it resolves to and its SHA-256. What Peeky checked is recorded as the bytes it parsed, not read again, and compared once more just before Cargo starts. After Cargo ran, every manifest its metadata and unit graph name (the members', the compiled packages', and each path dependency a member names, built or not) joins the record; the metadata and the unit graph are taken again and must be identical, and everything is compared again. The configuration files are compared as a set before any is read, and the vendored copies rechecked file by file (a new file or a link is refused). A change is `PROVENANCE_CHANGED`. There is no private copy of the workspace.
- **Full paths in the MIR:** `-Ztrim-diagnostic-paths=false` leaves the LLVM IR as it is (only `DIFile` directories differ). The standard library's paths and other crates' print rooted (`std::vec::Vec::<u32>::push`, `smallnum::one`), the crate's own bare (`double`, `inner::helper`). Run over the single-crate tests, it changed no soundness result: 7 tests differed, one by a symbol hash and six by more conservative refusals or other words. So it is set for a workspace's analysed crates alone.
- **A warm build fits an agent's wait.** "Warm" removes the analysed packages' artifacts (`cargo clean -p`) and builds again, as every `peeky build` will; dependencies are reused.

| Project | Analysed (what runs) | Cold | Warm | No change | MIR files, MB (`after`) |
| --- | --- | --- | --- | --- | --- |
| tokio-6752 | tokio-util (test `time_delay_queue`) | 2.7 s | 0.2 s | 0.03 s | 224, 4 (1) |
| tokio-4867 | tokio (test `sync_broadcast`) | 5.0 s | 1.1 s | 0.04 s | 3134, 34 (13) |
| axum-1934 | axum (its unit tests) | 19.6 s | 7.0 s | 0.06 s | 4246, 131 (63) |
| bat-3108 | bat (bin `bat`) | 14.8 s | 2.3 s | 0.04 s | 1376, 44 (20) |
| ripgrep-2209 | grep-printer (bin `rg`) | 3.5 s | 0.9 s | 0.02 s | 672, 10 (4) |
| coreutils-6690 | uu_cp (bin `coreutils`) | 9.7 s | 1.8 s | 0.06 s | 286, 9 (3) |

- **MIR size:** axum's `PreCodegen.after` bodies are 63 MB (2,123 bodies), just under `MAX_MIR_BYTES` (64 MiB). Peeky's reader parses them in 1.5 s. Larger crates will need the bound per crate, or bodies read as a stop needs them.
- **Editions:** tokio at tokio-4867, and ripgrep and grep-printer at ripgrep-2209, are edition 2018. Part 5 qualified 2018 for analysed packages, and edition 2015 stays refused. Peeky reads the MIR, which rustc lowers by the crate's edition, so most differences reach Peeky already decided. `tests/rust/test_rust_workspace_edition2018.py` checks the closures, `into_iter`, `panic!` and the resolver with real Cargo, against the program's own print, on macOS and Linux. Unit tests check the rest. Each difference:
  - **Closure captures.** A 2018 closure captures whole variables, where 2021 captures the places it uses. The MIR builds it from `&p` or `move p`, not `&p.a`, and Peeky's copy flow follows that. A change of `p.b` is seen through `&p`, and after `move || p.a`, all of `p` reads as moved.
  - Inside the closure, the debug information names the whole capture `p` (2021 names `p__a`); it reads with today's note on a capture. The MIR prints a closure's captures one per variable, which in 2018 is every capture. In 2021 one variable can give two, and the print leaves one out (#465).
  - **Arrays' `IntoIterator`.** In 2018, `arr.into_iter()` calls `<&[T; N] as IntoIterator>::into_iter`, a `slice::Iter` that borrows `arr`; 2021 copies `arr`. A change of `arr` after the iterator is made is seen in 2018, and refused in 2021 as a copy. `for v in arr` is by value in both.
  - **`panic!`'s forms.** In 2018, `panic!(x)` calls `std::rt::begin_panic(x)`, `panic!("{}", x)` calls `panic_display`, and a lone literal is the message as written. Peeky's list of std's panic entry points already holds these, checked on rustc 1.96 at 2018 and 2024. A change before `panic!(x)` reaches the payload a caller catches.
  - **The prelude.** 2021 adds `TryFrom`, `TryInto` and `FromIterator`. A workspace's MIR roots the standard library's paths, so a bare name is the crate's own. The impl-header scan counts those three as the prelude's in every edition, which withholds more, never less. The expansion's `rust_2018` prelude is stripped as 2021's is.
  - **Bare trait objects.** 2018 accepts `impl smallnum::Keep for Box<Sink>`, `Sink` a trait, or a `use` or `type` alias of one. The MIR and the debug information always print `dyn`. Such a name is never placed as a type now. Peeky finds no pattern for every alias (`type Paren = (dyn Sink);`). So without the debug information's types, any type a 2018 header names counts only where the MIR's `self` confirms it, inside a `Box`, a `Pin` or a reference too. With them, a path from the crate's root counts only where it names a type. So such a call reads as one Peeky can't tell, never as the dependency's.
  - **Lexing.** Edition 2018 reads `cr"…"` as `cr` and an ordinary string, where 2021 reads a raw C string, as Peeky's lexer does. A 2018 crate whose sources or expansion hold one is refused, naming the line; `cr "…"`, with a space, reads alike in both. `c"…"` lexes alike, and `async`, `dyn` and `try` are keywords in both.
  - **Macros.** `$x:pat` and or-patterns change which arm of a `macro_rules!` rustc picks. Peeky reads rustc's expansion and MIR, never a macro's rules, so nothing changes.
  - **Cargo's resolver.** A 2018 root package, or a virtual workspace without `resolver`, resolves at 1, which unifies features across dev-, build- and target dependencies. Peeky reads each unit's features from Cargo's unit graph of the build's own command, so its record is the build's.
  - Under resolver 1, a workspace whose root is a package gives `--features` to that package, and builds the one `-p` selects without them, saying nothing. Peeky refuses that now, naming `package/feature` and resolver 2. It checks a plain feature in the run's own unit, and `package/feature` in that package's, by its package ID, or the dependency the run calls so.
  - Single crates (`rust_manifest`) still take 2021 and 2024 alone: their MIR prints trimmed paths. Qualifying 2018 there means running the whole Rust suite at 2018, as for 2024.
- **Cargo configuration:** ripgrep's and coreutils' `.cargo/config.toml` set only other targets' flags and linkers, and clippy's lints. tokio-4867's `.cargo/config` is all comments. Peeky accepts these, and refuses what would change its build (`rust_workspace.check_cargo_config`).
- **Git dependencies:** Cargo.lock records no checksum for one, so nothing ties a vendored copy to the commit it names. Phase 2 refuses every git dependency, vendored or not, and says so (owner's decision).
- **Configuration above the workspace** is accepted under the workspace's allowlist (owner's decision), as Cargo reads `~/.cargo/config.toml` above a workspace in the home directory. A key off the allowlist is refused, naming the file and the key. Each file's path and SHA-256 are recorded, so a change is `PROVENANCE_CHANGED`. Peeky's own CARGO_HOME holds no configuration, so Cargo reads no other.

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
   - Files changed since the build, found before launch, are `BUILD_REQUIRED`; `PROVENANCE_CHANGED` is for changes during a build or a run, and to what identifies the build.
   - Analysed crates are compiled with `RUSTC_BOOTSTRAP=<crate>`, after Cargo's unchanged command as a stable check, with no second full build (see Build).
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
