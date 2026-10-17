# Live agent evaluation: three languages from the docs alone (2026-10-16)

## Setup

- **Agents:** three independent general-purpose coding agents, run in parallel with no shared context. Each was given `docs/AGENT_GUIDE.md`, `docs/COOKBOOK.md`, `docs/ERRORS.md`, the `peeky` CLI and one new bug (the program, an independent `check.py`, and a one-line issue). Reading the Peeky source was forbidden.
- **Tool version:** the cookbook branch (#43, `99faeda`), from an editable install in a separate worktree.
- **Cases:** new and written for this run, so they are absent from the corpus and the docs:
  - `wordcount` (C): `count_words` drops the last word unless the text ends with a space.
  - `orders` (Python): a coupon larger than the order makes its total negative; the input is 30 dicts.
  - `median` (Rust): `median` copies its input into a `Vec` but never sorts it.
- **Task:** confirm the cause with runtime evidence, then reach a general fix with `peeky verify` (the checker exercises inputs beyond the issue). The agents were asked to try probes, `--set`, a session, the evidence store and `queries`, and to report friction.

## Outcomes

| Case | Runtime evidence | Verified general fix | Peeky calls | Probe time | Verify time |
| --- | --- | --- | --- | --- | --- |
| `wordcount` (C) | At line 14 `in_word` was 1 and `words` 3 for "the quick brown fox". With a trailing space, `words` was 4. `--hits 19` on the loop and SQL over `last_run` showed that the last character leaves the word pending. `--set words=4` printed `WORDS:4`, and the control `--set in_word=0` changed nothing. | `return words + in_word;` (7 cases) | 15 | 0.62–0.78 s | 0.50 s |
| `orders` (Python) | `--hits 30` and SQL (`WHERE t.num < 0`, joined to the `order` row) found exactly one bad order: id 18, total −11. A sum query predicted 577 once totals are clamped. `--set total=0 --set-hit 19` produced `CHARGED: 577`, and a session reproduced it. | `total = max(0, total - order["coupon"])` (5 cases) | 34 (8 wasted) | about 1.05 s | 0.08 s |
| `median` (Rust) | `answer` was 1 for `5 1 3` and 3 for `1 3 5`; `middle` was 1. `--set answer=3` fixed the output, so the wrong value comes from `answer`, not from printing. The vector's contents could not be read. | `ordered.sort_unstable();` (6 cases, plus both hashed Cargo tests) | 20 | about 1.24 s | 1.33 s |

All three agents reached `passed: True` with a general fix. They worked from the cookbook's recipes, and none read the source or hit a timeout or hang. `queries diff`, `outputs` and `set_effect` and joins over `last_run` were used without help, so the heappy-style workflow carried over to debugging. The median agent noted, but did not fix, overflow in `a + b` for very large inputs; the checker does not test it.

## Findings and dispositions

Ranked by the agents' severity. "This PR" is the evaluation-fixes PR that adds this report.

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| 1 | Python dict keys could not be selected: `order['id']` was an "Invalid selector", and `order.id` said `'id' not found` with no list of keys | High | **Fixed in this PR.** `order['id']` and `order["id"]` read a mapping entry. debugpy names entries by `repr(key)`, so matching is exact. An entry whose key only looks like that value (an object whose repr mimics a string) is refused as ambiguous. `order.id` on a dict names the selector that works and lists the keys. Every not-found row lists the value's children |
| 2 | Rust `Vec`/slice elements read as `'[0]' not found`, which suggests a typo | High | **Message fixed in this PR:** the error says the value has no indexed elements in the debugger's view, lists its fields, says that Rust `Vec`, slice and `String` contents are not readable yet, and names the length field. **Reading the elements** needs a compiler-validated view like `Option<i32>` (Peeky does not run LLDB's Rust formatter scripts) and lldb-dap's `readMemory`, which CI's LLVM 18 lacks: tracked in [#49](https://github.com/sushantmane/peeky/issues/49) |
| 3 | `inspect` said "expand with `get`", but `session get` printed the same unexpanded line | High | **Fixed in this PR.** `session get` lists one level of children (up to 64), each with the selector that reads it (`order['qty']`, `ordered.len`, `values[2]`); `inspect` now hints `(children: peeky session get NAME)` |
| 4 | `describe` lists no function spans or local names for C, C++ or Rust | Medium | **Fixed in the compiled-describe PR:** each function in the declared sources, with its file, line span (line-table rows inside its address range) and DWARF parameters and locals; closures and lambdas under their function |
| 5 | `queries diff` sorts selectors alphabetically, counts selectors captured in only one run as differences, and flags pointer addresses that change on every run | Medium | **Fixed in the evidence-diff PR:** real differences first in hit and capture order, then address-only differences (marked, never hidden), then one row per selector only one run captured; a `note` column says which |
| 6 | `MUTATION_REJECTED` for a Rust `usize` said "Only writable i32 locals", and the hint said "compiled languages: scalars" | Medium | **Fixed in this PR.** The message names the local and its debugger type, and the hint states each language's rule |
| 7 | Sessions report the private build path (`.../peeky-build-*/src/main.rs:5`) | Medium | **Fixed in this PR.** Session states and `inspect` show the user's source path; frames keep the debugger's paths |
| 8 | Sessions cannot step, `continue` has no `--to <line>`, and `peeky session --help` describes only `start` | Medium | **Help fixed in this PR** for every subcommand. **Stepping fixed in the session-stepping PR:** `session step` (over, `--into`, `--out`) for Python, C, C++ and Rust, and `continue --to LINE` for all languages. A step that leaves your sources is reported and refused for reads, never fatal. Java stepping followed in [#48](https://github.com/sushantmane/peeky/issues/48) |
| 9 | No `--hits all` | Low | **Fixed in the evidence-diff PR:** every stop up to 100, with a warning at the cap |
| 10 | A failed `session get` selector exits 0 | Low | **Fixed in this PR:** every row is still printed, and the exit status is 1 |
| 11 | `--hits 30` prints 30 progress lines | Low | **Fixed in the evidence-diff PR:** the first and last hit, otherwise at most one line every 2 s |
| 12 | Undefined terms: "root locals", label syntax, `diff` versus `compare`, the `operations` count, "opaque enum types"; the agent guide's verify example lacks the `--at` that a Rust crate needs | Low | **Fixed in this PR** in the agent guide and the cookbook; `session report` explains `operations` |
| 13 | `verify` does not show the program's own output, or whether the build was fresh | Low | Deferred: `verify` always builds fresh in a scratch copy, and the program's output belongs to the checker |
| 14 | `session get` JSON had `"hit": 0` | Low | **Fixed in this PR:** a session read has no hit number, so the field is omitted |

## Conclusion

Peeky's core loop worked in all three languages without help: observe, change one value to test a hypothesis, then verify a general fix. The agents spent their wasted calls on reading values Peeky did not expose (dict entries, Rust containers) and on messages that pointed the wrong way. This PR fixes those messages, the dict entries, and child listing. Reading Rust container elements and a compiled-language `describe` are the remaining capability gaps.
