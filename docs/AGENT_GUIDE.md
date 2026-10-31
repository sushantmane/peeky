# Peeky agent guide

Peeky stops a program at a line, reads its values, can change one, and runs it to its end. Cite
a peeky command and its output for every claim. [COOKBOOK.md](COOKBOOK.md) and your language's
cookbook have every rule, cited here by section (7.1: the same number in both).
[ERRORS.md](ERRORS.md) has the error codes.

## When Peeky beats a print

Reading the code, a print in a short script and the project's tests fix most bugs. Start there.
Peeky pays off when a print costs more:

- **The value is deep in the project's code:** a print means editing there, and maybe a rebuild.
  A probe needs no edit.
- **You need it at several hits, or across runs:** `--hits all`, then `peeky queries diff`.
- **You want to test a hypothesis:** `--set` changes a value mid-run, with no edit.
- **Builds or test runs are slow:** one probe reads many values in one run.
- **You want to try a fix:** `peeky verify` runs your checker on a fresh copy with the edit.

Each question costs a run, and Peeky can't run every project (below).

## Your first command

Run it from the project's root. Stop at `FILE:LINE`, the line after the one that computes the
value: a breakpoint stops before its line runs. `--capture a,b.c` names what to read.
`peeky describe`, with the probe's target and `--at FILE`, lists the lines and names. Your
language's page has the rest: [Python](lang/python.md), [Java](lang/java.md),
[C and C++](lang/c-cpp.md), [Rust](lang/rust.md).

**Python, a test or a script in a repository.** The target is the root. `--run` names a `.py`
script that reaches the line. For a pytest test, write a two-line runner (delete it after):

```sh
printf 'import sys, pytest\nsys.exit(pytest.main(sys.argv[1:]))\n' > peeky_pytest.py
peeky probe . --at src/pkg/mod.py:LINE --capture a,b.c \
  --run peeky_pytest.py -- -q tests/test_x.py::test_y
```

- Without `--run`, the `--at` file runs as the program: `peeky probe . --at scripts/job.py:LINE`.
- `--at` and `--run` must be inside the root. Peeky stops only in the project's own code: a file
  in `site-packages` never stops (`no-stop`). An installed project must be editable:
  `pip install -e .`.
- It runs on the root's `.venv` or `venv`, else the active virtual environment. For the `python`
  on `PATH`, such as conda's, add `--python python`. The `python:` line says which ran.

**Java, a Maven or Gradle test.** Run `peeky setup-java` once, with the `java` your probes will
have on `PATH`. Start the test waiting for a debugger, then attach:

```sh
mvn test -Dtest='FooTest#bar' -Dmaven.surefire.debug > test.log 2>&1 &
# Gradle: ./gradlew test --tests 'pkg.FooTest' --debug-jvm > test.log 2>&1 &
peeky probe . --config attach.json --at src/main/java/pkg/Foo.java:LINE --capture a,b.c
```

Probe once `test.log` says `Listening for transport dt_socket`. `attach.json` names the files
you stop in and their classes (Gradle: `build/classes/java/main`):
`{"port": 5005, "sources": ["src/main/java/pkg/Foo.java"], "class_roots": ["target/classes"]}`.

**Java, a class with `main`:** `run.json` lists every `.java` file it needs, and no Maven or
Gradle dependencies: `{"sources": ["pkg/Main.java"], "main_class": "pkg.Main"}`. Then:
`peeky probe . --config run.json --at pkg/Main.java:LINE --capture a,b.c`.

**C or C++:** `peeky probe DIR --at file.c:LINE --capture a,b.c -- ARGS`. Peeky builds the `.c` or
`.cpp` files of one directory itself, as C11 or C++17, with the headers beside them. It takes no
flags, include paths or libraries. A project with its own build (Make, CMake, autotools) isn't
supported yet: Peeky can't build it, or attach to its binary.

**Rust:** `peeky probe CRATE --at src/main.rs:LINE --capture a,b.c`. Only a binary crate with no
dependencies works, as from `cargo new`, with its `Cargo.lock`. A crate with dependencies, a
`src/lib.rs`, `src/bin` or `build.rs` isn't supported yet.

