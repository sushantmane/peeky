# Peeky Cookbook

Recipes for debugging running programs from your terminal. Agents use the same commands: see
[AGENT_GUIDE.md](AGENT_GUIDE.md).

- **Real output:** the output shown is real, recorded on macOS with Python 3.13, from 2026-10-16
  on.
- **Copy and paste:** run the commands from the repository root. A recipe that uses a small program
  of its own shows that program.
- **Languages:** this book covers what every language shares. Each language has a one-page summary
  of its rules, and a cookbook for what is particular to it. A language cookbook's sections have
  the numbers of the sections here that they add to.

| Language | Its rules | Its cookbook |
| --- | --- | --- |
| Python | [python.md](lang/python.md) | [python-cookbook.md](lang/python-cookbook.md) |
| Java | [java.md](lang/java.md) | [java-cookbook.md](lang/java-cookbook.md) |
| C and C++ | [c-cpp.md](lang/c-cpp.md) | [c-cpp-cookbook.md](lang/c-cpp-cookbook.md) |
| Rust | [rust.md](lang/rust.md) | [rust-cookbook.md](lang/rust-cookbook.md) |

**The loop:** ask a question, keep the answer, check the fix.

```sh
peeky describe <target>                        # what can I ask? lines, names, rules
peeky probe <target> --at <file>:<line> --capture a,b.c [--set x=0]   # one question, one run
peeky queries diff        |  peeky sql "SELECT ..."                    # follow-ups on saved runs
peeky session start <target> --at <file>:<line>                        # several looks at one stop
peeky verify <target> --check <checker.py> --old '<text>' --new '<text>'   # proof, on a fresh build
```

**Words used here:**

