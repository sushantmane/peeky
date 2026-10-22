# Peeky agent guide

You debug programs with **peeky**, a standalone tool. You decide what to do; peeky runs the
debugger safely and returns evidence. Every claim you make must cite a peeky command and its
output. This guide is the common path. [COOKBOOK.md](COOKBOOK.md) and your language's cookbook
have every rule, cited here by section (7.1: the same number in both). [ERRORS.md](ERRORS.md) has
the error codes.

## First 5 minutes

Start with your language's page: [Python](lang/python.md), [Java](lang/java.md),
[C and C++](lang/c-cpp.md), [Rust](lang/rust.md).

1. **Learn the target:** `peeky describe examples/pagination_bug.py`. Look for `breakpoint lines`,
   each function's lines and names (`names in get_page (lines 4-9): …`), and `mutable:`: what
   `--set` can change. Pick a line where the suspicious value has just been computed.
2. **Watch it:** `peeky probe examples/pagination_bug.py --at 8 --capture start,page --hits all`.
   You get one row per stop (`hit`, `value`), `status: completed`, the program's `output`, and
   every `warning:`.
3. **Ask follow-ups, with no rerun** (rule 12): `peeky queries diff` after probing two inputs
   (`--label` each), `queries wide` (a row per hit, a column per selector), or
   `peeky sql "SELECT hit, num FROM last_run WHERE selector = 'start'"`.
4. **Test a hypothesis:** add `--set start=0` to the probe. Look for `set start: 3 -> 0`, the
   `after-set` rows, and whether the output changed as predicted. It is an experiment, not a fix.
5. **Try a fix:** add `--old 'start = page * page_size' --new 'start = (page - 1) * page_size'` to
   the probe. Values come from a fresh build with the edit; your files are unchanged. Its
   `compare:` line names the `queries diff` against the unedited run, or says how to make one.
   Write the general fix, not a special case for the observed input.
6. **Check the checker:** `peeky verify examples/pagination_bug.py --check
   examples/verify_pagination.py --baseline` must say `passed: False`. A checker that passes the
   bug can't tell a fix from it.
7. **Verify:** run the same with `--old '…' --new '…'` instead of `--baseline`. Look for
   `passed: True  stage: verification`, or the failing `stage` and `reason`. Only this verifies a
   fix.
8. **Several looks at one stop:** `peeky session start <target> --at <file>:<line>`, then
   `session inspect`, `get`, `step` and `continue`. Always end with `session close`.

Report the observation, the intervention and its effect, the edit and the verify result. Quote
the commands.

## Rules you will hit

1. **A breakpoint stops before its line runs.** To see what a line assigns, stop on the next line.
   Probe only lines `describe` lists. `NO_STOP` means the line never ran (10).
2. **Set a field in the frame that owns the struct.** Peeky never writes through a pointer or
   reference (`r->tail` in `push(struct ring *r)`, Rust `self.k`, `this`). Use the call a refusal
   names (`--set-at LINE@N`) only where it says `settable`. Otherwise follow the refusal: read
   `describe`'s `mutable:` line, or use a candidate edit (7.1, 7.2).
3. **Change only where nothing is under way:** C/C++ at a statement's first line, Rust where the
   MIR proves it, and never after `step --out`. Rows mark what can change `[settable]`. A probe
   whose change its build rules out is refused before it runs, with exit 2 (2.3, 7.1 to 7.3).
4. **`--set` applies once, at hit 1.** Use `--set-hit N` for another; other hits only observe.
   `--set-at LINE@HIT` changes at that line's hit, the `--at` line's too; repeat it per hit (2.4,
   2.8).
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
   - Rust: `[stale]` is a moved local's leftover bytes. Raw fields with a note are contents Peeky
     could not prove (7.2). `unproven_pointer`: `raw` (a raw pointer) or `unknown` (maybe one).
   - Optimized builds may show stale values.
7. **Rust reads `Vec`/`String` contents it can prove alive.** Those are owned locals, reference
   parameters of plain elements, `&str` loop items, and such fields: `self.values`, `entry.name`.
   Behind `&mut` or owned, they read only in the innermost frame. A crate with `unsafe` code or a
   nightly feature gets none: raw fields that note why (7.2).
8. **Members need `this.`** in C++ and Java (`this.head_`: 7.1, 7.3). Python dict entries are
   `d['key']` (2.1).
