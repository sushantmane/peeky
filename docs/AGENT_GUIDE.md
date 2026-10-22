# Peeky agent guide

You debug programs with **peeky**, a standalone tool. You decide what to do; peeky runs the debugger safely and returns evidence. Every claim you make must cite a peeky command and its output. This guide is the common path; [COOKBOOK.md](COOKBOOK.md) and your language's cookbook have every rule (cited by section: 7.1, the same number in both), [ERRORS.md](ERRORS.md) the error codes.

## First 5 minutes

Start with your language's page: [Python](lang/python.md), [Java](lang/java.md), [C and C++](lang/c-cpp.md), [Rust](lang/rust.md).

1. **Learn the target:** `peeky describe examples/pagination_bug.py`. Look for `breakpoint lines`, each function's lines and names (`names in get_page (lines 4-9): …`) and `mutable:` (what `--set` can change). Pick a line where the suspicious value has just been computed.
2. **Watch it:** `peeky probe examples/pagination_bug.py --at 8 --capture start,page --hits all`. One row per stop (`hit`, `value`), `status: completed`, the program's `output`, and every `warning:`.
3. **Ask follow-ups:** `peeky queries diff` after probing two inputs (`--label` each), `queries wide` (a row per hit, a column per selector), or `peeky sql "SELECT hit, num FROM last_run WHERE selector = 'start'"`. No rerun (rule 12).
4. **Test a hypothesis:** add `--set start=0` to the probe. Look for `set start: 3 -> 0`, the `after-set` rows, and whether the output changed as predicted. It is an experiment, not a fix.
5. **Try a fix:** add `--old 'start = page * page_size' --new 'start = (page - 1) * page_size'` to the probe. Values come from a fresh build with the edit (Python: an edited copy; your files are unchanged); its `compare:` line names the `queries diff` against the unedited run, or says how to make one. Write the general fix, not a special case for the observed input.
6. **Check the checker:** `peeky verify examples/pagination_bug.py --check examples/verify_pagination.py --baseline` must say `passed: False`; a checker that passes the bug cannot tell a fix from it.
7. **Verify:** the same with `--old '…' --new '…'` instead of `--baseline`. `passed: True  stage: verification`, or the failing `stage` and `reason`. Only this verifies a fix.
8. **Several looks at one stop:** `peeky session start <target> --at <file>:<line>`, then `session inspect`, `get`, `step`, `continue`, and always `session close`.

Report the observation, the intervention and its effect, the edit, and the verify result. Quote the commands.

## Rules you will hit

