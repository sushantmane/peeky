---
name: peeky-debug
description: Debug a running program with the peeky CLI — stop at a line, read live values, test a hypothesis by changing a local, and verify a source fix on a fresh build with an independent checker. Use for wrong-output or logic bugs in Python, C, C++, Rust or configured Java targets.
---

# Debugging with peeky

Start with docs/FIRST.md in the peeky repository: the first question is one command, from the project's own test command. docs/AGENT_GUIDE.md has the rest: when a probe beats a print, and every rule. Short form:

1. `peeky probe . --at <file>:<line> --capture <a,b.c> --test -- <the command that runs the failing test>`, from the project's root: observe real values (`pytest …`, `mvn test …`, `./gradlew test …`, `cargo test …`, `./build/prog ARGS`).
2. `peeky describe <target>`: valid breakpoint lines and what can be read or changed.
3. `peeky probe ... --set <local>=<value>`: test the hypothesis. Values must be literals, and they are applied once, at hit 1 or at `--set-hit N`. This is evidence only. Always read `warnings`.
4. Edit the source generally, then `peeky verify <target> --check <checker> --old '<text>' --new '<text>'`. For Python, `--check-command '<test command>'` in place of `--check` runs the project's tests on the edited copy.
5. Report the commands, the observed values, the intervention's effect and the verify result. Never claim a fix without `passed: True` from `verify`.

Every probe is saved with a `run_id`. SQL is optional; use it for large captures, for comparing runs, or when rerunning is slow. Ask follow-up questions without rerunning: `peeky sql "SELECT hit, selector, value FROM last_run"`, or a canned query (`peeky queries --help` lists them with their parameters; `peeky queries diff` compares the last two runs). `peeky sql --schema` lists the tables; `--schema captures` shows one in full.

For a breakpoint in an imported Python module, pass the project directory, `--at module.py:<line>` and `--run <entry script>`.

If a call may outlast your tool timeout, add `--background` and poll with `peeky job wait <id>` (exit 3 means still running). Programs with timeouts or heartbeats: prefer probes, and give sessions `--max-pause`.

For several looks at one stop, use `peeky session start ... --at ...`, then `session get`, `session set`, `session continue`, `session report` and `session close`. `session inspect --frame 1` and `session get --frame 1 <names>` read the caller's frame. Use `--json` when you need to parse output.
