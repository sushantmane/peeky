# Peeky agent guide

You debug programs with **peeky**, a standalone tool. You decide what to do; peeky runs the debugger safely and returns evidence. Every claim you make must cite a peeky command and its output.

## Principles

1. **Observe before changing.** Stop at a line and read real values before hypothesizing.
2. **Interventions are experiments, not fixes.** `--set` or `session set` changes one live value in one run. It tells you whether a hypothesis explains the behavior. It never proves a source fix.
3. **Only `peeky verify` verifies a fix.** It rebuilds your candidate edit from scratch and runs the independent checker (and the compiled Cargo test for Rust). Report `passed: True` only from `verify`.
4. **Never guess lines or names.** Run `peeky describe` first; use its breakpoint lines and the variable names you actually inspected.
5. **Respect opaque values.** `<unqualified Rust enum layout>`, `<unverified ...>` or `budget-exhausted` mean peeky refused to guess. Do not invent their contents.

## Commands

| Command | Cost | Use |
| --- | --- | --- |
| `peeky describe <target> [--json]` | one build | Learn the language, valid breakpoint lines, qualified views and mutation rules. Always first. |
| `peeky probe <target> --at <path>:<line> --capture a,b.c [--set x=0] [--hits N\|all] [--json]` | one run | Answer one question in one call: values at a line, optionally after one intervention, plus program output. `--hits all` captures every stop up to 100 (fewer with more than 20 selectors) without counting iterations; it needs the program to exit. |
| `peeky sql "SELECT ... FROM last_run"` / `peeky queries [name]` | instant | Ask follow-up questions about evidence that probes already saved. No rerun. `peeky sql --schema` lists the tables. |
| `peeky session start <target> --at <path>:<line>` | one run | Start step-by-step debugging when you need several looks at the same stop. |
| `peeky session inspect` / `get a.b 'c[0]' "d['k']"` / `set x=0` / `step [--into\|--out]` / `continue [--to LINE\|--to-exit]` / `report` / `close` | instant | Work with the stopped program. `get` also lists a value's children, each with the selector that reads it, and exits 1 if any selector failed. `step` runs to the next line (`--into` a call, `--out` of the function; for Java, `--into` and `--out` skip JDK classes). `continue` runs to the next stop (`--count N`: the Nth breakpoint stop); `session start --hit N` starts at the Nth stop; state lines show `hit N` for stops at the session's breakpoint. `--count` and `--hit` stop at anything else (a crash, a step, the exit) with a note, and exit 3 when they did not get there; `--to LINE` also stops once at LINE of the session's file; `--to-exit` clears the breakpoints and runs to the end. A state with `outside_program: true` is a step that left your sources (nothing is read there): `step --out` or `continue`. Always `close`. |
| `peeky probe <target> --at <path>:<line> --capture ... --old '<exact text>' --new '<replacement>'` | one build | See what a candidate fix does before you make it (C, C++, Rust, launched Java): the same probe on a fresh build with the edit; your files are not changed. `--at` keeps the original's line numbers (a line inside an edit that adds or removes lines is refused). An edit that does not compile fails with `BUILD_FAILED`. Compare with the unedited run through `peeky queries diff`. It is evidence, not verification. |
| `peeky verify <target> [--at <file>] --check <checker.py> --old '<exact text>' --new '<replacement>'` | one build | Verify one candidate edit on a fresh build. `--at` names the file to edit when the target is a directory (a Cargo crate, a Python project). |

Session names are claimed atomically, so a second `session start` with the same name fails with "exists" rather than taking over the first session. A session's worker ends with the session: on `close`, when the start fails or is interrupted, and after its idle timeout (5 minutes). Every command takes `--json`: stdout is then one JSON object (progress goes to stderr; `--quiet` silences it). Session records live in `$PEEKY_STATE_DIR` (default `~/.peeky`), so set it per task to keep sessions apart.

### Interventions (`--set name=value`)

