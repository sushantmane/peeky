# Error codes (protocol 6)

Every Peeky failure carries the same error object: from the HTTP worker, from the SDK
(`peeky.errors.PeekyError`), and from the CLI. With `--json`, the CLI prints `{"error": {...}}`.
Without it, stderr shows `[CODE] message` and a hint. A probe result uses the same object in its
`error` field, with a `status` of `failed`, `no-stop` or `timeout`.

## The fields of an error

| Field | Meaning |
| --- | --- |
| `code` | A stable name to branch on. Messages may change; codes don't. |
| `hint` | What to do next. |
| `retryable` | Whether repeating the same request can succeed after the hinted step. |
| `session_closed` | The worker discarded the session, after a provenance, debugger or time failure. Start a new one. |
| `cleanup` | Present when the error ended a session: what became of the processes it started. See [What `cleanup` says](#what-cleanup-says). |

## What each code means

The last column is the `retryable` field.

| Code | What happened | What to do | Retryable |
| --- | --- | --- | --- |
| `STALE_STOP` | The program moved on: the `stop_id` is old, or `--max-pause` resumed the stop. | `wait` until stopped, then `inspect`. On timed programs, prefer a probe. | no |
| `INSPECT_REQUIRED` | A change came without a fresh scope, or a value-view read is still pending. | `inspect` again, and use the new reference. | yes |
| `STALE_HANDLE` | An expansion handle was revoked, or the frame or scope changed. | `inspect` again. | yes |
| `NOT_FOUND` | A name or selector isn't visible ([details](#not_found)). | Use a name from the message's `visible:` or `children:` list. | no |
| `MUTATION_REJECTED` | Peeky can't make that change there. Nothing was written. | Follow the hint: it names the one way around the rule ([the rules](#when-a-change-is-refused-mutation_rejected)). | no |
| `BUDGET_EXHAUSTED` | A bounded read limit was reached, or a probe's limit of signal stops in a row ([details](#budget_exhausted)). | Narrow the request. | no |
| `INVALID_BREAKPOINT` | The line isn't executable, or a breakpoint field isn't supported. | Use a line that `peeky describe` lists. | no |
| `NO_STOP` (probe result) | The program ended without reaching the line ([details](#no_stop)). | Pick a line that runs. | no |
| `PROGRAM_CRASHED` (probe result) | A C, C++ or Rust program crashed: a fault, a signal, or `abort` ([details](#program_crashed)). | Read its output, and probe a line before the crash. | no |
| `BUILD_FAILED` | The program didn't compile, or its build timed out ([details](#build_failed)). | Fix what the compiler reports, and run again. | no |
| `PROVENANCE_CHANGED` | Sources, build outputs or tools changed, or a remote worker's sources differ from yours ([details](#provenance_changed)). | Start a new session on a fresh build, or sync the file it names. | no |
| `TIMEOUT` | A time bound was hit, and the session was discarded ([details](#timeout)). | Start again. Never blindly retry a change. | no |
| `DEBUGGER_ERROR` | The adapter or debugger failed, and the session was discarded ([details](#debugger_error)). | Start again. | no |
| `SESSION_ACTIVE` | A session was misused: one is already open. | Close the session. | no |
| `NO_SESSION` | A session was misused: there is no such session. | Start a session. | no |
| `BUSY` | Another probe is running on this worker. | Retry later. | yes |
| `UNSUPPORTED` | An operation or capability isn't offered, or the setup can't be used ([details](#unsupported)). | Do what the message names instead, or fix the setup it names. | no |
| `UNAUTHORIZED` | The bearer token is missing or wrong. | Use the worker's `DEBUG_TOKEN`. | no |
| `INVALID_REQUEST` | The arguments are malformed, or the evidence store can't be used ([details](#invalid_request)). | Fix the request. | no |
| `INVALID_QUERY` | `peeky sql` or `peeky queries`: bad SQL, a write, a missing or unknown `--param`, an unknown `--schema` name, or a query over its time bound. | Fix the query. `peeky sql --schema` lists the tables and views, and `peeky queries --help` each canned query's parameters. | no |
| `INTERNAL_ERROR` | A Peeky bug: an unexpected exception type. | Report it with the message. Don't retry, and don't change your request. | no |
| `UNREACHABLE` | No connection reached the worker: the tunnel or the worker is down. Nothing was applied, and the session is kept. | Restore the tunnel or the worker, and retry. | yes |
| `TRANSPORT_LOST` | The connection broke mid-request. The outcome is unknown. | Reconnect, then `inspect` or `wait`. Never repeat a change blindly. | no |
| `JOB_LOST` | A `--background` job's process died before it finished. | Rerun the command. | no |

## Details by code

### NOT_FOUND

- Names match exactly as the debugger lists them. A name spelled in another Unicode
  normalization form is refused, and the message gives the listed spelling.
- In Python, a name missing from the locals is `NOT_FOUND` when Peeky can't prove that it is a
  global there.
- Before a probe runs, a `--set` name that no function at its line declares is `NOT_FOUND`. Peeky
  checks this for Rust, and for Java from its class files' local variable tables.
- To list names, use `inspect` or `session get <value>`.
- Dict entries are `d['key']`. C++ and Java members are `this.x`. A caller's locals need
  `--frame N`.

### NO_STOP

- The line never runs, or the program failed first. `exit_code` is set when the debugger reports
  it.
- After a failure, read the output and check the program's arguments.
- After `--set-at` changes, the message counts those that changed a value, and where they were:
  `The program finished without stopping at tides.c:33. The run made 6 changes by --set-at (at
  lines 32 and 35): compare with a run without them`. Nothing proves that a change came first, so
  it doesn't say that one kept the line from running. Probe the line without the changes to see
  whether it runs.
- A write with the same text, not proven to change nothing, is named too: `1 write by --set-at
  with the same text, not proven unchanged`. A Java String or a Python `1000`
  written alike may be a new object. Only writes proven to change nothing are left out.

### PROGRAM_CRASHED

- The program stopped on a fault or a signal, such as `abort` after an uncaught C++ exception or
  a failed `assert`. The probe ends there, with `status: failed`, and Peeky ends the program.
- It is a crash only for SIGABRT, SIGSEGV, SIGBUS, SIGILL or SIGFPE, where the program neither
  handles nor ignores the signal. At any other signal stop, the probe lets the program go on.
- The message says what the debugger says, such as `signal SIGABRT`, and where in your code the
  crash came from. `crash` in JSON has the same. There is no exit status.
- The output so far is kept. The C++ runtime and `assert` write the exception's type or the failed
  condition to it.
- See [C and C++ 5.1](lang/c-cpp-cookbook.md#when-the-program-crashes).

### BUDGET_EXHAUSTED

- A read went past one of Peeky's bounds, such as on the children it lists. Read a narrower
  selector.
- A probe ended at the 100th stop in a row on the same signal, or C++ throw or catch filter, at
  one place in a call from one line of your code. Peeky let the program go on after each of the
  first 99, and doesn't take them for a crash.
- The program may raise that signal again and again, or LLDB may stop there without letting it
  past. Peeky can't tell which. Probe a line before those stops, or use an input that doesn't
  raise them.

### BUILD_FAILED

- With `--old`/`--new`, it means that the edit doesn't build.
- The message has the compiler's output. Your files weren't changed.
- A probe's failed build is a refusal before launch: exit 2, `refused_before_launch`, and no run
  saved.
- `verify`'s failed build is a failed verify: exit 1.

### TIMEOUT

- `The debuggee did not start` means that the program never reached the debugger during the
  launch.
- Then check that the program can read its own path and every directory above it. A hung file
  system, or a pending macOS privacy prompt, blocks it. Then retry.

### DEBUGGER_ERROR

- The causes are:
  - a broken protocol stream, for example a reply over 4 MiB;
  - java-debug's event thread dying;
  - an attached JVM's stop that the debugger never reported (`Lost stop:`).
- If values are huge, capture narrower selectors.
- For Java, keep `debugger_events` for Peeky's issue #27. See
  [Two Java debugger failures](#two-java-debugger-failures).

### UNSUPPORTED

| Case | What to do |
| --- | --- |
| An operation or capability that isn't offered, such as `--capture return` for Python | Call `version` to see what is offered, or do what the message names |
| A Java adapter bundle built before a required java-debug patch | Rebuild the adapter with `scripts/setup_java_adapter.py` |
| A C++ target whose compiler is neither clang nor GCC: `C++ targets need clang++ or g++ …`, refused before building ([C and C++ 7.1](lang/c-cpp-cookbook.md#which-c-builds-peeky-supports)) | Set `PEEKY_NATIVE_CXX` to clang++ or g++ |
| Inline assembly that can read a file: `Inline assembly that can read a file …`, refused before building | Take the directive out of the inline assembly, and put the data in a declared source or header |
| A Python whose `sqlite3` module has SQLite older than 3.30, or one without the JSON functions, `json_each`, `json_tree` or window functions: `Evidence store needs SQLite 3.30 or newer …`, refused before the store is opened. The message names what is missing | Run Peeky with a Python built with a newer SQLite |

Inline assembly that can read a file means `.incbin`, `.include` or an assembler macro. This
applies to a C or C++ `asm`, and to a Rust `asm!`, `global_asm!` or `naked_asm!`. No dependency
file lists what it reads.

### INVALID_REQUEST

- Malformed arguments, such as a `--frame` past the stack. The message says how many frames there
  are.
- A `verify` of a directory whose config declares several sources, without `--at`. The message
  lists them, and says which one to pass: `verify needs --at <source> (one of: a/One.java,
  b/Two.java) for this target: pass --at <the source containing --old>, …`. With one source,
  `--at` isn't needed.
- An evidence store that can't be used. Move it aside, or pass `--store`.
- A client and a remote worker of different protocols: `Worker speaks protocol 5, this client 6`,
  or, from the worker, `Client speaks protocol 5, this worker 6`. Install the same Peeky version
  on both hosts.
- A request that sends the program's hash, `expect_sha256`, without the other sources' hashes,
  `expect_inputs`, or with an empty `expect_inputs`. Use the same Peeky version on both hosts.
- A Python directory that can't be hashed in full for a remote run: more than 4096 `.py` files or
  16 MiB, a file that isn't a readable regular file, or a symlinked directory. Probe another
  directory, or start the worker with another `--root`.
- A remote session started by an older Peeky, without source provenance. Start it again.
- `session inspect` of a Rust frame that the debugger shows inside a macro's definition. Peeky
  reads no value there, and the session goes on. `session step` reaches your code's next line.

### PROVENANCE_CHANGED

- A source, a build output or a tool changed during the session. Start a new session on a fresh
  build.
- Through a remote worker, a source differs from yours, or is on one side only. The message names
  the first such file:
  `Worker source provenance differs for helper.py (the copy on the worker host differs from your
  checkout)`. Copy the file over, or remove it, then run again
  ([REMOTE.md](REMOTE.md#source-identity)).
- A remote Python session whose sources changed on the worker host after launch is closed, and
  no values come back: `helper.py changed on the worker host after launch`.
- A remote Python session with a stack frame whose code names a file under the root that wasn't
  compared is closed the same way: `a stack frame is in hook.py`. A probe is discarded.
- A source in your own checkout that changed after the worker compared it: the probe is
  discarded, or the session closed and its record dropped.
- A remote result that the client can't confirm is discarded with this code:
  `results_discarded: true`, with no rows and no output. For example, the worker didn't report
  which sources the run read.

## When a change is refused (`MUTATION_REJECTED`)

A refused change writes nothing. The hint names the one action that the broken rule leaves you.
For example: stop at the statement's first line, change the value in its own frame, or pass a
value in range. Otherwise, observe the value, or edit the source and `verify`.

### What can change

Anything not listed here is refused:

- a root local of the stopped (innermost) frame: one of the stopped function's own variables;
- in Java, also a scalar field or array element that isn't final or static;
- in Rust, C and C++, also an integer field or element of a local that the frame holds itself. It
  must be reached without a pointer, reference or `Box`, and lie outside unions, enums, packed
  types and standard-library types.

In Java, Peeky never changes a field that a JDK class declares, that is, one in a JDK module:
`it is declared by java.lang.String, a JDK class`. Nor does it change an element of an array that
such a field holds, or of an array inside it, under any name:
`the array is held by java.lang.String.value, a JDK class's field`. Such an element is refused too
where more than 64 objects refer to it. So is one where the search for them takes longer than 2 s,
or longer than the pause budget. Rows run the same search before they mark an element
`[settable]`. Where the debugger itself refuses a write, the hint says that no mark could promise
it.

The value must be a literal in Python, and a scalar in a compiled language.

### Where a change can be made

A change is made only where no statement under way may hold a copy of the value. Elsewhere, the
message says `cannot be changed here`.

- C and C++: at a statement's first instruction.
- Rust: at a breakpoint, or after a step to its line's start, where the MIR proves that no
  statement is under way. The MIR is the compiler's mid-level representation of the function.
- Rust: a mark of `[settable]` covers the paths that don't panic or loop forever. On a path that
  panics, or never leaves a loop, a change may not be seen.
- Rust: not where the program goes on with a copy of the old value. It copied the local into a
  binding of its own, or into a Vec by `push`. Then it replaces the local, or the local goes out of
  scope, before reading it. The copy may be used before or after that:
  ``the program has already copied `x` into `n` ``. Where a call given `&mut x` since may have
  changed `x`: ``the program's own `n` (at line 34) may hold a copy of `x` that it goes on with``.
  Where every path then panics or exits first: ``every path from here panics or exits before
  reading `x` ``. Where a path never leaves a loop: ``on a path from here the program loops
  forever without reading `x` ``.
- Rust: not where no path reads the value again before it is overwritten or goes out of scope. The
  stop's own line counts, as `total = k + 3;` does for `total`:
  `the program does not read it after this stop, so a change would never be seen: it is overwritten
  at line 7 first`. Where a call on the way may panic, it adds `, unless the program panics or exits
  before that`. Where a drop's `Drop` impl may read it, it says only that the program may not read
  it. The hint says to change it after that line, or before its last read.

### Texts

A text in double quotes changes only a C `char[N]`, a C++ `std::string`, a Rust `String` local, or
a Java String. These are refused, and nothing is written:

| Refused | The message says |
| --- | --- |
| A text that doesn't fit its storage | `the text does not fit` |
| A text for a pointer, a reference, a `&str` or a field | `does not write through`, `not into a field or element` |
| A Rust `String` that the MIR doesn't prove initialized, or that a live borrow reads | `a borrow of it ... is still used after the stop` |
| Any Rust `String` in a crate with unsafe code, a nightly feature, or a source that Peeky can't read as rustc does | `the crate has unsafe code (in …)`, `the crate enables nightly features (in …)`, `Peeky cannot rule out unsafe code in …` |
| A `const` or `volatile` string | `a const object` |
| A Java variable not declared String | |
| A text that this debugger can't write | `cannot write memory` (lldb-dap 21 can write it); `cannot read memory` for a libstdc++ string with lldb-dap 18 |
| A GCC build's `std::string`, whose own buffer LLDB reads as `char[0]`, with a debugger that can't write memory | `the debugger misreads this build's string buffer as char[0]` |

A Java adapter bundle built before String changes were supported says to rebuild it:
`predates String changes`.

### Java locals that the program doesn't read

- A Java local that the program never reads is refused: `the program never reads it`. An example
  is a constant that javac folds into its uses.
- So is one that no path from the stop reads again: `does not read it after this stop`. For
  example, a lambda or an inner class captured it before the stop.

### Python locals that the current pass assigns before reading them

Take a local that the current pass of a loop hasn't assigned before the stop's line. If every path
through the rest of the pass assigns it before reading it, a change is refused:
`this function's code assigns it again (line N) before reading it`. The hint says to change it
after that assignment.

### Refused before the program runs

Sometimes the build rules out a probe's `--set` at its line, in every function there. Then the
probe is refused before the program runs: `refused_before_launch`, no hits, no run saved, and exit
status 2. The build rules a change out for:

- any change in an optimized build;
- a Rust local kept in a register;
- a Rust local inside a statement that has already read it, or copied it into a binding of the
  program's own. This must hold at every place of the line;
- a Rust local that no path reads again from any place of the line;
- a field through a pointer, or a field of a type that no change reaches;
- a Java local whose declared type no change reaches, or that can't hold the value;
- a Java field of a value declared as a type outside the program's classes. It is ruled out when
  no class of the program that may be that value declares the field.

For a register, the hint names a candidate edit with `std::hint::black_box`
([Rust 6.6](lang/rust-cookbook.md#66-see-what-a-fix-does-before-making-it)).

### A restore with `@before`

`--set-at LINE@HIT name=@before` writes back the value `name` held just before the probe's latest
change to it ([COOKBOOK 2.8](COOKBOOK.md#skip-a-statement-restore-with-before)). Peeky makes it
only where it knows that value exactly. Otherwise the restore is refused, and the probe ends, as
for any `--set-at` refusal. The hint says to give the value itself, or to try a candidate edit.

| Refused at the stop | The message says |
| --- | --- |
| No change to that name before this stop | `` `trend` has no earlier change to undo ``, and where the probe changes it at another line, `` This stop of line 47 came before line 52's change, so restore at a line that stops once per pass, after line 52 `` |
| The latest change was at another hit of its line | `` `trend` was last changed at hit 2 of line 15, not at hit 3 `` |
| A text, an object, or a Java `float` or `double` | `cannot be written back exactly: …` |
| Another thread, function, depth or callers | `` `trend` was changed at line 15@2 in find_turns, and this stop is 1 call deeper (a recursive call) `` |
| The name names other storage now: a shadowing local, another block's, a field of another object | `` `b.n` cannot be changed back here: it is `n` of Swap$Box@56, not `n` of Swap$Box@51, which the change was made to `` |
| Peeky can't tell which storage the change wrote | `cannot be written back exactly: Peeky cannot tell which storage it wrote (…)` |
| The call that made the change wasn't seen to begin | `Peeky did not see the call of … begin` |
| The function's first line ran again since the change | `and the first line of find_turns ran again since, at the same stack depth or above` |
| The watch on first lines ended: the debugger moved one, or 1000 stops | `Peeky stopped watching for new calls (…)` |

Before the program runs, with exit status 2, the request itself is refused (`INVALID_REQUEST`)
where no other `--set` or `--set-at` changes that name, or `@before` is given to `--set`. So is
the restore (`MUTATION_REJECTED`, `` `@before` cannot be used here ``) where Peeky can't watch a
function on its line for new calls. That is a Rust async body, a Python generator or coroutine, a
Python lambda, comprehension or class body, or a function whose first line Peeky can't place.

A restore whose hit never comes isn't refused. Peeky makes no restore for the writes it owed,
and the probe exits 4. The status names each one, by kind: `6 writes had no matching restore: 1
change and 5 with no change (line 49, hits 1 to 6)`. So does a warning, `made no restore for 6
writes (…)`. The program may have written the value since, so this doesn't say the value stayed.

### A field through a C pointer parameter

When the program's sources call the function only once, the message names that call:

```
push has one caller: m.c:15 in main, which passes `&r`, where `r.tail` is settable
```

If that caller only passes its own pointer parameter on, the message follows the calls to the
frame that owns the struct:

```
fits has one caller: wrap.c:40 in wrap, which passes it on (`lay`, its own pointer parameter); wrap has one caller: wrap.c:75 in main, which passes `&lay`, where `lay.width` is settable
```

Where it can't follow them, because of several callers, or a pointer kept or computed, it names no
frame for the change. It points to `describe`'s `mutable:` line instead.

## Two Java debugger failures

Two java-debug failures end with `DEBUGGER_ERROR` at once, instead of a timeout. See
[JAVA_ATTACH.md](JAVA_ATTACH.md#lost-stop-events). Peeky's issue #27 covers both: java-debug
dropping lifecycle events, such as an attached JVM's stop or a launched JVM's exit.

### `java-debug's event thread died (…)`

The adapter's stderr showed its event thread failing, so nothing more can be reported.

- Peeky detaches from the program or ends it, as on any debugger failure. It keeps the session as a
  failed one.
- Later requests repeat the error.
- `report` returns the final state: `state: closed`, `adapter_failure`, and `adapter_failed` in the
  history. `close` ends the session.
- The session's `debugger_events.stderr` keeps the adapter's last 16 KiB of log. A failed probe
  carries `debugger_events` too.

### `Lost stop: …`

An attached JVM made a stop that the debugger never reported. The message names the suspended
thread, its location, the session's breakpoint, and how long the stop has gone unreported.

- Nothing is served from that stop. Peeky detaches, and the JVM runs on.
- Later requests on the session repeat the error.
- `report` still returns the session as it was: `state: detached`, `lost_stop`, `history` and
  `debugger_events`. `close` ends the session.
- A failed probe carries `debugger_events` too. Keep it for Peeky's issue #27.

## How Peeky ends a session's processes

When a session ends, on an error or not, Peeky ends every process it started. An attached program
is only detached, never ended.

### When it runs

A failed or timed-out launch, a failed request, `close`, an idle worker, and a probe's end all go
through it. The error's `cleanup`, the `close` result and `peeky session close` say what happened.

A session can also fail with no request in flight: the pause guard, a lost stop, or the adapter's
fatal event. Its repeated error, its `report` and its `close` then carry the same `cleanup`.

### What `cleanup` says

- `terminated pid N`, or `pids N, M`: the program, and what it started.
- `nothing Peeky could identify was still running`.
- `detached; the attached program keeps running`. Peeky never ends a program it attached to.
- `pid N did not exit after SIGKILL …`.
- Or why Peeky couldn't tell.

When the debugger was gone before the end, `cleanup` adds which processes may have escaped. See
[What Peeky can't know](#what-peeky-cant-know).

### What Peeky knows

While the session runs, Peeky lists its processes:

- every second;
- soon after each stop;
- when the adapter announces the program (DAP `process`, java-debug `processid`);
- once more at the end.

It records each process by its pid and start time. It records:

- the adapter's descendants, and the descendants of every process it recorded before that is
  still the same process. So a child whose parent died, and which was reparented, stays known;
- every member of a recorded process's group, except the adapter's group and Peeky's own. A group
  outlives its leader, and its id is never reused while it has members;
- every process still in the adapter's session, with its group. The adapter leads a session of
  its own, and a process can only inherit a session, never join one. So such a process was started
  from the adapter, whatever became of its parents, and whenever the listing comes;
- the program that the adapter announced, with its process group, while it is the same process (by
  its start time). This holds even if the program left that session (`setsid`).

### The adapter's own ids

Peeky never reaps the adapter before the end. A dead adapter stays a zombie, so its pid, and the
ids of its group and session, can't name anyone else's process. `close` reaps it last.

- Every Peeky command that starts adapters (`peeky`, `peeky-worker`, `peeky-probe`,
  `peeky-agent`) first sets an inherited, ignored `SIGCHLD` back to the default. If it stayed
  ignored, the kernel would reap the adapter itself.
- An adapter reaped anyway, by a caller or by the kernel, counts as gone. Its session and group no
  longer count, unless a process that Peeky recorded, still the same one, is still in them.
- Where Peeky can't read the process table, and Python has no `waitid` (macOS before Python
  3.13), the adapter's group isn't signalled. `cleanup` then says `nothing was ended`.

### How Peeky ends them

1. It asks the debugger first: `disconnect` with `terminateDebuggee`.
2. Then each process it knows that still runs gets SIGTERM, along with those processes' groups and
   the adapter's group. A launched JVM shares the adapter's group.
3. SIGKILL follows after 1 s.

So all of these are ended, and a JVM's shutdown hooks run:

- a program in a process group of its own, where debugpy's launcher puts it;
- a program that never reached the debugger;
- children in other groups and sessions;
- children that a program left behind;
- the children of a program whose adapter died.

### What Peeky can't know

Peeky can't tell a process from anyone else's when all three of these hold:

- it was started after the last listing;
- it is in a session of its own: it called `setsid`;
- its parent has exited.

Once the debugger is gone, its process tree is gone too. `cleanup` then says so:

```
…; the debugger was gone: a process the program started in the last N s, in a session of its own and whose parent had exited, may survive
```

It never claims more than it knows: `nothing Peeky could identify was still running`.

## Exit status

The CLI's exit status is the same for `peeky probe` and the `peeky-probe` script:

| Status | Meaning |
| --- | --- |
| 0 | Success. |
| 1 | A completed command with a negative result: a probe that failed after the debugger's launch or attach request was sent, timed out, or never stopped; a verify that didn't pass; a `session get` with a failed selector. |
| 2 | An error, or a refusal: anything Peeky turned down. See below. |
| 3 | Not there yet: a background job is still running, or `session start --hit`, `continue --count` or `continue --to` stopped before the stop asked for. |
| 4 | A probe that completed, but didn't make a change it was asked for (`--set`, `--set-at`), because its stop never came. JSON lists the change in `unapplied`. The values are evidence of a run without that change. A `--set-at LINE@all` that reached its cap of 100 stops while the line still ran exits 4 too, and its `unapplied` entry has `made`. The values and output are then from a partially intervened run: the first `made` stops were changed; later executions were not. A `@before` restore whose hits never came, so that it made no restore for writes it owed, exits 4 too. Its `unapplied` entry has `unrestored_all`, each such write's `LINE@HIT`, `unrestored_kinds`, and `unrestored`, the latest one's. That says no restore was made, not that the value stayed. |

Status 2 includes any probe that ended before the debugger's launch or attach request was sent,
whatever its code. Its JSON says `refused_before_launch`. The causes include:

- a `--set` or `--set-at` that its build rules out;
- a file outside the worker root;
- an outdated Java adapter;
- a build that failed;
- a debugger that didn't start;
- a remote worker's refusal, or a slow remote build.

Peeky didn't start the program then, though a build, or a Python candidate's interpreter, may have
run. So the text is only the error and its hint, and no run is kept as evidence. A remote answer
that Peeky can't read is `TRANSPORT_LOST` instead, because its outcome is unknown.

## Protocol version

`POST /version` returns the protocol version, the supported operations and the error codes. Every
HTTP response carries `X-Peeky-Protocol: 5`.

- **Protocol 2** adds the source comparison for remote clients
  ([REMOTE.md](REMOTE.md#source-identity)). `version` says which sources a client hashes.
- **Protocol 3** marks the paths in a remote result. A compared source is `source:NAME`, which the
  client shows as its own copy ([REMOTE.md](REMOTE.md#paths-in-results)). A client sends
  `protocol` with `expect_inputs`, and `version` returns the worker's `root`.
- **Protocol 4** adds restores: a `--set-at` value `@before`, which a protocol 3 worker would
  take as a value to write. The worker's `launch` takes `entry_lines`, and its `set_variable`
  takes `restorable` and `target` and returns `restore`
  ([COOKBOOK 2.8](COOKBOOK.md#skip-a-statement-restore-with-before)).
- **Protocol 5** withholds what a C or C++ pointer one past an array's end points at. The
  worker's values give `past_end`, and a protocol 4 worker would read `end[0]` there as a value
  ([C and C++ 2.1](lang/c-cpp-cookbook.md#a-pointer-one-past-an-arrays-end)).
- **Protocol 6** marks what is read through a C or C++ pointer `unproven`, with why, where Peeky
  can't show the pointer points at a live object. The worker's values give `unproven_read`. A
  protocol 5 worker shows such a value with no mark
  ([C and C++ 2.1](lang/c-cpp-cookbook.md#read-through-a-pointer)).
- A client and a worker of different protocols refuse each other before anything runs.
