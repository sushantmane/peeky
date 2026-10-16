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

C, C++ and Rust list the same from the build's debug information: each function in your sources, with its file, lines and parameters and locals. Functions carry the names that probes show in their `function` column (Rust: `crate::Type::method`, `<crate::Type as core::fmt::Display>::fmt`). Closures and lambdas are listed under their function. A function inlined into another (`#[inline(always)]`, `always_inline`) keeps its own lines. If the debug information cannot be read, the text says `function names: unavailable (reason)`:

```
names in parse_count (pagination_bug.c:6-14): end, minimum, result, text, value
names in main (pagination_bug.c:16-31): argc, argv, end, i, items, page, page_size, start, total

names in peeky_pagination::main (src/main.rs:22-41): args, end, mode, none, request, some, start, values
names in peeky_pagination::main::{{closure}} (src/main.rs:37-37): index
```

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

Selectors are dotted paths, indexes and string keys (`request.page`, `args[0]`, `items[-1]`, `order['id']`), never expressions: Peeky never evaluates code in your program. Python dict entries are read by key; `order.id` fails with a message that names `order['id']` and lists the keys. A selector that is not there lists what is. Quote selectors that contain `[...]`: zsh, the macOS default shell, would otherwise treat them as filename patterns.

### 2.2 Every Iteration of a Loop

**What it answers:** How does a value evolve across iterations?

```sh
peeky probe examples/corpus/running_max/program.py --at 6 --capture value,best --hits 3
peeky queries hits
```

```
selector  hit  line  value  changed
best      1    6     0      NULL
best      2    6     0      0
best      3    6     0      0
value     1    6     -3     NULL
value     2    6     -1     1
value     3    6     -2     1
```

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

Only root locals can be changed: the stopped function's own variables (`start`), not their fields or elements. Python takes literals, including lists and dicts. C, C++ and Java take scalars of the local's type. Rust takes `i32` locals that the program keeps in memory; any other change is refused, because it would not reach the program. A refusal (`MUTATION_REJECTED`) names the local and the rule.

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

`--run` names the script to execute when the breakpoint is in a module it imports. `--arg` is repeatable; use `--arg=-v` for values starting with a dash. C, C++, Rust and launched Java programs take `--arg` too.

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
run_a  run_b  hit  phase     selector  value_a                        value_b                      differs  note
1      2      1    observed  self      IPv6Address('::ffff:808:808')  IPv6Address('::ffff:a00:1')  1        NULL
1      2      1    observed  self._ip  281470816487432                281470849515521              1        NULL
```

`diff` keeps only the rows that differ. Real differences come first, in hit order and then in `--capture` order, so the first row is the first divergence. Then come values that only one run's `--set` changed (`intervention only in run b`), then values that differ only in heap or stack addresses (`note: addresses only`, for example a C string's pointer; NULL is never treated as an address), and last, one row per selector that only one run asked for (`only in run a`). Hits that one run never reached say `missing in run b`. Comparing needs two saved runs; with fewer, the query says so. `queries compare` shows every value side by side with the same notes. Labels are free text, stored as given.

### 3.2 Inputs, Outcomes and Outputs of the Last Two Runs

```sh
peeky queries outputs
```

```
run_id  label           args                 sets  status     error_code  output
1       mapped-public   ["::ffff:8.8.8.8"]   []    completed  NULL        ::ffff:8.8.8.8: private\n
2       mapped-private  ["::ffff:10.0.0.1"]  []    completed  NULL        ::ffff:10.0.0.1: private\n
```

Both are labelled private: the public one is the bug.

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
| `evidence` (view) | captures joined with their run: no JOIN needed |
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

`value` is the debugger's text; `num` is that value as a number, and NULL when it isn't one.

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
session default (python): state: stopped at .../pagination_bug.py:8 (paused 0.0 s)
stopped at .../pagination_bug.py:8 (paused 0.05 s)
[Locals]
  items: list = [10, 20, 30, 40, 50, 60] (children: peeky session get items)
  page: int = 1
  page_size: int = 3
  start: int = 3
start: int = 3
items[0]: int = 10
page: int = 1
items: list = [10, 20, 30, 40, 50, 60]
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

`session get` on a list, dict or struct lists one level of children, each with the selector that reads it. If any selector fails, every row is still printed and the exit status is 1. `session continue` without `--to-exit` runs to the next stop at the session's breakpoint; there is no single-stepping, so start the session at the line you want to look at.

### 5.2 Several Sessions at Once

```sh
peeky session start app.py --at 12 --name a
peeky session start app.py --at 40 --name b
peeky session get total --name b
peeky session list
```

A second `session start` with a name that is already taken fails with "exists". It never takes over the other session.

### 5.3 Programs with Timeouts

Tests with timeouts, heartbeats and RPC deadlines keep their clocks running while the program is paused. Prefer probes, which pause for milliseconds per hit, or give sessions a pause budget:

```sh
peeky session start app.py --at 12 --max-pause 5      # auto-resume after 5 s
peeky session inspect                                   # shows paused_s and resume_in_s
```

A breakpoint stop older than the budget is resumed; your next call says so (`STALE_STOP`). Crash and exception stops are never auto-resumed.

### 5.4 Cleaning Up

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

Your files are not changed. The edit is applied to a scratch copy (`--keep` keeps it).

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

### 6.4 Writing a Checker

A checker is a trusted script, run as `python checker.py <candidate-path>`. It exits 0 only when the behavior is right. Test the general rule, not the one input from the bug report (see `examples/verify_pagination.py`). For C, C++ and Rust it gets a build description to run the fresh binary (see [NATIVE_AGENT.md](NATIVE_AGENT.md) and [RUST_AGENT.md](RUST_AGENT.md)); for Rust, the crate's compiled Cargo tests must pass too.

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

All `.c` files in the directory that don't define their own `main` are built together with the target. A directory holding several programs links only the target's `main`. Headers read from outside the directory, including SDK headers, are hashed and rechecked.

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

Values Peeky cannot decode with certainty show as `<unverified ...>` or as opaque enums; they are never guessed (`describe` counts them as "other enum types"). Only `i32` locals that the program keeps in memory can be changed. The elements of a `Vec`, slice or `String` are not readable yet: the error says so and names the length field to capture instead (`ordered.len`, `values.length`).

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
1    observed   13    PaginationBug.main(String[])  start     int    3
1    observed   13    PaginationBug.main(String[])  pageSize  int    3
1    observed   13    PaginationBug.main(String[])  items     int[]  int[8]@51
...
set start: 3 -> 0
status: completed  hits: 1  seconds: 0.558
output:
  PAGE:[10, 20, 30]
```

A value must fit the variable's type exactly. For example, `int` 3000000000 and `010` (octal for `Long.decode`) are refused rather than silently changed.

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
| `NO_STOP` | The line never ran (e.g. `--at 6`, the `raise` that valid input never reaches) | Pick a line the run executes; `describe` lists lines with code, not lines that run |
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
| `continue` | `peeky session continue [--to-exit]` | Runs to the next stop or to exit; there is no step or step-out |
| "rerun with another input and compare by eye" | `--arg ... --label A`, then `peeky queries diff` | The first diff row is the first divergence |
| "I think this fixes it" | `peeky verify --old ... --new ...` | Fresh build plus an independent checker |
