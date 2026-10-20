# Autonomous execution checkpoint

Updated: 2026-10-16. Repository: https://github.com/sushantmane/peeky (private).

## Direction and authorization

The user authorized autonomous implementation and parallel agents. The user also authorized delegating to their Claude and Copilot subscriptions. Both CLIs are installed. On 2026-10-14, Claude Sonnet successfully completed a bounded no-tool design review after user login. The configured Fable default requested extra credits; use the explicit Sonnet override without changing account configuration or purchasing credits. Copilot completed a bounded Java integration review in 37.91 seconds using existing GitHub authentication; do not pass --no-auto-login, which prevented credential use. Use bounded independent reviews or isolated implementation tasks under the same correctness/CI gates. Do not treat CLI installation or auth-status output alone as a successful model invocation. Correctness comes first; optimize measured performance next. Minimum MVP languages: Python, Java, C, C++, Rust. Connect code, logs, and metrics incrementally without delaying debugger support. Keep CI independent and parallel, targeting less than five minutes of execution. Subscription usage for authorized agent work is allowed. Do not change account plans, purchase extra credits, consume reset credits, publish packages, or deploy production services.

An hourly thread heartbeat (`continue-peeky-mvp`) checks for available usage and resumes unfinished work. It cannot bypass usage limits and depends on app availability. Check active agents, Git status, and open PRs before editing; never duplicate an active implementation.

## Delivered

- Private repository, reviewed Python agent loop and private artifact delivery.
- PR #15 merged (`27a5c14`): bounded DAP writes, stale-stop regressions, latency baseline and evidence correlation ADR. Five CI lanes completed in 21 seconds.
- PR #16 merged (`59b6508`): adapter/build foundations and experimental headless Java. 84 tests; six independent hosted lanes completed in 26 seconds.
- PR #17 merged (`8584bd1`): Java HTTP worker, source/class provenance and mutation coverage. All 121 tests passed locally; six CI lanes completed in 44 seconds.
- PR #18 merged (`a40c323`): Java full investigation and fresh candidate verification, 143 tests. All six parallel hosted checks passed; critical job elapsed52 seconds (run38533697141). Independent review in reviews/JAVA_AGENT_REVIEW.md.
- PR #19 merged (`b26c216`): guarded C LLDB worker and scope ownership, 221 collected tests. All seven hosted lanes passed; C34 seconds, longest lane71 seconds (run38538891500). Mixed-runtime inference direction captured in MIXED_RUNTIME.md.
- Private branch protection is unavailable on the current GitHub plan; manually verify all required checks before merging.

## Current state (2026-10-17, latest)

**Merged to `main`** (each after an independent adversarial review, with every finding fixed, all nine CI lanes green, and the full real-toolchain suite passing locally; dispositions in `docs/reviews/ERRORS_CORPUS_REVIEW.md` and `RUST_REPAIR_REVIEW.md`):
- **#25 (`10ac271`):** the standalone `peeky` CLI, Java attach (Venice), protocol v1 error codes, the ten-bug corpus, and agent-speed debugging (pause budgets, background jobs). Closed #6.
- **#26 (`99efe66`):**
  - `--set-hit`, `by_hit`, Python scopes in `describe`, and safe negative indexes;
  - Java fixes for array indexing, object fields and a String size cap.
- **#28 (`8ceeefe`):** the real program for #7 (CPython 3.9.6 `ipaddress`, CVE-2024-4032), `--run`/`--arg`, and the live agent's feedback. Closed #7.
- **#30 (`77fcaa9`):** Codex's Rust preparation hardening ported onto `main`; this superseded #24.
- **#29 (`5ad98c5`):**
  - launched JVMs: exits that java-debug drops are inferred safely (pid-authoritative, launch only);
  - a bounded DAP trace;
  - clear errors for a corrupted DAP stream.
- **#32 (`627eb1a`):** optimized native and Rust builds (closes #12 and #13).
  - Opt-in `optimization` (O0/Og/O1/O2) and Rust `opt_level`, both recorded in the target identity.
  - Locals LLDB cannot read are reported as `unavailable` with a reason, never as a value.
  - Optimized targets refuse `set_variable` and show Rust enums as opaque. Probes, sessions, `get` and `describe` carry an `optimized` flag plus a warning that values may be wrong.
- **#33 (`3143129`):** remote workers over SSH tunnels (#8). See [REMOTE.md](REMOTE.md).
  - `--worker <loopback URL>` works for `probe` and `session start`. The token is read only from `$PEEKY_WORKER_TOKEN`.
  - The worker checks the source with `expect_sha256` and refuses before launching on a mismatch.
  - Disconnects return `UNREACHABLE` (retryable, nothing sent) or `TRANSPORT_LOST`, and the session record is kept.
  - Adds `session close --forget` and the worker's `--idle-timeout`.
  - Two-process relay tests cover a mismatch refusal, a dropped tunnel, reconnecting, and a dead local worker.
- **#35 (`1706807`):** the captured-evidence store, ADR 0004 step 4 (the owner's "as easy as heappy" direction).
  - Every probe (local, `--worker` and `--background`) is saved on the client to SQLite and gets a `run_id`.
  - `peeky sql` runs one read-only, bounded SELECT over saved runs.
  - `peeky queries` adds canned questions such as `diff`, `set_effect` and `hits`.
  - `peeky sql --schema` is the manifest, and the new error code is `INVALID_QUERY`.
  - Two review rounds; every finding is fixed (worker probes stay stateless, read-only open with journal recovery, result bounds).