## The loop

1. **Watch:** the probe above; `--hits all` captures every stop. Read each row, `status`, `output`
   and every `warning:` (1.2, 2.2).
2. **Ask follow-ups with no rerun** (rule 12): `peeky queries diff` after two probes,
   `queries wide`, or `peeky sql "SELECT hit, value FROM last_run"` (3, 4).
3. **Test a hypothesis:** add `--set name=value`. Read `set name: old -> new` and the output. It is
   an experiment, not a fix (2.3).
4. **Try a fix:** add `--old '<exact text>' --new '<new text>'` to the probe: a fresh copy with the
   edit. Run its `compare:` line. Write the general fix, not a special case (6.6).
5. **Verify:** `peeky verify <target> --check <checker.py> --baseline` must say `passed: False`,
   or the checker can't tell a fix from the bug. Then, with `--old`/`--new`, report `passed: True`
   only from `verify` (6.1, 6.5).
6. **Several looks at one stop:** `peeky session start <target> --at FILE:LINE`, then `inspect`,
   `get`, `step`, `continue`, and always `close` (5).

Report what you observed, changed and verified, quoting the commands.

## Rules you will hit

1. **A breakpoint stops before its line runs.** To see what a line assigns, stop on the next line.
   Probe only lines `describe` lists. `NO_STOP` means the line never ran (10).
2. **Set a field in the frame that owns the struct.** Peeky never writes through a C/C++ or Rust
   pointer, reference or Box (`r->tail`, `self.k`); Java can (7.3). Use the call a refusal names
   (`--set-at LINE@N`) only where it says `settable`. Otherwise follow the refusal: read
   `describe`'s `mutable:` line, or use a candidate edit (7.1, 7.2).
3. **Change only where nothing is under way:** C/C++ at a statement's first line, Rust where the MIR
   proves it, never after `step --out`. Rows mark what can change `[settable]` on paths that don't
   panic or loop forever. A change the build rules out is refused before launch (exit 2; 2.3, 7).
4. **`--set` applies once, at hit 1.** `--set-hit N` picks another, with `--hits` N or more;
   other hits only observe. `--set-at LINE@HIT NAME=VALUE` changes at any line's hit, `--at`'s
   too. Repeat it per change: `--set-at 9@2 n=0 --set-at 9@5 n=0`. A probe takes at most 8
   `--set` and `--set-at`: split more over probes or edit. `@all`: each stop while the probe
   runs, at most 100. `x=@before` undoes a change, as to skip a statement (2.8).
5. **Hits count stops, not passes.** A line with a macro's or a block's code, or a closure, can
   stop several times per pass. Read `warnings`, and each row's `stop` and `function`. A loop
   header may stop only before its loop: probe the body's first line (2.2).
6. **A `note`, `no value yet:` or `unavailable: true` is not a value.** Don't trust a value marked
   with a `note`, or anything read through that value. Never treat an error text as a value.
   - `unavailable: true`: no value at that line (optimized out, not live).
   - `no value yet:` (and why): the local's storage may hold leftovers. It can't be `--set`.
   - C and C++: read a local below its declaration. Read a `note` on a line a loop returns to (a
     `do` body's first) on the next line. Below a `setjmp` call, a `note` replaces `no value yet`:
     read it below its declaration too (7.1).
   - Python: a `note` marks a local the pass assigns below the stop (5.1).
   - Rust: `[stale]`: leftover or mid-change bytes; its note says which. Raw fields with a note are contents Peeky
     could not prove (7.2). `unproven_pointer`: `raw` (a raw pointer) or `unknown` (maybe one).
   - Optimized builds may show stale values.
7. **Rust reads `Vec`/`String` contents it can prove alive.** Those are owned locals, reference
   parameters of plain elements, `&str` loop items, and such fields: `self.values`, `entry.name`.
   Behind `&mut` or owned, they read only in the innermost frame. A crate with `unsafe` code or a
   nightly feature gets none: raw fields that note why (7.2).
8. **Members need `this.`** in C++ and Java (`this.head_`: 7.1, 7.3). Python dict entries are
   `d['key']` (2.1).
9. **`continue --to LINE` doesn't skip the session's breakpoint.** A stop there comes first (exit
   3, with a note): repeat it. If a loop stops there each pass, start the session `--at LINE` (5.2).
10. **Steps land where the code jumps:** a closing `}`, a loop header, an `if`'s line, or a caller.
    A `note` says when a step's stop is in another function or depth. Read a block's locals
    before stepping past its last statement (1.1, 5.2).
11. **Sessions need `--name` on every command** (unless `default`), and a `close`. Their programs'
    clocks run while you think, so prefer probes for timed code (5.3, 5.4).
12. **`queries diff` pairs the last two runs by hit number.** Name runs (`--param a=1 --param
    b=5`). Check the `(output)`, `(args)`, `(edit)` and `(sets)` rows before crediting an edit.
    After a candidate, run its `compare:` command. When hits don't line up (a sort's comparator),
    use `queries join` (3.1, 3.5).