1. **A breakpoint stops before its line runs:** to see what a line assigns, stop on the next. Probe only lines `describe` lists; `NO_STOP` means the line never ran (10).
2. **Set a field in the frame that owns the struct.** Peeky never writes through a pointer or reference (`r->tail` in `push(struct ring *r)`, Rust `self.k`, `this`). Use the call a refusal names (`--set-at LINE@N`) only where it says `settable`; else follow it (`describe`'s `mutable:` line, a candidate edit) (7.1, 7.2).
3. **Changes only where nothing is under way:** C/C++ at a statement's first line, Rust where the MIR proves it, never after `step --out`. Rows mark `[settable]`; a probe whose change its build rules out is refused before it runs (exit 2) (2.3, 7.1 to 7.3).
4. **`--set` applies once, at hit 1** (`--set-hit N` for another); `--set-at LINE@HIT` changes at another line's hit (2.4, 2.8).
5. **Hits count stops, not passes.** A line with a macro's or a block's code, or a closure, can stop several times per pass: read `warnings`, each row's `stop` and `function`. A loop header may stop only before its loop: probe the body's first line (2.2).
6. **A `note`, `no value yet:` or `unavailable: true` is not a value.** C and C++: read a local below its declaration, and a `note` on a line a loop returns to (a `do` body's first) on the next line; below a `setjmp` call, a `note` replaces `no value yet`: read it below its declaration too. Python: a `note` marks a local the pass assigns below (5.1). Rust: `[stale]` is a moved local's leftover bytes, and raw fields with a note are contents Peeky could not prove (7.1, 7.2).
7. **Rust reads `Vec`/`String` contents it can prove alive:** owned locals, reference parameters of plain elements, `&str` loop items, and such fields (`self.values`, `entry.name`; `&mut`/owned: innermost frame only). A crate with `unsafe` code or a nightly feature gets none: raw fields noting why (7.2).
8. **Members need `this.`** in C++ and Java (`this.head_`: 7.1, 7.3); Python dict entries are `d['key']` (2.1).
9. **`continue --to LINE` does not skip the session's breakpoint:** a stop there comes first (exit 3, with a note); repeat it (5.2).
10. **Steps land where the code jumps:** a closing `}`, a loop header, back on an `if`'s line. Read a block's locals before stepping past its last statement (1.1, 5.2).
11. **Sessions need `--name` on every command** (unless `default`) and a `close`; their programs' clocks run while you think, so prefer probes for timed code (5.3, 5.4).
12. **`queries diff` pairs the last two runs by hit number.** Name runs (`--param a=1 --param b=5`); check the `(output)`, `(args)`, `(edit)` and `(sets)` rows before crediting an edit; after a candidate, run its `compare:` command; when hits do not line up (a sort's comparator), use `queries join` (3.1, 3.5).
13. **`--old` must match the source exactly once**, with real newlines (`--old-file` for long text: 6.3), and goes before `--`, as everything after it goes to the program (2.5, 10).
14. **Same-named locals (C, C++):** a plain name means the one the code names at the stop; `name @ file:line` selects another (7.1). Rust: as `describe` lists it (`x @ main.rs:6`).
15. **Respect opaque values.** `<unqualified Rust enum layout>`, `<unverified ...>` or `budget-exhausted` mean peeky refused to guess. Do not invent their contents.

## Principles

1. **Observe before changing.** Stop at a line and read real values before hypothesizing.
2. **Interventions are experiments, not fixes.** `--set` or `session set` changes one live value in one run: it tells you whether a hypothesis explains the behavior, and never proves a source fix.
3. **Only `peeky verify` verifies a fix.** It rebuilds your candidate edit from scratch and runs the independent checker (and the crate's integration test for Rust, when it has one). Report `passed: True` only from `verify`.
4. **Never guess lines or names.** Use `describe`'s breakpoint lines and the names you actually inspected.

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

- **Program arguments:** after `--` or repeated `--arg` (`--arg=-v`), replacing a config's `arguments` (not for attached JVMs); `session start` too.
- **`--hits all`** captures every stop up to 100 (fewer past 20 selectors); it needs the program to exit. **`--capture return`**: a `(return)` row per call of the line's function (probe its first line; not Python; deep recursion is slow: limit `--hits`).
- **Output:** every command takes `--json`: stdout is then one JSON value (progress on stderr; `--quiet` silences it).
- **Sessions:** every session command takes its `--name` again (default `default`); starting a name in use fails with "exists". Each waits up to `--timeout S` (default 30) for a stop, leaving the program running (`session wait`). A session's worker ends on `close`, when the start fails or is interrupted, or after 5 idle minutes. Session records live in `$PEEKY_STATE_DIR` (default `~/.peeky`): set it per task to keep sessions apart.
- **In a session:** `get` also lists a value's children, each with the selector that reads it, and exits 1 if any selector failed. `--frame N` reads caller frame N.
- **Stepping:** `step` runs to the next line (`--into` a call, `--out` of the function, showing its return as `get return` does (not in Python); Java's skip JDK classes). `outside_program: true`: a step left your sources (only `get return` reads); `step --out` or `continue`.
- **Continuing:** `continue` runs to the next stop (`--count N`: the Nth breakpoint stop); `session start --hit N` starts at the Nth stop; states show `hit N` at the session's breakpoint. `--count` and `--hit` stop at anything else (a crash, a step, the exit) with a note; `--to LINE` also stops once at LINE of the session's file (an earlier stop says so: repeat it); `--to-exit` clears breakpoints and runs to the end.
- **Candidates:** any target but an attached JVM. `--at` keeps the original's line numbers (a line inside an edit that adds or removes lines is refused). A non-compiling edit is `BUILD_FAILED`: exit 2, not saved (`verify`: exit 1; Python: `NO_STOP` with its `SyntaxError`). Compare with an unedited run (`peeky queries diff`). It is evidence, not verification.

### Interventions (`--set name=value`)

- **What:** root locals (the stopped function's own variables); in C, C++ and Rust also integer fields and elements (`s.end`, `xs[2]`, `t.__0`) of a struct, tuple or array local the frame holds itself (Rust: in memory, MIR-proven initialized); in Java scalar fields and array elements reached from locals (`current.end=10`, `workers[1].load=5`), a char (`'+'` or a code point; 7.3). A text in double quotes (`--set 'name="apple"'`) changes a `char[N]` or `std::string` local within its size, a Rust `String` within its capacity while no borrow of it is live and the crate has no `unsafe`, or a Java String, where the debugger can write it (7.1 to 7.3). Never through a pointer, reference or Box, nor in a union, enum, packed, std or JDK type, bitfield or const. Values and widths per language: 2.3.
- **Where:** a change writes the variable's storage, which later reads see; the program's copies keep the old value (`let a = s.x;`, a statement under way), and the stop's line may overwrite it. So C, C++ and Rust allow one only where Peeky proves none is under way (C: at a statement's first instruction; Rust: by MIR, nor where it goes on with a copy: 7.2), never after `step --out`. Before running, a probe refuses what its build rules out: a Rust local kept in a register (`[not settable: register]`; `describe` lists them), a field through a pointer, a Java local's type or a value it cannot hold.
- **When:** `--set` applies once, at hit 1 (`--set-hit N`: later); other hits only observe. `--set-at LINE@HIT x=0` (repeatable) changes at that hit of its own line: change at 22, restore at 23 (2.8). One refusal fails the probe, and ends with what is settable there (`settable here: total, i`, or `none`).
- **Checks:** rows mark what `set` can change `[settable]`; `session inspect` says why when nothing is. Every change is read back: one the debugger refuses or can't store is `MUTATION_REJECTED` with the session kept; a value stored differently closes the session. Expressions, names and calls are rejected, never evaluated (2.3).

### Reading probe results

- A row with `unavailable: true` has no value at that line (optimized out, not live); never treat its error text as a value. A local whose storage may hold leftovers is withheld (`no value yet:` and why) and cannot be `--set` (C and C++ cases: 7.1; Rust: 7.2). **A row with a `note` is a value Peeky cannot vouch for**, as is all read through it (rule 6). Optimized builds may show stale values.
- **Read `warnings` every time.** They flag a breakpoint the debugger moved to another line (values come from that line); a line that stops several times per pass, or a loop header only before its loop (rule 5); runs that ended before all requested hits; a Java exit Peeky inferred when the debugger dropped its exit event (the result stands).
- Each row's `function` tells a function from a generator or lambda on the same line; session states and `inspect` name the stopped function. A line shared by a function and a closure or lambda can stop in each (Java: in one; 7.3).
- `output_complete: false` means the probe ended (`--until hits`) before the program did, so `output` holds only what was printed so far. `--until hits` saves the most for slow exits (JVMs, Python).
- `by_hit` groups values per hit: `{hit, line, observed: {selector: value}, after-set: {...}}`; a failed selector shows `{"error": ...}`. `rows` holds the same, one entry per hit, phase and selector. `phase` is `observed`, `after-set` (after your intervention) or `returned`. `line` is the stop's actual line.
- Values: C and C++ structs, arrays and `std::` containers show their first members (`{lo = 0, hi = 2}`; unions and pointer targets are not read); launched Java objects, arrays and `ArrayList`s their first fields or elements (7.3); Rust reference parameters, owned `Vec`/`String`/struct locals and container fields read from memory (`[memory]`; rule 7), anything else raw with a note (7.2).
- A missing selector's error lists the visible names, or the children of the value read. C++ and Java members are read through `this` (`this.head_`). Python reads globals it provably uses (not an unassigned local; one, or a misspelled name, lists all, running all reprs) and dunders (`f.__defaults__`): 5.1; `[id 7]`: 4.6. `describe` lists each function's lines and the names it can see (`scopes`; types except in Python); `scopes_unavailable` says why its debug information could not be read.
- debugpy lists a long list's first 100 items: a higher index is "beyond the debugger's visible window", not missing. An index from the end (`items[-1]`) needs the sequence's length and every element shown; otherwise it is an error, never a guess.
- Failures carry `error: {code, message, hint, retryable, session_closed}`. `status` is `completed`, `no-stop`, `failed` or `timeout`.

### Querying saved evidence (`peeky sql`, `peeky queries`)

Probes not refused before launch are saved (`evidence.run_id`). **SQL is optional**: for one probe, read its JSON; saved runs help with large captures, comparisons (with and without `--set`) and slow or unrepeatable runs.

- `peeky sql --schema` lists the tables (`runs`, `captures`, `interventions`, `pauses`), the `evidence` and `last_run` views, and the canned queries. Read it before writing SQL.
- `peeky queries` needs no SQL: `run`, `wide` (a row per hit), `hits`, `compare`, `join` (two runs' hits paired by values: `key=v` or `key=a,b`), `diff` (what differs, first divergence first: §3), `outputs`, `set_effect`, `values`, `unreadable`, `failures`. `--param a=1 --param b=5` (default: the last two) or `run=3` picks runs.
- Compare numbers with `num` (`value` is the debugger's text); mask addresses with `no_addresses(value)`; `object_ref(value)` tells a Java object from a copy (4.3 to 4.6). `--label` tags a run with free text, stored as given; it never changes which runs `diff` or `compare:` pair.
- Queries are read-only and bounded. A failing query returns `INVALID_QUERY`. Saved evidence describes past runs only: a new question about the program needs a new probe.

### Verifying an edit

- `--old` must match the source exactly once; with several pairs, each must (they must not overlap, each quoting the original file), and an error names the edit by number (`(edit 2)`) and which condition failed (the match count and lines, or `--new equals --old`). Multi-line text: `--old-file`/`--new-file`, or real newlines (6.3). `--at` names the file to edit when the target is a directory (a Cargo crate, a Python project).
- The result includes `stage` (`build` or `verification`), `seconds`, the unified `diff` applied, and on failure a `reason` (the checker's failure line if it only exited nonzero, else its exception or the build diagnostic). `--timeout S` bounds the whole verify, build included (`TIMEOUT`). `--keep`, failed Cargo tests and `--details`: 6.1.
- If `verify` fails at `stage: build`, read the compiler diagnostics and try again. Provenance or toolchain errors are fatal: stop and report them.

### Targets and selectors

- A `.py` file, a `.c`/`.cpp` file (or a directory holding one C/C++ program), a Cargo crate directory with its `Cargo.lock` (a plain `cargo new` binary crate works; shape: 7.2), or any target with `--config <trusted.json>` (required for Java; `describe`'s commands repeat it). A Java attach config attaches to a running `--debug-jvm` JVM (7.4).
- **Python programs with several files:** pass the project directory as the target and `--at <module.py>:<line>`. If the module is imported, not run, add `--run <script.py>`: `peeky probe app/ --at lib/parse.py:42 --run main.py`. `session start` accepts `--run` too, and `verify` edits the `--at` file. `peeky describe <dir> --at <module.py>` lists each function's line span (`IPv6Address.is_private (lines 2000-2008)`).
- **Remote targets:** if a worker runs on another host behind an SSH tunnel, add `--worker http://127.0.0.1:<port>` to `probe` or `session start`, with the token in `$PEEKY_WORKER_TOKEN`. Your local copy of the source must match the worker's, or the results are discarded. See [REMOTE.md](REMOTE.md).
- **Selectors** are dotted paths, indexes and keys (`request.page`, `args[0]`, `order['id']`; C++ `m[1]` is a map's value for key 1, `m[#0]` its first entry; a shadowed local as LLDB names it, `s @ main.rs:81.a`); expressions are rejected. Quote selectors that contain `[...]` in zsh.

## Time: your tool timeout and the program's clocks

- **Use `--background` for long calls.** If a call may outlast your tool timeout (JVM startup, compiled builds, slow programs), add `--background` to `probe` or `verify`. It returns `{"job": "<id>"}` at once. Then repeat `peeky job wait <id> [--timeout 20]` until the exit code is not **3**. The final stdout, stderr and exit code are what the foreground command would have produced; `job wait --json` prints JSON however the job was started.
- **Prefer probes for programs with clocks.** Test timeouts, heartbeats and RPC deadlines keep running while the program is paused. A probe pauses for milliseconds per hit (`pauses_ms`), with no model turn in between.
- **Sessions can't wait for you on timed programs.** Pass `session start --max-pause S`; attached JVMs default to 10 s. A breakpoint stop older than S is auto-resumed; your next call on it fails with `STALE_STOP`. Crash and exception stops (`reason` other than `breakpoint`) are never auto-resumed. `inspect` shows `paused_s` and `resume_in_s`.
- **`peeky describe` reports this target's timing contract** in its `timing` field.

## When something fails

Errors have a stable `code`, a `hint`, `retryable` and `session_closed` (see [ERRORS.md](ERRORS.md)). Use `--json` to read them. Follow the hint. Exit codes: 0 success, 1 a completed command with a negative result (probe failed or no-stop, verify did not pass), 2 an error or a refusal (a probe refused before launch), 3 not there yet (a background job still running; a session's `--to`, `--count` or `--hit` stopped first), 4 a probe change never made (`unapplied`). Never repeat a `set`/mutation after `TIMEOUT` or `DEBUGGER_ERROR`; the outcome is uncertain: start a new session. `NO_STOP` from a probe means your line never ran: choose another from `describe`.
