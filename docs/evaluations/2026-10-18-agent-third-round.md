# Live agent evaluation, third round: Rust floats, C integer time and Java objects (2026-10-18)

## Setup

- **Agents:** three independent general-purpose coding agents, run in parallel with no shared context.
  - Each was given `docs/AGENT_GUIDE.md`, `docs/COOKBOOK.md`, `docs/ERRORS.md`, the `peeky` CLI and one new bug: the program, an independent `check.py` and a one-line issue.
  - Reading the Peeky source was forbidden. Agents could change the program only through `peeky verify --old/--new`.
- **Tool version:** `main` at `f8e5efa`, which includes candidate probes (#68), owned Rust locals (#71) and container views for every element type (#73). It was installed editable, with a separate state directory per agent.
- **Cases:** new for this round, and absent from the corpus and the docs:
  - `histo` (Rust): `histogram(values: &[f64], lo, hi, buckets)` computes `idx = ((x - lo) / width) as usize`, so `x == hi` gives `idx == buckets`, which is then dropped. The checker includes inputs that defeat an `x == hi` special case.
  - `bucket` (C): a token bucket refills `elapsed / 1000 * rate` and then sets `last_ms = now`, so requests less than a second apart never refill. Reordering to `elapsed * rate / 1000` still loses the remainder.
  - `merge` (Java, launched): merging overlapping intervals sets `current.end = next.end`, so an interval contained in the previous one shrinks it.
- **Task:**
  1. Confirm the cause with runtime evidence.
  2. Test the hypothesis with an intervention.
  3. Probe the fix as a candidate.
  4. Reach a general fix with `peeky verify`.
  5. Report friction.
- **Dry run:** before the agents started, Peeky's maintainer ran each case. That found two problems:
  - a `[[test]]` target named like the `[[bin]]` failed with "Cargo application and test crates must have distinct names", which gives no fix;
  - LLDB showed Rust `f64` locals as `10` and `9.9999999999999995E-8`, not as Rust prints them (`10.0`, `1e-7`).

## Outcomes

| Case | Runtime evidence | Verified general fix | Peeky calls (wasted) | Probe / session step / verify |
| --- | --- | --- | --- | --- |
| `histo` (Rust) | At line 13, `--hits all` showed `x=10 idx=5 counts.len=5` on hits 4 and 5. A `peeky sql` self-join on `last_run` marked exactly those hits as dropped. A session `step` went from 13 to 8, skipping `counts[idx] += 1`. The candidate probe showed `idx` 5 → 4 in `queries diff`. | `.min(buckets.saturating_sub(1))` on the index | 19 (4) | 1.5–1.7 s / 0.07 s / 1.5 s |
| `bucket` (C) | At line 18, every request after the first had `elapsed=600 refill=0`, while `b.last_ms` advanced by 600; SQL showed 600 ms discarded per hit. `--set elapsed=1200 --set-hit 3` gave `YNYNN` as predicted. The candidate gave `YNYNY`, and `queries diff` put the first divergence at hit 3. | A `milli` remainder field carried between calls | 20 (2) | 0.8–0.85 s / 0.05 s / 0.6 s |
| `merge` (Java) | At line 32, `current.end` went from 10 to 3 for `1-10 2-3`. Stepping over line 32 changed `merged.elementData[0]`, the same object. `--set end=7` on the constructor's second call gave `[1-7]`, an overwrite and not a merge. The candidate kept `current.end=10` at hit 3. | `Math.max(current.end, next.end)` | 23 (1) | 0.6–0.7 s / <0.1 s / 0.64 s |

All three agents reached `passed: True` on their first `verify`, and each verified fix was general. No call came near a short tool timeout, and no session was left behind. Compared with the second round:
- all three agents probed their fix as a candidate (#68) and read the effect with `queries diff`, before running `verify`;
- the Rust agent read `&[f64]` and `Vec<usize>` contents from memory (#71, #73);
- every agent wrote its own SQL against the evidence store.

## Findings and dispositions

"This PR" is the eval-fixes PR that adds this report.

| # | Finding | Agent(s) | Severity | Disposition |
| --- | --- | --- | --- | --- |
| 1 | `queries hits` puts error text in the `value` column (`<cannot expand 'current' (leaf)>`), and then reports `changed=1` going to `10`. That contradicts the guide's "never treat its error text as a value". | Java | High | **Fixed in this PR.** `hits` and `run` have an `error` column. `value` is NULL when there is none, and `changed` is NULL next to a hit with no value. |
| 2 | After `step` to the loop head (line 8), the owned `counts` showed raw fields with the #53 note, though it was clearly live. | Rust | High | **Fixed in this PR** (a false negative, not a false proof). `vec!` is std code, spanning `alloc/src/macros.rs`. The proof counted code from another file as possibly on any line. Now it counts such code only on a line that can start a macro invocation. rustc collapses another crate's macro onto that line, and a line can start one only if it has a `!`, or ends in a path that a `!` on a later line completes. Separately, a MIR block that codegen emitted into another block's IR block, and that has no code of the line itself, defers to the block that has it. |
| 3 | An uninitialised C local looked like a value: `refill: long = 6134379376 [settable]` one line above its declaration. | C | High | #74. This was deferred in round 2 (finding 17). DWARF's declaration line can mark it. |
| 4 | Rust float scalars printed as `10` (and `9.9999999999999995E-8`) while elements printed `10.0`, so the precision was unclear. | Rust, dry run | Medium | **Fixed in this PR.** An `f64`/`f32` value prints exactly as Rust's `{:?}` (`10.0`, `1e-7`, `-0.0`, `NaN`, `inf`). LLDB prints floats with round-trip precision (17 or 9 significant digits; checked on LLDB 18, 20 and Apple LLDB). Text that is not exactly that rendering stays raw. This is tested against the program's own output and against an emulation of LLDB's formatting over 8000 bit patterns. |
| 5 | A failed `--set` probe printed `status: failed hits: 4 (all)` with no output, so it was unclear how far the program got. | Rust | Medium | **Fixed in this PR:** `hits: 4 before it failed`, and the output so far is shown as `output so far (the program was stopped when the probe failed)`. |
| 6 | `--old/--new` placed after `--` went to the program, which panicked; `NO_STOP` hinted "pick a line the program executes". | Rust | Medium | **Fixed in this PR.** `NO_STOP` gives the exit code (`The program failed (exit code 101) without stopping at …; its output says why`). When an argument after `--` is one of Peeky's own options, the hint says so (`Everything after -- goes to the program, including --old, which Peeky also takes`). A probe that succeeds anyway gets this as a warning. Results carry `exit_code` when the debugger reports one: debugpy and lldb-dap do. java-debug does not, so a Java `NO_STOP` says `exit code unknown` and the hint covers both causes. |
| 7 | No way to know in advance whether `--set` will work: the probe ran to hit 4 and then refused. | Rust | Medium | #77 |
| 8 | `queries diff` compared runs stopped at different lines without saying so. | C | Medium | **Fixed in this PR:** a `(line)` row, `a.py:18` vs `a.py:22`. |
| 9 | `verify` takes one `--old/--new` pair, so adding a field and changing a function forced one large edit. | C | Medium | #76 |
| 10 | `num` is REAL, so `long` values show as `600.0`. | C | Medium | #75. Integers above 2^53 lose precision too. |
| 11 | Objects show only as `Merge$Interval@172`, `ArrayList` has no list view, and fields cannot be set. | Java | Medium | #78. Round 2 deferred a logical `List` view because it runs code; a view from `size` and `elementData` needs none. |
| 12 | A null object said `<cannot expand 'current' (leaf)>`. | Java | Low | **Fixed in this PR:** `'current' is null: it has no members to select`. Other leaves name their value: `cannot expand 'x' (leaf; value 10)`. |
| 13 | A missing name printed as `x:  = <not found; …>` with an empty type. | Rust | Low | **Fixed in this PR:** `x: <not found; visible: …>`. |
| 14 | Scalars use LLDB's C type names (`double`, `unsigned long`); element rows use Rust's. | Rust | Low | #79 |
| 15 | "output differs at character 10" doesn't say it counts from 0. | C | Low | **Fixed in this PR:** `output differs at line 1, column 11`. A new SQL function `difference_position(a, b)` gives this; `first_difference` stays, documented as counting from 0. |
| 16 | `set elapsed: 600 -> 1200` was printed twice (progress and summary). | C | Low | **Fixed in this PR:** only the summary prints it. |
| 17 | `describe` printed `verify bucket.c --at bucket.c` for a single-file target. | C | Low | **Fixed in this PR:** a file target needs no `--at`. |
| 18 | `session get` marks children `(expandable)` while `inspect` says `(children: peeky session get …)`; the cookbook shows `[Locals]` where Java prints `[Local]`. | Java | Low | **Fixed in this PR.** Both print `(children: peeky session get <selector>)`, shell-quoted. The cookbook says scope names are the debugger's own. |
| 19 | A `[[test]]` target named like the `[[bin]]` gave no fix. | dry run | Low | **Fixed in this PR:** `The Cargo [[test]] target must not share the [[bin]] name "histo": rename it in Cargo.toml, for example name = "histo_test"`, before any build. |
