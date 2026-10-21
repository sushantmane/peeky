# Peeky agent guide

You debug programs with **peeky**, a standalone tool. You decide what to do; peeky runs the debugger safely and returns evidence. Every claim you make must cite a peeky command and its output.

## Principles

1. **Observe before changing.** Stop at a line and read real values before hypothesizing.
2. **Interventions are experiments, not fixes.** `--set` or `session set` changes one live value in one run. It tells you whether a hypothesis explains the behavior. It never proves a source fix.
3. **Only `peeky verify` verifies a fix.** It rebuilds your candidate edit from scratch and runs the independent checker (and the crate's integration test for Rust, when it has one). Report `passed: True` only from `verify`.
4. **Never guess lines or names.** Run `peeky describe` first; use its breakpoint lines and the variable names you actually inspected.
5. **Respect opaque values.** `<unqualified Rust enum layout>`, `<unverified ...>` or `budget-exhausted` mean peeky refused to guess. Do not invent their contents.

## Commands

| Command | Cost | Use |
| --- | --- | --- |
| `peeky describe <target> [--json]` | one build | Learn the language, valid breakpoint lines, qualified views and mutation rules. Always first. |
| `peeky probe <target> --at <path>:<line> --capture a,b.c [--set x=0] [--hits N\|all] [--json]` | one run | Answer one question in one call: values at a line, optionally after interventions, plus program output. `--hits all` captures every stop up to 100 (fewer past 20 selectors); it needs the program to exit. `--capture return`: a `(return)` row per call of the line's function (probe its first line; not Python; deep recursion is slow: limit `--hits`). |
| `peeky sql "SELECT ... FROM last_run"` / `peeky queries [name]` | instant | Ask follow-up questions about evidence that probes already saved. No rerun. `peeky sql --schema` lists the tables. |
| `peeky session start <target> --at <path>:<line>` | one run | Start step-by-step debugging when you need several looks at the same stop. |
| `peeky session inspect` / `get a.b 'c[0]' "d['k']"` / `set x=0` / `step [--into\|--out]` / `continue [--to LINE\|--to-exit]` / `report` / `close` | instant | Work with the stopped program. `get` also lists a value's children, each with the selector that reads it, and exits 1 if any selector failed. `--frame N` reads caller frame N. `step` runs to the next line (`--into` a call, `--out` of the function; for Java, `--into` and `--out` skip JDK classes). `continue` runs to the next stop (`--count N`: the Nth breakpoint stop); `session start --hit N` starts at the Nth stop; states show `hit N` at the session's breakpoint. `--count` and `--hit` stop at anything else (a crash, a step, the exit) with a note, and exit 3 when they did not get there; `--to LINE` also stops once at LINE of the session's file; `--to-exit` clears the breakpoints and runs to the end. `outside_program: true`: a step left your sources; only the return is read (`get return`); `step --out` or `continue`. Always `close`. |
| `peeky probe <target> --at <path>:<line> --capture ... --old '<exact text>' --new '<replacement>'` | one build | See what a candidate fix does before you make it (any target but an attached JVM): the same probe on a fresh build, or for Python an edited copy; your files are not changed. `--at` keeps the original's line numbers (a line inside an edit that adds or removes lines is refused). An edit that does not compile fails with `BUILD_FAILED` (Python: `NO_STOP`, with the `SyntaxError`). Compare with the unedited run through `peeky queries diff`. It is evidence, not verification. |
| `peeky verify <target> [--at <file>] --check <checker.py> --old '<exact text>' --new '<replacement>'` | one build | Verify one candidate edit on a fresh build; repeat `--old`/`--new` for several edits, each quoting the original file (they must not overlap). `--at` names the file to edit when the target is a directory (a Cargo crate, a Python project). |

A second `session start` with a name in use fails with "exists". A session's worker ends on `close`, when the start fails or is interrupted, or after 5 idle minutes. Every command takes `--json`: stdout is then one JSON object (progress on stderr; `--quiet` silences it). Every session command takes the session's `--name` again (default `default`). Session records live in `$PEEKY_STATE_DIR` (default `~/.peeky`): set it per task to keep sessions apart.

### Interventions (`--set name=value`)

- Root locals can be set: the stopped function's own variables (`start`). In Java, so can scalar fields and array elements reached from them (`current.end=10`, `this.count=0`, `arr[0]=1`, `arr[-1]=9`), through lists too (`workers[1].load=5`), not final or static fields, Strings, boxed values or an `ArrayList`'s elements. A field another field's name hides is refused. Old adapter bundles change only locals (rebuild). In C, C++ and Rust, so can integer fields and elements (`s.end`, `xs[2]`, `t.__0`) of a struct, tuple or array local the frame holds itself (Rust: in memory, MIR-proven initialized); never through a pointer, reference or Box, nor in a union, enum, packed or std type, bitfield or const; a plain name is the local the code names. Python refuses fields before the program runs, and accepts literals, containers too (`0`, `'x'`, `None`, `[]`, `{'k': 1}`). C and C++ accept integers of 16 to 64 bits (`short` to `long long`, `size_t`...) in range; Java scalars of the target's type; Rust `i16` to `i64`, `u16` to `u64`, `isize`, `usize`. Rows mark what `set` would change `[settable]`; `session inspect` notes why when a rule leaves nothing settable. Every change is read back: one the debugger refuses or cannot store is `MUTATION_REJECTED` with the session kept; a value stored differently from the one asked closes the session. Expressions, names and calls (`min(xs)`, `float('inf')`) are `MUTATION_REJECTED`, never evaluated. `-1e309` is negative infinity.
- A change writes the variable's storage, which later reads see; copies the program made keep the old value (`let a = s.x;` above, a statement under way), and the stop's line may overwrite it. So C, C++ and Rust allow one only where Peeky proves none is under way (C: at a statement's first instruction; Rust: by MIR), never after `step --out`. Before running, a probe refuses a `--set`/`--set-at` its build rules out: a Rust local kept in a register (`[not settable: register]`; `describe` lists them), a field through a pointer.
- `--set` applies once, at hit 1 (`--set-hit N`: later); other hits only observe. `--set-at LINE@HIT x=0` (repeatable) changes at that hit of its own line: change at 22, restore at 23 (COOKBOOK 2.8). One refusal fails the probe.

### Reading probe results

- A row with `unavailable: true` has no value at that line (optimized out, not live); never treat its error text as a value. A local whose storage may hold leftovers is withheld (`no value yet:` and why) and cannot be `--set`. C and C++: declared below the stop, in a running initializer, past a `case`/`goto` label, `this` in a constructor, hidden by a block's `static`/`extern`, or declared on the stop line where control enters it (`declared on line 3, which has not run yet`), not mid-line (after `step --out`). Rust: unassigned on a path (`let x;`) or not kept across an `.await`. **A row with a `note` is a value Peeky cannot vouch for**, as is all read through it (a line a loop returns to, a `do` body's first: read the next; or below a `setjmp`). Optimized builds may show stale values.
- Each row's `function` tells a function from a generator or lambda on the same line.
- `output_complete: false` means the probe ended (`--until hits`) before the program did, so `output` holds only what was printed so far (a program ending at the last hit may have printed it all). `--until hits` saves the most for slow exits (JVMs, Python).
- `by_hit` groups values per hit: `{hit, line, observed: {selector: value}, after-set: {...}}`. A selector that failed shows `{"error": ...}`. `rows` holds the same, one entry per hit, phase and selector. `phase` is `observed`, `after-set` (after your intervention) or `returned`. `line` is the stop's actual line.
- Read `warnings` every time. They flag:
  - a breakpoint the debugger moved to another line (values come from that line);
  - a line that stops several times per pass (a macro's code; C, C++, Rust): `--hits`, `--set-hit`, `--set-at` count stops; `stop` says which;
  - a loop header that stops only before its loop: `--at` the body's first line;
  - runs that ended before all requested hits;
  - a Java exit Peeky inferred when the debugger dropped its exit event (the result stands).
- C and C++ structs and arrays show their first members (`{lo = 0, hi = 2}`, `[{…}, …]`); unions and pointer targets are not read.
- In a launched Java program, a value you capture or `session get` shows more:
  - an object its first fields inline (`Merge$Interval@143 {end=10, start=1}`);
  - an array its first elements (`int[3]@144 [1, 2, 3]`);
  - an `ArrayList` its size and first elements (`ArrayList@142 size=2 [...]`).

  These are the debugger's reads of the value's own fields (no code runs in the program); Java `inspect` listings stay plain. An attached JVM shows plain values to keep its pauses short. `ArrayList` selectors index like arrays (`merged[0]`, `merged[-1].end`) within its `size`, unless its backing array holds more than 512 slots. Fields that are Strings and chars show as Java literals (`name="say \"hi\""`, `quote='"'`), a String cut after 38 characters with `…`. Java object numbers (`@143`) follow the order the debugger first touched objects, so `queries diff` treats them like addresses in Java runs.
- In C++ and Java methods, read members through `this` (`this.head_`, `this.total`); a bare member name's error names that selector. C++ changes no member through `this` (a pointer); Java does (`this.total=0`). A Java `step` from a block's last statement stops on its closing `}`, where the block's locals are gone; read them before stepping. Session states and `inspect` name the stopped function (`in Ring::push(int)`, `in topk::insert_sorted::{{closure}}`). A line shared by a function and a closure or lambda can stop in each: the probe then warns, and `--hits`, `--set-hit` and `--set-at`'s HIT count every stop (`function` says which). Java is the exception: on a line several methods share (a lambda body on its method's line, a static initializer's lambda), the debugger stops in only one, and the probe and `session start` warn which one stops and which never do.
- If a selector is missing, the row's error lists the visible names, or the children of the value it was reading. Python dict entries are read by key: `order['id']`, not `order.id` (the error says so). Python reads globals it provably uses (not an unassigned local; one, or a misspelled name, lists all, running all reprs) and dunders (`f.__defaults__`): COOKBOOK 5.1; `[id 7]`: 4.6. `peeky describe` also lists each function's lines and the names it can see (`scopes`; C, C++ and Rust add their types), for Python, C, C++, Rust and Java; `scopes_unavailable` says why when the debug information could not be read.
- Rust: reference parameters (`&[T]`, `&str`, `&Vec<T>`, `&String`, `&S`; `T` an integer, `bool`, float or `char`, or `S`, a tuple or crate struct of them: `runs[0].__0`, `spans[1].start`) and owned `Vec<T>`/`String`/`S` locals the build's MIR proves initialized are read from memory (`[memory]`; `values[0]`, `values[-1]`, `values.length`); values print as derived `Debug` would, with Rust type names. Anything else keeps its raw fields with a note, never a guess. Details: [RUST_AGENT.md](RUST_AGENT.md#reading-rust-values).
- debugpy lists a long list's first 100 items: a higher index is "beyond the debugger's visible window", not missing. An index from the end (`items[-1]`) needs the sequence's length and every element shown; otherwise it is an error, never a guess.
- Failures carry `error: {code, message, hint, retryable, session_closed}` (see [ERRORS.md](ERRORS.md)). `status` is `completed`, `no-stop`, `failed` or `timeout`.

### Querying saved evidence (`peeky sql`, `peeky queries`)

Every probe is saved, and the result's `evidence.run_id` names it. **SQL is optional.** For a single probe, read its JSON result. Use saved evidence when:
- **a capture is large** (many hits or selectors): query only the rows you need;
- **you are comparing runs** (two inputs, or with and without `--set`): `peeky queries diff` gives the first divergence;
- **runs are slow or unrepeatable** (JVM tests, timing-dependent programs): query what already happened instead of rerunning.

- `peeky sql --schema` lists the tables (`runs`, `captures`, `interventions`, `pauses`), the `evidence` and `last_run` views, and the canned queries. Read it before writing SQL.
- `peeky queries` needs no SQL: `run` (a run's values and notes), `wide` (a row per hit, a column per selector; `no_value` names NULL cells, `caveats` qualified ones), `hits` (a selector across hits, with `changed`, `error`, `caveat`), `compare` (runs side by side), `join` (two runs' hits paired by a value: `key=v`), `diff` (what differs: first `(status)`, `(output)`, `(edit)`, `(source)`, `(line)`, `(args)`, `(sets)`, to check before crediting an edit; then the first real divergence; then rows differing only by an intervention, a caveat (not evidence) or an address; COOKBOOK §3), `outputs`, `set_effect`, `values`, `unreadable`, `failures`. `--param a=1 --param b=5` (default: the last two) or `run=3` picks runs.
- Use `num` for numeric comparisons (`num > 10`); `value` is the debugger's text. Integers up to 64 bits are exact in `num`, so integer division truncates (`num * 1.0 / 1000` for a fraction) and `SUM(num)` fails past 64 bits (`TOTAL(num)` does not). `no_addresses(value)` masks heap and stack addresses (never NULL, short hex or quoted text) to compare runs in your own SQL; `no_addresses(value, language)` (the `evidence` view has `language`; the program's output is `runs.output`) also masks Java object numbers in Java runs. `object_ref(value)` is the Java object a value is (`Balance$Worker@100`): equal within one run means the same object (`object_ref(b.value) = object_ref(w.value)` tells a copy from the list's own element); Python's is the `identity` column. `--label` tags a run with free text, stored as given.
- Queries are read-only and bounded. A failing query returns `INVALID_QUERY`. Saved evidence describes past runs only: a new question about the program needs a new probe.

### Verifying an edit

- **Check the checker first:** `verify ... --baseline` checks the unedited program; it should report `passed: False`, or the checker cannot tell a fix from the bug.
- A refused `--set` ends with the locals settable there (`settable here: total, i`, or `none`).
- `--old` must match the source exactly once; with several pairs, each must, and an error names the edit by number (`(edit 2)`). For multi-line edits, prefer `--old-file`/`--new-file`: read byte for byte, while shell `$(cat file)` strips trailing newlines. Pass real newlines for multi-line text (bash: `--old $'a\n    b'`). If it fails, the error says which condition failed: the match count and lines, or `--new equals --old`.
- The result includes `stage` (`build` or `verification`), `seconds`, the unified `diff` that was applied, and on failure a `reason` (the checker's exception or the build diagnostic). `--timeout S` bounds the whole verify, build included (`TIMEOUT`).
- Pass `--keep` to keep the edited scratch copy (not build artifacts). Failed Cargo tests are listed by name with their assertion. `--json` is compact: the checker's descriptor and build manifest are identified by `descriptor_sha256` and `artifact_manifest_id`; `--details` includes them in full.

**Python programs with several files:** pass the project directory as the target and `--at <module.py>:<line>` for the breakpoint. If the module is imported, not run, add `--run <script.py>`: `peeky probe app/ --at lib/parse.py:42 --run main.py`. Pass program arguments with repeatable `--arg` (`--arg ::ffff:8.8.8.8`; `--arg=-v` for a value starting with a dash), or all of them after `--` (`peeky probe t --at 5 -- A 5 5 0`). `--arg` also works for C, C++, Rust and launched Java programs, where it replaces the configured `arguments`; attached JVMs refuse it. `session start` accepts `--run` and `--arg` too, and `verify` edits the `--at` file. `peeky describe <dir> --at <module.py>` lists each function's line span (`IPv6Address.is_private (lines 2000-2008)`).

**Remote targets:** if a worker runs on another host behind an SSH tunnel, add `--worker http://127.0.0.1:<port>` to `probe` or `session start`, with the token in `$PEEKY_WORKER_TOKEN`. Your local copy of the source must match the worker's, or the results are discarded. See [REMOTE.md](REMOTE.md).

Targets: a `.py` file, a `.c`/`.cpp` file (or a directory holding one C/C++ program), a Cargo crate directory with its `Cargo.lock` (a plain `cargo new` binary crate works: edition 2021 or 2024, no dependencies, no `src/lib.rs`, `src/bin` or `build.rs`, and at most one integration test in `tests/`; run `cargo generate-lockfile` once if `Cargo.lock` is missing), or any target with `--config <trusted.json>` (required for Java; `describe`'s commands repeat it). A Java attach config (`port`, `sources`, `class_roots`) attaches to a running `--debug-jvm` JVM; attached targets cannot be verified, and closing detaches without killing the JVM. Selectors are dotted paths, indexes and keys (`request.page`, `args[0]`, `order['id']`; C++ `m[1]` is a map's value for key 1, `m[#0]` its first entry; a shadowed local as LLDB names it, `s @ main.rs:81.a`); expressions are rejected. Quote selectors that contain `[...]` in zsh.

## Workflow

1. `peeky describe <target>`: pick a line where the suspicious value has just been computed.
2. `peeky probe <target> --at <file>:<line> --capture <names>`: confirm what the program actually computes. To compare inputs or hits, probe each input once (`--label`), then use `peeky queries diff` or `peeky sql`.
3. Form a hypothesis, then test it: `peeky probe ... --set <name>=<value>`. Did the output change as predicted?
4. Write the general fix (not a special case for the observed input). To see what it does at the same line first, probe it with `--old/--new`. Then run `peeky verify ...`.
5. Report the observation, the intervention and its effect, the edit, and the verify result. Quote the commands.

If `verify` fails at `stage: build`, read the compiler diagnostics and try again. Provenance or toolchain errors are fatal: stop and report them.

## Time: your tool timeout and the program's clocks

- **Use `--background` for long calls.** If a call may outlast your tool timeout (JVM startup, compiled builds, slow programs), add `--background` to `probe` or `verify`. It returns `{"job": "<id>"}` at once. Then repeat `peeky job wait <id> [--timeout 20]` until the exit code is not **3**. The final stdout, stderr and exit code are what the foreground command would have produced; `job wait --json` prints JSON however the job was started.
- **Prefer probes for programs with clocks.** Test timeouts, heartbeats and RPC deadlines keep running while the program is paused. A probe pauses for milliseconds per hit (`pauses_ms`), with no model turn in between.
- **Sessions can't wait for you on timed programs.** Pass `session start --max-pause S`; attached JVMs default to 10 s. A breakpoint stop older than S is auto-resumed; your next call on it fails with `STALE_STOP`. Crash and exception stops (`reason` other than `breakpoint`) are never auto-resumed. `inspect` shows `paused_s` and `resume_in_s`.
- **`peeky describe` reports this target's timing contract** in its `timing` field.

## When something fails

Errors have a stable `code`, a `hint`, `retryable` and `session_closed` (see [ERRORS.md](ERRORS.md)). Use `--json` to read them. Follow the hint. Exit codes: 0 success, 1 a completed command with a negative result (probe failed or no-stop, verify did not pass), 2 an error, 3 a background job still running, 4 a probe change never made (`unapplied`). Never repeat a `set`/mutation after `TIMEOUT` or `DEBUGGER_ERROR`; the outcome is uncertain, so start a new session instead. `NO_STOP` from a probe means your line never ran: choose another from `describe`.
