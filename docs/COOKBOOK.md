# Peeky Cookbook

Recipes for debugging running programs from your terminal. Each one is copy-paste ready from the repository root and shows real output (macOS, Python 3.13, recorded 2026-10-16). Agents use the same commands; see [AGENT_GUIDE.md](AGENT_GUIDE.md). Each language's rules, with pointers into this book: [Python](lang/python.md), [Java](lang/java.md), [C and C++](lang/c-cpp.md), [Rust](lang/rust.md).

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
   - [2.1 Fields and Indexes](#21-fields-and-indexes): selectors: fields, indexes, keys; names in any script; C and C++ pointers; C++ maps and sets
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
   - [5.1 Stop, Look, Change, Finish](#51-stop-look-change-finish): `start`, `inspect`, `get`, `set`, `close`; globals; caller frames
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

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # Python 3.11+; brings debugpy for Python targets
```

| Language | Also needs | Found automatically from |
| --- | --- | --- |
| Python | nothing | the interpreter running `peeky` |
| C, C++ | clang, lldb-dap, dwarfdump (macOS: Command Line Tools; Linux: LLVM 18) | `xcrun` or `PATH` (`PEEKY_NATIVE_*` overrides) |
| Rust | rustup toolchain 1.96.0, lldb-dap | `rustup which` (`PEEKY_RUST_*` overrides) |
| Java | JDK 17 and the adapter bundle: `python scripts/setup_java_adapter.py --output "$PWD/work/java-adapter"` | `--config target.json` |

Peeky keeps saved runs and session records in `~/.peeky`. Set `PEEKY_STATE_DIR=/some/dir` to keep a task's runs and sessions separate.

Targets are detected from the path:
- a `.py` file, or a directory plus `--at module.py:<line>`, is Python;
- a `.c`/`.cpp` file is C/C++;
- a directory with `Cargo.toml` is Rust;
- `--config target.json` covers Java and anything you want to pin.

---

## 1. First Look

The example: `examples/pagination_bug.py` should return page 1 as `[10, 20, 30]`, but prints `[40, 50, 60]`.

### 1.1 What Can I Ask?

**What it answers:** Which lines can stop? Which names exist in each function? What can be changed?

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

For a large module, `--json` and `jq` find a function quickly: `peeky describe dir --at module.py --json | jq '.scopes[] | select(.scope | test("is_private"))'`.

C, C++ and Rust list the same from the build's debug information: each function in your sources, with its file, lines and parameters and locals. Names are readable (`crate::Type::method`, `<crate::Type as core::fmt::Display>::fmt`, `geo::Box::area`).

- **Function names in a table:** A probe table's `function` column shows each frame's short name: `trim_left`, `geo::Box::area`, `same<int>`, a Rust function without its symbol hash; two overloads in one table keep their parameters (`twice(int)`, `twice(std::string const&)`).
- **Whole names:** JSON and the evidence store keep LLDB's whole frame name (a C++ signature, `trim_left(std::__1::basic_string<char, …> const&)`; Rust's symbol escapes, `_$LT$…`), and a session's text names the frame with its parameters, the standard types written as code writes them (`in trim_left(std::string const&)`).
- **In SQL:** SQL compares the whole name, so `WHERE function = 'trim_left'` finds nothing: a query's table that shows short names says so in a note, as does `peeky sql --help`; a probe's table says it once per state directory (never with `--quiet`). Match a frame with `short_function(function) = 'trim_left'` or with its whole name. `short_function` drops the parameters of every overload and a Rust symbol's hash, so overloads share one name (`operator+` for `operator+(V, V)` and `operator+(W, W)`; `G::get` for `get() &` and `get() &&`), and so do a Rust function's closures (`cz::main::{{closure}}` for `{closure#0}` and `{closure#1}`) and a generic's instances (`cz::gen_apply` for `gen_apply<i32>` and `gen_apply<i16>`): a `GROUP BY` or join on it combines them, and it is not always what a table shows (a table keeps the parameters of overloads that would read alike).
- **Closures, lambdas and generic instances:** A probe table shows a closure or instance as the build names it (`cz::main::{closure#0}`, `cz::gen_apply<i32>`); the `instance` column of `captures` (and of `evidence`, `interventions`) keeps that label, so `WHERE instance = 'cz::main::{closure#0}'` finds one closure's rows, and the table's note says so. Closures and lambdas are listed under their function.
- **Inlined functions:** A function inlined into another (`#[inline(always)]`, `always_inline`) keeps its own lines.
- **Types:** Each name shows its type; in C a struct, union or enum that no typedef names is written with its tag, as C code names it (`r (struct ring *)`, `c (enum color)`; a typedef's name stays: `t (box_t)`). C++ writes the standard library's types as code and probe tables do (`parts (std::vector<std::string>)`, `p (const std::string &, lines 6-7)`), and lists none of the compiler's own locals (a range `for`'s `__range1`, `__begin1` and `__end1`).
- **Block lines:** A local declared only inside nested blocks also shows those blocks' lines, where it is visible: `i (int, lines 23, 28)` is two one-line `for` loops' `i`, not a name of the whole function. A block that also runs another file's code (an `#include` inside it) gives none (a local macro's expansion, on its definition's lines, does not count), and past eight blocks one span covers them all.
- **Rust:** In Rust every `let` opens a block, so a local shows the lines after its `let`, where it is visible (#107). Bindings a macro from another crate declares (`println!`'s `args`, `core::fmt::rt::Argument[1]`) are left out: the build's MIR places them in the macro's own file. A macro of your crate's keeps its bindings, `#[collapse_debuginfo(yes)]` too.
- **Spans:** Lines between a block's own where only code outside the block runs are left out, so one block can give several spans. In C++ a block's cleanup code, which runs when an exception leaves the block, is on the function's closing line: `name (std::string, lines 22-25, 35)` is an `if` block's local, visible on its lines and, while an exception unwinds, at the closing `}`, not on the lines between. A span is never narrower than where the debugger can show the local, so lines without code (comments, a lone `}`) inside it are kept.
- **When the debug information cannot be read:** If the debug information cannot be read, the text says `function names: unavailable (reason)`. A very large optimized C++ unit can exceed the 16 MB bound on reading it:

```
names in parse_count (pagination_bug.c:6-14): end (char *), minimum (int), result (int *), text (const char *), value (long)
names in main (pagination_bug.c:16-31): argc (int), argv (char **), end (int), i (int, lines 23, 28), items (int[128]), page (int), page_size (int), start (int), total (int)

names in peeky_pagination::bounded (src/main.rs:14-20): default (i32), minimum (i32), text (core::option::Option<&alloc::string::String>), value (i32, lines 16-19)
names in peeky_pagination::main::{{closure}} (src/main.rs:37-37): index (i32)
```

With `--json`, a scope's `spans` maps such a name to its blocks' `[first, last]` lines.

The `mutable:` line (C, C++, Rust) also names, per function, the struct, tuple and array locals with an integer field or element `set` may change, judged from the type and the local's storage as `set` judges them (Rust: the IR keeps it in memory): `Locals with such fields or elements: movavg::main: w`, so `w.k` is settable where `w` is initialized and the stop is a statement's first instruction (7.1, 7.2). A local reached through a pointer or a reference (`self`) is never listed. JSON: a scope's `settable_parts` (`{"w": "fields"}`).

**Line ranges and stepping.** A function's lines (`main (pagination_bug.c:16-31)`) and a local's block lines are where its code is, not where a stop can land:

- A `session step` can stop on a line with no statement of its own: a closing `}` (C, C++, Java, Rust: where a block's locals go out of scope or a loop jumps back) or a loop header on its later passes (`for (...; i++)`).
- A step can also land back on an `if`'s line after the last line of its block: from `self.sum -= old;` (line 16) to `if self.values.len() >= self.k {` (line 14), then line 18. The compiler gives the jump that leaves the `if`'s block the `if`'s own line (the conditional's); the condition does not run again.
- A local listed with block lines (`i (int, lines 23, 28)`) is read only inside them; on the block's closing `}`, Java has already dropped it, so read it on the line before.
- Use `describe`'s breakpoint lines for `--at` and `continue --to`; a step goes wherever the compiled code goes next.

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

`start` is 3 for page 1: the offset treats pages as zero-based. Progress messages go to stderr; `--quiet` hides them.

---

## 2. Asking Questions with Probes

A probe is one complete run: build if needed, launch, stop at the line, capture, optionally change values, run to the end, report and clean up. Nothing is left running.

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

- **Every language:** Selectors are dotted paths, indexes and string keys (`request.page`, `args[0]`, `items[-1]`, `order['id']`), never expressions: Peeky never evaluates code in your program. A selector that is not there lists what is. Quote selectors that contain `[...]`: zsh, the macOS default shell, would otherwise treat them as filename patterns.
- **Names in any script:** A name is matched exactly as the debugger lists it, non-ASCII ones too (`größe.zähler`, `ñ`). Peeky never normalizes a name: compilers differ (Python lists `größe` written NFD in the source as NFC and `ﬁx` as `fix`, Rust lists NFC, javac and Clang keep NFC and NFD spellings as two variables), so a spelling that differs from a listed name only by Unicode normalization is refused, and the message gives the listed spelling escaped (`'gr\xf6\xdfe'`). `inspect` marks each row whose name differs from another row's only by Unicode normalization (the two are equal under NFKC) with how it is written (`[written 'gro\u0308\xdfe': ...]`). Names in different scripts that look alike (Latin `a` and Cyrillic `а`) are matched exactly too, but `inspect` does not flag them. A name holding an invisible character (U+200B, U+200D, a bidi control), or longer than 128 characters, is never selectable: `inspect` marks its row `[not selectable]`, never `[settable]`.
- **Python:** Python dict entries are read by key; `order.id` fails with a message that names `order['id']` and lists the keys.
- **C and C++ pointers:** In C and C++, follow a pointer with `.` (`stats.total_us`, not `->`); a pointer to a scalar carries no length, so `values[0]` reads what it points at; for other elements, read the array itself where it is declared (in the caller). The same holds for a pointer to an array, a container or a struct: `pa[0]` is what it points at, so `pa[0][1]` is `(*pa)[1]`, while `pa[1]`, which C reads past the pointee, is refused with that selector (#102). A typedef'd pointer (`RowPtr`, `const RowPtr`) works the same; the build's debug information tells it from a typedef'd reference, which indexes its referent. A pointer under a type name it does not resolve (a template's member typedef such as `std::vector<T>::pointer` to a container) is refused for indexing rather than read either way. A NULL or unreadable pointer is refused (`p is NULL`).
- **Rust:** Rust references and Boxes are never indexed this way.
- **C++ maps and sets:** On a C++ `std::map` or `std::unordered_map`, `m[1]` is the value for key 1, as in C++, never the debugger's second entry; `m[#0]` is the first entry, and a set has only `s[#0]`, ... (7.1).

### 2.2 Every Iteration of a Loop

**What it answers:** How does a value evolve across iterations?

**Common cases**

- `--hits N` captures N stops; `--hits all` every stop, up to 100, and needs the program to exit. `queries hits` shows one selector across hits, with `changed`.
- **To get one row per pass, put `--at` on a plain statement line inside the loop's body** (not the header, and not a line with several stops). A line with a macro's code, a one-line loop or a range `for` can stop several times per pass, and the probe warns: there, read every row with its `stop` label; don't filter by a stop number.
- **A loop header may stop only once, before its loop:** the probe warns; put `--at` on the first line of the body.
- Different functions on one line (a closure, a lambda) are told apart by each row's `function`.

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

A hit where the selector has no value shows NULL with the reason in `error`, and `changed` is NULL next to it: a missing value is never compared as one. Nor is a qualified one: `caveat` names a view that qualifies the value (`stale`, `unproven`, `opaque`, `budget-exhausted`) or `note` for a row the probe marked `[note]` (a C local's leftovers on a `do` body's first line; `queries run` shows the note), and `changed` is NULL next to it too.

`best` starts at 0 and never drops to a negative value, so the running maximum of all-negative input is wrong.

`--hits all` captures every stop, up to 100, without counting iterations first: the summary says `hits: 3 (all)`, and a warning says when the cap was reached. With more than 20 selectors the cap is lower, so that every hit fits the 2000-row probe budget. `all` needs the program to exit, so it cannot be combined with `--until hits` (attached JVMs take a number). Progress prints the first hit, then at most one line every 2 seconds, then the total when the program finishes.

**`--hits` counts stops, not passes.** A probe stops each time the program reaches one of the line's breakpoint locations. In C, C++ and Rust a line can have several in one function, since LLDB places one at the first code of each block on the line: a macro's own code (`format!` and `println!` in Rust, a C `do { int t_ = a; ... } while (0)` macro), a one-line loop's body (`for (...) { int sq = i * i; ... }`), a C++ range `for` header (its range is set up once, before the loop, and its element bound at each pass).

Each pass then stops at each location it runs, and `--hits N`, `--set-hit N` and `--set-at LINE@HIT` count those stops. The probe says so before the first stop, and each row's `stop` says which location it is at, 1 to N in address order (`stop` in `--json`, the evidence, `queries run` and `queries wide`, where a row's `stop` is empty unless every value in it was read at the same stop; `hit 4, stop 1 of 3` in session state lines):

```text
peeky: Line 29 stops up to 3 times per pass in movavg::main (3 breakpoint locations): --hits, --set-hit and --set-at's @HIT count stops, not passes (as do session hit numbers); each hit's stop (1 to 3, in address order) says which location.
hit  stop  phase     line  function      selector  type  value
1    1/3   observed  29    movavg::main  mean      f64   3.0
2    2/3   observed  29    movavg::main  mean      f64   3.0
3    3/3   observed  29    movavg::main  mean      f64   3.0
4    1/3   observed  29    movavg::main  mean      f64   4.5
```

In this example every pass runs location 1, so to read one row per pass, keep that location: `peeky sql "SELECT hit, value FROM last_run WHERE stop = 1"` (hits 1, 4, 7 and 10 above); to change the second pass, `--set-hit 4`. A change says which stop its hit was (`set x: 0 -> 100  (--set-at line 9@2, stop 2 of 3, in s1::main)`: the first pass's second location, not the second pass), as do `interventions.stop`, `queries set_effect` and `queries wide`'s `sets` (`line 9, hit 2, stop 2`).

Pick it from the rows, not by its number alone: a loop header's first location runs once, before the loop (a C++ range `for`: `stop` 1, then 2 at every pass), and a branch can skip one (`if (v[i] > v[i + 1]) SWAP(...)` stops at 2 only when it swaps). For the same reasons Peeky has no `--first-stop` (stop once per pass): breakpoints alone cannot tell where a pass starts.

**Finding the location each pass reaches.** `peeky sql "SELECT stop, COUNT(DISTINCT hit) AS hits FROM last_run WHERE phase = 'observed' GROUP BY stop ORDER BY hits DESC, stop DESC"` counts each location's hits:

- **The per-pass location** is the one with a count above 1. A C or C++ one-line loop whose body declares a local, and a range `for`, stop 1, 2, 2, 2 (location 1 runs once, before the loop); a Rust one-line `for` stops 1, 2, 3, 3, 3; a macro's code 1, 2, 1, 2, where each location repeats.
- **Every count 1:** the loop ran at most once, or no location runs at every pass (`if (v[i] > v[i + 1]) SWAP(...)` stops 1, 2 over three passes). Read all the rows, or probe a statement line in the body. An empty loop still stops before it (C++ `1`, Rust `1, 2`), with no pass at all.
- **One location:** a one-line C or C++ loop whose body declares no local (`for (...) { total += i; }`) has one location and stops once, however many passes run.
- **Ties** (a loop of one pass) come out in `stop DESC` order, the later location first. Whether the frame left the line between two stops is not something a stop says, and a call that returns between two locations looks like one pass.

A line with one location stops at most once per pass, however many statements it has (`a = twice(i); b = a + 1;`), and no warning is given. Different functions on one line (a closure, a lambda, a generator expression, a generic's instances) are a separate case: rows' `function` tells them apart, with a warning of its own. A function inlined into its callers (`#[inline(always)]`, a C `static inline` at `-O0` with `always_inline`) has a copy of its code at each call: each copy is a call of its own, and its line stops at that copy's locations, so `format!` in a function inlined twice stops 3 times at each call, 6 in all, and says so once (`stop` counts within a copy).

**A loop header can stop only before its loop.** A breakpoint binds a line's first code, and a header's first code often runs once, before the loop starts, never at the passes: `i = 0` of a C, C++ or Java `for (int i = 0; ...)`, the jump into a C or C++ `while` loop or Rust's `while`, a Java for-each's copy of its array or its iterator, Rust's `for v in xs` at its two locations (`into_iter` and the iterator's binding). Then `--hits all` gives one row (or two) where you expected one per pass, so Peeky says so before the first stop, in probes and `session start`:

```text
peeky: Line 4 is a loop header: it stops once each time its loop starts, before the first pass, never at the passes; put --at (or --set-at) on the first line of the loop's body for one stop per pass.
```

It decides from the build's code, never from the source's shape: the function's control flow (LLDB's disassembly; Java's bytecode, at the location java-debug binds) gives its loops and the line table which line each instruction is.

- **When it warns:** A loop is the line's where its header holds any of the line's code, and the note needs both: every breakpoint location of the line is outside that loop and every path from it passes through the loop's header before it can come back to the location or return (so it runs once each time the loop starts, never at a pass of a loop around it); and the line's code inside the loop is only the loop's own (its condition, its increment, a for-each's binding at the start of the body), while the loop holds other lines' code too.
- **Nested loops:** A nested loop's header stops once each time that loop starts, so once per pass of the loop around it. Each location stops at most once per start; where one runs only under a condition on the line (`if (z) { int w = 5; ... } for (...) {`), the note says `up to 3 times`.
- **When it does not:** No note, so the stops are at each pass of a loop around the line: a loop entered under a condition (`if (r == 3) for (...) {`), a line holding a whole loop (`for (...) total += i;`, a statement macro's `for`, `do { for ... } while (0)`), a line with other statements (`let mut n = 0; while n < 2 { ... } total += n;`); there #157's per-pass warning applies where the line has several locations.
- **Jumps:** Nor for a line whose code only jumps into a loop (`goto`, `for (;;)`, `do {`, Rust's `loop {`).
- **Headers that run at each pass:** A header whose first code is the condition the loop returns to stops at every pass and gets no note: a Java `while (k < n)`, `for (; j < n; j++)` and `do`'s `while`, a C or C++ `do`'s `while`, a C++ range `for` (its element is bound at each pass) and Rust's `while let`.
- **When it cannot tell:** Where Peeky cannot read the flow (a jump table, flow into the middle of a loop, an architecture other than arm64 or x86-64), it says nothing; nor does it look at a line that names no loop (no `for`, `while`, `loop`, `do`, a name with one in it, or a macro's capitals), or one in more than 8 functions or inlined copies. It asks the debugger once, at launch: one line table per file and one disassembly per function, never again when a probe re-arms its breakpoints.
- **GCC:** GCC gives `for (;;)` and `do {` no code of their own, so LLDB moves their breakpoint into the body, which stops at each pass (`moved` says so).
- **Python and Java:** Python stops once per pass at a line (a `for` header once per pass and once more when the loop ends); Java and Python have no `stop`. A Python local the pass assigns below the stop shows an earlier pass's value, with a `note` (5.1).
- **`describe`:** `describe` does not mark these lines: for C, C++ and Rust it would need the debugger's locations, which only a run has.

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

- **What:** Root locals can be changed: the stopped function's own variables (`start`). In Python a change rebinds the name in that frame: `--set 'seen=[]'` gives this call a new list, while the shared object it named (a default argument, a module global) keeps its contents, and the next call sees it again.
- **Fields and elements:** In Java, so can scalar fields and array elements (see 7.3); in C, C++ and Rust, integer fields and elements of a struct, tuple or array local the stopped frame holds itself (`s.end`, `xs[2]`, `t.__0`; see 7.1 and 7.2), never through a pointer, reference or `Box`.
- **Where:** A change writes the variable's storage, which the program's later reads see; what the program already copied keeps the old value, so C, C++ and Rust allow a change only where Peeky proves no statement is under way, and refuse it anywhere else (after `step --out`, on a statement's later line; see 7.1 and 7.2).
- **Values:** Python takes literals, including lists and dicts (`0`, `'x'`, `None`, `[]`, `{'k': 1}`), and refuses fields before the program runs. Expressions, names and calls (`min(xs)`, `float('inf')`) are `MUTATION_REJECTED`, never evaluated. `-1e309` is negative infinity. C and C++ take integers of 16 to 64 bits (`int`, `long`, `size_t`, `int64_t`, …) within the local's range; Java takes scalars of the target's type. Rust takes integer locals (`i16` to `i64`, `u16` to `u64`, `isize`, `usize`) that the program keeps in memory; any other change is refused, because it would not reach the program.
- **Text:** A value in double quotes is a text, written as JSON writes a string and quoted for the shell: `--set 'name="apple"'` (`"say \"hi\""`, `"caf\u00e9"`). C and C++ write it into a `char[N]` local (the text and a NUL, if both fit) or a `std::string` local (in place, within its capacity, never reallocating); Rust into a `String` local (within its capacity, while no borrow of it is used after the stop, in a crate without `unsafe` code); Java makes a new String for a variable declared String; Python evaluates it as a Python literal. Never through a pointer or a reference (`char *`, `&str`, `const std::string &`), and never into a field or element in C, C++ and Rust. See 7.1, 7.2 and 7.3; 2.8 has a `--set-at` example.
- **Marks:** For C, C++ and Rust, rows mark what a change would reach with `[settable]`: locals in `session inspect`, and fields and elements in probe rows and `session get` listings. When a rule leaves nothing settable at a stop, `session inspect` says why in a note. In JSON every root, field and element row a change could name carries `settable: true` or `false` (Java's too); `false` includes the internals of a standard-library type (`h.v.len` of a `Vec` field, raw where no view reads it) and what Peeky decodes from memory (a `Vec`'s elements and `len`), both of which `set` refuses.
- **`after-set` rows:** After a change, each selector is read again (`after-set`). The table leaves out an `after-set` row equal to its hit's `observed` one only when its value is shown whole and holds nothing else (a number, a boolean or null, or a quoted text with no `…` or `...` cut) and its selector is neither a change's target nor holds one (`s` when `s.ninth` changed), as `items[1]` above; a footer counts them. A pointer, a preview or summary of an aggregate, a cut value, a row with a note, a Java value other than a primitive (a String set through an alias is a new object with the same text) and a Python value whose identity changed stay, since they may hide a change. `--json` has every row.
- **Read back:** Every change is read back: one the debugger refuses or cannot store (a computed value, such as a structured binding of a bitfield) is refused and the session kept, and a value stored differently from the one asked for (a type redefined as a float) closes the session. A refusal (`MUTATION_REJECTED`) names the local and the rule, and its hint the one way around it.

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

### 2.5 A Module Imported by a Script, with Arguments

**What it answers:** What happens inside a library for one specific input?

This is a real bug: CPython 3.9's `ipaddress` (CVE-2024-4032) calls the public address `::ffff:8.8.8.8` private.

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

`--run` names the script to execute when the breakpoint is in a module it imports. `--arg` is repeatable; use `--arg=-v` for values starting with a dash. Or put all of the program's arguments after `--`: `peeky probe app.py --at 5 -- A 5 5 0`. C, C++, Rust and launched Java programs take arguments too.

### 2.6 Stop Right After the Last Hit

**What it answers:** I only need the values, not the output. Can it be faster?

```sh
peeky probe examples/pagination_bug.py --at 8 --capture start --until hits
```

```
status: completed  hits: 1  seconds: 0.267  max pause: 4.1 ms
```

That took 0.27 s, compared with 1.0 s when waiting for the program to exit. The output is then incomplete (`output_complete: false` in JSON). Attached JVMs default to this.

### 2.7 JSON for Scripts

**What it answers:** How do I feed results into another tool?

```sh
peeky probe examples/pagination_bug.py --at 8 --capture start --json --quiet | jq -c '.by_hit[0].observed'
# {"start":"3"}
```

Every command takes `--json`. Exit codes:
- 0: success;
- 1: a completed command with a negative result (a probe that failed once the debugger's launch or attach request was sent, or a failed verify, a failed build included);
- 2: an error, or a refusal: any probe that ended before the debugger's launch or attach request was sent, whatever its code (`refused_before_launch`: a refusal, a file outside the worker root, a build that failed, a debugger that did not start). Peeky did not start the program (a build, or a Python candidate's interpreter, may have run), so its text has no table, only the error and its hint, and no run is saved;
- 3: a background job still running;
- 4: a probe (`peeky probe`, or the `peeky-probe` script) that completed, but a change it was asked to make (`--set`, `--set-at`) never happened, because its stop never came (2.8). The values are evidence; the experiment did not happen. A shortfall of `--hits` alone is not a failure: 0, with a warning.

### 2.8 Change a Value, Then Restore It

**What it answers:** Would the output be right if one value were different for one statement only?

**Common cases**

- `--set-at LINE@HIT name=value` changes a value at hit `HIT` of `LINE` (counting that line's own stops), in the `--at` file; repeat it to change at one line and restore at the next.
- Values are captured only at `--at`; a change there gets `after-set` rows, as `--set` does.
- `HIT` counts every stop of the line, in any function (a lambda's too); each change says whose variable it changed.
- A change that never happened (its stop never came) makes the probe exit 4.

`stock.cpp` (C++) keeps `"apple "` and `"apple"` apart because `trim_left` leaves trailing spaces. Line 22 is `std::string name = trim_left(entry.substr(0, colon));` and line 23 `long qty = std::stol(entry.substr(colon + 1));`. To test the fix, shorten `colon` so line 22 cuts the space off, then restore it so line 23 still reads the quantity:

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

Shortened alone, line 23 reads `:4` and `std::stol` throws. With `--set` you would need a session (start, set, step, set, …) for this.

Or change the text itself, once line 22 has made it: `name` is a `std::string`, so it takes a text in double quotes (2.3), and nothing needs restoring:

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

- **Where and when:** `--set-at [PATH:]LINE[@HIT] name=value` changes a value at hit `HIT` (default 1) of `LINE`, counting that line's own stops, in the `--at` file. Repeat it for several lines and hits; changes at one stop are made in the order given.
- **Which function:** `HIT` counts every stop of the line, in any function: a lambda or closure on the line (`auto f = [](long v) { return v + 1; }; total += f(v);` stops in `main`, then in the lambda), a recursive call, or a generic's instances (Rust: `gen::tf<gen::A>` and `gen::tf<gen::B>`, named with their type arguments; a name past 300 characters is cut with `…`, and two that then read alike get their symbol hashes; closures as `main::{closure#1}`).
- **Python lambdas:** Python's debugger names two lambdas on one line alike (`<lambda>`, with no column), so the warning says their stops are counted together, and a change in one says `one of 2 on the line the debugger names alike`.
- **What a change says:** Each change says whose variable it changed (`set v: 0 -> 100  (--set-at line 6@2, in main::$_0::operator())`; `function` in JSON and in the `interventions` table), and a `--set-at` line that stopped in more than one function gets a warning listing each function's stops.
- **Captures:** The probe stops at every `--set-at` line as well as at `--at`, but captures values only at `--at`. A change at a captured stop gets `after-set` rows, as `--set` does. `--set` and `--set-hit` work as before, and together with `--set-at` make at most 8 changes.
- **How long it runs:** The probe keeps each breakpoint until the changes on that line are made, past `--hits` if need be. With `--until hits` it ends once both are done (an attached JVM, which never exits, waits for them until `--timeout`). A change whose stop never comes (`23@9` where line 23 stops 4 times) is not made, and the result says so: `status: completed (1 requested change not made)`, a warning, `unapplied` in JSON, and exit code 4. So is a `--set` whose `--set-hit` never comes.
- **Rules:** Each change follows the same rules as `--set` at its own stop (a statement under way, a local not yet initialized, a value with a note, a field through a pointer: see 2.3). Before the program runs, each `--set-at` is judged against the functions on its own line, as a `--set` is against the `--at` line's: one that no stop of its line could make (a Rust local kept in a register, a field through a pointer) is refused there, naming the line, and nothing runs (6.6; exit code 2, with only the error and its hint). One refusal fails the whole probe, naming its line and hit and how many changes were already made: half of a linked change is not the experiment you asked for. A line with no code of its own is refused, never moved to the next line.
- **Evidence:** `runs.sets` lists each change as `[name, value, "LINE@HIT"]` (a `--set` stays `[name, value]`), so `queries diff`'s `(sets)` row tells runs apart by them. The `interventions` table gives each change's `line`, `hit`, `capture_hit`, `function` and `instance` (schema 12; `captures.instance`, schema 13, labels captured values the same way). `queries set_effect` lists the changes in the order they were made, and `queries wide` puts each one in the `sets` column of the row it comes before.

### 2.9 What a Function Returned

**What it answers:** What did each call of this function return, including calls a library makes (a comparator `std::sort` or `qsort` calls, a key function, a `Comparator`)? C, C++, Rust and Java.

**Common cases**

- `--capture return` with `--hits all` gives a `(return)` row per hit, what its call of the `--at` line's function returned, a library's calls included: probe the function's first line for one row per call.
- A value is shown only when exact: C and C++ integers, `bool`, characters, floats and pointers, Rust integers, `bool` and floats, Java any type; a struct, a string or any return in an optimized build is withheld, saying why.
- After `session step --out`, the state shows what the function returned (`(return): int = 42`; `returned` in JSON), or a withheld row saying why (the rules below; an exception, `longjmp` or panic that ended the call included); nothing where another stop interrupted the step first, or the program ended. `session inspect` lists it under `[Returned]` and `session get return` reads it.
- Python's returns cannot be captured: capture what the caller stores, on the caller's next line (for `r = f(x)` on line 20, `--at 21 --capture r`).

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

`return` (or `(return)`) captures what the function holding the `--at` line returns: after each hit, Peeky steps out of that call, one frame, and records a `(return)` row (`phase` `returned`): `function` is the function that returned, `line` and `location` where it returned to, often in the library that called it.

- **One row per hit:** Each hit's `(return)` row is what its call returned, or why that is withheld. On a line that runs once per call, with `--hits all`, that is one row per call, and a recursive call's return goes to its own hit, also through another function (`f` calls `c` calls `f`: Peeky steps out of `c` too, until each `f` returns).
- **Which line:** Probe the function's first line, or any line that runs once per call. A line can stop again at the same depth before the call returns: a loop's next pass, or the line's next breakpoint location (a line with several, #157). In C, C++ and Rust, when the step out's watch on the caller (below) held until that stop, the frames under it are the call's, and its caller is at the same stack pointer, the stop is in the same call: no other call could have returned to that place without running the watched call instruction there. Peeky then follows the return across it, and each of those hits gets the call's return.
- **Withheld instead:** Otherwise the earlier hit's return is withheld rather than guessed. Where the stopped function is not the call's, the row says the line `stopped again at the same depth in a call of another function`; where the frames under the stop are not the call's, `in another call`. Else it says Peeky `could not tell whether in this call` (another of the line's breakpoint locations, or a loop's next pass) `or in a later call after this one ended without returning`: for example in Java, which has no such watch, or where the watch was dropped or another stop came in between. A call still running when the program ends is withheld too (`the program ended before it returned`), and so is one still running when the probe's `--timeout` runs out (`the probe timed out before it returned`).
- **Stops:** On a line with several breakpoint locations (#157), each `(return)` row has its hit's `stop` (and `stops` in JSON): where its call was stopped, not the line it returned to.
- **Cost:** Each hit costs a step out to its return, through every call it makes: through a deep or wide recursion (`f` calls `c` calls `f`, each call several more), `--capture return` costs about 7.5 ms per nested call on macOS (a depth-13 tree took 62.6 s, against 1.8 s without `return`). There, limit `--hits`, or probe a line only one call reaches.

A call that never returns gets no value.

- **Exceptions, `longjmp` and panics:** In C, C++ and Rust, when an exception, a `longjmp` or a panic ends a call and its caller goes on (it catches the exception, `setjmp` returns again, `catch_unwind` returns), LLDB's step out stays armed: the next call that returns to the same place in the same frame ends it, and LLDB lists that call's value as the first one's. So while it steps out, Peeky watches the caller on that thread: the call instruction, every branch to the address the call returns to, and every computed jump, in a frame at or above the caller's (none of them can run there while the call is still running).
- **Where it ends:** If one does, the step ends there, where the caller went on, and the row says `it never returned: its caller went on without its return (an exception, longjmp or panic ended it)`. When the step does end at that address, the stack pointer must be the caller's again, or the value is withheld.
- **The watch:** The watch reads the caller's disassembly once per function (forced for a function too large for LLDB's default; if LLDB still gives none, the row says so). A deeper call of a recursion through the caller runs the watched call too, and each run costs LLDB a stop of its own: after 128 of them in one step, LLDB drops the watch, the step goes on at the cost it has without one, and the value is withheld (`the watch was dropped to keep the step's cost bounded`). No step fails for it: a `step --out` over a recursion thousands of calls deep completes (each of LLDB's own stops used to take a place in the session's event history, and 2,000 of them failed the session; a run of them now takes one). An optimized build, whose returns are withheld anyway, is not watched.
- **Java:** Java's returns come from JDI, which lists nothing for a method that threw: `java-debug listed no returned value: the method returns nothing, or it ended by an exception`.
- **Inlined functions:** A function inlined into its caller (`always_inline`) has no return of its own: withheld, saying it was inlined.
- **Threads:** A probe follows one thread's returns at a time: a call another thread's stop comes before (or with: LLDB stops threads together) is withheld (`another thread stopped at a breakpoint before it returned`), and the probe goes on with every hit's rows.
- **A `--set-at` inside the function:** A `--set-at` line inside the function stops before the return, which still comes and is that call's (the probe checks that it comes back to the frame that made the call).

A session shows the same after `session step --out`: the step's state shows it (`(return): long = 1`; `returned` in JSON; nothing where another stop interrupted the step first), `session inspect` lists it under `[Returned]`, apart from the locals (`returned` in JSON), labelled with the function that returned, and `session get return` reads it. A step that stops outside your sources (`outside the program's sources`, in `sort.h` or libc) still shows what your function returned; the library's own locals are not read. A `step --out` of a call that throws (C, C++, Rust) stops where its caller goes on without it, at the next call from the same place or a jump back to it, and `[Returned]` says the function never returned.

Shown only when exact. LLDB reads a returned value from the return register, so C and C++ show integers, `bool`, characters, `float`/`double` and pointers, and Rust its integers (a `u8` as Rust prints it, `65`), `bool` and floats.

- **Withheld:** A struct, class, `std::string`, reference, Rust enum or `Option`, a value the debugger cannot read, any return in an optimized build, and a function returning nothing are withheld, with the reason. LLDB would otherwise step out past libc++'s hidden frames and code without debug information, and then list another value as that function's; Peeky keeps each step out to one frame and checks that it stopped where the call returns. lldb-dap before LLVM 21 (the Linux lanes' 18 and 20) lists no `(Return Value)`; Peeky then reads what LLDB itself recorded for that step out (`thread info`), under the same checks.
- **Java and Python:** Java's returns are JDI's exact values, any type. Python's are not available: debugpy shows one only by writing it into the program's globals (`__pydevd_ret_val_dict`), which the program could see, so Peeky refuses `--capture return` there; capture what the caller stores.

---

## 3. Comparing Runs

Every probe not refused before launch is saved with a run id (`--no-save` skips that). `--label` tags a run so you can find it later.

### 3.1 Two Inputs Side by Side

**What it answers:** Where do two runs diverge?

**Common cases**

- Probe each input once (`--label` each), then `peeky queries diff`: it compares the last two runs unless you name them (`--param a=1 --param b=5`).
- Read the `(status)`, `(output)`, `(edit)`, `(source)`, `(line)`, `(args)` and `(sets)` rows first: they say how the runs differed before you credit an edit.
- Then come real divergences, the first one first; address-only and qualified values come after them.
- Hits pair by number: where the runs stopped a different number of times (a sort's comparator), or as often but where hits may not be the same moments (an edit that changes the calls), a `note:` says so: use `queries join` (3.5) with a value that names the call.

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

1. **What the runs did and how they differed**, one row each where it applies: `(status)` and `(output)` when the runs' outcomes or outputs differ; `(edit)` when one run built a candidate edit; `(source)` when two runs that are not candidates ran different source text (another copy of the file); `(line)` when the probes' `--at` lines differ (their values then differ partly because of where they were read; a candidate's `--at` keeps the original's numbering, so `--at 33` is the same line where the candidate's rows show its line 34); `(args)` when they ran with different program arguments; `(sets)` when they made different interventions. Check these rows before crediting an edit with a difference.
2. **Real differences**, in hit order and then in `--capture` order: the first of them is the first divergence.
3. Values that only one run's `--set` changed (`intervention only in run b`). An `after-set` value equal to its run's `observed` value at that hit, in each run that has one, is left out: the intervention did not touch it (`r.buf` when only `r.tail` was set), and the `observed` row shows any difference.
4. Values a view or a note qualifies in either run (`note: qualified (run a: note, run b: note): not evidence by itself`): a C local's leftovers on a `do` body's first line, a moved Rust local's bytes (`stale`). Two runs' leftovers differ with anything that touched the stack before, so such a row is not a divergence of the program; `queries run` shows each note. Whatever else a row says, a qualified value's note says so too, and the row ranks here: `missing in run b; qualified (run a: note)`, `intervention only in run a; qualified (run a: note)`.
5. Values that differ only in heap or stack addresses (`note: addresses only`, for example a C string's pointer; NULL is never treated as an address), one row per selector: its first hit's, saying at how many hits (`addresses only, at 4 hits (one row per selector: its first hit)`), and per stop where both runs label stops (`at 2 hits at stop 1 (one row per selector and stop: its first hit)`). Text inside quotes is the program's data, never an address: `"0x00000001deadbeef"` in a `char[32]` is a real difference.
6. Last, one row per selector that only one run asked for (`only in run a`, with its first hit's value).

The `(output)` row shows up to 200 characters of each output, from 60 before the first character that differs (from the start when it differs within its first 60; `…` marks a cut), and says where (`output differs at line 4, column 13`, counted from 1), so after a `--set` it shows the intervention's effect. Hits that one run never reached say `missing in run b`. A value whose type changed (an edit to `long long`) says `type unsigned int -> long long`.

- **Which runs:** `diff` compares the last two runs unless you name them: `--param a=1 --param b=5`. Comparing needs two saved runs; with fewer, the query says so.
- **Pairing by hit:** `diff` pairs hits by number: where the runs stopped at their line a different number of times, the hits need not line up (a sort's comparator is called on other pairs once one answer differs, so every later hit shows as a difference), and a `note:` line says so and names `queries join` (3.5), which pairs them by a selector's value instead (`--param key=a.name`). Where both stopped as often, an edit can still change which call a hit is (eighth evaluation, M6), and the note gives the first sign it finds:
  - **Places:** each hit's function, instance (a closure, a generic's copy) and line in one run must pair with one place in the other at every hit. A function renamed, or lines an edit moved, pair the same way at every hit and count as the same place. Otherwise: `at hit 3 run 2 stopped in f at line 24 and run 3 in g at line 24, but at hit 1 run 2 in f at line 24 paired with run 3 in f at line 24`.
  - **Values at other hits:** a selector's values (addresses and Java object numbers masked, as `no_addresses` does) line up better at other hits than hit by hit: the same values in another order (a calculator's recursive `parseExpr` against a loop: ``runs 1 and 2 saw the same values of `op` in another order (run 2's hit 4 matches run 1's hit 5: -)``), or `[A, B, C]` against `[A, C, D]` (``saw `k`'s values at other hits``). The note says the calls *may* have come in another order or number: a fix that changes which value each moment sees gives the same, so read the rows.
  - **Nothing to tell:** where the hits' values differ, selector by selector (those both runs captured), at more than half of them (at least 2), they cannot show that the hits pair, and the note says so (`the runs' values differ at 4 of 5 hits, so they cannot show that a hit is the same moment in both`).

  Each note names `queries join` with a value that names the call and that the edit leaves alone (an argument, an index), not one the edit changed. No note does not prove the hits line up.
- **Stops:** On a line with several breakpoint locations (#157), where both runs label their stops, a hit pairs only with the other run's hit at the same stop: one at another stop is two rows (`missing in run b (run a: stop 2)`, `missing in run a (run b: stop 1)`), and where the stops part (an edit that changes how often the line stops per pass, or a run saved before stops were labelled), the note says at which hit and names `join`. A `(return)` row pairs by its hit's stop, which it records (schema 15; upgrading a store fills it in for earlier runs from that hit's other rows, where each has one and all agree). Where an earlier run's `(return)` row has none (`--capture return` alone on such a line, or rows of its hit at different stops), nothing says where its call began: it pairs with no other run's row at a stop, and the note says pairing by hit cannot be checked.
- **Other queries:** `queries compare` shows every value side by side with the same notes. Labels are free text, stored as given: `diff` and `compare` name them in a `note:` line (`labels: run 1 is 'mapped-public'; run 2 has no label`), and they never change which runs are compared, nor which run a candidate's `compare:` line names. A run saved before Peeky kept notes (`runs.notes_kept` NULL) may show such values unmarked, and every query showing its values says so in a `note:` line (`notes` in JSON).

### 3.2 Inputs, Outcomes and Outputs of the Last Two Runs

```sh
peeky queries outputs
```

```
run_id  label           args                 sets  set_hit  status     error_code  output
1       mapped-public   ["::ffff:8.8.8.8"]   []    NULL     completed  NULL        ::ffff:8.8.8.8: private\n
2       mapped-private  ["::ffff:10.0.0.1"]  []    NULL     completed  NULL        ::ffff:10.0.0.1: private\n
```

Both are labelled private: the public one is the bug. `set_hit` is the hit a run's `--set` was applied at.

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

**What it answers:** For the same value of `v`, what did each run see? `diff` pairs runs by hit number, which says little when the inputs come in a different order, or when the runs took different paths (a sort's comparator: `--param key=a.name` pairs its calls on the same element).

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
- **A repeated key pairs every hit:** a value at 2 hits of run a and 3 of run b gives 6 rows, in run a's hit order and then run b's. Here run b saw `7` twice, so run a's hit 1 appears twice.
- **Unmatched hits** come last, one row each: `only in run a` (or `b`) when the other run never had that key value, or when the hit has no key value (the key cell is NULL, and `no_value_a` says why).
- **Columns:** the key, each run's hit, then each run's other selectors as `a:<selector>` and `b:<selector>` (numbered as in `wide` where only case differs), and `no_value`/`caveats` per run as in `wide`. The key's own caveat is among them: a pair made on a value Peeky cannot vouch for says so. Both runs must have captured the key.
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

**Functions.** `no_addresses(value)` masks heap and stack addresses (never NULL, short hex or quoted text) to compare runs in your own SQL; `no_addresses(value, language)` (the `evidence` view has `language`; the program's output is `runs.output`) also masks Java object numbers in Java runs. `object_ref(value)` is the Java object a value is (`Balance$Worker@100`): equal within one run means the same object (`object_ref(b.value) = object_ref(w.value)` tells a copy from the list's own element; 4.6); Python's is the `identity` column.

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

`examples/java/demo/BalanceBug.java` gives each task to the least-loaded worker, but a tie copies the worker, so the load it adds is lost (`ORDER:a a a a`).

```sh
peeky probe examples/java --config examples/java/balance.json --at demo/BalanceBug.java:29 \
  --capture 'best,workers[1]' --hits 2
peeky sql "SELECT b.hit, object_ref(b.value) AS best, object_ref(w.value) AS list_a,
  object_ref(b.value) = object_ref(w.value) AS same FROM last_run b
  JOIN last_run w ON w.hit = b.hit AND w.selector = 'workers[1]' WHERE b.selector = 'best'"
```

```
hit  best                   list_a                 same
1    BalanceBug$Worker@56   BalanceBug$Worker@100  0
2    BalanceBug$Worker@102  BalanceBug$Worker@100  0
```

`object_ref(value)` is the object a Java value is. Within one run, equal means the same object. Across runs the numbers differ (`queries diff` masks them), so compare objects only inside a run. To test the hypothesis, give the list's worker the load it should have had: `--set 'workers[1].load=5' --set-hit 1` turns `a a a a` into `a b b b`.

**Python** values carry their object in the `identity` column instead (`[id 6]` in text): an integer, debugpy's number for the object, not an address. The number alone is the object's identity; its `type` is a separate column (an object whose `__class__` is reassigned keeps its number). Is `tags`, a mutable default, the same list on every call?

```sh
peeky probe examples/corpus/mutable_default/program.py --at 3 \
  --capture 'tags,add_tag.__defaults__[0]' --hits 2
peeky sql "SELECT hit, selector, value, identity, scope FROM last_run"
```

```
hit  selector                 value       identity  scope
1    tags                     ['a']       6         NULL
1    add_tag.__defaults__[0]  ['a']       6         globals
2    tags                     ['a', 'b']  6         NULL
2    add_tag.__defaults__[0]  ['a', 'b']  6         globals
```

At one hit, equal identities are the same object: `tags` is the function's default list, not a copy. Across hits, a different identity always means a different object, but an equal one means the same object or a new one that took a freed object's place; here the default lives as long as the function, and its contents grow. Numbers, strings and `None` have no identity (NULL). Never compare identities across runs. `scope` is `globals` for a selector whose first name was read from the module's globals (5.1), and `unknown` for a row an early build of schema 6 stored (it could read globals without recording it); NULL is the frame's own variables.

### 4.7 C++ References and Pointers in SQL

**What it answers:** Which arguments did `trim_left` change? A `const std::string &` (or a pointer) is saved as the debugger shows it, its address first: `0x000000016d78b298 "apple"`. It never equals a plain string's `"apple"`, so take the text after the address before comparing.

`examples/cpp/refs/trim.cpp` trims each argument's leading spaces. Capture the reference parameter `s` in `trim_left`, then the result `name` in `main`, one run each:

```sh
peeky probe examples/cpp/refs/trim.cpp --at trim.cpp:6 --arg apple --arg " pear" --arg "fig " \
  --capture s --hits all --label argument
peeky probe examples/cpp/refs/trim.cpp --at trim.cpp:11 --arg apple --arg " pear" --arg "fig " \
  --capture name --hits all --label name
peeky sql "SELECT n.hit, s.value AS argument, n.value AS name,
  substr(s.value, instr(s.value, ' ') + 1) = n.value AS unchanged
  FROM evidence s JOIN evidence n ON n.hit = s.hit AND n.label = 'name' AND n.selector = 'name'
  WHERE s.label = 'argument' AND s.selector = 's'"
```

```
hit  argument                    name     unchanged
1    0x000000016d78b298 "apple"  "apple"  1
2    0x000000016d78b298 " pear"  "pear"   0
3    0x000000016d78b298 "fig "   "fig "   1
```

`substr(value, instr(value, ' ') + 1)` is the text after the address: an address has no space, while the text may have several (`" pear"`), so take only the first. Use it only on a reference's or pointer's value (one that starts with `0x`); on a plain string it would cut the text at its first space. `no_addresses(value)` gives `0x… "apple"`: enough to compare two references, or one across runs, but still not equal to `"apple"`. Group and count such values by it, not by `value` (`GROUP BY no_addresses(value)`): two references to the same text at different addresses are then one group. A text Peeky could not check whole carries a note after it (`"v" (shown up to a NUL at byte 1 of 3)`, libstdc++ with lldb-dap 18; 7.1), so it never equals a plain string either.

---

## 5. Step-by-Step Sessions

Use a session when you want several looks at one stop. Every command is a separate shell call; a private worker keeps the program paused in between.

### 5.1 Stop, Look, Change, Finish

**Common cases**

- `session start` stops once; `inspect` lists the locals, `get` reads values and lists their children, `set` changes one, and `close` ends the session (always close it).
- Pass `--name` to every later command when the session is not `default`.
- Session states and `inspect` name the stopped function (`in Ring::push(int)`, `in topk::insert_sorted::{{closure}}`).
- `get` of a name that is not there prints why and names the stop; any failed selector makes the exit status 1.
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
- **Globals:** `session inspect --globals` also lists a Python module's `[Globals]`, read-only (at module level they are the locals, listed once). Listing the globals runs every global's `__repr__` in the program (listing locals runs theirs too), so they are listed only on request: with `--globals`, or when `session get` or a probe reads a name missing from the locals that the function reads from its globals. Then all of them are listed and every repr runs, not only the one asked for; a misspelled name counts too, since a name the function does not bind is a global (`--capture total,i`, where `i` is a module-level loop variable, runs every global's repr at each hit).
- **Long scopes:** A scope lists at most 50 entries (debugpy's groups count) and says how many more it has; `session get NAME` reads one past them by name.
- **`session get`:** `session get` on a list, dict or struct lists one level of children, each with the selector that reads it, and `(children: ...)` on a child that has children of its own. A name that is not there prints only why (`x: <not found; visible: ...>`), and a last line names the stop it was read at (`note: this stop is in main at sched.cpp:30`: a comparator's `a` is not `main`'s); JSON gives such a row the stop's `location`. If any selector fails, every row is still printed and the exit status is 1.
- **`continue`:** `session continue` without `--to-exit` runs to the next stop at the session's breakpoint.

`session get` reads a Python name missing from the locals from the module's globals (`[module global]`, `scope: globals`) only when Peeky proves, from the function's own scoping in its source (`symtable`), that the function reads that name from its globals; only then are the globals read. A source it cannot parse, or one changed since the program started (by mtime or ctime, so `cp -p` counts), proves nothing, and the error says the name may be such a global.

In `def shadow(): print(LIMIT); LIMIT = 1`, stopped at the `print`, `get LIMIT` says `LIMIT` is a local with no value there yet (Python raises `UnboundLocalError`), not the module's `LIMIT`; a local of an enclosing function, a closure variable, a comprehension's variable or a type parameter is named as such. debugpy files functions, classes and `__dunder__` names under groups; their members are selected by name (`add_tag.__defaults__`), and so is a local holding a function (`session set callback=None` changes it). `[id 6]` is the object's identity (see 4.6).

**A pass's own value.** Stopped in a loop, a Python local that the pass assigns below the stop still holds an earlier pass's value, or one from before the loop: back in `group` with `step --out` on `key = slug(title)`, `key` is the previous title's key. Peeky shows the value with a `note`, in `get`, `inspect` and probe rows, and refuses to `set` it (the function's code overwrites it before reading it):

```
key: str = 'hello-world'
  note: this pass's code has not assigned it before this line, and every path through the rest of the pass assigns it (line 21) before reading it: the value is likely from an earlier pass (or from before the loop at line 20)
```

- **How:** Peeky reads the function's bytecode, compiled from its source as the program's interpreter compiles it (plain and as `-O` does; both must agree), and only where Peeky runs on an interpreter that compiles alike (the same implementation, exact version and build, and bytecode magic number). The debugger gives a line, not an instruction, so the stop may be at any instruction of the line (a breakpoint's stop only where the line begins), and all must be in the same innermost loop (a `for` or `while` around the line). No path from the loop's head to the line may store the local, none from the line may read or `del` it before storing it (an exception's path counts), and every path through the rest of the pass that runs no exception must store it before the pass ends (back at the loop's head, out of the loop, a `return` or a `raise`): a store only in an `except` block, behind a `continue` or `break`, or on one branch proves nothing. An exception that a handler in the loop may swallow (a `with` whose `__exit__` may return true, an `except` that falls through, an `async for` that ends) is a path too: one that goes on in the pass without the store takes the note away, and the note names the store on each path (`lines 11, 13` for `x = 1` in the `try` and `x = 2` in its `except`).
- **No note:** a loop's header line (its code also runs before and after the loop); a line inside a `try` whose handler can skip the assignment for a later read (`key = str(x)` with `except ValueError: pass`, then `return key` after the loop); a local a closure shares; module-level code; a source changed since the program started; and any function of a module (the whole file) that may run other code than its source says (`exec`, `eval`, `compile`, `types.FunctionType`, `__code__`, `marshal`) or reach a frame's locals (`locals()`, `vars()` or `dir()` other than with one plain argument, `sys._getframe`, `f_locals`, `f_back`, a tracer, the `inspect` or `builtins` module, `__builtins__`, `__import__`, `globals()`, a string naming a builtin such as `getattr(b, "exec")`), whatever a module is imported as. The module global of that name (`--globals`) is never noted.
- **Limits:** the note states what the function's code does, not what ran: code in other modules that reaches into this frame (a helper writing `sys._getframe(1).f_locals`, which Python 3.13 writes through) or replaces this module's functions is not seen, hence "likely". Nor is a stop part-way through the assignment itself (in a `__del__` of the value it replaces).

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

- **Module level:** At module level the locals are the module's globals, so there is no separate `[Globals]`.
- **Caller frames:** A frame past the stack is `INVALID_REQUEST`, naming how many frames there are. Rust's owned-local and Box proofs hold only in the innermost frame, so a caller's `Vec` keeps its raw fields with a note.
- **C and C++ callers:** In C and C++, a caller's local is withheld while its declaration's code is not finished: in `long result = twice(seed);`, or `long multi =` over `twice(seed + 1);`, the value is stored only after `twice` returns (so are a `for` loop's counter while its body calls, on one line or several, since the increment's code follows the body, and a local whose declaration line has no code of its own: an array of structs, a nested struct or a 2-D array initialized over several lines is stored element by element on its elements' lines). So is a local whose declaration statement, read from the source to its `;`, is still running on the caller's line: in `struct trio t = { seed,` / `{ twice(3),` / `twice(4) } };`, the first line's code runs before the calls and `t.p` is stored after them.
- **Headers and other files:** A variable declared in a `for`, `if`, `while` or `switch` header counts until the `;` after the whole statement, so it stays withheld while the body calls, and where the source does not give the end, the local is withheld: brackets that do not balance, or text the compiler may read differently from the scan (between the declaration and its end, a directive such as `#include`, a line splice, `<::`, or a macro of the program's sources and headers that leaves a bracket open or holds a `;` outside its brackets; anywhere in the file, `#line`). So is every local whose declaration is in another file than the caller's line: an initializer can `#include` its rows, or be included itself.
- **How the source is read:** C's trigraphs are read as `-std=c11` reads them (`??/` is a backslash); C++17 has none. Digraphs (`<% %> <: :>`) are brackets, and lines are counted as the compiler's debug information counts them (`\r\n`, a lone `\r` or `\n`; `\n\r` is two).
- **Changes:** A caller's values cannot be changed (`MUTATION_REJECTED`), so caller listings never say `[settable]`.

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
- **Java:** For Java, `--into` and `--out` skip JDK classes (`java.*`, `javax.*`, `jdk.*`, `sun.*`, `com.sun.*`), so `step --into` on a line that calls `Math.min` stays in your code and reaches lambda bodies. A plain `step` at the end of a method that JDK code called (a `Runnable`, a comparator) stops outside your sources; `continue` from there. Java adapter bundles built before stepping are refused with a hint to rebuild them.

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
- A Rust local kept in a register can be kept in memory by a candidate edit (`std::hint::black_box(&mut x);`) and set in the same probe.
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
- **Intervening where no local can be set:** in Java, Rust, C and C++, `--set` changes a scalar field or element directly (`--set s.end=10` where `s` is a struct local; see 7.1 and 7.2 for which ones). `--set` also works in the same probe as `--old`/`--new`. Where the value you want to change cannot be set (a Python field, or one reached through a pointer, a reference or a `Box`, as `last.end` is when `last` is a `&mut Span`), add a local holding it with the edit, on an earlier line (a local is visible only after its declaration), and set that local. In Rust, at the line `last.end = s.end;` that follows `if s.start <= last.end {`:

  ```sh
  peeky probe spans --at src/main.rs:23 --capture end,last.end --arg 1-10,2-3 \
    --old 'if s.start <= last.end {' \
    --new 'let mut end = s.end; std::hint::black_box(&mut end); if s.start <= last.end {' \
    --old 'last.end = s.end;' --new 'last.end = end;' --set end=10
  ```

  This reports `set end: 3 -> 10`. `black_box` keeps the new Rust local in memory, where a change reaches the program.
- **A Rust local kept in a register:** a by-value parameter, or a `let` the code never borrows or reassigns, may live only in a register even in a debug build, so the debugger shows a copy and a change would not reach the program. Rows mark it `[not settable: register]` (a struct's fields too, in its preview) and `describe`'s `mutable:` line lists them per function. A probe that sets one is refused before the program runs (the debugger's launch request is not sent, no run saved), as is any `--set` its build rules out in every function on the `--at` line (#138), and any `--set-at` the build rules out on its own line (2.8). Keep it in memory with the edit, in the same probe: for the limiter's `fn allow(w: &mut Window, now: u64, limit: u32, width: u64)`,

  ```sh
  peeky probe limiter --at src/main.rs:10 --arg 2 --arg 10 --arg 0,1,10,11 --capture width \
    --old 'limit: u32, width: u64) -> bool {' \
    --new 'limit: u32, mut width: u64) -> bool { std::hint::black_box(&mut width);' \
    --hits 3 --set-hit 3 --set width=9
  ```

  This reports `set width: 10 -> 9`, and the output the program printed with the change. Borrowing the parameter itself keeps it in memory; a shadowing `let mut width = width;` would not, since a name is settable only when every binding of it is in memory. For a `let` local, add `std::hint::black_box(&mut x);` after its `let` (declared `let mut`).
- **Python:** like a compiled candidate, it runs from a private copy of the target root with the edit applied, and that copy is the working directory. Files keep their bytes, modes and times, and directories their modes.
- **What it reaches:** Whatever the script reaches through its own path, an import (also through `PYTHONPATH`, whose relative entries keep meaning your root, or an editable install, whose paths into the root are moved to the copy; one mapping a package to a directory of another name gets a note, and each name the root offers is then looked up as the program will import it: one still served from your tree, by an import hook ahead of every path as meson-python's is, gets a note, and such a hook may rebuild in its build directory), or another process (multiprocessing, a subprocess of `__file__`) is the copy, so all of it runs the edit. The program's writes stay in the copy, except writes inside an environment directory or through a link that leaves the root, which reach the originals:
  - A repository (`.git`, `.hg`, `.svn`) is copied whole, following its links, so `git add`, a commit, a rewritten index or a touched object stays in the copy. A `core.worktree` inside the root moves to the copy, and so does each linked worktree's entry: one outside the root is listed at a path that does not exist, with a note. So is any git directory under another name (a bare repository, `.bare` beside its worktrees); a bare repository as the root itself gets its worktree entries moved, or the candidate is refused. The copy's index is refreshed for the copied files. If the copy's git configuration differs from yours (a conditional include on the repository's path, `includeIf "gitdir:..."`), a note says so.
  - Some repositories are left out, with a note:
    - one over 20,000 files or 256 MB;
    - one that borrows objects from another (`alternates`, `--shared` clones);
    - one whose `core.worktree` is outside the root;
    - a `.git` file naming metadata outside the root (a linked worktree's).
  - Pure caches (`__pycache__`, `.mypy_cache`, `.ruff_cache`) are left out. Environments (`.venv`, `venv`, `.tox`, `.nox`, `node_modules`, `site-packages`, `.eggs`) are directories of links to the originals' entries: the interpreter uses them from either tree.
  - A directory nobody can open (another user's `pgdata`) is an empty one with its mode. One that can be entered but not listed is refused.

  Nothing above the target root is copied, and in general no reading of the source can tell whether a program reaches there. So the result notes it unless the root is a project's (it holds `.git`, `pyproject.toml`, `setup.py` or `setup.cfg`) and no repository above it holds it (a monorepo's package).

  Nor does it when the edited script is self-contained: it imports only standard modules that open no file, start no process and reach no network (`abc`, `array`, `bisect`, `cmath`, `collections`, `copy`, `dataclasses`, `decimal`, `enum`, `fractions`, `functools`, `heapq`, `itertools`, `json`, `math`, `numbers`, `random`, `re`, `statistics`, `string`, `textwrap`, `typing`, `unicodedata`; of `sys` only `argv`, the standard streams, `exit`, `maxsize` and the recursion limit), and names nothing that could reach one (`open`, `exec`, `eval`, `getattr`, `vars`, a frame's globals, `__file__` or another dunder but `__name__` and `__init__`, a module's private names or submodules).

  Text shows the note once per root and state directory, never with `--quiet`; `--json` always has it. If the program reads or imports anything from above, give the project directory as the target (`peeky probe proj --at scripts/run.py:14`). `--run` entry scripts are refused.

---

## 7. C, C++, Rust and Java

The commands are the same for every language; only the target changes.

### 7.1 C and C++

**Common cases**

- **Read** any value by selector: `s.end`, `bins[3].hi`, through a pointer with `.` (`ps.start`), C++ members as `this.head_`, a map by key (`m[1]`). A `std::vector`, `std::array` or `std::map` row shows its first elements (`size=3 ["users", "42", ""]`; not in an optimized build or a program running several threads, nor while a library call changes it), so a capture reads them without a session.
- **Set** a 16- to 64-bit integer local, or such a field or element of a struct or array local the stopped frame owns (`--set s.end=10`), at a statement's first line; rows mark it `[settable]` (an 8-bit `char` is refused).
- **Set a text** in a `char[N]` or `std::string` local (`--set 'name="apple"'`), within its size, never reallocating; not through a `char *` or a reference, nor in a const one. lldb-dap 21 (macOS) writes every case, older ones fewer (**Changing text** below).
- **A field through a pointer** (`r->tail` in `push(struct ring *r)`, `this.count`) cannot be set: set it in the frame that owns the struct, before the call (in C, when the program calls the function once, the refusal names that call: `push has one caller: …`).
- **`no value yet`** is a local not initialized at this stop (declared below it, an initializer still running): read it below its declaration (the row says where: `stop after line 35 to read it`). A `note` is a value Peeky cannot vouch for: on a line a loop returns to (a `do` body's first) the next line reads it; below a `setjmp` call, a local declared at or below the stop shows a `note` instead of `no value yet`, and reads plainly below its declaration.
- **Same-named locals:** a plain name is the declaration the code names at the stop; `name @ file:line` selects another.

**Quick start (C and C++).** What can be read, what can be set, and where:

- **Read** any local, parameter or global by selector: fields and elements (`s.end`, `bins[3].hi`, `grid[1][0]`), through pointers too (`ps.start`), C++ members through `this` (`this.head_`), a `std::string` whole, a `std::vector` by index and a `std::map` by key (`m[1]`, `ages["al"]`).
- **Structs, arrays and most standard containers** (**Standard containers** below) show their members: `bins` reads `[{lo = 0, hi = 2, count = 0}, …]`, `bins[3]` `{lo = 6, hi = 7, count = 0}`, a `std::vector<std::string>` `size=3 ["users", "42", ""]`, in probes, `inspect` and `session get`. Unions and what pointers point at are never read into a preview.
- **Withheld, never guessed:** a local whose storage may still hold leftovers (declared below the stop, an initializer still running, a jump past its declaration) shows `no value yet` and why; one Peeky shows but cannot vouch for (a `do` body's first line, below a `setjmp` call) carries a `note`, and so does every field and element read through it (``read through `q`: …``, in captures, in `session get` children, marked `[note]`, and in the evidence), which `--set` refuses where it refuses the local.
- **Set** integer locals, and integer fields and elements of a struct, class or array local the frame holds itself: `--set s.end=10`, `--set 'bins[3].hi=8'` (16 to 64 bits, in range).
- **Text:** a `char[N]` local or a `std::string` local takes a text in double quotes: `--set 'name="apple"'` (see **Changing text** below).
- **Never** through a pointer or a reference (`ps.end`, `this.count`, a C++ structured binding of a reference, `auto &[at, n] = *owner`, as `n` or `at.x`, or a C `__block` variable, all `[not settable: reference]` as fields; in C, a refusal for a pointer parameter names the function's call when the program's sources have only one: `push has one caller: m.c:15 in main, which passes `&r`, where `r.tail` is settable`), nor in a union, a packed struct, a bitfield, a const or a `std::` type; floats cannot be set, and a text only as above. A probe whose `--set` the build's debug information rules out like this in every function on its `--at` line is refused before the program runs (the debugger's launch request is not sent, no run saved); parameters are set as other locals (an unoptimized build keeps them on the stack).
- **Where:** at a statement's first instruction, so a breakpoint on its first line (`--at`, `session continue --to LINE`) or a `step` that lands there; not after `step --out`, not on a loop header's later passes.
- **Marks:** rows show `[settable]` on what `set` would change, fields and elements included (a preview's too); the fields of a local the debug information keeps in registers or as a static say `[not settable: register]` or `[not settable: static]`; when a rule leaves nothing settable, `session inspect` says why. A refused change's hint names the one way around it.
- **Later hits:** `--set-hit N` applies a probe's changes at hit N; in a session, change values at as many stops as you like.

The rest of this section shows them at work, then the detail behind them.

**Structs and arrays of structs.** A struct, class or fixed-size array shows its first members, read from the debugger's own children (#136). In the sixth evaluation's `histo` case ([report](evaluations/2026-10-20-agent-sixth-round.md); `struct bin bins[4]`, each `{lo, hi, count}`), one capture reads every bin:

```sh
peeky probe histo.c --at histo.c:28 --arg 0 --arg 7 --arg 7 --capture 'bins,bins[3]'
```

```
hit  phase     line  function  selector  type    value
1    observed  28    main      bins      bin[4]  [{lo = 0, hi = 2, count = 0}, {lo = 2, hi = 4, count = 0}, {lo = 4, hi = 6, count = 0}, {lo = 6, hi = 7, count = 0}]
1    observed  28    main      bins[3]   bin     {lo = 6, hi = 7, count = 0}
```

`session get bins` shows each element so, and its selector (`bins[3]: bin = {lo = 6, hi = 7, count = 0} (children: peeky session get 'bins[3]')`); fields are still read and set by name (`bins[3].hi`, `--set 'bins[3].hi=8'`).

- **Previews:** A preview shows 8 members or elements, then `…`, two levels deep (`{…}` below), at most 40 characters of one member. A union is not previewed (`<union>` inside another value: only one of its members holds what was stored); a pointer inside shows its address, never what it points at; a member with no value shows `<no value>`; a C++ base class shows as a member named by its class (`{Base = {b = 1}, d = 2}`); a standard container as below (`v = size=2 [1, 2]`), and any other `std::` value its debugger's summary or `{…}`.
- **No preview:** Types come from the build's debug information, and where it cannot vouch for one there is no preview, only the debugger's `T @ 0x…`: a local withheld at the stop, a variable-length array, an incomplete type (`op[0]` on `struct opaque *`), a map's entry (`m[#0]`, a `std::pair`), and every value of an optimized build.
- **Standard containers** (#181): a `std::vector`, `std::array`, `std::deque` or `std::list` shows its size and first elements (`have: size=3 ["users", "42", ""]`), with the bounds above; elements as their type shows (`size=2 [{x = 1, y = 2}, {x = 3, y = 4}]`, `size=2 [size=2 [1, 2], size=1 [3]]`).
  - **Maps and sets:** a `std::map` or `std::unordered_map` shows its entries in iteration order as `key: value` (`size=3 {1: 10, 2: 20, 5: 50}`, `{"a": {x = 1, y = 2}}`), a set (`std::set`, `std::multiset`, `std::unordered_set`, `std::unordered_multiset`) its elements (`size=2 {1, 3}`); a `std::multimap` keeps its summary (`size=2`). Selectors are as before: `m[1]` reads key 1, `s[#0]` the first element.
  - **Where they come from:** the debugger's children, which its formatter reads from the container's own storage, `size()` of them, each shown as the class template's argument in the build's debug information says (`_Tp`, `_Key`), never by LLDB's type names; a container the debugger lists otherwise (its own members, fewer elements than its size) keeps its summary. A `std::array` LLDB lists by its C array (`ai.__elems_`) shows that array's elements.
  - **References and pointers:** a reference shows its address, then the container (`0x… size=3 [1, 2, 3]`); a pointer its address and LLDB's summary (`0x… size=3`), and `p[0]` the container.
  - **Withheld:** a container not yet constructed is `no value yet`, as any local; an optimized build's shows its summary alone. A moved-from one shows what it holds: libc++ and libstdc++ leave it empty (`size=0 []`), as the program's `size()` says.
  - **A change under way:** a container shows its summary alone (`size=2`), and its elements no preview of their own (a struct element `Noisy @ 0x…`), where a standard-library function, or a constructor, destructor or assignment operator, may be part-way through changing it:
    - in a program running more than one thread: a stop pauses the others wherever they are (Peeky counts the threads lldb-dap lists, those LLDB's `thread list` lists and those that said they started, and withholds where any of them does not answer);
    - in a caller frame below a library function (`session get o.items --frame N` on `main`'s frame while `o.items.clear()` runs an element's destructor, the vector still counting an element already destroyed), or below a constructor, destructor or `operator=` of any class (`slot->~E(); ::new (slot) E(3);` in `main`, stopped in `E::~E()`);
    - for one reached through a pointer or reference (`this.owner[0].items`, a reference parameter, a structured binding of one, `auto &[items, n] = *owner`, a static local), wherever the stack holds such a frame (a `std::thread`'s or `std::function`'s too, or the frame's own destructor) or holds 64 frames or more.
- **Characters:** A wide character array shows element by element (`[L'a', L'\0', L'b', L'\0']`, where LLDB's text stops at the first NUL), and a char-typed bitfield as its number (`sc = -3`, not LLDB's `'\x05'`).
- **Details:** Details: NATIVE.md, CPP.md.

```sh
peeky probe examples/c/pagination_bug.c --at 25 --capture 'start,page,items[1]' --set start=0
```

```
hit  phase      line  function  selector  type  value
1    observed   25    main      start     int   3  [settable]
1    observed   25    main      page      int   1  [settable]
1    observed   25    main      items[1]  int   20  [settable]
1    after-set  25    main      start     int   0  [settable]
after-set: 2 rows equal to the hit's observed value left out (`--json` has every row)
set start: 3 -> 0
status: completed  hits: 1  seconds: 0.745
output:
  PAGE:[10, 20, 30]
```

```sh
peeky probe examples/c/pagination_bug.c --at 25 --capture start,page --arg 2     # PAGE:[70, 80]
peeky probe examples/cpp/pagination_bug.cpp --at 28 --capture start
```

**Building.** All `.c` files in the directory that don't define their own `main` are built together with the target. A directory holding several programs links only the target's `main`. A directory works as the target too: `peeky probe ring/ --at 16` finds its one program (a checker script beside it doesn't count), and `--at ring.cpp:16` names one when there are several. Headers read from outside the directory, including SDK headers, are hashed and rechecked.

**Locals Peeky withholds.** In short, a C or C++ local is withheld (`no value yet:` and why) when it is declared below the stop, in a running initializer, past a `case`/`goto` label, `this` in a constructor, hidden by a block's `static`/`extern`, or declared on the stop line where control enters it (`declared on line 3, which has not run yet`), not mid-line (after `step --out`). The cases in detail:

A C or C++ local declared below the stop line is not shown as a value: until its declaration runs, its storage holds whatever an earlier call or loop iteration left there, so the row says `no value yet: declared at line 18, below this stop` (the declaration the debugger shows, the one in the stopped function whose block holds the stop, is declared below it: another of the name, in an earlier loop or a lambda, changes nothing; where Peeky cannot tell which is shown, any declared below withholds it. A static or parameter keeps its value).

- **Where the stop cannot be placed:** Where Peeky cannot place the stop before the declaration (the line also runs code after it, as a temporary's cleanup does, or a `goto` label lets control return above it), it is withheld all the same, saying it may not be initialized.
- **`setjmp`:** In a function that calls `setjmp` (or a macro expanding to it), below the call (a stop on the call's own line is not: a `longjmp` returns past the call), a local declared at or below the stop is shown with a `note` instead (a `longjmp` can bring control back past its declaration with it live, or this is the first pass) and cannot be changed; what is declared above the stop, and every other function, is judged as usual.
- **Names a block declares:** Inside a block that declares an outer local's name otherwise (a `static`, `extern` or `typedef`, an enumerator, also one of an enum inside a C struct's body, a function declaration, a C++ `using` declaration, class or structured binding (`auto [t, u]`, also a range-`for`'s or an `if`'s, which clang 18 on Linux does not describe; a range-`for`'s counts in its loop, not in a lambda in its range, nor at the line's first instruction, where the range runs first), or a `thread_local`, which the debugger lists apart as `q @ file:line`; a block opening on the stop line counts), the code names that one: the outer local's row says `not what the code reads` and cannot be changed.
- **How the source is read:** Peeky reads the source as the build's compiler preprocesses it (`-E`, the same flags), so whatever a macro expands to, and only what a conditional keeps, counts; a prototype's parameter names do not. Where it cannot read that (the preprocessor fails, or a system header is included inside the block), the row says `may not be what the code reads`.
- **Block names in optimized builds (`-O1`, `-O2`, #173):** a block's declaration hides the outer local there too, though the outer local lives in a register or as a constant rather than in the frame.
  - On a range-`for`'s line, the stop cannot be told from the line's first instruction, where the range runs with the outer local: the row says `may not be what the code reads` and names the binding.
  - A block's `static` that is never changed is kept as a constant, and lldb-dap lists it among the frame's locals as `t @ file:line`. That row is the static the code names, so it is shown; the outer local's row says the static is `listed separately as` it.
- **On the stop line:** A local declared on the stop line itself is withheld at the line's first instruction, where its declaration has not run yet, so probe the next line to read it; on a line a loop returns to (a `do` body's first line) it is shown with a `note` that it may be the last pass's value, and mid-line (a `for` counter on later passes, after `step --out`) as its storage reads.
- **Part-way through the stop line:** A local declared on the stop line is withheld part-way through that line while the line still has code to run (back from a call with `step --out` in `long r = twice(3) + twice(4);`), except a variable a `for`, `if` or `switch` header sets with no call (`for (int i = 0; ...)`), which a one-line loop keeps live; a `while`'s, or one set by a call (`if (long r = twice(3))`), is withheld.
- **While its declaration runs:** A local is also withheld while its own declaration is still running at the stop: `struct outer o = {` over lines 17-22 (or an array of structs, or a 2-D array), stopped at line 20, says `its declaration (lines 17-22) is still running at this stop` until its `;` has run. The end comes from the source, read as for caller frames (5.1), and where it cannot be found the local is withheld; a declaration below the stop whose own line has no code is placed by its elements' lines.
- **Header variables:** A variable a `for`, `if`, `while`, `switch` or `catch` header declares has run with its part of the header, so the body reads it.
- **Below a label:** Below a label a jump can reach past a declaration, the local is withheld too (`a jump ... can reach the label on line 21`): a `case` or `default` of the `switch` whose body holds the declaration, or a label that a `goto` above the declaration names, or one outside its block jumps back to (re-entering a block starts its locals anew, C11 6.2.4; any label, with a computed `goto`). A `goto` counts only within its function's own lines, so a namespace's other functions do not.
- **Declared in an `#include`d file:** A local declared in an `#include`d file counts as declared at that `#include`'s line: below the stop it is withheld, and above it, when its file may `#include` more after it (a `case` there can skip it).
- **Outside the function's own lines:** When the frame (stopped, or a caller on its own file's line) runs a line outside its function's own (a class's default member initializer, as when you break on `long b = twice(2);` in `struct Cfg`, also a class defined inside the function, or an `#include`d fragment), the usual rules run from the own line that brings it in (that `#include`), or else from the last own line whose code comes before it; its class, struct and array locals are withheld too, since one of them is being built there, and where Peeky cannot place the stop, everything but parameters is.
- **`this` in C++:** In C++, `this` is withheld while its object is being built: in a constructor, in any frame a constructor called (Peeky reads up to 64 frames to tell; past that it withholds), in a lambda defined inside a declaration or a constructor that may still be running (the note names that declaration), and in a lambda a class's default member initializer defines (`long b = [this] { ... }();`), since a class with no constructor runs its initializers where the object is built. An unnamed struct's constructor (`$_0::$_0()`) counts as a constructor too.

**Members.** In C++ and Java methods, read members through `this` (`this.head_`); a bare `head_` that is a member gives an error naming that selector. C++ changes no member through `this` (a pointer); Java does (`this.total=0`). Peeky never reads a member in place of a local: the debugger's list of locals can be incomplete (statics, classes built without local-variable tables), so a missing local does not mean the code uses the member.

**Strings with a NUL byte (C++).** LLDB shows a libc++ (macOS) `std::string` whole, a NUL as `\0`: `"a\0b"`. Its summary of a libstdc++ (Linux) string is a C string, which ends at the first NUL, and a value LLDB names `std::string` gets `error: summary string parsing error` on clang's Linux builds. So on Linux the worker checks each string against its `_M_string_length` (#128), in `inspect`, `get`, listings, probe rows and the evidence store alike:

- With a debugger that reads memory (lldb-dap 20), it reads the string's bytes and shows them as LLDB shows a libc++ string (`"a\0b"  [memory]`; past 1024 bytes, `"..."...`), for `std::string`, `std::wstring`, `std::u16string` and `std::u32string`, by value, reference (`0x… "a\0b"`) or pointer.
- Without one (lldb-dap 18), a text the debugger cut at a NUL keeps a note: `"v" (shown up to a NUL at byte 1 of 3)`. A string whose summary failed has no value (`no readable value: the string's summary failed ..., and this debugger cannot read memory (lldb-dap 20 can); it holds 3 bytes`); a pointer or reference to one shows `0x… <no text: ...>`.
- A string the build's debug information gives no members (clang leaves `std::wstring` to libstdc++'s own) has no length to check: its text gets `(not checked for a NUL byte: ...)`, or no value where LLDB's summary failed.
- A string under a typedef's name (`typedef std::basic_string<char> Bytes`, `using Name = std::string`, a reference or pointer to one) is checked the same: Peeky resolves the typedef from the build's debug information, or knows the string by its libstdc++ members. LLDB's `error: summary string parsing error` is never shown as a value, for any type.
- On every platform, a literal reads back in C as the same bytes: where LLDB writes `\0` before a digit (`"n\064"`, which C reads as `"n4"`) or `\xff` before a hex digit, Peeky writes `"n\00064"` and `"\377a"`. Long text stays within 2000 characters, cut between characters with `...`.
- Each libstdc++ string costs a debugger request (two or three where its bytes are read), within the inspection's budget: a large page of strings may say `(not checked for a NUL byte: ...)` instead.
- A `std::string` local takes a text in double quotes (`--set 'name="apple"'`), within its capacity: see **Changing text** below. Its NUL bytes count as text there too: a new size is written with the bytes.
- A string is its text: `session get name` shows `name: std::string = "apple"` and no rows under it (the library's own members, `(anonymous struct)` or `_M_dataplus`, are never listed), and `inspect` offers no `(children: ...)` for it. Selecting inside one (`name[0]`, `name.size`) is refused with what to do instead: capture the string and compare its text, in SQL (4.7). A pointer to one shows its address and text, and `p[0]` is the string.

**Changing text.** A value in double quotes is a text (JSON's string syntax, so `\"` and `\u00e9` work), which Peeky writes in the storage the local already has, and only there (#137):

- **A `char[N]` array:** the text and a NUL, when both fit in its N bytes (the debug information gives N); the bytes after the NUL stay as they were, and the debugger shows them after `\0` (`set name: "apple :4" -> "apple\0:4"`). A text that does not fit is refused (`name` is a char[16], which holds 15 bytes of text and a NUL), as is one with a NUL in it.
- **A `std::string`:** the text, a NUL and the new size, in its own short buffer (libc++: 22 bytes; libstdc++: 15) or in its heap buffer, within its current capacity, never reallocating: `set name: "apple " -> "apple"`. Peeky reads where the text is held from the debugger's members and checks it against memory; a longer text is refused (`with room for 22 bytes, and the text is 30 bytes`), as is a layout that does not check out (a string not yet constructed). A heap buffer's room is the string's `capacity()`: libc++ keeps the allocation's size in `__cap_` (macOS arm64's layout) or half of it (its standard layout), and where its short buffer lies tells which.
- **Views taken before:** a `std::string_view`, a `c_str()` or `data()` pointer or an iterator the program took from the string before the change keeps the old size and reads a mix of old and new text (`string_view sv = s;` then `s="bye"`: `sv` reads `bye\0o world`). Peeky cannot see those, so every `std::string` change says so (`note` on the change in JSON, and a warning).
- **Debuggers:** lldb-dap 21 (macOS) writes memory, so every case above works. lldb-dap 18 and 20 cannot: Peeky writes an array's text one element at a time, for printable ASCII only, and a libstdc++ string's only in its short buffer and only with lldb-dap 20, which reads memory to tell where the text is (lldb-dap 18 cannot, so its strings are refused); a heap buffer needs lldb-dap 21. Rows say `[settable]` where a change can be made: with lldb-dap 20, a string whose text is in its own buffer.
- **Never** through a pointer or a reference: a `char *` may point at a string literal (read-only) or a buffer of unknown size, and a `const std::string &` at a string another frame owns. Never a `const` or `volatile` one either (`const std::string cs`, `const char tag[8]`, a typedef of one: the program may never change a const object), judged from the build's debug information, which also tells a typedef's array or string (`using Name = std::string` is changed as a `std::string`). Never into a field or element either (`rec.name`): copy it into a local with a candidate edit. All `--set` rules apply (a statement under way, a local not yet initialized, an optimized build).
- **Read back:** every byte written and the new size are read back; anything that does not read back as asked closes the session.

**Maps and sets (C++).** The debugger lists a `std::map`'s entries as `[0]`, `[1]`, ... in iteration order, but in C++ `m[1]` is the value for key 1. Peeky reads `m[K]` as C++ does, and `m[#N]` is the Nth entry in iteration order (a pair with `.first` and `.second`), so a position is never taken for a key (#119). In `examples/cpp/maps/scores.cpp`, `m` is `std::map<int, int> m{{2, 20}, {1, 10}, {5, 50}}`:

```sh
peeky probe examples/cpp/maps/scores.cpp --at 9 --capture 'm[1],m[#1].first,m[#1].second,ages["al"],s[#0],s[1],m[3]'
```

```
hit  phase     line  function  selector      type       value
1    observed  9     main      m[1]          int        10
1    observed  9     main      m[#1].first   const int  2
1    observed  9     main      m[#1].second  int        20
1    observed  9     main      ages["al"]    int        42
1    observed  9     main      s[#0]         int        1
1    observed  9     main      s[1]                     <[1] not read: s (std::set<int>) is a C++ set, which has no [] in C++, and the debugger lists its elements in iteration order, not by index; select an element by position with s[#N] (`peeky session get s` lists them)>
1    observed  9     main      m[3]                     <[3] not found: m has no key 3 (keys: 1, 2, 5); in C++, m[3] would insert it with a default value>
```

- `m[K]` reads `std::map` and `std::unordered_map` (libc++ and libstdc++) with integer keys (`m[1]`, `m[-1]`; one outside the key type's range is refused) or `std::string` keys (`ages["al"]`; printable ASCII without `"` or `\`), matched exactly as Peeky shows each key (strings with a NUL byte below), and of the key's length. A key whose length cannot be read is refused. That holds through a typedef (`using Index = std::map<int, int>`, or a class's member typedef such as `Holder<T>::S`; the build's debug information names its class), a reference, a pointer (`mp[0][1]`, as C++'s `(*mp)[1]`), a struct (`h.m[1]`) and a vector of maps (`vm[0][4]`). A missing key is `not found`, with the keys there are; nothing is inserted.
- Keys of any other type (`double`, `char`, `bool`, enums, structs, `const char *`) are refused for `m[K]`: read those entries with `m[#N].first` and `.second`.
- A map with a comparator of its own (`std::map<int, int, ByAbs>`), or a hash or equality of its own, reads an exact key (`am[-1]`) but never says a key is missing: C++'s `am[1]` reads the entry whose key `ByAbs` treats as equal (`-1`), so `am[1]` says no key equals 1 exactly and names `am[#N]`. `not found ... would insert it` comes only with `std::less`, `std::greater`, or `std::hash` with `std::equal_to`.
- Sets, multisets and multimaps have no `[]` in C++: `s[1]` and `mm[1]` are refused, and `s[#1]`, `mm[#1].second` read by position. `t[1]` is refused too where `t`'s type is a typedef whose class the debug information does not give and the debugger lists pairs with a const key, as it does a map's entries: it could be a key or a position (`t[#1]` reads by position). So is a type the debug information gives several classes, a map or set among them (`std::map<int, std::set<int>>::mapped_type`, where other maps' `mapped_type` is `int`).
- `session get m` lists `m[1]: int = 10`, `m[2]: int = 20`, ... when the keys compare (each selector reads the value its row shows), and `m[#0]`, `m[#1]`, ... otherwise, with a note saying which. An entry whose key no selector can write (more than six digits, a string with other characters or a NUL) is listed by position, and no two entries share a label.
- A `std::map` with integer keys in the standard order (`std::less`, the default, or `std::greater`) and more than 20 entries is bisected, so any size up to 512 entries reads in a few steps. Any other map is compared entry by entry, and one inspection (512 nodes) holds the entries and keys of a map of about 200; a bigger one is refused before any is read (use `m[#N]`).
- Vectors, deques and arrays index by position as before.

In a session, select a map's entry by key the same way, quoted for the shell, and pass the session's `--name` to every command:

```sh
peeky session start examples/cpp/maps/scores.cpp --at 9 --name maps
peeky session get 'ages["al"]' ages --name maps
peeky session close --name maps
```

```
ages["al"]: int = 42
ages: std::map<std::string, int> = size=2
  ages["al"]: int = 42
  ages["bo"]: int = 31
  note: ages is a C++ map: ages[K] is the value for key K, as in C++ (integer and std::string keys), and ages[#N] its Nth entry in iteration order, a pair with .first and .second
```

**Changing fields and elements.** `--set` (and `session set`) changes an integer field or array element of a local the stopped frame holds itself, as it does an integer local (#105):

```sh
peeky session set s.end=10        # set s.end: 2 -> 10
peeky session set 's.arr[2]=7'    # an element of an array in a struct
peeky session set ps.end=10       # MUTATION_REJECTED: `ps` is a pointer: Peeky changes only values the frame holds itself ...
```

**What `set` checks.**

- **Path:** The path is checked against the build's debug information, not the debugger's type names: fields of structs and classes and elements of fixed-size arrays only, never through a pointer (including `this`), a reference, a base class or an anonymous member, nor inside a union, a packed struct or a `std::` type; the target is an integer of 16 to 64 bits, not const, volatile or a bitfield, and the value must fit it.
- **Through a pointer or a reference:** Peeky does not write through a pointer or a reference, since what it reaches may be freed or moved; change the value where it is owned, before the call that passed it: a probe `--at` the call's line with `--hits N --set-hit N`, or `session start --at` it with `--hit N`, where N counts the call line's own stops, not the called function's, which differ when it is called from several lines (find N with `peeky probe ... --at <call line> --capture <argument> --hits all`); `session continue --to` the call's line reaches only the next call.
- **The function's one caller (C):** In C, when the program's sources call the function once, the refusal names that call instead (`push has one caller: m.c:15 in main, which passes `&r`, where `r.tail` is settable: a probe's `--set-at 15@N r.tail=<value>` ...`).
  - It says settable, and how to change it, only where a stop on the first line of the call's statement can make the change and the call sees it: the call is in the first statement on that line (`r.tail = 1; total += push(&r, 1);` overwrites it first); its statement, read as the compiler preprocesses it (a macro's code where it is used), is `push(ARGS);`, `NAME = push(ARGS);` (`+=` and the like too), a declaration's `int NAME = push(ARGS);`, `return push(ARGS);` or `if (push(ARGS) ...)`, NAME not the root; and ARGS, and the rest of an `if`'s condition, only read: no assignment, `++`, `--` or `&`, no call of any kind (`atoi(s)`, `ops[0](r.buf)`, `(*fp)(x)`, `v.fn(x)`, `sizeof(x)`), the root only as the `&r` passed and in reads of its integer fields (`r.head`; not `r.buf`, a pointer into it).
  - Anything else may change the value first (`r = q, push(&r, 1)`, `push(&r, RESET(r))`, `push(&r, ops[0](r.buf))` through the array at `r`'s start), so the eval's `push(&r, atoi(argv[i] + 1))` is named alone.
  - Also, the caller takes no address of the root or its fields elsewhere (a pointer `a = &r` may change it), but to pass `&r` whole to a function of the same file that only uses its fields through it (`pop(&r, &v)` reading `r->count`, `r->buf[i]`); the root is declared above, and these rules allow the change at that stop (`int total = 1 +` / `push(&r, 1);` is named by its statement's first line, 10, whose stop comes after the call).
  - A call in another source file is changed by a probe `--at` that file's line (`--at other.c:6 --set q.tail=<value> --set-hit N`), as `--set-at` names a line of the probe's own file; one in a header cannot be stopped at.
  - Otherwise the call is named alone.
  - It counts every mention of the function's name in the preprocessed sources, so a macro used twice is two calls, and any mention that is not a call by name, a prototype or the definition (its address taken) leaves the callers unknown; C++ keeps the general advice, as overloads and member calls make a call's target unclear from the text.
- **The root:** The root must be an automatic or a parameter of the stopped frame that Peeky does not withhold (declared below the stop, an initializer still running, a jump past it), nor shows with a note that its declaration still runs after the stop (a `do` body's first line), in an unoptimized build.
- **Same-named locals:** A local LLDB names with where it is declared, when another of its name is in scope, is selected as named (`'p @ s1.c:38.x=3'`, `'n @ s1.c:15=9'`; two of one name declared on the same line are ambiguous, and `get` and `set` refuse them).
- **A plain name** means the declaration the code names at the stop, as C and C++ scope it: where LLDB lists an inner block's `t` and an outer one as `t @ same.c:22` and `t @ same.c:20`, `t` reads and changes the inner, and the outer before the inner's declaration and after a `while`'s or a `switch`'s own `t`, or a `t` that is a loop's or a `switch`'s whole body without braces (`while (c) long t = 8;`) (whose scope ends with its statement, though the debug information keeps it to the end of the block; scopes are read from the source as the compiler preprocessed it, so a macro's `while (decl)` counts too).
- **When Peeky cannot tell:** Where Peeky cannot tell which one the code names (the source does not say where a scope ends, two are as inner), `t` is refused and the error lists the `t @ file:line` names, which select.
- **One name, different types:** Where the function declares one name with different types (a loop's `Task t` and a later range-`for`'s `const Task &t`, or `s` in sibling blocks), the path is checked against the type of the declaration the debugger shows at the stop, the one whose block holds it, and `describe`'s `mutable:` line names such locals; two declared on one line cannot be told apart, and a change to them is refused. A probe is refused for it before the program runs unless the line's breakpoint location (its lowest address that runs before the function returns, not an exception's landing pad) singles one out; LLDB's later stops on the line, one per other block (#157), are part-way through it, where no change is made, and the refusal says where one of them names another declaration.
- **Caller frames:** A caller frame (`--frame 1`) is judged where its call is, the address before the one it returns to, as LLDB reads it; a frame a signal interrupted (above the system's `_sigtramp` or `__restore_rt`, not a function of the program's named so), at the instruction it stopped at.
- **Arrays:** An element of an array of any length is fetched alone, at any step of the path (`big[700].a=1`, `grid[599][2]=3`).
- **Read back:** The field is read back from the debugger after the write, and only the value asked counts as a change.
- **Marks:** Probe rows and `session get` listings mark the fields and elements `set` would change `[settable]`, judged by the same rules at the stop (`bins[3].hi  [settable]`, never `ps.end`); a refusal names the rule it broke, and its hint the one way around it.

What a change makes true, for a local or a field: its storage holds the new value, and the program's later reads of it see it. What the program copied before keeps the old value: a copy on an earlier line (`int a = s.x;`), and a statement under way at the stop, which may hold what it read in a register. So C and C++ allow a change only at a statement's first instruction, and refuse it anywhere else: `set` says `cannot be changed here` and keeps the session, and `inspect` drops `[settable]` there and says why (`note: nothing here is [settable]: the stop is not at the first instruction of the statement on line 9 ...`; `unsettable` in JSON). Peeky proves the stop is there from the source and the line table:

- the stop is on one of its function's own lines, and that line starts a statement: every bracket around it, up to the function's body, is a block of statements (not a call's arguments, an initializer, a `new long[1]{`, a compound literal; nothing inside a GNU statement expression `({ … })` at all, even a macro's loop there), no directive or bracket-hiding macro lies between it and the previous token, and that token ends a statement (`;`, `{`, `}`, a label, `else`, the `)` of an `if`/`for`/`while`/`switch` header). A brace after something like parameters (`REPEAT(k, 1) {`) is a function's body only at file scope or in a class's or namespace's body; elsewhere it is a block of the function around it;
- the pc is the lowest address of all the function's code on the lines of the statements starting there: clang may put a later line's code first (`t =` / `p.x + g();` loads `p.x` and calls `g` before line 1's store);
- on a function's first line (a one-line function or lambda), the statements are those after its body's `{`, and the pc is the lowest of their rows past the function's entry (`long one(struct P p) { long t =` / `p.x + g();` runs the second line first, so neither line qualifies);
- no call in those statements is one the compiler inlined (`always_inline`, `flatten`; Peeky reads which from the debug information): an inlined body's code carries its own function's lines and runs before the statement's lowest row (`t = inl(&p) + p.y;`).

A breakpoint on a statement's first line (`--at`, `continue --to`) or a `step` that lands there qualifies.

- **Refused:** Back from a call (`step --out`, a return address), on a statement's later line (`+ f();` under `int t = s.a`), on a loop header's later passes, in a K&R-style definition (`long f(a) long a; {`, not supported), and wherever Peeky cannot tell, the change is refused. A `step` onto `} else if (p.y > 1) {` on the else path is refused too: that line's lowest address is the jump that ends the `if` block, so the step stops past it, at the condition (and a breakpoint on that line, placed at the jump, is never hit on the else path, with or without Peeky); change the value before the `if`, or at the first statement inside the `else if` block.
- **Also refused:** Two more stops are refused though nothing is under way there: in an inlined function's own code (a one-line `always_inline` function's line, where its frame stops), and on a body's `{` placed on the line under its signature with a statement after it (`long f(struct P p)` / `{ long t = p.x + 1;`); change the value in the caller, or at the next statement.
- **Function-try-blocks:** A function-try-block (`long f(P p) try { … } catch (…) { … }`) is a function body like any other, its handlers too.
- **Overwrites:** The stop's own line may still overwrite the change (`s = (struct tri){9, 9, 9};`).

### 7.2 Rust (a Cargo crate)

**Common cases**

- **Read** locals with their Rust types; an owned `String`, or a `Vec` of plain elements (integers, `bool`, floats, `char`, or tuples and crate structs of them), and a reference parameter of such elements (`&[T]`, `&str`), from memory (`[memory]`), in an unoptimized build with lldb-dap 20. A `Vec<String>` (`args`) stays raw.
- **`Vec` and `String` fields read too:** `self.values` behind `&mut self` (innermost frame only; behind `&self`, any frame), and `w.values` in `main`, which owns `w`; a moved struct's stay raw, with a note.
- **A crate with `unsafe` code or a nightly feature** reads no `Vec` or `String` contents, no reference parameter's contents and no such field: each stays raw, with the reason. Scalars (`self.k`, `w.k`) are still read.
- **Set** a 16- to 64-bit integer local kept in memory, or such a field of a struct local the frame owns (`--set w.start=10`); a local kept in a register is `[not settable: register]` (keep it in memory with `black_box`, 6.6).
- **Set a `String` to a text** (`--set 'name="pear"'`) within its capacity, where the MIR proves it initialized and no borrow of it (`&s[..2]`, `s.chars()`) is used after the stop, with lldb-dap 21 (macOS); never a `&str`, nor in a crate with `unsafe` code or a nightly feature.
- **Through `&mut Window`** (`w.start` in the callee), set it in the caller that owns `w`, at the call's line.
- **A plain `cargo new` crate** works; run `cargo generate-lockfile` once if `Cargo.lock` is missing.

**Quick start (Rust).** What can be read, what can be set, and where:

- **Read** integer, float, `bool` and `char` locals with their Rust types (`usize`, and `&mut limiter::Window` for a reference parameter; a field or element's too, from the build's debug information: `self.k  usize`, where LLDB says `unsigned long`, kept in `debugger_type`, as for a field a preview reads from memory), `Option<i32>`, a struct or tuple local (or a reference parameter to one) as derived `Debug` prints it (`Window { start: 0, count: 2 }`), and the contents of `Vec<T>`, `String`, `&[T]` and `&str` (`[memory]`), also as fields (`self.values`); fields stay selectable (`w.start`, `runs[0].__0`).
- **Never guessed:** a value Peeky cannot prove (moved, dropped or not yet assigned, in a caller frame, in an optimized build) keeps its raw fields with a note (`[stale]`, `[unproven]`, `<unverified ...>`). A local whose storage may hold leftovers is withheld (`no value yet:` and why): unassigned on a path (`let x;`) or not kept across an `.await`.
- **Set** integer locals (`i16` to `i64`, `u16` to `u64`, `isize`, `usize`) the program keeps in memory, and integer fields and elements of a struct, tuple or array local the frame holds itself, which the MIR proves initialized: `--set w.start=10 --set w.count=0`.
- **Text:** a `String` local takes a text in double quotes, `--set 'name="pear"'`: Peeky writes its bytes and `len` in the buffer it has, within its capacity (`name`'s buffer holds 16 bytes, and the text is 17 bytes is refused), only where the MIR proves the local initialized at the stop (a moved or dropped `String`'s buffer may be freed). The buffer is on the heap, so this needs a debugger that writes memory (lldb-dap 21; macOS ships it); with lldb-dap 18 or 20 it is refused. A `&str` is refused before the program runs: it borrows text that may be read-only.
- **Borrows:** a `String` takes no text while a borrow of it is used after the stop: a `&str` of it (`let head = &s[..2];`), a reference, an iterator (`s.chars()`), anything built from one or given one (a `Vec<&str>` it was pushed into, a closure that borrows it, a `Box<dyn Fn() + '_>` or `Pin<Box<dyn Future + '_>>` holding one). Such a borrow keeps the old length: it would read a mix of old and new text, or bytes that are not UTF-8. Change it before the borrow is taken or after its last use (a `len()` or a clone keeps no borrow).
- **What may hold a borrow:** a value holds none only where its type provably holds no pointer: numbers, `String`, arrays, tuples and standard containers of those, and a struct, enum or union of your crate without type or lifetime parameters whose fields, read from the debug information, are all such or a `Box` of such (`Vec<Span>` for `struct Span { start: u32, end: u32 }`, a C-like `enum`, `struct Rec { v: u32, next: Option<Box<Rec>> }`). A trait object, an `impl` type, a type parameter or another type of your crate may.
- **Drops:** dropping a value counts as using its borrow where the drop may run code that reads it (a guard whose `Drop` reads its `&str`, a trait object, a closure, a type of your crate), not for a standard container of references (`Vec<&str>`).
- **Unsafe code:** a crate with `unsafe` code or a nightly feature takes no text at all, with the reason (see **Crates with unsafe code** below).
- **Never** through a reference or a `Box` (`w.start` where `w: &mut Window`), nor inside an enum, a `#[repr(packed)]` struct or a standard-library type. Peeky does not write through a reference, since what it reaches may be freed or moved; change the value where it is owned, before the same call: a probe `--at` the call's line with `--hits N --set-hit N`, or `session start --at` it with `--hit N`, where N counts the call line's own stops, not `allow`'s, which differ when `allow` is called from several lines (find N with `peeky probe ... --at <call line> --capture now --hits all`); `session continue --to` the call's line reaches only the next call, and `session step --out` returns part-way through the line, where changes are refused.
- **Registers:** A local kept only in a register (a by-value parameter, or a `let` the code never borrows or reassigns) is never settable: rows mark it `[not settable: register]`, `describe`'s `mutable:` line lists them per function, and a probe that sets one is refused before the program runs, unless another function on the line keeps it in memory (a closure, an inlined copy).
- **Bound twice, inlined:** A name the function binds twice is settable only when both bindings are in memory (`[not settable: another binding in a register (line 4)]`, and the refusal names the `let` to black-box), and a local of an `#[inline(always)]` function never is (`[not settable: inlined]`); `describe`'s `mutable:` line gives each name the same reason (`bind::shadow: x (line 4)`).
- **Async:** A local an async body keeps across an `.await` is never settable either (`[not settable: async state]`): the poll reaches it in the future's state through a pointer; copy it into a local after the `.await` with a candidate edit and set that.
- **Keeping a local in memory:** A candidate edit adding `std::hint::black_box(&mut x);` after its `let` keeps it in memory (a parameter: declared `mut x` and borrowed first thing; see 6.6).
- **Where:** a line breakpoint, or a step that lands on a line's start, where the MIR shows no statement under way has read the value; never back from a call (`step --out`).
- **Marks:** rows show `[settable]` on what `set` would change, fields and elements included; when a rule leaves nothing settable, `session inspect` says why.
- **Builds:** an unoptimized build (the default) for every change and for owned `Vec`/`String` contents; a plain `cargo new` crate with no dependencies. The target is the crate directory with its `Cargo.lock`: a plain `cargo new` binary crate works (edition 2021 or 2024, no dependencies, no `src/lib.rs`, `src/bin` or `build.rs`, and at most one integration test in `tests/`); run `cargo generate-lockfile` once if `Cargo.lock` is missing.

The rest of this section shows them at work, then the detail behind them.

```sh
peeky describe examples/rust
peeky probe examples/rust --at 36 --capture start,some,none,mode,request,request.page --set start=0
```

```
breakpoint lines in src/main.rs: 14-17, 20, 22-25, 27-30, 32-38, 40-41
qualified enum views: core::option::Option<i32>

hit  phase      line  function                selector      type                       value
1    observed   36    peeky_pagination::main  start         i32                        3  [settable]
1    observed   36    peeky_pagination::main  some          core::option::Option<i32>  Some(3)  [active-variant]
1    observed   36    peeky_pagination::main  none          core::option::Option<i32>  None  [active-variant]
1    observed   36    peeky_pagination::main  mode          peeky_pagination::Mode     First
1    observed   36    peeky_pagination::main  request       peeky_pagination::Request  Request { page: 1, page_size: 3, total: 8 }  [memory]
1    observed   36    peeky_pagination::main  request.page  i32                        1  [memory]  [settable]
...
set start: 3 -> 0
output:
  PAGE:[10, 20, 30]
```

- **Decoding:** Values Peeky cannot decode with certainty show as `<unverified ...>` or as opaque enums; they are never guessed (`describe` counts them as "other enum types").
- **Changing values:** Only integer locals (`i16` to `i64`, `u16` to `u64`, `isize`, `usize`) that the program keeps in memory can be changed, and `String` locals to a text (see the quick start); `session inspect` marks them `[settable]`. So can such integer fields and elements of a struct, tuple or array local kept in memory that the build's MIR proves initialized at the stop (`--set s.end=10`, `t.__0=3`, `arr[2]=7`): never through a reference or a `Box`, nor inside an enum, a `#[repr(packed)]` struct or a standard-library type, and only at a line breakpoint, or a step that ends at its line's start, where the MIR shows no statement under way has read the local into a temporary (so never back from `f` in `let t = s.a + f();`, nor after a step back onto `let arr = [s.a,` from its later lines), which also holds for integer locals (optional background: [RUST_AGENT.md](RUST_AGENT.md#changing-fields-and-elements-105)).
- **Reference parameters:** Reference parameters (`&[T]`, `&str`, `&Vec<T>`, `&String`, shared or `&mut`, for `T` any integer type, `bool`, `f32`, `f64` or `char`) are read from memory at offsets checked against the build's debug information (`describe` lists them). Elements print as Rust's `{:?}` does, floats included (`[0.30000000000000004, 1e16, -0.0, NaN]`), so they can be compared with the program's own output. In `fn median(values: &[i32], label: &str)`, `--capture 'values,values[0],values[-1],values.length,label'` shows `[5, 1, 3]`, `5`, `3`, `3` and `"größe"`. Their type column shows the Rust type (`&mut limiter::Window`), not LLDB's (`limiter::Window *`), which `debugger_type` keeps.
- **Tuples and structs:** Tuples, and structs your crate defines, of those scalars are read too: `runs` (`Vec<(char, usize)>`) shows `[('a', 3), ('b', 1)]`, and `runs[0].__0` and `spans[1].start` select fields (LLDB's names: `__0` for a tuple's first). A tuple or struct local of them, or a reference parameter to one, previews the same way under the same proofs (`request` above: `Request { page: 1, page_size: 3, total: 8 }`), and its fields stay selectable (#106). Optional background: [RUST_AGENT.md](RUST_AGENT.md#reading-rust-values).
- **Owned `Vec` and `String`:** Owned `Vec<T>` and `String` locals are read as well, in the innermost frame of a crate with no unsafe code, wherever the compiler's own MIR proves them initialized at the stop line: Peeky builds the binary crate with `-Zmir-opt-level=0` and dumps its MIR, so every move, drop and assignment is explicit. A local that may be moved, dropped or not yet assigned around the line (`drop(ordered)`, `consume(v)`, the old `s` in `let s = s + "x"`, a deferred `let label: String;`) keeps its raw fields (`ordered.len`) with a note, since its pointer can reach reused memory and its contents would be a guess (#53). A `&str` or slice local reads where #110 shows its target still standing (`v` of `for v in args[2].split(',')`, whose `args` nothing changes); other reference locals keep their raw fields.
- **Fields holding a `Vec`, `String`, `&str` or `Box<str>`** read from memory too (#156), reached through plain struct and tuple fields and fixed-size array elements:
  - through a reference parameter: `self.values` in `fn push(&mut self, v: i64)` shows `[3, 6]`, and `self.values[0]`, `self.values[-1]` and `self.values.len` select from it. A `&mut self` reads only in the innermost frame (in a caller, the call under way may be changing it); a `&self` in any frame;
  - in an owned local (or what its `Box` holds), under the local's proof, in the innermost frame only: `w.values` in `main`, where `w.sum` is `[settable]`;
  - never through a `RefCell` (its contents change behind `&self`), a `Mutex` or another standard-library type, a pointer, a reference other than the root parameter, or an enum, nor in a moved, dropped or partly moved struct (`[stale]`, #108);
  - a `&str` field of an owned local reads where #110 shows its target standing (a struct with no lifetime holds only `&'static str`);
  - where a field stays raw, its note says why (`` `self` is a `&mut` parameter of a caller frame ``, `` `self.cell` is a standard-library type ``, `the debugger gives no memory address for it`), and `session get self` lists them; an index into it (`self.values[0]`) is refused with the same reason and the length selector to capture instead (`self.values.len`).
- **Crates with unsafe code:** unsafe code can free a buffer that safe code still shows owned, unseen (`ptr::read(&w.values)`, `ptr::drop_in_place(&mut self.values)`, in a closure or another function), so where the crate has any, Peeky reads nothing from memory itself (#156):
  - raw, with the reason ("the crate has unsafe code (in src/main.rs), which can free a buffer without Peeky seeing it"): a reference parameter's contents and a struct preview through a reference, an owned `Vec` or `String` local, a `&str` or slice local, a container field; a Box shows only its pointer (`[unproven]`);
  - no `String` takes a text (`--set 'name="x"'`), with the same reason: a raw pointer, or an address kept as an integer (`s.as_ptr() as usize`), can read its bytes at their old length where no borrow shows it;
  - still read, as LLDB reads them: scalar locals, a struct's scalar fields in the frame or behind a reference parameter (`w.k`), a by-value struct's preview, an enum's variant;
  - what counts: an `unsafe` block, function, impl, trait or extern block, `#[unsafe(...)]`, `#[no_mangle]`, `#[export_name]`, `#[link_section]` or `global_asm!` in any of the crate's files (`tests/` too; `macro_rules!` bodies included, comments and strings not); a nightly feature (`#![feature(...)]`: "the crate enables nightly features"); and a source Peeky cannot read as rustc does (frontmatter, an unclosed literal: "Peeky cannot rule out unsafe code in …"). Optional background: [RUST_AGENT.md](RUST_AGENT.md#reading-rust-values).
- **Long values:** Long values show their first elements with `(first k of N)`, and up to 256 elements stay selectable.
- **Debugger:** This needs a debugger with `readMemory` (lldb-dap from LLVM 20); older debuggers keep the raw fields, with an error that names the length field.

### 7.3 Java (launched)

**Common cases**

- A Java target needs `--config <file.json>` (the adapter bundle, sources and main class); `describe`'s commands repeat it.
- Objects and arrays show their first fields or elements, an `ArrayList` its size and first elements (an empty one, its raw fields); read members through `this` (`this.total`).
- Scalar fields and array elements can be set too (`--set current.end=10`), not final or static fields, boxed values or an `ArrayList`'s elements.
- A String local, field or element (declared String) takes a text in double quotes (`--set 'name="apple"'`): a new String, with a current adapter bundle; a local the program does not read after the stop is refused.
- A line several methods share (a lambda on its method's line) stops in only one of them; the probe says which.

`examples/java/pagination.json` (the bundle path is relative to the config file):

```json
{"bundle": "../../work/java-adapter", "sources": ["demo/PaginationBug.java"], "main_class": "demo.PaginationBug", "arguments": []}
```

```sh
peeky probe examples/java --config examples/java/pagination.json --at demo/PaginationBug.java:13 \
  --capture start,pageSize,items --set start=0
```

```
hit  phase      line  function                      selector  type   value
1    observed   13    PaginationBug.main(String[])  start     int    3  [settable]
1    observed   13    PaginationBug.main(String[])  pageSize  int    3  [settable]
1    observed   13    PaginationBug.main(String[])  items     int[]  int[8]@51 [10, 20, 30, 40, 50, 60, 70, 80]
...
set start: 3 -> 0
status: completed  hits: 1  seconds: 0.54
output:
  PAGE:[10, 20, 30]
```

Fields and array elements can be changed too. `examples/java/demo/MergeBug.java` merges intervals but overwrites `current.end` with a smaller end. To test "if `current.end` kept 10, the merge would be right", change the field at the second hit:

```sh
peeky probe examples/java --config examples/java/merge.json --at demo/MergeBug.java:26 \
  --capture current,next --hits 2 --set current.end=10 --set-hit 2
```

```
hit  phase      line  function                 selector  type               value
1    observed   26    MergeBug.main(String[])  current   MergeBug$Interval  MergeBug$Interval@57 {end=10, start=1}
1    observed   26    MergeBug.main(String[])  next      MergeBug$Interval  MergeBug$Interval@58 {end=5, start=2}
2    observed   26    MergeBug.main(String[])  current   MergeBug$Interval  MergeBug$Interval@57 {end=5, start=1}
2    observed   26    MergeBug.main(String[])  next      MergeBug$Interval  MergeBug$Interval@63 {end=14, start=12}
2    after-set  26    MergeBug.main(String[])  current   MergeBug$Interval  MergeBug$Interval@57 {end=10, start=1}
after-set: 1 row equal to the hit's observed value left out (`--json` has every row)
set current.end: 5 -> 10
output:
  MERGED:[[1,10], [12,14]]
```

The output changed from `[[1,5], [12,14]]` to `[[1,10], [12,14]]`: the merge was right except for `end`. `this.count=0`, `arr[0]=1` and `arr[-1]=9` work the same way, and `session set current.end=10` changes a field in a session. Captured fields and elements say `[settable]` when they can be changed. Final and static fields, boxed values (`Integer`) and an `ArrayList`'s elements cannot (change a field of one: `merged[0].end=10`); a String field or element takes a text (below). The value is read back from the object after the change. A field whose name a subclass's static field hides, or another field shares, is refused (java-debug would write the other field). Field and element changes need an adapter bundle built by this version of `scripts/setup_java_adapter.py`; an older bundle changes only locals and says so.

A value must fit the variable's type exactly. For example, `int` 3000000000 and `010` (octal for `Long.decode`) are refused rather than silently changed.

- **Strings:** A String local, field or element takes a text in double quotes, `--set 'name="apple"'`: java-debug makes a new String of it, and the variable refers to that; other references (`String alias = name;`) keep the old one. java-debug shows a String between quotes without escaping it (`"new "quoted""`). This needs an adapter bundle built with this version of `scripts/setup_java_adapter.py`; an older one refuses texts and says to rebuild it.
- **Declared types:** What a variable is declared decides, as java-debug's own check does: `Object o = "x"`, `CharSequence cs`, an `Object` field or an `Object[]`'s element holds a String but takes no text, and is not marked `[settable]` (Peeky reads declared types from the class files).
- **Locals never read again:** A local the program never reads after the stop is never changed, of any type, and is not marked `[settable]`. Peeky follows the method's bytecode from where the stop's line begins (a line breakpoint or a step stops there), and refuses when no path reads the local before storing into it again: a local a lambda or inner class captured before the stop (`Supplier<String> lam = () -> cap;`: `lam` keeps its own copy), and a constant javac compiles into the code that uses it (`final int k = 5;`, `final String s = "x";`). Where Peeky cannot follow the code (another kind of stop), the change is made with a note that a lambda or inner class that captured it before keeps the old value.
- **Shared lines:** A line that several methods share (a lambda body on its method's line, like `.filter(item -> below(item))`, a static initializer's lambda, nested lambdas) stops in only one of them: java-debug picks the first in the class file. Peeky warns which one stops and which never do (`stops only in needsRestock there, never in lambda$needsRestock$0`). To stop in another, put the breakpoint on a line of its own, or `session step --into` from that line.

**Reading Java values.**

- In a launched Java program, a value you capture or `session get` shows more: an object its first fields inline (`Merge$Interval@143 {end=10, start=1}`), an array its first elements (`int[3]@144 [1, 2, 3]`), an `ArrayList` its size and first elements (`ArrayList@142 size=2 [...]`; an empty one shows its raw fields, `size=0`).
- These are the debugger's reads of the value's own fields (no code runs in the program); Java `inspect` listings stay plain. An attached JVM shows plain values to keep its pauses short.
- `ArrayList` selectors index like arrays (`merged[0]`, `merged[-1].end`) within its `size`, unless its backing array holds more than 512 slots.
- Fields that are Strings and chars show as Java literals (`name="say \"hi\""`, `quote='"'`), a String cut after 38 characters with `…`.
- Java object numbers (`@143`) follow the order the debugger first touched objects, so `queries diff` treats them like addresses in Java runs.
- A Java `step` from a block's last statement stops on its closing `}`, where the block's locals are gone; read them before stepping.

### 7.4 Java Tests Already Running (Gradle `--debug-jvm`, e.g. Venice)

```sh
./gradlew :internal:venice-test-common:integrationTests_32 \
  --tests 'com.linkedin.venice.endToEnd.TestEmptyPush.testEmptyPushWithZstdWithDictCompression' --debug-jvm
```

```json
{"bundle": "/abs/path/to/peeky/work/java-adapter", "port": 5005,
 "sources": ["internal/venice-test-common/src/integrationTest/java/com/linkedin/venice/endToEnd/TestEmptyPush.java"],
 "class_roots": ["internal/venice-test-common/build/classes/java/integrationTest"]}
```

```sh
T=internal/venice-test-common/src/integrationTest/java/com/linkedin/venice/endToEnd/TestEmptyPush.java
peeky probe ~/workspace/venice --config attach.json --at $T:85 \
  --capture storeName,sendControlMessageDirectly --hits 2 --timeout 240
```

Attaching never kills the JVM; closing detaches. Provenance is labelled `attached`, edits cannot be verified this way, and `--arg` is refused because the program is already running. Details are in [JAVA_ATTACH.md](JAVA_ATTACH.md), including breakpoints in production classes.

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
| `print m[1]` (a C++ `std::map`) | `--capture "m[1],m[#0].first"` | `m[1]` is the value for key 1, as in C++; `m[#0]` is the first entry in iteration order (7.1) |
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
