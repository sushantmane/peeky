# Live agent evaluation, eleventh round: C++ grades, a Rust on-call rota, Java seat booking and C tide turns (2026-10-23)

## Setup

- **Agents:** four independent general-purpose coding agents, with no shared context.
  - Each got the same five docs as the tenth round: AGENT_GUIDE, COOKBOOK, ERRORS, its language's
    page and its language's cookbook.
  - Each also got the `peeky` CLI, one new bug, an independent `check.py` and a short issue.
  - Reading Peeky's source was forbidden. Agents changed the program only through candidate probes
    and `peeky verify --old/--new`.
- **Tool version:** `main` at `8b8c2f2`. Since the tenth round, it adds the tenth round's fixes,
  GCC for C++ (#167) and the C++ scanner fixes (#287, #295, #296, #298).
- **Cases:** new, and absent from the corpus and the docs.
  - `grades` (C++): `Stats::reset()` clears `sum` and `count` but not `lowest`. The first
    student's lowest quiz is dropped from everyone else's average.
  - `rota` (Rust): last week's engineer is kept as a clone, and the "not twice in a row" rule
    compares addresses with `ptr::eq`. The clone never matches, so a tie can repeat someone.
  - `seats` (Java): `block()` doesn't reset its count of free seats at a sold one. A group can be
    given a block with sold seats inside it.
  - `tides` (C): while the tide stands, `turn` moves to each equal reading. High and low water are
    reported at the stand's last reading, not its first.
- **Task:** confirm the cause with runtime evidence; test it with an intervention; probe a
  candidate fix; reach a general fix with `peeky verify`; report friction.

## Outcomes

| Case | First verify | Wrong value seen | Calls (wasted) | Typical probe | Wall time |
| --- | --- | --- | --- | --- | --- |
| `grades` (C++, clang) | passed | none | 24 (2) | 1.9–2.3 s | 3.7 min |
| `rota` (Rust) | passed | none | 22 (1) | 1.8–3.1 s | 4.3 min |
| `seats` (Java) | passed | none | 26 (0) | 0.8–1.4 s | 3.7 min |
| `tides` (C) | passed | none | 23 (1) | 1.0–1.15 s | 3.6 min |

Every agent ran `verify --baseline` first, saw the checker fail, then passed with a general fix on
its first try. No wrong or guessed value reached an agent.

| Case | Key evidence | Intervention | Fix |
| --- | --- | --- | --- |
| `grades` | `stats.lowest` read 58 at all four students. A session showed `reset()` return with `lowest = 58`. | `--set-at 61@all stats.lowest=101`: the expected output. | `reset()` also sets `lowest = 101`. |
| `rota` | `l` held one stack address at every hit; each `e` was in the heap. SQL on `address()` showed no match on 7 same-name rows. | `--set-hit 6 --set pick=2`: the expected rota. | Keep last week's index and compare indexes. |
| `seats` | At a sold seat, `free` kept counting (`s 4 free 3`, `s 5 free 3`), and `block` returned 2 for a span with sold seats. | `--set-at 26@all free=0`: the expected seating. | `free = 0;` at a sold seat. |
| `tides` | SQL turned `turn` into an index of `level` with `address()`: 6 and 17, not 4 and 16. | Six `--set-at`s that skipped `turn = p`, since pointers can't be set: the expected output. | Leave `turn` alone while the tide stands. |

## Compared with the tenth round

- **The same result:** every first verify passed, with no wrong value. 95 calls in all (86 last
  round), about 4 wasted (about 10).
- **What cost calls:** two agents re-ran `sql --schema`, whose 20 KB output their tools cut off.
  One guessed a query's parameters.
- **Docs:** one agent mentioned their length, as a Low. No agent misread a doc.
- **One false reason.** Peeky withheld correctly at `rota`'s `ptr::eq` line, but said why in a
  way that wasn't true (A below).

## Findings and dispositions

| # | Finding | Case | Severity | Disposition |
| --- | --- | --- | --- | --- |
| A | Rust: at `if ptr::eq(e, l)`, every local was refused as "already read into a temporary". The line reads none of `week`, `pick` or `log`. | rota | Medium | #300, fixed by #310. `std::ptr::eq` and `addr_eq` only compare addresses, so they no longer block a change. A note says "has already read" only when every place the stop can be has a real read; otherwise it says what may have. |
| B | `queries diff`'s alignment note was one long sentence; the verdict came last. | grades, seats, rota | Medium | #301, fixed by #308. A verdict line first, then one detail per line. It names the exact `queries join` command that pairs the hits. |
| C | A pointer into an array showed only an address, so `tides` needed SQL with a hard-coded `sizeof(int)`. `diff` hid the change as "addresses differ". | tides, grades | Medium | #302, fixed by #312. C and C++ rows say `[at level[6]'s address]`, or `[level + 6 by address; same array not proven]` from a pointer parameter. The store keeps the element size, and `diff` shows both runs' offsets. |
| D | Rust `Option<Engineer>` read `<unqualified Rust enum layout>`. | rota | Medium | #303, fixed by #309. An `Option` of a crate's struct or tuple reads when the debug information proves its layout. Other enums say why not, and what to read instead. |
| E | `sql --schema` printed 20 KB; query parameters were hard to find. | tides, rota, grades | Medium | #304, fixed by #307. `--schema` prints a short index, `--schema NAME` one part, and `queries --help` each query's parameters. |
| F | Testing "skip this statement" took six hand-made `--set-at`s. | tides | Enhancement | #305, open. |

### The Lows (#306)

Fixed by #314 (CLI and sessions) and #315 (languages and docs). The main ones:

- A write that leaves a value as it was says so (`0 -> 0, no change`), where the debug information
  proves it. Same text that isn't proven unchanged says that instead.
- `NO_STOP` after changes names them, and no longer blames the line.
- A `void` function's step out says `(return): void`.
- Session paths match `session start`'s; `session report` shows where the program is.
- Rust types read as Rust writes them: `&rota::Engineer`, `*const [u32; 3]`.
- A `for` loop's hidden `iter` is no longer listed.
- A GCC lambda's capture says why it can't be read.
- Java `verify` with one source needs no `--at`.

## What the reviews found beyond the evaluation

Each fix had rounds of independent review until nothing blocking was left. They found:

- **A wrong `[settable]` on main (#311, High).** After `let t = total(vec![x, 1])`, `x` was
  `[settable]` on the next line, but the change never reached `t`. `vec!` copies through a raw
  pointer, which the analysis skipped. Its fix, #316, is in review; it also found a second false
  fact after a partial write (`b[0] = 7;`).
- **#310:** a first rule ("no unsafe code, so no raw-pointer reads") was unsound: standard
  macros such as `vec!` expand to unsafe code. Only proven `ptr::eq` and `addr_eq` are exempt now.
  Four more rounds made "has already read" a strict must-analysis.
- **#312:** an address matching `level[6]` doesn't prove the pointer belongs to `level` (unions,
  neighbouring arrays). Rows now state the address fact only, and an unproven offset change ranks
  with address differences.
- **#308:** "hits line up by `i`" was too strong: a counter agrees even when two calls swap.
  It now says "hits pair by `i`", and a named join key never exceeds `join`'s limit of 8.
- **#307, #314, #315:** help lines and messages that claimed a little more than the data shows.
  One example: a no-op detector that trusted a type's name (`typedef float int64_t`).
- **#313 (Low, open):** canned queries fail on SQLite older than 3.35, which Python 3.11 may link.
