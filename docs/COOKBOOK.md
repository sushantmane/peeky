# Peeky Cookbook

Recipes for debugging running programs from your terminal. Each one is copy-paste ready from the repository root and shows real output (macOS, Python 3.13, recorded 2026-10-16). Agents use the same commands; see [AGENT_GUIDE.md](AGENT_GUIDE.md). Each language's rules, with pointers into this book: [Python](lang/python.md), [Java](lang/java.md), [C and C++](lang/c-cpp.md), [Rust](lang/rust.md). What is particular to one language is in its cookbook, under the number of the section here it adds to: [Python](lang/python-cookbook.md), [Java](lang/java-cookbook.md), [C and C++](lang/c-cpp-cookbook.md), [Rust](lang/rust-cookbook.md).

**The loop:** ask a question, keep the answer, check the fix.

```sh
peeky describe <target>                        # what can I ask? lines, names, rules
peeky probe <target> --at <file>:<line> --capture a,b.c [--set x=0]   # one question, one run
peeky queries diff        |  peeky sql "SELECT ..."                    # follow-ups on saved runs
peeky session start <target> --at <file>:<line>                        # several looks at one stop
peeky verify <target> --check <checker.py> --old '<text>' --new '<text>'   # proof, on a fresh build
```

> **Tip:** a breakpoint stops **before** its line runs. To see a value a line assigns, stop on the next line.

> **Tip:** an intervention (`--set`) is an experiment on one run. It tells you whether a hypothesis explains the output. Only `peeky verify` says a source fix works.

---

## Table of Contents

