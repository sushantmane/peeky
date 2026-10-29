# Changelog

## Unreleased

### Values Peeky now reads

- **C++ built by GCC:**
  - A `std::unordered_map`'s or `std::unordered_set`'s entries, read from the container's own
    nodes where the debug information proves their layout. The entries must be integers, bools
    or strings, alone or in pairs and structs (#371).
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

### Other changes

- **Faster C and C++ builds:** sources preprocess and compile in parallel. Each tool's answer
  to `--version` or `--help` is reused while its SHA-256 is unchanged, and the checks of
  preprocessed text are 4× faster. An 8-file C++ probe takes 2.1 s on macOS, down from 5.7 s
  (#398).
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
- **C and C++ notes:** a withheld unordered container says why no node is read, and what to
  try. Refused changes to a lambda's capture or a structured binding say where the value is.
  `describe` marks statics and lists GCC captures (#380).
- **Docs:** [docs/HISTORY.md](docs/HISTORY.md) replaces the development log, `docs/PROGRESS.md`,
  which stays in the git history. The repository has a code of conduct, and issue and pull
  request templates.

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
