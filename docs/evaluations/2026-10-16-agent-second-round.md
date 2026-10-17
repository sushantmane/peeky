# Live agent evaluation, second round: Java, C++ and Rust with stepping (2026-10-16)

## Setup

- **Agents:** three independent general-purpose coding agents, run in parallel with no shared context.
  - Each was given `docs/AGENT_GUIDE.md`, `docs/COOKBOOK.md`, `docs/ERRORS.md`, the `peeky` CLI and one new bug: the program, an independent `check.py` and a one-line issue.
  - Reading the Peeky source was forbidden. Agents could change the program only through `peeky verify --old/--new`.
- **Tool version:** `main` at `cac5628` (#51 Java stepping, #52 Rust reference parameters), installed editable in a separate state directory per agent.
- **Cases:** new for this round, and absent from the corpus and the docs:
  - `restock` (Java, launched): `below()` returns `margin > 0 && available >= 0`. That drops items whose free stock equals the threshold, and over-reserved items. The decision is made inside a stream lambda.
  - `ring` (C++): `Ring::push` advances `head_` modulo `capacity() - 1`. That loses the newest readings after a second wrap and computes `% 0` for capacity 1.
  - `topk` (Rust): `insert_sorted(best: &mut Vec<i32>, score)` uses `position(...).unwrap_or(0)`, so a score below every kept value goes to the front.
- **Task:**
  1. Confirm the cause with runtime evidence.
  2. Use sessions (`step`, `--into`, `--out`, `continue --to`) where natural.
  3. Reach a general fix with `peeky verify`.
  4. Report friction.
- **Dry run:** before the agents started, Peeky's maintainer ran each case. That found three problems the agents would have hit too:
  - a C/C++ directory holding a checker script was detected as a Python target;
  - a crate without `Cargo.lock` gave "Cargo manifest/lockfile must be a bounded regular file";
  - C++ members needed `this.`.

## Outcomes

| Case | Runtime evidence | Verified general fix | Peeky calls (wasted) | Probe / session step / verify |
| --- | --- | --- | --- | --- |
| `restock` (Java) | At `below()` line 20, the issue input gave `available=5, margin=0` and `available=2, margin=2`, with output `[B]`. An over-reserved item gave `available=-2, margin=6` and output `[]`. `--set margin=1` produced `[A, B]`. A session stepped `--into` the stream lambda, then into `below`; `set margin`, then `step --out`, showed `below(): true`. SQL over nine items found the four mismatches. | `return margin >= 0;` (a special-cased fix and a boundary-only fix both failed) | 50 (1) | 0.61–0.73 s / 0.05 s / 0.49–0.60 s |
| `ring` (C++) | `this.head_` went `0,0,0,1,0,1` and `tail` `0,1,2,0,1,0` (output `SUM:14`). Stepping into `push` on the 5th push showed `head_` 1 → 0, where it should be 2. `session set tail=2` gave `SUM:15`. A SQL invariant on capacity 1 flagged `head >= cap`. | `% capacity()` (a fix special-cased to capacity 3 failed) | 54 (0) | 0.92–1.01 s / 0.04–0.08 s / 0.68–0.72 s |
| `topk` (Rust) | The `&mut Vec` parameter was read from memory: `best=[9, 5]`, then `[1, 9]` after `score=1` went to `position=0`. At the right hit, `--set score=6` gave `[9, 7]`. `queries diff` showed the divergence at hit 10. | `.unwrap_or(best.len())` (a fix special-cased to `score == 1` failed) | 57 (5) | 1.2–1.6 s / 0.05 s / 1.24 s |

All three agents reached `passed: True` with a general fix, after `verify` rejected their special-cased attempts with a concrete counterexample. No call came near a short tool timeout, and no session was left behind. Compared with the first round:
- the Rust agent read the vector it needed (#52);
- all three used `step --into`/`--out` and `continue --to` (#47, #51) as documented.

## Findings and dispositions

"This PR" is the eval-fixes PR that adds this report.

| # | Finding | Agent(s) | Severity | Disposition |
| --- | --- | --- | --- | --- |
| 1 | A line shared by a function and a closure stops in both. Hit numbers then mix frames, and `--set-hit` landed in the closure, where `score` is a register copy. `MUTATION_REJECTED` did not say which frame. | Rust | High | **Fixed in this PR.** The probe warns `Line 15 stopped in 2 functions: …::insert_sorted (hits 1, 2, 5); …::insert_sorted::{{closure}} (hits 3, 4, …)`, and the rejection names the frame (`'score' in …::{{closure}}`). Function names read as in `describe` (`{{closure}}`, not `_$u7b$$u7b$closure$u7d$$u7d$`). |
| 2 | `describe`'s commands omit `--config`, and the error then calls a Java directory a Python target. The same misdetection hit a C++ directory holding `check.py` in the dry run. | Java, dry run | Medium | **Fixed in this PR.** `describe` repeats `--config`. Without a config, a Java target gets an error that says what the config needs. A directory with one C/C++ program is that program (`--at ring.cpp:16` names one when there are several). |
| 3 | Session states and `inspect` don't name the current function, so telling the lambda frame from `needsRestock` took three extra calls. | Java | Medium | **Fixed in this PR:** `stopped at …/Restock.java:12 in Restock.lambda$needsRestock$0(Restock$Item)`. |
| 4 | C++ members need `this.` (`head_` gave "not found; visible: tail, this, value"). | dry run | Medium | **Fixed in this PR.** The error names the selector: `this has a member 'head_': select it as this.head_ if the code means the member`. Peeky does not read the member implicitly. The debugger's list of locals can be incomplete (function-local statics, classes without local-variable tables, truncated scopes), so a missing local does not prove the code means the member, as review showed. `session set head_=2` says the member can be read as `this.head_` and that only the function's own locals can be changed. |
| 5 | A lambda line never stops inside the lambda, and nothing warns. | Java | Medium | #55 |
| 6 | An owned `Vec` shows `Vec<…> @ 0x…` with no hint at the point of failure. | Rust | Medium | **Fixed in this PR.** The row's note and a warning say why it is raw (#53), and point to `.len` or the reference parameter. |
| 7 | Rust `set` takes only `i32` (`usize` refused); nothing shows which locals are writable. | Rust | Medium | #58 |
| 8 | Cannot probe the fixed candidate. `verify --keep` says `artifacts_retained: False`, and `verify --json` carries the `lldb-dap --help` text twice. | C++ | Medium | #57 |
| 9 | A crate without `Cargo.lock` gives "Cargo manifest/lockfile must be a bounded regular file". | dry run | Medium | **Fixed in this PR:** "Cargo.lock is missing: run `cargo generate-lockfile` in the crate once". |
| 10 | Eight program arguments needed eight `--arg` flags. | Java | Low | **Fixed in this PR:** `peeky probe t --at 5 -- A 5 5 0` (also for `session start`). |
| 11 | `queries diff` doesn't say the output changed; `queries outputs` omits the set-hit. | Java, C++ | Low | #56 |
| 12 | Reaching the Nth stop in a session is tedious, and the state shows no hit number. | C++ | Low | #59 |
| 13 | `--timeout` has no help text. | C++ | Low | **Fixed in this PR** for `probe` and `session` commands. |
| 14 | `<null>` appears in "visible" lists (a closure's environment). | Rust | Low | **Fixed in this PR:** nameless slots are not listed. |
| 15 | A failing compiled Rust test prints as a raw dict without the test's name. | Rust | Low | #57 |
| 16 | `% 0` (undefined behavior) passes silently on arm64. | C++ | Low | Deferred: a sanitizer build option is a target-configuration feature, not a debugger one. |
| 17 | Uninitialised C++ locals look like values (`tail = 1871229440` before its declaration ran). | C++ | Low | Deferred: DWARF does not say whether a C++ local's declaration has run; the cookbook's advice is to stop after the line that assigns it. |
| 18 | Java `describe` lists no functions or locals; `ArrayList` shows raw fields. | Java | Low | Deferred. A logical `List` view would run code in the target (`toArray`), which Peeky avoids; `items.elementData[i]` with `items.size` works. |
| 19 | A rejected `--set` fails the whole probe instead of continuing as an observation. | Rust | Medium | Kept: a run that silently skipped a requested intervention could be mistaken for its result. The error now names the frame (finding 1). |

## Conclusion

The loop the first round established held up across three more languages: observe, change one value, step to confirm, then verify a general fix. Wasted calls fell (1, 0 and 5, against 8 in the first round's worst case). The remaining friction came from frames sharing a line (closures and lambdas) and from target detection. This PR fixes both kinds, along with the naming and argument issues, and the rest is tracked in #55–#59.
