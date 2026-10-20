# Live agent evaluation, sixth round: a Rust rate limiter, a C++ stock map and a C histogram (2026-10-20)

## Setup

- **Agents:** three independent general-purpose coding agents, run with no shared context.
  - Each got `docs/AGENT_GUIDE.md`, `docs/COOKBOOK.md`, `docs/ERRORS.md`, the `peeky` CLI and one new bug: the program, an independent `check.py` and a short issue.
  - Reading Peeky's source was forbidden. Agents could change the program only through candidate probes and `peeky verify --old/--new`.
  - Each had its own state directory.
- **Tool version:** `main` at `a4e3c77`. Since the fifth round, it adds:
  - Rust struct and tuple previews (#106);
  - `--set` on scalar fields and elements in Rust, C and C++ (#105);
  - libstdc++ strings with NUL bytes (#128) and C++ map lookups by key (#119);
  - Python caller frames and globals (#104);
  - per-block `describe` spans (#107);
  - macro-stop narrowing (#115);
  - the C/C++ withholding of values an initializer, a jump or a constructor leaves unset (#121).
- **Cases:** new for this round, and absent from the corpus and the docs.
  - `limiter` (Rust, a plain `cargo new` crate): a fixed-window rate limiter opens a new window only when `now - w.start > width`, so a request at exactly `start + width` is counted in the old, full window. `2 10 0,1,10,11` gives `ACCEPTED:0,1,11`, not `0,1,10,11`.
  - `stock` (C++): `trim_left` strips only leading spaces, so `"apple "` and `"apple"` become different `std::map` keys. `"apple:3, pear:2, apple :4,pear :1"` gives `STOCK:apple=3,apple =4,pear=2,pear =1`.
  - `histo` (C): bins are half-open, `[lo, hi)`, but the last bin's upper bound is `HI` itself, so a value equal to `HI` lands in no bin. `0 7 0 1 2 3 4 5 6 7` gives `HISTO:2,2,2,1`.
- **Task:**
  1. Confirm the cause with runtime evidence.
  2. Test the hypothesis with an intervention.
  3. Probe the fix as a candidate.
  4. Reach a general fix with `peeky verify`.
  5. Report friction.
- **Dry run:** Peeky's maintainer ran each case before the agents started. It found nothing blocking, but noted three gaps the agents then reported too: no C/C++ struct previews, `Vec<String>` not decoded, and long libc++ type names in `describe`.

## Outcomes

| Case | Runtime evidence | Intervention | Verified general fix | Peeky calls (wasted) | Probe / session step / verify |
| --- | --- | --- | --- | --- | --- |
| `limiter` (Rust) | At line 10, hit 3: `now 10`, `w.start 0 [memory]`, `w.count 2`, `width 10`, read as `Window { start: 0, count: 2 }` (#106). A session step skipped the reset. SQL over the run computed `elapsed=10 resets_as_coded=0 should_reset=1`. | `--set w.start=10 --set w.count=0 --set-hit 3` at line 30 in `main` (#105) printed `ACCEPTED:0,1,10,11`. | `>` → `>=` | 27 (3) | 1.35–1.47 s / 0.12 s / 0.97–1.16 s |
| `stock` (C++) | At line 24, `--hits all`: `name "apple "`, `"pear "`. At line 9: `s " apple "`, `i 1`. `session get stock` read `stock["apple "]=4` (#119). | In a session, `colon` was shortened on each bad hit and restored at the next line; `report` showed `STOCK:apple=7,pear=3`. | `trim_left` trims both ends | 31 (0) | 1.06–1.11 s / 0.10–0.14 s / 0.89 s |
| `histo` (C) | Probe of all eight bounds: `bins[3]` is `[6, 7)`. `--hits all` at the increment: 7 hits, none for `v=7`. SQL over two runs: `v=7 bin_tests=4 counted_in_bin=NULL`. | `--set bins[3].hi=8` (#105) printed `HISTO:2,2,2,2`. | The last bin's upper bound is inclusive | 22 (1) | 0.86–1.19 s / 0.12 s / 0.65–0.76 s |

All three agents reached `passed: True` on their first `verify`, and each fix was general. Two of the three interventions used the new field `--set` (#105). The third needed a session, because a `std::string` can't be set and two linked changes can't be made in one probe (#137). No session was left open, and no call came near a short tool timeout.

## Findings and dispositions

"This PR" is the eval-fixes PR that adds this report.

| # | Finding | Agent(s) | Severity | Disposition |
| --- | --- | --- | --- | --- |
| 1 | At `stock.cpp:22`, `qty` (declared on line 23) showed `6712214884 [settable]`, then the previous iteration's `3`, and `--set qty=99` "succeeded" although line 23 overwrites it. `name`, declared on line 22 itself, was withheld correctly. A wrong value with no note breaks Peeky's core rule. | C++ | High | Fixed in its own PR (`fix/cpp-declared-below`). |
| 2 | A C struct array shows only its address (`bins  bin[4] @ 0x…`); the agent listed eight selectors by hand. `session get bins` lists one level only. | C | Medium | #136. |
| 3 | No way to set a `std::string`, or to make two linked changes in one probe. The natural experiment took 14 session calls. | C++ | Medium | #137. |
| 4 | `--set width=9` on a register-held Rust parameter was refused only after running to hit 3, and the run's output was lost. Rows carried no hint. | Rust | Medium | #138. |
| 5 | No way to run the checker against the unedited program: `verify --old X --new X` is rejected, and `verify --help` shows the edit as optional. The agent used a whitespace-only edit to show the checker fails on the original. | Rust | Medium | Done: `peeky verify <target> --check check.py --baseline` builds and checks the unedited program like any verify (`passed: False/True`, the checker's output, `baseline: true`, no diff); `verify --help` shows `--old`/`--new` as required unless `--baseline`. |
| 6 | COOKBOOK 7.1 and 7.2 are dense: single paragraphs of several hundred words on edge cases, so the simple rule (struct fields are settable at a statement's first line) was hard to find. The `MUTATION_REJECTED` hint repeats about 80 words of general rules. | C, Rust | Medium (docs) | Done: a quick start per language at the top of COOKBOOK 7.1 and 7.2, the densest paragraphs (7.1, 7.2, 2.3) as lists, and a `MUTATION_REJECTED` hint that names the one action the broken rule leaves (stop at the statement's first line, change it in its own frame, keep a register-held local in memory, ...). |
| 7 | Settable fields aren't marked `[settable]`, so an observation probe can't tell whether `bins[3].hi` or `w.start` can be changed. A field read through `&mut` and one in the frame look identical. | C, Rust | Low | Done: probe rows, `session get` listings and a preview's fields carry `[settable]` where `set` would change them, judged by the rules `set` applies (`validate_member`; the root's once per listing), never through a pointer, reference or Box, nor in a caller frame. Verdicts are kept per place (the stack's functions and pcs; Rust's stopped frame), so a loop's later hits reuse them. Measured with `--hits all` (100 hits, 8 field selectors): Rust 4.85 s on `a4e3c77`, 4.97 s (+2.5%; 7.63 s before the cache); C 4.31 s, 4.39 s (+2%; 4.63 s before). |
| 8 | `session inspect` at a loop header's later pass marks nothing `[settable]` and gives no reason. | C | Low | Done: `session inspect` adds `note: nothing here is [settable]: <locals>: <the rule>` (a statement part-way through, a loop header's later pass, an inlined call), naming the locals each reason holds for, only when no root, field or element is settable; JSON `unsettable`. |
| 9 | `queries diff` puts `only in run a` rows for selectors one run didn't capture before the decisive `(output)` row. | C | Low | Done: `queries diff` puts `(status)`, `(output)`, `(edit)`, `(source)`, `(line)`, `(args)` and `(sets)` first, then the first divergence; selectors only one run captured come last. |
| 10 | Comparing several selectors per hit needs SQL self-joins, and linking runs by a value needs a correlated subquery. | C, Rust | Low | #139. |
| 11 | The identical-`--old`/`--new` error names two conditions without saying which failed. | Rust | Low | Done: the error says `--new equals --old`, or `--old matched N times` (both, when both fail). |
| 12 | `describe` lists `println!`'s internal `args` (`core::fmt::rt::Argument[1]`) beside the program's own `args`. | Rust | Low | Done: a binding the build's MIR spans in another crate's file (`println!`'s `args`) is left out where the program's sources declare no binding of that name on that line. |
| 13 | A Rust `&mut Window` parameter's type shows as `limiter::Window *`. | Rust | Low | Done: from the build's IR, the instantiation's own type (`&mut limiter::Window`; LLDB's stays in `debugger_type`). |
| 14 | `session report` prints `operations: 53` after seven commands, with no explanation. | C | Low | Done: dropped from the text (the worker's requests, several per command); `--json` keeps `history`. |
| 15 | `session get name` (a `std::string`) lists a junk child row, `[not selectable]: std::basic_string<char>::(anonymous struct) @ 0x…`. | C++ | Low | Fixed in the C++ eval PR (`fix/eval6-cpp`). |
| 16 | The `function` column shows `trim_left(std::__1::basic_string<char, …> const&)`, about 100 characters, so the table is about 200 columns wide. | C++ | Low | Fixed in the C++ eval PR. |
| 17 | `describe` gives `colon (size_t, lines 17-35)` and `name (lines 22-35)` for locals of a `while` body and an `if` block, wider than their blocks. | C++ | Low | Fixed in the C++ eval PR, or explained there. |
| 18 | A `const std::string &` capture stores its address in `value` (`0x000000016b493220 "apple"`), so joins with plain string columns need `no_addresses()`. | C++ | Low (docs) | Documented in the C++ eval PR. |
| 19 | No doc says every later session command needs `--name` again, nor shows reading a `std::map` by string key in a session. | C++ | Low (docs) | Done: COOKBOOK 5.3 (every session command takes `--name`) and 7.1 (`session get 'ages["al"]' ages --name maps`). |
| 20 | The docs don't relate `describe`'s line ranges to stepping (a `step` stops on a closing `}`). | C | Low (docs) | Done: COOKBOOK 1.1, "Line ranges and stepping". |
| 21 | No doc explains how to confirm the checker fails on the original program before trusting `passed: True`. | Rust | Low (docs) | Done: AGENT_GUIDE's verify section and COOKBOOK 6.1 (`--baseline`). |

## What worked

- **The new readers and writers were used as intended:**
  - The Rust agent read `w` as `Window { start: 0, count: 2 }` (#106) and changed `w.start` and `w.count` in one probe (#105).
  - The C agent changed `bins[3].hi` (#105) and saw the predicted output.
  - The C++ agent read `stock["apple "]` by key in a session (#119).
- **SQL carried the reasoning in all three cases:** the limiter's reset condition, the trimmed names across two stops, and which bin each value reached.
- **Diffs:** `queries diff` with `(output)` and `(edit)` rows confirmed each candidate before `verify`.
- **No guessed values reached an agent, except finding 1,** which is fixed separately.