- Only root locals can be set: the stopped function's own variables (`start`), not their fields or elements (`request.page`, `items[0]`). Python accepts literal values, including containers (`0`, `'x'`, `None`, `[]`, `{'k': 1}`). C and C++ accept integers of 16 to 64 bits (`short` to `long long`, signed or unsigned, and `int16_t` to `uint64_t`, `size_t`, `ssize_t`, `ptrdiff_t`, `intptr_t`, `uintptr_t`) within the local's range; Java accepts scalars of the local's type; Rust accepts `i16` to `i64`, `u16` to `u64`, `isize` and `usize` locals the program keeps in memory. For C, C++ and Rust, `session inspect` marks such locals `[settable]` (`settable` in JSON). Every change is read back: one the debugger refuses or cannot store (a computed value such as a bitfield binding) is `MUTATION_REJECTED` with the session kept, and a value stored differently from the one requested closes the session. The `MUTATION_REJECTED` message names the local and the rule it broke. Expressions, names and calls (`min(xs)`, `float('inf')`) are rejected with `MUTATION_REJECTED` and are never evaluated. Use `-1e309` for negative infinity.
- Rust: only locals the program keeps in memory can be changed. These are usually locals the code borrows or reassigns. For other locals the compiler keeps the value in a register, and the debugger only shows a copy, so the change is refused (`MUTATION_REJECTED`) rather than reported as applied.
- A probe applies its interventions once, at hit 1 by default. Use `--set-hit N` to apply them at a later hit, for example the iteration where things go wrong. Other hits only observe. For changes on several iterations, use a session: `continue`, then `set` again.

### Reading probe results

- A row with `unavailable: true` has no value at that line (optimized out or not yet live); never treat its error text as a value. In optimized builds, even values that are shown can be stale.
- Each row has a `function`, which tells a function body apart from a generator or lambda frame on the same line.
- `output_complete: false` means the probe ended (`--until hits`) before the program did, so `output` holds only what was printed so far.
- `by_hit` groups values per hit: `{hit, line, observed: {selector: value}, after-set: {...}}`. A selector that failed shows `{"error": ...}`. `rows` holds the same data with one entry per hit, phase and selector. `phase` is `observed`, or `after-set` (the value after your intervention). `line` is where the program actually stopped.
- Read `warnings` every time. They flag:
  - a breakpoint the debugger moved to another line (the values then come from that line);
  - runs that ended before all requested hits;
  - a Java exit that Peeky inferred because the debugger dropped its exit event (the result is still valid).
- In C++ and Java methods, read members through `this` (`this.head_`, `this.total`); a bare member name's error names that selector. Only the stopped function's own locals can be changed, not members. Session states and `inspect` name the stopped function (`in Ring::push(int)`, `in topk::insert_sorted::{{closure}}`). A line shared by a function and a closure or lambda can stop in each: the probe then warns, and `--hits`/`--set-hit` count every stop (the `function` column says which is which). Java is the exception: on a line several methods share (a lambda body on its method's line, a static initializer's lambda), the debugger stops in only one, and the probe and `session start` warn which one stops and which never do.
- If a selector is missing, the row's error lists the visible names, or the children of the value it was reading. Python dict entries are read by key: `order['id']`, not `order.id` (the error says so). `peeky describe` also lists each function's lines and the names it can see (`scopes`), for Python, C, C++ and Rust; `scopes_unavailable` says why when the debug information could not be read.
- Rust reference parameters (`&[T]`, `&str`, `&Vec<T>`, `&String`, shared or `&mut`, where `T` is any integer type, `bool`, `f32` or `f64`) are read from the program's memory, at offsets checked against the build's debug information, when the debugger supports `readMemory` (lldb-dap from LLVM 20). Rows show `[memory]`; selectors index them (`values[0]`, `values[-1]`) and read their length (`values.length` for slices and `&str`, `list.len` and `list.capacity` through `&Vec`/`&String`). Text is shown whole. `&mut` parameters are read only in the innermost frame. Elements show as Rust's `{:?}` prints them (`[0.1, -0.0, 1e16, NaN]`, `[true, false]`), and each element row carries its exact type (`u64`, not LLDB's `unsigned long`). Owned `Vec<T>`/`String` locals are read too, in the innermost frame, where the build's MIR proves them initialized at the stop line. Where they may be moved, dropped or not yet assigned (`drop(v)`, `consume(v)`, `let s = s + "x"`, a `let label: String;` before its assignment, a move on that very line), they keep their raw fields (`ordered.len`) with a note, because their pointer can reach freed memory (#53). Reference locals (`let view: &[i32]`) keep their raw fields. Optimized builds give no proof, so parameters there keep their raw fields or show as optimized out. Long values show their first elements or characters with `(first k of N)`; up to 256 elements stay selectable.
- debugpy lists only the first 100 items of a long list. A higher index is reported as "beyond the debugger's visible window" rather than missing; it does not mean the list is short.
- Selectors take indexes from the end (`items[-1]`), but only when the debugger states the sequence's length and shows every element. Otherwise you get an error, never a guess. Long debugpy lists show 100 elements individually, so for them use a non-negative index.
- Failures carry `error: {code, message, hint, retryable, session_closed}` (see [ERRORS.md](ERRORS.md)). `status` is `completed`, `no-stop`, `failed` or `timeout`.

