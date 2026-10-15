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
| `peeky probe <target> --at <path>:<line> --capture a,b.c [--set x=0] [--hits N] [--json]` | one run | Answer one question in one call: values at a line, optionally after one intervention, plus program output. |
| `peeky session start <target> --at <path>:<line>` | one run | Start step-by-step debugging when you need several looks at the same stop. |
| `peeky session inspect` / `get a.b c[0]` / `set x=0` / `continue` / `report` / `close` | instant | Work with the stopped program. Always `close`. |
| `peeky verify <target> --check <checker.py> --old '<exact text>' --new '<replacement>'` | one build | Verify one candidate edit on a fresh build. |

Every command takes `--json`: stdout is then one JSON object (progress goes to stderr; `--quiet` silences it). Session records live in `$PEEKY_STATE_DIR` (default `~/.peeky`), so set it per task to keep sessions apart.

### Interventions (`--set name=value`)

- Only existing locals in the stopped frame can be set. Python accepts literal values, including containers (`0`, `'x'`, `None`, `[]`, `{'k': 1}`). Compiled languages accept scalars only. Expressions, names and calls (`min(xs)`, `float('inf')`) are rejected with `MUTATION_REJECTED` and are never evaluated. Use `-1e309` for negative infinity.
- A probe applies its interventions once, at hit 1. Later `--hits` observe without re-applying. For per-iteration changes, use a session: `continue`, then `set` again.

### Reading probe results

- `rows` has one entry per hit, phase and selector. `phase` is `observed`, or `after-set` (the value after your intervention). `line` is where the program actually stopped.
- Read `warnings` every time. They flag a breakpoint the debugger moved to another line (the values then come from that line) and runs that ended before all requested hits.
- If a selector is missing, the row's error lists the visible names.
- Failures carry `error: {code, message, hint, retryable, session_closed}` (see [ERRORS.md](ERRORS.md)). `status` is `completed`, `no-stop`, `failed` or `timeout`.

### Verifying an edit

- `--old` must match the source exactly once. Pass real newlines for multi-line text (bash: `--old $'a\n    b'`). If it fails, the error gives the match count and the lines where it matched.
- The result includes `stage` (`build` or `verification`), the unified `diff` that was applied, and on failure a `reason` (the checker's exception or the build diagnostic).
- The scratch copy is deleted afterwards. Pass `--keep` to keep it for inspection.

Targets: a `.py` file, a `.c`/`.cpp` file, a Cargo crate directory, or any target with `--config <trusted.json>` (required for Java). A Java attach config (`port`, `sources`, `class_roots`) attaches to a running `--debug-jvm` JVM; attached targets cannot be verified, and closing detaches without killing the JVM. Selectors are dotted paths and indexes (`request.page`, `args[0]`); expressions are rejected.

## Workflow

1. `peeky describe <target>`: pick a line where the suspicious value has just been computed.
2. `peeky probe <target> --at <file>:<line> --capture <names>`: confirm what the program actually computes.
3. Form a hypothesis, then test it: `peeky probe ... --set <name>=<value>`. Did the output change as predicted?
4. Write the general fix (not a special case for the observed input) and run `peeky verify ...`.
5. Report the observation, the intervention and its effect, the edit, and the verify result. Quote the commands.

If `verify` fails at `stage: build`, read the compiler diagnostics and try again. Provenance or toolchain errors are fatal: stop and report them.

## Time: your tool timeout and the program's clocks

- **Use `--background` for long calls.** If a call may outlast your tool timeout (JVM startup, compiled builds, slow programs), add `--background` to `probe` or `verify`. It returns `{"job": "<id>"}` at once. Then repeat `peeky job wait <id> [--timeout 20]` until the exit code is not **3**. The final output and exit code are exactly what the foreground command would have printed.
- **Prefer probes for programs with clocks.** Test timeouts, heartbeats and RPC deadlines keep running while the program is paused. A probe pauses for milliseconds per hit (`pauses_ms`), with no model turn in between.
- **Sessions can't wait for you on timed programs.** Pass `session start --max-pause S`; attached JVMs default to 10 s. A stop older than S is auto-resumed, and your next call on it fails with `STALE_STOP` and says so. `inspect` shows `paused_s` and `resume_in_s`, so you can see how much time is left.
- **`peeky describe` reports this target's timing contract** in its `timing` field.

## When something fails

Errors have a stable `code`, a `hint`, `retryable` and `session_closed` (see [ERRORS.md](ERRORS.md)). Use `--json` to read them. Follow the hint. Exit codes: 0 success, 1 a completed command with a negative result (probe failed or no-stop, verify did not pass), 2 an error, 3 a background job still running. Never repeat a `set`/mutation after `TIMEOUT` or `DEBUGGER_ERROR`; the outcome is uncertain, so start a new session instead. `NO_STOP` from a probe means your line never ran: choose another from `describe`.
