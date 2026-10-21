# Live agent evaluation, eighth round: a Java calculator, a Python title grouper, a C++ router and a Rust leaderboard (2026-10-21)

## Setup

- **Agents:** four independent general-purpose coding agents, run with no shared context.
  - Each got `docs/AGENT_GUIDE.md`, `docs/COOKBOOK.md`, `docs/ERRORS.md`, the `peeky` CLI and one new bug: the program, an independent `check.py` and a short issue.
  - Reading Peeky's source was forbidden. Agents could change the program only through candidate probes and `peeky verify --old/--new`.
  - Each had its own state directory.
- **Tool version:** `main` at `1ea0e6d`. Since the seventh round, it adds:
  - several changes at their own lines and hits, `--set-at LINE@HIT` (#151);
  - a warning and a `stop` label where a line stops several times per pass (#160), and a note where a loop header stops only before its loop (#166);
  - C and C++ `--set` picking the same-named local the code names (#159);
  - return values: `--capture return` and `session step --out` (#162);
  - Rust `Vec`, `String`, `&str` and `Box<str>` fields read from memory (#163);
  - texts set in place: C `char[N]`, C++ `std::string`, Rust `String`, Java String (#164);
  - the seventh round's fixes (#165) and the agent-first docs (#176);
  - range-`for` bindings in lambdas (#172), clang-only C++ (#171), optimized-build declarations (#175) and a dead adapter's cleanup (#170).
- **Cases:** new for this round, and absent from the corpus and the docs. Each was chosen to exercise one of the new features.
  - `calc` (Java): a recursive-descent calculator whose `parseExpr` reads its right operand with a recursive `parseExpr()`, so `+` and `-` group to the right. `10-4-3 2*3-4 2-3+4` gives `RESULTS:9,2,-5`, not `3,2,3`. Return values are the natural evidence.
  - `articles` (Python): `slug()` joins the pieces of `re.split(r"[^a-z0-9]+", …)`, so a title ending in punctuation gets a trailing `-` (`"Hello, World!"` is `hello-world-`), and titles that name the same article are counted apart. A string change is the natural experiment.
  - `router` (C++): `normalize()` keeps a trailing `/`, so `/users/42/` matches no route and gets `404`. Its loops are one-line, with several stops per pass, and the data is `std::string`.
  - `board` (Rust, a plain `cargo new` crate): `record()` raises a player's best score without moving `since` (when they reached it) to this entry, so a tie ranks by when a player first joined. The evidence is in `Vec` fields behind `&self`, and in the comparator's returns.
- **Task:**
  1. Confirm the cause with runtime evidence.
  2. Test the hypothesis with an intervention.
  3. Probe the fix as a candidate.
  4. Reach a general fix with `peeky verify`.
  5. Report friction.

## Outcomes

| Case | Runtime evidence | Intervention | Verified general fix | Peeky calls (wasted) | Probe / session step / verify |
| --- | --- | --- | --- | --- | --- |
| `calc` (Java) | `--capture this.pos,return` at `parseExpr`'s first line: the returns 3, 1 (`4-3`), 9, each to its own hit of the recursion (#162). `session step --out` and `session get return` read the same. | `--set-at 24@1 right=-3 --set-at 24@4 right=-4` (#151) negated the trailing operands: `RESULTS:3,2,3`. A `char` set (`op='+'`) was refused for the wrong reason first (M5). | A loop in `parseExpr`, as `parseTerm` has | 26 (2) | ~0.7 s (`--capture return` 2.7 s) / 0.11–0.14 s / ~0.45 s |
| `articles` (Python) | `--capture title,key --hits all` at line 22: hit 2's `key` is `'hello-world-'`; at line 14, `words` is `['hello', 'world', '']`. | `--set "words=['hello', 'world']" --set-hit 2`: `ARTICLES:Hello World (3); …`. | `re.findall(r"[^\W_]+", title.casefold())` | 25 (2) | ~1.1 s / ~0.11 s / 0.51 s |
| `router` (C++) | `--capture path,i --hits all` at line 57: hit 2's `path` is `"/users/42/"` and hit 4's `"/posts/7/edit/"`; `pattern`, `have` and `want` in `matches`. | `--set-at 57@2 'path="/users/42"' --set-at 57@4 'path="/posts/7/edit"'`, two `std::string` sets (#164): `HANDLERS:users.show,users.show,posts.show,posts.edit,home`. | Skip empty segments in `segments()` | 17 (0) | 1.3–1.6 s / 0.26 s (`get`) / 0.84 s |
| `board` (Rust) | `self.best [60, 60]` and `self.since [0, 1]` behind `&mut self` in `record` (#163): alice's `since` stayed 0 after she reached 60. In `before`, `a 1`, `b 0`. | `a`, `b` and `i` were `[not settable: register]`, so a candidate copied `self.since[b]` into a `black_box`ed local and `--set sb=2`: `RANKING:bob=60,alice=60,carol=40` (COOKBOOK 6.6). | `self.since[i] = self.entries;` where `best` rises | 24 (1) | 1.4–1.65 s / ~0.13 s / 1.07–1.14 s |

All four agents reached `passed: True` on their first `verify`, and each fix was general. All four ran `verify --baseline` first. Every new feature was used where its case called for it, and `queries diff` confirmed each candidate (`self.since [0, 1] -> [2, 1]` in `board`). No wrong or guessed value reached an agent.

## Findings and dispositions

"This PR" is the eval-fixes PR that adds this report.

| # | Finding | Agent(s) | Severity | Disposition |
| --- | --- | --- | --- | --- |
| M1 | A C++ `std::vector<std::string>` shows only `size=3` in probe rows: `have[2] = ""` needed a session, and an element can't be captured across `--hits all`. COOKBOOK 7.1 says a `std::` container keeps its summary. | C++ | Medium | #181. |
| M2 | Python: after `session step --out`, `get key` at a loop's line shows the previous pass's value with no note, although line 21 has not assigned it yet (C gives a loop-return note). | Python | Medium | #182. |
| M3 | A Rust `--set 'entry="zed:50"'` at a one-line `for` body was refused as "a statement under way … has already read it into a temporary (at its second line)", with a hint naming the line it was already on; the probe also ran before refusing. | Rust | Medium | #183. |
| M4 | `describe` doesn't say which parts are settable (Rust: "main: board", but only `board.entries` is), nor Java types (`mutable: numeric/boolean root locals` is ambiguous for `char`); the guide never points to COOKBOOK 6.6's `black_box` recipe when everything is register-held. | Rust, Java | Medium | #183. |
| M5 | A Java `char` set (`--set-at 24@1 "op='+'"`) first got "requires a numeric or boolean scalar literal"; only `op=43` revealed that `char` locals can't be set. | Java | Medium | #183. |
| M6 | `queries diff` pairs by hit even when an edit changes the call structure: both runs had 5 hits, so no note appeared, and rows like `op + -> -` looked as if the edit changed an operator. | Java | Medium | Done: where both runs stopped as often, `diff` (and `compare`) now note the first hit that is not the same moment in both: a hit in another function, or at another line counted from each run's first hit (a candidate's lines shift as a whole), or a selector whose values (two or more distinct) are the same but in another order (on `calc`: ``runs 1 and 2 saw the same values of `op` in another order (from hit 4: + and -)``), each naming `queries join`. No note does not prove the hits line up (COOKBOOK 3.1). |
| M7 | COOKBOOK (156–160 KB) is too big to read in one pass; agents want each language's rules in one place. ERRORS' `MUTATION_REJECTED` row is one long paragraph. | C++, Python, Java | Medium (docs) | Done: `docs/lang/python.md`, `java.md`, `c-cpp.md` and `rust.md` (about 4 KB each) give each language's targets, reading, changing, returns and candidate rules, one line each with a link to its COOKBOOK section (a test checks every link and anchor); AGENT_GUIDE and COOKBOOK link them at the top. ERRORS' row now points to "MUTATION_REJECTED in detail", the same rules as lists (what can change, where, texts, Java locals, refusals before launch, a C pointer's caller). |
| L1 | `session get` children hints and `session report` hints omit `--name X`, so pasting one acts on `default`. | Rust, Java | Low | Done: for a session other than `default`, every session command a state, hint or child listing names carries `--name X` (`peeky session wait --name r`). |
| L2 | `session step --out` doesn't print the returned value; it needs `get return`. | Java | Low | Done: after `step --out` (C, C++, Rust, Java), the state is followed by the return as `get return` reads it (`(return): long = 1`, or why it is withheld); JSON has `returned`. None where the step did not end at a return. |
| L3 | `session step` over a recursive call stopped at the session's breakpoint in the nested call (`hit 2`) with no note. | Java | Low | Done: a step the session's breakpoint stops adds `note: the session's breakpoint (hit 2) stopped the step before it ended, and the step is over` (a recursive call on the line, or the rest of the function under `step --out`); COOKBOOK 5.2. |
| L4 | The `--capture return` probe warned "The debugger never reported the program's exit … (java-debug, issue #27)", which isn't actionable, and `program finished after 3 hit(s)` came after the `evidence:` line. | Java | Low | Done (the warning): `The debugger never reported the program's exit (java-debug #27); with no threads left, it has ended, but its exit code is unknown. The captured hits stand: nothing to do.` Not done (the order): `program finished` is progress on stderr, written and flushed before the result; a tool that shows stdout before stderr puts it last, and `--quiet` leaves it out. |
| L5 | A failing `verify` prints the long `FAIL …` line twice (`reason:` and `output:`). | Java | Low | Done: when the checker printed only the line `reason` shows, the text says `output: the reason above, its only line`; JSON keeps `stdout`. |
| L6 | A Python candidate probe printed a 70-word "ran from a copy … not a project root" warning twice, once even with `--quiet`, for a one-file stdlib-only script. | Python | Low | Done: launch warnings are shown once, with the result (no longer also as progress). The copy note is shorter (about 55 words, from 75); text shows it once per root and state directory, never with `--quiet`, and `--json` always has it. A self-contained script gets none: it imports only standard modules that open no file, start no process and reach no network (of `sys` only `argv`, the streams, `exit`, `maxsize` and the recursion limit; no private names or submodules), and names nothing that could reach one (`open`, `exec`, `eval`, `getattr`, `vars`, frames' globals, `__file__` and other dunders but `__name__`/`__init__`); anything else keeps the note (COOKBOOK 6.6). |
| L7 | `queries set_effect` shows an empty `stop` column for Python runs. | Python | Low | Done: every query leaves out a `stop` column no row fills (Python, Java, and lines with one location). |
| L8 | COOKBOOK 3.1 says `(output)` shows each output "from just before the first character that differs", but it showed both from column 1 for a difference at column 23. | Python | Low (docs) | Done: 3.1 says what it does: up to 200 characters, from 60 before the first difference (from the start when it is within the first 60), `…` where cut. |
| L9 | Python has no `--capture return`; the docs should give the recipe. | Python | Low (docs) | Done: COOKBOOK 2.9 and `lang/python.md`: capture what the caller stores, on the caller's next line (`r = f(x)` on line 20: `--at 21 --capture r`). |
| L10 | Paths print as `/private/tmp/...` when `/tmp/...` was passed (macOS). | Python | Low | Documented, not changed: Peeky prints resolved paths, which name the files it checked; COOKBOOK 10 says `/tmp` is `/private/tmp` on macOS. |
| L11 | The docs don't say that `--label` doesn't affect `compare:` pairing. | Python | Low (docs) | Done: AGENT_GUIDE ("Querying") and COOKBOOK 3.1 and 6.6: a label never changes which runs `diff` or `compare:` pair. |
| L12 | `describe` shows raw libc++ names (`std::__1::vector<std::__1::basic_string<…>>`), while probe tables show `std::vector<std::string>`. | C++ | Low | Done: `describe`'s types name the standard library as code and probe tables do (`parts (std::vector<std::string>)`), by the same reading as frame names (`names.standard_type`); only the display changes. |
| L13 | `describe` lists compiler-generated locals (`__begin2`, `__end2`, `__range2`). | C++ | Low | Done: variables the debug information marks `DW_AT_artificial` (clang's `__range1`, `__begin1` and `__end1` for a range `for`) are left out; `this`, an artificial parameter, stays. |
| L14 | Every `std::string` set prints the `string_view`/`c_str()` warning, even where the function takes no view. | C++ | Low | Not done: the review of #164 (M2) made every `std::string` change say so, because a view may be held where no reading of this frame shows it (a caller of a reference parameter, a callee that stored `c_str()`); it is printed once per probe. |
| L15 | Reference values carry addresses (`0x… "/users/42"`), so `GROUP BY value` works only by luck; `num` is NULL for `size=3`. | C++ | Low | Documented: COOKBOOK 4.7 says to group by `no_addresses(value)`; 4.3 already says `num` is NULL where the value is not one number. No `text` column: `no_addresses` and 4.7's `substr` cover it. |
| L16 | Probe-table `after-set` rows repeat values that didn't change (`i`); `queries diff` already drops them. | C++ | Low | Done: the table leaves out an `after-set` row equal to its hit's `observed` value, of a selector no change named, with a footer (``after-set: 2 rows equal to the hit's observed value left out (`--json` has every row)``); COOKBOOK 2.3 and its examples. |
| L17 | `queries diff` doesn't show run labels. | C++ | Low | Done: `diff` and `compare` add `note: labels: run 1 is 'original'; run 2 is 'loop'` where either run has one. |
| L18 | `session list` is missing from AGENT_GUIDE's command table. | C++ | Low (docs) | Done. |
| L19 | The Rust type column shows `alloc::vec::Vec<unsigned int, alloc::alloc::Global>` (LLDB's names); elements show `u32`. | Rust | Low | #183. |
| L20 | `self.names` (`Vec<String>`) shows `… @ 0x…` with no note, although 7.2 says a raw field carries its note. | Rust | Low | #183. |
| L21 | After an intervention-plus-candidate probe whose `--set` targets a variable only the edit creates, `compare:` said "run it without --old/--new, then peeky queries diff", which can't work. | Rust | Low | Done: after a candidate with `--set` or `--set-at`, the line says `run it without --old/--new and without its --set/--set-at`. |
| L22 | The docs don't say candidate-mapped lines count as the same line in `diff` (no `(line)` row for 33 against 34). | Rust | Low (docs) | Done: COOKBOOK 3.1: `(line)` compares the probes' `--at` lines, and a candidate's keeps the original's numbering. |
| L23 | AGENT_GUIDE and COOKBOOK point to RUST_AGENT.md and NATIVE_AGENT.md, which an agent given only the three docs can't read. | Rust | Low (docs) | Done: the guide's pointer now names COOKBOOK 7.2, and COOKBOOK's four say "optional background". The language pages are the per-language reading; give them to agents alongside the three docs. |
| L24 | AGENT_GUIDE step 3 should point to `queries wide` for several selectors per hit (the agent wrote a 4-way self-join). | Rust | Low (docs) | Done. |
| L25 | `session start --timeout` isn't documented. | Java | Low (docs) | Done: AGENT_GUIDE ("Sessions") and COOKBOOK 8: a session command waits up to `--timeout S` (default 30, at most 300), leaving the program running; `session wait` waits again. |

## What worked

- **Every new feature was used where its case needed it, unprompted:**
  - `calc` read the recursion's returns per hit (#162), 3, 1 and 9, and changed two hits of one line with `--set-at` (#151).
  - `router` changed a `std::string` at two hits (#164), and read `"/users/42/"` in `main` and the segments in `matches`. It never probed the one-line loops, so #160's stop labels went unused.
  - `board` read `self.since` and `self.best`, `Vec` fields behind `&mut self` (#163), at each hit.
  - `articles` set a list of strings at the second hit.
- **The refusals led to the right experiments:** in `board`, every useful local was register-held, and the agent kept one in memory with COOKBOOK 6.6's `black_box` recipe and set it in the same candidate probe.
- **`verify --baseline` first, and first-try verifies:** all four agents checked that the checker fails on the original program, and all four fixes verified on the first `verify`.
- **Diffs confirmed each candidate** before `verify` (`self.since [0, 1] -> [2, 1]`; `RESULTS:9,2,-5` against `3,2,3`), with `compare:` naming the unedited run.
- **No wrong or guessed value reached an agent.** M2's value was the variable's real value at that stop (the previous pass's); what it lacked was a note that the line had not assigned it yet.
