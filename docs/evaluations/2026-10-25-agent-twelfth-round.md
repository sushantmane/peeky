# Live agent evaluation, twelfth round: Python rainfall, Rust lap times, a C++ playlist and Java check-in desks (2026-10-25)

## Setup

- **Agents:** four independent coding agents, with no shared context. Each got AGENT_GUIDE,
  COOKBOOK, ERRORS, its language's page and cookbook, the `peeky` CLI, one new bug, a `check.py`
  and a short issue. Reading Peeky's source was forbidden.
- **Tool version:** `main` at `382e7d7`: the eleventh round's fixes, plus #316, #318, #319, #322
  (`@before`), #323, #325, #327, #328, #333, #334, #335 and #338.
- **Cases:** new, and absent from the docs. One was renamed before the run, because the docs
  quoted its variable name.
  - `rainfall` (Python): a day without a reading counts as a dry day of the spell under way.
  - `laps` (Rust): a second transponder read moves the lap's start.
  - `playlist` (C++): "previous" from the first track wraps to one past the last.
  - `desks` (Java): the earliest-free desk wins, not the lowest-numbered free one.
- **Task:** confirm the cause with runtime evidence, test it with an intervention, probe a fix,
  pass `peeky verify`, and report friction.

## Outcomes

| Case | First verify | Wrong value seen | Calls (wasted) | Typical probe |
| --- | --- | --- | --- | --- |
| `rainfall` (Python) | passed | none | 28 (0) | 1.1 s |
| `laps` (Rust) | passed | none | 24 (0) | 1.5–2.2 s |
| `playlist` (C++) | passed | none | 19 (0) | 1.4–1.7 s |
| `desks` (Java) | passed | none | 23 (3) | 0.75–0.81 s |

The same result as the eleventh round: every first verify passed, with no wrong value. 94 calls in
all, 3 wasted (about 4 last round).

- `rainfall` tested "skip this statement" with one `@before` change. The eleventh round needed six.
- `laps` was told its change is refused at line 62 and allowed at line 61, as #334 intends.
- `playlist` read `[at queue[3]'s address]` and the one-past-the-end note (#338).
- `desks` checked every hit against the rule in SQL. Its 3 wasted calls: two on a SQL parse of an
  array value, and a `session continue --to` that stopped at the session's own breakpoint.

## Findings and dispositions

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| A | `queries diff`: a reorder note beside hits already paired by a key; the first difference not listed first; `(output)` cut in the wrong place; `(sets)` hiding the second of two changes. | Medium | #343, fixed by #349. |
| B | C/C++: "same array not proven" beside a proof that both pointers are one past `queue`; a stale element read through a pointer equal to `end`, with nothing tying the two. | Medium | #344, fixed by #354. |
| C | SQL: no way to read a stored array's elements or a string's text. `json_each` failed on `int[3]@55 [490, 484, 497]`. | Medium | #345, fixed by #347: `elements(value, language)` and `text(value, language, type)`. |
| D | 18 Lows, from refusal hints to schema columns per language. | Low | #346, fixed by #350. |

## What the reviews found beyond the evaluation

- **#329 (#340):** a copy that a call keeps in a `static`, a `thread_local!` or a spawned thread
  could leave a Rust local `[settable]`, though the change wouldn't reach the copy. It is refused
  now.
- **#339 (#341):** C/C++ values read through a pointer that may dangle showed with no note. They
  are marked `[unproven]` unless Peeky proves the pointee is live. On Linux this includes string
  literals, which the linker merges into plain read-only data.
- **#342 (#348):** an aborting C++ program was reported as the debugger's error. It is
  `PROGRAM_CRASHED` now, and only with proof: a handled or ignored signal isn't a crash.
- **#347's review:** a Python object's own `__repr__` can look like `[1]` or `'Göteborg'`, and even
  its type name can be faked. So both SQL helpers return NULL for Python values. A `long double`
  that a 64-bit float can't hold exactly also gives NULL, not a rounded number.
- **A clean-machine install (#351, #352):** the wheel installed and worked on fresh Linux and
  macOS. But no command printed a version, and the README assumed a repository checkout.