13. **`--old` must match the source exactly once**, with real newlines (`--old-file` for long text:
    6.3). It goes before `--`, because everything after `--` goes to the program (2.5, 10).
14. **Same-named locals (C, C++):** a plain name means the one the code names at the stop;
    `name @ file:line` selects another (7.1). Rust: as `describe` lists it (`x @ main.rs:6`).
15. **Respect opaque values.** `<unqualified Rust enum layout>`, `<unverified ...>` or
    `budget-exhausted` mean peeky refused to guess. Don't invent their contents.

## Commands

| Command | Cost | Use |
| --- | --- | --- |
| `peeky describe <target> [--json]` | one build | Valid breakpoint lines, each function's names, and what `--set` can change. |
| `peeky probe <target> --at <path>:<line> --capture a,b.c [--set x=0] [--hits N\|all] [--json] [-- ARGS]` | one run | Answer one question in one call: values at a line, after any interventions, and the output. |
| `peeky probe ... --old '<exact text>' --new '<replacement>'` | one build | See what a candidate fix does, on a fresh build. |
| `peeky sql "SELECT ... FROM last_run"` / `peeky queries [name]` | instant | Follow-ups on saved evidence. No rerun. |
| `peeky session start <target> --at <path>:<line> [--hit N]` | one run | Several looks at the same stop (`--hit N`: the Nth). |
| `peeky session inspect` / `get a.b 'c[0]' "d['k']"` / `set x=0` | instant | List, read and change values at the stop. |
| `peeky session step [--into\|--out]` / `continue [--to LINE\|--count N\|--to-exit]` / `wait` | instant | Move the program on; `wait` waits for a running one to stop. |
| `peeky session report` / `list` / `close` | instant | What happened; the open sessions; end one. |
| `peeky verify <target> [--at <file>] --check <checker.py> --old '...' --new '...'` / `--baseline` | one build | Verify an edit on a fresh build; `--baseline` checks the unedited program. |
| `peeky job wait <id>` | instant | Collect a `--background` probe or verify. |

- **Program arguments:** after `--` or repeated `--arg` (`--arg=-v`). They replace a config's
  `arguments` (not for attached JVMs). `session start` takes them too.
- **`--hits all`** captures every stop, up to 100 (fewer past 20 selectors). It needs the program
  to exit. **`--capture return`** gives a `(return)` row per call of the line's function: probe
  its first line. Not in Python. Deep recursion is slow: limit `--hits`.
- **`--json`** works on every command. stdout is then one JSON value; progress goes to stderr, and
  `--quiet` silences it.
- **Sessions:** each command waits up to `--timeout S` (default 30) for a stop, leaving the
  program running (`session wait`). A session's worker ends on `close`, or after 5 idle minutes.
  Records live in `$PEEKY_STATE_DIR` (default `~/.peeky`): set it per task to keep sessions apart.
- **In a session:** `get` also lists a value's children, each with the selector that reads it. It
  exits 1 if any selector failed. `--frame N` reads caller frame N.
- **Stepping:** `step` runs to the next line; `--into` enters a call; `--out` leaves the function
  and shows its return, as `get return` does (not in Python). Java's steps skip JDK classes.
  `outside_program: true` means a step left your sources (only `get return` reads there): use
  `step --out` or `continue`.
