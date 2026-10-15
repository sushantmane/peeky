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

Targets: a `.py` file, a `.c`/`.cpp` file, a Cargo crate directory, or any target with `--config <trusted.json>` (required for Java). A Java attach config (`port`, `sources`, `class_roots`) attaches to a running `--debug-jvm` JVM; attached targets cannot be verified, and closing detaches without killing the JVM. Selectors are dotted paths and indexes (`request.page`, `args[0]`); expressions are rejected.

## Workflow

1. `peeky describe <target>`: pick a line where the suspicious value has just been computed.
2. `peeky probe <target> --at <file>:<line> --capture <names>`: confirm what the program actually computes.
3. Form a hypothesis, then test it: `peeky probe ... --set <name>=<value>`. Did the output change as predicted?
4. Write the general fix (not a special case for the observed input) and run `peeky verify ...`.
5. Report the observation, the intervention and its effect, the edit, and the verify result. Quote the commands.

If `verify` fails at `stage: build`, read the compiler diagnostics and try again. Provenance or toolchain errors are fatal: stop and report them.
