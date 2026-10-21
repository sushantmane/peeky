# Peeky Cookbook

Recipes for debugging running programs from your terminal. Each one is copy-paste ready from the repository root and shows real output (macOS, Python 3.13, recorded 2026-10-16). Agents use the same commands; see [AGENT_GUIDE.md](AGENT_GUIDE.md).

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

0. [Setup](#0-setup)
1. [First Look](#1-first-look)
2. [Asking Questions with Probes](#2-asking-questions-with-probes)
3. [Comparing Runs](#3-comparing-runs)
4. [SQL over Saved Runs](#4-sql-over-saved-runs)
5. [Step-by-Step Sessions](#5-step-by-step-sessions)
6. [Verifying a Fix](#6-verifying-a-fix)
7. [C, C++, Rust and Java](#7-c-c-rust-and-java)
8. [Slow Programs and Tool Timeouts](#8-slow-programs-and-tool-timeouts)
9. [A Program on Another Host](#9-a-program-on-another-host)
10. [When Something Goes Wrong](#10-when-something-goes-wrong)
11. [From pdb / gdb / lldb to Peeky](#11-from-pdb--gdb--lldb-to-peeky)

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

C, C++ and Rust list the same from the build's debug information: each function in your sources, with its file, lines and parameters and locals. Names are readable (`crate::Type::method`, `<crate::Type as core::fmt::Display>::fmt`, `geo::Box::area`). A probe table's `function` column shows each frame's short name: `trim_left`, `geo::Box::area`, `same<int>`, a Rust function without its symbol hash; two overloads in one table keep their parameters (`twice(int)`, `twice(std::string const&)`). JSON and the evidence store keep LLDB's whole frame name (a C++ signature, `trim_left(std::__1::basic_string<char, …> const&)`; Rust's symbol escapes, `_$LT$…`), and a session's text names the frame with its parameters, the standard types written as code writes them (`in trim_left(std::string const&)`). SQL compares the whole name, so `WHERE function = 'trim_left'` finds nothing: a table that shows short names says so in a note. Match a frame with `short_function(function) = 'trim_left'` or with its whole name. `short_function` drops the parameters of every overload, so overloads share one name (`operator+` for `operator+(V, V)` and `operator+(W, W)`; `G::get` for `get() &` and `get() &&`): a `GROUP BY` or join on it combines them, and it is not always what a table shows (a table keeps the parameters of overloads that would read alike). Closures and lambdas are listed under their function. A function inlined into another (`#[inline(always)]`, `always_inline`) keeps its own lines. Each name shows its type. A local declared only inside nested blocks also shows those blocks' lines, where it is visible: `i (int, lines 23, 28)` is two one-line `for` loops' `i`, not a name of the whole function. A block that also runs another file's code (an `#include` inside it) gives none (a local macro's expansion, on its definition's lines, does not count), and past eight blocks one span covers them all. In Rust every `let` opens a block, so a local shows the lines after its `let`, where it is visible (#107). Bindings a macro from another crate declares (`println!`'s `args`, `core::fmt::rt::Argument[1]`) are left out: the build's MIR places them in the macro's own file. A macro of your crate's keeps its bindings, `#[collapse_debuginfo(yes)]` too. Lines between a block's own where only code outside the block runs are left out, so one block can give several spans. In C++ a block's cleanup code, which runs when an exception leaves the block, is on the function's closing line: `name (std::string, lines 22-25, 35)` is an `if` block's local, visible on its lines and, while an exception unwinds, at the closing `}`, not on the lines between. A span is never narrower than where the debugger can show the local, so lines without code (comments, a lone `}`) inside it are kept. If the debug information cannot be read, the text says `function names: unavailable (reason)`. A very large optimized C++ unit can exceed the 16 MB bound on reading it:

```
names in parse_count (pagination_bug.c:6-14): end (char *), minimum (int), result (int *), text (const char *), value (long)
names in main (pagination_bug.c:16-31): argc (int), argv (char **), end (int), i (int, lines 23, 28), items (int[128]), page (int), page_size (int), start (int), total (int)

names in peeky_pagination::bounded (src/main.rs:14-20): default (i32), minimum (i32), text (core::option::Option<&alloc::string::String>), value (i32, lines 16-19)
names in peeky_pagination::main::{{closure}} (src/main.rs:37-37): index (i32)
```

With `--json`, a scope's `spans` maps such a name to its blocks' `[first, last]` lines.

**Line ranges and stepping.** A function's lines (`main (pagination_bug.c:16-31)`) and a local's block lines are where its code is, not where a stop can land:

- A `session step` can stop on a line with no statement of its own: a closing `}` (C, C++, Java, Rust: where a block's locals go out of scope or a loop jumps back) or a loop header on its later passes (`for (...; i++)`).
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

Selectors are dotted paths, indexes and string keys (`request.page`, `args[0]`, `items[-1]`, `order['id']`), never expressions: Peeky never evaluates code in your program. Python dict entries are read by key; `order.id` fails with a message that names `order['id']` and lists the keys. A selector that is not there lists what is. Quote selectors that contain `[...]`: zsh, the macOS default shell, would otherwise treat them as filename patterns. In C and C++, follow a pointer with `.` (`stats.total_us`, not `->`); a pointer to a scalar carries no length, so `values[0]` reads what it points at; for other elements, read the array itself where it is declared (in the caller). The same holds for a pointer to an array, a container or a struct: `pa[0]` is what it points at, so `pa[0][1]` is `(*pa)[1]`, while `pa[1]`, which C reads past the pointee, is refused with that selector (#102). A typedef'd pointer (`RowPtr`, `const RowPtr`) works the same; the build's debug information tells it from a typedef'd reference, which indexes its referent. A pointer under a type name it does not resolve (a template's member typedef such as `std::vector<T>::pointer` to a container) is refused for indexing rather than read either way. A NULL or unreadable pointer is refused (`p is NULL`). Rust references and Boxes are never indexed this way. On a C++ `std::map` or `std::unordered_map`, `m[1]` is the value for key 1, as in C++, never the debugger's second entry; `m[#0]` is the first entry, and a set has only `s[#0]`, ... (7.1).

### 2.2 Every Iteration of a Loop

**What it answers:** How does a value evolve across iterations?

```sh
peeky probe examples/corpus/running_max/program.py --at 6 --capture value,best --hits 3
peeky queries hits
```

```
selector  hit  line  value  error  changed
best      1    6     0      NULL   NULL
best      2    6     0      NULL   0
best      3    6     0      NULL   0
value     1    6     -3     NULL   NULL
value     2    6     -1     NULL   1
value     3    6     -2     NULL   1
```

A hit where the selector has no value shows NULL with the reason in `error`, and `changed` is NULL next to it: a missing value is never compared as one.

`best` starts at 0 and never drops to a negative value, so the running maximum of all-negative input is wrong.

`--hits all` captures every stop, up to 100, without counting iterations first: the summary says `hits: 3 (all)`, and a warning says when the cap was reached. With more than 20 selectors the cap is lower, so that every hit fits the 2000-row probe budget. `all` needs the program to exit, so it cannot be combined with `--until hits` (attached JVMs take a number). Progress prints the first hit, then at most one line every 2 seconds, then the total when the program finishes.

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
1    after-set  8     get_page  items[1]  int   20
set start: 3 -> 0
status: completed  hits: 1  seconds: 1.004  max pause: 10.0 ms
output:
  PAGE: [10, 20, 30]
```

- **What:** Root locals can be changed: the stopped function's own variables (`start`). In Python a change rebinds the name in that frame: `--set 'seen=[]'` gives this call a new list, while the shared object it named (a default argument, a module global) keeps its contents, and the next call sees it again.
- **Fields and elements:** In Java, so can scalar fields and array elements (see 7.3); in C, C++ and Rust, integer fields and elements of a struct, tuple or array local the stopped frame holds itself (`s.end`, `xs[2]`, `t.__0`; see 7.1 and 7.2), never through a pointer, reference or `Box`.
- **Where:** A change writes the variable's storage, which the program's later reads see; what the program already copied keeps the old value, so C, C++ and Rust allow a change only where Peeky proves no statement is under way, and refuse it anywhere else (after `step --out`, on a statement's later line; see 7.1 and 7.2).
- **Values:** Python takes literals, including lists and dicts. C and C++ take integers of 16 to 64 bits (`int`, `long`, `size_t`, `int64_t`, …) within the local's range; Java takes scalars of the target's type. Rust takes integer locals (`i16` to `i64`, `u16` to `u64`, `isize`, `usize`) that the program keeps in memory; any other change is refused, because it would not reach the program.
- **Marks:** For C, C++ and Rust, rows mark what a change would reach with `[settable]`: locals in `session inspect`, and fields and elements in probe rows and `session get` listings. When a rule leaves nothing settable at a stop, `session inspect` says why in a note. In JSON every root, field and element row a change could name carries `settable: true` or `false` (Java's too); `false` includes the internals of a standard-library type (`h.v.len` of a `Vec` field, raw where no view reads it) and what Peeky decodes from memory (a `Vec`'s elements and `len`), both of which `set` refuses.
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
- 1: a completed command with a negative result (a failed probe or a failed verify);
- 2: an error;
- 3: a background job still running.

---

## 3. Comparing Runs

Every probe is saved with a run id (`--no-save` skips that). `--label` tags a run so you can find it later.

### 3.1 Two Inputs Side by Side

**What it answers:** Where do two runs diverge?

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

1. **What the runs did and how they differed**, one row each where it applies: `(status)` and `(output)` when the runs' outcomes or outputs differ; `(edit)` when one run built a candidate edit; `(source)` when two runs that are not candidates ran different source text (another copy of the file); `(line)` when the runs stopped at different lines (their values then differ partly because of where they were read); `(args)` when they ran with different program arguments; `(sets)` when they made different interventions. Check these rows before crediting an edit with a difference.
2. **Real differences**, in hit order and then in `--capture` order: the first of them is the first divergence.
3. Values that only one run's `--set` changed (`intervention only in run b`).
4. Values that differ only in heap or stack addresses (`note: addresses only`, for example a C string's pointer; NULL is never treated as an address).
5. Last, one row per selector that only one run asked for (`only in run a`, with its first hit's value).

The `(output)` row shows each output from just before the first character that differs (`output differs at line 4, column 13`, counted from 1), so after a `--set` it shows the intervention's effect. Hits that one run never reached say `missing in run b`. A value whose type changed (an edit to `long long`) says `type unsigned int -> long long`. `diff` compares the last two runs unless you name them: `--param a=1 --param b=5`. Comparing needs two saved runs; with fewer, the query says so. `queries compare` shows every value side by side with the same notes. Labels are free text, stored as given.

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
hit  selector  observed  after_set  interventions
1    start     3         0          start: 3 -> 0
```

Pass `--param a=<run> --param b=<run>` to `diff`, `compare` and `outputs` to pick specific runs.

---

## 4. SQL over Saved Runs

The store is SQLite. Queries are read-only and bounded, and they need no rerun.

### 4.1 What Is in the Store?

```sh
peeky sql --schema          # tables, columns, views, canned queries, examples
peeky queries               # canned questions: runs, run, hits, compare, diff, outputs, set_effect, values, unreadable, failures
```

| Table or view | One row per |
| --- | --- |
| `runs` | probe run: inputs, interventions, status, output, build identity |
| `captures` | selector × hit × phase; `num` holds the value as a number |
| `interventions`, `pauses` | applied `--set`, pause per hit |
| `evidence` (view) | captures joined with their run (with its `language`): no JOIN needed; output is `runs.output` |
| `last_run` (view) | the evidence rows of the newest run |

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
peeky queries values --param selector=start
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

`substr(value, instr(value, ' ') + 1)` is the text after the address: an address has no space, while the text may have several (`" pear"`), so take only the first. Use it only on a reference's or pointer's value (one that starts with `0x`); on a plain string it would cut the text at its first space. `no_addresses(value)` gives `0x… "apple"`: enough to compare two references, or one across runs, but still not equal to `"apple"`. A text Peeky could not check whole carries a note after it (`"v" (shown up to a NUL at byte 1 of 3)`, libstdc++ with lldb-dap 18; 7.1), so it never equals a plain string either.

---

## 5. Step-by-Step Sessions

Use a session when you want several looks at one stop. Every command is a separate shell call; a private worker keeps the program paused in between.

### 5.1 Stop, Look, Change, Finish

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

Scope headers are the debugger's own names: `[Locals]` for Python, `[Local]` for Java. `session inspect --globals` also lists a Python module's `[Globals]`, read-only (at module level they are the locals, listed once). Listing the globals runs every global's `__repr__` in the program (listing locals runs theirs too), so they are listed only on request: with `--globals`, or when `session get` or a probe reads a name missing from the locals that the function reads from its globals. Then all of them are listed and every repr runs, not only the one asked for; a misspelled name counts too, since a name the function does not bind is a global (`--capture total,i`, where `i` is a module-level loop variable, runs every global's repr at each hit). A scope lists at most 50 entries (debugpy's groups count) and says how many more it has; `session get NAME` reads one past them by name. `session get` on a list, dict or struct lists one level of children, each with the selector that reads it, and `(children: ...)` on a child that has children of its own. A name that is not there prints only why (`x: <not found; visible: ...>`). If any selector fails, every row is still printed and the exit status is 1. `session continue` without `--to-exit` runs to the next stop at the session's breakpoint.

`session get` reads a Python name missing from the locals from the module's globals (`[module global]`, `scope: globals`) only when Peeky proves, from the function's own scoping in its source (`symtable`), that the function reads that name from its globals; only then are the globals read. A source it cannot parse, or one changed since the program started (by mtime or ctime, so `cp -p` counts), proves nothing, and the error says the name may be such a global. In `def shadow(): print(LIMIT); LIMIT = 1`, stopped at the `print`, `get LIMIT` says `LIMIT` is a local with no value there yet (Python raises `UnboundLocalError`), not the module's `LIMIT`; a local of an enclosing function, a closure variable, a comprehension's variable or a type parameter is named as such. debugpy files functions, classes and `__dunder__` names under groups; their members are selected by name (`add_tag.__defaults__`), and so is a local holding a function (`session set callback=None` changes it). `[id 6]` is the object's identity (see 4.6).

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

At module level the locals are the module's globals, so there is no separate `[Globals]`. A frame past the stack is `INVALID_REQUEST`, naming how many frames there are. Rust's owned-local and Box proofs hold only in the innermost frame, so a caller's `Vec` keeps its raw fields with a note. In C and C++, a caller's local is withheld while its declaration's code is not finished: in `long result = twice(seed);`, or `long multi =` over `twice(seed + 1);`, the value is stored only after `twice` returns (so are a `for` loop's counter while its body calls, on one line or several, since the increment's code follows the body, and a local whose declaration line has no code of its own: an array of structs, a nested struct or a 2-D array initialized over several lines is stored element by element on its elements' lines). So is a local whose declaration statement, read from the source to its `;`, is still running on the caller's line: in `struct trio t = { seed,` / `{ twice(3),` / `twice(4) } };`, the first line's code runs before the calls and `t.p` is stored after them. A variable declared in a `for`, `if`, `while` or `switch` header counts until the `;` after the whole statement, so it stays withheld while the body calls, and where the source does not give the end, the local is withheld: brackets that do not balance, or text the compiler may read differently from the scan (between the declaration and its end, a directive such as `#include`, a line splice, `<::`, or a macro of the program's sources and headers that leaves a bracket open or holds a `;` outside its brackets; anywhere in the file, `#line`). C's trigraphs are read as `-std=c11` reads them (`??/` is a backslash); C++17 has none. Digraphs (`<% %> <: :>`) are brackets, and lines are counted as the compiler's debug information counts them (`\r\n`, a lone `\r` or `\n`; `\n\r` is two). So is every local whose declaration is in another file than the caller's line: an initializer can `#include` its rows, or be included itself. A caller's values cannot be changed (`MUTATION_REJECTED`), so caller listings never say `[settable]`.

### 5.2 Stepping and Running to a Line

**What it answers:** What happens line by line from here, inside a call, or a few lines further on?

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

State lines number the stops at the session's breakpoint (`…:8 in get_page, hit 3`). `session start --hit 5` runs on to the 5th stop, and `session continue --count 3` to the 3rd breakpoint stop from here, each within one `--timeout`. They never run past anything else: a crash, a finished step or the program's exit ends them there, with a note saying why and the last hit reached, and exit status 3 (as when `--timeout` ends them first). `peeky session continue --to 9` stops once at line 9 of the session's file (the extra breakpoint is removed at the next stop), then the session's own breakpoint applies again. A step can leave your sources, for example into a library, into Rust's standard library, or past the end of `main`. The state then says `outside the program's sources`, and nothing there is read or changed. While your code is still on the stack, `step --out` returns to it; past `main`, the state says so and `continue` finishes. `continue --to` refuses a line the debugger would move (no code there) and keeps the stop. When other threads are stopped at the breakpoint too (LLDB stops them together; Java stops each one separately), `step` waits until `continue` has served them. For Java, `--into` and `--out` skip JDK classes (`java.*`, `javax.*`, `jdk.*`, `sun.*`, `com.sun.*`), so `step --into` on a line that calls `Math.min` stays in your code and reaches lambda bodies. A plain `step` at the end of a method that JDK code called (a `Runnable`, a comparator) stops outside your sources; `continue` from there. Java adapter bundles built before stepping are refused with a hint to rebuild them.

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

A checker that passes here cannot tell a fix from the bug. An `--old`/`--new` pair that changes nothing is refused (`--new equals --old`); `--baseline` is the way to check the original.

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

`--old` must match the source exactly once; the error says how many times it matched, and where. Files are read byte for byte, whereas shell `$(cat file)` drops trailing newlines.

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

A checker is a trusted script, run as `python checker.py <candidate-path>`. It exits 0 only when the behavior is right. Test the general rule, not the one input from the bug report (see `examples/verify_pagination.py`). For C, C++, Rust and Java its argument is the path of a JSON descriptor of the fresh build: run each case as `[*spec["command"], *case_arguments]` with `env=spec["environment"]` (`spec["language"]` names the language; see `examples/c/verify_pagination.py` and [NATIVE_AGENT.md](NATIVE_AGENT.md)). For Rust, the crate's integration test, when it has one, must pass too.

### 6.6 See What a Fix Does Before Making It

**What it answers:** With this edit, what would the program compute at the same line, and what would it print?

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
- **Not a verification:** this is evidence about one run. Only `peeky verify` checks a fix.
- **Intervening where no local can be set:** in Java, Rust, C and C++, `--set` changes a scalar field or element directly (`--set s.end=10` where `s` is a struct local; see 7.1 and 7.2 for which ones). `--set` also works in the same probe as `--old`/`--new`. Where the value you want to change cannot be set (a Python field, or one reached through a pointer, a reference or a `Box`, as `last.end` is when `last` is a `&mut Span`), add a local holding it with the edit, on an earlier line (a local is visible only after its declaration), and set that local. In Rust, at the line `last.end = s.end;` that follows `if s.start <= last.end {`:

  ```sh
  peeky probe spans --at src/main.rs:23 --capture end,last.end --arg 1-10,2-3 \
    --old 'if s.start <= last.end {' \
    --new 'let mut end = s.end; std::hint::black_box(&mut end); if s.start <= last.end {' \
    --old 'last.end = s.end;' --new 'last.end = end;' --set end=10
  ```

  This reports `set end: 3 -> 10`. `black_box` keeps the new Rust local in memory, where a change reaches the program.
- **Python:** like a compiled candidate, it runs from a private copy of the target root with the edit applied, and that copy is the working directory. Files keep their bytes, modes and times, and directories their modes. Whatever the script reaches through its own path, an import (also through `PYTHONPATH`, whose relative entries keep meaning your root, or an editable install, whose paths into the root are moved to the copy; one mapping a package to a directory of another name gets a note, and each name the root offers is then looked up as the program will import it: one still served from your tree, by an import hook ahead of every path as meson-python's is, gets a note, and such a hook may rebuild in its build directory), or another process (multiprocessing, a subprocess of `__file__`) is the copy, so all of it runs the edit. The program's writes stay in the copy, except writes inside an environment directory or through a link that leaves the root, which reach the originals:
  - A repository (`.git`, `.hg`, `.svn`) is copied whole, following its links, so `git add`, a commit, a rewritten index or a touched object stays in the copy. A `core.worktree` inside the root moves to the copy, and so does each linked worktree's entry: one outside the root is listed at a path that does not exist, with a note. So is any git directory under another name (a bare repository, `.bare` beside its worktrees); a bare repository as the root itself gets its worktree entries moved, or the candidate is refused. The copy's index is refreshed for the copied files. If the copy's git configuration differs from yours (a conditional include on the repository's path, `includeIf "gitdir:..."`), a note says so.
  - Some repositories are left out, with a note:
    - one over 20,000 files or 256 MB;
    - one that borrows objects from another (`alternates`, `--shared` clones);
    - one whose `core.worktree` is outside the root;
    - a `.git` file naming metadata outside the root (a linked worktree's).
  - Pure caches (`__pycache__`, `.mypy_cache`, `.ruff_cache`) are left out. Environments (`.venv`, `venv`, `.tox`, `.nox`, `node_modules`, `site-packages`, `.eggs`) are directories of links to the originals' entries: the interpreter uses them from either tree.
  - A directory nobody can open (another user's `pgdata`) is an empty one with its mode. One that can be entered but not listed is refused.

  Nothing above the target root is copied, and no reading of the source can tell whether a program reaches there. So the result notes it unless the root is a project's (it holds `.git`, `pyproject.toml`, `setup.py` or `setup.cfg`) and no repository above it holds it (a monorepo's package). If the program reads or imports anything from above, give the project directory as the target (`peeky probe proj --at scripts/run.py:14`). `--run` entry scripts are refused.

---

## 7. C, C++, Rust and Java

The commands are the same for every language; only the target changes.

### 7.1 C and C++

**Quick start (C and C++).** What can be read, what can be set, and where:

- **Read** any local, parameter or global by selector: fields and elements (`s.end`, `bins[3].hi`, `grid[1][0]`), through pointers too (`ps.start`), C++ members through `this` (`this.head_`), a `std::string` whole, a `std::vector` by index and a `std::map` by key (`m[1]`, `ages["al"]`).
- **Structs and arrays** show their members: `bins` reads `[{lo = 0, hi = 2, count = 0}, …]`, `bins[3]` `{lo = 6, hi = 7, count = 0}`, in probes, `inspect` and `session get`. Unions and what pointers point at are never read into a preview.
- **Withheld, never guessed:** a local whose storage may still hold leftovers (declared below the stop, an initializer still running, a jump past its declaration) shows `no value yet` and why; one Peeky shows but cannot vouch for (a `do` body's first line, below a `setjmp` call) carries a `note`.
- **Set** integer locals, and integer fields and elements of a struct, class or array local the frame holds itself: `--set s.end=10`, `--set 'bins[3].hi=8'` (16 to 64 bits, in range).
- **Never** through a pointer or a reference (`ps.end`, `this.count`), nor in a union, a packed struct, a bitfield, a const or a `std::` type; strings and floats cannot be set.
- **Where:** at a statement's first instruction, so a breakpoint on its first line (`--at`, `session continue --to LINE`) or a `step` that lands there; not after `step --out`, not on a loop header's later passes.
- **Marks:** rows show `[settable]` on what `set` would change, fields and elements included; when a rule leaves nothing settable, `session inspect` says why. A refused change's hint names the one way around it.
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

`session get bins` shows each element so, and its selector (`bins[3]: bin = {lo = 6, hi = 7, count = 0} (children: peeky session get 'bins[3]')`); fields are still read and set by name (`bins[3].hi`, `--set 'bins[3].hi=8'`). A preview shows 8 members or elements, then `…`, two levels deep (`{…}` below), at most 40 characters of one member. A union is not previewed (`<union>` inside another value: only one of its members holds what was stored); a pointer inside shows its address, never what it points at; a member with no value shows `<no value>`; a C++ base class shows as a member named by its class (`{Base = {b = 1}, d = 2}`); a `std::` container keeps its summary (`v = size=2`). Types come from the build's debug information, and where it cannot vouch for one there is no preview, only the debugger's `T @ 0x…`: a local withheld at the stop, a variable-length array, an incomplete type (`op[0]` on `struct opaque *`), a value inside a `std::` container, and every value of an optimized build. A wide character array shows element by element (`[L'a', L'\0', L'b', L'\0']`, where LLDB's text stops at the first NUL), and a char-typed bitfield as its number (`sc = -3`, not LLDB's `'\x05'`). Details: NATIVE.md, CPP.md.

```sh
peeky probe examples/c/pagination_bug.c --at 25 --capture 'start,page,items[1]' --set start=0
```

```
hit  phase      line  function  selector  type  value
1    observed   25    main      start     int   3
1    observed   25    main      page      int   1
1    observed   25    main      items[1]  int   20
1    after-set  25    main      start     int   0
...
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

**Locals Peeky withholds.** A C or C++ local declared below the stop line is not shown as a value: until its declaration runs, its storage holds whatever an earlier call or loop iteration left there, so the row says `no value yet: declared at line 18, below this stop` (the declaration the debugger shows, the one in the stopped function whose block holds the stop, is declared below it: another of the name, in an earlier loop or a lambda, changes nothing; where Peeky cannot tell which is shown, any declared below withholds it. A static or parameter keeps its value).

- **Where the stop cannot be placed:** Where Peeky cannot place the stop before the declaration (the line also runs code after it, as a temporary's cleanup does, or a `goto` label lets control return above it), it is withheld all the same, saying it may not be initialized.
- **`setjmp`:** In a function that calls `setjmp` (or a macro expanding to it), below the call (a stop on the call's own line is not: a `longjmp` returns past the call), a local declared at or below the stop is shown with a `note` instead (a `longjmp` can bring control back past its declaration with it live, or this is the first pass) and cannot be changed; what is declared above the stop, and every other function, is judged as usual.
- **Names a block declares:** Inside a block that declares an outer local's name otherwise (a `static`, `extern` or `typedef`, an enumerator, also one of an enum inside a C struct's body, a function declaration, a C++ `using` declaration or class, or a `thread_local`, which the debugger lists apart as `q @ file:line`; a block opening on the stop line counts), the code names that one: the outer local's row says `not what the code reads` and cannot be changed. Peeky reads the source as the build's compiler preprocesses it (`-E`, the same flags), so whatever a macro expands to, and only what a conditional keeps, counts; a prototype's parameter names do not. Where it cannot read that (the preprocessor fails, or a system header is included inside the block), the row says `may not be what the code reads`.
- **On the stop line:** A local declared on the stop line itself is withheld at the line's first instruction, where its declaration has not run yet, so probe the next line to read it; on a line a loop returns to (a `do` body's first line) it is shown with a `note` that it may be the last pass's value, and mid-line (a `for` counter on later passes, after `step --out`) as its storage reads.
- **Part-way through the stop line:** A local declared on the stop line is withheld part-way through that line while the line still has code to run (back from a call with `step --out` in `long r = twice(3) + twice(4);`), except a variable a `for`, `if` or `switch` header sets with no call (`for (int i = 0; ...)`), which a one-line loop keeps live; a `while`'s, or one set by a call (`if (long r = twice(3))`), is withheld.
- **While its declaration runs:** A local is also withheld while its own declaration is still running at the stop: `struct outer o = {` over lines 17-22 (or an array of structs, or a 2-D array), stopped at line 20, says `its declaration (lines 17-22) is still running at this stop` until its `;` has run. The end comes from the source, read as for caller frames (5.1), and where it cannot be found the local is withheld; a declaration below the stop whose own line has no code is placed by its elements' lines.
- **Header variables:** A variable a `for`, `if`, `while`, `switch` or `catch` header declares has run with its part of the header, so the body reads it.
- **Below a label:** Below a label a jump can reach past a declaration, the local is withheld too (`a jump ... can reach the label on line 21`): a `case` or `default` of the `switch` whose body holds the declaration, or a label that a `goto` above the declaration names, or one outside its block jumps back to (re-entering a block starts its locals anew, C11 6.2.4; any label, with a computed `goto`). A `goto` counts only within its function's own lines, so a namespace's other functions do not.
- **Declared in an `#include`d file:** A local declared in an `#include`d file counts as declared at that `#include`'s line: below the stop it is withheld, and above it, when its file may `#include` more after it (a `case` there can skip it).
- **Outside the function's own lines:** When the frame (stopped, or a caller on its own file's line) runs a line outside its function's own (a class's default member initializer, as when you break on `long b = twice(2);` in `struct Cfg`, also a class defined inside the function, or an `#include`d fragment), the usual rules run from the own line that brings it in (that `#include`), or else from the last own line whose code comes before it; its class, struct and array locals are withheld too, since one of them is being built there, and where Peeky cannot place the stop, everything but parameters is.
- **`this` in C++:** In C++, `this` is withheld while its object is being built: in a constructor, in any frame a constructor called (Peeky reads up to 64 frames to tell; past that it withholds), in a lambda defined inside a declaration or a constructor that may still be running (the note names that declaration), and in a lambda a class's default member initializer defines (`long b = [this] { ... }();`), since a class with no constructor runs its initializers where the object is built. An unnamed struct's constructor (`$_0::$_0()`) counts as a constructor too.

**Members.** In C++ and Java methods, read members through `this` (`this.head_`); a bare `head_` that is a member gives an error naming that selector. Peeky never reads a member in place of a local: the debugger's list of locals can be incomplete (statics, classes built without local-variable tables), so a missing local does not mean the code uses the member.

**Strings with a NUL byte (C++).** LLDB shows a libc++ (macOS) `std::string` whole, a NUL as `\0`: `"a\0b"`. Its summary of a libstdc++ (Linux) string is a C string, which ends at the first NUL, and a value LLDB names `std::string` gets `error: summary string parsing error` on clang's Linux builds. So on Linux the worker checks each string against its `_M_string_length` (#128), in `inspect`, `get`, listings, probe rows and the evidence store alike:

- With a debugger that reads memory (lldb-dap 20), it reads the string's bytes and shows them as LLDB shows a libc++ string (`"a\0b"  [memory]`; past 1024 bytes, `"..."...`), for `std::string`, `std::wstring`, `std::u16string` and `std::u32string`, by value, reference (`0x… "a\0b"`) or pointer.
- Without one (lldb-dap 18), a text the debugger cut at a NUL keeps a note: `"v" (shown up to a NUL at byte 1 of 3)`. A string whose summary failed has no value (`no readable value: the string's summary failed ..., and this debugger cannot read memory (lldb-dap 20 can); it holds 3 bytes`); a pointer or reference to one shows `0x… <no text: ...>`.
- A string the build's debug information gives no members (clang leaves `std::wstring` to libstdc++'s own) has no length to check: its text gets `(not checked for a NUL byte: ...)`, or no value where LLDB's summary failed.
- A string under a typedef's name (`typedef std::basic_string<char> Bytes`, `using Name = std::string`, a reference or pointer to one) is checked the same: Peeky resolves the typedef from the build's debug information, or knows the string by its libstdc++ members. LLDB's `error: summary string parsing error` is never shown as a value, for any type.
- On every platform, a literal reads back in C as the same bytes: where LLDB writes `\0` before a digit (`"n\064"`, which C reads as `"n4"`) or `\xff` before a hex digit, Peeky writes `"n\00064"` and `"\377a"`. Long text stays within 2000 characters, cut between characters with `...`.
- Each libstdc++ string costs a debugger request (two or three where its bytes are read), within the inspection's budget: a large page of strings may say `(not checked for a NUL byte: ...)` instead.
- Strings cannot be `--set`; only integers (locals, fields and elements) can.
- A string is its text: `session get name` shows `name: std::string = "apple"` and no rows under it (the library's own members, `(anonymous struct)` or `_M_dataplus`, are never listed), and `inspect` offers no `(children: ...)` for it. Selecting inside one (`name[0]`, `name.size`) is refused with what to do instead: capture the string and compare its text, in SQL (4.7). A pointer to one shows its address and text, and `p[0]` is the string.

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

The path is checked against the build's debug information, not the debugger's type names: fields of structs and classes and elements of fixed-size arrays only, never through a pointer (including `this`: Peeky does not write through a pointer or a reference, since what it reaches may be freed or moved; change the value where it is owned, before the call that passed it: a probe `--at` the call's line with `--hits N --set-hit N`, or `session start --at` it with `--hit N`, where N counts the call line's own stops, not the called function's, which differ when it is called from several lines (find N with `peeky probe ... --at <call line> --capture <argument> --hits all`); `session continue --to` the call's line reaches only the next call), a reference, a base class or an anonymous member, nor inside a union, a packed struct or a `std::` type; the target is an integer of 16 to 64 bits, not const, volatile or a bitfield, and the value must fit it. The root must be an automatic or a parameter of the stopped frame that Peeky does not withhold (declared below the stop, an initializer still running, a jump past it), nor shows with a note that its declaration still runs after the stop (a `do` body's first line), in an unoptimized build; one LLDB names with where it is declared, when another of its name is in scope, is selected as named (`'p @ s1.c:38.x=3'`, `'n @ s1.c:15=9'`; two of one name declared on the same line are ambiguous, and `get` and `set` refuse them), and an element of an array of any length is fetched alone, at any step of the path (`big[700].a=1`, `grid[599][2]=3`). The field is read back from the debugger after the write, and only the value asked counts as a change. Probe rows and `session get` listings mark the fields and elements `set` would change `[settable]`, judged by the same rules at the stop (`bins[3].hi  [settable]`, never `ps.end`); a refusal names the rule it broke, and its hint the one way around it.

What a change makes true, for a local or a field: its storage holds the new value, and the program's later reads of it see it. What the program copied before keeps the old value: a copy on an earlier line (`int a = s.x;`), and a statement under way at the stop, which may hold what it read in a register. So C and C++ allow a change only at a statement's first instruction, and refuse it anywhere else: `set` says `cannot be changed here` and keeps the session, and `inspect` drops `[settable]` there and says why (`note: nothing here is [settable]: the stop is not at the first instruction of the statement on line 9 ...`; `unsettable` in JSON). Peeky proves the stop is there from the source and the line table:

- the stop is on one of its function's own lines, and that line starts a statement: every bracket around it, up to the function's body, is a block of statements (not a call's arguments, an initializer, a `new long[1]{`, a compound literal; nothing inside a GNU statement expression `({ … })` at all, even a macro's loop there), no directive or bracket-hiding macro lies between it and the previous token, and that token ends a statement (`;`, `{`, `}`, a label, `else`, the `)` of an `if`/`for`/`while`/`switch` header). A brace after something like parameters (`REPEAT(k, 1) {`) is a function's body only at file scope or in a class's or namespace's body; elsewhere it is a block of the function around it;
- the pc is the lowest address of all the function's code on the lines of the statements starting there: clang may put a later line's code first (`t =` / `p.x + g();` loads `p.x` and calls `g` before line 1's store);
- on a function's first line (a one-line function or lambda), the statements are those after its body's `{`, and the pc is the lowest of their rows past the function's entry (`long one(struct P p) { long t =` / `p.x + g();` runs the second line first, so neither line qualifies);
- no call in those statements is one the compiler inlined (`always_inline`, `flatten`; Peeky reads which from the debug information): an inlined body's code carries its own function's lines and runs before the statement's lowest row (`t = inl(&p) + p.y;`).

A breakpoint on a statement's first line (`--at`, `continue --to`) or a `step` that lands there qualifies. Back from a call (`step --out`, a return address), on a statement's later line (`+ f();` under `int t = s.a`), on a loop header's later passes, in a K&R-style definition (`long f(a) long a; {`, not supported), and wherever Peeky cannot tell, the change is refused. A `step` onto `} else if (p.y > 1) {` on the else path is refused too: that line's lowest address is the jump that ends the `if` block, so the step stops past it, at the condition (and a breakpoint on that line, placed at the jump, is never hit on the else path, with or without Peeky); change the value before the `if`, or at the first statement inside the `else if` block. Two more stops are refused though nothing is under way there: in an inlined function's own code (a one-line `always_inline` function's line, where its frame stops), and on a body's `{` placed on the line under its signature with a statement after it (`long f(struct P p)` / `{ long t = p.x + 1;`); change the value in the caller, or at the next statement. A function-try-block (`long f(P p) try { … } catch (…) { … }`) is a function body like any other, its handlers too. The stop's own line may still overwrite the change (`s = (struct tri){9, 9, 9};`).

### 7.2 Rust (a Cargo crate)

**Quick start (Rust).** What can be read, what can be set, and where:

- **Read** integer, float, `bool` and `char` locals with their Rust types (`usize`, and `&mut limiter::Window` for a reference parameter), `Option<i32>`, a struct or tuple local (or a reference parameter to one) as derived `Debug` prints it (`Window { start: 0, count: 2 }`), and the contents of `Vec<T>`, `String`, `&[T]` and `&str` (`[memory]`); fields stay selectable (`w.start`, `runs[0].__0`).
- **Never guessed:** a value Peeky cannot prove (moved, dropped or not yet assigned, in a caller frame, in an optimized build) keeps its raw fields with a note (`[stale]`, `[unproven]`, `<unverified ...>`).
- **Set** integer locals (`i16` to `i64`, `u16` to `u64`, `isize`, `usize`) the program keeps in memory, and integer fields and elements of a struct, tuple or array local the frame holds itself, which the MIR proves initialized: `--set w.start=10 --set w.count=0`.
- **Never** through a reference or a `Box` (`w.start` where `w: &mut Window`: Peeky does not write through a reference, since what it reaches may be freed or moved; change the value where it is owned, before the same call: a probe `--at` the call's line with `--hits N --set-hit N`, or `session start --at` it with `--hit N`, where N counts the call line's own stops, not `allow`'s, which differ when `allow` is called from several lines (find N with `peeky probe ... --at <call line> --capture now --hits all`); `session continue --to` the call's line reaches only the next call, and `session step --out` returns part-way through the line, where changes are refused), nor inside an enum, a `#[repr(packed)]` struct or a standard-library type. A local kept only in a register is refused: a candidate edit adding `std::hint::black_box(&mut x);` after its `let` keeps it in memory.
- **Where:** a line breakpoint, or a step that lands on a line's start, where the MIR shows no statement under way has read the value; never back from a call (`step --out`).
- **Marks:** rows show `[settable]` on what `set` would change, fields and elements included; when a rule leaves nothing settable, `session inspect` says why.
- **Builds:** an unoptimized build (the default) for every change and for owned `Vec`/`String` contents; a plain `cargo new` crate with no dependencies.

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
- **Changing values:** Only integer locals (`i16` to `i64`, `u16` to `u64`, `isize`, `usize`) that the program keeps in memory can be changed; `session inspect` marks them `[settable]`. So can such integer fields and elements of a struct, tuple or array local kept in memory that the build's MIR proves initialized at the stop (`--set s.end=10`, `t.__0=3`, `arr[2]=7`): never through a reference or a `Box`, nor inside an enum, a `#[repr(packed)]` struct or a standard-library type, and only at a line breakpoint, or a step that ends at its line's start, where the MIR shows no statement under way has read the local into a temporary (so never back from `f` in `let t = s.a + f();`, nor after a step back onto `let arr = [s.a,` from its later lines), which also holds for integer locals (see [RUST_AGENT.md](RUST_AGENT.md#changing-fields-and-elements-105)).
- **Reference parameters:** Reference parameters (`&[T]`, `&str`, `&Vec<T>`, `&String`, shared or `&mut`, for `T` any integer type, `bool`, `f32`, `f64` or `char`) are read from memory at offsets checked against the build's debug information (`describe` lists them). Elements print as Rust's `{:?}` does, floats included (`[0.30000000000000004, 1e16, -0.0, NaN]`), so they can be compared with the program's own output. In `fn median(values: &[i32], label: &str)`, `--capture 'values,values[0],values[-1],values.length,label'` shows `[5, 1, 3]`, `5`, `3`, `3` and `"größe"`. Their type column shows the Rust type (`&mut limiter::Window`), not LLDB's (`limiter::Window *`), which `debugger_type` keeps.
- **Tuples and structs:** Tuples, and structs your crate defines, of those scalars are read too: `runs` (`Vec<(char, usize)>`) shows `[('a', 3), ('b', 1)]`, and `runs[0].__0` and `spans[1].start` select fields (LLDB's names: `__0` for a tuple's first). A tuple or struct local of them, or a reference parameter to one, previews the same way under the same proofs (`request` above: `Request { page: 1, page_size: 3, total: 8 }`), and its fields stay selectable (#106). See [RUST_AGENT.md](RUST_AGENT.md#reading-rust-values).
- **Owned `Vec` and `String`:** Owned `Vec<T>` and `String` locals are read as well, in the innermost frame, wherever the compiler's own MIR proves them initialized at the stop line: Peeky builds the binary crate with `-Zmir-opt-level=0` and dumps its MIR, so every move, drop and assignment is explicit. A local that may be moved, dropped or not yet assigned around the line (`drop(ordered)`, `consume(v)`, the old `s` in `let s = s + "x"`, a deferred `let label: String;`) keeps its raw fields (`ordered.len`) with a note, since its pointer can reach reused memory and its contents would be a guess (#53). Reference locals (`let view: &[i32]`) keep their raw fields too.
- **Long values:** Long values show their first elements with `(first k of N)`, and up to 256 elements stay selectable.
- **Debugger:** This needs a debugger with `readMemory` (lldb-dap from LLVM 20); older debuggers keep the raw fields, with an error that names the length field.

### 7.3 Java (launched)

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
2    after-set  26    MergeBug.main(String[])  next      MergeBug$Interval  MergeBug$Interval@63 {end=14, start=12}
set current.end: 5 -> 10
output:
  MERGED:[[1,10], [12,14]]
```

The output changed from `[[1,5], [12,14]]` to `[[1,10], [12,14]]`: the merge was right except for `end`. `this.count=0`, `arr[0]=1` and `arr[-1]=9` work the same way, and `session set current.end=10` changes a field in a session. Captured fields and elements say `[settable]` when they can be changed. Final and static fields, Strings, boxed values (`Integer`) and an `ArrayList`'s elements cannot (change a field of one: `merged[0].end=10`). The value is read back from the object after the change. A field whose name a subclass's static field hides, or another field shares, is refused (java-debug would write the other field). Field and element changes need an adapter bundle built by this version of `scripts/setup_java_adapter.py`; an older bundle changes only locals and says so.

A value must fit the variable's type exactly. For example, `int` 3000000000 and `010` (octal for `Long.decode`) are refused rather than silently changed. A line that several methods share (a lambda body on its method's line, like `.filter(item -> below(item))`, a static initializer's lambda, nested lambdas) stops in only one of them: java-debug picks the first in the class file. Peeky warns which one stops and which never do (`stops only in needsRestock there, never in lambda$needsRestock$0`). To stop in another, put the breakpoint on a line of its own, or `session step --into` from that line.

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

Set `"optimization": "O2"` (C/C++) or `"opt_level": "2"` (Rust) in a `--config` to debug the optimized program. Locals may then be `unavailable`, shown values can be stale, and changes are refused; every result carries `optimized` and a warning.

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
| `next`, `step`, `finish` | `peeky session step`, `step --into`, `step --out` | Java `--into`/`--out` skip JDK code. After `step --out`, C, C++ and Rust `session inspect` lists the returned value as `(Return Value)`, marked `[not selectable]`: read it there |
| `until 42`, `advance 42` | `peeky session continue --to 42` | One-shot; the session's own breakpoint stays |
| `continue` | `peeky session continue [--to-exit]` | Runs to the next stop or to exit |
| "rerun with another input and compare by eye" | `--arg ... --label A`, then `peeky queries diff` | The `(output)`, `(args)`, ... rows come first, then the first divergence |
| "I think this fixes it" | `peeky verify --old ... --new ...` | Fresh build plus an independent checker |
