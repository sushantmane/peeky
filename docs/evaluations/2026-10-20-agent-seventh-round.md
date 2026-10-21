# Live agent evaluation, seventh round: a C ring buffer, a C++ scheduler and a Rust moving average (2026-10-20)

## Setup

- **Agents:** three independent general-purpose coding agents, run with no shared context.
  - Each got `docs/AGENT_GUIDE.md`, `docs/COOKBOOK.md`, `docs/ERRORS.md`, the `peeky` CLI and one new bug: the program, an independent `check.py` and a short issue.
  - Reading Peeky's source was forbidden. Agents could change the program only through candidate probes and `peeky verify --old/--new`.
  - Each had its own state directory.
- **Tool version:** `main` at `5cddb2c`. Since the sixth round, it adds:
  - withholding a C++ local declared below the stop (#140);
  - `verify --baseline` and `[settable]` marks on fields (#141);
  - `queries wide` and `queries join` (#145);
  - C and C++ struct previews (#146);
  - cleanup of a session's orphaned processes (#147);
  - notes kept on fields and children (#150);
  - refusing before launch a `--set` that no stop could apply, with `[not settable: …]` reasons (#149);
  - C block-scope shadowing (#144), Rust async scalars (#135) and C++ fixes (#142).
- **Cases:** new for this round, and absent from the corpus and the docs.
  - `ring` (C): `push` wraps the write index with `% (CAP - 1)` while `pop` wraps the read index with `% CAP`. Slot 3 is never written, and pushes overwrite unread values. `+1 +2 +3 - - +4 +5 +6 - - - -` gives `POPPED:1,2,6,0,4,5`, not `1,2,3,4,5,6`.
  - `sched` (C++): the `std::sort` comparator orders equal priorities by `a.deadline > b.deadline`, so the later deadline runs first. `build:2:30,test:2:10,deploy:1:5,lint:3:50` gives `ORDER:lint,build,test,deploy`, not `lint,test,build,deploy`.
  - `movavg` (Rust, a plain `cargo new` crate): `push` trims the window when `self.values.len() >= self.k` after adding the new value, so the window holds at most K−1 values. `3 3,6,9,12` gives `MEANS:3.0,4.5,7.5,10.5`, not `3.0,4.5,6.0,9.0`, and `K=1` gives `NaN`.
- **Task:**
  1. Confirm the cause with runtime evidence.
  2. Test the hypothesis with an intervention.
  3. Probe the fix as a candidate.
  4. Reach a general fix with `peeky verify`.
  5. Report friction.

## Outcomes

| Case | Runtime evidence | Intervention | Verified general fix | Peeky calls (wasted) | Probe / session step / verify |
| --- | --- | --- | --- | --- | --- |
| `ring` (C) | `--hits all` at line 15: `r.tail` ran `0 1 2 0 1 2` while `r.head` (line 23) ran `0 1 2 3 0 1`, and `r.buf [1, 2, 3, 0]` (#146). A session step at line 16 took `r.tail` from 2 to 0. | `r` is a pointer in `push`, so the agent set `main`'s `r.tail` before the call in a session (`0 -> 3`, then `1 -> 0`): `POPPED:1,2,3,4,5,6`. A single-set probe on another input changed `4,2,3,0` to `1,2,3,4`. | `% (CAP - 1)` → `% CAP` | 33 (3) | 0.85–0.95 s / 0.12–0.15 s / 0.48–0.60 s |
| `sched` (C++) | The comparator stopped once at the deadline rule: `a "test" 2 10`, `b "build" 2 30`. SQL over all 5 calls at line 16 set the code's answer against the specification's; only hit 1 disagreed. | `--set b=8` in the parser made `build`'s deadline 0, and `build` then ran after `test`. A correct comparator would have kept the order. | `>` → `<` | 33 (3) | 1.2–1.7 s / 0.10–0.14 s / 0.75–0.86 s |
| `movavg` (Rust) | `--hits all` at line 18: at hit 3, `self.values.len 2` with `self.sum 15`. A session at line 14, hit 3, showed `self.values.len = 3` before the trim and 2 after. SQL set `self.values.len` against `MIN(hit, k)`. | `--set w.k=4` (#105) gave the expected `MEANS:3.0,4.5,6.0,9.0`, and `--set w.k=2` fixed `K=1`. | `>=` → `>` | 27 (3) | 1.35–2.0 s / 0.12–0.24 s / 1.1–1.2 s |

All three agents reached `passed: True` on their first `verify`, and each fix was general. All three ran `verify --baseline` first (#141) to show the checker fails on the unedited program. Every session was closed, and no call came near a short tool timeout.

## Findings and dispositions

"This PR" is the eval-fixes PR that adds this report.

| # | Finding | Agent(s) | Severity | Disposition |
| --- | --- | --- | --- | --- |
| 1 | No way to read a function's return value. After `session step --out` from the comparator into `std::sort` (outside the program's sources), `session inspect` refused everything, although COOKBOOK 11 says `inspect` lists `(Return Value)`. Probes can't capture a return value either, so the agent derived the comparator's answers in SQL. | C++ | High | #155. |
| 2 | A line can stop several times per loop pass with no warning. `movavg.rs:29` (`format!`) stopped 12 times for 4 values, so `--hits N` and `--set-hit N` count stops, not passes. | Rust | Medium | #157. |
| 3 | `self.values` (a `Vec<i64>` field behind `&mut self`) shows only raw fields, with a warning that gives the wrong reason ("owned … may be moved, dropped or not yet assigned", #53). `self.values[0]` is not found, and the hint names `values.len` instead of `self.values.len`. In `main`, `w.values` stays raw beside a `[settable]` `w.sum`. | Rust | Medium | #156. |
| 4 | `--set t.deadline=-30` at `sched.cpp:32` was refused because the function declares two locals named `t` of different types, although only the loop's `Task t` is in scope there. The rule isn't in the docs, and `describe` doesn't flag it. | C++ | Medium | #158. |
| 5 | `session continue --to 17` stopped first at the session's own breakpoint (line 30, hit 2), with no note that line 17 wasn't reached. | C++ | Medium | This PR. |
| 6 | AGENT_GUIDE and COOKBOOK 7.1/7.2 are still dense. The simple rules ("set a field in the frame that owns the struct", "a `Vec` field behind `&mut self` stays raw", "`--to` doesn't skip the session's breakpoint") sit inside long paragraphs of edge cases. | all three | Medium (docs) | This PR, or a docs PR after the feature PRs above merge. |
| 7 | `v`, a `&str` borrowing `args` (which lives until the end of `main`), showed `<not shown: may point at freed memory> [unproven]`. | Rust | Low | #156. |
| 8 | Every probe repeats `note: function shows short frame names; … short_function(function) …`, even with `--quiet`. | Rust, C++ | Low | This PR. |
| 9 | `queries diff` repeats an address-only row for the same selector once per hit. | Rust | Low | This PR. |
| 10 | `queries diff` lists `after-set` rows equal to the observed value (`r.buf`, which the intervention never touched) as differences. | C | Low | This PR. |
| 11 | `verify --baseline` gives `reason: SystemExit: 1`. The useful line, the checker's `FAIL …`, is only in `output`. | C, C++ | Low | This PR. |
| 12 | After a candidate probe, a plain `queries diff` compares it with the latest run, which may have other arguments or a `--set`. Nothing names the matching baseline run. | C | Low | This PR. |
| 13 | `MUTATION_REJECTED` for a pointer parameter says to change the value "before the call that passed it", but doesn't name the call line, although `push` has a single caller (`ring.c:36`). | C | Low | This PR. |
| 14 | A probe refused before launch printed an empty table header and exited 1, not 2. | C | Low | This PR. |
| 15 | `session get a.name` at the wrong stop printed `<cannot expand 'a' (leaf; value 4)>` without naming the stop's function (`main`). | C++ | Low | This PR. |
| 16 | `describe`'s `mutable:` line lists only register-bound locals; it doesn't say that fields (`w.k`) are settable, nor flag same-named locals. | Rust, C++ | Low | This PR (fields) and #158 (same-named locals). |
| 17 | `describe` prints `r (ring)` for a C `struct ring` with no typedef. | C | Low | This PR. |
| 18 | The type column mixes LLDB and Rust names: `self.k` and `w.k` show `unsigned long`, while `k` shows `usize`. | Rust | Low | This PR. |
| 19 | From line 16, `session step` went back to the `if` on line 14 before line 18, with no note. The docs mention only a closing `}` and loop headers. | Rust | Low (docs) | This PR. |
| 20 | `queries diff` pairs rows by hit number, so for a sort comparator the decisive difference (hit 1's answer) is hidden; every later hit shows as a difference. | C++ | Low | This PR. |
| 21 | AGENT_GUIDE's command table doesn't list `session wait`, which `session start --help` mentions. | C | Low (docs) | This PR. |
| 22 | Passing program arguments (`--`, `--arg`) is explained only under "Python programs with several files". | C | Low (docs) | This PR. |

## What worked

- **`verify --baseline` (#141):** all three agents used it first, unprompted, to show the checker fails on the original program before trusting `passed: True`.
- **The new readers were used as intended:**
  - The C agent read `r.buf` as `[1, 2, 3, 0]` (#146), which showed the never-written slot.
  - The Rust agent read `self.values.len` beside `self.sum` at each hit.
  - The C++ agent read `a.name`/`b.name` as strings in the comparator.
- **The pointer refusal led to the right experiment:** the C agent followed `MUTATION_REJECTED`'s advice and set `r.tail` in the frame that owns `r`.
- **SQL carried the reasoning in all three cases:** the two indexes' wrap points, the comparator's answers against the specification's, and the window's length against `MIN(hit, k)`.
- **Diffs:** `queries diff` with `(output)` and `(edit)` rows confirmed each candidate before `verify`.
- **No wrong or guessed value reached an agent.** Every value that was unavailable was withheld with a reason, though finding 3's reason was the wrong one.
