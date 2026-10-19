# Live agent evaluation, fifth round: Rust structs, a C unsigned sum and a Python mutable default (2026-10-21)

## Setup

- **Agents:** three independent general-purpose coding agents, run with no shared context.
  - Each got `docs/AGENT_GUIDE.md`, `docs/COOKBOOK.md`, `docs/ERRORS.md`, the `peeky` CLI and one new bug: the program, an independent `check.py` and a short issue.
  - Reading Peeky's source was forbidden. Agents could change the program only through candidate probes and `peeky verify --old/--new`.
  - Each had its own state directory.
- **Tool version:** `main` at `24d2760`. It includes:
  - Rust tuple and struct elements (#98);
  - withheld C/C++ locals on their own declaration line (#99);
  - C/C++ pointer pointees (`p[0]`, #100);
  - the evidence and `describe` gaps of the fourth round (#97).
- **Cases:** new for this round, and absent from the corpus and the docs.
  - `spans` (Rust, a plain `cargo new` crate): merging overlapping spans sets `last.end = s.end`, so a span inside the previous one shortens it. `1-10,2-3` gives `1-3`.
  - `average` (C): the sum is `unsigned`, so a negative sum wraps and the division is unsigned. `-3 1` gives `2147483647`.
  - `tags` (Python): `normalize(word, seen=[])` shares its default list across calls, so tags already seen on one line vanish from the next.
- **Task:**
  1. Confirm the cause with runtime evidence.
  2. Test the hypothesis with an intervention.
  3. Probe the fix as a candidate.
  4. Reach a general fix with `peeky verify`.
  5. Report friction.
- **Dry run:** before the agents started, Peeky's maintainer ran each case. That found that `values[0]` on a C `const int *` parameter said only "children: *values", which became #100. The dry run missed `describe`'s failure on the Rust crate (finding 2): the maintainer's check looked for one scope line and did not notice the "unavailable" line.

## Outcomes

| Case | Runtime evidence | Verified general fix | Peeky calls (wasted) | Probe / session step / verify |
| --- | --- | --- | --- | --- |
| `spans` (Rust) | At line 23, `s.end=3 last.end=10`, and `out` read as `[Span { start: 1, end: 10 }]` (#98). After a session step, `out[0]` was `Span { start: 1, end: 3 }`. SQL over several inputs showed the end shrinking on every merge. | `last.end = last.end.max(s.end)` | 24 (3) | 1.3–1.45 s / 0.1 s / 1.25 s |
| `average` (C) | `describe` showed `total (unsigned int)` (#97). At line 9, `total=4294967294`. `queries hits` showed -3 already wrapped at the second iteration. SQL reproduced `4294967294 / 2 = 2147483647` exactly. | `long long total = 0;` | 24 (2) | 0.69–0.77 s / 0.1 s / 0.59 s |
| `tags` (Python) | `--hits all` showed `seen ['a', 'b']` when line 2 began. A SQL join with the caller's per-line `tags = []` showed that only `seen` leaked. A session step showed `key = None` for `b`. | `seen` passed in from `unique_tags`, one list per line | 30 (4) | 1.0–1.1 s / 0.1 s / 0.56 s |

All three agents reached `passed: True` on their first `verify`, and each fix was general. Each tested its hypothesis with an intervention:
- **C:** `--set count=1` printed `AVG:-2`, which shows the stored bits hold the right sum.
- **Python:** `--set 'seen=[]' --set-hit 4` printed `TAGS:b|c`, and the next call saw the shared list again.
- **Rust:** nothing at the line was settable, so the agent added a local through a candidate edit and set it (`set new_end: 3 -> 10`, `MERGED:1-10`).

No session was left open, and no call came near a short tool timeout.

## Findings and dispositions

"This PR" is the eval-fixes PR that adds this report.

| # | Finding | Agent(s) | Severity | Disposition |
| --- | --- | --- | --- | --- |
| 1 | `probe --old/--new` was refused for Python ("needs a target Peeky builds … edit the file"), so a fix could not be tried before `verify`, and editing the file is against the rules. The agent probed `verify --keep`'s copy instead. | Python | High | **Fixed in this PR:** a Python candidate is a private copy of the target root with the edit applied, run from there, as a compiled candidate runs from a fresh build. Whatever the script reaches through its own path, an import or another process is the copy, so all of it runs the edit. The review took four rounds to reach this design: a bare copy, then a bootstrap faking `__file__`, then a symlink mirror each misattributed values or, once, wrote the edit into the user's file (see the review record). Nothing above the root is copied, so unless the root is a project's the result says so, and nothing the program writes reaches the user's tree (a private git directory; caches left out). `--run` entry scripts are refused. |
| 2 | `describe` on the 37-line crate said `function names: unavailable (Native symbol metadata exceeds limit)`. The crate's unit holds every std generic it instantiates, more than the 2 MB default. | Rust | Medium | **Fixed in this PR:** `describe` reads the unit with the same 16 MB bound as `prepare`. Test: `test_describe_reads_a_unit_full_of_std_generics` (this case's program). |
| 3 | `queries diff` showed `values[0] NULL NULL … only in run a` for a selector run a had captured (`-3`): NULL read as "no value". | C | Medium | **Fixed in this PR:** selector-only rows show the selector's first hit and its value. |
| 4 | Comparing a run with `verify --keep`'s copy gave no row saying the runs came from different source text. | Python | Medium | **Fixed in this PR:** a `(source)` row appears when two non-candidate runs ran different source text. |
| 5 | Nothing at the buggy Rust line could be `--set` (struct fields, a reference, a `Vec`). | Rust | Medium | #105 (field and element `--set` for Rust, C and C++, with proofs). **This PR:** COOKBOOK 6.6 shows how to add a settable local with a candidate edit in the same probe. |
| 6 | `--set s.end=10` was refused only after a build and a run (1.34 s, saved as a failed run). The hint promised a list of settable locals that did not appear. | Rust | Low | **Fixed in this PR:** a field or element `--set` outside Java is refused before anything is built (0.06 s), and the message points to the recipe. |
| 7 | A new local `let mut new_end` was "kept in a register" in a debug build. | Rust | Medium | **Fixed in this PR:** the message suggests `std::hint::black_box(&mut x);`, which the agent found worked. |
| 8 | Sessions cannot read caller frames or module globals, and Python values carry no identity. | Python | Medium | #104. |
| 9 | Struct locals show raw (`spans::Span @ 0x…`) while `Vec<Span>` is decoded. | Rust | Low | #106. |
| 10 | The diff did not mention that the fix changed `total`'s type. | C | Low | **Fixed in this PR:** `type unsigned int -> long long` in the row's note. |
| 11 | `session inspect` lists `(Return Value)` after `step --out`, but no selector reads it, and the docs never mention it. | C | Low | **Fixed in this PR:** marked `[not selectable]`; the gdb table says where the returned value appears. |
| 12 | `--set total=-2` on an `unsigned int` failed only after a run, with a generic hint. | C | Low | Not changed. The range check needs the debugger's type, known only at the stop. The message already gives the type's range. |
| 13 | `describe` lists a `for` loop's `i` among the names of a line after the loop. | C | Low | #107 (each local's block span). |
| 14 | `runs.set_hit` was 1 for runs without `--set`, while `queries outputs` showed NULL. | Python | Low | **Fixed in this PR:** NULL without `--set`. |
| 15 | `describe --quiet` was rejected by the argument parser, even with `--json`. | Rust | Low | **Fixed in this PR:** accepted. |
| 16 | In Python, `--set` rebinds the name, so it cannot change a shared default list. | Python | Low (docs) | **Documented in this PR**, in COOKBOOK 2.4. |
| 17 | `queries diff` compares the last two runs unless given `--param a=… --param b=…`, which only one cookbook line said. | C | Low (docs) | **Documented in this PR**, in the guide's queries line and the cookbook's `diff` section. |
| 18 | The cookbook's "higher indexes are read where the array is declared" sounded as if Peeky reads them. | C | Low (docs) | **Fixed in this PR:** "read the array itself where it is declared (in the caller)". |
| 19 | `verify --keep` leaves its copy in the system's temporary directory with no cleanup command. | Python | Low (docs) | **Documented in this PR:** where it is, and that a candidate probe is simpler for trying an edit. |
| 20 | The cookbook (45 KB) is too large to read at once. Progress lines go to stderr without `--quiet`. | Python, C | Low | Not changed. The guide is the one-read entry point, under 20 KB, and progress on stderr is by design. |
| 21 | The `evidence` view has `program` but not `target`, so same-named files from different directories look alike. | Python | Low | Not changed. `runs.target` has it, and the new `(source)` row flags the comparison. |

## What worked

- **The new readers were used as intended:**
  - The Rust agent read `out` and `spans` as `[Span { start: 1, end: 10 }]` without capturing fields one by one (#98).
  - The C agent's first command, `describe`, showed `total (unsigned int)` (#97).
  - Its later `--at 9` stop and its `values[0]` capture read correctly (#99, #100).
- **SQL carried the reasoning in two of three cases:**
  - C: the unsigned arithmetic, reproduced exactly.
  - Python: a per-hit join of caller and callee state.
- **Diffs:** `queries diff` with `(edit)`, `(args)` and `(sets)` rows kept the agents from crediting the wrong change.