0. [Setup](#0-setup): install Peeky and the debugger or toolchain each language needs
1. [First Look](#1-first-look): what a target offers, and a first probe
   - [1.1 What Can I Ask?](#11-what-can-i-ask): `describe`: breakpoint lines, each function's names and types, what `--set` can change; lines and stepping
   - [1.2 First Probe](#12-first-probe): values at one line, and the program's output
2. [Asking Questions with Probes](#2-asking-questions-with-probes): one run per question: values, changes, returns
   - [2.1 Fields and Indexes](#21-fields-and-indexes): selectors: fields, indexes, keys; names in any script
   - [2.2 Every Iteration of a Loop](#22-every-iteration-of-a-loop): `--hits N`/`all`; lines that stop several times per pass; loop headers
   - [2.3 Test a Hypothesis by Changing a Value](#23-test-a-hypothesis-by-changing-a-value): `--set`: what can be changed, where, and to which values
   - [2.4 Change the Value at a Later Hit](#24-change-the-value-at-a-later-hit): `--set-hit N`: change a later hit
   - [2.5 A Module Imported by a Script, with Arguments](#25-a-module-imported-by-a-script-with-arguments): `--run` for a module a script imports, and program arguments
   - [2.6 Stop Right After the Last Hit](#26-stop-right-after-the-last-hit): `--until hits`: stop once the values are in
   - [2.7 JSON for Scripts](#27-json-for-scripts): `--json` and `jq`
   - [2.8 Change a Value, Then Restore It](#28-change-a-value-then-restore-it): `--set-at LINE@HIT`: change at one line, restore at another
   - [2.9 What a Function Returned](#29-what-a-function-returned): `--capture return`: what each call returned
3. [Comparing Runs](#3-comparing-runs): what differs between saved runs
   - [3.1 Two Inputs Side by Side](#31-two-inputs-side-by-side): `queries diff`: what else differed, then the first divergence
   - [3.2 Inputs, Outcomes and Outputs of the Last Two Runs](#32-inputs-outcomes-and-outputs-of-the-last-two-runs): `queries outputs`: arguments, changes, status and output per run
   - [3.3 What My Intervention Changed](#33-what-my-intervention-changed): `queries set_effect`: values before and after a change
   - [3.4 One Row per Hit](#34-one-row-per-hit): `queries wide`: a row per hit, a column per selector
   - [3.5 Joining Two Runs by a Value](#35-joining-two-runs-by-a-value): `queries join`: pair two runs' hits by a value
4. [SQL over Saved Runs](#4-sql-over-saved-runs): your own SQL over every probe ever run
   - [4.1 What Is in the Store?](#41-what-is-in-the-store): tables, views and SQL functions (`no_addresses`, `object_ref`)
   - [4.2 The Newest Run](#42-the-newest-run): `last_run`
   - [4.3 Numeric Filters](#43-numeric-filters): `num`: numbers, not text
   - [4.4 Values Seen Across All Runs](#44-values-seen-across-all-runs): `queries values`, and counts across runs
   - [4.5 Parameters and Export](#45-parameters-and-export): `--param`, JSON and CSV export
   - [4.6 Same Object, or a Copy? (Java, Python)](#46-same-object-or-a-copy-java-python): Java object numbers and Python identities
   - [4.7 C++ References and Pointers in SQL](#47-c-references-and-pointers-in-sql): addresses in C++ references and pointers
5. [Step-by-Step Sessions](#5-step-by-step-sessions): several looks at one stop
   - [5.1 Stop, Look, Change, Finish](#51-stop-look-change-finish): `start`, `inspect`, `get`, `set`, `close`; caller frames
   - [5.2 Stepping and Running to a Line](#52-stepping-and-running-to-a-line): `step`, `continue --to`, `--count`, `--hit`
   - [5.3 Several Sessions at Once](#53-several-sessions-at-once): `--name`
   - [5.4 Programs with Timeouts](#54-programs-with-timeouts): `--max-pause` for programs with clocks
   - [5.5 Cleaning Up](#55-cleaning-up): when a session's worker ends
6. [Verifying a Fix](#6-verifying-a-fix): the only proof: the checker on a fresh build
   - [6.1 Check One Edit on a Fresh Build](#61-check-one-edit-on-a-fresh-build): `verify`, `--baseline` and `reason`
   - [6.2 An Overfit Fix Is Rejected](#62-an-overfit-fix-is-rejected): a special-case fix fails the checker
   - [6.3 Multi-Line Edits](#63-multi-line-edits): `--old-file`/`--new-file`
   - [6.4 Several Edits at Once](#64-several-edits-at-once): repeated `--old`/`--new` pairs
   - [6.5 Writing a Checker](#65-writing-a-checker): what a checker gets and must do
   - [6.6 See What a Fix Does Before Making It](#66-see-what-a-fix-does-before-making-it): candidate probes (`--old`/`--new`) and the `compare:` line
7. [C, C++, Rust and Java](#7-c-c-rust-and-java): what each language reads and changes
   - [7.1 C and C++](#71-c-and-c): read, set, withheld locals, strings and changing their text, maps, what `set` checks
   - [7.2 Rust (a Cargo crate)](#72-rust-a-cargo-crate): values read from memory, settable locals and texts, crate shape
   - [7.3 Java (launched)](#73-java-launched): configs, previews, fields and Strings that can be set
   - [7.4 Java Tests Already Running (Gradle `--debug-jvm`, e.g. Venice)](#74-java-tests-already-running-gradle---debug-jvm-eg-venice): attach to a `--debug-jvm` JVM
   - [7.5 Optimized Builds](#75-optimized-builds): `optimization`/`opt_level`: what changes
8. [Slow Programs and Tool Timeouts](#8-slow-programs-and-tool-timeouts): `--background` and `peeky job wait`
9. [A Program on Another Host](#9-a-program-on-another-host): a worker on another host, over SSH
10. [When Something Goes Wrong](#10-when-something-goes-wrong): common failures and what to do
11. [From pdb / gdb / lldb to Peeky](#11-from-pdb--gdb--lldb-to-peeky): debugger commands and their Peeky equivalents

---

## 0. Setup

### Install Peeky

Peeky needs Python 3.11 or later. `pip install` also brings debugpy, the debugger Peeky uses for
Python targets:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

### What each language also needs

| Language | Also needs | Peeky finds it from |
| --- | --- | --- |
| Python | nothing | the interpreter running `peeky` |
| C, C++ | clang, lldb-dap and dwarfdump: on macOS the Command Line Tools, on Linux LLVM 18 | `xcrun` or `PATH`. Set `PEEKY_NATIVE_*` to override |
| Rust | the rustup toolchain 1.96.0, and lldb-dap | `rustup which`. Set `PEEKY_RUST_*` to override |
| Java | JDK 17, and the [adapter bundle](lang/java.md#words-used-here): `python scripts/setup_java_adapter.py --output "$PWD/work/java-adapter"` | the target's `--config target.json` |

### Keep a task's runs apart

Peeky keeps saved runs and session records in `~/.peeky`. To keep one task's runs and sessions
separate, set `PEEKY_STATE_DIR=/some/dir`.

### How Peeky tells the language

Peeky tells a target's language from the path you give it:

| Path | Language |
| --- | --- |
| A `.py` file, or a directory with `--at module.py:<line>` | Python |
| A `.c` or `.cpp` file | C or C++ |
| A directory with a `Cargo.toml` | Rust |
| Any path with `--config target.json` | Java, or any target whose settings you want to pin |

---

## 1. First Look

The example `examples/pagination_bug.py` should return page 1 as `[10, 20, 30]`. It prints
`[40, 50, 60]` instead.

### 1.1 What Can I Ask?

**What it answers:** Which lines can stop? Which names exist in each function? What can be
changed?

```sh
peeky describe examples/pagination_bug.py
```

```
language: python
breakpoint lines in pagination_bug.py: 1, 4-9, 12-13
names in <module>: get_page
names in get_page (lines 4-9): end, items, page, page_size, start
mutable: Python literals: numbers, strings, bytes, True/False/None and lists/tuples/dicts/sets of them
probe: peeky probe examples/pagination_bug.py --at pagination_bug.py:<line> [--run <script.py>] [--arg A ...] --capture <a,b.c> ...
...
```

For a large module, `--json` and `jq` find a function quickly:

```sh
peeky describe dir --at module.py --json | jq '.scopes[] | select(.scope | test("is_private"))'
```

#### Names in C, C++ and Rust

C, C++ and Rust list the same, from the build's debug information. That is each function in your
sources, with its file, its lines, and its parameters and locals. Names are readable:
`crate::Type::method`, `<crate::Type as core::fmt::Display>::fmt`, `geo::Box::area`.

```
names in parse_count (pagination_bug.c:6-14): end (char *), minimum (int), result (int *), text (const char *), value (long)
names in main (pagination_bug.c:16-31): argc (int), argv (char **), end (int), i (int, lines 23, 28), items (int[128]), page (int), page_size (int), start (int), total (int)

names in peeky_pagination::bounded (src/main.rs:14-20): default (i32), minimum (i32), text (Option<&String>), value (i32, lines 16-19)
names in peeky_pagination::main::{{closure}} (src/main.rs:37-37): index (i32)
```

Closures and lambdas are listed under their function. A function inlined into another, with
`#[inline(always)]` or `always_inline`, keeps its own lines.

If the debug information can't be read, the text says `function names: unavailable (reason)`. A
very large optimized C++ unit can exceed the 16 MB bound on reading it.

#### How a function's name is shown

| Where | What it shows | Example |
| --- | --- | --- |
| A probe table's `function` column | Each frame's short name. A Rust function shows without its symbol hash | `trim_left`, `geo::Box::area`, `same<int>` |
| The same column, for two overloads in one table | The short name with the parameters | `twice(int)`, `twice(std::string const&)` |
| The same column, for a closure or a generic's instance | The name the build gives it | `cz::main::{closure#0}`, `cz::gen_apply<i32>` |
| JSON and the evidence store | LLDB's whole frame name: a C++ signature, or a Rust name with its symbol escapes | `trim_left(std::__1::basic_string<char, …> const&)`, `_$LT$…` |
| A session's text | The frame with its parameters, the standard types written as code writes them | `in trim_left(std::string const&)` |

#### Match a function in SQL

SQL compares the whole name, so `WHERE function = 'trim_left'` finds nothing. Match a frame with
`short_function(function) = 'trim_left'`, or with its whole name.

- A query's table that shows short names says so in a note. So does `peeky sql --help`.
- A probe's table says it once per state directory, and never with `--quiet`.

`short_function` drops the parameters of every overload, and a Rust symbol's hash. So these share
one name:

- overloads: `operator+` for `operator+(V, V)` and `operator+(W, W)`, and `G::get` for `get() &`
  and `get() &&`;
- a Rust function's closures: `cz::main::{{closure}}` for `{closure#0}` and `{closure#1}`;
- a generic's instances: `cz::gen_apply` for `gen_apply<i32>` and `gen_apply<i16>`.

A `GROUP BY` or a join on `short_function` combines them. So it is not always what a table shows:
a table keeps the parameters of overloads that would read alike.

To find one closure's or one instance's rows, use the `instance` column. `captures` keeps the
build's label there, and so do `evidence` and `interventions`. For example,
`WHERE instance = 'cz::main::{closure#0}'` finds one closure's rows. The table's note says so.

#### Block lines

A local declared only inside nested blocks also shows those blocks' lines, where it is visible.
Above, `i (int, lines 23, 28)` is the `i` of two one-line `for` loops, not a name of the whole
function. With `--json`, a scope's `spans` maps such a name to its blocks' `[first, last]` lines.

- A span leaves out lines between the block's own lines where only code outside the block runs.
  So one block can give several spans.
- In C++, a block's cleanup code is on the function's closing line. That code runs when an
  exception leaves the block. So `name (std::string, lines 22-25, 35)` is an `if` block's local.
  It is visible on the block's lines, and at the closing `}` while an exception unwinds. It is not
  visible on the lines between.
- A span is never narrower than where the debugger can show the local. So it keeps the lines
  inside it without code, such as comments or a lone `}`.
- A block that also runs another file's code, such as an `#include` inside it, gives no lines. A
  local macro's expansion, on its definition's lines, does not count as another file's code.
- Past eight blocks, one span covers them all.

#### Fields and elements `set` may change (C, C++, Rust)

For C, C++ and Rust, the `mutable:` line also names fields and elements, per function. They are
the integer fields and elements that `set` may change in struct, tuple and array locals:

```
Such fields and elements (`[*]`: any element): movavg::main: w.k, w.sum; board::main: board.entries
```

- Peeky judges them from the type and the local's storage, as `set` judges them. For Rust, the IR
  must keep the local in memory.
- They go to any depth through structs and fixed-size arrays: `grid.cells[*].n`,
  `grid.inner.deep[*][*]`.
- The line lists eight per local, then `… and N more`.
- Each is settable only where the local is initialized and the stop is a statement's first
  instruction ([C and C++ 7.1](lang/c-cpp-cookbook.md#71-c-and-c),
  [Rust 7.2](lang/rust-cookbook.md#72-rust-a-cargo-crate)).
- A `Vec`'s elements, such as `board.since[0]`, are never listed. Nor is what a pointer or a
  reference reaches, such as `self`.
- In JSON, a scope's `settable_parts` lists them: `{"w": ["w.k", "w.sum"]}`, up to 64 paths, then
  `… and N more`.

#### Where a step can land

A function's lines, such as `main (pagination_bug.c:16-31)`, and a local's block lines are where
its code is. They are not where a stop can land.

- A `session step` can stop on a line with no statement of its own. One is a closing `}`, where a
  block's locals go out of scope or a loop jumps back, in C, C++, Java and Rust. Another is a loop
  header on its later passes: `for (...; i++)`.
- A step can also land back on an `if`'s line after the last line of its block. For example, it
  goes from `self.sum -= old;` on line 16 to `if self.values.len() >= self.k {` on line 14, then
  to line 18. The compiler gives the jump that leaves the `if`'s block the `if`'s own line. The
  condition does not run again.
- A local listed with block lines, such as `i (int, lines 23, 28)`, is read only inside them. On
  the block's closing `}`, Java has already dropped it, so read it on the line before.
- Give `--at` and `continue --to` one of `describe`'s breakpoint lines. A step goes wherever the
  compiled code goes next.

**Per language:**

- [Python 1.1](lang/python-cookbook.md#11-what-can-i-ask): module globals in a function's names.
- [C and C++ 1.1](lang/c-cpp-cookbook.md#11-what-can-i-ask): C and C++ types.
- [Rust 1.1](lang/rust-cookbook.md#11-what-can-i-ask): block lines, type names, nothing settable.
- [Java 1.1](lang/java-cookbook.md#11-what-can-i-ask): declared types, `mutable:`.

### 1.2 First Probe

**What it answers:** What does the program actually compute at this line?

```sh
peeky probe examples/pagination_bug.py --at 8 --capture start,page,page_size
```

```
hit  phase     line  function  selector   type  value
1    observed  8     get_page  start      int   3
1    observed  8     get_page  page       int   1
1    observed  8     get_page  page_size  int   3
status: completed  hits: 1  seconds: 1.016  max pause: 4.1 ms
output:
  PAGE: [40, 50, 60]
evidence: saved as run 1; query it with `peeky queries run --param run=1` or `peeky sql` (see `peeky sql --schema`)
```

`start` is 3 for page 1: the offset treats pages as zero-based.

Progress messages go to stderr. `--quiet` hides them.

---

## 2. Asking Questions with Probes

A probe is one complete run. It builds the program if needed, launches it, stops at the line and
captures values. It can also change values. Then it runs the program to the end, reports, and
cleans up. Nothing is left running.

### 2.1 Fields and Indexes

**What it answers:** What is inside a list, a struct or an object?

```sh
peeky probe examples/pagination_bug.py --at 8 --capture 'items[0],items[-1],items'
```

```
hit  phase     line  function  selector   type  value
1    observed  8     get_page  items[0]   int   10
1    observed  8     get_page  items[-1]  int   60
1    observed  8     get_page  items      list  [10, 20, 30, 40, 50, 60]
```

#### Write a selector

A selector is a dotted path, an index or a string key: `request.page`, `args[0]`, `items[-1]`,
`order['id']`. It is never an expression: Peeky never evaluates code in your program.

A selector that is not there lists what is:

```sh
peeky probe examples/pagination_bug.py --at 8 --capture 'items[0],pagesize'
```

```
hit  phase     line  function  selector  type  value
1    observed  8     get_page  items[0]  int   10
1    observed  8     get_page  pagesize        <not found: 'pagesize' is neither a local nor a module global here; visible: items, page, page_size, start; module globals: the names in debugpy's function variables, special variables (select them by name)>
...
```

Quote a selector that contains `[...]`. Otherwise zsh, the macOS default shell, treats it as a
filename pattern.

#### Names in any script

Peeky matches a name exactly as the debugger lists it, non-ASCII names too: `größe.zähler`, `ñ`.
Peeky never normalizes a name, because compilers differ:

| Compiler | How it lists names |
| --- | --- |
| Python | Lists `größe` written NFD in the source as NFC, and `ﬁx` as `fix` |
| Rust | Lists names in NFC |
| javac and Clang | Keep NFC and NFD spellings as two variables |

So a spelling that differs from a listed name only by Unicode normalization is not matched. The
message gives the listed spelling, escaped. For example, `sizes.py` writes `größe` in NFD, and
Python lists it in NFC:

```python
def area(w, h):
    größe = w * h
    return größe


print(area(3, 4))
```

```sh
peeky probe sizes.py --at 3 --capture "$(printf 'gro\xcc\x88\xc3\x9fe')"   # größe in NFD
```

```
hit  phase     line  function  selector  type  value
1    observed  3     area      größe          <not found; visible: größe, h, w; the debugger lists 'größe' (written 'gr\xf6\xdfe'), which differs from 'gro\u0308\xdfe' only by Unicode normalization: names are matched exactly as the debugger lists them (Peeky never normalizes one), so select it as listed if that is the one you mean>
status: completed  hits: 1  seconds: 1.091  max pause: 5.0 ms
output:
  12
...
```

- `inspect` marks each row whose name differs from another row's only by Unicode normalization,
  that is, when the two are equal under NFKC. The mark says how the name is written:
  `[written 'gro\u0308\xdfe': ...]`.
- Names in different scripts that look alike, such as Latin `a` and Cyrillic `а`, are matched
  exactly too. `inspect` does not flag them.
- A name that holds an invisible character, such as U+200B, U+200D or a bidi control, is never
  selectable. Nor is a name longer than 128 characters. `inspect` marks its row
  `[not selectable]`, never `[settable]`.

**Per language:**

- [Python 2.1](lang/python-cookbook.md#21-fields-and-indexes): dict entries.
- [C and C++ 2.1](lang/c-cpp-cookbook.md#21-fields-and-indexes): pointers, maps and sets.
- [Rust 2.1](lang/rust-cookbook.md#21-fields-and-indexes): references and Boxes.

### 2.2 Every Iteration of a Loop

**What it answers:** How does a value evolve across iterations?

#### One row per pass

- `--hits N` captures N stops. `--hits all` captures every stop, up to 100 or fewer, and needs the
  program to exit ([below](#every-stop---hits-all)).
- `queries hits` shows one selector across hits, with `changed`.
- For one row per pass, put `--at` on a plain statement line inside the loop's body. Not on the
  loop's header, and not on a line with several stops.
- A line with a macro's code, a one-line loop or a range `for` can stop several times per pass,
  and the probe warns. There, read every row with its `stop` label. Don't pick a stop by its
  number alone ([below](#stops-not-passes)).
- A loop header may stop only once, before its loop, and the probe warns. Put `--at` on the first
  line of the body ([below](#a-loop-header-can-stop-only-before-its-loop)).
- Different functions on one line, such as a closure or a lambda, are told apart by each row's
  `function`.

```sh
peeky probe examples/corpus/running_max/program.py --at 6 --capture value,best --hits 3
peeky queries hits
```

```
selector  hit  line  value  error  caveat  changed
best      1    6     0      NULL   NULL    NULL
best      2    6     0      NULL   NULL    0
best      3    6     0      NULL   NULL    0
value     1    6     -3     NULL   NULL    NULL
value     2    6     -1     NULL   NULL    1
value     3    6     -2     NULL   NULL    1
```

`best` starts at 0 and never drops to a negative value. So the running maximum of all-negative
input is wrong.

`changed` compares a hit's value with the one before. It is NULL where a value is missing or
qualified:

- **Missing:** a hit where the selector has no value shows NULL, with the reason in `error`. A
  missing value is never compared.
- **Qualified:** `caveat` names a view that qualifies the value: `stale`, `unproven`, `opaque` or
  `budget-exhausted`. Or it says `note`, for a row the probe marked `[note]`, such as a C local's
  leftovers on a `do` body's first line. `queries run` shows the note.

#### Every stop: `--hits all`

`--hits all` captures every stop, up to 100, without counting iterations first. With more than 20
selectors the cap is lower, so that every hit fits the probe's budget of 2000 rows. The summary
says `hits: 3 (all)`:

```sh
peeky probe examples/corpus/running_max/program.py --at 6 --capture value,best --hits all
```

```
hit  phase     line  function     selector  type  value
1    observed  6     running_max  value     int   -3
1    observed  6     running_max  best      int   0
...
3    observed  6     running_max  best      int   0
status: completed  hits: 3 (all)  seconds: 1.07  max pause: 5.3 ms
...
```

- A warning says when the cap was reached.
- `all` needs the program to exit, so it can't be combined with `--until hits`. For an attached
  JVM, give a number.
- Progress prints the first hit, then at most one line every 2 seconds, then the total when the
  program finishes.

#### Stops, not passes

`--hits` counts stops, not passes. A probe stops each time the program reaches one of the line's
breakpoint locations. In C, C++ and Rust, a line can have several in one function. LLDB places one
at the first code of each block on the line. Such blocks are:

- a macro's own code: `format!` and `println!` in Rust, or a C macro such as
  `do { int t_ = a; ... } while (0)`;
- a one-line loop's body: `for (...) { int sq = i * i; ... }`;
- a C++ range `for` header: its range is set up once, before the loop, and its element is bound at
  each pass.

Each pass stops at each location it runs. `--hits N`, `--set-hit N` and `--set-at LINE@HIT` count
those stops. The probe warns, and each row's `stop` says which location it is at: 1 to N, in
address order. For example, `squares.c` has a one-line loop whose body declares a local:

```c
#include <stdio.h>

int main(void) {
    int total = 0;
    for (int i = 0; i < 3; i++) { int sq = i * i; total += sq; }
    printf("TOTAL:%d\n", total);
    return 0;
}
```

```sh
peeky probe squares.c --at 5 --capture i,total --hits all
```

```
hit  stop  phase     line  function  selector  type  value
1    1/2   observed  5     main      i         int   <no value yet: declared on line 5, which has not run yet at this stop (its first instruction), so not initialized: its storage holds leftovers; step once, or stop after line 5, to read it>
1    1/2   observed  5     main      total     int   0  [settable]
2    2/2   observed  5     main      i         int   0
2    2/2   observed  5     main      total     int   0
3    2/2   observed  5     main      i         int   1
3    2/2   observed  5     main      total     int   0
4    2/2   observed  5     main      i         int   2
4    2/2   observed  5     main      total     int   1
status: completed  hits: 4 (all)  seconds: 0.99  max pause: 36.7 ms
warning: Line 5 stops up to 2 times per pass in main (2 breakpoint locations): --hits, --set-hit and --set-at's @HIT count stops, not passes (as do session hit numbers); each hit's stop (1 to 2, in address order) says which location.
warning: Some selectors could not be read by the debugger; see each row's reason.
output:
  TOTAL:5
...
```

Hit 1 is at location 1, which runs once, before the loop. Hits 2 to 4 are at location 2, once per
pass.

- A probe shows the warning with its result. `session start` shows it with the first stop.
- `stop` is also in `--json`, the evidence, `queries run` and `queries wide`. In `queries wide`, a
  row's `stop` is empty unless every value in it was read at the same stop.
- A session's state line says `hit 4, stop 1 of 3`.

To read one row per pass, keep the location that every pass runs. Here that is location 2:

```sh
peeky sql "SELECT hit, value FROM last_run WHERE stop = 2 AND selector = 'i'"
```

```
hit  value
2    0
3    1
4    2
(3 rows)
```

To change a later pass, give `--set-hit` the hit of that pass's stop: here, `--set-hit 3` for the
second pass. In C, C++ and Rust, the stop must also be at a statement's first instruction
([2.3](#when-a-change-can-be-made-c-c-rust)).

A change says which stop its hit was: `set total: 0 -> 100  (stop 1 of 2)`. A `--set-at` change
names its line and function too: `set x: 0 -> 100  (--set-at line 9@2, stop 2 of 3, in s1::main)`.
That is the first pass's second location, not the second pass. `interventions.stop`,
`queries set_effect` and `queries wide`'s `sets` say it too: `line 9, hit 2, stop 2`.

Pick the location from the rows, not by its number alone:

- A loop header's first location runs once, before the loop. A C++ range `for` stops at 1, then
  at 2 at every pass.
- A branch can skip a location: `if (v[i] > v[i + 1]) SWAP(...)` stops at 2 only when it swaps.
- For the same reasons, Peeky has no `--first-stop` to stop once per pass. Breakpoints alone can't
  tell where a pass starts.

#### Find the location each pass reaches

Count each location's hits:

```sh
peeky sql "SELECT stop, COUNT(DISTINCT hit) AS hits FROM last_run WHERE phase = 'observed' GROUP BY stop ORDER BY hits DESC, stop DESC"
```

```
stop  hits
2     3
1     1
(2 rows)
```

| What the counts show | What it means |
| --- | --- |
| A count above 1 | That is the per-pass location. A C or C++ one-line loop whose body declares a local, and a range `for`, stop 1, 2, 2, 2: location 1 runs once, before the loop. A Rust one-line `for` stops 1, 2, 3, 3, 3. A macro's code stops 1, 2, 1, 2, where each location repeats |
| Every count is 1 | The loop ran at most once, or no location runs at every pass: `if (v[i] > v[i + 1]) SWAP(...)` stops 1, 2 over three passes. Read all the rows, or probe a statement line in the body. An empty loop still stops before it, with no pass at all: C++ `1`, Rust `1, 2` |
| One location | A one-line C or C++ loop whose body declares no local, such as `for (...) { total += i; }`. It stops once, however many passes run |
| A tie | A loop of one pass. The locations come out in `stop DESC` order, the later one first |

A stop doesn't say whether the frame left the line between two stops. So a call that returns
between two locations looks like one pass.

#### Lines with one location, and inlined copies

- A line with one location stops at most once per pass, however many statements it has:
  `a = twice(i); b = a + 1;`. No warning is given.
- Different functions on one line are a separate case: a closure, a lambda, a generator expression
  or a generic's instances. Rows' `function` tells them apart, with a warning of its own.
- A function inlined into its callers has a copy of its code at each call. Examples are
  `#[inline(always)]`, and a C `static inline` with `always_inline` at `-O0`. Each copy is a call
  of its own, and its line stops at that copy's locations.
- So `format!` in a function inlined twice stops 3 times at each call, 6 in all. The warning says
  so once, and `stop` counts within a copy.

#### A loop header can stop only before its loop

A breakpoint binds a line's first code. A loop header's first code often runs once, before the
loop starts, and never at the passes. Examples:

- `i = 0` of a C, C++ or Java `for (int i = 0; ...)`;
- the jump into a C or C++ `while` loop, or into Rust's `while`;
- a Java for-each's copy of its array, or its iterator;
- Rust's `for v in xs`, at its two locations: `into_iter` and the iterator's binding.

Then `--hits all` gives one row, or two, where you expected one per pass. So Peeky warns. A probe
shows the warning with its result, and `session start` with the first stop:

```c
#include <stdio.h>

int main(void) {
    int total = 0;
    for (int i = 0; i < 3; i++) {
        total += i;
    }
    printf("TOTAL:%d\n", total);
    return 0;
}
```

```sh
peeky probe header.c --at 5 --capture total --hits all
```

```
hit  phase     line  function  selector  type  value
1    observed  5     main      total     int   0  [settable]
status: completed  hits: 1 (all)  seconds: 0.965  max pause: 35.5 ms
warning: Line 5 is a loop header: it stops once each time its loop starts, before the first pass, never at the passes; put --at (or --set-at) on the first line of the loop's body for one stop per pass.
output:
  TOTAL:3
...
```

Peeky decides from the build's code, never from the source's shape. The function's control flow
gives its loops: LLDB's disassembly, or Java's bytecode at the location java-debug binds. The line
table gives the line of each instruction.

- **When it warns:** a loop is the line's when its header holds any of the line's code. Peeky warns
  only when both of these hold:
  - Every breakpoint location of the line is outside that loop. And every path from a location
    passes through the loop's header before it can come back to the location or return. So the
    location runs once each time the loop starts, never at a pass of a loop around it.
  - The line's code inside the loop is only the loop's own. That is its condition, its increment,
    or a for-each's binding at the start of the body. And the loop holds other lines' code too.
- **Nested loops:** a nested loop's header stops once each time that loop starts, so once per pass
  of the loop around it. Each location stops at most once per start. Where a location runs only
  under a condition on the line, such as `if (z) { int w = 5; ... } for (...) {`, the note says
  `up to 3 times`.
- **When it doesn't warn:** the stops are then at each pass of a loop around the line. This is the
  case for a loop entered under a condition, `if (r == 3) for (...) {`. It is the case for a line
  holding a whole loop: `for (...) total += i;`, a statement macro's `for`, or
  `do { for ... } while (0)`. And for a line with other statements:
  `let mut n = 0; while n < 2 { ... } total += n;`. There, the
  [per-pass warning](#stops-not-passes) applies where the line has several locations.
- **Jumps:** there is no note for a line whose code only jumps into a loop: `goto`, `for (;;)`,
  `do {`, or Rust's `loop {`.
- **Headers that run at each pass:** some headers' first code is the condition the loop returns
  to. Such a header stops at every pass, and gets no note. Examples:
  - Java's `while (k < n)`, `for (; j < n; j++)` and `do`'s `while`;
  - a C or C++ `do`'s `while`;
  - a C++ range `for`, whose element is bound at each pass;
  - Rust's `while let`.
- **When it can't tell:** Peeky says nothing where it can't read the flow. That is a jump table,
  flow into the middle of a loop, or an architecture other than arm64 or x86-64.
- **Which lines it checks:** only a line that names a loop. It names one with `for`, `while`,
  `loop` or `do`, a name with one of them in it, or a macro's capitals. Peeky skips a line in more
  than 8 functions or inlined copies.
- **Cost:** it asks the debugger once, at launch: one line table per file and one disassembly per
  function. It never asks again when a probe re-arms its breakpoints.

#### Loops in Python and Java

- Python stops once per pass at a line. A `for` header stops once per pass, and once more when the
  loop ends.
- Java and Python rows have no `stop`.
- A Python local that the pass assigns below the stop shows an earlier pass's value, with a `note`
  ([Python 5.1](lang/python-cookbook.md#51-stop-look-change-finish)).
- `describe` doesn't mark loop headers or lines with several locations. For C, C++ and Rust it
  would need the debugger's locations, which only a run has.

**Per language:**

- [C and C++ 2.2](lang/c-cpp-cookbook.md#22-every-iteration-of-a-loop): GCC's `for (;;)` and `do {`.

### 2.3 Test a Hypothesis by Changing a Value

**What it answers:** If this value were right, would the output be right?

```sh
peeky probe examples/pagination_bug.py --at 8 --capture 'start,items[1]' --set start=0
```

```
hit  phase      line  function  selector  type  value
1    observed   8     get_page  start     int   3
1    observed   8     get_page  items[1]  int   20
1    after-set  8     get_page  start     int   0
after-set: 1 row equal to the hit's observed value left out (`--json` has every row)
set start: 3 -> 0
status: completed  hits: 1  seconds: 1.004  max pause: 10.0 ms
output:
  PAGE: [10, 20, 30]
```

With `start` at 0, the program prints the right page.

#### What a change can reach

`--set` changes root locals: the stopped function's own variables, such as `start`. Each language
limits which ones, and what else a change can reach:

| Language | What `--set` can change | Rules |
| --- | --- | --- |
| Python | The frame's own variables, to a literal. Fields are refused before the program runs | [Python](lang/python.md#change-values) |
| Java | Locals, and also scalar fields and array elements, each only under its conditions | [Java 7.3](lang/java-cookbook.md#what-can-be-set) |
| C, C++ | Integer locals, and integer fields and elements of a struct or array local that the stopped frame holds itself: `s.end`, `xs[2]`. Also a text, into some string locals ([below](#which-variables-take-a-text)) | [C and C++ 7.1](lang/c-cpp-cookbook.md#71-c-and-c) |
| Rust | Integer locals that the program keeps in memory, and integer fields and elements of a struct, tuple or array local that the frame holds itself: `t.__0`. Also a text, into a `String` local ([below](#which-variables-take-a-text)) | [Rust 7.2](lang/rust-cookbook.md#72-rust-a-cargo-crate) |

- In C, C++ and Rust, a change never goes through a pointer, a reference or a `Box`. It is also
  made only where no statement is under way ([below](#when-a-change-can-be-made-c-c-rust)).
- In Python, a change rebinds the name in that frame. `--set 'seen=[]'` gives this call a new
  list. The shared object that `seen` named keeps its contents, such as a default argument or a
  module global. The next call sees that object again.

#### When a change can be made (C, C++, Rust)

A change writes the variable's storage, which the program's later reads see. What the program
already copied keeps the old value. So C, C++ and Rust allow a change only where Peeky proves that
no statement is under way. They refuse it anywhere else, such as after `step --out`, or on a
statement's later line ([C and C++ 7.1](lang/c-cpp-cookbook.md#71-c-and-c),
[Rust 7.2](lang/rust-cookbook.md#72-rust-a-cargo-crate)).

For example, the second pass of `squares.c`'s one-line loop stops part-way through the line's
statement:

```sh
peeky probe squares.c --at 5 --capture total --hits 4 --set total=100 --set-hit 3
```

```
hit  stop  phase     line  function  selector  type  value
1    1/2   observed  5     main      total     int   0  [settable]
2    2/2   observed  5     main      total     int   0
3          observed  5     main      total     int   0
status: failed  hits: 3 before it failed  seconds: 0.81  max pause: 36.5 ms
warning: Line 5 stops up to 2 times per pass in main (2 breakpoint locations): --hits, --set-hit and --set-at's @HIT count stops, not passes (as do session hit numbers); each hit's stop (1 to 2, in address order) says which location.
error [MUTATION_REJECTED]: 'total' cannot be changed here. Change it at a statement's first instruction: stop on the statement's first line (a probe's `--at`, `session continue --to LINE`), or `session step` to it. Here, the stop is not at the first instruction of the statement on line 5: it is part-way through it (after a call returns, on a loop's later pass, or where the compiler moved code), so the statement may hold a copy of the old value and would not see the change; settable here: none
hint: Stop on the statement's first line (`--at`, `session continue --to LINE`), or `session step` to it.
...
```

#### What values each language takes

| Language | Takes |
| --- | --- |
| Python | Literals, including lists and dicts: `0`, `'x'`, `None`, `[]`, `{'k': 1}`. `-1e309` is negative infinity |
| C, C++ | Integers of 16 to 64 bits, such as `int`, `long`, `size_t` or `int64_t`, within the local's range |
| Java | Scalars of the target's type. A char takes a char in single quotes, `--set "op='+'"`, or a code point, `op=43` |
| Rust | Integers, into `i16` to `i64`, `u16` to `u64`, `isize` and `usize` locals that the program keeps in memory. A local kept only in a register is refused: a change would not reach the program. Other types take no value, apart from a text into a `String` local ([below](#which-variables-take-a-text)) |

Expressions, names and calls are refused with `MUTATION_REJECTED`: `min(xs)`, `float('inf')`. Peeky
never evaluates them.

#### Which variables take a text

A value in double quotes is a text. Write it as JSON writes a string, and quote it for the shell:
`--set 'name="apple"'`, `"say \"hi\""`, `"caf\u00e9"`.

| Language | Where a text can go | Conditions |
| --- | --- | --- |
| C, C++ | a `char[N]` local | only if the text and a NUL both fit |
| C, C++ | a `std::string` local | in place, only within its capacity, never reallocating |
| Rust | a `String` local | only within its capacity, while no borrow of it is used after the stop, in a crate without `unsafe` code |
| Java | a variable declared `String` | Java makes a new String; [Java 7.3](lang/java-cookbook.md#set-a-string) has the other conditions |
| Python | the frame's own variables | Python evaluates the text as a Python literal |

- A text never goes through a pointer or a reference: `char *`, `&str`, `const std::string &`.
- In C, C++ and Rust, a text never goes into a field or an element.
- The language cookbooks have the details: [C and C++ 7.1](lang/c-cpp-cookbook.md#71-c-and-c),
  [Rust 7.2](lang/rust-cookbook.md#72-rust-a-cargo-crate) and
  [Java 7.3](lang/java-cookbook.md#73-java-launched). [2.8](#28-change-a-value-then-restore-it)
  has a `--set-at` example.

#### `[settable]` marks on rows

For C, C++ and Rust, rows mark what a change would reach with `[settable]`. The mark is on locals
in `session inspect`, and on fields and elements in probe rows and `session get` listings. When a
rule leaves nothing settable at a stop, `session inspect` says why in a note.

In JSON, every root, field and element row that a change could name carries `settable: true` or
`false`, Java's rows too. `false` includes two things that `set` refuses:

- the internals of a standard-library type, such as `h.v.len` of a `Vec` field, raw where no view
  reads it;
- what Peeky decodes from memory, such as a `Vec`'s elements and `len`.

#### Which `after-set` rows a table shows

After a change, Peeky reads each selector again, in an `after-set` row. The table leaves out an
`after-set` row equal to its hit's `observed` row only when both of these hold:

- Its value is shown whole and holds nothing else: a number, a boolean, null, or a quoted text with
  no `…` or `...` cut.
- Its selector is neither a change's target nor holds one, as `s` holds `s.ninth`.

`items[1]` above is such a row. A footer counts the rows left out, and `--json` has every row.

These rows stay, since they may hide a change:

- a pointer;
- a preview or summary of an aggregate;
- a cut value;
- a row with a note;
- a Java value other than a primitive: a String set through an alias is a new object with the same
  text;
- a Python value whose identity changed.

#### Every change is read back

- Peeky reads every change back.
- A change that the debugger refuses or can't store is refused, and the session is kept. An
  example is a computed value, such as a structured binding of a bitfield.
- A value stored differently from the one asked for closes the session. An example is a type
  redefined as a float.
- A refusal, `MUTATION_REJECTED`, names the local and the rule. Its hint gives the one way around
  it.

### 2.4 Change the Value at a Later Hit

**What it answers:** Does fixing the value on iteration N fix the output?

```sh
peeky probe examples/corpus/running_max/program.py --at 6 --capture best --hits 3 --set best=-3 --set-hit 1
```

```
hit  phase      line  function     selector  type  value
1    observed   6     running_max  best      int   0
1    after-set  6     running_max  best      int   -3
2    observed   6     running_max  best      int   -1
3    observed   6     running_max  best      int   -1
set best: 0 -> -3
warning: Interventions are applied once, at hit 1; other hits observe without re-applying.
output:
  MAX: [-3, -1, -1]
```

With `best` at -3 from the first hit, the running maximum is right. In C, C++ and Rust,
`--set-hit` counts stops, not passes ([2.2](#stops-not-passes)).

### 2.5 A Module Imported by a Script, with Arguments

**What it answers:** What happens inside a library for one specific input?

This is a real bug: CPython 3.9's `ipaddress` (CVE-2024-4032) calls the public address
`::ffff:8.8.8.8` private.

```sh
peeky probe examples/real/ipaddress_mapped --at ipaddress39.py:2008 --run classify.py \
  --arg ::ffff:8.8.8.8 --capture self,self._ip
```

```
hit  phase     line  function    selector  type         value
1    observed  2008  is_private  self      IPv6Address  IPv6Address('::ffff:808:808')
1    observed  2008  is_private  self._ip  int          281470816487432
output:
  ::ffff:8.8.8.8: private
```

- `--run` names the script to run when the breakpoint is in a module that the script imports.
- `--arg` passes one program argument, and can be repeated. For a value that starts with a dash,
  write `--arg=-v`.
- Or put all of the program's arguments after `--`: `peeky probe app.py --at 5 -- A 5 5 0`.

C, C++, Rust and launched Java programs take arguments too:

```sh
peeky probe examples/c/pagination_bug.c --at 25 --capture page,page_size,start -- 2 3 8
```

```
hit  phase     line  function  selector   type  value
1    observed  25    main      page       int   2  [settable]
1    observed  25    main      page_size  int   3  [settable]
1    observed  25    main      start      int   6  [settable]
status: completed  hits: 1  seconds: 0.998  max pause: 54.8 ms
output:
  PAGE:[70, 80]
...
```

### 2.6 Stop Right After the Last Hit

**What it answers:** I only need the values, not the output. Can it be faster?

Add `--until hits`. The probe then ends right after its last hit, instead of waiting for the
program to exit. This saves the most where the program is slow to exit, as JVMs and Python are.
Attached JVMs work this way by default.

```sh
peeky probe examples/pagination_bug.py --at 8 --capture start --until hits
```

```
hit  phase     line  function  selector  type  value
1    observed  8     get_page  start     int   3
status: completed  hits: 1  seconds: 0.348  max pause: 6.0 ms
output so far (the program ran on after the last hit, so this can include text written after it):
  PAGE: [40, 50, 60]
```

That took about 0.3 s. Waiting for the program to exit took 1.0 s.

#### What happens after the last hit

After the last hit, Peeky removes its breakpoints, resumes the program, reads its output, and
then ends the program. An attached JVM is detached instead, and keeps running.

Until then, the program runs unwatched. What it prints meanwhile may be in `output`. What it does
happens, such as writing a file.

#### Why the output is not the output at the hit

So the output may include text written after the hit, as `PAGE: [40, 50, 60]` above was:

- JSON says `output_after_last_hit: true`, and `output_complete` is `false`.
- The text says so above the output, as in the example:

  ```
  output so far (the program ran on after the last hit, so this can include text written after it):
  ```

For the whole output, run to the exit, which is the default.

#### When the output is complete

Sometimes the program had already exited before Peeky disconnected, and Peeky read all of its
output. That happens when the debugger reported the program's end (`exited` or `terminated`)
first. Peeky then ends the adapter's input and reads its stream to its end. Output can follow
`terminated`, but none can follow that end.

The output is what the debugger sent before Peeky sent its disconnect. When nothing more came
before the end, not even while the adapter handled the disconnect:

- `output_complete` is `true`, with `exit_code`;
- the text says `output (all of it: the program ran on after the last hit, to its exit):`;
- or, with `output_truncated: true`, the text says:

  ```
  output through the program's exit (it ran on after the last hit; truncated: some of it was dropped):
  ```

A C, C++, Rust or Java program that exits right after the hit can end in time. A Python program
rarely does, because debugpy delays its exit by about 0.5 s.

#### When the program ended, but its output isn't proven whole

In these cases, `output_complete` stays `false` with `program_ended: true`. The text says that the
program ended `but its output may still have been arriving`:

- The adapter keeps its stream open for 2 s.
- The adapter sends text once Peeky began closing the session, even before it answers the
  disconnect. That text, its own or the program's that was still on its way, is kept apart in
  `output_after_disconnect`. For example, lldb-dap 18 on Linux prints its crash as it exits.
- The adapter exits with a status other than 0. The result has `adapter_exit` and a warning: the
  adapter may have dropped output it held.
- The end is only Peeky's inference: java-debug dropped its exit event (see [Java](lang/java.md)).

One non-zero exit doesn't count. debugpy exits with status 1 when Peeky ends its input as debugpy
answers the disconnect. Peeky provokes that exit. If debugpy's only failure is the one it then
prints on stderr, the result has `exit_provoked: true` and no warning.

These rules hold for any probe, not only for `--until hits`.

### 2.7 JSON for Scripts

**What it answers:** How do I feed results into another tool?

Add `--json` to any command, and read the result with a tool such as `jq`:

```sh
peeky probe examples/pagination_bug.py --at 8 --capture start --json --quiet | jq -c '.by_hit[0].observed'
# {"start":"3"}
```

Branch on the exit status:

| Status | Meaning |
| --- | --- |
| 0 | Success. |
| 1 | A completed command with a negative result: a probe that failed after the debugger's launch or attach request was sent, timed out, or never stopped; a verify that didn't pass, a failed build included; a `session get` with a failed selector. |
| 2 | An error, or a refusal. See below. |
| 3 | Not there yet: a background job is still running, or `session start --hit`, `session continue --count` or `session continue --to` stopped before the stop asked for. |
| 4 | A probe completed, but a change it was asked to make (`--set`, `--set-at`) never happened, because its stop never came ([2.8](#28-change-a-value-then-restore-it)). This holds for `peeky probe` and for the `peeky-probe` script. The values are evidence, but the experiment didn't happen. |

- Status 2 includes any probe that ended before the debugger's launch or attach request was sent,
  whatever its code. JSON says `refused_before_launch`. The cause can be a refusal, a file outside
  the worker root, a build that failed, or a debugger that didn't start.
- In that case Peeky didn't start the program, though a build, or a Python candidate's
  interpreter, may have run. So the text has no table, only the error and its hint, and no run is
  saved.
- Fewer hits than `--hits` asked for is not a failure on its own: the status is 0, with a warning.

[ERRORS.md](ERRORS.md) lists every exit status and error code.

### 2.8 Change a Value, Then Restore It

**What it answers:** Would the output be right if one value were different for one statement only?

**Common cases**

- `--set-at LINE@HIT name=value` changes a value at hit `HIT` of `LINE`, in the `--at` file. `HIT`
  counts that line's own stops. Repeat it to change a value at one line and restore it at the next.
- Values are captured only at `--at`. A change there gets `after-set` rows, as `--set` does.
- `HIT` counts every stop of the line, in any function, a lambda's too. Each change says whose
  variable it changed.
- A change that never happened, because its stop never came, makes the probe exit 4.

#### Example: change a value for one line

`stock.cpp` (C++) treats `"apple "` and `"apple"` as different items, because `trim_left` leaves
trailing spaces. Line 22 is `std::string name = trim_left(entry.substr(0, colon));`. Line 23 is
`long qty = std::stol(entry.substr(colon + 1));`.

To test a fix, shorten `colon` so that line 22 cuts the space off. Then restore it, so that line
23 still reads the quantity:

```sh
peeky probe stock --at stock.cpp:22 --capture colon,entry --hits all \
  --set-at 22@3 colon=6 --set-at 23@3 colon=7 \
  --set-at 22@4 colon=4 --set-at 23@4 colon=5 -- "apple:3, pear:2, apple :4,pear :1"
```

```
hit  phase      line  function  selector  type         value
...
3    observed   22    main      colon     size_t       7  [settable]
3    observed   22    main      entry     std::string  " apple :4"  [settable]
3    after-set  22    main      colon     size_t       6  [settable]
4    observed   22    main      colon     size_t       5  [settable]
4    observed   22    main      entry     std::string  "pear :1"  [settable]
4    after-set  22    main      colon     size_t       4  [settable]
after-set: 2 rows equal to the hit's observed value left out (`--json` has every row)
set colon: 7 -> 6  (--set-at line 22@3, in main)
set colon: 6 -> 7  (--set-at line 23@3, in main)
set colon: 5 -> 4  (--set-at line 22@4, in main)
set colon: 4 -> 5  (--set-at line 23@4, in main)
output:
  STOCK:apple=7,pear=3
```

If you only shortened `colon`, line 23 would read `:4`, and `std::stol` would throw. With `--set`
alone, you would need a session for this: start, set, step, set, and so on.

Or change the text itself, once line 22 has made it. `name` is a `std::string`, so it takes a text
in double quotes ([2.3](#23-test-a-hypothesis-by-changing-a-value)), and nothing needs restoring:

```sh
peeky probe stock --at stock.cpp:24 --capture name --hits all \
  --set-at '23@3' 'name="apple"' --set-at '23@4' 'name="pear"' -- "apple:3, pear:2, apple :4,pear :1"
```

```
set name: "apple " -> "apple"  (--set-at line 23@3, in main)
set name: "pear " -> "pear"  (--set-at line 23@4, in main)
output:
  STOCK:apple=7,pear=3
```

#### Where and when a change is made

- `--set-at [PATH:]LINE[@HIT] name=value` changes a value at hit `HIT` of `LINE`, in the `--at`
  file. `HIT` is 1 by default, and counts that line's own stops.
- Repeat `--set-at` for several lines and hits. Changes at one stop are made in the order given.
- The probe stops at every `--set-at` line as well as at `--at`, but captures values only at
  `--at`. A change at a captured stop gets `after-set` rows, as `--set` does.
- `--set` and `--set-hit` work as usual. Together with `--set-at`, they make at most 8 changes.

#### Which function's variable changes

`HIT` counts every stop of the line, in any function:

- a lambda or closure on the line: `auto f = [](long v) { return v + 1; }; total += f(v);` stops
  in `main`, then in the lambda;
- a recursive call;
- a generic's instances. In Rust they are named with their type arguments: `gen::tf<gen::A>` and
  `gen::tf<gen::B>`. A name past 300 characters is cut with `…`, and two names that then read
  alike get their symbol hashes. Closures are named like `main::{closure#1}`.

Each change says whose variable it changed:
`set v: 0 -> 100  (--set-at line 6@2, in main::$_0::operator())`. JSON and the `interventions`
table have it as `function`. When a `--set-at` line stopped in more than one function, a warning
lists each function's stops.

#### How long the probe runs

- The probe keeps each breakpoint until the changes on that line are made, past `--hits` if need
  be.
- With `--until hits`, the probe ends once both are done. An attached JVM never exits, so it waits
  for them until `--timeout`.
- A change whose stop never comes is not made: for example `23@9`, where line 23 stops 4 times.
  The result says so: `status: completed (1 requested change not made)`, a warning, `unapplied` in
  JSON, and exit code 4.
- The same holds for a `--set` whose `--set-hit` never comes.

#### Which changes are refused

- Each change follows the same rules as `--set` at its own stop
  ([2.3](#23-test-a-hypothesis-by-changing-a-value)). Those rules cover a statement under way, a
  local not yet initialized, a value with a note, and a field through a pointer.
- Before the program runs, each `--set-at` is judged against the functions on its own line, as a
  `--set` is against the `--at` line's. A change that no stop of its line could make is refused
  there, naming the line, and nothing runs. Examples are a Rust local kept in a register, and a
  field through a pointer ([6.6](#66-see-what-a-fix-does-before-making-it)). The exit code is 2,
  and the text has only the error and its hint.
- One refusal fails the whole probe. The error names its line and hit, and how many changes were
  already made. Half of a linked change is not the experiment you asked for.
- A line with no code of its own is refused. It is never moved to the next line.

#### How the evidence records changes

- `runs.sets` lists each requested change as `[name, value, "LINE@HIT"]`. A `--set` stays
  `[name, value]`, with its hit in `runs.set_hit`.
- `queries diff`'s `(sets)` row and `queries outputs` show each change, a `--set`'s too, as
  `[name, value, "requested at LINE@HIT"]`: `["gleich", "true", "requested at 56@4"]`. They add
  `, not made: that hit never came` where the probe warned so.
- So runs whose changes differ only in their hit, or in which changes were made, tell apart.
- The `interventions` table lists each change made: its `line`, `hit`, `capture_hit`, `function`
  and `instance` (schema 12). `captures.instance` (schema 13) labels captured values the same way.
- `queries set_effect` lists the changes in the order they were made.
- `queries wide` puts each change in the `sets` column of the row it comes before.

**Per language:** [Python 2.8](lang/python-cookbook.md#28-change-a-value-then-restore-it)
(two lambdas on one line).

### 2.9 What a Function Returned

**What it answers:** What did each call of this function return? That includes calls a library
makes, such as a comparator that `std::sort` or `qsort` calls, a key function, or a `Comparator`.
This works for C, C++, Rust and Java.

**Common cases**

- `--capture return` with `--hits all` gives a `(return)` row per hit: what that hit's call of the
  `--at` line's function returned, a library's calls included. For one row per call, probe the
  function's first line.
- A value is shown only when it is exact. That means C and C++ integers, `bool`, characters,
  floats and pointers; Rust integers, `bool` and floats; and Java values of any type.
- A struct, a string, or any return in an optimized build is withheld, with the reason.
- After `session step --out`, the state shows what the function returned: `(return): int = 42`,
  or `returned` in JSON. Or it shows a withheld row with the reason, following the rules below.
  That includes a call that an exception, a `longjmp` or a panic ended.
- It shows nothing where another stop interrupted the step first, or where the program ended.
  `session inspect` lists the value under `[Returned]`, and `session get return` reads it.
- Python's returns can't be captured. Capture what the caller stores, on the caller's next line:
  for `r = f(x)` on line 20, use `--at 21 --capture r`.

```sh
peeky probe sched.cpp --at 16 --capture "a.name,b.name,return" --hits all -- "build:2:30,test:2:10,deploy:1:5,lint:3:50"
```

```
hit  phase     line  function  selector  type         value
1    observed  16    before    a.name    std::string  "test"
1    observed  16    before    b.name    std::string  "build"
1    returned  115   before    (return)  bool         false
...
```

`return`, or `(return)`, captures what the function holding the `--at` line returns. After each
hit, Peeky steps out of that call, one frame, and records a `(return)` row whose `phase` is
`returned`. Its `function` is the function that returned. Its `line` and `location` are where the
function returned to, often in the library that called it.

#### One row per hit

- Each hit's `(return)` row is what its call returned, or why that value is withheld.
- On a line that runs once per call, with `--hits all`, that is one row per call.
- A recursive call's return goes to its own hit, also through another function. If `f` calls `c`
  calls `f`, Peeky steps out of `c` too, until each `f` returns.

#### Which line to probe

Probe the function's first line, or any line that runs once per call.

A line can stop again at the same depth before the call returns. That happens at a loop's next
pass, or at the line's next breakpoint location, on a line with several. In C, C++ and Rust, Peeky
can prove that such a stop is in the same call. Three things must hold:

- the step out's watch on the caller (see [The watch on the caller](#the-watch-on-the-caller)) held
  until that stop;
- the frames under the stop are the call's;
- the caller is at the same stack pointer.

No other call could then have returned to that place without running the watched call instruction
there. So Peeky follows the return across that stop, and each of those hits gets the call's
return.

#### When the return is withheld

Otherwise, the earlier hit's return is withheld rather than guessed. The row says why:

| Case | The row says |
| --- | --- |
| The stopped function is not the call's | the line `stopped again at the same depth in a call of another function` |
| The frames under the stop are not the call's | `in another call` |
| Anything else: in Java, which has no such watch; where the watch was dropped; or where another stop came in between | Peeky `could not tell whether in this call` (another of the line's breakpoint locations, or a loop's next pass) `or in a later call after this one ended without returning` |
| A call still running when the program ends | `the program ended before it returned` |
| A call still running when the probe's `--timeout` runs out | `the probe timed out before it returned` |

On a line with several breakpoint locations, each `(return)` row has its hit's `stop`, and `stops`
in JSON. That is where its call was stopped, not the line it returned to.

#### What it costs

Each hit costs a step out to its return, through every call it makes. Through a deep or wide
recursion, `--capture return` costs about 7.5 ms per nested call on macOS. In such a recursion,
`f` calls `c` calls `f`, and each call makes several more. A tree of depth 13 took 62.6 s, against
1.8 s without `return`. There, limit `--hits`, or probe a line that only one call reaches.

#### A call that never returns

A call that never returns gets no value.

In C, C++ and Rust, an exception, a `longjmp` or a panic can end a call while its caller goes on.
For example, the caller catches the exception, `setjmp` returns again, or `catch_unwind` returns.
LLDB's step out then stays armed. The next call that returns to the same place, in the same frame,
ends it. LLDB then lists that call's value as the first call's.

So while it steps out, Peeky watches the caller on that thread, in a frame at or above the
caller's. It watches the call instruction, every branch to the address the call returns to, and
every computed jump. None of them can run there while the call is still running.

If one of them runs, the step ends there, where the caller went on. The row says:

```
it never returned: its caller went on without its return (an exception, longjmp or panic ended it)
```

When the step does end at the return address, the stack pointer must be the caller's again, or
the value is withheld.

#### The watch on the caller

- The watch reads the caller's disassembly once per function. Peeky forces it for a function too
  large for LLDB's default. If LLDB still gives none, the row says so.
- A deeper call of a recursion through the caller runs the watched call too. Each run costs LLDB a
  stop of its own. After 128 of them in one step, LLDB drops the watch, and the step goes on at the
  cost it has without one. The value is then withheld:
  `the watch was dropped to keep the step's cost bounded`.
- No step fails for this. A `step --out` over a recursion thousands of calls deep completes. A run
  of LLDB's own stops takes one place in the session's event history.
- An optimized build is not watched, because its returns are withheld anyway.

#### Other cases

- **Inlined functions:** a function inlined into its caller (`always_inline`) has no return of its
  own. Its return is withheld, saying it was inlined.
- **Threads:** a probe follows one thread's returns at a time. A call that another thread's stop
  comes before is withheld: `another thread stopped at a breakpoint before it returned`. So is one
  that another thread's stop comes with, because LLDB stops threads together. The probe goes on
  with every hit's rows.
- **A `--set-at` inside the function:** a `--set-at` line inside the function stops before the
  return. The return still comes, and it is that call's. The probe checks that it comes back to the
  frame that made the call.

#### In a session

A session shows the same after `session step --out`:

- The step's state shows the return: `(return): long = 1`, or `returned` in JSON. It shows nothing
  where another stop interrupted the step first.
- `session inspect` lists it under `[Returned]`, apart from the locals (`returned` in JSON),
  labelled with the function that returned. `session get return` reads it.
- A step that stops outside your sources (`outside the program's sources`, in `sort.h` or libc)
  still shows what your function returned. The library's own locals are not read.
- In C, C++ and Rust, a `step --out` of a call that throws stops where its caller goes on without
  it. That is the next call from the same place, or a jump back to it. `[Returned]` says that the
  function never returned.

#### Which values are shown

Values are shown only when they are exact. LLDB reads a returned value from the return register:

- C and C++ show integers, `bool`, characters, `float`/`double` and pointers.
- Rust shows its integers (a `u8` as Rust prints it: `65`), `bool` and floats.
- Java's returns are JDI's exact values, of any type.

These are withheld, with the reason:

- a struct, a class, a `std::string`, a reference, a Rust enum or `Option`;
- a value the debugger can't read;
- any return in an optimized build;
- the return of a function that returns nothing.

Otherwise, LLDB would step out past libc++'s hidden frames and code without debug information,
and then list another value as that function's. So Peeky keeps each step out to one frame, and
checks that it stopped where the call returns.

lldb-dap before LLVM 21 lists no `(Return Value)`; the Linux lanes use 18 and 20. Peeky then reads
what LLDB itself recorded for that step out (`thread info`), under the same checks.

Python's returns are not available. debugpy shows one only by writing it into the program's
globals (`__pydevd_ret_val_dict`), which the program could see. So Peeky refuses
`--capture return` for Python: capture what the caller stores.

**Per language:** [Java 2.9](lang/java-cookbook.md#29-what-a-function-returned)
(a method that threw).

---

## 3. Comparing Runs

Every probe not refused before launch is saved with a run id (`--no-save` skips that). `--label` tags a run so you can find it later.

### 3.1 Two Inputs Side by Side

**What it answers:** Where do two runs diverge?

**Common cases**

- Probe each input once (`--label` each), then `peeky queries diff`: it compares the last two runs unless you name them (`--param a=1 --param b=5`).
- Read the `(status)`, `(output)`, `(edit)`, `(source)`, `(line)`, `(args)` and `(sets)` rows first: they say how the runs differed before you credit an edit.
- Then come real divergences, the first one first; address-only and qualified values come after them.
- Hits pair by number: where the runs stopped a different number of times (a sort's comparator), or as often but where hits may not be the same moments (an edit that changes the calls), a `note:` says so: use `queries join` (3.5) with a value that names the call, or several (`key=a,b`). Where values read the same at every hit in both runs and nothing suggests another pairing, a `note:` says so, and with no intervention in either run, that the hits line up by them.

```sh
p() { peeky probe examples/real/ipaddress_mapped --at ipaddress39.py:2008 --run classify.py --capture self,self._ip "$@"; }
p --arg ::ffff:8.8.8.8  --label mapped-public
p --arg ::ffff:10.0.0.1 --label mapped-private
peeky queries diff
```

```
run_a  run_b  hit   phase     selector  value_a                        value_b                      differs  note
1      2      NULL  NULL      (output)  ::ffff:8.8.8.8: private\n      ::ffff:10.0.0.1: private\n   1        output differs at line 1, column 8
1      2      NULL  NULL      (args)    ["::ffff:8.8.8.8"]             ["::ffff:10.0.0.1"]          1        the runs had different program arguments: values also differ by input
1      2      1     observed  self      IPv6Address('::ffff:808:808')  IPv6Address('::ffff:a00:1')  1        NULL
1      2      1     observed  self._ip  281470816487432                281470849515521              1        NULL
```

`diff` keeps only the rows that differ, in this order:

1. **What the runs did and how they differed**, one row each where it applies: `(status)` and `(output)` when the runs' outcomes or outputs differ; `(edit)` when one run built a candidate edit; `(source)` when two runs that are not candidates ran different source text (another copy of the file); `(line)` when the probes' `--at` lines differ (their values then differ partly because of where they were read; a candidate's `--at` keeps the original's numbering, so `--at 33` is the same line where the candidate's rows show its line 34); `(args)` when they ran with different program arguments; `(sets)` when they made different interventions, each as `[name, value, "LINE@HIT"]`, a `--set` at its `--at` line's hit. Check these rows before crediting an edit with a difference.
2. **Real differences**, in hit order and then in `--capture` order: the first of them is the first divergence.
3. Values that only one run's `--set` changed (`intervention only in run b`). An `after-set` value equal to its run's `observed` value at that hit, in each run that has one, is left out: the intervention did not touch it (`r.buf` when only `r.tail` was set), and the `observed` row shows any difference.
4. Values a view or a note qualifies in either run (`note: qualified (run a: note, run b: note): not evidence by itself`): a C local's leftovers on a `do` body's first line, a moved Rust local's bytes (`stale`). Two runs' leftovers differ with anything that touched the stack before, so such a row is not a divergence of the program; `queries run` shows each note. Whatever else a row says, a qualified value's note says so too, and the row ranks here: `missing in run b; qualified (run a: note)`, `intervention only in run a; qualified (run a: note)`.
5. Values that differ only in heap or stack addresses (`note: addresses only`, for example a C string's pointer; NULL is never treated as an address), one row per selector: its first hit's, saying at how many hits (`addresses only, at 4 hits (one row per selector: its first hit)`), and per stop where both runs label stops (`at 2 hits at stop 1 (one row per selector and stop: its first hit)`). Text inside quotes is the program's data, never an address: `"0x00000001deadbeef"` in a `char[32]` is a real difference.
6. Last, one row per selector that only one run asked for (`only in run a`, with its first hit's value).

The `(output)` row shows up to 200 characters of each output, from 60 before the first character that differs (from the start when it differs within its first 60; `…` marks a cut), and says where (`output differs at line 4, column 13`, counted from 1), so after a `--set` it shows the intervention's effect. Hits that one run never reached say `missing in run b`. A value whose type changed (an edit to `long long`) says `type unsigned int -> long long`.

- **Which runs:** `diff` compares the last two runs unless you name them: `--param a=1 --param b=5`. Comparing needs two saved runs; with fewer, the query says so.
- **Pairing by hit:** `diff` pairs hits by number: where the runs stopped at their line a different number of times, the hits need not line up (a sort's comparator is called on other pairs once one answer differs, so every later hit shows as a difference), and a `note:` line says so and names `queries join` (3.5), which pairs them by a selector's value instead (`--param key=a.name`, or several, `key=p.name,n.name`). A second note says how many rows come after the first hit that differs (`the first difference at a hit is at hit 4; 19 of the 20 rows at a hit come after it`): those may compare different calls. Every row is kept. Where both stopped as often, an edit can still change which call a hit is (eighth evaluation, M6), and the note gives the first sign it finds:
  - **Places:** each hit's function, instance (a closure, a generic's copy) and line in one run must pair with one place in the other at every hit. A function renamed, or lines an edit moved, pair the same way at every hit and count as the same place. Otherwise: `at hit 3 run 2 stopped in f at line 24 and run 3 in g at line 24, but at hit 1 run 2 in f at line 24 paired with run 3 in f at line 24`.
  - **Values at other hits:** a selector's values (addresses and Java object numbers masked, as `no_addresses` does) line up better at other hits than hit by hit: the same values in another order (a calculator's recursive `parseExpr` against a loop: ``runs 1 and 2 saw the same values of `op` in another order (run 2's hit 4 matches run 1's hit 5: -)``), or `[A, B, C]` against `[A, C, D]` (``saw `k`'s values at other hits``). The note says the calls *may* have come in another order or number: a fix that changes which value each moment sees gives the same, so read the rows.
  - **Nothing to tell:** where the hits' values differ, selector by selector (those both runs captured), at more than half of them (at least 2), they cannot show that the hits pair, and the note says so (`the runs' values differ at 4 of 5 hits, so they cannot show that a hit is the same moment in both`).

  Each note names `queries join` with a value that names the call and that the edit leaves alone (an argument, an index), not one the edit changed. No note does not prove the hits line up.
- **What counts:** Only plain values tell. One a view or a note qualifies (a moved Rust local's bytes, a C local's leftovers) is no evidence either way. A value read after a `--set` or `--set-at` in its run on a like name (the same name, a part of it, or what holds it: `lay.width` after `--set-at 75@1 lay.width=16`) may follow from the change, but a name does not prove the same variable (`x` in `helper` is not `main`'s `x`, nor one call's local another's). So such a value never counts toward saying the hits line up, nor toward the count of hits whose values differ. It still counts where it lines up better at other hits, and the note then says that an intervention on a like name may account for it. The same selector read before the change counts as any other, and `after-set` rows never count. Each selector is checked on its own; then:
  - **Another pairing suggested:** where any selector's values line up better at other hits, the note says so, whatever else agrees. Where other values read the same at every hit, it says both, and that the values do not settle it, never that the hits line up: `` `word` reads the same at each of the 8 hits in both runs, but runs 1 and 3 saw `column`'s values at other hits (run 3's hit 6 matches run 1's hit 7: 14): the values do not settle whether a hit is the same moment in both ``. Peeky cannot tell which of them names the call: a value alike by chance (a counter), or one that follows from the edit, reads the same either way.
  - **Most hits differ:** where values differ at more than half the hits (at least 2), the note says so before anything that agrees, and that the values do not show the hits are the same moments: `` `key` reads the same at each of the 2 hits in both runs, but `call` differs at 2 of them: the values do not show that a hit is the same moment in both ``.
  - **No intervention in either run:** where values reading the same at every hit tell the hits apart (each hit reads differently): ``the hits line up by `title`: … and no single selector's values line up better at other hits, so pairing hits by number pairs them as `queries join` by it would``. Where they read alike at some hits: ``the hits line up as far as `word` shows: …; but it reads alike at some hits, which could still trade places``. Either holds only as far as those values name the call. Selectors are checked one at a time, and two that differ could line up better together, so where two or more differ, no note says the hits line up outright: ``the hits line up by `key` as far as each selector alone shows: …; `p` and `q` differ, and how they line up together was not checked``.
  - **After an intervention:** what follows from it is unknown, so the note says only what agrees, what differs and which interventions there were: `` `column` and `word` read the same at each of the 8 hits in both runs, and no single selector's values line up better at other hits; `lay.width` differs; but the interventions (run 2's on `lay.width`) can change other values and which calls reach the line, so this does not show that each hit is the same moment in both ``.
  - **Nothing reads the same at every hit** (or only a constant does): the signs above.
- **Stops:** On a line with several breakpoint locations (#157), where both runs label their stops, a hit pairs only with the other run's hit at the same stop: one at another stop is two rows (`missing in run b (run a: stop 2)`, `missing in run a (run b: stop 1)`), and where the stops part (an edit that changes how often the line stops per pass, or a run saved before stops were labelled), the note says at which hit and names `join`. A `(return)` row pairs by its hit's stop, which it records (schema 15; upgrading a store fills it in for earlier runs from that hit's other rows, where each has one and all agree). Where an earlier run's `(return)` row has none (`--capture return` alone on such a line, or rows of its hit at different stops), nothing says where its call began: it pairs with no other run's row at a stop, and the note says pairing by hit cannot be checked.
- **Other queries:** `queries compare` shows every value side by side with the same notes. Labels are free text, stored as given: `diff` and `compare` name them in a `note:` line (`labels: run 1 is 'mapped-public'; run 2 has no label`), and they never change which runs are compared, nor which run a candidate's `compare:` line names. A run saved before Peeky kept notes (`runs.notes_kept` NULL) may show such values unmarked, and every query showing its values says so in a `note:` line (`notes` in JSON).

### 3.2 Inputs, Outcomes and Outputs of the Last Two Runs

```sh
peeky queries outputs
```

```
run_id  label           args                 sets  status     error_code  output
1       mapped-public   ["::ffff:8.8.8.8"]   []    completed  NULL        ::ffff:8.8.8.8: private\n
2       mapped-private  ["::ffff:10.0.0.1"]  []    completed  NULL        ::ffff:10.0.0.1: private\n
```

Both are labelled private: the public one is the bug. `sets` lists each run's requested changes as `[name, value, "requested at LINE@HIT"]`: a `--set`'s at its `--at` line's `--set-hit` (`["gap", "20", "requested at 34@4"]`), a `--set-at`'s at its own. One whose hit never came ends `, not made: that hit never came`, as the probe warned.

### 3.3 What My Intervention Changed

```sh
peeky probe examples/pagination_bug.py --at 8 --capture start --set start=0
peeky queries set_effect
```

```
hit  line  selector  observed  after_set  caveat_observed  caveat_after_set  interventions
1    8     start     3         0          NULL             NULL              start: 3 -> 0
```

`caveat_observed` and `caveat_after_set` name a view or `note` that qualifies either value, as `hits` does. Rows come in the order the changes were made. A change at a stop where nothing was captured (a `--set-at` line, 2.8) is one row: what it changed, before and after.

Pass `--param a=<run> --param b=<run>` to `diff`, `compare`, `outputs` and `join` to pick specific runs.

### 3.4 One Row per Hit

**What it answers:** What were all the captured values at each hit, side by side?

`bins.c` loops over its arguments, and line 9 reads `total += v * bins[v % 4].hi;`:

```sh
peeky probe bins --at bins.c:9 --capture 'v,total,bins[3].hi' --hits all -- 2 7 7 8
peeky queries wide                  # the newest run; --param run=3 for another
```

```
hit  phase     line  v  total  bins[3].hi  no_value  caveats  sets
1    observed  9     2  0      4           NULL      NULL     NULL
2    observed  9     7  6      4           NULL      NULL     NULL
3    observed  9     7  34     4           NULL      NULL     NULL
4    observed  9     8  62     4           NULL      NULL     NULL
```

- **Columns:** one per selector, named exactly as captured, in `--capture` order.
  - A selector named like a fixed column (`line`) becomes `line (selector)`.
  - SQLite ignores case in column names, so a selector that differs from an earlier column only by case gets a number: `--capture x,X` gives `x` and `X (2)`.
  - In your own SQL, quote such names with double quotes: `"bins[3].hi"`, `"X (2)"`.
- **Cells** are the values exactly as stored (text; use `num` in `captures` to compare numbers).
- **NULL is never a value.** `no_value` is a JSON object naming each cell with no value (optimized out, withheld, an error) and why: `{"name":"no value yet: declared at line 7, below this stop, …"}`. A selector not captured at that hit is NULL and not named.
- **`caveats`** is a JSON object naming each cell whose value is qualified. Read such a cell with its caveat, never as a plain value.
  - `stale`, `unproven`, `opaque` or `budget-exhausted`: the row's view (`peeky sql --schema` explains `view`). `stale` is a moved or dropped Rust local's leftover bytes, whatever view decoded them.
  - `note`: the probe marked the row `[note]` (for example a C local declared on a `do` body's first line: `{"k":"note"}`, "may be the last pass's value, or leftovers"), or gave it one of the Rust notes among its warnings. `peeky queries run` shows each row's note.
- **Phases:** a run with `--set` has an `after-set` row after its hit's `observed` row. `--param phase=observed` (or `after-set`) keeps one.
- **`sets`** lists the changes made since the previous row's values were read (a JSON list, `["colon: 7 -> 6 (line 22, hit 3)"]`): on an `after-set` row, those made at that stop; on an `observed` row, those made at other lines' stops after the previous hit (`--set-at`, 2.8). A change after the last hit precedes no row; `queries set_effect` lists them all.
- **`--json`** also gives the SQL it ran (`sql`): refine it with `peeky sql`, e.g. `SELECT * FROM (<sql>) WHERE "v" = '7'`. Up to 94 selectors fit (100 columns at most); a wider run is refused.

### 3.5 Joining Two Runs by a Value

**What it answers:** For the same value of `v`, what did each run see? `diff` pairs runs by hit number, which says little when the inputs come in a different order, or when the runs took different paths (a sort's comparator: `--param key=a.name` pairs its calls on the same element). Where no one selector names a call, several do together: `--param key=a.name,b.name` pairs hits where each has the same value.

```sh
peeky probe bins --at bins.c:9 --capture 'v,total,bins[3].hi' --hits all -- 7 2 5
peeky probe bins --at bins.c:9 --capture 'v,total,bins[3].hi' --hits all -- 2 7 7 8
peeky queries join --param key=v    # the last two runs; --param a=3 --param b=4 for others
```

```
v  hit_a  hit_b  a:total  a:bins[3].hi  b:total  b:bins[3].hi  no_value_a  no_value_b  caveats_a  caveats_b  matched
7  1      2      0        4             6        4             NULL        NULL        NULL       NULL       both
7  1      3      0        4             34       4             NULL        NULL        NULL       NULL       both
2  2      1      28       4             0        4             NULL        NULL        NULL       NULL       both
5  3      NULL   34       4             NULL     NULL          NULL        NULL        NULL       NULL       only in run a
8  NULL   4      NULL     NULL          62       4             NULL        NULL        NULL       NULL       only in run b
```

- **Matching** is by the key's stored text, exactly. It joins the `observed` rows unless you pass `--param phase=after-set`.
- **Addresses:** a key whose values carry one (a pointer, a C string `0x16fbcbb1e "apple"`) rarely matches, as addresses differ between runs. The result then says so in a `note:` line (`notes` in JSON). `--param masked=1` matches the keys with addresses masked (`no_addresses`), showing each run's own value as `a:<key>` and `b:<key>`. Masked, any address matches any other, so a bare pointer pairs with every other bare pointer; NULL is not masked, and neither is text inside quotes (`"0x00000001deadbeef"` in a `char[32]` is data).
- **A repeated key pairs every hit:** a value at 2 hits of run a and 3 of run b gives 6 rows, in run a's hit order and then run b's. Here run b saw `7` twice, so run a's hit 1 appears twice. A `note:` line says so, with the first such value and how many rows they make (``the key repeats: run 2's hits 2 and 3 both have `v` 7, …``): add a selector that tells those hits apart (`key=v,i`).
- **Several selectors:** `key=a,b` (commas outside brackets and quotes separate them: `m["a,b"]` is one) pairs hits where every one of them has the same value, with a column for each; a hit missing any of them is unmatched. Up to 8.
- **Unmatched hits** come last, one row each: `only in run a` (or `b`) when the other run never had that key value, or when the hit has no key value (the key cell is NULL, and `no_value_a` says why), or a key value a view or a note qualifies (the key cell is NULL, and `caveats_a` says why): such a value pairs nothing.
- **Columns:** the key (each of its selectors), each run's hit, then each run's other selectors as `a:<selector>` and `b:<selector>` (numbered as in `wide` where only case differs), and `no_value`/`caveats` per run as in `wide`. The key's own caveat is among them, on an unmatched hit: no pair is made on a value Peeky cannot vouch for. Both runs must have captured the key.
- **`--json`** gives the SQL too (`sql`).

---

## 4. SQL over Saved Runs

The store is SQLite. Queries are read-only and bounded, and they need no rerun.

### 4.1 What Is in the Store?

```sh
peeky sql --schema          # tables, columns, views, canned queries, examples
peeky queries               # canned questions: runs, run, hits, compare, diff, outputs, set_effect, values, wide, join, unreadable, failures
```

| Table or view | One row per |
| --- | --- |
| `runs` | probe run: inputs, interventions, status, output, build identity |
| `captures` | selector × hit × phase; `num` holds the value as a number; `view` and `note` qualify a value (read it with them) |
| `interventions`, `pauses` | applied `--set`, pause per hit |
| `evidence` (view) | captures joined with their run (with its `language`): no JOIN needed; output is `runs.output` |
| `last_run` (view) | the evidence rows of the newest run |

**Functions.** `no_addresses(value)` masks heap and stack addresses (never NULL, short hex or quoted text) to compare runs in your own SQL; `no_addresses(value, language)` (the `evidence` view has `language`; the program's output is `runs.output`) also masks Java object numbers in Java runs. `object_ref(value)` is the Java object a value is (`Balance$Worker@100`): equal within one run means the same object (`object_ref(b.value) = object_ref(w.value)` tells a copy from the list's own element; [Java 4.6](lang/java-cookbook.md#46-same-object-or-a-copy-java-python)); Python's is the `identity` column.

### 4.2 The Newest Run

```sh
peeky sql "SELECT hit, selector, value FROM last_run"
```

### 4.3 Numeric Filters

**What it answers:** On which hits was a value out of range?

```sh
peeky sql "SELECT hit, value, num FROM evidence WHERE run_id = 3 AND selector = 'value' AND num < -1"
```

```
hit  value  num
1    -3     -3.0
3    -2     -2.0
```

`value` is the debugger's text; `num` is that value as a number, and NULL when it isn't one. An integer that fits 64 bits is exact (`600`, not `600.0`); a larger one, such as a `u64` near its maximum, is approximate in `num`, so compare those through `value`. As in any SQL, integer division truncates (`num * 1.0 / 1000` keeps the fraction) and `SUM` of integers fails past 64 bits (`TOTAL(num)` returns a REAL). Older stores are upgraded in place, with `num` recomputed from `value`.

### 4.4 Values Seen Across All Runs

```sh
peeky queries values --param selector=start   # qualified: how many sightings a view or note qualifies
peeky sql "SELECT program, COUNT(*) AS runs, SUM(status = 'completed') AS completed FROM runs GROUP BY program"
```

```
program            runs  completed
ipaddress39.py     2     2
pagination_bug.py  2     2
program.py         1     1
```

### 4.5 Parameters and Export

```sh
peeky sql "SELECT * FROM evidence WHERE run_id = :run" --param run=3 --json > run3.json
sqlite3 -readonly -csv -header ~/.peeky/evidence/evidence.db "SELECT * FROM evidence" > evidence.csv
```

Limits: one `SELECT` per call, 200 rows by default (`--limit` up to 10000), 10 s, 16 MB of JSON. The newest 500 runs are kept. Writes, `ATTACH` and settings are refused (`INVALID_QUERY`).

---

### 4.6 Same Object, or a Copy? (Java, Python)

**What it answers:** Is this variable the list's own element, or a copy whose changes go nowhere?

Java compares objects with `object_ref` ([Java 4.6](lang/java-cookbook.md#46-same-object-or-a-copy-java-python)); Python with the `identity` column ([Python 4.6](lang/python-cookbook.md#46-same-object-or-a-copy-java-python)).

### 4.7 C++ References and Pointers in SQL

**What it answers:** Which arguments did `trim_left` change? A `const std::string &` (or a pointer) is saved as the debugger shows it, its address first: `0x000000016d78b298 "apple"`. It never equals a plain string's `"apple"`, so take the text after the address before comparing.

The recipe is in [C and C++ 4.7](lang/c-cpp-cookbook.md#47-c-references-and-pointers-in-sql).

---

## 5. Step-by-Step Sessions

Use a session when you want several looks at one stop. Every command is a separate shell call; a private worker keeps the program paused in between.

### 5.1 Stop, Look, Change, Finish

**Common cases**

- `session start` stops once; `inspect` lists the locals, `get` reads values and lists their children, `set` changes one, and `close` ends the session (always close it).
- Pass `--name` to every later command when the session is not `default`.
- Session states and `inspect` name the stopped function (`in Ring::push(int)`, `in topk::insert_sorted::{{closure}}`).
- `get` of a name that is not there prints why and names the stop; any selector with no value makes the exit status 1, and a last line on stderr names them (`peeky: no value for nosuch (each row above says why), so the exit status is 1`).
- `--frame N` reads caller frame N; a caller's values cannot be changed.

```sh
peeky session start examples/pagination_bug.py --at 8
peeky session inspect
peeky session get start 'items[0]' page
peeky session get items
peeky session set start=0
peeky session continue --to-exit
peeky session report
peeky session close
```

```
session default (python): state: stopped at .../pagination_bug.py:8 in get_page, hit 1 (paused 0.0 s)
stopped at .../pagination_bug.py:8 in get_page, hit 1 (paused 0.06 s)
[Locals]
  items: list = [10, 20, 30, 40, 50, 60]  [id 6] (children: peeky session get items)
  page: int = 1
  page_size: int = 3
  start: int = 3
start: int = 3
items[0]: int = 10
page: int = 1
items: list = [10, 20, 30, 40, 50, 60]  [id 6]
  items[0]: int = 10
  items[1]: int = 20
  items[2]: int = 30
  items[3]: int = 40
  items[4]: int = 50
  items[5]: int = 60
  len() [not selectable]: int = 6
set start: 3 -> 0
state: terminated (program exited; `peeky session report` shows output)
state: terminated
output:
PAGE: [10, 20, 30]
closed default
```

- **Scope headers:** Scope headers are the debugger's own names: `[Locals]` for Python, `[Local]` for Java.
- **Long scopes:** A scope lists at most 50 entries (debugpy's groups count) and says how many more it has; `session get NAME` reads one past them by name.
- **`session get`:** `session get` on a list, dict or struct lists one level of children, each with the selector that reads it, and `(children: ...)` on a child that has children of its own. A name that is not there prints only why (`x: <not found; visible: ...>`), and a last line names the stop it was read at (`note: this stop is in main at sched.cpp:30`: a comparator's `a` is not `main`'s); JSON gives such a row the stop's `location`. If any selector fails, every row is still printed and the exit status is 1.
- **`continue`:** `session continue` without `--to-exit` runs to the next stop at the session's breakpoint.

**Caller frames.** `--frame N` reads frame N of the stack, as `session inspect --json` lists `frames` (0, the default, is where the program stopped; 1 its caller; at most 10):

```sh
peeky session start examples/corpus/mutable_default/program.py --at 3
peeky session inspect --frame 1
peeky session get --frame 1 add_tag.__defaults__
peeky session inspect --frame 2
```

```
stopped at .../program.py:3 in add_tag, hit 1 (paused 0.1 s)
frame 1: <module> (.../program.py:7)
[Locals]
  (special variables: debugpy's group; select its members by name)
  (function variables: debugpy's group; select its members by name)
frame 1: <module> (.../program.py:7)
add_tag.__defaults__: tuple = (['a'],)  [id 24]
  add_tag.__defaults__[0]: list = ['a']  [id 79] (children: peeky session get 'add_tag.__defaults__[0]' --frame 1)
  len() [not selectable]: int = 1
peeky: [INVALID_REQUEST] Frame 2 is out of range: this stop lists 2 frame(s), 0 to 1 (0 is the innermost; at most 10 are listed)
```

- **Caller frames:** A frame past the stack is `INVALID_REQUEST`, naming how many frames there are. Rust's owned-local and Box proofs hold only in the innermost frame, so a caller's `Vec` keeps its raw fields with a note.
- **Changes:** A caller's values cannot be changed (`MUTATION_REJECTED`), so caller listings never say `[settable]`.

**Per language:** [Python 5.1](lang/python-cookbook.md#51-stop-look-change-finish) (globals, a name missing from the locals, a pass's own value, module level), [C and C++ 5.1](lang/c-cpp-cookbook.md#51-stop-look-change-finish) (a caller's local still being initialized).

### 5.2 Stepping and Running to a Line

**What it answers:** What happens line by line from here, inside a call, or a few lines further on?

**Common cases**

- `step` runs to the next line, `step --into` into a call, `step --out` until the function returns; `continue` runs to the next stop, `--to LINE` once to a line, `--to-exit` to the end.
- **`continue --to LINE` does not skip the session's breakpoint:** when a stop there comes first, the state says so and the exit status is 3; repeat it.
- A step can land on a closing `}`, a loop header, or back on an `if`'s line (1.1); read a block's locals before stepping past it.
- A step that leaves your sources says `outside the program's sources`: `step --out` or `continue`.

```sh
peeky session start examples/pagination_bug.py --at 13
peeky session step --into          # into the call on this line
peeky session step                 # the next line (over calls)
peeky session step
peeky session get start
peeky session step --out           # until this function returns
peeky session continue --to-exit
```

```
session default (python): state: stopped at .../pagination_bug.py:13 in <module> (paused 0.0 s)
state: stopped at .../pagination_bug.py:5 in get_page (paused 0.0 s)
state: stopped at .../pagination_bug.py:7 in get_page (paused 0.0 s)
state: stopped at .../pagination_bug.py:8 in get_page (paused 0.0 s)
start: int = 3
state: stopped at .../pagination_bug.py:13 in <module> (paused 0.0 s)
state: terminated (program exited; `peeky session report` shows output)
```

- **Hit numbers:** State lines number the stops at the session's breakpoint (`…:8 in get_page, hit 3`), and say which of the line's breakpoint locations a stop is at where it has several in the function (`hit 4, stop 1 of 3`; see `--hits` in 2.2). `session start --hit 5` runs on to the 5th stop, and `session continue --count 3` to the 3rd breakpoint stop from here, each within one `--timeout`. They never run past anything else: a crash, a finished step or the program's exit ends them there, with a note saying why and the last hit reached, and exit status 3 (as when `--timeout` ends them first).
- **`continue --to`:** `peeky session continue --to 9` stops once at line 9 of the session's file (the extra breakpoint is removed at the next stop), then the session's own breakpoint applies again. If another stop comes first, such as the session's own breakpoint in a loop, the state says so and the exit status is 3 (`note: stopped at the session's breakpoint (line 30, hit 2) before reaching line 17; `session continue --to 17` again`); so does a program that exits first (`the program exited without reaching line 17`). There is no option to skip the session's breakpoint: repeating `--to` reaches the line with every stop counted. `continue --to` refuses a line the debugger would move (no code there) and keeps the stop.
- **Leaving your sources:** A step can leave your sources, for example into a library, into Rust's standard library, or past the end of `main`. The state then says `outside the program's sources`, and nothing there is read or changed, except what your function returned when the step left it (2.9). In C, C++ and Rust, `step --out` leaves one frame at a time, also in a library. While your code is still on the stack, `step --out` returns to it; past `main`, the state says so and `continue` finishes.
- **The session's breakpoint during a step:** A step whose code reaches the session's breakpoint first (a recursive call on the line, or the rest of the function under `step --out`) stops there, and the state says so (`hit 2` and `note: the session's breakpoint (hit 2) stopped the step before it ended, and the step is over`); `step --out` then shows no return, since the function has not returned.
- **Threads:** When other threads are stopped at the breakpoint too (LLDB stops them together; Java stops each one separately), `step` waits until `continue` has served them. A step that a breakpoint (or a crash) stops first is over: in C, C++ and Rust, the next `continue` runs to the next breakpoint, not to where the step would have ended. When another thread's stop catches the stepping thread mid-step (LLDB may report it as a step's end anywhere it was, inside `usleep`), the state says `the step was interrupted here` and `continue` serves that thread's stop.

**Per language:** [Java 5.2](lang/java-cookbook.md#52-stepping-and-running-to-a-line) (`--into` and `--out` skip JDK classes).

### 5.3 Several Sessions at Once

```sh
peeky session start app.py --at 12 --name a
peeky session start app.py --at 40 --name b
peeky session get total --name b
peeky session list
```

A second `session start` with a name that is already taken fails with "exists". It never takes over the other session.

Every later command needs the same `--name`, `inspect`, `get`, `set`, `step`, `continue`, `report` and `close` alike: without it, a command acts on the session named `default` (or says there is none). `peeky session list` shows the names in use. With one session at a time, leave `--name` out everywhere.

### 5.4 Programs with Timeouts

Tests with timeouts, heartbeats and RPC deadlines keep their clocks running while the program is paused. Prefer probes, which pause for milliseconds per hit, or give sessions a pause budget:

```sh
peeky session start app.py --at 12 --max-pause 5      # auto-resume after 5 s
peeky session inspect                                   # shows paused_s and resume_in_s
```

A breakpoint or step stop older than the budget is resumed; your next call says so (`STALE_STOP`). Crash and exception stops are never auto-resumed.

### 5.5 Cleaning Up

A session's worker ends with `session close`, when its start fails or is interrupted, and after 5 idle minutes. `peeky session close --forget` drops a record whose worker is gone for good.

---

## 6. Verifying a Fix

### 6.1 Check One Edit on a Fresh Build

**What it answers:** Does this change pass the independent checks, on a clean copy?

```sh
peeky verify examples/pagination_bug.py --check examples/verify_pagination.py \
  --old 'start = page * page_size' --new 'start = (page - 1) * page_size'
```

```
passed: True  stage: verification
checker exit: 0
output:
PASS: 12 independent pagination checks

diff:
-    start = page * page_size
+    start = (page - 1) * page_size
```

Your files are not changed. The edit is applied to a scratch copy; `--keep` keeps the edited file (build artifacts are always removed) in the system's temporary directory, at the `kept:` path the result prints; delete it when you are done. To see what an edit does before verifying, a candidate probe (6.6) is simpler. For a Cargo crate, its compiled tests run too, and a failure names each failed test with how it failed (`compiled tests: failed (1 failed)`, then `first_page_starts_at_zero: thread … panicked at tests/pagination.rs:10:5: assertion `left == right` failed …`). `--json` identifies the checker's descriptor and build by hash (`descriptor_sha256`, `artifact_manifest_id`); `--details` adds both in full.

**Does the checker catch the bug?** Before trusting `passed: True`, run the checker against the unedited program with `--baseline` (no `--old`/`--new`). It builds and checks the program as it is, like any verify, and should fail:

```sh
peeky verify examples/pagination_bug.py --check examples/verify_pagination.py --baseline
```

```
passed: False  stage: verification
reason: AssertionError: (1, 3, [10, 20, 30], [40, 50, 60])
checker exit: 1
baseline: the unedited program, no edit (the checker rejects it, as it should reject the bug)
```

A checker that passes here cannot tell a fix from the bug. `reason` is the checker's exception, or, for a checker that only exited nonzero (`print("FAIL ..."); sys.exit(1)`), the first output line that starts like a failure (`FAIL`, `FAILED`, pytest's `E`, `…Error:`, `…Exception:`, `assert`; unittest's `FAIL:` header with the exception it reports, not its `FAILED (failures=1)`), with up to two indented lines after it (`reason: FAIL ['+1', '-']: got 'POPPED:' want 'POPPED:1'`; `FAIL first case; expected 3; got 2`), else the last line (`mismatch: wrap got 6 want 7`); from stdout, or stderr when stdout is empty, with `(SystemExit: N)` added when N is not the `checker exit` status; `checker.error` in JSON keeps `SystemExit: 1`. An `--old`/`--new` pair that changes nothing is refused (`--new equals --old`); `--baseline` is the way to check the original.

### 6.2 An Overfit Fix Is Rejected

```sh
peeky verify examples/pagination_bug.py --check examples/verify_pagination.py \
  --old 'start = page * page_size' --new 'start = 0 if page == 1 else page * page_size'
```

```
passed: False  stage: verification
reason: AssertionError: (2, 3, [40, 50, 60], [70])
checker exit: 1
```

### 6.3 Multi-Line Edits

```sh
peeky verify app/ --at lib/parse.py --check checks/parse_check.py --old-file old.txt --new-file new.txt
```

`--old` must match the source exactly once; the error says how many times it matched, and where. Files are read byte for byte, whereas shell `$(cat file)` drops trailing newlines. On the command line, pass real newlines for multi-line text (bash: `--old $'a\n    b'`).

### 6.4 Several Edits at Once

A general fix often touches more than one place: a new field and the function that uses it. Repeat the pair, once per edit:

```sh
peeky verify bucket.c --check check.py \
  --old 'long tokens;' --new 'long tokens;
  long milli;' \
  --old 'long refill = elapsed / 1000 * b->rate;' --new 'long refill = (b->milli + elapsed * b->rate) / 1000;'
```

Each `--old` quotes the original file and must match it exactly once; the edits must not overlap, and their order does not matter. The `-file` forms repeat the same way (`--old-file a.txt --new-file b.txt --old-file c.txt --new-file d.txt`). Up to 32 edits; an error names the edit by its number (`(edit 2)`). Candidate probes take the same pairs, and `--at` keeps the original's numbering, mapped through every edit.

### 6.5 Writing a Checker

A checker is a trusted script, run as `python checker.py <candidate-path>`. It exits 0 only when the behavior is right. Test the general rule, not the one input from the bug report (see `examples/verify_pagination.py`). For C, C++, Rust and Java its argument is the path of a JSON descriptor of the fresh build: run each case as `[*spec["command"], *case_arguments]` with `env=spec["environment"]` (`spec["language"]` names the language; see `examples/c/verify_pagination.py`; optional background: [NATIVE_AGENT.md](NATIVE_AGENT.md)). For Rust, the crate's integration test, when it has one, must pass too.

### 6.6 See What a Fix Does Before Making It

**What it answers:** With this edit, what would the program compute at the same line, and what would it print?

**Common cases**

- Run the same probe twice, once as it is and once with `--old`/`--new`: the second builds a fresh copy with the edit; your files are not changed.
- `--at` keeps the original's line numbers; a line inside an edit that adds or removes lines is refused.
- The candidate's `compare:` line names the `queries diff` against the same probe without the edit when such a run is saved; otherwise it says how to make one.
- It is evidence, not verification: `verify` checks the fix.

Run the same probe twice: once as it is, once with `--old`/`--new`. The second builds a fresh copy with the edit; your files are not changed.

```sh
peeky probe examples/c/pagination_bug.c --at 25 --capture start --arg 2
peeky probe examples/c/pagination_bug.c --at 25 --capture start --arg 2 \
  --old 'int start = page * page_size;' --new 'int start = (page - 1) * page_size;'
peeky queries diff
```

```
hit  phase     line  function  selector  type  value
1    observed  25    main      start     int   3
status: completed  hits: 1  seconds: 0.665  max pause: 13.7 ms
output:
  PAGE:[40, 50, 60]
candidate: values are from a fresh build with this edit; your files are unchanged
--- pagination_bug.c
+++ pagination_bug.c
@@ -21,7 +21,7 @@
...
-    int start = page * page_size;
+    int start = (page - 1) * page_size;
     int end = start + page_size; /* OBSERVE */

run_a  run_b  hit   phase     selector  value_a           value_b               differs  note
1      2      NULL  NULL      (output)  PAGE:[70, 80]\n  PAGE:[40, 50, 60]\n  1        output differs at line 1, column 7
1      2      NULL  NULL      (edit)    none              candidate             1        the runs built different edits (runs.edit has each diff)
1      2      1     observed  start     6                 3                     1        NULL
```

- **Lines:** `--at` uses the original file's numbering, so the same command works before and after. A line after an edit that adds or removes lines is moved to match (`line 25 is line 27 of the candidate`). Some lines have no faithful match and are refused before anything is built:
  - a line inside such an edit;
  - the line right after an edit whose `--old` ends with a newline but whose `--new` does not, because the candidate joins that line onto the edit.
- **Build errors:** an edit that does not compile fails with `BUILD_FAILED` and the compiler's message. A Python edit with a syntax error fails with `NO_STOP`, with the `SyntaxError` in the output.
- **Evidence:**
  - The run's `edit` column holds the diff, and its `source_sha256` is the edited text's.
  - `queries runs` marks the run `(candidate)`, and the `evidence` and `last_run` views have a `candidate` column. Captured `line`s are in the candidate's own numbering.
  - `queries values` leaves candidate runs out, so they never mix with values the real program produced.
  - `queries diff` pairs values by hit and selector, so a candidate whose lines moved still compares.
  - A plain `queries diff` compares the newest run with the one before it, whatever that was. So after a candidate probe a `compare:` line names the newest saved run that is the same probe without the edit (the same target, `--at`, `--capture` and program arguments; the same build, by its target identity: compiler options, optimization, sources; every source the build read as it was when the edit was made, headers, `Cargo.toml` and `Cargo.lock` too, and in Python every `.py` file under the target; no edit, no `--set` or `--set-at`; a `--label` does not count): `compare: peeky queries diff --param a=1 --param b=2 (run 1: this probe without the edit)`, or says how to make one (after a candidate with `--set` or `--set-at`, run it without them too) (`evidence.compare` in JSON).
  - A run whose hits are at other breakpoint locations of the line (#157: `stop` differs at a hit both reached, as when the edit changes how often the line stops per pass) is not named: the line says so and names `queries join`, which pairs by value. Runs saved before Peeky recorded what they read are never named, nor are any where it cannot tell (a Python tree past 4096 `.py` files or 16 MB): run the probe without the edit then.
- **Not a verification:** this is evidence about one run. Only `peeky verify` checks a fix.
- **Intervening where no local can be set:** in Java, Rust, C and C++, `--set` changes a scalar field or element directly (`--set s.end=10` where `s` is a struct local; see [C and C++ 7.1](lang/c-cpp-cookbook.md#71-c-and-c) and [Rust 7.2](lang/rust-cookbook.md#72-rust-a-cargo-crate) for which ones). `--set` also works in the same probe as `--old`/`--new`. Where the value you want to change cannot be set (a Python field, or one reached through a pointer, a reference or a `Box`, as `last.end` is when `last` is a `&mut Span`), add a local holding it with the edit, on an earlier line (a local is visible only after its declaration), and set that local. In Rust, at the line `last.end = s.end;` that follows `if s.start <= last.end {`:

  ```sh
  peeky probe spans --at src/main.rs:23 --capture end,last.end --arg 1-10,2-3 \
    --old 'if s.start <= last.end {' \
    --new 'let mut end = s.end; std::hint::black_box(&mut end); if s.start <= last.end {' \
    --old 'last.end = s.end;' --new 'last.end = end;' --set end=10
  ```

  This reports `set end: 3 -> 10`. `black_box` keeps the new Rust local in memory, where a change reaches the program.

**Per language:** [Rust 6.6](lang/rust-cookbook.md#66-see-what-a-fix-does-before-making-it) (a local kept in a register), [Python 6.6](lang/python-cookbook.md#66-see-what-a-fix-does-before-making-it) (what a Python candidate's copy holds).

---

## 7. C, C++, Rust and Java

The commands are the same for every language; only the target changes. Each language's own sections are in its cookbook.

### 7.1 C and C++

In [C and C++ 7.1](lang/c-cpp-cookbook.md#71-c-and-c): read, set, withheld locals, strings and changing their text, maps, what `set` checks.

### 7.2 Rust (a Cargo crate)

In [Rust 7.2](lang/rust-cookbook.md#72-rust-a-cargo-crate): values read from memory, settable locals and texts, crate shape.

### 7.3 Java (launched)

In [Java 7.3](lang/java-cookbook.md#73-java-launched): configs, previews, fields and Strings that can be set.

### 7.4 Java Tests Already Running (Gradle `--debug-jvm`, e.g. Venice)

In [Java 7.4](lang/java-cookbook.md#74-java-tests-already-running-gradle---debug-jvm-eg-venice): attach to a `--debug-jvm` JVM.

### 7.5 Optimized Builds

Set `"optimization": "O2"` (C/C++) or `"opt_level": "2"` (Rust) in a `--config` to debug the optimized program. Locals may then be `unavailable`, shown values can be stale, and changes are refused (a probe's `--set` before the program runs, saying where the debug information keeps the local only in registers); every result carries `optimized` and a warning.

---

## 8. Slow Programs and Tool Timeouts

```sh
peeky probe examples/pagination_bug.py --at 8 --capture start --background
# job 3f2c... started; collect it with `peeky job wait 3f2c...`
peeky job wait 3f2c... --timeout 20     # exit 3 while still running; then the exact output
peeky job list
```

`--background` works for `probe` and `verify`. Other bounds:
- `--timeout S`: per probe (default 60 s, at most 300);
- a `session` command's `--timeout S` (`session start` included): how long it waits for the program (default 30 s, at most 300); a program still running is left running, and `session wait` waits again;
- `--hits N`: at most 100 (`--hits all` captures every stop up to 100);
- `--until hits`: skips waiting for the program to exit.

---

## 9. A Program on Another Host

Run the worker next to the program and reach it through SSH:

```sh
# on the host
export DEBUG_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
python3 -m peeky.worker --root /srv/app --port 8765

# on your machine (same sources)
ssh -N -o ExitOnForwardFailure=yes -L 8765:127.0.0.1:8765 you@host &
export PEEKY_WORKER_TOKEN='<the token>'
peeky probe ./app/job.py --at 42 --capture rows --worker http://127.0.0.1:8765
peeky session start ./app/job.py --at 42 --worker http://127.0.0.1:8765
```

If the host's source differs from yours, the worker refuses before running anything (`PROVENANCE_CHANGED`). A dropped tunnel gives `UNREACHABLE`; reconnect and continue, because the worker keeps the session. To try this without a second machine, `PEEKY_E2E_DOCKER=1 pytest tests/remote -v` runs it against a Linux container (needs Docker or OrbStack). The recorded run is in [REMOTE.md](REMOTE.md).

---

## 10. When Something Goes Wrong

Errors carry a stable `code`, a `hint`, and whether retrying can help (`--json` shows all fields).

| You see | Meaning | Do this |
| --- | --- | --- |
| `NO_STOP` | The line never ran (e.g. `--at 6`, the `raise` that valid input never reaches), or the program failed before it (the message gives the exit code when the debugger reports one, and the output says why) | Pick a line the run executes; `describe` lists lines with code, not lines that run. After a failure, check the program's arguments: everything after `--` goes to the program, and the hint names any of Peeky's own options found there |
| `warning: The debugger moved the breakpoint ...` | The line has no code of its own; values come from the line named in the warning | Use a line from `describe` |
| `<not found; visible: items, page, ...>` in a row | The name is not visible at that stop, or is not assigned yet (a breakpoint stops before its line) | Use a listed name, or stop one line later |
| `MUTATION_REJECTED` | Not a literal (`--set start=min(1, 2)`), wrong type or range, or a Rust local held in a register | Use a literal of the right type, or edit the source and `verify` |
| `INVALID_BREAKPOINT` | Conditions and logpoints are refused (they run code), or a Java line has no code | Unconditional lines only; probe with `--hits` and filter in SQL |
| `PROVENANCE_CHANGED` | Sources, build outputs, tools or headers changed during the session | Start again on a fresh build |
| `STALE_STOP` | The program moved on, possibly auto-resumed by `--max-pause` | `session wait`, then `inspect` |
| `NO_SESSION` | No session under that name, or its start was interrupted | `session start` again (it takes the name over) |
| `TIMEOUT` / `DEBUGGER_ERROR` | A bound was hit, or the debugger failed; the session is closed | Start again; never repeat a `set` blindly |
| `UNREACHABLE` / `TRANSPORT_LOST` | The worker or the SSH tunnel is down / broke mid-request | Restore the tunnel; after `TRANSPORT_LOST`, `inspect` before anything else |
| `INVALID_QUERY` | Bad SQL, or a write | `peeky sql --schema` |
| A path you did not type (`/private/tmp/case/app.py` for `/tmp/case/app.py`) | Peeky prints real paths: on macOS `/tmp` is a link to `/private/tmp`, so both name the same file | Match files by the part after `/tmp` |

All codes are listed in [ERRORS.md](ERRORS.md). If you suspect a filesystem with unreliable timestamps, `PEEKY_NO_DIGEST_CACHE=1` rehashes every file on every check.

---

## 11. From pdb / gdb / lldb to Peeky

| You would type | Peeky | Notes |
| --- | --- | --- |
| `break file:12`, `run`, `print x` | `peeky probe target --at file:12 --capture x` | One command, one complete run, nothing left running |
| `print obj.field`, `print a[0]`, `print d['k']` | `--capture "obj.field,a[0],d['k']"` | Paths, indexes and string keys only; no expressions. Quote selectors with `[...]` (zsh globs them) |
| `print m[1]` (a C++ `std::map`) | `--capture "m[1],m[#0].first"` | `m[1]` is the value for key 1, as in C++; `m[#0]` is the first entry in iteration order ([C and C++ 7.1](lang/c-cpp-cookbook.md#read-a-c-map-or-set-by-key)) |
| `set var x = 0`, `continue` | `--set x=0` | Applied once, at `--set-hit N`; the output shows the effect |
| `ignore 1 9`, `continue` ×10 | `--hits 10` (or `--hits all`), then `peeky queries hits` | Every hit is captured and saved |
| `commands 1 / silent / print x / end` | `--hits N --capture x` | Logging without breaking into the run |
| `condition 1 x > 5` | `--hits N`, then `peeky sql "... WHERE num > 5"` | Conditions would run code in the program |
| `info locals`, `print *obj` | `peeky session inspect`, `peeky session get obj` | `get` lists one level of children with their selectors |
| `bt` | `peeky session inspect --json \| jq '.frames'` | The stack at the stop; values come from the top frame |
| `frame 1`, `up` | `peeky session inspect --frame 1`, `peeky session get --frame 1 x` | Reads a caller's locals (frame N as `frames` lists them) |
| `next`, `step`, `finish` | `peeky session step`, `step --into`, `step --out` | Java `--into`/`--out` skip JDK code. After `step --out`, C, C++, Rust and Java `session inspect` shows what the function returned under `[Returned]` (also when the step stopped in a library), and `session get return` reads it; withheld unless exact (2.9). Python: not available |
| `finish` at every call (`Run till exit ... Value returned is`) | `peeky probe target --at <function's first line> --capture return --hits all` | One `(return)` row per call, recursion and library callers included (2.9) |
| `until 42`, `advance 42` | `peeky session continue --to 42` | One-shot; the session's own breakpoint stays, and stopping there first says so (exit 3) |
| `continue` | `peeky session continue [--to-exit]` | Runs to the next stop or to exit |
| "rerun with another input and compare by eye" | `--arg ... --label A`, then `peeky queries diff` | The `(output)`, `(args)`, ... rows come first, then the first divergence |
| "I think this fixes it" | `peeky verify --old ... --new ...` | Fresh build plus an independent checker |