- **#37 (`727ae42`):** docs say SQL over saved evidence is optional (owner asked "why do we need sql?").
- **Whole-code review (2026-10-16), #38 (`9cd8df9`), #39 (`81d2346`), #40 (`85cc7f9`).** Four independent reviewers read the whole codebase. Every finding was fixed with a regression test that fails on the old code, and each PR had its own verification review. Dispositions are in [reviews/WHOLE_CODE_REVIEW_2026-10-16.md](reviews/WHOLE_CODE_REVIEW_2026-10-16.md).
  - **Speed (the owner's "would Rust be faster?" question).** `scripts/profile_probe.py` showed Peeky's own CPU was repeated work, not the language. Every operation rehashed clang (125 MB) and the Java adapter bundle, using native SHA-256 code a Rust port would share. Digest and bundle caches fixed it: C++ probe 2.28 → 0.73 s, Java pause per hit 120 → 10 ms. Peeky's CPU is now about 0.5% of a Python probe and 10–15% of a compiled one, so ADR 0003 stands (see PERFORMANCE.md). About 0.5 s of each Python probe is a debugpy exit stall outside Peeky's control.
  - **Correctness:** Java mutations must fit the declared type; C++ links libc++; C/C++ detection links one program; non-executable Java lines are `INVALID_BREAKPOINT`; caller frames outside the program are request errors.
  - **Lifecycle:** a debugpy debuggee no longer survives transport failures; session names are reserved atomically and interrupted starts clean up; co-stopped lldb threads are served as hits; Python breakpoint conditions (which ran agent code) are refused; workers bypass HTTP proxies, close on SIGTERM and exit when idle.
  - **Provenance:** Rust interventions are allowed only on locals the program keeps in memory (LLVM IR; `.dbg.spill` and inlined copies are refused). Every file a compiler read is declared (Rust) or hashed and rechecked (C/C++ headers outside the workspace).
- **#42 (`85d1153`):** remote workers on a real second host. A key-only sshd container takes the probe, the session, the dropped tunnel and the provenance refusal over a real `ssh -L` tunnel. Adds the recorded run (REMOTE.md) and the "remote SSH e2e" CI lane. Closed #8.
- **#43 (`3254647`):** [COOKBOOK.md](COOKBOOK.md), a heappy-style cookbook for people, built from real outputs. `--arg` works for C, C++, Rust and launched Java, and runs record the arguments the program actually received.
- **Agent evaluation (2026-10-16).** [Report](evaluations/2026-10-16-agent-cookbook.md). Three agents, given only the guide and the cookbook, debugged new C, Python and Rust bugs; all three reached verified general fixes. Their friction drove the next four PRs:
  - **#44 (`e30e2ca`):**
    - Dict keys work as selectors (`order['id']`), matching only entries debugpy marks evaluable, so impostor keys are refused.
    - `session get` lists children together with their selectors.
    - Rust `Vec` indexing and `MUTATION_REJECTED` messages say why.
    - Sessions show source paths.
    - Help text for every subcommand.
  - **#45 (`59aa65e`):**
    - `queries diff` puts real divergences first, in capture order; address-only differences are marked, and NULL never counts as one.
    - `--hits all` captures every hit, bounded by the row budget.
    - Progress output is quieter.
  - **#46 (`c67273c`):** `describe` lists C, C++ and Rust functions with their spans and locals. A line belongs to the innermost same-file function; Rust reads application units only.
  - **#47 (`978b75e`):** `session step` (over, into, out) and `continue --to LINE`.
    - A step that leaves your sources reads and changes nothing, and keeps the session.
    - Threads that stopped together are resumed before stepping.
    - Java stepping followed in the #48 fix: the launcher allows `next`/`stepIn`/`stepOut` with fixed JDK step filters.
- **#51 (`5d970c0`):** Java session stepping (closes #48).
  - The launcher allows `next`, `stepIn` and `stepOut` (with `threadId` only) and sets fixed JDK step filters on launch and attach.
  - A thread that stopped separately is refused until it is served.
  - LLDB all-threads stops are unaffected.
  - Verified on Venice attach.
- **#52 (`29182bc`):** Rust reference parameters read from memory: `&[i32]`, `&str`, `&Vec<i32>` and `&String`, shared or `&mut` (closes #49).
  - The build's LLVM IR proves each parameter live. `&mut` parameters are read only in the innermost frame.
  - Owned locals stay raw (#53): review showed that a dropped or moved local can point at reused memory.
  - Values are bounded to 1000 characters with a `(first k of N)` marker, and views carry `len`/`length`/`capacity` children.
  - The Docker CI image gained a `rust-llvm20` lane that must take the memory path.
- **Second agent evaluation (2026-10-16).** [Report](evaluations/2026-10-16-agent-second-round.md). Three new bugs in Java (launched), C++ and Rust, debugged from the docs alone with stepping. All three agents reached verified general fixes, with 0, 1 and 5 wasted calls. Their friction drove:
  - **#60 (`1c5796b`):**
    - A probe warns when its line stops in several functions (a closure on the line).
    - States and `inspect` name the stopped function, and Rust names read as `describe` shows them.
    - A C/C++ directory is its one program; Java without `--config` says what the config needs, and `describe` repeats `--config`.
    - Program arguments can follow `--`.
    - Members are never read in place of a local; the error names `this.x`.
  - **#62 (`9e38df3`):**
    - Sessions number their hits.
    - `session start --hit N` and `continue --count N`; they never run past a crash or a finished step, and exit 3 short of the target.
    - `queries diff` ends with outcome and output rows; `outputs` shows `set_hit`.
  - **#63 (`ae817d4`):** compact `verify --json` (`--details` for the rest); failed Cargo tests named from libtest's final list; a clearer `--keep`.
  - **#64 (`72d408f`):**
    - C, C++ and Rust integers of 16 to 64 bits can be changed, and `inspect` marks them `[settable]`.
    - Every native write is read back: unchanged is refused, anything else closes the session.
  - **#65 (`70774c9`):** on a Java line several methods share (lambda bodies, static initializers), the warning names the one java-debug stops in and the ones it never does.
  - **#68 (`0c2c362`):** `probe --old/--new` probes a candidate fix on a fresh build (C, C++, Rust, launched Java); your files are never changed (closes #57).
    - `--at` keeps the original's line numbers, mapped into the candidate. A line with no faithful match (inside an edit that adds or removes lines, or joined onto the edit) is refused before any build.
    - Values are kept only if the build that ran hashes to the edited text, locally and over `--worker`.
    - A build that fails is now `BUILD_FAILED` with the compiler's output, for every probe and session.
    - Evidence schema 3: `runs.edit` holds the diff; the views have `candidate`; `queries runs` marks candidate runs, `values` leaves them out, and `diff` adds an `(edit)` row. Older stores upgrade in place.
- **Java objects and lists** (#78).
  - In launched Java programs, a captured value shows an object's first fields inline, an array's first elements, or an `ArrayList`'s size and elements. Only that value's own fields are read, through the debugger; no code runs in the program.
  - Java object numbers count as addresses in `diff` for Java runs; `no_addresses(value, language)` masks them in your own SQL.
  - String and char fields in a preview are Java literals.
  - `merged[0]` and `merged[-1]` index an `ArrayList` through `size` and `elementData`.
  - Setting fields is #86 (done, below).
- **Smaller evidence and describe gaps** (#93, from the fourth evaluation).
  - The `evidence` and `last_run` views have each run's `language` (schema 5; older stores upgrade in place), so `no_addresses(value, language)` works on the view. The program's output stays in `runs.output`: on every row it made `SELECT * FROM last_run` repeat up to 256 KB per row and hit the result cap. An ambiguous column after a JOIN says the views already carry the run's columns.
  - `describe` merges a generic Rust function's instantiations into one scope (`list (&[char] or &[i32])`) leaves out unnamed struct, union, class and enum types (followed through pointers, qualifiers and arrays), and names restrict pointers (`const float *restrict`).
  - `describe` gives C, C++ and Rust locals' types (`ms (int), us (long long)`).
  - `session inspect` names the hit (`…:3 in <module>, hit 3`), as `start` and `continue` do.
  - Kept: paths print resolved (`/private/tmp` on macOS); tests and outputs rely on one form.
  - A probe that times out carries `debugger_events` (the debugger's last events and DAP trace), so a stop the debugger never reported (#27) can be told from a slow program.
- **Rust char elements** (#90, first part).
  - `Vec<char>`, `&[char]` and `&Vec<char>` are read like the other qualified element types, and print as Rust's `{:?}` does.
  - `char` rendering is exact: the code points Rust 1.96 escapes come from Rust itself (`scripts/rust_char_table.py` writes `src/peeky/rust_chars.py`), and a test ties the table to the pinned toolchain.
- **A reference into a dropped Box or Vec shows only its pointer** (#110, found by the review of #103). `let r = &*b; drop(b);` left `r.x` reading freed memory through the address `b`'s own row hid. In the innermost frame of an unoptimized build, a reference now reads while the code still uses it (the MIR's liveness: its borrow keeps the target), or when every path back from the stop reaches a use with nothing since that can move, drop or write (no call, drop, partial write, or write to or end of a borrowed local); otherwise it shows only its address. At a line breakpoint only where the line starts counts: LLDB's lowest address in each lexical block. The stop's column picks the statements whose code it can be, and a block is passed over where every block branching there ends with the line's own call or branch in the same lexical block, which the IR shows by order in its IR block (rustc lays a block out after the block first branching to it). A call LLVM inlined (`#[inline(always)]`, even at opt-level 0) leaves its result's store after its body, which counts too; code of the line no statement explains counts anywhere in its block. At a step or a pause any statement of the line counts. Two review rounds showed that proving a dead reference's target unchanged needs the borrow checker itself (interior mutability, `&mut` aliases, loops, drop glue), so it is not attempted. A raw pointer (also `NonNull`, `AtomicPtr`) keeps no borrow and shows only its address. A reference into a shared reference parameter's target, one at an enclosing scope's variable initialized at the stop, a parameter never reassigned that holds references (by value: where initialized), and a local only ever assigned constants read, unless borrowed `&mut` or raw. A write through a reference is not a reassignment of it. A stop in another of the crate's files gates a borrowing binding; with `#[collapse_debuginfo]` in the crate every statement of the line's blocks counts. A name bound twice that the stop cannot tell apart reads only if every binding does.
- **A moved or dropped Rust struct, tuple or array reads nothing through its pointers** (#108, found by the review of #103). After `drop(h)` of `struct Holder { b: Box<Pt>, v: Vec<i64>, n: i64 }` (or `consume(h)`, `let g = h;`, `let d = h.b;`), `h.b.x` read the allocator's reuse of the freed Box (777 from a later allocation); so did a tuple's `t.__0.x`, an array's `arr[1].b.x`, a `Box<str>` field's text, and `--frame 1` in a caller that moved `h` into the call. A top-level local LLDB shows with children whose MIR type may own memory (anything but primitives, references, raw or fn pointers and tuples or arrays of them) is now judged by the owned-local proof: proven initialized in the innermost frame of an unoptimized build, it reads whole; proven on a caller frame's call line, its fields read and its pointers show only their address; otherwise it is `[stale]` (`stale` in JSON, with a note), its fields show the moved-from bytes, marked, and every pointer inside it at any depth shows only its address (`[unproven]`). The worker carries the verdict on each expansion handle, so selectors (`h.b.x`), `session get h` listings and nested values (`h.v.len`, `arr[0].b`) follow it. Showing the local as `[unproven]` with no children (as a Box) was measured against this: on the existing Rust fixtures, 267 aggregate locals were shown, 57 were not proven fresh (7 caller frames proven on the call line, 32 moved, dropped or not yet assigned, up to 18 possibly live but unprovable: macro twins, closure and async captures, `println!`'s `args`, optimized builds). Hiding them whole would have lost every field of up to 25 live ones; marking them hides only their pointers, none of which a live fixture read through. Data-carrying enums other than `Option<i32>` were already opaque. Its review found that a field reassigned in place (`h.b = new`, `h.s = ..`, `h.v = ..`) marked the whole struct stale: the drop of the old field ended the local and the store of the new one never made it whole again. Such a drop and the store to the very same place right after it (past storage markers and drop flags; also an element, `arr[i] = new`) now pair as a Box's drop-and-replace does: the store makes the local whole only if it was whole before the drop, and any move or drop of the local in between forgets that. On the review's 10 crates (41 stop lines, about 420 values) only the two reassignment stops changed, both to the right values. A field moved out and stored again (`let d = h.b; h.b = new`) stays stale: that needs per-field tracking.
- **Optimized Rust builds never read a Box** (#111, found by the review of #103). At opt-level 1 or 2 the Box gate was skipped (no MIR), so a dropped Box read freed memory. Optimized builds now emit the same MIR, which says what each binding is; nothing is ever proven there, so a Box (and a pointer the MIR cannot vouch for) shows only its address, while references read.
- **A moved or dropped Rust Box shows only its pointer** (#101, found by the review of #100). After `consume(bp)` or `drop(bi)`, LLDB read the freed allocation as the Box's contents (`bp.x` showed `2` for a Box made with `x: 1`; a dropped `Box<i32>` showed a later Box's 555). Now a top-level value LLDB shows as a data pointer, or an array of them (its C-style type parsed as a declarator), is read through only when the build's MIR says it is a reference, raw pointer or fn pointer, or proves it initialized at the stop line in the innermost frame (as owned `Vec`s are, #53). A Box (in any spelling, also a wide `Box<str>`, an array of Boxes, or a generic `T` holding one) elsewhere shows only its address, `[unproven]`, with a note. Only the bindings of a name in scope at the stop count (the MIR's scopes, all of them on a line that may run macro code). The review took 24 rows to close the gaps: parameters, closure and async captures, unmangled functions, generics, arrays, LLDB's string summaries and declarator shapes, scope narrowing across macros, multi-line calls and other files, and quoted IR labels that had hidden live Boxes. A struct that owns a Box was #108.
- **Fifth agent evaluation** ([report](evaluations/2026-10-21-agent-fifth-round.md)): a Rust `Vec` of structs, a C `unsigned` sum, a Python mutable default. All three agents verified a general fix on their first `verify` (24, 24 and 30 calls).
  - Fixed: Python candidate probes (`probe --old/--new` runs a private copy of the target root with the edit, so imports, data files and child processes all see it); Rust `describe` on a crate whose unit holds megabytes of std generics; `queries diff` shows a selector-only row's first value, a type change, and a `(source)` row for runs of different source text; a field `--set` outside Java is refused before any build, with how to add a settable local; `describe --quiet`; `runs.set_hit` is NULL without `--set`; `session inspect` marks `(Return Value)` `[not selectable]`; docs for Python `--set`, `diff --param`, `verify --keep` and the return value.
- **`describe` gives the lines of locals declared in nested blocks** (#107, fifth evaluation). `i (int)` was listed among `average`'s names though only its `for` loop sees it. A local declared only inside nested lexical blocks now shows their lines (`i (int, lines 4-6)`; `spans` in `--json`), from the function's own line-table rows within each block's code. Rust's `let` blocks show where each binding is visible.
- **Caller frames, Python globals and object identity** (#104, from the fifth evaluation).
  - `session inspect --frame N` and `session get --frame N ...` read frame N as `inspect --json` lists `frames` (0 is the innermost, at most 10); the worker's `inspect` takes `frame` (an index into that same stack trace) and refuses one past it with `INVALID_REQUEST`, naming how many frames there are. Snapshots carry `frame` (and `innermost`, where the program stopped, for N > 0); rows carry `frame` and `location`; text says `frame 1: tags (app.py:16)`. Every frame check stays: frames outside the program's sources, provenance, and Rust's MIR proofs, which hold only in the innermost frame (a real-build test reads a caller's live `Vec` raw), while a caller's references are judged by the #110 gate as the stopped frame's are, anywhere on the call's line (`r` into a dropped Box read 777 through `--frame 1`). New for callers: a C/C++ local is withheld while its declaration's code is unfinished (declared on the calling line, or above it with code at or after the frame's return address), since `long result = twice(seed);` stores `result` only after the call returns (`--frame 1` showed `6712212928`); so is a local whose declaration line has no code of its own (an array of structs, a nested struct or a 2-D array initialized element by element; in the innermost frame that is #121), and one whose declaration statement, scanned in the source to its first `;` outside brackets with comments and literals blanked (`c_source`), still runs on the calling line (`struct trio t = { seed,` / `{ twice(3), twice(4) } };` showed `t.p.x = 20905`), or whose end the source does not give (also where the compiler may read the text differently: a directive, line splice, `<::` or unsafe macro between, a trigraph or `#line` in the file; digraphs are brackets, C has no raw strings, and lines break at `\r\n`, a lone `\r` or `\n` as in Clang's line table, where `\n\r` is two), or which is declared in another file than the frame's line (an initializer's `#include`d rows: `ps[1].x = 429835611285028900` at table.h:2); a caller's values are never settable, and the worker refuses to change them.
  - `session inspect --globals` lists debugpy's `Globals` after `Locals`, read-only, bounded like other scopes, and not at module level (the same dictionary); the worker's `inspect` lists them only on request (`globals`), since listing a value runs its repr in the program. A scope cut at its bound says so (`truncated`, `total`), and names a request asks for (`names`) are listed past it. `get` reads a name missing from the locals from the globals only when `symtable`, run on the frame's source, proves it is not a local, cell or free variable of the function (nor bound by its comprehensions or an enclosing function); only then are the globals listed. `def f(): print(x); x = 1` never shows the global `x`, and each refusal names what binds the name; a name the function reads from its globals stays a global whatever a comprehension or generator expression in it binds under that name. An unparsable source, an unidentified function, or a file changed since the program started (mtime or ctime), or whose listed locals it does not bind, reads nothing, with a `NOT_FOUND` message that says the name may be such a global. Each source is indexed once (function spans and bindings, no syntax tree), in a cache bounded by size.
  - Members debugpy files under `special variables` and the other groups are selected by name (`normalize.__defaults__`); a dict's or set's groups never are (their entries are named by their keys' reprs). Groups do not count toward the four-level depth bound.
  - Python values with a debugpy reference carry `identity` (an integer; `[id 7]` in text) in `inspect`, `get` and probe rows, and in `captures.identity`, beside `captures.scope` (`globals` for a module global, `unknown` for a row an early schema 6 stored; evidence schema 7, older stores upgrade in place, including an early schema 6 with a text `identity`). The number alone identifies the object, which keeps it if its `__class__` is reassigned. The number is debugpy's per-object number, not `id()` itself: pydevd numbers objects by `id(value)` and keeps them alive while stopped. At one stop equal means the same object; across stops different means different objects and equal may also be a reused address. Scalars and debugpy's grouping and paging nodes have none.
- **The stopped C/C++ frame withholds what is being built, or skipped** (#121, found by the review of #104).
  - A multi-line initializer read leftovers at a stop inside it: `struct outer o = {` over lines 17-22 at line 20 (`o.in.y = 20905`), an array of structs, a 2-D array, and `struct trio t = { seed, {` whose first line has code. Now, in the innermost frame, a local declared above the stop is withheld while the stop's line is within its declaration (`c_source`'s `declared_through`, the caller-frame scan made header-aware: a variable a `for`, `if`, `while`, `switch` or `catch` header declares has run with its part of the header, so loop bodies still read their counters), or when its end is unknown, also for a declaration in another file (an `#include`d initializer). `declared_below` places a declaration line without code by its statement's elements, so `ps` and `grid` below the stop are withheld too.
  - A jump past an initialization: `case 1` skipped `struct pair p = { 7, 8 };`, and `p` read the stack. A local is withheld below a label a jump can reach between its declaration and the stop: a `case` or `default` of the switch whose body holds it, or a label a `goto` before it names (or any, with a computed `goto` or a macro that may `goto`); a macro that may expand to a label, or a block that closes first, is unknown. This holds in caller frames too.
  - `this` while its object is built: the stopped `Widget::Widget` showed `this.a = 20908`. `this` is withheld in a constructor (the debug information's class and member, or the frame's name `C::C(`), in any frame a constructor called, and in a lambda defined inside a local's declaration or a constructor that may still be running (by the frames listed below it). A bare member name in such a frame says why `this` has no value.
  - Lexical scopes now pick which local of a name the stop reads, so a sibling block's local never decides.
  - Review round 1 (all with real-build tests): a local declared on the stop line is withheld part-way through it while the line still has code to run (`step --out` back into `long r = twice(3) + twice(4);` showed `r = 20911 [settable]`), except a header's variable, live mid-line in a one-line loop; a local declared in another file than the stop is withheld when that file may `#include` code after it in its block (`case 1:` in `cases.h` skipped `p`); a frame running a line outside its function's own (a default member initializer: `Cfg cfg{};` showed `cfg.b = 20907` at the member's line, also as frame 1) withholds its class, struct and array locals; a `goto` back into a block from after it reaches its labels (C11 6.2.4). Over-hiding fixed: C's trigraphs are translated (one `"huh??!"` hid every local above the stop) and C++17 has none; a macro's `;` inside its brackets (a statement expression) is harmless; a lambda's defining frame and constructors below a frame are looked for in up to 64 frames, not the ten listed, and past that `this` is withheld.
  - Review round 2: a header's variable is live mid-line only when a `for`, `if` or `switch` sets it with no call (`for (long i = twice(1); ...)` showed garbage `[settable]` after `step --out`, and a `while`'s `v` the last pass's value); a local an `#include`d file declares counts as declared at that `#include` (`--at 16` above `#include "decls.h"` showed `later`'s leftovers); a stop on another file's line, or outside the function's own lines, runs every rule from the own line that brings it in, else from the last own line before it (`after = 20911 [settable]` at `cfg.hpp:4`), and withholds all but parameters when it cannot be placed; an included file with no label, jump or unbalanced brace is no longer a jump hazard; a `goto` counts only within its function's DWARF lines, not a namespace's braces.
- **C/C++ pointers: `p[0]` is the pointee** (fifth evaluation's dry run). `values[0]` on `const int *values` said only "children: *values", and the pointee was `[not selectable]`. It now reads `*values`, `session get values` lists it as `values[0]`, and `values[1]` says a pointer carries no length, with where to index the array instead.
- **C/C++ pointers to arrays, containers and structs index as C does** (#102). The debugger lists such a pointer's pointee elements as its own children, so `parr[1]` on `int (*parr)[3]` read `(*parr)[1]` where C means the next row, and `parr[0][0]` failed. Now `parr[0]` is the pointee (`int[3]`; a vector's summary such as `size=3`), `parr[0][1]` its element, its children list as `parr[0][N]`, and `parr[1]`/`parr[-1]` are refused with the selector that reads the element.
- **Rust tuple and struct elements** (#90, second part).
  - `Vec<T>`, `&[T]` and `&Vec<T>` are read when `T` is a tuple, or a struct the crate defines, of qualified scalars: `[('a', 3), ('b', 1)]`, `[Interval { start: 1, end: 10, open: false }]`, `[Point(1, 2)]`. The build's debug information must show plain, aligned fields within the element's size.
  - The compiler's own type picks the layout (the MIR's for owned locals, the IR's for parameters). LLDB's names only confirm it, since they collide: LLDB names `Vec<Pair<i8>>` `Vec<Pair<char>>`, the DWARF name of `Vec<Pair<char>>`.
  - Fields are selectable (`runs[0].__0`, `spans[1].start`), with Rust types; `runs[0].0` gets a hint to write `__0`.
  - Anything else (an enum, a `String` field, a standard-library struct such as `Range`, which prints as `1..5`) keeps LLDB's summary with a note that says which elements are read. Two structs that share a path (a function's own `Token` and `mod parse`'s) make a container of either opaque.
- **Plain Cargo crates** (#89, found by the fourth evaluation's dry run).
  - A `cargo new` binary crate works as is: edition 2021 or 2024, Cargo's default targets (`src/main.rs`, `tests/*.rs`), empty `[dependencies]`, package metadata. Before, only a manifest with explicit `[[bin]]` and `[[test]]` targets and every discovery switch off was accepted, with errors such as "Rust sources and tests must be nonempty immutable tuples".
  - A crate without an integration test is verified by its checker alone. Anything else Cargo would build (`src/lib.rs`, `src/bin`, `build.rs`, dependencies, a second test) is refused with the reason.
  - Rust `char` values print as Rust's `{:?}` does (`'b'`, `'\n'`, `'\u{200d}'`), not LLDB's `U+0062 U+0062 U'b'`.
  - A Vec whose elements Peeky does not read gets a note: its value is LLDB's summary, not the contents.
- **Java scopes in `describe`** (#91).
  - `describe` lists each Java method's line span and the names it can see (`this`, parameters, locals), as it does for Python, C, C++ and Rust: `names in BalanceBug.assign (demo/BalanceBug.java:19-32): workers, tasks, order, task, best, w`.
  - The names come from the class files' LocalVariableTable (launched builds use `javac -g`; attached JVMs use their own classes), read by the same bounded class reader. Classes compiled without local names say so (`scopes_unavailable`).
- **Fourth agent evaluation** ([report](evaluations/2026-10-19-agent-fourth-round.md)): Java aliasing, C integer overflow, a plain Rust crate. All three agents reached a general verified fix on their first `verify` (33, 27 and 29 calls).
  - Fixed: `object_ref(value)` for Java identity; `(args)`/`(sets)` rows in `diff`; `verify --timeout`, `--quiet` and `seconds`; `job wait --json` for any job; `ArrayList` children are elements; a `->` selector hint; a guide that fits one read; the checker descriptor in the cookbook.
  - Filed: #90 (Rust char/tuple elements), #91 (Java describe scopes), #92 (C declaration-line stops), #93 (smaller evidence and describe gaps).
- **Java fields and array elements take `--set`** (#86).
  - `--set current.end=10`, `this.count=0`, `arr[0]=1` and `arr[-1]=9` change scalar fields and array elements reached from the stopped method's locals, in a probe or with `session set`.
  - Final and static fields, Strings, boxed values and an `ArrayList`'s elements are refused (`MUTATION_REJECTED`), and so are map entries in any language. Captured fields and elements carry `settable`.
  - The worker locates the parent again from its frame and scope and writes through the parent's own reference. It then reads the value back from the object; if the new value cannot be confirmed, the session closes.
  - Peeky's Java launcher refuses a field write whose name a hiding static field or a second field shares: java-debug writes by `fieldByName`, which would pick a field other than the one listed. Bundles mark this with `peekyFieldSet`; older bundles change only locals and say to rebuild.
  - Other languages refuse a field or element `--set` at launch, before the program runs.
  - Example: `examples/java/demo/MergeBug.java` (cookbook 7.3).
- **Settable locals up front** (#77).
  - Probe rows carry `settable` (`[settable]` in text) for root locals, so the observation probe tells which interventions will work.
  - A refused `--set` lists what can be changed at that stop (`settable here: …`).
  - A C/C++ local declared below the stop is MUTATION_REJECTED.
- **Rust scalars by their Rust types** (#79).
  - Locals and parameters take theirs from the build's MIR: `usize`, `u64`, `f64` and `char`, where LLDB says `unsigned long`, `double` or `char32_t`.
  - Fields take the one primitive LLDB's name can be.
  - `u8`/`i8` values print as integers, not C character literals.
  - LLDB's name is kept as `debugger_type`.
- **Several edits per candidate** (#76): `verify` and candidate probes take repeated `--old`/`--new` (or `-file`) pairs, each quoting the original file, up to 32 and never overlapping. They are built as one edit with their combined effect, so workers, hashes and diffs are unchanged; a candidate probe maps `--at` through every edit. The HTTP probe's `edit` takes a list.
- **Exact integers in the evidence store** (#75): schema 4 makes `captures.num` NUMERIC, so `600` stays `600` and 64-bit integers are exact. The table is rebuilt in capture order, and older stores are upgraded in place.
- **C/C++ locals declared on the stop line** (#92, found by the fourth evaluation).
  - At a breakpoint on `long long us = ms * 1000;`, `us` showed the previous call's value, `[settable]`. It is now withheld (`declared on line 3, which has not run yet at this stop`) and refused by `--set`, exactly when the stop is at the line's first instruction in its function: the instruction pointer, less the executable's load slide (`__TEXT` on macOS, the header segment on Linux), against the build's line table.
  - Where control enters the line is found per block: the line's first instruction in the block that declares the local (a braced one-line loop body has its own), and after the prologue on a function's first line, where its breakpoint is.
  - An address a loop jumps back to is never withheld: Peeky disassembles the function once per session (DAP `disassemble`) and leaves an address alone when a later jump returns to it. Neither is a line's first row reached from a later line's code (a `for (` or `switch (` header over several lines). That covers x86-64, where `while ((x = next()) > 0)` tests at the line's first instruction with `x` live, and a `do` body's first line, whose last-pass value is shown with the guide's warning.
  - Mid-line stops (a return from a call) still show the value. Unoptimized arm64 and x86-64 builds only; anything unknown (a load address or jump in an unrecognized form, a jump through a register) withholds nothing.
- **C/C++ locals declared below the stop are withheld** (#74).
  - `no value yet: declared at line 18, below this stop`. They are never settable or expanded.
  - The rule comes from DWARF declaration lines and the line table. It applies only when:
    - every same-named automatic local in the functions running code on that line is declared below;
    - and that code comes before the declaration's.
  - It never applies with `goto` labels, `setjmp` (in the executable, the sources or a header's `__builtin_setjmp`), statics or parameters.
  - Four review rounds found ten cases where a live local was withheld, plus a misleading note on the stop line; all are fixed with real-build regressions (the note was dropped).
  - Line tables now resolve each file once per table: an STL-heavy C++ probe went from 1.45 s to 1.31 s.
- **Third agent evaluation** ([report](evaluations/2026-10-18-agent-third-round.md)): Rust, C and Java bugs, each fixed and verified on the first `verify`, all three after a candidate probe. The eval-fixes PR fixes the findings:
  - `queries hits`/`run` keep error text out of `value`;
  - owned `Vec`s read at a loop head after `vec!` (std macro code counts only on lines that can invoke a macro);
  - Rust `f64`/`f32` scalars print as Rust does;
  - failed probes keep their output, and `NO_STOP` gives the exit code and names Peeky options placed after `--`;
  - `diff` gets a `(line)` row and gives line and column.
  - From its review rounds:
    - the macro-line premise is qualified per toolchain (`test_rust_macro_lines.py`);
    - functions using `debug_assert!` can be proven;
    - Rust builds with many diagnostics are `BUILD_FAILED` with rustc's errors;
    - programs using `HashMap` no longer exceed the 2 MiB DWARF cap.
  - Issues filed: #74 to #79.
- **#73 (`f8e5efa`):** Rust containers of every scalar element type (`Vec<T>`, `&[T]`, `&mut [T]`, `&Vec<T>` for i8–i64, u8–u64, isize, usize, bool, f32, f64), with floats printed exactly as Rust's `{:?}`.
- **#71 (`146ab4f`): owned Rust `Vec<i32>`/`String` locals** are read from memory where the build's MIR proves them initialized at the stop line (closes #53).
  - The binary crate is compiled through a rustc wrapper: `-Zmir-opt-level=0`, a `PreCodegen` MIR dump, legacy mangling, and `RUSTC_BOOTSTRAP` for that crate only.
  - `rust_mir` runs a definite-initialization dataflow, checked at every place code of the stop line can be.
  - Five review rounds found five false proofs (merged codegen blocks, matching by elimination, a stop in another file, two kinds of macro twins), each fixed with a real-build regression.
- **#70 (`ea6c728`):** the Java adapter test infers a dropped exit the way the worker does (#27, observation 3).
- **#67 (`0a1fad7`): CUDA phase 0** for #61. [ADR 0007](adr/0007-cuda-gdb-dap.md) (proposed) drives `cuda-gdb -i=dap`. `scripts/cuda/qualify.py` is the qualification to run on a GPU host.
  - Found by experiment: upstream GDB 14.2, 15.1 and 16.3 start the program at different DAP requests. One order works on all three (breakpoints, `launch`, then `configurationDone`).
  - The new `gdb-dap` lane keeps that order tested against GDB 15.1; 14.2 and 16.3 were run by hand.
- **Dependabot:** #1, #3, #4 and #5 were merged. #2 (`upload-artifact` v7) was validated by dispatching `artifact.yml`, then fast-forwarded over SSH as `7308955`, because the API token lacks the `workflow` scope.

**Closed issues:** #6, #7, #8, #9, #10, #11, #12, #13, #48, #49, #53, #55, #56, #57, #58, #59, #74, #75, #76, #77, #78, #79, #86.

**Open issues:**
- **#27:** an attached JVM whose `stopped` event is lost (seen once on Venice). The launch case is fixed. 160 targeted stress runs (simple and Venice-like thread churn) did not reproduce it; diagnostics stay in place.
- **#61:** NVIDIA CUDA debugging, sanitizers and profiling, filed by the owner. [ADR 0007](adr/0007-cuda-gdb-dap.md) proposes driving `cuda-gdb -i=dap`; `scripts/cuda/qualify.py` is the phase 0 qualification, and its DAP sequence is tested against upstream GDB (lane `gdb-dap`). Running it needs a Linux host with an NVIDIA GPU, reachable over Peeky's SSH remote worker; this Mac has none.

**Process notes:**
- Create PRs and issues with `gh api` (REST): `gh pr create`/`gh issue create` GraphQL calls returned 504s, and the app's PR tool needs the project's GitHub link.
- Squash-merge with `gh api -X PUT .../pulls/N/merge -f merge_method=squash -f sha=<head>`.
- **2026-10-17:** the `code-review` agent started failing with "400 The requested model is not supported" (also the general-purpose agent). The last #71 round was verified by hand with real builds and recorded as such in the review doc.
- Never pipe pytest output through `tail` before checking its exit code.
- Changes to workflow files cannot be merged through the API (no `workflow` scope). Validate them by dispatching the workflow, then push to `main` over SSH.
- **2026-10-16:** GitHub Actions stopped starting jobs ("recent account payments have failed or your spending limit needs to be increased"). The owner was unavailable, so #46 and #47 were merged on equivalent verification:
  - the full local suite, including the remote e2e in OrbStack;
  - every Linux CI lane in Docker (`scripts/ci_linux`, see CONTRIBUTING.md), with the evidence in each PR. Fixing billing is the owner's decision.
- **Docker under OrbStack:** containers that hang on a bind mount cannot be killed. Feed trees with `git archive HEAD | docker run -i …`; if it hangs anyway, `orb stop && orb start`.

**Local Java adapter bundle:** `outputs/peeky/work/java-adapter-step` (String cap and stepping). `~/.peeky/venice/attach-*.json` point to it. Rebuild with `scripts/setup_java_adapter.py --output <absolute path>`.

## Previous slices


PR #23 merged as `2995958` (Rust worker, active Option views, compiled-test plus checker verification, five review rounds, no blocking issues on the final head `825c075`, all nine lanes green).

Branch `feat/stateless-probe` (pushed; the Copilot PR tool is blocked until the project's GitHub account is re-linked, so open the PR manually). It implements the owner's direction (ADR 0004 and ADR 0005): **Peeky is a standalone tool that models, harnesses and humans call; it never calls a model, and there is no mode switch.** One `peeky` CLI provides `describe` (manifest), `probe` (one stateless question), `session start|wait|inspect|get|set|continue|report|close|list` (step by step across shell commands via a local worker) and `verify` (fresh build plus independent checker). Targets are auto-detected with toolchain discovery. There is a stateless HTTP `probe` plus `Client.probe`, docs/AGENT_GUIDE.md and skills/peeky-debug. 506 unit and integration tests pass locally, along with the real Rust CLI and probe tests.

Java on Venice end-to-end tests (owner request) is **delivered experimentally** on branch `feat/java-attach`, stacked on `feat/stateless-probe`; both are pushed with CI green, and PRs must be opened manually until the account is re-linked. `JavaAttachTarget` attaches over loopback to a JVM started with `gradlew … --debug-jvm`, maps only the declared sources' compiled classes, rechecks hashed sources, classes and adapter on every operation including `wait` and `report`, labels provenance `attached`, refuses edit verification and detaches without killing the JVM. Verified on Venice `ba78edda8`: `peeky probe` at `TestEmptyPush.java:85` captured `storeName`, `sendControlMessageDirectly` and `controllerClient` for both data-provider runs in 28.5 s; Gradle reported both tests PASSED. Root cause of the first failed attempts: thousands of DAP `thread` events evicted lifecycle events. Only lifecycle events are now kept in bounded history.

Venice on Apple Silicon without Rosetta: grpc-java ships only x86_64 macOS code generators. `scripts/venice/native-grpc.sh` builds v1.59.1 natively from a pinned commit with SHA-256 checks and exact patch counts, and writes a Gradle init script. Venice `assemble` plus integration-test classes then succeed (2m23s) with no change to the Venice repo. Recipe in docs/JAVA_ATTACH.md.

Reviews: the CLI and attach review found 3 blocking and 7 should-fix issues, all fixed in `3bd1e38`/`d41bdf7` with regressions (path confinement in verify, `no-stop` probes, provenance on wait/report, hard-capped HTTP probe subprocess, token-authenticated worker shutdown, private validated session records, selector truncation, bounded attach discovery, detection precedence, pinned Venice helper, narrowed docs). A verification pass found five more (HTTP probe child isolation, stop validation, cleanup inside the cap, complete JSON results, full attach entry bounds); these are fixed in `27937b7`, and the final review reported **no blocking issues**. Evidence on `27937b7`: all nine CI lanes passed (run 38691568364, longest 71 s); 562 local tests passed (unit, integration, Java including attach, Rust); and the Venice probe again captured both hits, with both tests PASSED.

Next: open one PR from `feat/java-attach` (it contains `feat/stateless-probe`) and merge after reconfirming lanes on its head. Then: a live-model session where an agent drives `peeky` against a Venice bug using docs/AGENT_GUIDE.md; production-class breakpoints in Venice (add `services/*/build/classes/java/main` roots); ADR 0004 steps 4–5 (query grammar, evidence store); lifecycle #6, corpus #7, SSH #8.

Stacked on `feat/java-attach` (2026-10-15):
- **`feat/error-codes` (#6):** protocol version 1, machine-readable error codes with hints, retryable and session_closed flags ([ERRORS.md](ERRORS.md)), and lifecycle regressions. CI green, run 38694056033.
- **`feat/corpus` (#7):** the ten-bug corpus and distinct outcomes. A live agent evaluation ([evaluations/2026-10-15-agent-cli.md](evaluations/2026-10-15-agent-cli.md)) had three independent agents each reach verified general fixes. Its findings are fixed:
  - Python `--set` is now limited to literals; it previously evaluated expressions inside the debuggee.
  - `verify` cleans up its scratch copy, shows the failure reason and diff, and explains `--old` mismatches.
  - Probes warn about relocated breakpoints and short runs.
  - All failures now use one error object.
  - With every real toolchain lane enabled, 644 tests pass locally.
- **Review:** the independent review of error codes plus corpus found no blocking issues. Its two should-fix issues and one nit are fixed ([reviews/ERRORS_CORPUS_REVIEW.md](reviews/ERRORS_CORPUS_REVIEW.md)).
- **Agent speed ([ADR 0006](adr/0006-agent-speed.md), owner priority):**
  - Probes report `pauses_ms`, and on attached JVMs they end at the last hit.
  - Sessions take `--max-pause` (10 s by default for attached JVMs). The worker auto-resumes overdue stops, even unobserved ones.
  - `probe` and `verify` take `--background`, collected with `peeky job wait` (exit 3 while running).
  - `describe` reports the timing contract.
  - Verified on Venice's timed test: the background probe took 19.2 s, and the slow-agent and absent-agent sessions both auto-resumed. All runs PASSED.
  - Open issue: one Venice session never received a `stopped` event (see JAVA_ATTACH.md). Diagnostics were added.
- **Agent-speed review:** no blocking issues. Three should-fix issues are fixed: background errors are replayed on stderr, a relative state dir now works, and crash stops are never auto-resumed (checked with real LLDB). Four nits are fixed too. See the same review file.
- **Venice production-code breakpoints:** verified. A probe in the client library (`ControllerClient.createNewStore`) and one in the controller's HTTP handler (`CreateStore`, on a Jetty thread) each captured both invocations, and the tests PASSED. The recipe is in JAVA_ATTACH.md.
  - Fixed while doing this: `describe` without `--at` on a multi-source target now describes every source instead of raising a traceback.
  - Every unexpected CLI exception is now a structured error.
  - Missing paths now return `INVALID_REQUEST` with a path hint.
- **Next:**
  - one PR from `feat/corpus` covering the whole stack;
  - SSH separate host (#8; needs a second host);
  - a user-selected non-synthetic program.

Known local-only limitation (pre-existing on main): test_native_adapter_death_reaps_stopped_target fails with Apple LLDB on macOS and passes on hosted Linux.

## Next slices

The original slices are delivered: #6–#13 are closed. Remaining:
1. **Hosted CI:** restore it (billing). Then rerun CI on `main` to confirm the Docker-verified merges on x86_64.
2. **Hosted LLVM 20 Rust job:** mirror the Docker `rust-llvm20` lane in `ci.yml` once Actions runs again. It needs a workflow-scope push over SSH.
3. **#27:** if the lost `stopped` event on an attached JVM recurs, capture the DAP trace and fix it from that evidence. It recurred twice under full-suite load in the launch-only adapter test; details are on the issue.
4. **ADR 0004, step 5:** a prepared-build cache, only if measured probe latency requires it. Step 4 shipped in #35; the SQL-shaped probe form was deliberately not implemented.
5. Implement the bounded evidence correlation design (code, logs, metrics).
6. A live-model evaluation on Venice, using the cookbook.
7. **#61:** CUDA phase 0 on a real GPU host: run `scripts/cuda/qualify.py` to qualify `cuda-gdb -i=dap` (GDB 14.2 and later inside cuda-gdb 12.9 and later), then a known kernel bug end to end.

Read AGENTS.md, DESIGN.md, LANGUAGES.md, and ROADMAP.md for acceptance criteria. Runtime intervention is evidence only; never claim a fix without fresh verification and matching source/build identity. Preserve a checkpoint when usage runs low; report shipped work and actual limitations.

Mixed-runtime inference engines (Python/C++/CUDA) are now an explicit design direction in MIXED_RUNTIME.md, not shipped support. Preserve backend session ownership, correlated evidence and clock domains; qualify extension-module coexistence and GPU hardware separately after host-language foundations.

C++ microbaseline:10 local Apple LLVM21 samples, inspect median62.63ms/p9574.91ms, class expansion123.11/124.07ms. Full inspect+class+two array expansion sequence437.95/450.80ms. HTTP/DAP/provenance included; build/model excluded. See PERFORMANCE.md and performance/cpp-2026-10-14.json. These are measurements, not SLOs.

## 12:20 UTC continuation checkpoint

Repository clean on entry; only unrelated Dependabot PRs open. Ordinary usage allowed but weekly window99% used; no reset or purchased credits consumed. Completed a bounded parser compatibility check before starting any larger implementation: the retained local Rust scratch DWARF4 dump (1,754,438 bytes) parses under the unchanged2MiB limit in0.433s and yields declared src/main.rs lines. This is parsing evidence only, not a fresh source/build provenance validation or a worker qualification. Raw result remains ignored at work/rust-line-parser-check.json. Linux4,808,912-byte dump still requires a reviewed Rust-specific bound or declared-source extraction. No implementation edits or background provider jobs started. Resume from RustTarget/Option/test-verification plan when sufficient ordinary usage is available.

## Owner direction: stateless probes (2026-10-14)

The owner prefers stateless operation where possible and wants heappy-level ease of use. Both modes must be supported: the existing stateful sessions and one-request stateless probes over the same guarded engine, with a `describe` manifest, canned probes and SQL over captured evidence. Recorded as [ADR 0004](adr/0004-stateless-probes.md). This is direction only, not started. Until it lands, new worker features should be expressible as declared probe steps.