### Querying saved evidence (`peeky sql`, `peeky queries`)

Every probe is saved, and the result's `evidence.run_id` names it. **SQL is optional.** For a single probe, read its JSON result. Use saved evidence when:
- **a capture is large** (many hits or selectors): query only the rows you need instead of reading them all;
- **you are comparing runs** (two inputs, or with and without `--set`): `peeky queries diff` gives the first divergence;
- **runs are slow or unrepeatable** (JVM tests, timing-dependent programs): query what already happened instead of rerunning.


- `peeky sql --schema` lists the tables (`runs`, `captures`, `interventions`, `pauses`), the `evidence` and `last_run` views, and the canned queries. Read it before writing SQL.
- `peeky queries` needs no SQL. `run` shows a run's values, `hits` shows a selector across hits with `changed`, `compare` puts every value of two runs side by side and `diff` keeps only the rows that differ: real differences first in hit and capture order (the first row is the first divergence), then `intervention only in run a/b` rows, then `addresses only` rows (values that differ only in a heap or stack address; NULL never counts as one), then one `only in run a/b` row per selector that only one run asked for, then `(status)` and `(output)` rows when the runs' outcomes or outputs differ; `outputs` shows two runs' outputs, outcomes and the `set_hit` of their interventions, `set_effect` shows values before and after an intervention, `values` shows the distinct values of a selector across runs, and `unreadable` and `failures` list what has no value. Pass `--param name=value`.
- Use `num` for numeric comparisons (`num > 10`); `value` is the debugger's text. `no_addresses(value)` masks heap and stack addresses (never NULL or short hex), for comparing values across runs in your own SQL. `--label` tags a run with free text (`--label no-trailing-space`) so you can find it later; it is stored as given, not parsed.
- Queries are read-only and bounded. A failing query returns `INVALID_QUERY`; fix the SQL. Saved evidence describes past runs only: a new question about the program needs a new probe.

### Verifying an edit