9. **`continue --to LINE` doesn't skip the session's breakpoint.** A stop there comes first (exit
   3, with a note): repeat the command (5.2).
10. **Steps land where the code jumps:** a closing `}`, a loop header, or back on an `if`'s line.
    Read a block's locals before stepping past its last statement (1.1, 5.2).
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
    `budget-exhausted` mean that peeky refused to guess. Don't invent their contents.

## Principles

1. **Observe before changing.** Stop at a line and read real values before you form a hypothesis.
2. **Interventions are experiments, not fixes.** `--set` or `session set` changes one live value in
   one run. It tells you whether a hypothesis explains the behavior; it never proves a source fix.
3. **Only `peeky verify` verifies a fix.** It rebuilds your candidate edit from scratch and runs
   the independent checker. For Rust it also runs the crate's integration test, if any. Report
   `passed: True` only from `verify`.
4. **Never guess lines or names.** Use `describe`'s breakpoint lines and the names you inspected.

## Commands

| Command | Cost | Use |
| --- | --- | --- |
| `peeky describe <target> [--json]` | one build | Language, valid breakpoint lines, qualified views and mutation rules. Always first. |
| `peeky probe <target> --at <path>:<line> --capture a,b.c [--set x=0] [--hits N\|all] [--json] [-- ARGS]` | one run | Answer one question in one call: values at a line, after any interventions, and the output. |
| `peeky probe ... --old '<exact text>' --new '<replacement>'` | one build | See what a candidate fix does, on a fresh build; your files are not changed. |
| `peeky sql "SELECT ... FROM last_run"` / `peeky queries [name]` | instant | Follow-ups on saved evidence. No rerun. `peeky sql --schema` lists the tables. |
| `peeky session start <target> --at <path>:<line> [--hit N]` | one run | Several looks at the same stop (`--hit N`: the Nth). |
| `peeky session inspect` / `get a.b 'c[0]' "d['k']"` / `set x=0` | instant | List, read and change values at the stop. |
| `peeky session step [--into\|--out]` / `continue [--to LINE\|--count N\|--to-exit]` / `wait` | instant | Move the program on; `wait` waits for a running one to stop. |
| `peeky session report` / `list` / `close` | instant | What happened; the open sessions; end one. Always `close`. |
| `peeky verify <target> [--at <file>] --check <checker.py> --old '...' --new '...'` / `--baseline` | one build | Verify an edit on a fresh build; `--baseline` checks the unedited program. |
| `peeky job wait <id>` | instant | Collect a `--background` probe or verify. |

- **Program arguments:** after `--` or repeated `--arg` (`--arg=-v`). They replace a config's
  `arguments` (not for attached JVMs). `session start` takes them too.
- **`--hits all`** captures every stop, up to 100 (fewer past 20 selectors). It needs the program
  to exit. **`--capture return`** gives a `(return)` row per call of the line's function: probe
  its first line. Not in Python. Deep recursion is slow: limit `--hits`.
- **`--json`** works on every command. stdout is then one JSON value; progress goes to stderr, and
  `--quiet` silences it.
- **Sessions:** pass `--name` to every session command (default `default`). Starting a name in use
  fails with "exists". Each command waits up to `--timeout S` (default 30) for a stop, leaving the
  program running (`session wait`). A session's worker ends on `close`, when its start fails or is
  interrupted, or after 5 idle minutes. Records live in `$PEEKY_STATE_DIR` (default `~/.peeky`):
  set it per task to keep sessions apart.
- **In a session:** `get` also lists a value's children, each with the selector that reads it. It
  exits 1 if any selector failed. `--frame N` reads caller frame N.
- **Stepping:** `step` runs to the next line; `--into` enters a call; `--out` leaves the function
  and shows its return, as `get return` does (not in Python). Java's steps skip JDK classes.
  `outside_program: true` means a step left your sources (only `get return` reads there): use
  `step --out` or `continue`.
- **Continuing:** `continue` runs to the next stop; `--count N` to the Nth breakpoint stop.
  `session start --hit N` starts at the Nth stop. States show `hit N` at the session's breakpoint.
  `--count` and `--hit` stop at anything else (a crash, a step, the exit) with a note. `--to LINE`
  also stops once at LINE of the session's file (rule 9). `--to-exit` clears breakpoints and runs
  to the end.
