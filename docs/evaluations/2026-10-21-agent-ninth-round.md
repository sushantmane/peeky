# Live agent evaluation, ninth round: a C line wrapper, a Python session counter, a Java invoice with German names and a Rust table (2026-10-21)

## Setup

- **Agents:** four independent general-purpose coding agents, run with no shared context.
  - Each got `docs/AGENT_GUIDE.md`, `docs/COOKBOOK.md`, `docs/ERRORS.md` and its language's page (`docs/lang/c-cpp.md`, `python.md`, `java.md` or `rust.md`, made for the eighth round's M7) as a fourth doc, the `peeky` CLI and one new bug: the program, an independent `check.py` and a short issue.
  - Reading Peeky's source was forbidden. Agents could change the program only through candidate probes and `peeky verify --old/--new`.
  - Each had its own state directory.
- **Tool version:** `main` at `29728f5`. Since the eighth round, it adds:
  - the eighth round's fixes and the per-language pages (#189);
  - a note on a Python local the loop's pass has not assigned yet (#185);
  - every refusal before launch with no table, exit 2 and nothing saved (#196);
  - returns followed across a line's stops in one call (#198);
  - C++ standard containers' elements in probe rows (#186);
  - selectors with Unicode names (#199), and Rust non-ASCII locals (#194);
  - Java and Rust refusals with their real reasons, Java `char` sets, and settable paths and types in `describe` (#192).
- **Cases:** new for this round, and absent from the corpus and the docs. Each was chosen to exercise new features.
  - `wrap` (C): a greedy word wrapper whose `fits()` leaves the indent out of the line's width, so each line can run `indent` columns past it. The width lives in `main`'s `struct layout`, passed down as a pointer: a change must be made in the frame that owns it.
  - `sessions` (Python): `sessionize()` measures a user's inactivity from the session's start, not its last event, so a session longer than 30 minutes splits however active (`alice=2/40` instead of `1/60`). Its loop's locals carry #185's stale-pass note.
  - `rechnung` (Java, German names): `gleicherArtikel` compares `Integer` article numbers with `==`, by identity, so 42 (cached) merges and 1001 does not, and the discount never applies. Its non-ASCII names (`stückzahl`, `zusammengeführt`) are selectable only with #199.
  - `roster` (Rust, a plain crate): `widest()` measures cells with `str::len()` (bytes), but `format!` pads by characters, so a 2-byte character makes its column one too wide (Name 6 instead of 5, City 7 instead of 6). Its loop line in `widest` (line 41) is one where #198 follows a call's return across the line's stops.
- **Task:**
  1. Confirm the cause with runtime evidence.
  2. Test the hypothesis with an intervention.
  3. Probe the fix as a candidate.
  4. Reach a general fix with `peeky verify`.
  5. Report friction.

## Outcomes

| Case | Runtime evidence | Intervention | Verified general fix | Peeky calls (wasted) | Typical probe |
| --- | --- | --- | --- | --- | --- |
| `wrap` (C) | `--capture column,word,lay.width,lay.indent,return --hits all` at `fits`' line 29: at hit 3, `column 15`, `word 3`, `lay.width 20`, `lay.indent 4` returned 1, though 4 + 15 + 1 + 3 > 20. | A set of `lay.width` at `fits`' line was refused before launch, naming the caller `wrap`, which only passes the pointer on (B); `--set-at 75@1 lay.width=16` in `main`, which owns the struct, printed the expected lines. | `return lay->indent + column + 1 + word <= lay->width;` | 22 (1) | 1.1 s |
| `sessions` (Python) | `--capture user,now,session,session.start,session.last,gap --hits all` at line 35: at hit 4, alice's `now 580`, `session.last 560` and `gap 40`, measured from `session.start 540`. | `--set-at 35@4 gap=20 --set-at 35@6 gap=20` (two hits of the `--at` line): `SESSIONS:alice=1/60,bob=2/0`. | `gap = now - session.last if session else None` | 25 (0) | 1.1 s |
| `rechnung` (Java) | `--capture p.bezeichnung,neu.bezeichnung,p.artikelNr,neu.artikelNr,gleich --hits all` at line 56: at hit 4, `Integer@60 {value=1001}` against `Integer@106 {value=1001}` and `gleich false`; at hit 8, `Integer@96 {value=42}` on both sides and `gleich true`. | `--set gleich=true --set-hit 4`: `RECHNUNG:Schraube*12=270;…;SUMME=850`. | `a.artikelNr.equals(b.artikelNr)` behind the null check | 18 (1) | 0.9 s |
| `roster` (Rust) | `title`, `cell` and `width` at `widest`'s loop line 41, and `table.widths` in `main`: widths `[6, 7, 3]` with accented data, `[5, 6, 3]` without. | `--set-at 43@1 width=5 --set-at 43@2 width=6` at `widest`'s last line (`width`), in its first two calls: the expected table. | `width.max(cell.chars().count())`, and the title's | 21 (~3) | 1.5–1.9 s |

All four agents reached `passed: True` on their first `verify`, and each fix was general. No wrong or guessed value reached an agent. Each candidate has an unedited run with the same line and selectors to compare with hit by hit (in `roster`, run 6, saved after the candidate).

## Findings and dispositions

The Mediums each got an issue. The Lows are #208, fixed or explained by the PR that adds this report.

| # | Finding | Case(s) | Severity | Disposition |
| --- | --- | --- | --- | --- |
| A | `queries diff` warned that hits do not line up where an intervention or a fix changed values (wrap's `lay.width`, roster's shifted `width`), and the `join` it suggests takes one key, so rechnung's gave a 16-row cross product. | wrap, rechnung, roster | Medium | #201 (done in #209: alignment is read from plain values, a value an intervention may have changed never proves it, and no claim is made over a contradiction; `join --param key=a,b`). |
| B | C caller advice names `wrap` at line 40, which only passes the pointer on, so it takes a second refused probe to learn that `main` owns the struct. | wrap | Medium | #202. |
| C | Python `--until hits` said "output so far (the program was still running)" for output written after the last hit. | sessions | Medium | #203. |
| D | Java: a String's internals (`hash`, `hashIsZero`) were marked `[settable]`. | rechnung | Medium | #204. |
| E | Rust: a `&&str` loop item and `&[&str]` show only pointers; a hint names `.len` for a field called `length`; `names.len` on a `Vec<&str>` gives a long "Box" message. | roster | Medium | #205. |
| F | Rust: "several locals named `entry`" fires when the other is out of scope, and `describe` merges shadowed locals. | roster | Medium | #206. |
| G | COOKBOOK (181 KB, very long lines) is too big to read; the language pages helped. | wrap, sessions, rechnung | Medium (docs) | #207, and #212 for plainer prose. |
| L1 | A withheld C `len` blamed "a `switch` or a `goto`" in a function with neither (its only jump a `continue` after the declaration). | wrap | Low | Done: where Peeky finds no `case` or `goto` label that can reach the line, the reason names none; it says Peeky cannot tell from the source whether the line can be reached without running the declaration. |
| L2 | `session get --frame 1 i`, a caller's `for` counter, was `no value yet` on the third pass. | wrap | Low | Done: a counter its header sets with no call (`for (int i = 0; …)`) is set before anything else of the loop runs, so a caller frame in the loop reads it; one whose header calls, and a C++ object a header declares, are still withheld while the loop runs a call. |
| L3 | A `char[8192]` showed `"    The quick brown fox"...`, whose `...` suggests a cut, and `<children: … exceeds bounded expansion limit>`. | wrap | Low | Done: LLDB adds `...` to any `char[N]` past its 1024-byte summary limit; fewer bytes than that prove the text ended at its NUL, and the `...` is dropped. A probe row of a long `char[N]` shown as its text no longer carries the children error; each character stays selectable (`text[5]`). |
| L4 | `session get` exited 1 without saying which selector failed. | wrap | Low | Done: a last line on stderr names them (`peeky: no value for nosuch (each row above says why), so the exit status is 1`). |
| L5 | AGENT_GUIDE rule 2 doesn't cover a pointer passed down several frames; rule 4 doesn't say `--set-at` works on the `--at` line at several hits. | wrap, sessions | Low (docs) | Rule 4 done. Rule 2 is #202's (PR #211 rewrites it). |
| L6 | `output: the reason above, its only line` (a baseline verify) is cryptic. | sessions | Low | Done: `output: the checker printed only the reason: line above`. |
| L7 | `--set-hit` beyond `--hits` gave `set_hit must be within 1..1`. | prep | Low | Done: `--set-hit 4 is past the last hit this probe captures (1, --hits 1): raise --hits to 4 or more, or use --hits all; or --set-at 34@4 NAME=VALUE changes it at that hit without capturing there`. |
| L8 | "settable here: …" is missing from Python's stale refusal, Java's `final` refusal and C's pointer refusals. | prep | Low | Not done: C's is #202's (PR #211), Java's #204's (PR #210); Python reports no settability yet, so its refusal has none to list. |
| L9 | `queries outputs` showed `set_hit NULL` for `--set-at` runs, which reads as no change. | sessions | Low | Done: `outputs` drops `set_hit`; its `sets` gives each change as `[name, value, "LINE@HIT"]`, a `--set`'s too. |
| L10 | `describe` doesn't list the module globals a function reads, though probes read them. | sessions | Low | Done: `names in sessionize (…): …, MAX_GAP (module global)`, for names the module itself assigns. |
| L11 | `session get` suggested `'history['"'"'alice'"'"'][0]'`. | sessions | Low | Done: `"history['alice'][0]"` where the selector holds nothing a shell reads in double quotes. |
| L12 | debugpy's "special variables" note on every dataclass `get` is noise. | sessions | Low | Done: shown only where nothing else is listed; JSON keeps `children_grouped`. |
| L13 | Java `after-set` rows equal to the observed ones were kept, while COOKBOOK 7.3's example left one out. | rechnung | Low (docs) | Done: the example now shows the kept row; COOKBOOK 2.3 already says a Java value other than a primitive stays. |
| L14 | Boxed numbers (`Integer@60 {value=1001}`) have `num` NULL. | rechnung | Low | Not done here: #204 (PR #210) shows boxed numbers as values. |
| L15 | `diff`'s `(sets)` row lacks the hit a `--set` applied at. | rechnung | Low | Done: each change as `[name, value, "LINE@HIT"]`, so runs whose `--set`s differ only in their hit tell apart. |
| L16 | Session states print absolute paths where probes print relative ones. | rechnung | Low | Done: text names a file below the working directory relative to it; JSON keeps the whole path. |
| L17 | A Java `final` field set is refused only after launch (exit 1). | prep | Low | Not done here: it is Java field checking, which PR #210 changes. |
| L18 | No tip for testing an identity hypothesis. | rechnung | Low (docs) | Done: COOKBOOK 4.6, "Identity, not value": read both sides' objects, and set the boolean the comparison produced. |
| L19 | Rust types are spelled LLDB's way (`roster::Table *`, `&str *`). | roster | Low | Not done here: it is #205's `&str` reading, and the type spellings with it. |

## What worked

- **Every agent verified on its first try.**
- **A refusal pointed toward the owner:** in `wrap`, the refused set of `lay.width` in `fits` named its caller, and a `--set-at 75@1` in `main`, where the struct lives, gave the expected output. That took one hop more than it should have (B).
- **Several changes in one run:** `sessions` set `gap` at hits 4 and 6 of its `--at` line, and `roster` set `width` in `widest`'s first two calls. Each change has its `after-set` row (`width 6` observed, `5` after the set).
- **Identity in Java:** `rechnung` read the objects `==` compares: at hit 4, `Integer@60` and `Integer@106`, both 1001.
- **No wrong or guessed value reached an agent.** None of the Mediums is a wrong value: they are false alarms (A, F), advice or a claim that misleads (B, C, E's hints), a `[settable]` mark on JDK internals (D), values Peeky cannot read yet (E) and the docs' size (G).