- **Continuing:** `continue` runs to the next stop; `--count N` to the Nth breakpoint stop, where
  `session start --hit N` starts. States show `hit N` there. Both stop at anything else first (a
  crash, the exit), with a note. `--to LINE` also stops at LINE of the session's file (rule 9).
  `--to-exit` clears breakpoints and runs to the end.
- **Candidates** (`--old`/`--new`) work on any target but an attached JVM, and not with `--run`:
  use `verify` there. `--at` keeps the original's line numbers; a line inside an edit that adds or
  removes lines is refused. An edit that doesn't compile is `BUILD_FAILED`: exit 2, not saved
  (`verify`: exit 1; Python: `NO_STOP` with its `SyntaxError`).

### Interventions (`--set name=value`)

- **What:** the stopped function's own variables. In C, C++ and Rust, also integer fields and
  elements (`s.end`, `xs[2]`, `t.__0`) of a struct, tuple or array local the frame holds itself. In
  Rust, that local must be in memory and MIR-proven initialized. In Java, also scalar fields and
  array elements reached from locals (`current.end=10`, `workers[1].load=5`). A text in double
  quotes (`--set 'name="apple"'`) changes a string, where the debugger can write it. Each language's
  types, values and texts: its page, and 2.3.
- **Never** through a pointer, reference or Box, nor in a union, enum, packed, std or JDK type,
  bitfield or const.
- **Where:** a change writes the variable's storage. The program's copies keep the old value
  (`let a = s.x;`, a statement under way), and the stop's line may overwrite the change. So C, C++
  and Rust allow a change only where Peeky proves none is under way (rule 3). Rust also refuses
  where the program goes on with a copy or never reads it again (7.2).
- **Refused before the run:** what the build rules out, such as a Rust local kept in a register
  (`[not settable: register]`, listed by `describe`), or a field through a pointer. So is a Java
  local whose declared type isn't supported, a value that doesn't fit, or a local never read after
  the stop. One refusal fails the probe, and Rust's lists what is settable there.
- **Checks:** every change is read back. One the debugger refuses or can't store is
  `MUTATION_REJECTED`, and the session is kept; a value stored differently closes it. Expressions, names and calls are
  rejected, never evaluated (2.3).

### Reading probe results

- **Read `warnings` every time.** They flag:
  - a moved breakpoint, in a probe or `session start` (values come from the line it names);
  - a line that stops several times per pass, or a loop header only before its loop (rule 5);
  - runs that ended before all requested hits;
  - a Java exit Peeky inferred when the debugger dropped its exit event (the result stands).
- `unavailable: true`, `no value yet:` and `note`: rule 6. Opaque values: rule 15.
- Each row's `function` names the stopped function. A line shared with a closure or lambda can
  stop in each (Java: in one; 7.3).
- `output_complete: false`: `output` isn't proven whole (2.6). `--until hits` adds
  `output_after_last_hit: true`: it ran past its last hit.
- `--json`: `by_hit` has `{hit, line, observed: {selector: value}, after-set: {...}}` per hit;
  `rows`, one per hit, phase and selector. `line` is the stop's actual line.
- A missing selector's error lists the visible names, or the value's children. `describe` lists
  each function's names (`scopes`), or says why it can't (`scopes_unavailable`).
- Python reads a global only where the function provably uses it, not for an unassigned local.
  Reading one, or a misspelled name, lists all globals, running all reprs. Dunders read too
  (`f.__defaults__`): 5.1; `[id 7]`: 4.6.
- debugpy lists a long list's first 100 items: a higher index is "beyond the debugger's visible
  window", not missing. An index from the end (`items[-1]`) needs the sequence's length and every
  element shown. Otherwise it is an error, never a guess.
- Failures carry `error: {code, message, hint, retryable, session_closed}`. `status` is
  `completed`, `no-stop`, `failed` or `timeout`.

### Querying saved evidence (`peeky sql`, `peeky queries`)

Probes not refused before launch are saved (`evidence.run_id`). **SQL is optional.** For one
probe, read its JSON.

