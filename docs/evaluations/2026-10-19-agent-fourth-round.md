# Live agent evaluation, fourth round: Java aliasing, C integer overflow and a plain Rust crate (2026-10-19)

## Setup

- **Agents:** three independent general-purpose coding agents, run with no shared context.
  - Each got `docs/AGENT_GUIDE.md`, `docs/COOKBOOK.md`, `docs/ERRORS.md`, the `peeky` CLI and one new bug: the program, an independent `check.py` and a one-line issue.
  - Reading the Peeky source was forbidden. Agents could change the program only through candidate probes and `peeky verify --old/--new`.
  - Each had its own state directory.
- **Tool version:**
  - The Java and C agents used `main` at `ce57d75`. It includes Java object previews (#78), Java field and element `--set` (#86), several edits per verify (#76), exact 64-bit `num` (#75) and settable markers (#77).
  - The Rust agent used #89's branch, because the dry run found that a plain `cargo new` crate was refused (below).
- **Cases:** new for this round, and absent from the corpus and the docs:
  - `balance` (Java, launched): the least-loaded worker takes each task, ties go to the alphabetically first name. The tie branch copies the worker (`best = new Worker(w.name, w.load)`), so the load added to it is lost and every task goes to `a`. It only shows when the workers are not listed in name order.
  - `stats` (C): `long long us = ms * 1000;` multiplies in `int`, so durations above 2,147,483 ms wrap.
  - `rle` (Rust, a plain `cargo new` crate, edition 2024, no tests): run-length encoding pushes the final run only `if count > 1`, so `aaab` encodes as `3a`.
- **Task:**
  1. Confirm the cause with runtime evidence.
  2. Test the hypothesis with an intervention.
  3. Probe the fix as a candidate.
  4. Reach a general fix with `peeky verify`.
  5. Report friction.
- **Dry run:** before the agents started, Peeky's maintainer ran each case. That found three problems:
  - **Plain crates refused:** a plain `cargo new` crate failed with "Rust sources and tests must be nonempty immutable tuples". Peeky accepted only explicit `[[bin]]`/`[[test]]` targets with every discovery switch off, and edition 2021.
  - **Rust chars:** `char` values showed as LLDB's `U+0062 U+0062 U'b'`.
  - **Unread Vecs:** a `Vec<(char, usize)>` showed its type and address as if that were its value.

## Outcomes

| Case | Runtime evidence | Verified general fix | Peeky calls (wasted) | Probe / session step / verify |
| --- | --- | --- | --- | --- |
| `balance` (Java) | At line 29, `best` was a new object at every hit (`Worker@56`, `@104`, …), while the list's `a` stayed `Worker@100 {load=0}`. SQL on the `@N` prefixes showed `best` never was a list element; in the candidate it was. A session step over line 26 showed the copy being made. | `best = w;` in the tie branch | 33 (3) | 0.75 s / 0.05–0.09 s / under 3 s |
| `stats` (C) | At line 12, `ms=3000000 us=-1294967296`. Over `--hits all` on boundary input, a SQL join showed `us - ms*1000 = -4294967296` exactly where `ms > INT_MAX/1000`, and 0 elsewhere. | `(long long)ms * 1000` | 27 (1) | 0.66–0.72 s / 0.05 s / 0.56 s |
| `rle` (Rust) | At line 18 with `aaab`, `count=1 current='b'`. A session step went 18 → 21, skipping the push, and `out.len` stayed 1. `queries diff` of `aaab` against `aaabb` put `count 1/2` beside `RLE:3a`/`RLE:3a2b`. | Push the final run unconditionally | 29 (2) | 1.1–1.2 s / 0.11 s / 1.1 s |

All three agents reached `passed: True` on their first `verify`, and each verified fix was general. Each tested its hypothesis with a predicted intervention:
- the Java agent set a list element's field, `--set 'workers[1].load=5'` (#86), and got `a b b b` as predicted;
- the C agent set `us=3000000000`;
- the Rust agent set `count=2`, and the reverse on `aaabb`.

Each also probed its fix as a candidate and compared it with `queries diff`. No session was left open, and no call came near a short tool timeout.

## Findings and dispositions

"This PR" is the eval-fixes PR that adds this report. "#89" is the plain-crate PR.

| # | Finding | Agent(s) | Severity | Disposition |
| --- | --- | --- | --- | --- |
| 1 | A plain `cargo new` crate (edition 2024, no `[[bin]]`/`[[test]]`, empty `[dependencies]`) was refused with "Rust sources and tests must be nonempty immutable tuples"; the required shape was not in the agent docs. | dry run | High | **Fixed in #89.** Plain binary crates work, with or without one integration test (`tests/*.rs`). Edition 2024 was qualified on the whole Rust suite. Anything else Cargo would build is refused with the reason. |
| 2 | Rust `char` values showed as `U+0062 U+0062 U'b'` (Apple LLDB) or `U+0x00000062 …` (LLVM). | dry run | Medium | **Fixed in #89:** `'b'`, `'\n'`, `'\u{200d}'`, as Rust's `{:?}` prints them. The categories used are certain for all 1,112,064 scalar values, checked against Rust's own output; the rest stay raw. |
| 3 | `get 'out[0]'` on a `Vec<(char, usize)>` blamed freed memory ("a moved, dropped or not-yet-assigned local"); the real reason is the element type. The row itself showed LLDB's summary as if it were the value. | Rust, dry run | Medium | **Fixed in #89:** the row has a note ("LLDB's summary, not the contents"), and indexing names the element type. Reading such elements is #90. |
| 4 | `Vec<(char, usize)>` contents cannot be read at all. | Rust | Medium | #90: char elements, and tuples by their DWARF member offsets. |
| 5 | `queries diff` treats Java object numbers as noise, so "is `best` the list's element?" took `substr`/`instr` SQL. | Java | Medium | **Fixed in this PR:** `object_ref(value)` is the object a value is; equal within one run means the same object. It is in the guide and in cookbook 4.6, with a new example (`examples/java/demo/BalanceBug.java`). |
| 6 | `queries diff` between runs on different input showed only the edit, so the input's effect was easy to credit to the edit; a `--set` run could be the default pair. | Rust | Medium | **Fixed in this PR:** `(args)` and `(sets)` rows when the runs' program arguments or interventions differ. |
| 7 | At a breakpoint on `long long us = ms * 1000;`, `us` showed a leftover value marked `[settable]`. | C | Medium | **Guide made explicit in this PR** (in bold, with `[settable]`). The exact fix needs the stop address against the line's first address, since one-line loops and `step --out` stop mid-line (review of #81, finding 10): #92. |
| 8 | `--until hits` labeled complete output as incomplete, and gave no speedup for C. | C | Medium | **Documented in this PR:** a program that finishes right after the last hit may have printed everything; `--until hits` saves most for slow exits (JVMs, Python). |
| 9 | `job wait --json` printed text for a job started without `--json`. | Java | Low | **Fixed in this PR:** jobs always record JSON; `job wait` prints JSON with `--json` (here or at submission), else the foreground text. |
| 10 | `verify` had no `--timeout` or `--quiet`, and no duration. | Java, C, Rust | Low | **Fixed in this PR:** `--timeout S` bounds the whole verify, build included (`TIMEOUT`); `--quiet` is accepted; results carry `seconds` (also in the text headline and `job list`). |
| 11 | `verify` did not say that no Cargo tests ran. | Rust | Low | **Fixed in #89:** `compiled tests: none (the crate has no integration test; the checker decides)`. |
| 12 | `session get workers` listed `elementData`, `modCount` and `size`, not the elements. | Java | Low | **Fixed in this PR:** an `ArrayList`'s children are its elements, each with its selector (`workers[0]`). |
| 13 | `--capture 'stats->total_us'` was refused without saying what to write. | C | Low | **Fixed in this PR:** `follow a pointer with . (not ->): stats.total_us`. |
| 14 | `AGENT_GUIDE.md` (20.4 KB) came back truncated on the first read. | C | Low | **Fixed in this PR:** 19.7 KB. Rust value details moved to RUST_AGENT.md, and a test keeps the guide under 20 KB. |
| 15 | The checker's descriptor (`command`, `environment`) was described only in NATIVE_AGENT.md. | C | Low | **Fixed in this PR:** cookbook 6.5 says how to run each case. |
| 16 | A Java `step` from line 26 landed on the block's `}`, where `w` was gone. | Java | Low | **Documented in this PR:** read a block's locals before stepping out of it. |
| 17 | Rows did not say that a preview's nested field (`workers[1].load`) can be set. | Java | Low | **Documented in this PR:** `workers[1].load=5` is the guide's example. |
| 18 | `describe` gives no Java scopes. | Java | Low | #91 |
| 19 | The `evidence` view has no `output` column; `describe` lists no types; `/private/tmp` paths; `inspect` omits `hit N`. | Rust, C | Low | #93 |
| 20 | An explicit `--set-hit 1` still warns that interventions apply once. | Java | Low | Kept: the warning states what happened at the other hits. |
| 21 | `out.len` shows `unsigned long` beside `count: usize`. | Rust | Low | Kept (documented): a field's ambiguous LLDB name stays when it is i64/isize or u64/usize. |
| 22 | `step --out` from `add` reported line 24 (the loop's `}`), not the call on line 23. | C | Low | Kept: that is where LLDB returns, after the call. |