- **Candidates** (`--old`/`--new`) work on any target but an attached JVM. `--at` keeps the
  original's line numbers; a line inside an edit that adds or removes lines is refused. An edit
  that doesn't compile is `BUILD_FAILED`: exit 2, not saved (`verify`: exit 1; Python: `NO_STOP`
  with its `SyntaxError`). Compare with an unedited run (`peeky queries diff`). A candidate is
  evidence, not verification.

### Interventions (`--set name=value`)

- **What:** the stopped function's own variables. In C, C++ and Rust, also integer fields and
  elements (`s.end`, `xs[2]`, `t.__0`) of a struct, tuple or array local the frame holds itself. In
  Rust, that local must be in memory and MIR-proven initialized. In Java, also scalar fields and
  array elements reached from locals (`current.end=10`, `workers[1].load=5`). A text in double
  quotes (`--set 'name="apple"'`) changes a string, where the debugger can write it. Each language's
  types, values and texts: its page, and 2.3.
- **Never** through a pointer, reference or Box, nor in a union, enum, packed, std or JDK type,
  bitfield or const.
- **Where:** a change writes the variable's storage, which later reads see. The program's copies
  keep the old value (`let a = s.x;`, a statement under way), and the stop's line may overwrite the
  change. So C, C++ and Rust allow a change only where Peeky proves none is under way (rule 3).
  Rust also refuses where the program goes on with a copy (7.2).
- **Refused before the run:** what the build rules out, such as a Rust local kept in a register
  (`[not settable: register]`, listed by `describe`), or a field through a pointer. So is a Java
  local whose declared type isn't supported, a value that doesn't fit, or a local never read after
  the stop. One refusal fails the probe, and ends with what is settable there
  (`settable here: total, i`, or `none`).
- **Checks:** `session inspect` says why when nothing is `[settable]`; a Java row says why it isn't.
  Every change is read back. One the debugger refuses or can't store is `MUTATION_REJECTED`, and
  the session is kept. A value stored differently closes the session. Expressions, names and calls
  are rejected, never evaluated (2.3).

### Reading probe results

- **Read `warnings` every time.** They flag:
  - a breakpoint the debugger moved to another line (values come from that line);
  - a line that stops several times per pass, or a loop header only before its loop (rule 5);
  - runs that ended before all requested hits;
  - a Java exit Peeky inferred when the debugger dropped its exit event (the result stands).
- `unavailable: true`, `no value yet:` and `note`: rule 6. Opaque values: rule 15.
- Each row's `function` tells a function from a generator or lambda on the same line. Session
  states and `inspect` name the stopped function. A line shared by a function and a closure or
  lambda can stop in each (Java: in one; 7.3).
- `output_complete: false`: `output` isn't proven whole (2.6). `program_ended: true`: the program
  ended. `--until hits` (for slow exits) adds `output_after_last_hit: true`: the program ran on
  past its last hit.
- `by_hit` groups values per hit: `{hit, line, observed: {selector: value}, after-set: {...}}`. A
  failed selector shows `{"error": ...}`. `rows` holds the same, one entry per hit, phase and
  selector. `phase` is `observed`, `after-set` (after your intervention) or `returned`. `line` is
  the stop's actual line.
- How each language shows structs, objects and containers, and what Rust reads from memory: your
  language's page.
- A missing selector's error lists the visible names, or the children of the value read.
  `describe` lists each function's lines and the names it can see (`scopes`; types except in
  Python). `scopes_unavailable` says why its debug information could not be read.
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
probe, read its JSON. Saved runs help with large captures, comparisons (with and without `--set`),
and slow or unrepeatable runs.

- `peeky sql --schema` lists the tables, views and canned queries (4.1). Read it before writing
  SQL.
- `peeky queries` needs no SQL. Its queries: `run`, `wide` (a row per hit), `hits`, `compare`, and
  `join` (two runs' hits paired by values: `key=v` or `key=a,b`). Also `diff` (what differs, first
  divergence first: §3), `outputs`, `set_effect`, `values`, `unreadable` and `failures`. `--param
  a=1 --param b=5` (default: the last two) or `run=3` picks runs.
- Compare numbers with `num`; `value` is the debugger's text. Mask addresses with
  `no_addresses(value)`. `object_ref(value)` tells a Java object from a copy (4.3 to 4.6). `--label`
  tags a run with free text, stored as given; it never changes which runs `diff` or `compare:`
  pair.
