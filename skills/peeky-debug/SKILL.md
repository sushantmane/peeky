---
name: peeky-debug
description: Debug a running program with the peeky CLI — stop at a line, read live values, test a hypothesis by changing a local, and verify a source fix on a fresh build with an independent checker. Use for wrong-output or logic bugs in Python, C, C++, Rust or configured Java targets.
---

# Debugging with peeky

Follow docs/AGENT_GUIDE.md in the peeky repository. Short form:

1. `peeky describe <target>`: valid breakpoint lines and what can be read or changed.
2. `peeky probe <target> --at <file>:<line> --capture <a,b.c>`: observe real values.
3. `peeky probe ... --set <local>=<value>`: test the hypothesis. Values must be literals, and they are applied once, at hit 1 or at `--set-hit N`. This is evidence only. Always read `warnings`.
4. Edit the source generally, then `peeky verify <target> --check <checker> --old '<text>' --new '<text>'`.
5. Report the commands, the observed values, the intervention's effect and the verify result. Never claim a fix without `passed: True` from `verify`.

Every probe is saved with a `run_id`. SQL is optional; use it for large captures, for comparing runs, or when rerunning is slow. Ask follow-up questions without rerunning: `peeky sql "SELECT hit, selector, value FROM last_run"`, or a canned query (`peeky queries` lists them; `peeky queries diff` compares the last two runs). `peeky sql --schema` lists the tables.

For a breakpoint in an imported Python module, pass the project directory, `--at module.py:<line>` and `--run <entry script>`.

If a call may outlast your tool timeout, add `--background` and poll with `peeky job wait <id>` (exit 3 means still running). Programs with timeouts or heartbeats: prefer probes, and give sessions `--max-pause`.

For several looks at one stop, use `peeky session start ... --at ...`, then `session get`, `session set`, `session continue`, `session report` and `session close`. Use `--json` when you need to parse output.
