# Live agent evaluation, tenth round: a C button debouncer, a Rust trip kitty, a Java runoff count and a Python outliner (2026-10-22)

## Setup

- **Agents:** four independent general-purpose coding agents, run with no shared context.
  - Each got five docs: `docs/AGENT_GUIDE.md`, `docs/COOKBOOK.md`, `docs/ERRORS.md`, its language's
    page and its language's cookbook (`docs/lang/c-cpp-cookbook.md`, `rust-cookbook.md`,
    `java-cookbook.md` or `python-cookbook.md`).
  - The cookbook is the one change to the ninth round's prompt. #212 moved each language's rules
    out of COOKBOOK into those cookbooks, and AGENT_GUIDE sends agents to them.
  - Each also got the `peeky` CLI and one new bug: the program, an independent `check.py` and a
    short issue.
  - Reading Peeky's source was forbidden. Agents could change the program only through candidate
    probes and `peeky verify --old/--new`.
  - Each had its own state directory.
- **Tool version:** `main` at `db418fe`. Since the ninth round, it adds:
  - the ninth round's fixes (#201 to #207, and #208's Lows);
  - every doc and message in plain English (#212): the per-language cookbooks, short sentences,
    and refusals that say what to do first, then why;
  - raw pointers inside a Rust value shown only as their address (#256);
  - a remote worker that compares every source with the client's, not only the program (#254).
- **Cases:** new for this round, and absent from the corpus and the docs. Each was chosen to
  exercise new features.
  - `debounce` (C): `settle()` returns early when the input agrees with the accepted level, without
    resetting `count`. So the samples of separate blips add up, and a bounce is accepted early.
    The counter lives in `main`'s `struct debounce`, passed down two calls as a pointer.
  - `kitty` (Rust, non-ASCII names): `parse` saves each payer as a raw pointer into a `Vec` of
    friends. `main` sorts the `Vec` by name before `settle` compares those pointers, so money goes
    to whoever now sits in the old slot.
  - `runoff` (Java): when a candidate is knocked out, each of their ballots moves on one rank,
    even onto a candidate already out. Those votes are lost, and the wrong candidate wins.
  - `outline` (Python, non-ASCII text): undo snapshots copy the list of items but not the item
    dicts. A rename or an indent changes the snapshot too, so undo can't undo it.
- **Task:**
  1. Confirm the cause with runtime evidence.
  2. Test the hypothesis with an intervention.
  3. Probe the fix as a candidate.
  4. Reach a general fix with `peeky verify`.
  5. Report friction.

## Outcomes

| Case | First verify | Wrong value seen | Peeky calls (wasted) | Typical times | Wall time |
| --- | --- | --- | --- | --- | --- |
| `debounce` (C) | passed | none | 24 (2) | probe 1.5–1.9 s, session start 0.9 s, session command 0.13 s, verify 0.6–0.74 s | 3.6 min |
| `kitty` (Rust) | passed | none | 22 (about 3) | probe and session start 1.9–2.1 s, session command 0.14–0.2 s, verify 1.2–1.4 s | 4.6 min |
| `runoff` (Java) | passed | none | 19 (1 or 2) | probe 0.8–0.9 s, session start 0.7 s, session command 0.11–0.16 s, verify 0.49 s unedited and 2.4 s with the fix | 3.6 min |
| `outline` (Python) | passed | none | 21 (3) | probe 1.1 s, session start 0.45 s, session command 0.11–0.13 s, verify 0.51 s with the fix | 3.8 min |

All four agents reached `passed: True` on their first `verify`, after a `verify --baseline` that
the checker failed. Each fix was general. No wrong or guessed value reached an agent.

How each agent got there:

| Case | Runtime evidence | Intervention | Verified fix |
| --- | --- | --- | --- |
| `debounce` | At `settle`'s early return (line 36), 14 of 20 hits kept a `count` above 0. At hit 3, `level 0`, `stable 0` and `count 1`. | Six `--set-at 80@N db.count=0` changes in `main`, which owns the struct, as the refusal advised. Output `DEBOUNCE:2:P@9,R@20`, as expected. | `db->count = 0;` before that return. |
| `kitty` | Payer pointers from `parse`, and the names at those addresses after the sort, joined across three runs by their addresses' last digits. | `--set-at 72@1 payer=3 --set-at 72@3 payer=0` in `settle`. Output exactly the expected balances. | Store the payer's name, and look it up by name. |
| `runoff` | At hit 3, `tally [4, 0, 2, 3]` with `out [false, true, true, false]`: Kofi is out but holds 2 votes. A session step showed a ballot move onto Kofi. | `--set 'choice[1]=2' --set 'choice[3]=2' --set-hit 3`. Output ends `WINNER=Jonas`, as expected. | Skip candidates already out: `do { choice[b]++; } while (…out[…]);`. |
| `outline` | `self.items[2]` and `self.history[-1][2]` are both `[id 19]`, while the two lists differ. A session showed the snapshot change across the rename. | `--set-at 66@1` and `66@2` each set `previous` to the right snapshot. Output exactly the expected outline. | Copy each item: `[dict(item) for item in self.items]`. |

## Compared with the ninth round

- **The same result.** Every first verify passed in both rounds, with no wrong value. The four
  agents made 19 to 24 calls each, 86 in all, as in the ninth round (18 to 25, 86 in all).
- **More calls wasted:** about 10 in all, against about 5. Most were dead ends a message could have
  prevented: a `get` after a step that had left the function (#262), an index named by a variable,
  and `queries diff` output too wide to read (#263).
- **Typical probes** took about as long. C took 1.5–1.9 s, against 1.1 s for the ninth round's C
  case. Rust took 1.9–2.1 s (1.5–1.9 s), Java 0.8–0.9 s (0.9 s) and Python 1.1 s (1.1 s). The
  ninth round's report gave no wall times.
- **Much less docs friction.** In the ninth round, three of four agents could not read the 181 KB
  COOKBOOK, a Medium. This round, only one agent mentioned the docs' length, as a Low. No agent
  misread a doc. Two named missing facts: the array preview's limit, and typical Java times.
  - The `debounce` agent found the C cookbook's 130 lines on a field behind a pointer too long. It
    called the refusal clearer: it names the owner's line and the `--set-at` to use.
  - The `kitty` agent did not find `--set-at LINE@HIT` in the Rust cookbook's section on changes.
- **The new features worked.**
  - The C refusal followed `db` through two calls to `main`, and its `--set-at` reproduced the
    expected output.
  - Rust raw pointers showed only their addresses, with a note.
  - Non-ASCII names and text (`Åsa`, `Chloé`, `Brötchen`) read and set without trouble.

## Findings and dispositions

The Mediums each got an issue and a fix. The two enhancements are filed. The Lows are #267, fixed
or explained by the PR that adds this report.

| # | Finding | Case(s) | Severity | Disposition |
| --- | --- | --- | --- | --- |
| A | Rust: at `if ptr::eq(f, e.payer)`, `f.name` was withheld, with a note that "a call or drop may have come since `f` was last used". Neither had, and the note had an issue number in it. | kitty | Medium | #259, fixed by #272: comparing addresses with `std::ptr::eq` or `addr_eq` no longer ends the proof, so `f.name` reads there. Where the proof fails, the note names what may have run, and where. No printed note has an issue number. |
| B | Rust: `friends[0].name` on a live `&mut [Friend]` said to "read it as a &Vec or &[T] parameter", which it was. A closure's `&str` showed only its address, with no note. | kitty | Medium | #260, fixed by #273: the elements of a slice or `Vec` of structs with a `String` field read from memory. Each value still withheld has a true, specific note. |
| C | Java: `ranking[1]` was marked `[settable]`, then refused, with a hint that said `[settable]` rows can be changed. `counted` and `left` had no mark and no reason. | runoff | Medium | #261, fixed by #270: a mark promises only what a write does. A row says why it isn't settable, or that it may be (`[maybe settable: …]`). The refusal names `ranking[1]`, and its hint no longer contradicts it. |
| D | A `session step` that left a function or a block said nothing, so the next `get` failed. A Python `session start` at a blank line stopped at the line before it with no warning. | outline, runoff | Medium | #262, fixed by #274: the state line says when a step's stop is in another function or call depth, or past a block. `session start` warns when the debugger moved its breakpoint. |
| E | `queries diff` cut long values before the part that differed. The `(sets)` row made lines about 230 columns wide, and interventions came last. Its alignment note fired on values the change itself moved. `join` repeated unchanged selectors. | outline, debounce, runoff, kitty | Medium | #263, fixed by #269: long values show from just before where they differ. `(sets)` is short, and interventions come first. Values a change may have moved are left out of the alignment check. `join` drops selectors that agree. |
| F | Java `describe` listed a name that two loops declare once, with no lines. | runoff | Medium | #264, fixed by #271: each declaration's lines are listed, as for C, C++ and Rust. |
| G | No conditional or every-hit intervention: `debounce` needed six hand-computed `--set-at` hits, near the cap of 8. | debounce | Enhancement | #265, open. |
| H | Heap addresses differ from run to run, so `kitty` joined runs on an address's last three digits. | kitty | Enhancement | #266, open. |

### The Lows (#267)

| # | Finding | Case | Disposition |
| --- | --- | --- | --- |
| L1 | A failed `session get`'s line on stderr, `no value for … (each row above says why)`, came before the rows it calls "above", where both streams share a pipe. | runoff, outline | Done: stdout is flushed first. Any error's stderr lines come after what the command printed. |
| L2 | Array previews stop at 8 elements, but the docs said only "its first elements". | runoff | Done: java.md and the Java cookbook say 8, and to read the rest by index (`choice[8]`). The C and C++ cookbook already said 8. |
| L3 | The Java docs gave no typical times. | runoff | Done: java.md gives this round's times for `describe`, a probe, `session start` and `verify`. |
| L4 | `probe --hits all` with 5 selectors printed 160 lines. `wide` isn't a SQL view, so a filter across selectors took a triple self-join. | debounce | Done: a table of more than 40 rows, of several selectors, ends with `` evidence: one line per hit, a column per selector: `peeky queries wide --param run=7` ``. `queries wide --json` already gives its SQL to build on, so no view was added: its columns differ from run to run. |
| L5 | C types: `describe` says `struct debounce *`, and `session inspect` says `debounce *`. Rust: `f` is `kitty::Friend *` in `settle`, and `&kitty::Friend` in a closure. | debounce, kitty | Documented. Rows show LLDB's type names. `describe` writes a C tag as C code does, and a Rust reference parameter's type comes from the build's IR. Changing every row's type is more than a display change: other checks read it. |
| L6 | `--capture 'self.items[at]'` said only that selectors are paths. | outline | Done: the error adds that an index is a literal: capture `` `at` `` first, then use its value, as `self.items[2]`. |
| L7 | `session get 'self.history[-1][2]' 'self.items[2]'` listed the dict's children only once. | outline | Done. It was not the shared object: a value 4 levels below a local is past the inspection's depth limit. Its row now says so, and COOKBOOK 5.1 explains it. |
| L8 | Rust `<not found; visible: …>` rows restated the frame's ten names at every stop. | kitty | Done: a later row with the same list says `visible: as listed at hit 1`, or `as listed above` in `session get`. JSON keeps every list. |
| L9 | `payer @ main.rs:47`, a `&str` into `args`, was withheld as "may point at freed memory". | kitty | Done by #272: the note now names the call that may have run since its last use, and says to probe a line that still uses it. |
| L10 | `--set-at LINE@HIT` was hard to find. | kitty | Done: the Rust cookbook's "What `--set` can change" and "Change a value behind a reference" show it, with `kitty`'s own example. AGENT_GUIDE, at its size cap, already names it in rule 4. |
| L11 | Python's field refusal hint mentioned `[settable]` rows, which Python never shows. It printed `peeky: [MUTATION_REJECTED]` on stderr, where other languages print `error [MUTATION_REJECTED]:`. | prep | Done: the hint says to set a local, or add one with a candidate edit. The probe's refusal prints as the other languages' do. |
| L12 | C pointer refusals don't end with `settable here:`, as AGENT_GUIDE promised. | prep | AGENT_GUIDE narrowed: Rust's refusals before the run end with it. Java's don't either. A C refusal through a pointer names the frame where the field is settable instead. |
| L13 | Rust `describe` listed the `?` operator's `residual` and `val` bindings. | prep | Done: the DWARF doesn't mark them as the compiler's, but the MIR places each at the `?`. A binding you write sits at its name, so your own `val` stays listed. |
| L14 | Rust `--set e.payer=0` advised changing it "before the call that passed it". `e` is a `for` item, which no call passed. | prep | Done: a reference or pointer local that isn't a parameter, in Rust, C or C++, says so, and to change the value where a frame holds it, or edit the source. |
| L15 | `--json`'s `output` has `\r\n`. | prep | Documented: a C, C++ or Rust program writes through a terminal that LLDB opens, which adds the `\r`. The evidence store keeps it too. Peeky can't tell that `\r` from one the program wrote. |
| L16 | The C cookbook's "Change a field behind a pointer" ran about 130 lines. | debounce | Done: it starts with the refusal and what to do, and keeps every condition, in 109 lines. |

## What the reviews found beyond the evaluation

Each fix PR had review rounds. They found these problems, none of which an agent met:

- **#269 (queries):**
  - A selector that a change may have moved could still show a reorder. That sign now blocks any
    claim that the hits line up.
  - A probe saved while a query ran made its notes describe other runs. A query now names its runs
    once.
  - A run evicted mid-query silenced a note. Each query now reads one snapshot of the store.
- **#270 (Java `[settable]`):**
  - A verdict made while other threads ran was kept. A thread wrapped the list later, and the
    write was refused. No promise is made now while other threads run, and every thread is
    suspended across the check and the write.
  - A JVM that could not be resumed after a change was left suspended. Now the adapter
    disconnects, and the session closes and says why.
- **#271 (Java block lines):**
  - A line was listed where a breakpoint can't see the local. Now a line counts only where its
    first instruction is in scope.
  - A declaration that runs to the method's end hid the others of its name. Each is listed now.
- **#272 (`ptr::eq`):**
  - A crate's own `mod std`, declared by a macro, had a `ptr::eq` that frees its argument.
    `f.name` read freed memory with no note. Three independent checks now refuse that case.
  - Two notes were not true for every reason: a reference parameter's field note, and a
    capture's.
- **#273 (slices of structs):**
  - In a caller's frame, a `&mut Vec` that `retain_mut` was changing showed its length with no
    note. That was on main too.
  - Such a container, owned or borrowed, now marks its fields `[stale]`, with a reason that is true
    whether the call took it or not.
  - A general note on a raw `&str` broke a test in the lane without `readMemory`.
- **#274 (session steps):**
  - Comparing stacks is not comparing calls. A recursive `f(0)` was said to be "entered from `f`"
    at a later call.
  - "Returned from" was false for a generator at `yield` and a C++ throw.
  - The moved-breakpoint warning came without a stop.
  - A stack of 20,000 frames passed the debug protocol's 4 MiB limit and closed the session. At
    most 256 frames are read now.

## What worked

- **Every agent verified on its first try.**
- **A refusal led to the owner in one step:** in `debounce`, the refused set of `db.count` named
  `main`'s line 80 and the `--set-at` to use. In the ninth round, it took a second refusal.
- **Several changes in one run:** six `--set-at` changes in `debounce`, two in `kitty` and
  `outline`, two at one hit in `runoff`.
- **Identity in Python:** `outline` used `[id 19]` on both paths to prove the snapshot shares its
  dicts.
- **Raw pointers:** `kitty`'s addresses showed only as addresses, with a note, and its agent still
  reached the cause by matching them.
- **No wrong or guessed value reached an agent.** None of the Mediums is a wrong value: they are
  false or missing notes (A, B, D), a mark the write didn't keep (C), output too hard to read (E)
  and missing lines (F).
