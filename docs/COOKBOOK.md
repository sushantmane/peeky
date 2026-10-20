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

C, C++ and Rust list the same from the build's debug information: each function in your sources, with its file, lines and parameters and locals. Names are readable (`crate::Type::method`, `<crate::Type as core::fmt::Display>::fmt`, `geo::Box::area`). A probe's `function` column keeps LLDB's frame name, which matches for plain functions and methods. For Rust trait impls and closures it keeps symbol escapes (`_$LT$…`, `_$u7b$$u7b$closure$u7d$$u7d$`), and for C++ it adds the signature, so match those in SQL with `LIKE '%fmt%'`. Closures and lambdas are listed under their function. A function inlined into another (`#[inline(always)]`, `always_inline`) keeps its own lines. Each name shows its type. A local declared only inside nested blocks also shows those blocks' lines, where it is visible: `i (int, lines 23, 28)` is two one-line `for` loops' `i`, not a name of the whole function. A block that also runs another file's code (an `#include` inside it) gives none (a local macro's expansion, on its definition's lines, does not count), and past eight blocks one span covers them all. In Rust every `let` opens a block, so a local shows the lines after its `let`, where it is visible (#107). If the debug information cannot be read, the text says `function names: unavailable (reason)`. A very large optimized C++ unit can exceed the 16 MB bound on reading it:

```
names in parse_count (pagination_bug.c:6-14): end (char *), minimum (int), result (int *), text (const char *), value (long)
names in main (pagination_bug.c:16-31): argc (int), argv (char **), end (int), i (int, lines 23, 28), items (int[128]), page (int), page_size (int), start (int), total (int)

names in peeky_pagination::bounded (src/main.rs:14-20): default (i32), minimum (i32), text (core::option::Option<&alloc::string::String>), value (i32, lines 16-19)
names in peeky_pagination::main::{{closure}} (src/main.rs:37-37): index (i32)
```

With `--json`, a scope's `spans` maps such a name to its blocks' `[first, last]` lines.

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

Selectors are dotted paths, indexes and string keys (`request.page`, `args[0]`, `items[-1]`, `order['id']`), never expressions: Peeky never evaluates code in your program. Python dict entries are read by key; `order.id` fails with a message that names `order['id']` and lists the keys. A selector that is not there lists what is. Quote selectors that contain `[...]`: zsh, the macOS default shell, would otherwise treat them as filename patterns. In C and C++, follow a pointer with `.` (`stats.total_us`, not `->`); a pointer to a scalar carries no length, so `values[0]` reads what it points at; for other elements, read the array itself where it is declared (in the caller). The same holds for a pointer to an array, a container or a struct: `pa[0]` is what it points at, so `pa[0][1]` is `(*pa)[1]`, while `pa[1]`, which C reads past the pointee, is refused with that selector (#102). A typedef'd pointer (`RowPtr`, `const RowPtr`) works the same; the build's debug information tells it from a typedef'd reference, which indexes its referent. A pointer under a type name it does not resolve (a template's member typedef such as `std::vector<T>::pointer` to a container) is refused for indexing rather than read either way. A NULL or unreadable pointer is refused (`p is NULL`). Rust references and Boxes are never indexed this way.

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

Root locals can be changed: the stopped function's own variables (`start`). In Python a change rebinds the name in that frame: `--set 'seen=[]'` gives this call a new list, while the shared object it named (a default argument, a module global) keeps its contents, and the next call sees it again. In Java, so can scalar fields and array elements (see 7.3). Python takes literals, including lists and dicts. C and C++ take integers of 16 to 64 bits (`int`, `long`, `size_t`, `int64_t`, …) within the local's range; Java takes scalars of the target's type. Rust takes integer locals (`i16` to `i64`, `u16` to `u64`, `isize`, `usize`) that the program keeps in memory; any other change is refused, because it would not reach the program. For C, C++ and Rust, `session inspect` marks the locals whose type allows a change with `[settable]`. Every change is read back: one the debugger refuses or cannot store (a computed value, such as a structured binding of a bitfield) is refused and the session kept, and a value stored differently from the one asked for (a type redefined as a float) closes the session. A refusal (`MUTATION_REJECTED`) names the local and the rule.

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
1      2      1     observed  self      IPv6Address('::ffff:808:808')  IPv6Address('::ffff:a00:1')  1        NULL
1      2      1     observed  self._ip  281470816487432                281470849515521              1        NULL
1      2      NULL  NULL      (output)  ::ffff:8.8.8.8: private\n      ::ffff:10.0.0.1: private\n   1        output differs
```

`diff` keeps only the rows that differ. Real differences come first, in hit order and then in `--capture` order, so the first row is the first divergence. Then come values that only one run's `--set` changed (`intervention only in run b`), then values that differ only in heap or stack addresses (`note: addresses only`, for example a C string's pointer; NULL is never treated as an address), then one row per selector that only one run asked for (`only in run a`, with its first hit's value), and last, `(status)` and `(output)` rows when the runs' outcomes or outputs differ, an `(edit)` row when one run built a candidate edit, a `(source)` row when two runs that are not candidates ran different source text (another copy of the file), a `(line)` row when the runs stopped at different lines (their values then differ partly because of where they were read), an `(args)` row when they ran with different program arguments, and a `(sets)` row when they made different interventions. Check those last rows before crediting an edit with a difference. The `(output)` row shows each output from just before the first character that differs (`output differs at line 4, column 13`, counted from 1), so after a `--set` it shows the intervention's effect. Hits that one run never reached say `missing in run b`. A value whose type changed (an edit to `long long`) says `type unsigned int -> long long`. `diff` compares the last two runs unless you name them: `--param a=1 --param b=5`. Comparing needs two saved runs; with fewer, the query says so. `queries compare` shows every value side by side with the same notes. Labels are free text, stored as given.

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

At one hit, equal identities are the same object: `tags` is the function's default list, not a copy. Across hits, a different identity always means a different object, but an equal one means the same object or a new one that took a freed object's place; here the default lives as long as the function, and its contents grow. Numbers, strings and `None` have no identity (NULL). Never compare identities across runs. `scope` is `globals` for a selector whose first name was read from the module's globals (5.1).

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
operations: 11 (worker requests in this session, including waits; --json lists them)
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

At module level the locals are the module's globals, so there is no separate `[Globals]`. A frame past the stack is `INVALID_REQUEST`, naming how many frames there are. Rust's owned-local and Box proofs hold only in the innermost frame, so a caller's `Vec` keeps its raw fields with a note. In C and C++, a caller's local is withheld while its declaration's code is not finished: in `long result = twice(seed);`, or `long multi =` over `twice(seed + 1);`, the value is stored only after `twice` returns (so are a `for` loop's counter while its body calls, on one line or several, since the increment's code follows the body, and a local whose declaration line has no code of its own: an array of structs, a nested struct or a 2-D array initialized over several lines is stored element by element on its elements' lines). A caller's values cannot be changed (`MUTATION_REJECTED`), so caller listings never say `[settable]`.

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
1      2      1     observed  start     6                 3                     1        NULL
1      2      NULL  NULL      (output)  PAGE:[70, 80]\n  PAGE:[40, 50, 60]\n  1        output differs at line 1, column 7
1      2      NULL  NULL      (edit)    none              candidate             1        the runs built different edits (runs.edit has each diff)
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
- **Intervening where no local can be set:** `--set` works in the same probe as `--old`/`--new`. Where the value you want to change is a field (Rust, C, C++, Python), add a local holding it with the edit, on an earlier line (a local is visible only after its declaration), and set that local. In Rust, at the line `last.end = s.end;` that follows `if s.start <= last.end {`:

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

All `.c` files in the directory that don't define their own `main` are built together with the target. A directory holding several programs links only the target's `main`. A directory works as the target too: `peeky probe ring/ --at 16` finds its one program (a checker script beside it doesn't count), and `--at ring.cpp:16` names one when there are several. A C or C++ local declared below the stop line is not shown as a value: until its declaration runs, its storage holds whatever an earlier call or loop iteration left there, so the row says `no value yet: declared at line 18, below this stop` (only when every local of that name in the functions with code on that line is declared below it, and the line's code comes before the declaration's in the build. A static or parameter keeps its value. Nothing is withheld in a function with a `goto` label, or in a program that uses `setjmp`: jumping back above a declaration leaves the local live). A local declared on the stop line itself is shown as its storage reads (a `for` counter is live there on later passes); at a breakpoint on that line its declaration has not run yet, so probe the next line to read it. In C++ and Java methods, read members through `this` (`this.head_`); a bare `head_` that is a member gives an error naming that selector. Peeky never reads a member in place of a local: the debugger's list of locals can be incomplete (statics, classes built without local-variable tables), so a missing local does not mean the code uses the member. Headers read from outside the directory, including SDK headers, are hashed and rechecked.

### 7.2 Rust (a Cargo crate)

```sh
peeky describe examples/rust
peeky probe examples/rust --at 36 --capture start,some,none,mode,request.page --set start=0
```

```
breakpoint lines in src/main.rs: 14-17, 20, 22-25, 27-30, 32-38, 40-41
qualified enum views: core::option::Option<i32>

hit  phase      line  function                selector      type                       value
1    observed   36    peeky_pagination::main  start         int                        3
1    observed   36    peeky_pagination::main  some          core::option::Option<i32>  Some(3)  [active-variant]
1    observed   36    peeky_pagination::main  none          core::option::Option<i32>  None  [active-variant]
1    observed   36    peeky_pagination::main  mode          peeky_pagination::Mode     First
1    observed   36    peeky_pagination::main  request.page  int                        1
...
set start: 3 -> 0
output:
  PAGE:[10, 20, 30]
```

Values Peeky cannot decode with certainty show as `<unverified ...>` or as opaque enums; they are never guessed (`describe` counts them as "other enum types"). Only integer locals (`i16` to `i64`, `u16` to `u64`, `isize`, `usize`) that the program keeps in memory can be changed; `session inspect` marks them `[settable]`. Reference parameters (`&[T]`, `&str`, `&Vec<T>`, `&String`, shared or `&mut`, for `T` any integer type, `bool`, `f32`, `f64` or `char`) are read from memory at offsets checked against the build's debug information (`describe` lists them). Elements print as Rust's `{:?}` does, floats included (`[0.30000000000000004, 1e16, -0.0, NaN]`), so they can be compared with the program's own output. In `fn median(values: &[i32], label: &str)`, `--capture 'values,values[0],values[-1],values.length,label'` shows `[5, 1, 3]`, `5`, `3`, `3` and `"größe"`. Tuples, and structs your crate defines, of those scalars are read too: `runs` (`Vec<(char, usize)>`) shows `[('a', 3), ('b', 1)]`, and `runs[0].__0` and `spans[1].start` select fields (LLDB's names: `__0` for a tuple's first). See [RUST_AGENT.md](RUST_AGENT.md#reading-rust-values). Owned `Vec<T>` and `String` locals are read as well, in the innermost frame, wherever the compiler's own MIR proves them initialized at the stop line: Peeky builds the binary crate with `-Zmir-opt-level=0` and dumps its MIR, so every move, drop and assignment is explicit. A local that may be moved, dropped or not yet assigned around the line (`drop(ordered)`, `consume(v)`, the old `s` in `let s = s + "x"`, a deferred `let label: String;`) keeps its raw fields (`ordered.len`) with a note, since its pointer can reach reused memory and its contents would be a guess (#53). Reference locals (`let view: &[i32]`) keep their raw fields too. Long values show their first elements with `(first k of N)`, and up to 256 elements stay selectable. This needs a debugger with `readMemory` (lldb-dap from LLVM 20); older debuggers keep the raw fields, with an error that names the length field.

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
| "rerun with another input and compare by eye" | `--arg ... --label A`, then `peeky queries diff` | The first diff row is the first divergence |
| "I think this fixes it" | `peeky verify --old ... --new ...` | Fresh build plus an independent checker |