| Word | Meaning |
| --- | --- |
| target | The program to debug: a file or a directory, with a `--config` where needed ([0](#0-setup)) |
| probe | One complete run that stops at a line, captures values and can change them ([2](#2-asking-questions-with-probes)) |
| hit | One stop at the probe's line |
| selector | What to capture: a name, a dotted path, an index or a string key ([2.1](#21-fields-and-indexes)) |
| intervention | A change made with `--set` or `--set-at` during a run ([2.3](#23-test-a-hypothesis-by-changing-a-value)) |
| run | A probe's saved results, which `peeky queries` and `peeky sql` read ([4](#4-sql-over-saved-runs)) |
| session | A program kept stopped, for several looks at one stop ([5](#5-step-by-step-sessions)) |
| worker | The process that runs the debugger next to the program ([9](#9-a-program-on-another-host)) |

> **Tip:** a breakpoint stops **before** its line runs. To see a value that a line assigns, stop on
> the next line.

> **Tip:** an intervention is an experiment on one run. It tells you whether a hypothesis explains
> the output. Only `peeky verify` says that a source fix works.

---

## Table of Contents

0. [Setup](#0-setup): install Peeky and the debugger or toolchain each language needs
   - [Install Peeky](#install-peeky): Python 3.11 or later; `pip install` brings debugpy
   - [What each language also needs](#what-each-language-also-needs): the tools for C, C++, Rust and Java, and how Peeky finds them
   - [Keep a task's runs apart](#keep-a-tasks-runs-apart): `PEEKY_STATE_DIR`
   - [How Peeky tells the language](#how-peeky-tells-the-language): from the target's path, or `--config`
1. [First Look](#1-first-look): what a target offers, and a first probe
   - [1.1 What Can I Ask?](#11-what-can-i-ask): `describe`: breakpoint lines, each function's names and their types (not in Python), what `--set` can change; lines and stepping
   - [1.2 First Probe](#12-first-probe): values at one line, and the program's output
2. [Asking Questions with Probes](#2-asking-questions-with-probes): one run per question: values, changes, returns
   - [2.1 Fields and Indexes](#21-fields-and-indexes): selectors: fields, indexes, keys; names in any script
   - [2.2 Every Iteration of a Loop](#22-every-iteration-of-a-loop): `--hits N`/`all`; lines that stop several times per pass; loop headers
   - [2.3 Test a Hypothesis by Changing a Value](#23-test-a-hypothesis-by-changing-a-value): `--set`: what can be changed, where, and to which values
   - [2.4 Change the Value at a Later Hit](#24-change-the-value-at-a-later-hit): `--set-hit N`: change a later hit
   - [2.5 A Module Imported by a Script, with Arguments](#25-a-module-imported-by-a-script-with-arguments): `--run` for a module a script imports, and program arguments
   - [2.6 Stop Right After the Last Hit](#26-stop-right-after-the-last-hit): `--until hits`: stop once the values are in
   - [2.7 JSON for Scripts](#27-json-for-scripts): `--json` and `jq`
   - [2.8 Change a Value, Then Restore It](#28-change-a-value-then-restore-it): `--set-at LINE@HIT`: change at one line, restore at another; `LINE@all`: at every stop
   - [2.9 What a Function Returned](#29-what-a-function-returned): `--capture return`: what a call returned, in C, C++, Rust and Java
3. [Comparing Runs](#3-comparing-runs): what differs between saved runs
   - [3.1 Two Inputs Side by Side](#31-two-inputs-side-by-side): `queries diff`: what else differed, then the first divergence
   - [3.2 Inputs, Outcomes and Outputs of the Last Two Runs](#32-inputs-outcomes-and-outputs-of-the-last-two-runs): `queries outputs`: arguments, changes, status and output per run
   - [3.3 What My Intervention Changed](#33-what-my-intervention-changed): `queries set_effect`: values before and after a change
   - [3.4 One Row per Hit](#34-one-row-per-hit): `queries wide`: a row per hit, a column per selector
   - [3.5 Joining Two Runs by a Value](#35-joining-two-runs-by-a-value): `queries join`: pair two runs' hits by a value
4. [SQL over Saved Runs](#4-sql-over-saved-runs): your own SQL over saved runs
   - [4.1 What Is in the Store?](#41-what-is-in-the-store): tables, views and SQL functions (`no_addresses`, `object_ref`, `address`)
   - [4.2 The Newest Run](#42-the-newest-run): `last_run`
   - [4.3 Numeric Filters](#43-numeric-filters): `num`: numbers, not text
   - [4.4 Values Seen Across All Runs](#44-values-seen-across-all-runs): `queries values`, and counts across runs
   - [4.5 Parameters and Export](#45-parameters-and-export): `--param`, JSON and CSV export
   - [4.6 Same Object, or a Copy? (Java, Python)](#46-same-object-or-a-copy-java-python): Java object numbers and Python identities
   - [4.7 C++ References and Pointers in SQL](#47-c-references-and-pointers-in-sql): addresses in C++ references and pointers
   - [4.8 Compare Addresses Across Runs](#48-compare-addresses-across-runs): `address(value)`, and offsets from a base each run has
5. [Step-by-Step Sessions](#5-step-by-step-sessions): several looks at one stop
   - [5.1 Stop, Look, Change, Finish](#51-stop-look-change-finish): `start`, `inspect`, `get`, `set`, `close`; caller frames
   - [5.2 Stepping and Running to a Line](#52-stepping-and-running-to-a-line): `step`, its notes, `continue --to`, `--count`, `--hit`
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
| C, C++ | clang (or GCC: set `PEEKY_NATIVE_CC` or `PEEKY_NATIVE_CXX`), lldb-dap and dwarfdump: on macOS the Command Line Tools, on Linux LLVM 18 | `xcrun` or `PATH`. Set `PEEKY_NATIVE_*` to override |
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

How C++ names are shortened: [C and C++ 1.1](lang/c-cpp-cookbook.md#how-a-c-functions-name-is-shown).

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
- A later span starts right after the last line it leaves out. LLDB moves a breakpoint on a line
  without code to the next line with code, and that code is the block's.
- In C++, a block's cleanup code is on the function's closing line. That code runs when an
  exception leaves the block. So `name (std::string, lines 22-25, 35)` is an `if` block's local.
  It is visible on the block's lines, and at the closing `}` while an exception unwinds. It is not
  visible on the lines between. C blocks have no such cleanup code.
- A span is never narrower than where the debugger can show the local. So it keeps the lines
  inside it without code, such as comments or a lone `}`.
- A block that also runs another file's code, such as an `#include` inside it, gives no lines. A
  local macro's expansion, on its definition's lines, does not count as another file's code.
- For example, a Rust `for` loop's `i`, whose body runs `let s = twice!(i);` with a macro defined
  above the loop, reads `i (lines 66, 70-71)`: the macro's line, then the body's. The loop header's
  line between them runs outside the block.
- Past eight blocks, one span covers them all.
- In Java, the lines come from the class file's LocalVariableTable. A line is listed where its
  breakpoint sees the local: its first instruction, where java-debug stops, is in the local's
  scope.
- So neither the line that declares a Java local nor a `for` header that starts its loop is
  listed. `ranking (int[], lines 51-53, 78-79)` is declared on lines 50 and 77
  ([Java 1.1](lang/java-cookbook.md#see-where-a-local-is-visible)).
- A Java block on one line gives no line: `x (int, no breakpoint line)`, and `[]` in `spans`.
- A Java name declared once, for the rest of the method, gets no lines. One declared more than
  once gets every declaration's lines.
- Where Java declares one name with different types, it lists it once per type, each with its own
  lines: `value (int, line 31), value (String, line 34)`. With `--json`, a scope's `typed_spans`
  maps such a name to `[{"type": …, "spans": […]}, …]`, where `spans` may be empty.

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
- [Java 1.1](lang/java-cookbook.md#11-what-can-i-ask): declared types, block lines, `mutable:`.

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
1    observed  3     area      größe          <not found; visible: größe, h, w; select it as listed if that is the one you mean: the debugger lists 'größe' (written 'gr\xf6\xdfe'), which differs from 'gro\u0308\xdfe' only by Unicode normalization, and Peeky never normalizes a name>
status: completed  hits: 1  seconds: 1.086  max pause: 7.0 ms
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

`--hits all` captures every stop, up to 100, without counting iterations first. The cap is
lowered so that every hit fits the probe's budget of 2,000 rows. Each hit takes one row per
selector, and after-set rows count toward the budget too. The summary says `hits: 3 (all)`:

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
- A table of more than 40 rows, of several selectors, ends with a pointer to one line per hit:
  `` evidence: one line per hit, a column per selector: `peeky queries wide --param run=7` ``
  ([3.4](#34-one-row-per-hit)).
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
`false`, Java's rows too. A Java row of a type that `set` changes says why it isn't settable in
`not_settable`. A Java element whose mark can't be promised either way says why in
`maybe_settable`: `[maybe settable: …]`.
`false` includes two things that `set` refuses:

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

To change a value at every stop of a line, not at one hit, use `--set-at LINE@all`
([2.8](#change-it-at-every-stop-all)).

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

`output` is the program's text as the debugger passed it on. A C, C++ or Rust program writes
through a terminal that LLDB opens, and a terminal ends each line with `\r\n`. So their `output`
has `\r\n` where the program wrote `\n`. Java's and Python's keep the program's own line ends. The
evidence store keeps `output` as JSON does. The text table shows the lines without their ends.
Peeky doesn't turn `\r\n` back into `\n`: it can't tell a `\r` the terminal added from one the
program wrote.

LLDB 18 also prints its own errors about some builds' debug information. An example is GCC's
`std::string`: `error: program 0x000000fc: DW_TAG_member '_M_local_buf' refers to type …`.
lldb-dap sends those as `stderr` output. In the launch Peeky makes, the program writes through
its terminal, which lldb-dap sends as `stdout`, its stderr too. So a `stderr` line of exactly
that shape is moved out of `output`. The result has a warning, and `debugger_diagnostics`
lists the first 20 such lines. `session report --json` also gives `diagnostic_count`, how many
there were. lldb-dap sends a program's stderr as `stderr` only where it isn't a terminal. There,
a program line of exactly that shape would be moved too, and the warning says so.

Branch on the exit status:

| Status | Meaning |
| --- | --- |
| 0 | Success. |
| 1 | A completed command with a negative result: a probe that failed after the debugger's launch or attach request was sent, timed out, or never stopped; a verify that didn't pass, a failed build included; a `session get` with a failed selector. |
| 2 | An error, or a refusal. See below. |
| 3 | Not there yet: a background job is still running, or `session start --hit`, `session continue --count` or `session continue --to` stopped before the stop asked for. |
| 4 | A probe completed, but a change it was asked to make (`--set`, `--set-at`) never happened, because its stop never came ([2.8](#28-change-a-value-then-restore-it)). It also means a `--set-at LINE@all` stopped at its cap of 100 stops while the line still ran: the values and output are then from a partially intervened run. This holds for `peeky probe` and for the `peeky-probe` script. The values are evidence, but the experiment didn't happen. |

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
- `--set-at LINE@all name=value` changes it at every stop of `LINE`, up to 100
  ([below](#change-it-at-every-stop-all)).
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

#### Change it at every stop: `@all`

`--set-at LINE@all name=value` makes the change at each stop of `LINE` while the probe runs. Peeky
takes no conditions, since a selector is a path, never an expression. To change a value only in
some case, put `@all` on a line that runs only in that case.

`debounce.c` (C) turns a button's noisy signal into presses and releases. Its `settle` returns
early at line 36 when a sample agrees with the accepted level, without resetting the count
`db->count`. To test "reset it there", reset it at every stop of line 36. But `db` is `settle`'s
pointer, so `--set-at 36@all db.count=0` is refused before the run, as one change there would be.
The refusal names line 80, in `main`, which owns `db`:

```
error [MUTATION_REJECTED]: `db.count` cannot be changed at debounce.c:36, at any stop of this build: in settle, `db` is a pointer: …; feed has one caller: debounce.c:80 in main, which passes `&db`, where `db.count` is settable: a probe's `--set-at 80@N db.count=<value>`, N counting line 80's own stops (…); `--set-at 80@all` would make the change at every stop of line 80, whether or not the call made there reaches line 36: to make it only where line 36 runs, try it in a candidate edit (`--old`/`--new`). Refused before the program runs: nothing ran
```

`main`'s line 80 runs once per sample, so `--set-at 80@all db.count=0` resets the count before
every sample. That is another experiment: the program prints `DEBOUNCE:0:`. A candidate edit makes the
change only where line 36 runs, in one run ([6.6](#66-see-what-a-fix-does-before-making-it)):

```sh
peeky probe debounce.c --at 35 --capture level,db.count --hits all \
  --old 'return 0; /* the input agrees with the accepted level */' \
  --new $'db->count = 0; /* a blip, if any, is over */\n        return 0;' \
  -- 3 00101101111110100100001101100000
```

```
...
output:
  DEBOUNCE:2:P@9,R@20
```

`@all` does it in one option where the frame that owns the value has such a line.
`debounce_main.c` is the same debouncer with its loop in `main`, where line 18 runs only when a
sample agrees with the accepted level:

```sh
peeky probe debounce_main.c --at 18 --capture ms,db.count --hits all \
  --set-at 18@all db.count=0 -- 00101101111110100100001101100000
```

```
...
set db.count: 0 -> 0  (--set-at line 18@all, hit 1, in main)
set db.count: 0 -> 0  (--set-at line 18@all, hit 2, in main)
set db.count: 1 -> 0  (--set-at line 18@all, hit 3, in main)
set db.count: 13 more changes by --set-at line 18@all, at hits 4 to 16 (`--json` lists each)
status: completed  hits: 16 (all)  seconds: 1.498  max pause: 63.7 ms
output:
  DEBOUNCE:P@9,R@20, quiet 16
```

In Java, `set` can change an eligible program-owned instance field through a Java local, parameter
or `this` reference, subject to the holder and field checks
([Java 7.3](lang/java-cookbook.md#change-a-field-or-an-array-element)). So in a Java
`settle(Debounce db, int level)`, `--set-at LINE@all db.count=0` on its early `return` works as it
is.

How `@all` works:

- Each stop's change is made and checked as one `LINE@N`'s is: the check before the run, the rules
  at the stop, and the read-back.
- A refusal at any stop ends the probe, as one `--set-at`'s does. The error names that stop's hit,
  and how many changes were already made. A change made at some stops only isn't the experiment
  asked for.
- On a line that stops more than once per pass ([2.2](#stops-not-passes)), every stop counts. In C,
  C++ and Rust, a stop part-way through a line refuses a change, so pick a line with one stop per
  pass.
- At most 100 stops get the change, as `--hits all` captures at most 100. If the line stops again,
  the probe leaves that stop unchanged and removes the line's breakpoint. It warns, lists the change
  in `unapplied` with `made`, the number of stops changed, and exits 4.
- An `@all` whose line never stops isn't made. A warning and `unapplied` say so, and the probe
  exits 4.
- One `@all` counts as one of the 8 changes a probe may make.
- With `--until hits`, the probe ends after its last hit, and its `@all` with it. A warning says
  that any later execution of the line ran without the change.
- The text shows the first 3 changes, each with its hit, then one line that counts the rest. In
  JSON, each change is in `interventions`, with its own `at` (`18@4`) and `"all": true`.
  `set_outcomes` says how far each request got ([below](#how-the-evidence-records-changes)).

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
- `queries diff`'s and `queries compare`'s `(sets)` rows and `queries outputs` show each change,
  a `--set`'s too, as `[name, value, "requested at LINE@HIT"]`: `["gleich", "true", "requested at
  56@4"]`. Each adds how it went where it wasn't simply made: `, not made: that hit never came`,
  or `, refused at that hit`.
- An `@all` is one request, `"requested at 80@all"`, with how far it got:
  - `, made at 32 stops, to the program's end`;
  - `, made at 1 stop before --until hits ended the probe`;
  - `, made at 6 stops; refused at hit 7`;
  - `, made at the first 100 stops: the cap`;
  - `, not made: its line never stopped`.
- `runs.set_outcomes` (schema 16) keeps each request's outcome, in the order of `runs.sets`: `made`,
  the stops it was made at; `ended`, why no more were; and `complete`. The probe's JSON has the same
  list as `set_outcomes`. Runs saved before schema 16 say what their warnings did:
  `, not made: that hit never came`, `, not made: its line never stopped`, or
  `, made only at the first 100 stops: the cap`.
- A run counts as having recorded its outcomes only where each one matches its request: the same
  option, location, name and value, every key, of the right type, and an end that such a request
  can have. Otherwise its changes read as its warnings did.
- `diff` and `compare` tell two runs' requests apart first, by the requests alone. Where they are
  the same, the runs count as made alike only where both recorded their outcomes. Otherwise the
  `(sets)` row says
  `the same requested interventions; how they were applied wasn't recorded for one or both older runs`.
- So runs whose changes differ in their hit, in which were made, or in how far an `@all` got, tell
  apart. Two runs that changed the same first hit alike, but one to the program's end and one until
  `--until hits` ended it, differ in `(sets)`.
- `diff` and `compare` start with a `(status)` row, with each run's error, where either run didn't
  complete. Two runs refused at different hits differ there too.
- `diff`'s table shows its `(sets)` row short: how many changes, and the first that differs from the
  other run's. `--json` has them all. With an `@all` among them, it counts requests:
  `1 request: db.count=0 (requested at 18@all, made at 16 stops, to the program's end)`.
- `diff` shows more than 3 alike `intervention only in run b` rows as one, its first hit's. Its note
  says how many hits there were: `intervention only in run b, at 15 hits`.
- The `interventions` table lists each change made: its `line`, `hit`, `capture_hit`, `function`
  and `instance` (schema 12). An `@all` has one row per stop it changed. `captures.instance`
  (schema 13) labels captured values the same way.
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
| Anything else: in Java, which has no such watch; where the watch was dropped; or where another stop came in between | Peeky `could not tell whether in this call` (another breakpoint location, or a loop's next pass) `or in a later call after this one ended without returning` |
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

Peeky saves each probe's run with a run id, unless it refused the probe before launch or you passed
`--no-save`. `--label` tags a run, so you can find it later.

These words come up often here:

| Word | Meaning |
| --- | --- |
| hit | One stop at the probe's `--at` line, numbered from 1. |
| stop | One of a line's breakpoint locations, where the line has several ([2.2](#22-every-iteration-of-a-loop)). |
| phase | When a value was read: `observed` before that hit's changes, `after-set` after them, `returned` for a `(return)` row ([2.9](#29-what-a-function-returned)). |
| selector | A name you capture, such as `self._ip` or `bins[3].hi`. |
| intervention | A change a probe makes with `--set` or `--set-at`. |
| view | How Peeky decoded a value that needs care, such as a moved Rust local's bytes (`stale`). |
| note | What Peeky says about a value it can't vouch for, or shows raw, such as a C local's leftovers. |
| caveat | A view or a note that qualifies a value. Read the value with it, never as a plain value. |
| candidate | A run of an edited copy of your source, from `probe --old/--new` ([6.6](#66-see-what-a-fix-does-before-making-it)). |
| schema | The store's tables and columns. Its version number goes up when they change. |

Some examples use `bins`, a small C program. Save it as `bins/bins.c`:

```c
#include <stdio.h>
#include <stdlib.h>
struct bin { int lo, hi; };
int main(int argc, char **argv) {
    struct bin bins[4] = {{0, 1}, {1, 2}, {2, 3}, {3, 4}};
    int total = 0;
    for (int i = 1; i < argc; i++) {
        int v = atoi(argv[i]);
        total += v * bins[v % 4].hi;
    }
    printf("total: %d\n", total);
    return 0;
}
```

### 3.1 Two Inputs Side by Side

**What it answers:** Where do two runs diverge?

Probe each input once, with its own `--label`. Then run `peeky queries diff`. It compares the last
two saved runs. To compare two others, name them: `--param a=1 --param b=5`. `compare`, `outputs`
and `join` take the same two parameters.

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
note: labels: run 1 is 'mapped-public'; run 2 is 'mapped-private'
(4 rows)
```

The first two rows say how the runs differed: their output and their arguments. The next two are the
values that differ at hit 1.

Comparing needs two saved runs. With fewer, the query says so. `peeky queries runs` lists the saved
runs.

#### The rows `diff` shows, in order

`diff` keeps only the rows that differ. They come in this order:

| Order | Rows | What they are |
| ---: | --- | --- |
| 1 | `(status)`, `(output)`, `(edit)`, `(source)`, `(line)`, `(args)`, `(sets)` | How the runs differed |
| 2 | `intervention only in run a` or `run b` | Values that only one run's `--set` changed |
| 3 | values that differ | Real differences, in hit order |
| 4 | `addresses differ: …` | Pointers whose addresses differ: each may point at another element |
| 5 | `qualified (…): not evidence by itself` | Values a view or a note qualifies |
| 6 | `addresses only` | Other values that differ only in addresses, such as a Python object's repr, or a C pointer to the same element |
| 7 | `only in run a` | Selectors only one run captured |

**1. How the runs differed.** Check these rows before you credit an edit with a difference. Each
appears only where it applies:

- `(status)` and `(output)`: the runs' outcomes or outputs differ. `(status)` also appears where
  either run didn't complete, with each run's error, such as the hit at which a change was refused.
- `(edit)`: the runs built different edits, such as a candidate edit in one run only.
- `(source)`: two runs that are not candidates ran different source text, such as another copy of
  the file.
- `(line)`: the probes' `--at` lines differ. Their values then differ partly because of where they
  were read.
- `(args)`: the runs had different program arguments.
- `(sets)`: the runs requested or made different interventions. Each is
  `[name, value, "requested at LINE@HIT"]`, with how it went, as in
  [3.2](#32-inputs-outcomes-and-outputs-of-the-last-two-runs). The table shows how many each run
  made, and the first that differs: `6 changes: db.count=0 (requested at 80@5), …`, or `none`.
  `--json` has the whole list.

A candidate's `--at` keeps the original's line numbers. So `--at 33` is the same line, even where
the candidate's rows show its line 34. It gives no `(line)` row.

The `(output)` row shows up to 200 characters of each output. Each window starts 60 characters
before the first character that differs. It starts at the beginning when the difference is within
the first 60. `…` marks a cut. The note says where the outputs differ, counted from 1:
`output differs at line 4, column 13`. So after a `--set`, this row shows the intervention's effect.

**2. Values that only one run's `--set` changed** say `intervention only in run a` or
`intervention only in run b`. They come before what may follow from them. Peeky leaves out an `after-set` row where, in each run that has one, it
equals that run's `observed` value at that hit. The intervention didn't touch it, as `r.buf` when
only `r.tail` was set. The `observed` row shows any difference.

**3. Real differences** come in hit order, then in `--capture` order. The first of them is the first
divergence.

- A hit that one run never reached says `missing in run b`.
- A C or C++ pointer that holds another element of an array both runs captured says which, in
  each run: ``another element of `level`: index 6 in run a, 4 in run b``. It is a real difference
  even where its address reads the same in both
  ([C and C++ 4.8](lang/c-cpp-cookbook.md#48-compare-addresses-across-runs)).
- Where that array is a pointer, such as a parameter, an offset from it by address is compared
  instead: ``another offset from `level` by address: `level + 6` in run a, `level + 4` in run b``.
  It names no element.
- A value whose type changed says so, for example after an edit to `long long`:
  `type unsigned int -> long long`.

**Long values.** The table cuts a value at 120 characters. Where the two runs' values first differ
near or past that cut, both show from 40 characters before the difference instead. Both start at
the same place, so the two windows line up. `…` marks every cut. `--json` keeps every value whole.
The table never cuts a note, so a note always shows what to do.

**4. Pointers whose addresses differ** say `addresses differ`. An address can be another element's
in the other run, so such a row may be a real difference. A pointer into an array may point at
element 6 in one run and at element 4 in the other.

- Most are a pointer or a reference, by their type, such as `const int *` or
  `const std::string &`. They say `it may point at another element too`.
- Any other value of a C, C++ or Rust run is here too, as a struct's preview shows its pointers as
  addresses. Such a row says `an address in it may be another element's too`.
- The stack and the heap start at another address in each run. So most pointers differ, whether or
  not they point at another element. That is why these rows come after the real differences, not
  among them: the first real difference stays the first divergence.
- To tell, compare each address's offset from a base with `address()`
  ([4.8](#48-compare-addresses-across-runs)). An offset that differs is another element.
- C and C++ do that for you where both runs captured the array a pointer points into. Such a
  pointer is a real difference (3) or the same element (6), never here.
- There is one row per selector, as for `addresses only` below. The note says at how many hits:
  `addresses differ at 2 hits (one row per selector: its first hit): …`.

**5. Qualified values.** A value that a view or a note qualifies, in either run, ranks here. Its
note says `qualified (run a: note, run b: note): not evidence by itself`.

- Examples are a C local's leftovers on a `do` body's first line, or a moved Rust local's bytes
  (`stale`).
- Two runs' leftovers can differ because of anything that touched the stack before. So such a row is
  not a divergence of the program.
- Whatever else the row says, its note says the value is qualified, and the row ranks here:
  `missing in run b; qualified (run a: note)`,
  `intervention only in run a; qualified (run a: note)`.
- `queries run` shows each note.

**6. Address-only differences** say `addresses only`. These are other values that differ only in
addresses, such as a Python object's repr (`<Node object at 0x…>`), or a Java object's number.

- A C or C++ pointer that holds the same element of an array both runs captured says
  ``addresses only: the same element of `level` in both runs``. Only the array moved. The same
  offset from a base pointer says ``addresses only: the same offset from `level` in both runs``.
- A C or C++ pointer or reference whose text after its address reads the same in both runs is
  here too, such as `0x… "Maya"`. It says `what it points at reads the same in both runs, though
  it may be another element that reads the same`. Two elements can hold the same text.
- One whose text holds another address, such as `0x… {next = 0x…}`, stays in tier 4. What that
  address points at may differ.

- NULL is never treated as an address.
- Text inside quotes is the program's data, never an address. So `"0x00000001deadbeef"` in a
  `char[32]` is a real difference.
- There is one row per selector: its first hit's. The note says at how many hits:
  `addresses only, at 4 hits (one row per selector: its first hit)`.
- Where both runs label stops, there is one row per selector and stop:
  `at 2 hits at stop 1 (one row per selector and stop: its first hit)`.

**7. Selectors that only one run asked for** come last. Each has one row, `only in run a`, with its
first hit's value.

#### How `diff` pairs hits

`diff` pairs hits by number: hit 3 of one run with hit 3 of the other. That is right only where hit
3 is the same moment in both runs. Where Peeky finds a sign that it may not be, it adds a `note:`
line. It keeps every row either way.

**When the runs stopped a different number of times,** the hits need not line up. For example, a
sort calls its comparator on other pairs once one answer differs. Then pairing by hit number can
show every later hit as a difference. Two notes say so:

- The first names `queries join` ([3.5](#35-joining-two-runs-by-a-value)), which pairs hits by a
  selector's value instead: `--param key=a.name`. Several selectors work too:
  `--param key=p.name,n.name`.
- The second says how many rows come after the first hit with a real difference (tier 3). Those
  rows may compare different calls. A pointer, a qualified value or an address-only value is never
  that first difference, even at an earlier hit. With no real difference at a hit, there is no
  second note.

These runs were recorded in a fresh state directory ([0](#0-setup)), so they are runs 1 to 6:

```sh
b() { peeky probe bins --at bins.c:9 --hits all "$@"; }
b --capture v,total -- 7 2 5
b --capture v,total -- 2 7 7 8
peeky queries diff
```

```
run_a  run_b  hit   phase     selector  value_a          value_b               differs  note
1      2      NULL  NULL      (output)  total: 44\n     total: 70\n          1        output differs at line 1, column 8
1      2      NULL  NULL      (args)    ["7", "2", "5"]  ["2", "7", "7", "8"]  1        the runs had different program arguments: values also differ by input
1      2      1     observed  v         7                2                     1        NULL
1      2      2     observed  v         2                7                     1        NULL
1      2      2     observed  total     28               6                     1        NULL
1      2      3     observed  v         5                7                     1        NULL
1      2      4     observed  v         NULL             8                     1        missing in run a
1      2      4     observed  total     NULL             62                    1        missing in run a
note: pair the hits by a value instead: `peeky queries join --param a=1 --param b=2 --param key=<selector>[,<selector>…]`. Run 1 stopped 3 times at its line and run 2 4, so a hit need not be the same moment in both, and pairing by hit number can show every later hit as a difference
note: the first difference at a hit is at hit 1; 5 of the 6 rows at a hit come after it, where pairing by hit number may compare different calls: read those with `join`
(8 rows)
```

**When the runs stopped as often,** an edit can still change which call a hit is. The note then
gives the first sign it finds, of these three. A value that differs after an edit or an intervention
may follow from the change, so the note words its sign with care
([below](#which-values-count-when-pairing-hits)).

1. **Places don't pair.** A hit's place is its function, its instance and its line. An instance is a
   closure, or a generic's copy. Each place in one run must pair with one place in the other, the
   same way at every hit. A function renamed, or lines an edit moved, pair the same way at every
   hit. So they count as the same place. Otherwise the note says where the pairing breaks:

   ```
   at hit 3 run 2 stopped in f at line 24 and run 3 in g at line 24, but at hit 1 run 2 in f at line 24 paired with run 3 in f at line 24
   ```

2. **Values line up better at other hits.** Peeky masks addresses and Java object numbers first, as
   `no_addresses` does. Then it checks whether a selector's values line up better at other hits than
   hit by hit:
   - The same values can come in another order:

     ```
     runs 1 and 2 saw the same values of `op` in another order (run 2's hit 4 matches run 1's hit 5: -)
     ```

   - `[A, B, C]` against `[A, C, D]` gives ``saw `k`'s values at other hits``.

   The note says the calls *may* have come in another order or number. So read the rows. Where only
   values that differ after a change give the sign, the note says the change may account for it.
3. **Most hits differ.** A hit differs where any selector that both runs captured reads differently
   there, before any change. Where more than half the hits differ, and at least 2, the values can't
   show that the hits pair. The note says so:

   ```
   the runs' values differ at 4 of 5 hits, so they cannot show that a hit is the same moment in both
   ```

Each of these three notes names `queries join` where it could pair the hits: where the captured
selectors, taken together, tell each run's hits apart. Join by a value that names the call and that
the edit leaves alone, such as an argument or an index. Don't join by a value the edit changed.
Where no captured selector tells the hits apart, the note says that `join` could not pair them.

**No note does not prove that the hits line up.**

#### Which values count when pairing hits

Only plain values count in these checks. Peeky checks each selector on its own.

- A value that a view or a note qualifies is not plain. It is no evidence either way. Examples are a
  moved Rust local's bytes and a C local's leftovers.
- A value that differs between the runs after a change may follow from the change. A change is an
  edit, or an intervention in either run. An edit is a candidate, or other source text.
- After an edit, that is from the first hit. After an intervention, it is from the first hit after
  the change, whatever the name changed. The same selector read before the change counts like any
  other value.
- So such a selector is left out of what agrees and of the hits that differ. The note names it.
- It still counts where its values line up better at other hits. The change may account for that,
  so the note says it doesn't prove a reorder. But no note then says the hits line up.
- `after-set` rows never count.

In this C example, `--set-at` resets a counter, `db.count`, in `main`. The probe stops in `settle`,
once per sample. The reset changes `db.stable` too, at later samples. Its 0s and 1s then line up
better at other hits:

```
note: `level` agrees between the runs at each of the 32 hits, but it reads alike at some hits, which could still trade places; `db.candidate` and `db.count` differ after the interventions (run 2's on `db.count` in main) and may follow from them, so this check leaves them out; but runs 1 and 2 saw `db.stable`'s values at other hits (run 2's hit 11 matches run 1's hit 7: 1): `db.stable` may differ because of the interventions, so this does not prove a reorder, but it keeps Peeky from saying the hits line up; no captured selector tells the hits apart, so `queries join` could not pair them
```

`level` is the input sample. It agrees between the runs, but it is 0 or 1, so it can't tell the hits
apart. No captured value names `settle`'s call, so the note says `join` couldn't pair them.

#### When values agree at every hit

Some values may agree between the runs at every hit. Where nothing suggests another pairing, a note
says so. It may also say that the hits line up by them, but only with no intervention in either
run, and with no selector left out. The cases below say when. A constant, a value that never
changes from hit to hit, doesn't count here.

Here the second run's last input differs. `i` tells the hits apart, so the hits line up by it:

```sh
b --capture i,v,total -- 0 0 7 8
b --capture i,v,total -- 0 0 7 9
peeky queries diff
```

```
run_a  run_b  hit   phase     selector  value_a               value_b               differs  note
3      4      NULL  NULL      (output)  total: 36\n          total: 46\n          1        output differs at line 1, column 8
3      4      NULL  NULL      (args)    ["0", "0", "7", "8"]  ["0", "0", "7", "9"]  1        the runs had different program arguments: values also differ by input
3      4      4     observed  v         8                     9                     1        NULL
note: the hits line up by `i`: it agrees between the runs at each of the 4 hits, and its value is different at every hit, and no single selector's values line up better at other hits, so pairing hits by number pairs them as `queries join` by it would
(3 rows)
```

After an edit, a selector that differs is left out. The note then says what pairing by number amounts
to, and that this doesn't prove the hits are the same moments. Here a candidate edit changes `total`
at hit 4:

```sh
b --capture i,v,total --old 'bins[v % 4].hi' --new 'bins[v % 4].lo' -- 0 0 7 8
peeky queries diff --param a=3 --param b=5
```

```
run_a  run_b  hit   phase     selector  value_a       value_b       differs  note
3      5      NULL  NULL      (output)  total: 36\n  total: 21\n  1        output differs at line 1, column 8
3      5      NULL  NULL      (edit)    none          candidate     1        the runs built different edits (runs.edit has each diff)
3      5      4     observed  total     28            21            1        NULL
note: `i` agrees between the runs at each of the 4 hits, and its value is different at every hit, so pairing hits by number pairs them as `queries join` by it would, though that does not prove the hits are the same moments; `total` differs after the edit and may follow from it, so this check leaves it out
(3 rows)
```

After an intervention, the same values show less:

```sh
b --capture i,v,total --set total=5 --set-hit 4 -- 0 0 7 8
peeky queries diff --param a=3 --param b=6
```

```
run_a  run_b  hit   phase      selector  value_a       value_b                               differs  note
3      6      NULL  NULL       (output)  total: 36\n  total: 13\n                          1        output differs at line 1, column 8
3      6      NULL  NULL       (sets)    none          1 change: total=5 (requested at 9@4)  1        the runs requested different interventions (--set, --set-at), or made different ones
3      6      4     after-set  total     NULL          5                                     1        intervention only in run b
note: `i`, `v` and `total` agree between the runs at each of the 4 hits, and their values are different at every hit, and no single selector's values line up better at other hits; but the interventions (run 6's on `total` in main) can change other values and which calls reach the line, so this does not show that each hit is the same moment in both
(3 rows)
```

What the note says depends on the case:

- **Another pairing is suggested.** Where any selector's values line up better at other hits, the
  note says so, whatever else agrees. Where other values agree between the runs at every hit, it says
  both. It says the values don't settle it, and never that the hits line up:

  ```
  `key` agrees between the runs at each of the 2 hits, but runs 1 and 2 saw the same values of `call` in another order (run 2's hit 1 matches run 1's hit 2: 'B'): the values do not settle whether a hit is the same moment in both
  ```

  Peeky can't tell which of them names the call. A value alike by chance, such as a counter, agrees
  either way.
- **The sign comes from a selector left out.** Its values may differ because of the change. So the
  note says the sign doesn't prove a reorder. It still keeps the note from saying the hits line up.
  Here an edit swaps the first two calls, and `key` agrees at every hit:

  ```
  `key` agrees between the runs at each of the 3 hits, and its value is different at every hit, so pairing hits by number pairs them as `queries join` by it would, though that does not prove the hits are the same moments; but runs 1 and 2 saw the same values of `call` in another order (run 2's hit 1 matches run 1's hit 2: 'B'): `call` may differ because of the edit, so this does not prove a reorder, but it keeps Peeky from saying the hits line up
  ```

- **Most hits differ.** Where values differ at more than half the hits, and at least 2, the note
  says so before anything that agrees. It says the values don't show that the hits are the same
  moments:

  ```
  `key` agrees between the runs at each of the 2 hits, but `call` differs at 2 of them: the values do not show that a hit is the same moment in both
  ```

- **No change in either run.** The note says what the agreeing values show:
  - The values tell the hits apart, as each hit reads differently, and at most one selector differs.
    Then the note says the hits line up by them, as in the `i` example above.
  - The values read alike at some hits. The note says the hits line up as far as they show, but
    alike hits could still trade places:

    ```
    the hits line up as far as `word` shows: …; but it reads alike at some hits, which could still trade places
    ```

  - Two or more selectors differ. Peeky checks selectors one at a time, and two that differ could
    line up better together. So no note then says the hits line up outright. It says they line up as
    far as each selector alone shows:

    ```
    the hits line up by `key` as far as each selector alone shows: …; `p` and `q` differ, and how they line up together was not checked
    ```

  In each case, this holds only as far as those values name the call. An edit with no selector left
  out gives the same notes.
- **After a change that left selectors out,** what follows from it is unknown. So the note says only
  what agrees, and what was left out. Where the values that agree tell the hits apart, it says what
  pairing by number amounts to, as in the edit example above. Here the counter program is probed in
  `main`'s loop, where `ms` counts the samples:

  ```
  `ms` agrees between the runs at each of the 32 hits, and its value is different at every hit, so pairing hits by number pairs them as `queries join` by it would, though that does not prove the hits are the same moments; `db.candidate`, `db.count` and `changes` differ after the interventions (run 3's on `db.count` in main) and may follow from them, so this check leaves them out; but runs 1 and 3 saw `db.stable`'s values at other hits (run 3's hit 11 matches run 1's hit 7: 1): `db.stable` may differ because of the interventions, so this does not prove a reorder, but it keeps Peeky from saying the hits line up
  ```

- **After an intervention that left nothing out,** the note says what agrees, what differs and which
  interventions there were, as in the `total=5` example above.
- **A change moved every value.** Where no selector both agrees between the runs and changes from hit
  to hit, a short note says so. Here a candidate copies each undo snapshot's items, so every value
  differs:

  ```
  the values cannot show whether the hits line up: `previous` and `self.items` differ after the edit and may follow from it, and no other selector both agrees between the runs and changes from hit to hit
  ```

- **No value agrees at every hit,** or only a constant does, and nothing changed. Then only the three
  signs above give a note.

#### Pairing hits on lines with several stops

Some lines have several breakpoint locations, so one pass stops there more than once
([2.2](#22-every-iteration-of-a-loop)). Where both runs label their stops, a hit pairs only with the
other run's hit at the same stop.

- A hit at another stop gives two rows: `missing in run b (run a: stop 2)` and
  `missing in run a (run b: stop 1)`.
- The runs' stops can part. An edit may change how often the line stops per pass. Or an older Peeky
  saved one run, before it labelled stops. The note then says at which hit, and names `join`.
- A `(return)` row ([2.9](#29-what-a-function-returned)) pairs by its hit's stop, which it records
  from schema 15 on. Upgrading an older store fills it in for earlier runs. It takes the stop from
  that hit's other rows, where each has one and all agree.
- An earlier run's `(return)` row may have no stop. That happens with `--capture return` alone on
  such a line, or with rows of its hit at different stops. Then nothing says where its call began.
  It pairs with no other run's row at a stop, and the note says pairing by hit can't be checked.

#### Labels, `compare` and older runs

- `queries compare` shows every value of the two runs side by side, with the same notes. Its
  `(status)` row comes first where either run didn't complete, and its `(sets)` row where either
  asked for a change ([2.8](#how-the-evidence-records-changes)).
- A canned query reads its rows, its notes and its table from one snapshot of the store. So a run
  saved meanwhile, or a run it evicts, can't change them.
  - A probe that saves meanwhile waits for the query, but for about 10 seconds at most. Past that,
    the probe still returns its result, but its evidence isn't saved. It warns
    `Evidence was not saved: database is locked`.
  - The 10-second SQL limit applies to each statement. A canned query runs several, so its snapshot
    can last longer.
- Labels are free text, stored as given. `diff` and `compare` name them in a `note:` line:
  `labels: run 1 is 'mapped-public'; run 2 has no label`.
- Labels never change which runs are compared. Nor do they change which run a candidate's `compare:`
  line names.
- A run that an older Peeky saved may lack its notes: its `runs.notes_kept` is NULL. It may then
  show qualified values unmarked. Every canned query that shows its values says so in a `note:`
  line (`notes` in JSON). Your own `peeky sql` doesn't: check `runs.notes_kept` there.

### 3.2 Inputs, Outcomes and Outputs of the Last Two Runs

**What it answers:** What went into each run, and what came out?

`queries outputs` shows each run's label, arguments, requested changes, status and output:

```sh
peeky queries outputs
```

```
run_id  label           args                 sets  status     error_code  output
1       mapped-public   ["::ffff:8.8.8.8"]   []    completed  NULL        ::ffff:8.8.8.8: private\n
2       mapped-private  ["::ffff:10.0.0.1"]  []    completed  NULL        ::ffff:10.0.0.1: private\n
(2 rows)
```

Both outputs say `private`. But `::ffff:8.8.8.8` is public, so its output is the bug.

`sets` lists each run's requested changes. Each is `[name, value, "requested at LINE@HIT"]`:

- A `--set` is at its `--at` line's `--set-hit`: `["gap", "20", "requested at 34@4"]`.
- A `--set-at` is at its own `LINE@HIT`, or `LINE@all`.
- A change whose hit never came ends with `, not made: that hit never came`. The probe warned about
  it too. One refused there ends with `, refused at that hit`.
- An `@all` says how far it got: `, made at 6 stops; refused at hit 7`, or `, made at 1 stop before
  --until hits ended the probe` ([2.8](#how-the-evidence-records-changes)).

### 3.3 What My Intervention Changed

**What it answers:** What did my changes do to the captured values?

`queries set_effect` shows each selector before and after the run's interventions:

```sh
peeky probe examples/pagination_bug.py --at 8 --capture start --set start=0
peeky queries set_effect
```

```
hit  line  function  selector  observed  after_set  caveat_observed  caveat_after_set  interventions
1    8     get_page  start     3         0          NULL             NULL              start: 3 -> 0
(1 row)
```

- Rows come in the order the changes were made.
- `caveat_observed` and `caveat_after_set` name a view or a note that qualifies either value, as
  `hits` does.
- A change at a stop where nothing was captured is one row, with what it changed before and after. A
  `--set-at` on a line other than `--at` gives such a stop
  ([2.8](#28-change-a-value-then-restore-it)).
- **Long values:** the table shows `observed` and `after_set` as `diff` shows its pairs
  ([3.1](#31-two-inputs-side-by-side)). It shows each change in `interventions` from 30 characters
  before where its two values differ. `--json` keeps them whole. A Python list whose last item's
  `level` was set from 1 to 0 shows as:
  `previous: …1}, {'text': 'Café', 'level': 1}] -> …1}, {'text': 'Café', 'level': 0}]`.

### 3.4 One Row per Hit

**What it answers:** What were all the captured values at each hit, side by side?

`queries wide` gives one row per hit and phase, and one column per selector. It shows the newest
run, unless you name another.

`bins.c` ([3](#3-comparing-runs)) loops over its arguments. Line 9 reads
`total += v * bins[v % 4].hi;`.

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
(4 rows)
```

- **Columns:** one per selector, named exactly as captured, in `--capture` order.
  - A selector named like a fixed column, such as `line`, becomes `line (selector)`.
  - SQLite ignores case in column names. So a selector that differs from an earlier column only by
    case gets a number: `--capture x,X` gives `x` and `X (2)`.
  - In your own SQL, quote such names with double quotes: `"bins[3].hi"`, `"X (2)"`.
  - A result has 100 columns at most. So `wide` fits 94 selectors, or 93 when it adds a `stop`
    column. It adds one where the run's line has several stops.
  - `wide` refuses a wider run. Use `peeky queries run` instead, or `peeky sql` with the selectors
    you need.
- **Cells** hold the values exactly as stored, as text. To compare numbers, use `num` in `captures`
  ([4.3](#43-numeric-filters)).
- **NULL is never a value.** `no_value` is a JSON object. It names each cell with no value, and says
  why: optimized out, withheld, or an error. For example:
  `{"name":"no value yet: declared at line 7, below this stop, …"}`.
- A selector not captured at that hit is NULL, and `no_value` doesn't name it.
- **`caveats`** is a JSON object. It names each cell whose value is qualified. Read such a cell with
  its caveat, never as a plain value. The caveat is one of these:
  - The row's view, such as `stale`, `unproven`, `opaque` or `budget-exhausted`.
    `peeky sql --schema` explains `view`. `stale` is a moved or dropped Rust local's leftover bytes,
    or a caller's Rust container that may be mid-change, whatever view decoded them.
  - `note`, where the probe marked the row `[note]`. A C local declared on a `do` body's first line
    gives `{"k":"note"}`: "may be the last pass's value, or leftovers".
  - `note` too, where the probe gave the row one of the Rust notes among its warnings.

  `peeky queries run` shows each row's note.
- **Phases:** a run with `--set` has an `after-set` row after its hit's `observed` row. To keep one
  phase, pass `--param phase=observed` or `--param phase=after-set`.
- **`sets`** is a JSON list of the changes made since the previous row's values were read:
  `["colon: 7 -> 6 (line 22, hit 3)"]`.
  - On an `after-set` row, it lists the changes made at that stop.
  - On an `observed` row, it lists the changes made at other lines' stops after the previous hit
    (`--set-at`, [2.8](#28-change-a-value-then-restore-it)).
  - A change after the last hit comes before no row. `queries set_effect` lists them all
    ([3.3](#33-what-my-intervention-changed)).
- **`--json`** also gives the SQL it ran, as `sql`. Refine it with `peeky sql`:
  `SELECT * FROM (<sql>) WHERE "v" = '7'`.

### 3.5 Joining Two Runs by a Value

**What it answers:** For the same value of `v`, what did each run see?

`diff` pairs hits by number. That says little when the inputs come in a different order, or when the
runs took different paths. `queries join --param key=v` pairs the hits that have the same value of
`v` instead.

- For a sort's comparator, `--param key=a.name` pairs its calls on the same element.
- Where no one selector names a call, several can together. `--param key=a.name,b.name` pairs hits
  where each has the same value.

```sh
peeky probe bins --at bins.c:9 --capture 'v,total,bins[3].hi' --hits all -- 7 2 5
peeky probe bins --at bins.c:9 --capture 'v,total,bins[3].hi' --hits all -- 2 7 7 8
peeky queries join --param key=v    # the last two runs; --param a=3 --param b=4 for others
```

```
v  hit_a  hit_b  a:total  b:total  no_value_a  no_value_b  caveats_a  caveats_b  matched
7  1      2      0        6        NULL        NULL        NULL       NULL       both
7  1      3      0        34       NULL        NULL        NULL       NULL       both
2  2      1      28       0        NULL        NULL        NULL       NULL       both
5  3      NULL   34       NULL     NULL        NULL        NULL       NULL       only in run a
8  NULL   4      NULL     62       NULL        NULL        NULL       NULL       only in run b
note: the key repeats: add a selector that names the call to the key (key=v,<selector>). Run 9's hits 2 and 3 both have `v` 7, and such a value pairs every hit that has it in one run with every one in the other (2 rows, from 1 value)
note: left out 1 selector that agrees between the runs at every paired hit: `bins[3].hi` (with its values at unpaired hits); --param unchanged=1 or --json shows it
(5 rows)
```

Run b saw `7` twice, so run a's hit 1 appears twice. `bins[3].hi` reads 4 in both runs at every
pair, so the table leaves it out.

- **Matching** is by the key's stored text, exactly. `join` uses the `observed` rows, unless you
  pass `--param phase=after-set`.
- **Both runs must have captured the key.**
- **Repeated key values.** A plain one pairs every hit that has it in one run with every hit that
  has it in the other. A qualified one matches nothing (see unmatched hits below).
- A plain value at 2 hits of run a and 3 of run b gives 6 rows. They come in run a's hit order, then
  run b's. A `note:` line says so. It starts with the selector to add, then gives the first such
  value and how many rows they make. Here `key=v,i` tells those hits apart.
- **Several selectors:** `key=a,b` pairs hits where every one of them has the same value. Each gets
  a column. A hit missing any of them is unmatched. You can give up to 8.
- Commas outside brackets and quotes separate the selectors. So `m["a,b"]` is one selector.
- **Unmatched hits** come last, one row each, as `only in run a` or `only in run b`. A hit is
  unmatched in three cases:
  - The other run never had that key value.
  - The hit has no key value. The key cell is NULL, and `no_value_a` says why.
  - A view or a note qualifies the hit's key value. The key cell is NULL, and `caveats_a` says why.
    Such a value pairs nothing.
- **Columns:** first the key, one column per selector, then each run's hit. Then each run's other
  selectors, as `a:<selector>` and `b:<selector>`. Names that differ only by case are numbered, as
  in `wide`. Last, `no_value` and `caveats` for each run, as in `wide`.
- **Unchanged selectors are left out of the table.** A selector whose cell is the same in both runs'
  hits of every pair is not shown. Its value, why it has none, and its caveat must all match. A
  `note:` line names them and says how many.
  - Their values at unpaired hits go too. `--param unchanged=1` shows them all.
  - Where no hits pair, nothing is left out. A key is never left out.
  - `--json` always has every column, and its `sql` gives them all.
- On an unmatched hit, `caveats` includes the key's own caveat. Peeky makes no pair on a value it
  can't vouch for.
- **`--json`** gives the SQL too, as `sql`.

**Keys with addresses.** A key whose values carry an address rarely matches, as addresses differ
between runs. Examples are a pointer, or a C string: `0x16fbcbb1e "apple"`. The result then says so
in a `note:` line (`notes` in JSON).

- `--param masked=1` matches the keys with addresses masked, as `no_addresses` does. It shows each
  run's own value as `a:<key>` and `b:<key>`.
- Masked, any address matches any other. So a bare pointer pairs with every other bare pointer.
- NULL is not masked. Neither is text inside quotes: `"0x00000001deadbeef"` in a `char[32]` is data.

---

## 4. SQL over Saved Runs

Peeky keeps saved runs in a SQLite database, the store. Your queries on it are read-only and
bounded. They need no rerun.

### 4.1 What Is in the Store?

**What it answers:** Which tables, functions and canned queries can I use?

```sh
peeky sql --schema          # tables, columns, views, canned queries, examples
peeky queries               # canned questions: runs, run, hits, compare, diff, outputs, set_effect, values, wide, join, unreadable, failures
```

A canned query is a question Peeky has ready, such as `peeky queries diff`.

| Table or SQL view | One row per |
| --- | --- |
| `runs` | probe run: its inputs, interventions, status, output and build identity |
| `captures` | selector, hit and phase. `num` holds the value as a number. `view` and `note` qualify a value: read it with them. |
| `interventions` | applied `--set` or `--set-at` |
| `pauses` | pause, one per hit |
| `evidence` (SQL view) | capture, joined with its run and the run's `language`. You need no JOIN. |
| `last_run` (SQL view) | `evidence` row of the newest run |

The program's output is not in `evidence`. Read it from `runs.output`.

**Functions** for your own SQL:

| Function | What it gives |
| --- | --- |
| `no_addresses(value)` | The value with heap and stack addresses masked, to compare runs. It never masks NULL, short hex or quoted text. |
| `no_addresses(value, language)` | The same, and it also masks Java object numbers in Java runs. `evidence` has `language`. |
| `object_ref(value)` | The Java object a value is, such as `Balance$Worker@100`. Within one run, equal means the same object. |
| `address(value)` | The first `0x` token in the value, as an integer, to compare addresses across runs ([4.8](#48-compare-addresses-across-runs)). Call it on a pointer's own selector. NULL where there is none, or it is zero. |

`object_ref(b.value) = object_ref(w.value)` tells a copy from the list's own element
([Java 4.6](lang/java-cookbook.md#46-same-object-or-a-copy-java-python)). Python records which
object a value is in the `identity` column instead.

### 4.2 The Newest Run

**What it answers:** What did the newest run capture?

```sh
peeky probe examples/corpus/running_max/program.py --at 6 --capture value --hits all
peeky sql "SELECT hit, selector, value FROM last_run"
```

```
hit  selector  value
1    value     -3
2    value     -1
3    value     -2
(3 rows)
```

### 4.3 Numeric Filters

**What it answers:** On which hits was a value out of range?

```sh
peeky sql "SELECT hit, value, num FROM evidence WHERE run_id = 7 AND selector = 'value' AND num < -1"
```

```
hit  value  num
1    -3     -3
3    -2     -2
(2 rows)
```

- `value` is the debugger's text.
- `num` is that value as a number. It is NULL when the value isn't a number.
- An integer that fits 64 bits is exact in `num`: `600`, not `600.0`.
- A larger integer, such as a `u64` near its maximum, is approximate in `num`. Compare those through
  `value`.
- As in any SQL, integer division truncates. Write `num * 1.0 / 1000` to keep the fraction.
- `SUM` of integers fails past 64 bits. Use `TOTAL(num)`, which returns a REAL.
- Peeky upgrades older stores in place, and recomputes `num` from `value`.

### 4.4 Values Seen Across All Runs

**What it answers:** Which values did a selector take, in all saved runs?

```sh
peeky queries values --param selector=v
```

```
program  line  value  seen  qualified  runs  first_run  last_run
bins.c   9     7      5     0          3     4          6
bins.c   9     2      3     0          3     4          6
bins.c   9     8      2     0          2     4          6
bins.c   9     5      1     0          1     5          5
(4 rows)
```

`qualified` counts the sightings that a view or a note qualifies. Candidate runs are left out.

For anything else, write the SQL:

```sh
peeky sql "SELECT program, COUNT(*) AS runs, SUM(status = 'completed') AS completed FROM runs GROUP BY program"
```

```
program            runs  completed
bins.c             3     3
ipaddress39.py     2     2
pagination_bug.py  1     1
program.py         1     1
(4 rows)
```

### 4.5 Parameters and Export

**What it answers:** How do I pass a value into a query, and save the result?

```sh
peeky sql "SELECT * FROM evidence WHERE run_id = :run" --param run=3 --json > run3.json
sqlite3 -readonly -csv -header ~/.peeky/evidence/evidence.db "SELECT * FROM evidence" > evidence.csv
```

`:run` in the SQL takes the value of `--param run=3`. `sqlite3` reads the store file directly:
`evidence/evidence.db` in the state directory.

`peeky sql` has these limits:

| Limit | Value |
| --- | --- |
| Statements | one `SELECT`, or `WITH … SELECT`, per call |
| Rows | 200 by default; `--limit` up to 10000 |
| Time | 10 s |
| JSON result | 16 MB |
| Runs kept | the newest 500 |

Peeky refuses writes, `ATTACH` and settings with `INVALID_QUERY`. The store holds evidence already
captured. For a new question about the program, run a new probe.

### 4.6 Same Object, or a Copy? (Java, Python)

**What it answers:** Is this variable the list's own element, or a copy whose changes go nowhere?

- Java compares objects with `object_ref`: see
  [Java 4.6](lang/java-cookbook.md#46-same-object-or-a-copy-java-python).
- Python compares them with the `identity` column: see
  [Python 4.6](lang/python-cookbook.md#46-same-object-or-a-copy-java-python).

### 4.7 C++ References and Pointers in SQL

**What it answers:** Which arguments did `trim_left` change?

A `const std::string &`, or a pointer, is saved as the debugger shows it, with its address first:
`0x000000016d78b298 "apple"`. It never equals a plain string's `"apple"`. So take the text after the
address before you compare.

The recipe is in [C and C++ 4.7](lang/c-cpp-cookbook.md#47-c-references-and-pointers-in-sql).

### 4.8 Compare Addresses Across Runs

**What it answers:** Two runs saw pointers into the same array. Which element did each one point
at?

The heap may start at another address in each run. So one element can have another address in
each run, and one address can be another element. Matching their last hex digits works only by
chance. `queries diff` sends you here from a row whose note says `addresses differ`
([3.1](#31-two-inputs-side-by-side)).

Instead, subtract a base from each address: the address of one element that every run saw, such as
the array's first. Then join the runs on that offset. `address(value)` reads the first `0x` token
in a value, of 1 to 16 hex digits, as an integer:

| Value | `address(value)` |
| --- | --- |
| `0x0000000102ec5850`, a pointer | `4344010832`: the address it holds |
| `0x00000000004052a0`, a pointer into a Linux program's heap, built without PIE | `4215456` |
| `0x000000016d78b298 "apple"`, a C string | `6131593880`: the pointer's. Hex inside quotes is text. |
| `&str @ 0x104afcf50`, LLDB's summary | `4373598032`: where the value itself is stored |
| `{x = 0x1234567890, p = 0x0000600000c04000}`, a struct | `78187493520`: `x`, the first hex token, not `p` |
| `0x0000000000000000`, a null pointer | NULL |
| `"0x0000000102ec5850"`, `'abc'` | NULL: a string's text |
| `Merge$Interval@143 {start=1}` | NULL: a Java object number isn't an address. Use `object_ref`. |

`address` is lexical: it takes the first hex token, whatever the type. So call it on a pointer's
own selector, such as `h.p`, not on a struct's preview. A Python identity (`[id 6]` in tables)
isn't an address. It is in the `identity` column, not in the value.

`no_addresses` masks less: only hex of 9 to 16 significant digits, which is surely an address. So it
leaves `0x4052a0` and `0xdeadbeef` as they are, and `address` reads both.

This SQL gives each address of selector `p` in run 7, and of `q` in run 8, as an offset from its
run's lowest. Then it pairs equal offsets:

```sh
peeky sql "WITH a AS (SELECT run_id, hit, phase, selector, value,
    address(value) - MIN(address(value)) OVER (PARTITION BY run_id, selector) AS from_base
  FROM evidence WHERE run_id IN (:a, :b) AND phase = 'observed' AND address(value) > 0)
SELECT x.hit AS hit_a, y.hit AS hit_b, x.from_base FROM a x
  JOIN a y ON y.run_id = :b AND y.phase = x.phase AND y.selector = 'q'
    AND y.from_base = x.from_base
  WHERE x.run_id = :a AND x.selector = 'p' ORDER BY x.hit" --param a=7 --param b=8
```

Keep only the rows of one moment and one place in each run, or the base and the pairs mix them:

- The SQL keeps `observed` rows. An `after-set` row, after a change, and a `(return)` row are other
  moments. A join to another selector's rows also needs `phase`.
- A line with several breakpoint locations gives each row a `stop` number. Keep one location per
  run: `AND (run_id != :a OR stop = 2)` in the CTE's `WHERE` keeps run a's location 2.
- `address(value) > 0` skips values that read negative, such as sentinels and pointers tagged in their
  top bit, so `MAP_FAILED` (-1) never becomes the base.

[Rust 4.8](lang/rust-cookbook.md#48-compare-addresses-across-runs) runs it on a real bug: pointers
that a sort left at their old slots.

Choose the base with care:

- It must be the same element in every run. A run's lowest address is the first element's only if
  that run saw the first element.
- Otherwise take a known element's address, such as the first hit's:
  `FIRST_VALUE(address(value)) OVER (PARTITION BY run_id, selector ORDER BY hit)`.
- Take it from addresses in one block of memory. A copy on the stack, or a global, is elsewhere.
- An offset is in bytes. Divide it by the element's size for its index. C and C++ rows give it as
  `element_size`.
- An offset holds only while the block stays put. A `Vec` or `std::vector` that grows may move.

Read an address with its row's view and note, as you read the value:

- `[unproven]` on a raw pointer is about what it points at. Its address is the pointer's own value.
- `[stale]` bytes are leftovers, not what the variable holds now.

SQLite's integers are signed. So an address at or above 2^63 comes back negative, as its 64-bit
two's complement. Such a value is a kernel's address, a tagged pointer, garbage, or a sentinel such
as `MAP_FAILED`: `0xffffffffffffffff` reads as -1. User-space addresses on macOS and Linux are far
below 2^63.

- Two such addresses still subtract and order correctly.
- Against an address below 2^63, SQLite's subtraction overflows into an inexact REAL.
- The recipe's `address(value) > 0` leaves them out. To compare such addresses, drop it, and take a
  known element's address as the base.
- `printf('0x%x', address(value))` gives the hex back. It prints NULL as `0x0`.

Peeky has no canned query for this: the base, and what pairs the runs, depend on the program.

C and C++ need it less. Capture the array with the pointer, at the same stop. Where Peeky proves
which element the pointer holds, its row says `[points at level[6]]`. The store keeps it in
`points_into` and `element_index`, and `diff` compares it
([C and C++ 4.8](lang/c-cpp-cookbook.md#48-compare-addresses-across-runs)). Where the array is a
pointer, the row gives only an offset from it by address, in `offset_from` and `element_offset`.

---

## 5. Step-by-Step Sessions

Use a session when you want several looks at one stop. Each command is a separate shell call. A
private worker keeps the program paused in between.

### 5.1 Stop, Look, Change, Finish

**What it answers:** What do the values look like at this stop, and what happens if I change one?

`session start` stops the program once. Then use these commands, and always close the session:

| Command | What it does |
| --- | --- |
| `session inspect` | Lists the locals. |
| `session get NAME …` | Reads values, and lists their children. |
| `session set NAME=VALUE` | Changes one value. |
| `session continue --to-exit` | Runs the program to its end. |
| `session report` | Shows the state and the program's output. |
| `session close` | Ends the session. |

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
session default (python): state: stopped at examples/pagination_bug.py:8 in get_page, hit 1 (paused 0.0 s)
stopped at examples/pagination_bug.py:8 in get_page, hit 1 (paused 0.12 s)
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
cleanup: nothing Peeky could identify was still running
```

#### What `inspect` and `get` show

- Session states and `inspect` name the stopped function: `in Ring::push(int)`,
  `in topk::insert_sorted::{{closure}}`.
- Scope headers are the debugger's own names: `[Locals]` for Python, `[Local]` for Java.
- A scope lists at most 50 entries, and says how many more it has. debugpy's groups count as
  entries. To read one past them, name it: `session get NAME`.
- `session get` on a list, dict or struct lists one level of children. Each child shows the
  selector that reads it.
- A child that has children of its own shows `(children: ...)`.
- A value 4 levels below a local, such as `self.history[-1][2]`, lists no children. That is the
  inspection's depth limit, and the row says so:
  `<children: none listed: this value is 4 levels below a local, …>`. Read the same object by a
  shorter path, if there is one: `self.items[2]`, where `[id 31]` shows it is the same dict.
- Each selector is read on its own. Two paths to one object each list its children, within the
  depth limit.

#### When a name has no value

`get` of a name that isn't there prints only why, such as `x: <not found; visible: ...>`. A note
at the end names the stop it was read at.

Any selector with no value makes the exit status 1. Every row is still printed, and a last line on
stderr names the selectors with no value:

```
$ peeky session get nosuch start
nosuch: <not found: 'nosuch' is neither a local nor a module global here; visible: items, page, page_size, start; module globals: the names in debugpy's function variables, special variables (select them by name)>
start: int = 3
note: this stop is in get_page at pagination_bug.py:8
peeky: no value for nosuch (each row above says why), so the exit status is 1
```

The note tells you where you are. For example, this note says that a comparator's `a` is not
`main`'s: `note: this stop is in main at sched.cpp:30`. In JSON, such a row has the stop's
`location`.

When several rows give the same `visible:` list, the later ones say `visible: as listed above`.
A probe's table does the same: `visible: as listed at hit 1`. JSON keeps every list whole.

#### Go on to the next stop, or use another session

- `session continue`, without `--to-exit`, runs to the next stop at the session's breakpoint
  ([5.2](#52-stepping-and-running-to-a-line)).
- A session that isn't `default` needs `--name` on every later command
  ([5.3](#53-several-sessions-at-once)).

#### Read a caller's frame

`--frame N` reads frame N of the stack. Frame 0, the default, is where the program stopped. Frame 1
is its caller. At most 10 frames are listed, and `session inspect --json` lists them under
`frames`.

```sh
peeky session start examples/corpus/mutable_default/program.py --at 3
peeky session inspect --frame 1
peeky session get --frame 1 add_tag.__defaults__
peeky session inspect --frame 2
```

```
session default (python): state: stopped at examples/corpus/mutable_default/program.py:3 in add_tag, hit 1 (paused 0.0 s)
stopped at examples/corpus/mutable_default/program.py:3 in add_tag, hit 1 (paused 0.12 s)
frame 1: <module> (examples/corpus/mutable_default/program.py:7)
[Locals]
  (special variables: debugpy's group; select its members by name)
  (function variables: debugpy's group; select its members by name)
frame 1: <module> (examples/corpus/mutable_default/program.py:7)
add_tag.__defaults__: tuple = (['a'],)  [id 24]
  add_tag.__defaults__[0]: list = ['a']  [id 79] (children: peeky session get 'add_tag.__defaults__[0]' --frame 1)
  len() [not selectable]: int = 1
peeky: [INVALID_REQUEST] Frame 2 is out of range: this stop lists 2 frame(s), 0 to 1 (0 is the innermost; at most 10 are listed)
  hint: Fix the request arguments; see `peeky <command> --help`.
```

- A frame past the stack is refused with `INVALID_REQUEST`, which says how many frames there are.
- A caller's values can't be changed. `session set` takes no `--frame`, and a change to a caller's
  value is refused with `MUTATION_REJECTED`. So caller listings never say `[settable]`.
- Rust's proofs for owned locals and `Box`es hold only in the innermost frame. So a caller's `Vec`
  keeps its raw fields, with a note.

**Per language:**

- [Python 5.1](lang/python-cookbook.md#51-stop-look-change-finish): globals, a name missing from
  the locals, a pass's own value, module level.
- [C and C++ 5.1](lang/c-cpp-cookbook.md#51-stop-look-change-finish): a caller's local still being
  initialized.

### 5.2 Stepping and Running to a Line

**What it answers:** What happens line by line from here, inside a call, or a few lines further on?

| Command | Runs |
| --- | --- |
| `session step` | to the next line, over calls |
| `session step --into` | into the call on this line |
| `session step --out` | until this function returns |
| `session continue` | to the next stop |
| `session continue --to LINE` | once, to a line |
| `session continue --to-exit` | to the program's end |

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
session default (python): state: stopped at examples/pagination_bug.py:13 in <module>, hit 1 (paused 0.0 s)
state: stopped at examples/pagination_bug.py:5 in get_page (paused 0.0 s)
note: now in `get_page`, one call deeper, called from `<module>`
state: stopped at examples/pagination_bug.py:7 in get_page (paused 0.0 s)
state: stopped at examples/pagination_bug.py:8 in get_page (paused 0.0 s)
start: int = 3
state: stopped at examples/pagination_bug.py:13 in <module> (paused 0.0 s)
note: left `get_page`; now in `<module>`
state: terminated (program exited; `peeky session report` shows output)
```

A step can land on a closing `}`, on a loop's header, or back on an `if`'s line
([1.1](#11-what-can-i-ask)). So read a block's locals before you step past it.

#### When a step lands in another function

A step off a function's last line lands in its caller, where the function's names are not
visible. So the state then has a note that says how the stack changed:

- ``note: left `get_page`; now in `<module>` ``: the stack is shallower, and the stop is in a
  function that was below. With `, 2 calls up`, it is two frames shallower.
- ``note: now in `walk`, one call deeper, called from `run` ``: the stack is deeper, over the
  function the step started in. With more frames, it says ``3 calls deeper, below `run` ``.
- ``note: left `add`; now in `shift` ``: the function is no longer at its depth, and the stack
  changed below it too.

The note never says that a function returned: it may have thrown, yielded or been unwound. Where
a function did return, `step --out` shows its value with the stop, except in Python
([2.9](#29-what-a-function-returned)).

Depth counts, not only names. So a recursive call gets a note too: ``now in `walk`, one call
deeper, called from `walk` ``. Only a step's own end gets a note. A `continue`, or a step that a
breakpoint, a crash or a pause stopped first, gets none. The program may have left the call and
made another that looks the same: the stacks can't tell two calls of a function apart. Nor does
a stack deeper than 256 frames get a note, because it is not read whole.

No note doesn't prove the same call. In C, C++ and Rust, an exception or `longjmp` can end a
stepped call, and LLDB's step then ends in a later call from the same place.

A `get` with no value at such a stop repeats the note:

```
$ peeky session start examples/pagination_bug.py --at 9
session default (python): state: stopped at examples/pagination_bug.py:9 in get_page, hit 1 (paused 0.0 s)
$ peeky session step
state: stopped at examples/pagination_bug.py:13 in <module> (paused 0.0 s)
note: left `get_page`; now in `<module>`
$ peeky session get 'items[0]'
items[0]: <not found; visible: the names in debugpy's function variables, special variables (select them by name)>
note: this stop is in <module> at pagination_bug.py:13, after a step from `get_page`
```

In JSON, the state has `call_change`, and so does a `get` row with no value at that stop. It
holds the `kind` (`left`, `deeper` or `other`), the two functions as `from` and `to`, and `calls`:
how many frames shallower or deeper.

#### When a step leaves a block

In C, C++, Rust and Java, a local lives only in its block. A step past a block's last statement
leaves the block, often for its loop's header. A `get` of a local the block declared then says
why it is gone:

```
$ peeky session start examples/java --config examples/java/balance.json --at demo/BalanceBug.java:30
$ peeky session get best
best: BalanceBug$Worker = BalanceBug$Worker@56 {load=5, name="a"}
...
$ peeky session step
state: stopped at demo/BalanceBug.java:20 in BalanceBug.assign(List,int[]) (paused 0.0 s)
$ peeky session get best.load
best.load: <not found: 'best' was visible at line 30, earlier in this same call, but the block declaring it has ended (read it at a line inside that block); visible: order, tasks, workers>
```

- The message needs a read at that earlier stop: `inspect`, or a `get` of any name. Peeky
  doesn't read every stop's locals, because some debuggers run the program's code to show them.
- Only a step's own end gets the message, as with the notes above.
- In C, C++ and Rust, the message doesn't say "this same call", because LLDB's step can end in a
  later call. It says only that this stop is outside the local's scope:
  `'square' was visible at line 14, at an earlier stop in this function, and this stop is outside
  its scope`.
- Python has no block scopes. A local that is gone was deleted, and its message says it has no
  value.

#### Run to a line with `continue --to`

`session continue --to 9` stops once at line 9 of the session's file. The extra breakpoint is
removed at the next stop. Then the session's own breakpoint applies again.

`--to` doesn't skip the session's breakpoint. If a stop there comes first, as in a loop, the state
says so and the exit status is 3. Repeat the command: every stop is counted. There is no option to
skip the session's breakpoint.

For example, in `examples/corpus/running_max/program.py`, line 5 is in a loop and line 7 is after
it:

```
$ peeky session start examples/corpus/running_max/program.py --at 5
session default (python): state: stopped at examples/corpus/running_max/program.py:5 in running_max, hit 1 (paused 0.0 s)
$ peeky session continue --to 7
state: stopped at examples/corpus/running_max/program.py:5 in running_max, hit 2 (paused 0.0 s)
note: stopped at the session's breakpoint (line 5, hit 2) before reaching line 7; `session continue --to 7` again
$ echo $?
3
```

- A program that exits first gives exit status 3 too, with this note:
  `the program exited without reaching line 17`.
- `continue --to` refuses a line the debugger would move, because it has no code. The session keeps
  its stop, so you can choose another line:

  ```
  $ peeky session continue --to 8
  peeky: [INVALID_BREAKPOINT] Line 8 has no code to stop at (the debugger would use line 7); choose a line from `peeky describe`
    hint: Choose a line from `peeky describe` breakpoint_lines.
  $ peeky session continue --to 7
  state: stopped at examples/corpus/running_max/program.py:7 in running_max (paused 0.0 s)
  ```

- `session start` at such a line starts all the same, at the line the debugger chose. Then it
  warns, as a probe does:

  ```
  $ peeky session start examples/pagination_bug.py --at 10
  session default (python): state: stopped at examples/pagination_bug.py:9 in get_page, hit 1 (paused 0.0 s)
  warning: The debugger moved the breakpoint from line 10 to line 9 (line 10 has no code of its own); values are from line 9. Choose a line from `peeky describe`.
  ```

#### Count stops with `--hit` and `--count`

- State lines number the stops at the session's breakpoint: `…:8 in get_page, hit 3`.
- A line can have several breakpoint locations in the function. State lines then also say which
  one a stop is at: `hit 4, stop 1 of 3` (see `--hits` in [2.2](#22-every-iteration-of-a-loop)).
- `session start --hit 5` runs on to the 5th stop.
- `session continue --count 3` runs on to the 3rd breakpoint stop from here.
- Both run within one `--timeout`.

They never run past anything else. A crash, a finished step or the program's exit ends them there.
A note says why, and names the last hit reached, if any. The exit status is 3, as when `--timeout`
ends them first:

```
$ peeky session start examples/corpus/running_max/program.py --at 5 --hit 5
session default (python): state: terminated (program exited; `peeky session report` shows output)
note: the program exited before hit 5; last hit 3
```

#### When a step leaves your sources

A step can leave your sources: into a library, into Rust's standard library, or past the end of
`main`. Then:

- The state says `outside the program's sources`.
- Nothing there is read or changed. The one exception is what your function returned when the step
  left it ([2.9](#29-what-a-function-returned)).
- To get back, use `step --out` or `continue`.
- In C, C++ and Rust, `step --out` leaves one frame at a time, also in a library. While your code
  is still on the stack, `step --out` returns to it.
- Past `main`, the state says so, and `continue` finishes.

#### When the session's breakpoint stops a step

A step's code can reach the session's breakpoint first. Examples are a recursive call on the line,
or the rest of the function under `step --out`. The step then stops there, and the state says so
with `hit 2` and this note:

```
note: the session's breakpoint (hit 2) stopped the step before it ended, and the step is over
```

`step --out` then shows no return, because the function hasn't returned.

#### Steps with several threads

- Other threads may be stopped at the breakpoint too. LLDB stops them together; Java stops each one
  separately. Then `step` waits until `continue` has served them.
- A step that a breakpoint or a crash stops first is over. In C, C++ and Rust, the next `continue`
  runs to the next breakpoint, not to where the step would have ended.
- Another thread's stop can catch the stepping thread mid-step. LLDB may then report a step's end
  anywhere the thread was, such as inside `usleep`. The state says `the step was interrupted here`,
  and `continue` serves that thread's stop.

**Per language:** [Java 5.2](lang/java-cookbook.md#52-stepping-and-running-to-a-line) (`--into`
and `--out` skip JDK classes).

### 5.3 Several Sessions at Once

**What it answers:** How do I keep two stops open at once?

Give each session a name with `--name`, and pass the same `--name` to every later command:
`inspect`, `get`, `set`, `step`, `continue`, `report` and `close`.

```sh
peeky session start examples/pagination_bug.py --at 8 --name a
peeky session start examples/corpus/running_max/program.py --at 5 --name b
peeky session get start --name a
peeky session get best --name b
peeky session list
peeky session close --name a
peeky session close --name b
```

```
session a (python): state: stopped at examples/pagination_bug.py:8 in get_page, hit 1 (paused 0.0 s)
session b (python): state: stopped at examples/corpus/running_max/program.py:5 in running_max, hit 1 (paused 0.0 s)
start: int = 3
best: int = 0
a
b
closed a
cleanup: terminated pid 63598
closed b
cleanup: terminated pid 63628
```

- `peeky session list` shows the names in use.
- A command without `--name` acts on the session named `default`, or says there is none:

  ```
  $ peeky session get best
  peeky: [NO_SESSION] No session named 'default'; start one with `peeky session start`
    hint: Start a new session (launch or `peeky session start`).
  ```

- A second `session start` with a name already taken fails, and never takes over that session:

  ```
  $ peeky session start examples/pagination_bug.py --at 7 --name a
  peeky: [INVALID_REQUEST] Session 'a' exists; close it or pass --name
    hint: Fix the request arguments; see `peeky <command> --help`.
  ```

- With one session at a time, leave `--name` out everywhere.

### 5.4 Programs with Timeouts

**What it answers:** How do I debug a program whose clocks keep running while it is paused?

Tests with timeouts, heartbeats and RPC deadlines keep their clocks running while the program is
paused. Prefer probes, which pause for milliseconds per hit. Or give a session a pause budget with
`--max-pause`:

```sh
peeky session start app.py --at 12 --max-pause 5      # auto-resume after 5 s
peeky session inspect                                   # shows how long it has been paused
```

The state shows the time left, and JSON has `paused_s` and `resume_in_s`. A breakpoint or step stop
older than the budget is resumed, and your next call says so (`STALE_STOP`). For example, with
`--max-pause 3`:

```
$ peeky session start examples/corpus/running_max/program.py --at 5 --max-pause 3
session default (python): state: stopped at examples/corpus/running_max/program.py:5 in running_max, hit 1 (paused 0.0 s, auto-resume in 3.0 s)
$ sleep 4
$ peeky session get best
peeky: [STALE_STOP] Stale stop_id: the program was auto-resumed after its 3.0 s pause budget (--max-pause); wait for the next stop or use a probe
  hint: Call wait until state is stopped, then inspect the latest stop.
```

Crash and exception stops are never auto-resumed.

### 5.5 Cleaning Up

A session's worker ends:

- with `session close`;
- when its start fails or is interrupted;
- after 5 idle minutes.

`peeky session close --forget` drops a record whose worker is gone for good.

---

## 6. Verifying a Fix

### 6.1 Check One Edit on a Fresh Build

**What it answers:** Does this change pass the independent checks, on a clean copy?

`verify` applies the edit to a scratch copy, builds it fresh, and runs your checker on it. Your
files don't change.

```sh
peeky verify examples/pagination_bug.py --check examples/verify_pagination.py \
  --old 'start = page * page_size' --new 'start = (page - 1) * page_size'
```

```
passed: True  stage: verification  seconds: 0.035
checker exit: 0
output:
PASS: 12 independent pagination checks

diff:
--- pagination_bug.py
+++ pagination_bug.py
@@ -4,7 +4,7 @@
 def get_page(items, page, page_size):
     if page < 1 or page_size < 1:
         raise ValueError("page and page_size must be positive")
-    start = page * page_size
+    start = (page - 1) * page_size
     end = start + page_size
     return items[start:end]
```

- To see what an edit does before you verify it, a candidate probe is simpler
  ([6.6](#66-see-what-a-fix-does-before-making-it)).
- To keep the edited file, add `--keep`. It stays in the system's temporary directory, with a
  compiled program's build evidence beside it. Build artifacts are always removed.
- The result prints the file's path after `candidate:`, and JSON names its directory in `kept`.
  Delete it when you are done.
- For a compiled program, `--json` identifies the checker's descriptor and the build by hash:
  `descriptor_sha256` and `artifact_manifest_id`. `--details` adds both in full.

#### A Cargo crate's tests

For a Cargo crate, its compiled tests run too. A failure names each failed test, and how it
failed:

```
$ peeky verify examples/rust --check examples/rust/verify_pagination.py --baseline
passed: False  stage: verification  seconds: 1.379
compiled tests: failed (1 failed)
  first_page_starts_at_zero: thread 'first_page_starts_at_zero' (67900711) panicked at tests/pagination.rs:10:5:
    assertion `left == right` failed
      left: "PAGE:[40, 50, 60]\n"
     right: "PAGE:[10, 20, 30]\n"
reason: AssertionError: (1, 3, 8, [40, 50, 60], [10, 20, 30])
checker exit: 1
baseline: the unedited program, no edit (the checker rejects it, as it should reject the bug)
```

#### Does the checker catch the bug?

Before you trust `passed: True`, run the checker on the unedited program with `--baseline`, and no
`--old`/`--new`. It builds and checks the program as it is, like any verify. It should fail:

```sh
peeky verify examples/pagination_bug.py --check examples/verify_pagination.py --baseline
```

```
passed: False  stage: verification  seconds: 0.033
reason: AssertionError: (1, 3, [10, 20, 30], [40, 50, 60])
checker exit: 1
baseline: the unedited program, no edit (the checker rejects it, as it should reject the bug)
```

A checker that passes here can't tell a fix from the bug.

An `--old`/`--new` pair that changes nothing is refused. `--baseline` is the way to check the
original:

```
peeky: [INVALID_REQUEST] --new equals --old, so the edit changes nothing (to run the unedited program, leave out --old/--new; to check it, `verify --baseline`)
  hint: Fix the request arguments; see `peeky <command> --help`.
```

#### Where `reason` comes from

`reason` says why the checker failed:

- If the checker raised an exception, `reason` is that exception, as above.
- If it only exited non-zero (`print("FAIL ..."); sys.exit(1)`), `reason` is the first output line
  that starts like a failure. Up to two indented lines after it are added.
- If no line starts like a failure, `reason` is the last line: `mismatch: wrap got 6 want 7`.
- Peeky reads stdout, or stderr when stdout is empty.

A line starts like a failure when it starts with one of these:

| Start | Note |
| --- | --- |
| `FAIL`, `FAILED` | |
| `E` | pytest's |
| `…Error:`, `…Exception:` | |
| `assert` | |
| unittest's `FAIL:` header | with the exception it reports; not its `FAILED (failures=1)` |

For example, `reason: FAIL ['+1', '-']: got 'POPPED:' want 'POPPED:1'`, or
`FAIL first case; expected 3; got 2`. This `fail_check.py` prints two lines and exits 1:

```python
import runpy
import sys

get_page = runpy.run_path(sys.argv[1])["get_page"]
got = get_page([10, 20, 30, 40], 1, 2)
if got != [10, 20]:
    print("checking page 1")
    print(f"FAIL page 1: got {got} want [10, 20]")
    sys.exit(1)
print("PASS")
```

```
$ peeky verify examples/pagination_bug.py --check fail_check.py --baseline
passed: False  stage: verification  seconds: 0.033
reason: FAIL page 1: got [30, 40] want [10, 20]
checker exit: 1
output:
checking page 1
FAIL page 1: got [30, 40] want [10, 20]

baseline: the unedited program, no edit (the checker rejects it, as it should reject the bug)
```

A checker's `SystemExit` code N can differ from the `checker exit` status. Then `reason` adds
`(SystemExit: N)`. In JSON, `checker.error` keeps the exception itself, such as `SystemExit: 1`.

### 6.2 An Overfit Fix Is Rejected

**What it answers:** Does a fix that only covers the reported case pass?

No: the checker's other cases fail.

```sh
peeky verify examples/pagination_bug.py --check examples/verify_pagination.py \
  --old 'start = page * page_size' --new 'start = 0 if page == 1 else page * page_size'
```

```
passed: False  stage: verification  seconds: 0.034
reason: AssertionError: (2, 3, [40, 50, 60], [70])
checker exit: 1
diff:
...
-    start = page * page_size
+    start = 0 if page == 1 else page * page_size
...
```

### 6.3 Multi-Line Edits

**What it answers:** How do I pass an edit that spans lines?

Put the texts in files, and pass them with `--old-file` and `--new-file`:

```sh
peeky verify app/ --at lib/parse.py --check checks/parse_check.py --old-file old.txt --new-file new.txt
```

`--old` must match the source exactly once. Otherwise the error says how many times it matched,
and where:

```
peeky: [INVALID_REQUEST] --old matched 5 times (lines 4, 5, 6, 7, 8); it must match exactly once: include more surrounding text
  hint: Fix the request arguments; see `peeky <command> --help`.
```

- Peeky reads the files byte for byte. Shell `$(cat file)` drops trailing newlines, so use the
  `-file` options instead.
- On the command line, pass real newlines for multi-line text. In bash: `--old $'a\n    b'`.

### 6.4 Several Edits at Once

**What it answers:** How do I check a fix that touches more than one place?

A general fix often touches more than one place, such as a new field and the function that uses
it. Repeat the `--old`/`--new` pair, once per edit:

```sh
peeky verify bucket.c --check check.py \
  --old 'long tokens;' --new 'long tokens;
  long milli;' \
  --old 'long refill = elapsed / 1000 * b->rate;' --new 'long refill = (b->milli + elapsed * b->rate) / 1000;'
```

- Each `--old` quotes the original file, and must match it exactly once.
- Their order doesn't matter.
- Edits must not overlap:

  ```
  peeky: [INVALID_REQUEST] Edits 1 and 2 overlap: each --old must quote its own part of the original file
  ```

- The `-file` forms repeat the same way:
  `--old-file a.txt --new-file b.txt --old-file c.txt --new-file d.txt`.
- Up to 32 edits are allowed. An error names the edit by its number:

  ```
  peeky: [INVALID_REQUEST] --old matched 0 times (edit 2); it must match exactly once: copy the exact text, including indentation
  ```

- Candidate probes take the same pairs. `--at` keeps the original's numbering, mapped through every
  edit ([6.6](#66-see-what-a-fix-does-before-making-it)).

### 6.5 Writing a Checker

**What it answers:** What must a checker do?

A checker is a trusted script, run as `python checker.py <candidate-path>`. It exits 0 only when
the behavior is right.

- Test the general rule, not the one input from the bug report. See
  `examples/verify_pagination.py`.
- For Python, the argument is the edited file. `examples/verify_pagination.py` loads it with
  `runpy.run_path`.
- For C, C++, Rust and Java, the argument is the path of a JSON descriptor of the fresh build.
  `spec["language"]` names the language.
- Run each case as `[*spec["command"], *case_arguments]`, with `env=spec["environment"]`. From
  `examples/c/verify_pagination.py`:

  ```python
  spec = json.loads(Path(sys.argv[1]).read_text())
  ...
  result = subprocess.run(
      [*command, str(page), str(size), str(total)],
      capture_output=True,
      text=True,
      timeout=5,
      check=False,
      env=spec["environment"],
  )
  ```

- For Rust, the crate's integration test must pass too, if it has one.

How the investigation loop uses a checker: [AGENT_LOOP.md](AGENT_LOOP.md#the-checker-contract).

### 6.6 See What a Fix Does Before Making It

**What it answers:** With this edit, what would the program compute at the same line, and what would
it print?

Run the same probe twice: once as it is, and once with `--old`/`--new`. The second builds a fresh
copy with the edit. Your files don't change. Then compare the two runs with `queries diff`.

```sh
peeky probe examples/c/pagination_bug.c --at 25 --capture start --arg 2
peeky probe examples/c/pagination_bug.c --at 25 --capture start --arg 2 \
  --old 'int start = page * page_size;' --new 'int start = (page - 1) * page_size;'
peeky queries diff
```

```
hit  phase     line  function  selector  type  value
1    observed  25    main      start     int   6  [settable]
status: completed  hits: 1  seconds: 1.013  max pause: 56.9 ms
output:
  PAGE:[70, 80]
evidence: saved as run 1; query it with `peeky queries run --param run=1` or `peeky sql` (see `peeky sql --schema`)
hit  phase     line  function  selector  type  value
1    observed  25    main      start     int   3  [settable]
status: completed  hits: 1  seconds: 0.99  max pause: 66.9 ms
output:
  PAGE:[40, 50, 60]
candidate: values are from a fresh build with this edit; your files are unchanged
candidate: In the edited file, locations name the candidate's copy, since the line numbers are the candidate's
--- pagination_bug.c
+++ pagination_bug.c
@@ -21,7 +21,7 @@
         (argc > 3 && !parse_count(argv[3], 0, &total))) return 2;
     int items[MAX_PAGE_ITEMS];
     for (int i = 0; i < total; i++) items[i] = (i + 1) * PAGE_ITEM_MULTIPLIER;
-    int start = page * page_size;
+    int start = (page - 1) * page_size;
     int end = start + page_size; /* OBSERVE */
     if (end > total) end = total;
     printf("PAGE:[");
evidence: saved as run 2; query it with `peeky queries run --param run=2` or `peeky sql` (see `peeky sql --schema`)
compare: peeky queries diff --param a=1 --param b=2 (run 1: this probe without the edit)
run_a  run_b  hit   phase     selector  value_a           value_b               differs  note
1      2      NULL  NULL      (output)  PAGE:[70, 80]\n  PAGE:[40, 50, 60]\n  1        output differs at line 1, column 7
1      2      NULL  NULL      (edit)    none              candidate             1        the runs built different edits (runs.edit has each diff)
1      2      1     observed  start     6                 3                     1        NULL
(3 rows)
```

This is evidence about one run, not a verification. Only `peeky verify` checks a fix.

#### Which line a candidate stops at

`--at` uses the original file's numbering, so the same command works before and after the edit. A
line after an edit that adds or removes lines is moved to match:

```
candidate: values are from a copy of your tree with this edit; your files are unchanged (line 8 is line 9 of the candidate)
candidate: In the edited file, locations name the candidate's copy, since the line numbers are the candidate's
```

In the edited file, locations, frames and the breakpoint name the candidate's copy, so that they
agree with these line numbers. A compiled target's other files are shown as yours. A Python
candidate's are in its copy of your tree.

Some lines have no faithful match. Peeky refuses them before anything is built:

- a line inside an edit that adds or removes lines:

  ```
  peeky: [INVALID_REQUEST] Line 7 is inside the edit (line 7, which becomes lines 7-8 of the candidate). An edit that changes the number of lines has no matching line inside it: probe a line before or after the edit
  ```

- the line right after an edit whose `--old` ends with a newline but whose `--new` doesn't. The
  candidate joins that line onto the edit.

#### When the edit doesn't build or run

- An edit that doesn't compile fails with `BUILD_FAILED` and the compiler's message.
- A Python edit with a syntax error fails with `NO_STOP`, with the `SyntaxError` in the output.

#### Compare with the same probe without the edit

A plain `queries diff` compares the newest run with the one before it, whatever that was. So after
a candidate probe, a `compare:` line names the newest saved run that is the same probe without the
edit, as above. JSON has it in `evidence.compare`.

"The same probe" means all of these:

- the same target, `--at`, `--capture` and program arguments;
- the same build, by its target identity: compiler options, optimization and sources;
- every source the build read, as it was when the edit was made. That includes headers,
  `Cargo.toml` and `Cargo.lock`, and in Python every `.py` file under the target;
- no edit, and no `--set` or `--set-at`.

A `--label` doesn't count. When no such run is saved, the line says how to make one:

```
compare: no saved run is this probe without the edit (the same --at, --capture, program arguments and build, of the sources as they are now, with no --set or --set-at): run it without --old/--new, then `peeky queries diff`
```

After a candidate with `--set` or `--set-at`, run it without them too.

The line names no run in these cases:

- **Other breakpoint locations:** the run's hits are at other breakpoint locations of the line.
  `stop` differs at a hit both runs reached, as when the edit changes how often the line stops per
  pass. The line says so, and names `queries join`, which pairs by value.
- **Older runs:** runs saved before Peeky recorded what they read are never named.
- **When Peeky can't tell:** a run where Peeky can't tell what it read is never named either. An
  example is a Python tree past 4096 `.py` files or 16 MB.

In the last two cases, run the probe without the edit again.

#### How candidate runs are stored

- The run's `edit` column holds the diff. Its `source_sha256` is the edited text's.
- `queries runs` marks the run `(candidate)`. The `evidence` and `last_run` views have a
  `candidate` column.
- Captured `line`s are in the candidate's own numbering.
- `queries values` leaves candidate runs out, so they never mix with values the real program
  produced.
- `queries diff` pairs values by hit and selector, so a candidate whose lines moved still
  compares.

#### Change a value that `--set` can't reach

In Java, Rust, C and C++, `--set` changes a scalar field or element directly: `--set s.end=10`,
where `s` is a struct local. See [C and C++ 7.1](lang/c-cpp-cookbook.md#71-c-and-c) and
[Rust 7.2](lang/rust-cookbook.md#72-rust-a-cargo-crate) for which ones. `--set` also works in the
same probe as `--old`/`--new`.

Some values can't be set: a Python field, or one reached through a pointer, a reference or a `Box`.
Then add a local that holds the value, with the edit, on an earlier line, and set that local. A
local is visible only after its declaration.

For example, in this Rust `spans` crate, `last.end` can't be set on line 23, because `last` is a
`&mut Span`:

```rust
fn merge(spans: &[Span]) -> Vec<Span> {
    let mut sorted = spans.to_vec();
    sorted.sort_by_key(|s| s.start);
    let mut out: Vec<Span> = Vec::new();
    for s in sorted {
        if let Some(last) = out.last_mut() {
            if s.start <= last.end {
                last.end = s.end;
                continue;
            }
        }
        out.push(s);
    }
    out
}
```

So the edit adds `end`, and the probe sets it:

```sh
peeky probe spans --at src/main.rs:23 --capture end --arg 1-10,2-3 \
  --old 'if s.start <= last.end {' \
  --new 'let mut end = s.end; std::hint::black_box(&mut end); if s.start <= last.end {' \
  --old 'last.end = s.end;' --new 'last.end = end;' --set end=10
```

```
hit  phase      line  function      selector  type  value
1    observed   23    spans::merge  end       u32   3  [settable]
1    after-set  23    spans::merge  end       u32   10  [settable]
set end: 3 -> 10
status: completed  hits: 1  seconds: 1.639  max pause: 142.2 ms
output:
  MERGED:1-10
candidate: values are from a fresh build with this edit; your files are unchanged
candidate: In the edited file, locations name the candidate's copy, since the line numbers are the candidate's
...
```

This reports `set end: 3 -> 10`, and the program prints `MERGED:1-10`. Without the change, it
prints `MERGED:1-3`. `black_box` keeps the new Rust local in memory, where a change reaches the
program.

**Per language:**

- [Rust 6.6](lang/rust-cookbook.md#66-see-what-a-fix-does-before-making-it): a local kept in a
  register.
- [Python 6.6](lang/python-cookbook.md#66-see-what-a-fix-does-before-making-it): what a Python
  candidate's copy holds.

---

## 7. C, C++, Rust and Java

The core command pattern is shared; targets and supported operations differ by language. Each
language's own sections are in its cookbook.

### 7.1 C and C++

In [C and C++ 7.1](lang/c-cpp-cookbook.md#71-c-and-c): read, set, withheld locals, strings and changing their text, maps, what `set` checks.

### 7.2 Rust (a Cargo crate)

In [Rust 7.2](lang/rust-cookbook.md#72-rust-a-cargo-crate): values read from memory, settable locals and texts, crate shape.

### 7.3 Java (launched)

In [Java 7.3](lang/java-cookbook.md#73-java-launched): configs, previews, fields and Strings that can be set.

### 7.4 Java Tests Already Running (Gradle `--debug-jvm`, e.g. Venice)

In [Java 7.4](lang/java-cookbook.md#74-java-tests-already-running-gradle---debug-jvm-eg-venice): attach to a `--debug-jvm` JVM.

### 7.5 Optimized Builds

To debug the optimized program, set its level in a `--config`:

| Language | Key | Levels |
| --- | --- | --- |
| C, C++ | `optimization` | `"Og"`, `"O1"` or `"O2"`; `"O0"` by default |
| Rust | `opt_level` | `"1"` or `"2"`; `"0"` by default |

A C or C++ config may also name explicit tool paths
([C and C++ 7.1](lang/c-cpp-cookbook.md#name-the-tools-in-a-config)). For example, this
`examples/c/optimized.json` names them:

```json
{"sources": ["pagination_bug.c"], "headers": ["pagination.h"], "optimization": "O2", "compiler": "/Library/Developer/CommandLineTools/usr/bin/clang", "adapter": "/Library/Developer/CommandLineTools/usr/bin/lldb-dap", "dwarfdump": "/usr/bin/dwarfdump", "dsymutil": "/Library/Developer/CommandLineTools/usr/bin/dsymutil", "sysroot": "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"}
```

```sh
peeky probe examples/c --config examples/c/optimized.json --at pagination_bug.c:25 \
  --capture page,page_size,start
```

```
hit  phase     line  function    selector   type  value
1    observed  25    main [opt]  page       int   1
1    observed  25    main [opt]  page_size  int   3
1    observed  25    main [opt]  start      int   3
status: completed  hits: 1  seconds: 0.994  max pause: 37.7 ms
warning: Optimized build (-O2): values that are shown can still be stale or wrong (the debugger cannot tell). Confirm with an unoptimized build before trusting them.
output:
  PAGE:[40, 50, 60]
...
```

What changes in an optimized build:

- Locals may be `unavailable`. LLDB reports such a local as text, such as
  `<error: variable not available>` or `<error: could not evaluate DW_OP_entry_value: ...>`. Peeky
  gives it no value, an error that quotes the debugger's reason, and a warning.
- Values that are shown can be stale, and look normal. An inlined `-O2` C function reported
  `b=2, t=8` where the live values were 0 and 3. At Rust's `opt-level 2`, `start` read `1` where
  the program's value was `3`, with no `unavailable` mark. Peeky can't detect this, so prefer an
  unoptimized build for debugging.
- In this macOS C example, the `function` column marks the frame `[opt]`. Only lldb-dap on macOS
  adds that mark. Linux's never does, and Rust function names have it removed.
- Every probe result, session snapshot, `session get` row and `describe` carries `optimized` and a
  warning.
- A Rust enum is opaque, because LLDB zero-fills the enum pieces it can't locate.
- Changes are refused. A probe's `--set` is refused before the program runs. The error says when
  the debug information gives the local no stack slot (a register or a location list):

```sh
peeky probe examples/c --config examples/c/optimized.json --at pagination_bug.c:25 \
  --capture page,page_size,start --set start=0
```

```
error [MUTATION_REJECTED]: `start` cannot be changed: changing values is refused in an optimized build (-O2), where the program may keep using other copies of a value; here the debug information gives `start` no stack slot (a register or a location list), so a change could not reach every copy. Refused before the program runs: the debugger's launch/attach request was not sent. Use an unoptimized build
hint: Use an unoptimized build to change values.
```

- These rules apply to C, C++ and Rust, which all use LLDB. Peeky never reinterprets the value text
  of another language's debugger.
- In an unoptimized build, a value that the debugger can't read, such as one behind a NULL
  pointer, shows the debugger's reason. It is never called "optimized out".

---

## 8. Slow Programs and Tool Timeouts

If a probe or a `verify` may outlast your tool's timeout, add `--background`. Then collect the
result with `peeky job wait`:

```sh
peeky probe examples/pagination_bug.py --at 8 --capture start --background
```

```
job e85df1538ef6ffc0 started; collect it with `peeky job wait e85df1538ef6ffc0`
```

```sh
peeky job wait e85df1538ef6ffc0 --timeout 20
```

```
peeky: started; breakpoint pagination_bug.py:8 verified
peeky: waiting for the program to reach it...
peeky: hit 1/1 at pagination_bug.py:8; capturing
peeky: all requested hits captured; letting the program finish
peeky: program finished after 1 hit(s)
hit  phase     line  function  selector  type  value
1    observed  8     get_page  start     int   3
status: completed  hits: 1  seconds: 1.069  max pause: 10.7 ms
output:
  PAGE: [40, 50, 60]
...
```

- `--background` works for `probe` and `verify` only.
- While the job is still running, `peeky job wait` exits with status 3. Once the job has finished,
  it prints the job's exact output.
- `peeky job list` lists the jobs from the last 24 hours:
  `e85df1538ef6ffc0  probe  finished  1.1 s`.

Other bounds:

| Bound | Default | At most | What it does |
| --- | --- | --- | --- |
| A probe's `--timeout S` | 60 s | 300 s | Bounds the whole probe, build included |
| A `session` command's `--timeout S`, `session start` included | 30 s | 300 s | Bounds how long the command waits for the program. A program still running is left running, and `session wait` waits again |
| `--hits N` | | 100 | `--hits all` captures every stop, up to 100, lowered so every hit fits the 2,000-row budget. After-set rows count toward it ([2.2](#22-every-iteration-of-a-loop)) |
| `--until hits` | | | Skips waiting for the program to exit |
| `peeky job wait`'s `--timeout S` | 20 s | 300 s | Bounds how long it waits for the job |

---

## 9. A Program on Another Host

Run the worker next to the program, and reach it through an SSH tunnel.

On the host, make a token and start the worker:

```sh
export DEBUG_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
python3 -m peeky.worker --root /srv/app --port 8765
```

On your machine, with the same sources, open the tunnel and give the same token:

```sh
ssh -N -o ExitOnForwardFailure=yes -L 8765:127.0.0.1:8765 you@host &
export PEEKY_WORKER_TOKEN='<the token>'
peeky probe ./app/job.py --at 42 --capture rows --worker http://127.0.0.1:8765
peeky session start ./app/job.py --at 42 --worker http://127.0.0.1:8765
```

The worker's `--root` holds the host's copy of the target's directory. So `./app/job.py` on your
machine is `/srv/app/job.py` on the host. A stop in a compared source names your copy:
`/srv/app/job.py:42` shows as `app/job.py:42`. Any other path is the host's, marked `worker:`
([REMOTE.md](REMOTE.md#paths-in-results)).

The probe's output looks as it does locally. For example, with `job.py` a copy of
`examples/pagination_bug.py`, and the worker on the same machine without a tunnel:

```sh
peeky probe ./app/job.py --at 8 --capture start --worker http://127.0.0.1:8765
```

```
hit  phase     line  function  selector  type  value
1    observed  8     get_page  start     int   3
status: completed  hits: 1  seconds: 1.058  max pause: 6.3 ms
output:
  PAGE: [40, 50, 60]
...
```

If the host's copy of any source differs from yours, the worker refuses before running anything:

```
error [PROVENANCE_CHANGED]: Worker source provenance differs for the program job.py: refused before launching. Sync sources before debugging remotely
hint: Sources, build outputs or tools changed; start a new session on a fresh build.
```

- Every source is compared: for Python, every `.py` file under the directory, except in virtual
  environments. A file on one side only is refused too, by name
  ([REMOTE.md](REMOTE.md#source-identity)).
- For Java, start the worker with `--java-config`, or `--java-attach-config` for a waiting JVM.
  Pass your own `--config` too ([REMOTE.md](REMOTE.md#java-on-the-worker-host)).
- For C and C++, start the worker with `--native-config`; for Rust, with `--rust-config`. You need
  no `--config` on your machine ([REMOTE.md](REMOTE.md#c-c-and-rust-on-the-worker-host)).
- A dropped tunnel gives `UNREACHABLE`. Reconnect and continue: the worker keeps the session.
- To try this without a second machine, run `PEEKY_E2E_DOCKER=1 pytest tests/remote -v`. It runs
  against a Linux container, and needs Docker or OrbStack.
- [REMOTE.md](REMOTE.md) has the recorded run.

---

## 10. When Something Goes Wrong

An error carries a stable `code`, a `hint`, and whether retrying can help. `--json` shows all the
fields.

| You see | What it means | What to do |
| --- | --- | --- |
| `NO_STOP` | The line never ran, such as `--at 6`: the `raise` that valid input never reaches. Or the program failed before it. Then the message gives the exit code if the debugger reports one, and the output says why | Pick a line that the run executes. `describe` lists lines with code, not lines that run. After a failure, check the program's arguments. Everything after `--` goes to the program, and the hint names any of Peeky's own options found there |
| `warning: The debugger moved the breakpoint ...` | The line has no code of its own. Values come from the line that the warning names | Use a line from `describe` |
| `<not found; visible: items, page, ...>` in a row | The name is not visible at that stop. Or it is not assigned yet: a breakpoint stops before its line | Use a listed name, or stop one line later |
| `MUTATION_REJECTED` | A change was refused. For example: not a literal (`--set start=min(1, 2)`), a wrong type or range, or a Rust local held in a register | Use a literal of the right type. Or edit the source, and `verify` the edit |
| `INVALID_BREAKPOINT` | Conditions and logpoints are refused, because they run code. Or a Java line has no code | Use unconditional lines only. Probe with `--hits`, and filter in SQL |
| `PROVENANCE_CHANGED` | Sources, build outputs, tools or headers changed during the session. Or a worker's copy differs from yours ([9](#9-a-program-on-another-host)) | Start again on a fresh build |
| `STALE_STOP` | The program moved on. `--max-pause` may have resumed it | Run `session wait`, then `inspect` |
| `NO_SESSION` | No session has that name, or its start was interrupted | Run `session start` again. It takes the name over |
| `TIMEOUT` or `DEBUGGER_ERROR` | A bound was hit, or the debugger failed. The session is closed | Start again. Never repeat a `set` blindly |
| `UNREACHABLE` | The worker or the SSH tunnel is down | Restore the tunnel |
| `TRANSPORT_LOST` | The worker or the SSH tunnel broke mid-request | Restore the tunnel, then run `inspect` before anything else |
| `INVALID_QUERY` | Bad SQL, or a write | Run `peeky sql --schema` |
| A path you did not type, such as `/private/tmp/case/app.py` for `/tmp/case/app.py` | Peeky prints real paths. On macOS, `/tmp` is a link to `/private/tmp`, so both name the same file | Match files by the part after `/tmp` |

For example, line 6 of `examples/pagination_bug.py` is a `raise` that this run never reaches:

```sh
peeky probe examples/pagination_bug.py --at 6 --capture page
```

```
hit  phase  line  function  selector  type  value
status: no-stop  hits: 0  seconds: 1.094
error [NO_STOP]: The program finished without stopping at pagination_bug.py:6; check `peeky describe` for executable lines
hint: Pick a line the program executes; see `peeky describe`.
cleanup: nothing Peeky could identify was still running
output:
  PAGE: [40, 50, 60]
...
```

Line 10 is blank, so the debugger moves the breakpoint:

```sh
peeky probe examples/pagination_bug.py --at 10 --capture start
```

```
hit  phase     line  function  selector  type  value
1    observed  9     get_page  start     int   3
status: completed  hits: 1  seconds: 1.086  max pause: 6.3 ms
warning: The debugger moved the breakpoint from line 10 to line 9 (line 10 has no code of its own); values are from line 9. Choose a line from `peeky describe`.
...
```

An expression is not a literal:

```sh
peeky probe examples/pagination_bug.py --at 8 --capture start --set 'start=min(1, 2)'
```

```
hit  phase     line  function  selector  type  value
1    observed  8     get_page  start     int   3
status: failed  hits: 1 before it failed  seconds: 0.342
error [MUTATION_REJECTED]: Python --set values must be a literal (number, string, bytes, True/False/None, or a list/tuple/dict/set of literals); expressions are not evaluated
hint: Pass a literal value of the variable's type (C, C++ and Rust: a decimal integer, or text in double quotes for a string).
...
```

[ERRORS.md](ERRORS.md) lists all the codes. If you suspect a filesystem with unreliable timestamps,
set `PEEKY_NO_DIGEST_CACHE=1`: Peeky then rehashes every file on every check.

---

## 11. From pdb / gdb / lldb to Peeky

| You would type | Peeky | Notes |
| --- | --- | --- |
| `break file:12`, `run`, `print x` | `peeky probe target --at file:12 --capture x` | One command, one complete run. Nothing is left running |
| `print obj.field`, `print a[0]`, `print d['k']` | `--capture "obj.field,a[0],d['k']"` | Paths, indexes and string keys only, no expressions. Quote selectors with `[...]`: zsh globs them |
| `print m[1]` (a C++ `std::map`) | `--capture "m[1],m[#0].first"` | `m[1]` is the value for key 1, as in C++. `m[#0]` is the first entry in iteration order ([C and C++ 7.1](lang/c-cpp-cookbook.md#read-a-c-map-or-set-by-key)) |
| `set var x = 0`, `continue` | `--set x=0` | Only where the language allows the change ([2.3](#23-test-a-hypothesis-by-changing-a-value)). Applied once, at `--set-hit N`. The output shows the effect |
| `ignore 1 9`, `continue` ×10 | `--hits 10` (or `--hits all`), then `peeky queries hits` | Every hit up to N is captured and saved |
| `commands 1 / silent / print x / end` | `--hits N --capture x` | Logging without breaking into the run |
| `condition 1 x > 5` | `--hits N`, then `peeky sql "... WHERE num > 5"` | Conditions would run code in the program |
| `info locals`, `print *obj` | `peeky session inspect`, `peeky session get obj` | `get` lists one level of children, with their selectors |
| `bt` | `peeky session inspect --json \| jq '.frames'` | The stack at the stop. Values come from the top frame |
| `frame 1`, `up` | `peeky session inspect --frame 1`, `peeky session get --frame 1 x` | Reads a caller's locals. Frame N is as `frames` lists them |
| `next`, `step`, `finish` | `peeky session step`, `step --into`, `step --out` | Java's `--into` and `--out` skip JDK code. After `step --out` in C, C++, Rust and Java, `session inspect` shows what the function returned, under `[Returned]`. It does so also when the step stopped in a library. `session get return` reads it. The value is withheld unless it is exact ([2.9](#29-what-a-function-returned)). Python: not available |
| `finish` at every call (`Run till exit ... Value returned is`) | `peeky probe target --at <function's first line> --capture return --hits all` | One `(return)` row per call, recursion and library callers included ([2.9](#29-what-a-function-returned)). Python refuses it ([Python](lang/python.md#see-what-a-function-returned)) |
| `until 42`, `advance 42` | `peeky session continue --to 42` | One-shot. The session's own breakpoint stays. If the program stops there first, Peeky says so, with exit status 3 |
| `continue` | `peeky session continue [--to-exit]` | Runs to the next stop, or to the exit |
| "rerun with another input and compare by eye" | `--arg ... --label A`, then `peeky queries diff` | The `(output)`, `(args)`, ... rows come first, then the first divergence |
| "I think this fixes it" | `peeky verify --old ... --new ...` | A fresh build, and an independent checker |