- `peeky sql --schema` lists the tables and views. `--schema NAME ...` (or `all`) prints them
  in full (4.1). Read it before writing SQL.
- `peeky queries` needs no SQL: `runs`, `run`, `wide`, `hits`, `compare`, `join` (hits paired by
  values: `key=v` or `key=a,b`), `diff` (what differs, real differences first: §3), `outputs`,
  `set_effect`, `values`, `unreadable`, `failures`. Pairs of runs: `--param a=1 --param b=5`
  (default: the last two); one run: `run=3`. `queries --help` lists their parameters.
- Compare numbers with `num`; non-Python strings and lists: `text()`, `elements()` (4.9). Mask
  addresses with `no_addresses(value)`. `object_ref(value)` tells a Java object from a copy (4.3 to
  4.8). `--label` tags a run; it never changes which runs `diff` or `compare:` pair.
- Queries are read-only and bounded; a failing one returns `INVALID_QUERY`. A new question needs
  a new probe.

### Verifying an edit

- With several `--old`/`--new` pairs, each `--old` matches the original file exactly once, and
  the edits must not overlap (rule 13). An error names the edit (`(edit 2)`) and why. Multi-line
  text: 6.3. `--at` names the file to edit when the target is a directory.
- The result has `stage` (`build` or `verification`), `seconds`, and the unified `diff` applied. On
  failure it has a `reason`: the checker's failure line if it only exited nonzero, else its
  exception or the build diagnostic. `--timeout S` bounds the whole verify, build included
  (`TIMEOUT`). `--keep`, failed Cargo tests and `--details`: 6.1.
- At `stage: build`, read the compiler diagnostics and try again. Provenance or toolchain errors are
  fatal: stop and report them.

### Targets and selectors

- **Targets:** each kind is in [Your first command](#your-first-command). A lone `.py`, `.c` or
  `.cpp` file works too. `describe`'s commands repeat a `--config`. `session start` takes `--run`
  too, and `verify` edits the `--at` file.
- **Remote targets:** for a worker on another host behind an SSH tunnel, add
  `--worker http://127.0.0.1:<port>` to `probe` or `session start`, with the token in
  `$PEEKY_WORKER_TOKEN`. Your local source must match the worker's, or the results are discarded.
  See [REMOTE.md](REMOTE.md).
- **Selectors** are dotted paths, indexes and keys: `request.page`, `args[0]`, `order['id']`.
  C++ `m[1]` is a map's value for key 1, and `m[#0]` its first entry. A shadowed local goes as LLDB
  names it: `s @ main.rs:81.a`. A probe's `1:i` is `i` in frame 1, the caller, read as
  `get --frame 1` does (2.10). Expressions are rejected. In zsh, quote selectors with `[...]`.

## Time: your tool timeout and the program's clocks

- **Use `--background` for long calls** (JVM startup, builds, slow programs). `probe` or `verify`
  then returns `{"job": "<id>"}` at once. Repeat `peeky job wait <id> [--timeout 20]` until the
  exit code is not **3**. Its output and exit code are the foreground command's (8).
- **Prefer probes for programs with clocks.** Timeouts and deadlines keep running while the
  program is paused. A probe pauses for milliseconds per hit (`pauses_ms`).
- **Sessions on timed programs:** pass `session start --max-pause S` (attached JVMs: 10 s). A
  breakpoint stop older than S is resumed, and your next call on it fails with `STALE_STOP`.
  Crash and exception stops never are. `inspect` shows `paused_s` and `resume_in_s` (5.4).

## When something fails

Errors have a stable `code` and a `hint` ([ERRORS.md](ERRORS.md)). Use `--json` to read them,
and follow the hint.

Exit codes:

- 0: success.
- 1: a completed command with a negative result (probe failed or no-stop, verify did not pass).
- 2: an error or a refusal (a probe refused before launch).
- 3: not there yet (a background job still running; a session's `--to`, `--count` or `--hit`
  stopped first).
- 4: a probe change never made (`unapplied`).

Never repeat a `set` or other change after `TIMEOUT` or `DEBUGGER_ERROR`: the outcome is
uncertain, so start a new session. `NO_STOP`: rule 1.