- Queries are read-only and bounded; a failing one returns `INVALID_QUERY`. Saved evidence
  describes past runs only: a new question about the program needs a new probe.

### Verifying an edit

- With several `--old`/`--new` pairs, each `--old` must match exactly once, quoting the original
  file, and the edits must not overlap (rule 13). An error names the edit by number (`(edit 2)`)
  and the failed condition: the match count and lines, or `--new equals --old`. Multi-line text:
  `--old-file`/`--new-file`, or real newlines (6.3). `--at` names the file to edit when the target
  is a directory (a Cargo crate, a Python project).
- The result has `stage` (`build` or `verification`), `seconds`, and the unified `diff` applied. On
  failure it has a `reason`: the checker's failure line if it only exited nonzero, else its
  exception or the build diagnostic. `--timeout S` bounds the whole verify, build included
  (`TIMEOUT`). `--keep`, failed Cargo tests and `--details`: 6.1.
- At `stage: build`, read the compiler diagnostics and try again. Provenance or toolchain errors are
  fatal: stop and report them.

### Targets and selectors

- **Targets:**
  - a `.py` file;
  - a `.c`/`.cpp` file, or a directory holding one C/C++ program;
  - a Cargo crate directory with its `Cargo.lock` (a plain `cargo new` binary crate works; shape:
    7.2);
  - any target with `--config <trusted.json>`, which Java requires (`describe`'s commands repeat
    it). A Java attach config attaches to a running `--debug-jvm` JVM (7.4).
- **Python projects:** pass the project directory and `--at <module.py>:<line>`. For a module that
  is imported, not run, add `--run <script.py>` ([Python](lang/python.md)). `session start` takes
  `--run` too, and `verify` edits the `--at` file.
- **Remote targets:** for a worker on another host behind an SSH tunnel, add
  `--worker http://127.0.0.1:<port>` to `probe` or `session start`, with the token in
  `$PEEKY_WORKER_TOKEN`. Your local source must match the worker's, or the results are discarded.
  See [REMOTE.md](REMOTE.md).
- **Selectors** are dotted paths, indexes and keys: `request.page`, `args[0]`, `order['id']`.
  C++ `m[1]` is a map's value for key 1, and `m[#0]` its first entry. A shadowed local goes as LLDB
  names it: `s @ main.rs:81.a`. Expressions are rejected. In zsh, quote selectors with `[...]`.

## Time: your tool timeout and the program's clocks

- **Use `--background` for long calls.** If a call may outlast your tool timeout (JVM startup,
  compiled builds, slow programs), add `--background` to `probe` or `verify`. It returns
  `{"job": "<id>"}` at once. Then repeat `peeky job wait <id> [--timeout 20]` until the exit code
  is not **3**. The final stdout, stderr and exit code are those of the foreground command;
  `job wait --json` prints JSON however the job was started.
- **Prefer probes for programs with clocks.** Timeouts, heartbeats and RPC deadlines keep running
  while the program is paused. A probe pauses for milliseconds per hit (`pauses_ms`), with no
  model turn in between.
- **Sessions can't wait for you on timed programs.** Pass `session start --max-pause S`; attached
  JVMs default to 10 s. A breakpoint stop older than S is auto-resumed, and your next call on it
  fails with `STALE_STOP`. Crash and exception stops (`reason` other than `breakpoint`) are never
  auto-resumed. `inspect` shows `paused_s` and `resume_in_s`.
- **`peeky describe` reports this target's timing contract** in its `timing` field.

## When something fails

Errors have a stable `code`, a `hint`, `retryable` and `session_closed` (see
[ERRORS.md](ERRORS.md)). Use `--json` to read them, and follow the hint.

Exit codes:

- 0: success.
- 1: a completed command with a negative result (probe failed or no-stop, verify did not pass).
- 2: an error or a refusal (a probe refused before launch).
- 3: not there yet (a background job still running; a session's `--to`, `--count` or `--hit`
  stopped first).
- 4: a probe change never made (`unapplied`).

Never repeat a `set` or other change after `TIMEOUT` or `DEBUGGER_ERROR`: the outcome is
uncertain, so start a new session. `NO_STOP` from a probe means that your line never ran: choose
another from `describe`.
