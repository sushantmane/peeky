# Live agent evaluation, thirteenth round: GCC stock depot, a lambda's stale tariff, a static's leftovers and a night car park (2026-10-29)

## Setup

- **Agents:** four independent coding agents, with no shared context. Each got AGENT_GUIDE,
  COOKBOOK, ERRORS, the C and C++ page and cookbook, the `peeky` CLI, one new bug, a `check.py`
  and a short issue. Reading Peeky's source was forbidden.
- **Tool version:** `main` at `bbed6e4a`. Since the twelfth round it adds GCC reads of unordered
  containers (#371), lambda captures (#369) and static locals (#370), a caller's multi-line
  aggregate (#366), short-string sets (#365), reference bindings over local containers (#368) and
  stop-read timeouts (#367).
- **Cases:** new, each built so that one of those reads is the decisive evidence.
  - `depot` (C++, g++ 13 on Linux): `stock[part] >= units` adds a part never delivered, which
    lowers the average.
  - `bill` (C++, g++ 13 on Linux): the pricing lambda keeps a copy of the tariff from before it
    changed.
  - `ward` (C, Apple clang): a `static` summary carries one patient's totals into the next.
  - `nightlot` (C++, Apple clang): a `std::map` walks hours after midnight before the evening's.
- **GCC on a Mac:** the two GCC cases ran Peeky in a Linux container through a wrapper that takes
  the same arguments as `peeky`.

## Outcomes

| Case | First verify | Wrong value seen | Calls (wasted) | Typical probe |
| --- | --- | --- | --- | --- |
| `depot` (C++, g++) | passed | none | 18 (1) | 0.9–1.2 s |
| `bill` (C++, g++) | passed | none | 23 (2) | 1.1 s |
| `ward` (C, clang) | passed | none | 21 (2) | 1.0 s |
| `nightlot` (C++, clang) | passed | none | 28 (5) | 1.7–2.3 s |

Every first verify passed, with no wrong value. In each case the new read showed the decisive
value: the map's added `"HX-88": 0`, the lambda's `__tariff` at 30 while `main`'s was 45, the
static's leftover totals, and the map's hour order.

## Findings and dispositions

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| A | After a `session step`, a frame outside the sources got a false "your code is no longer on the stack" refusal (found preparing). | Medium | #379, fixed by #387: the step's own frame decides the message. |
| B | An unordered map withheld behind a reference or below a destructor named LLDB's miscount, not Peeky's reason or where it reads. | Medium | #380, fixed by #393: names the candidate local (hedged) or the call, and says to retry. |
| C | Captures: a by-value capture can be set through the closure's member from its holder's frame, but the refusal and the docs said no capture can be changed. | Medium | #380 and #385, fixed by #393: the refusal names the closure's copy; the cookbook says what can change. |
| D | A C `char[32]` showed bytes past its NUL, and `text()` gave NULL for it. | Medium | #381, fixed by #390: text up to the NUL, with the rest counted and read only on a proven path. |
| E | A probe can't capture a caller frame's locals. | Medium | #382, fixed by #388: `--capture 1:i` reads frame 1. |
| F | The 8-change limit isn't in the guide, and its error doesn't say how many were given. | Medium | #383, fixed by #389. |
| G | `queries diff` counted rows withheld on one side as differences. | Medium | #384, fixed by #389: a value one run lacks isn't a difference, unless its type changed. |
| H | 12 Lows: repeated notes, hints that don't fit, lldb-dap 18 noise, preview limits, docs size. | Low | #385, fixed by #389 and #393. |

## What the reviews found beyond the evaluation

- **#390's review:** LLDB's text for a char array can come from a partial read. Peeky first padded
  the rest with NULs. It now reads the rest itself, only on a proven path, and otherwise says how
  many bytes it didn't read.
- **#389's review:** a type change hidden behind a missing value, and a once-per-build note keyed
  on file names only. Both fixed: a new `build_id` keys the note.
- **#393's review:** a hedge ("may be it") that could be cut off, and "read it after that call
  returns" promised a read that may still be withheld. It now says "retry".
- **A macOS-only test** expected a binding to be withheld that now reads its true value (#391).
  The Linux lanes never took that path; the macOS CI job in #392 would.
