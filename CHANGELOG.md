# Changelog

## Unreleased

### Values Peeky now reads

- **C++ built by GCC:**
  - A `std::unordered_map`'s or `std::unordered_set`'s entries, read from the container's own
    nodes where the debug information proves their layout. The entries must be integers, bools
    or strings, alone or in pairs and structs (#371).
  - More unordered-container entries from their nodes: `float`s and `double`s, characters,
    enums, pointers, a `std::array` held in one, and more than 256 entries, read by position or
    key. A string whose text is in heap memory now shows `<not read>`, as a vector there does
    (#374).
  - A lambda's captures, through GCC's `__closure`, in a `Captures` scope. A capture is never
    changed inside the lambda; its refusal names the lambda's own copy where a change works
    (#369, #380).
  - A text set in a `std::string`'s own buffer, with lldb-dap 20. Heap strings still need
    lldb-dap 21 (#365).
- **C and C++:**
  - `static` locals, in a `Statics` scope, where the code at the stop names them. They are never
    settable (#370).
  - In a caller's frame, a struct or array whose declaration's lines have run, in an unoptimized
    build (#366).
  - C++ structured bindings by reference, where Peeky proves what they refer to is alive (#368).
- **Rust:** a `NonNull<T>` local's address, as a raw pointer's. It is never followed (#420).

### Other changes

- **Rust: a store through a lock's guard into a static is a copy there (#461).** `let r =
  slot(); *r.lock().unwrap() = x;`, with `slot()` returning a `&'static Mutex<u32>`, wrote `x`
  into the static, yet `x` was marked `[settable]`: a change didn't reach the static's copy,
  which the program went on with. It is refused now, naming the store. So through an
  `RwLock`'s write guard, and a `RefCell`'s `RefMut` in a `static mut`, and by a call given
  what the guard's `deref_mut` returned (`r.lock().unwrap().push(x)`, `mem::replace`).

- **`verify --check-command` checks a Python edit with the project's own tests:** it runs the
  command in a fresh copy of the project with the edit, on the program's interpreter, with its
  directory first on `PATH` and no worker token. It runs no shell, and gets a checker's 20 s. The
  result leads with `verdict: passed`, `failed` or `unknown`, and names the command, its exit code
  and the end of its output. `--baseline` says the tests fail on the bug only when pytest's or
  unittest's output shows failed tests. An edit is refused when a module the project supplies would
  import from outside the copy, such as a stale install. A Python command runs through a bootstrap
  of Peeky's, whose watch reports what it loaded and started (for pytest, a plugin adds the test
  files). A run that may not be the copy's, that started a program other than one that starts none
  (`echo`, `cp`), that Peeky stopped at its bound or an uncaught KeyboardInterrupt stopped, or that
  isn't a Python Peeky starts, is `unknown`. The command is trusted, not sandboxed. Other languages
  are refused for now. A build's output limit now counts both streams together, and a Cargo build
  past it is `BUILD_FAILED` (#448).
- **Python: a breakpoint in an installed package stops there.** debugpy skipped library code, so
  a probe at a file in the root's `.venv` said its breakpoint was pending, then ended `no-stop`.
  Now debugpy stops there too, where the program's interpreter says the file is installed, with
  the same rules: notes, readings, hashes, remote checks. A file debugpy still skips is refused
  before debugpy starts your script, with why (#446).
- **C and C++: a binary your project built** (ADR 0008, phase 1). `peeky probe . --binary ./jq
  --at src/execute.c:120` and `describe` read a binary clang built at `-O0` with `-g -gdwarf-5
  -grecord-command-line`. Each unit is judged from its own debug information. A file changed
  since the build refuses the binary by name, and frames in units Peeky doesn't judge show
  nothing, with why. A unit's text is its preprocessing, replayed with its recorded `-I`, `-D`
  and the like, where every header it reads has an MD5 in its line table (`-fdebug-macro` on
  Linux). Where a unit isn't read, holds `asm`, or the binary has code no unit covers (assembly
  built without `-g`), any call may return twice; a config's `returns_twice` names those that do.
  A unit's text is withheld where a `__has_include` finds no file (the build may have), for
  `-march=native`, for a `-D` that names a probe or may paste one, and where one file is built
  twice with different commands. `vfork` returns twice, as setjmp does. Startup code whose name
  a program may use counts as the program's unless its bytes are the toolchain's.
- **DWARF 5:** Peeky's clang builds use DWARF 5, as projects do; GCC's stay at DWARF 4. Peeky
  reads DWARF 5's indexed range and location lists, and statics' address indexes. In a DWARF 5
  unit, anything it can't read fails the read, and is never dropped. Split DWARF, type units and
  partial units are refused as `UNSUPPORTED`, naming the option to rebuild without.
- **Phase 1 of ADR 0008 checked:** clang records every `-D`, `-I` and `-include` exactly, and
  GCC 13 writes no checksums. The study's C projects can be judged by their line tables'
  checksums. Few of its C++ units can: a `#pragma GCC system_header` or an inheriting
  constructor drops every checksum of a unit. Phase 3 starts with a compiler wrapper for them.
  `native_units` reads these facts, and `tests/projects` measures the projects.
- **Python: probe a pytest test without a runner script (protocol 13).** `--run-module pytest --
  tests/test_x.py::test_y` runs pytest as `python -m pytest` does, and no other module; `probe`
  and `session start` take it. It must be the pytest installed for the program's interpreter.
  Peeky gives pytest `--assert=plain`, since rewritten asserts are not the code Peeky reads. A
  `--run` script that imports pytest gets a warning (#447).
- **`peeky build` builds a Rust workspace with dependencies (protocol 14; ADR 0008, phase 2):** a
  config names the packages to analyse and what runs (`analyse`, `run`, `features`, `vendor`).
  The build is offline, from the lockfile and `cargo vendor`'s copy, into Peeky's own directory;
  the analysed packages are compiled every time, at opt-level 0, checked from rustc's commands.
  What stable rustc refuses fails the build, as a stable build fails. A probe or session without
  a current build, or with a source, a manifest or a build script's input edited since, is
  refused with the new `BUILD_REQUIRED`, naming the file and the command, and never builds;
  `--build` builds first. A changed lockfile, configuration or vendored copy is
  `PROVENANCE_CHANGED`.
- **A Rust workspace's build is probed (ADR 0008, phase 2):** a probe or session reads the
  analysed packages' values, from their own MIR, and stops only in their files. A dependency's
  code is unknown code, taken as sound: a change it may keep a copy of is refused, naming the
  call, its line and its crate. So is one the crate's own code may hand on to it. A dependency's
  frame is read as a library's. The source scans read the crate as rustc expands it.
- **A Rust workspace through a worker (protocol 15; ADR 0008, phase 2):** a worker started with a
  workspace's `--rust-config` builds it on request (`peeky build . --worker URL`, or `--build`),
  from its own configuration only. It compares every workspace file the build compiled, the
  binary's included, what local build scripts read, its manifests, lockfile, Cargo configuration
  and vendored checksum files with the client's; never `.git`. Each is compared by its links too,
  never through one out of the root, and each watched directory entry by entry. A package Cargo
  watches whole is compared by the files `cargo package --list` gives, so a client's git-ignored
  files and `.DS_Store` are no difference; where Cargo can't list it, every entry, links by their
  text, and the build says why. A worker's end stops its build, Cargo's processes too, and a build
  that doesn't end is never called stopped. Its `BUILD_REQUIRED` names the client's own command. A
  line of a generic function only another crate uses is refused before launch, naming it, where it
  was a `DEBUGGER_ERROR`. A stop in another crate's copy of the crate's code reads no value, and
  says why: it was read as the crate's own, with none of the checks of that copy. So does one Peeky
  can't tell is the crate's own. A build script watching a directory no longer fails the build with
  `Expected a regular file`. The docs cover workspaces: Rust 7.2, REMOTE and WORKER.
- **The agent guide starts with when to use Peeky, and the first command:** when a probe beats a
  print, and a copy-paste command for a Python test or script in a repository, a Maven or Gradle
  test, a Java class, C and C++ sources and a Rust crate. It says plainly what isn't supported
  yet. The README says the same in short. In the value study, agents read the guide but rarely
  ran Peeky (#441).
- **Java on the project's JDK:** the adapter runs on the `java` its bundle was built with,
  whatever `java` is on `PATH`, and the program builds and runs on the project's JDK. A `java:`
  line, and `java` in `--json`, say which ran what. A config's `java` must still be the bundle's.
  Another is refused with `UNSUPPORTED`, naming the bundle's `java` and what to do. The bundle
  records its `java` file, by real path and SHA-256, and Peeky checks it before every use, so run
  `peeky setup-java` again after upgrading. Where the two differ, each must be a JDK's own
  `bin/java`. An adapter on JDK 19 or later launches programs only on JDK 19 or later, so build
  the bundle with JDK 17 for older projects (#442).
- **A study of Peeky's value on real bugs** (`experiments/value-study`, #441). A coding agent
  attempted 29 real bugs, 3 times each, with and without Peeky. The outcome was the same: 89%
  of bugs solved without Peeky and 86% with it, a paired difference of −2 points (95% interval
  −8 to +2). The Peeky arm used about 1.2 more AI credits per bug. Agents read Peeky's guide but
  ran Peeky in only 3 of 87 attempts. The report says why, and what to change.
- **`python -OO`:** `peeky` and `peeky setup-java` no longer fail at start when Python drops
  docstrings (`-OO` or `PYTHONOPTIMIZE=2`).
- **A Python program runs on its project's interpreter (protocol 12):** `--python PATH`, else the
  root's `.venv` or `venv`, else the active virtual environment, else Peeky's own. So Peeky
  installed with pipx or uv runs a program that imports its project's packages. The output names
  the interpreter (`python`). Peeky reads the program's code with it, at the program's
  optimization level, asked again at each launch. It refuses one older than CPython 3.11.
  `peeky-worker --python` chooses a worker's (#437).
- **Java from a wheel:** `peeky setup-java` builds the Java adapter from the installed package,
  into `~/.peeky/java-adapter`. A Java config without `bundle` uses it. Each build is a version
  there, switched to in one step under a lock; a session keeps the version it started with, and
  `--prune` removes older ones. Peeky runs it only from directories only you can use, and runs
  any bundle only if its recorded recipe (patch text, launcher, lock, notices, flags) is its own;
  setup also rebuilds for another javac. A missing bundle's error gives the command. `scripts/setup_java_adapter.py` runs the same setup in a checkout. The state
  directory is now made mode 0700, and refused if it is a link or another user's (#407).
- **A rare failure closing a session:** closing a session just as its program started a
  process could fail with "cannot join thread before it is started". It no longer does.
- **Faster C and C++ builds:** sources preprocess and compile in parallel, the checks of
  preprocessed text are 4× faster, and a new process doesn't rehash the toolchain binaries. An
  8-file C++ probe takes 2.3 s on macOS, down from 6.1 s (#398).
- **Bigger C, C++ and Rust programs:** Peeky reads a build's symbols as llvm-dwarfdump writes
  them, keeping at most 300 MiB, not 2 MiB and 16 MiB of text. An 8-file C++ program using the
  STL, and a Rust crate using BTreeMap, are no longer refused or read without their checks. A
  build over a bound is `BUDGET_EXHAUSTED`, and says what to do. A symbol read that times out or
  whose tool fails is a `TIMEOUT` or `DEBUGGER_ERROR` (#397).
- **Frames after a step:** after a step that stopped in your code, a frame outside your sources
  is refused by its index, as before the step. It no longer says your code left the stack (#379).
- **Thirteenth evaluation's CLI fixes:** `queries diff` tells a value one run has none of from a
  difference (`no value in run b`, `differs` NULL), after the real ones, and counts them apart.
  The 8-intervention error says how many were given. Probe tables give a long reason once per
  selector, and say how to read a cut container. A lambda comparator shows its compiler's name.
  lldb-dap 18's notes are whole once per build (#383, #384, #385).
- **C and C++ char arrays:** a `char[N]` shows its text up to its first NUL, then a note of what
  follows it, each byte as `\xNN`. `--json` keeps every byte as `buffer`, and `text()` reads it
  (#381).
- **Under load:** every launch step has what remains of the launch's one deadline. A stop the
  debugger doesn't describe in time now fails with `TIMEOUT`, rather than losing its place (#367).
- **Remote protocol 8** (#369).
- **Security:** `PEEKY_WORKER_TOKEN`, a client's name for the worker's token, is now removed from
  the environments of debuggers, debuggees, builds, checkers, candidates and the model, as
  `DEBUG_TOKEN` was. The worker bounds each read of a request at 10 s, before the token check too.
  The tunnel command adds `-o ExitOnForwardFailure=yes`. [SECURITY.md](SECURITY.md) says how to
  report a vulnerability (#378).
- **Probes read a caller's frame:** `--capture 'reading,1:i'` reads `i` in frame 1, the caller,
  beside the stopped frame's values, by the rules `session get --frame 1` applies. Rows say
  `frame`; the store keeps `captures.frame` (schema 21) (#382).
- **Under load:** every launch step has what remains of the launch's one deadline. A stop the
  debugger doesn't describe in time now fails with `TIMEOUT`, rather than losing its place (#367).
- **Remote protocol 8** (#369), and **9** (#382).
- **Protocol 10:** `expand` takes `key`, for a map's entries Peeky read from an unordered
  container's nodes. Every request must carry `X-Peeky-Protocol`. A worker refuses an older
  client's requests, and a client sends an older worker nothing, except `version` (#374).
- **`version`'s `error_codes`** lists every code Peeky sends, now with `BUILD_FAILED`, `NO_STOP`
  and `PROGRAM_CRASHED` (#423).
- **C and C++ declarations over several lines:** a stop on the first line, just after code of a
  later line, withholds the local: Peeky can't tell whether it is fully initialized. It showed
  leftovers, with GCC and clang. A row the debugger can't read is never marked `[settable]`. The
  GCC lane runs two more test files (#425).
- **C and C++ notes:** a withheld unordered container says why no node is read, and what to
  try. Refused changes to a lambda's capture or a structured binding say where the value is.
  `describe` marks statics and lists GCC captures (#380).
- **Rust notes:** a value Peeky shows none of says so, not that it shows its pointer. Where its
  type can hold a pointer but may hold none, as an `Option<&T>`, the note says only that. A change
  that the program's own copy rules out at every stop gives that one reason, and its hint agrees
  (#420, #421).
- **Security review before going public (protocol 11):** a client sends the token only after the
  worker proves it holds it, through a new `hello`. A process on a stopped worker's port gets
  nothing. The worker refuses a web page's requests, and a token under 32 characters. Builds,
  Python candidates and `verify` candidates go only where no other user can write. `peeky`,
  `peeky-worker` and `peeky-agent` take the token out of their environment first. A session's
  own worker runs isolated, and `peeky-agent --url` takes only loopback. A worker bounds each
  request at 30 s, and answers `BUSY` past 32 connections.
- **Probe and describe fixes:** a handled signal's or exception's stop no longer starts a line's
  count of stops again, so `--set-at LINE@HIT` is made at that line's HIT-th stop. A probe whose
  debugger lost a stop, with a `--set-at` never made, no longer fails with an internal error.
  Where that stop was at the change's line, it says the line's count of stops is unknown, not
  that its hit never came, and an `@all` there is listed in `unapplied`. A stop lost at another
  line no longer marks an `@all` incomplete; one Peeky can't match to a line counts for every
  pending change. The evidence store is schema 22: a change at one hit can end `lost_stop`.
  `describe` lists no `verify` command for an attached JVM, which `verify` refuses. One stop is
  `1 stop`, not `1 stops` (#408, #422, #424).
  Protocol 11 also covers the Rust withheld-value labels and notes, and `NonNull` rows (#431).
- **Docs:** [docs/HISTORY.md](docs/HISTORY.md) replaces the development log, `docs/PROGRESS.md`,
  which stays in the git history. The repository has a code of conduct, and issue and pull
  request templates.
- **Packaging:** install Peeky with pipx or uv, in an environment of its own. PyPI's page gets
  classifiers, keywords and links to the docs, issues and changelog. The README's links are full
  URLs, so they work there. The source archive has the presentation, the changelog and the code
  of conduct. `scripts/venice/native-grpc.sh`, a Venice build helper, is gone.
- **Release hardening:** `peeky-agent --url` reads the worker's token from `PEEKY_WORKER_TOKEN`,
  as every client does. Given only `DEBUG_TOKEN`, the worker's own name, a client refuses it and
  names the right one. No error the worker sends quotes a request's headers. As a second defence,
  the worker removes its token from the error text it builds, a probe child's too. A token must be
  32 to 512 characters. CI's checkouts keep no credentials, and
  [docs/DESIGN.md](docs/DESIGN.md#module-map) has a module map (#407).

## 0.1.0 (2026-10-26)

The first tagged version of Peeky. Peeky is a debugger for agents and people. It pauses a program,
reads its values, tries a change and verifies a fix. It never shows a wrong or guessed value
without a note: where it can't prove a value, it withholds it and says why.

Twelve rounds of live agent evaluation shaped it. In the last, four agents each found and fixed
a new bug, in Python, Rust, C++ and Java, with no wrong value shown
([report](docs/evaluations/2026-10-25-agent-twelfth-round.md)).

### What it covers

- **Languages:** Python, Java, C, C++ and Rust.
  - Python through debugpy.
  - Java through the java-debug adapter, which `scripts/setup_java_adapter.py` builds from the
    repository or the source archive. A program can be launched or attached to.
  - C, C++ and Rust through lldb-dap. C and C++ build with clang or GCC; Rust with Cargo.
- **Commands:**
  - `peeky describe` lists a program's lines and names.
  - `peeky probe` answers one question in one run: stop, read, change, compare.
  - `peeky session` steps through a program interactively.
  - `peeky verify` checks a fix with your own checker on a fresh build.
  - `peeky sql` and `peeky queries` ask follow-up questions over every saved run.
    `text()` and `elements()` read a stored string or list, except for Python values.
- **Changes:** `--set`, `--set-at LINE@HIT`, `@all`, and `NAME=@before` to put a value back.
- **Candidate fixes:** `probe --old/--new` and `verify --old/--new`, several edits in one file.
- **A separate machine:** a worker over an SSH tunnel, for all five languages. Remote protocol 7.
- **The evidence store:** SQLite, schema 20. It needs SQLite 3.30 or later with its JSON functions.

### The promises, and their limits

- A value Peeky can't prove is withheld, with a reason and what to do instead.
- A `[settable]` mark promises that the change reaches what the program goes on with.
  - In Rust, the promise covers the paths that don't panic or loop forever.
  - Peeky refuses a change it can't promise, before the run where it can.
- `queries diff` leads with a verdict, and never claims two runs' hits are the same moments.

### Known gaps

- **C and C++ built by GCC:** some values LLDB misreads are withheld (unordered containers,
  lambda captures, strings' own buffers). See the C and C++ cookbook.
- **C and C++ pointers:** a value read through a pointer is marked `[unproven]` unless Peeky
  proves it points at a live object. On Linux that includes string literals.
- **Rust:** a change is refused where Peeky can't prove it reaches every copy the program keeps.
  Some of those refusals are stricter than they need to be.
- **Text changes:** setting a whole string needs lldb-dap 21 (macOS ships it).
- **Verification** edits one file per run.
- **Attach** works for Java only.
- **Not supported:** CUDA (#61), Kubernetes workers, several users per worker.
- **CI:** Linux only. macOS is used for development but isn't in the CI matrix. Hosted CI wasn't
  running, so this version was checked by the same Linux lanes in Docker.

### Install

- Python 3.11 or later. macOS's built-in `python3` is 3.9 and won't work.
- From the release: `pip install peeky-0.1.0-py3-none-any.whl`. The examples are in
  `peeky-0.1.0.tar.gz`.
- `peeky --version` prints `peeky 0.1.0`.
- C, C++ and Rust need LLVM's `lldb-dap`; the COOKBOOK lists the packages.
- No licence has been chosen yet, so none is included.