- `--old` must match the source exactly once. For multi-line edits, prefer `--old-file`/`--new-file`: they are read byte for byte, whereas shell `$(cat file)` silently strips trailing newlines. Pass real newlines for multi-line text (bash: `--old $'a\n    b'`). If it fails, the error gives the match count and the lines where it matched.
- The result includes `stage` (`build` or `verification`), the unified `diff` that was applied, and on failure a `reason` (the checker's exception or the build diagnostic).
- The scratch copy is deleted afterwards. Pass `--keep` to keep the edited file for inspection (build artifacts are not kept). Failed Cargo tests are listed by name with their assertion. `--json` is compact: the checker's descriptor and build manifest are identified by `descriptor_sha256` and `artifact_manifest_id`; `--details` includes them in full.

**Python programs with several files:** pass the project directory as the target and `--at <module.py>:<line>` for the breakpoint. If the module is imported rather than run directly, add `--run <script.py>` to say which script to run, for example `peeky probe app/ --at lib/parse.py:42 --run main.py`. Pass program arguments with repeatable `--arg` (for example `--arg ::ffff:8.8.8.8`, or `--arg=-v` for a value that starts with a dash), or all of them after `--` (`peeky probe t --at 5 -- A 5 5 0`); use them to narrow the run to the failing input. `--arg` also works for C, C++, Rust and launched Java programs, where it replaces the configured `arguments`; attached JVMs refuse it. `session start` accepts `--run` and `--arg` too, and `verify` edits the `--at` file. `peeky describe <dir> --at <module.py>` lists each function's line span and names (`IPv6Address.is_private (lines 2000-2008)`), so you can jump straight to the line.

**Remote targets:** if a worker runs on another host behind an SSH tunnel, add `--worker http://127.0.0.1:<port>` to `probe` or `session start`, with the token in `$PEEKY_WORKER_TOKEN`. Your local copy of the source must match the worker's, or the results are discarded. See [REMOTE.md](REMOTE.md).

Targets: a `.py` file, a `.c`/`.cpp` file (or a directory holding one C/C++ program), a Cargo crate directory with its `Cargo.lock`, or any target with `--config <trusted.json>` (required for Java; `describe`'s commands repeat it). A Java attach config (`port`, `sources`, `class_roots`) attaches to a running `--debug-jvm` JVM; attached targets cannot be verified, and closing detaches without killing the JVM. Selectors are dotted paths, indexes and string keys (`request.page`, `args[0]`, `order['id']`); expressions are rejected. Quote selectors that contain `[...]` in zsh.

## Workflow

1. `peeky describe <target>`: pick a line where the suspicious value has just been computed.
2. `peeky probe <target> --at <file>:<line> --capture <names>`: confirm what the program actually computes. To compare inputs or hits, probe each input once (`--label`), then use `peeky queries diff` or `peeky sql`.
3. Form a hypothesis, then test it: `peeky probe ... --set <name>=<value>`. Did the output change as predicted?
4. Write the general fix (not a special case for the observed input). To see what it does at the same line first, probe it with `--old/--new` (compiled targets). Then run `peeky verify ...`.
5. Report the observation, the intervention and its effect, the edit, and the verify result. Quote the commands.

If `verify` fails at `stage: build`, read the compiler diagnostics and try again. Provenance or toolchain errors are fatal: stop and report them.

## Time: your tool timeout and the program's clocks

- **Use `--background` for long calls.** If a call may outlast your tool timeout (JVM startup, compiled builds, slow programs), add `--background` to `probe` or `verify`. It returns `{"job": "<id>"}` at once. Then repeat `peeky job wait <id> [--timeout 20]` until the exit code is not **3**. The final stdout, stderr and exit code are exactly what the foreground command would have produced.
- **Prefer probes for programs with clocks.** Test timeouts, heartbeats and RPC deadlines keep running while the program is paused. A probe pauses for milliseconds per hit (`pauses_ms`), with no model turn in between.
- **Sessions can't wait for you on timed programs.** Pass `session start --max-pause S`; attached JVMs default to 10 s. A breakpoint stop older than S is auto-resumed, and your next call on it fails with `STALE_STOP` and says so. Crash and exception stops (`reason` other than `breakpoint`) are never auto-resumed, so you can inspect them. `inspect` shows `paused_s` and `resume_in_s`, so you can see how much time is left.
- **`peeky describe` reports this target's timing contract** in its `timing` field.

## When something fails

Errors have a stable `code`, a `hint`, `retryable` and `session_closed` (see [ERRORS.md](ERRORS.md)). Use `--json` to read them. Follow the hint. Exit codes: 0 success, 1 a completed command with a negative result (probe failed or no-stop, verify did not pass), 2 an error, 3 a background job still running. Never repeat a `set`/mutation after `TIMEOUT` or `DEBUGGER_ERROR`; the outcome is uncertain, so start a new session instead. `NO_STOP` from a probe means your line never ran: choose another from `describe`.
