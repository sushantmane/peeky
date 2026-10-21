# Error codes (protocol 1)

Every Peeky failure, from the HTTP worker, the SDK (`peeky.errors.PeekyError`) or the CLI (`--json` prints `{"error": {...}}`; stderr shows `[CODE] message` plus a hint), carries the fields below. Probe results use the same object in their `error` field, with a `status` of `failed`, `no-stop` or `timeout`.

| Field | Meaning |
| --- | --- |
| `code` | Stable identifier to branch on. Messages may change; codes do not. |
| `hint` | What to do next. |
| `retryable` | Whether repeating the same request can succeed after the hinted step. |
| `session_closed` | The worker discarded the session (provenance, debugger or time failures). Start a new one. |
| `cleanup` | Present when the error ended a session: what became of the processes it started. `terminated pid N` (or `pids N, M`: the program and what it started), `nothing Peeky could identify was still running`, `detached; the attached program keeps running` (Peeky never ends a program it attached to), `pid N did not exit after SIGKILL …`, or why Peeky could not tell. When the debugger was gone before the end, it adds which processes may have escaped (below). |

| Code | Do next | Why, and more |
| --- | --- | --- |
| `STALE_STOP` | `wait` until stopped, then `inspect` | The program moved on; `stop_id` is old, or the stop was auto-resumed by `--max-pause`. On timed programs, prefer a probe |
| `INSPECT_REQUIRED` | `inspect` again, use the new reference | Mutation without a fresh scope, or a value-view read still pending |
| `STALE_HANDLE` | `inspect` again | Expansion handle revoked or frame/scope changed |
| `NOT_FOUND` | Use a name from the message's `visible:` or `children:` list | Name/selector not visible, or (Python) a name missing from the locals that Peeky could not prove is a global there; before a probe runs, a `--set` name no function at its line declares (Rust). Or list names with `inspect`, `session get <value>`; dict entries are `d['key']`; C++/Java members are `this.x`; a caller's locals need `--frame N` |
| `MUTATION_REJECTED` | Follow the hint: it names the one way around the rule | A change Peeky cannot make there; nothing was written. The rules, as lists: [MUTATION_REJECTED in detail](#mutation_rejected-in-detail) |
| `BUDGET_EXHAUSTED` | Narrow the request | A bounded read limit was reached |
| `INVALID_BREAKPOINT` | Use `peeky describe` lines | Line not executable or unsupported breakpoint field |
| `NO_STOP` (probe result) | Pick an executed line | Program ended without reaching the line: the line never runs, or the program failed first (`exit_code` when the debugger reports it). After a failure, read the output and check the program's arguments |
| `BUILD_FAILED` | Fix what the compiler reports and run again | The program did not compile, or its build timed out (with `--old/--new`: the edit does not build); the message has the compiler's output. Your files were not changed |
| `PROVENANCE_CHANGED` | New session on a fresh build | Sources, build outputs or tools changed |
| `TIMEOUT` | Start again; never blindly retry a mutation | A time bound was hit; session discarded. `The debuggee did not start` means the program never reached the debugger during the launch. For a debuggee that did not start, check that it can read its own path and every directory above it (a hung file system or a pending macOS privacy prompt blocks it), then retry |
| `DEBUGGER_ERROR` | Start again | Adapter/debugger failure: a broken protocol stream (for example a reply over 4 MiB), java-debug's event thread dying, or an attached JVM's stop the debugger never reported (`Lost stop:`); session discarded. Capture narrower selectors if values are huge; for Java, keep `debugger_events` (#27) |
| `SESSION_ACTIVE` / `NO_SESSION` | Close / start a session | Session lifecycle misuse |
| `BUSY` | Retry later | Another probe is running on this worker |
| `UNSUPPORTED` | Do what the message names instead, or fix the setup it names | Operation or capability not offered (`--capture return` for Python), a Java adapter bundle built before a required java-debug patch, or a C++ target whose compiler is not clang (`C++ targets need clang++ …`, refused before building: CPP.md). Call `version`; rebuild the Java adapter with `scripts/setup_java_adapter.py`; set `PEEKY_NATIVE_CXX` to clang++ |
| `UNAUTHORIZED` | Use the worker's `DEBUG_TOKEN` | Missing or wrong bearer token |
| `INVALID_REQUEST` | Fix the request | Malformed arguments (such as a `--frame` past the stack, whose message says how many frames there are), or an evidence store that cannot be used. For a store, move it aside or pass `--store` |
| `INVALID_QUERY` | Fix the query | `peeky sql`/`peeky queries`: bad SQL, a write, a missing `--param`, or a query over its time bound. `peeky sql --schema` lists tables, columns and canned queries |
| `INTERNAL_ERROR` | Report it with the message; do not retry or change your request | A Peeky bug (unexpected exception type) |
| `UNREACHABLE` | Restore the tunnel/worker and retry | No connection reached the worker (tunnel or worker down); nothing was applied. The session is kept |
| `TRANSPORT_LOST` | Reconnect, then `inspect`/`wait`; never repeat a mutation blindly | The connection broke mid-request; outcome unknown |
| `JOB_LOST` | Rerun the command | A `--background` job's process died before finishing |

## MUTATION_REJECTED in detail

A refused change writes nothing. The hint names the one action the broken rule leaves (stop at the statement's first line, change it in its own frame, pass a value in range, ...); otherwise observe, or edit and `verify`.

**What can change.** Anything else is refused:
- a root local of the stopped (innermost) frame;
- Java: also a scalar field or array element that is not final or static;
- Rust, C, C++: also an integer field or element of a local the frame holds itself, reached without a pointer, reference or Box and outside unions, enums, packed and standard-library types;
- the value must be a literal (Python) or a scalar (compiled).

**Where.** Only where no statement under way may hold a copy, else `cannot be changed here`:
- C and C++: at a statement's first instruction;
- Rust: at a breakpoint, or a step to its line's start, where the MIR proves none.

**Texts (#137).** A text in double quotes changes only a C `char[N]`, a C++ `std::string`, a Rust `String` local or a Java String. These are refused, nothing written:
- one that does not fit its storage (`the text does not fit`);
- one for a pointer, reference, `&str` or field (`does not write through`, `not into a field or element`);
- a Rust `String` the MIR does not prove initialized, or that a live borrow reads (`a borrow of it ... is still used after the stop`);
- any Rust `String` of a crate with unsafe code, a nightly feature or a source Peeky cannot read as rustc does (`the crate has unsafe code (in …)`, `the crate enables nightly features (in …)`, `Peeky cannot rule out unsafe code in …`);
- a `const` or `volatile` string (`a const object`);
- a Java variable not declared String;
- one this debugger cannot write (`cannot write memory`: lldb-dap 21 can; or `cannot read memory` for a libstdc++ string with lldb-dap 18).

A Java bundle built before then says to rebuild it (`predates String changes`).

**Java locals the program does not read.**
- Any Java local the program never reads (a constant javac folds into its uses) is refused (`the program never reads it`).
- So is one no path from the stop reads again (`does not read it after this stop`: a lambda or inner class captured it before).

**Refused before the program runs.**
- A probe whose `--set` its build rules out at its line in every function there (any change in an optimized build, a Rust local kept in a register, a field through a pointer or of a type no change reaches) is refused before the program runs: `refused_before_launch`, no hits, no run saved (#138), and exit status 2.
- For a register, the hint names a candidate edit with `std::hint::black_box` (COOKBOOK 6.6).

**A field through a C pointer parameter.** When the program calls the function once, the message names that call (``push has one caller: m.c:15 in main, which passes `&r`, where `r.tail` is settable``).

Two Java debugger failures (#27) end with `DEBUGGER_ERROR` at once instead of a timeout; see [JAVA_ATTACH.md](JAVA_ATTACH.md#lost-stop-events-27):
- **`java-debug's event thread died (…)`.** The adapter's stderr showed its event thread failing, so nothing more can be reported. Peeky detaches or ends the program, as on any debugger failure, and keeps the session as a failed one. Later requests repeat the error. `report` returns its final state (`state: closed`, `adapter_failure`, history `adapter_failed`), and `close` ends it. Its `debugger_events.stderr` keeps the adapter's last 16 KiB of log; a failed probe carries `debugger_events` too.
- **`Lost stop: …`.** An attached JVM's stop that the debugger never reported. The message names the suspended thread, its location, the session's breakpoint and how long the stop has gone unreported. Nothing is served from that stop, and Peeky detaches, so the JVM runs on. Later requests on the session repeat the error. `report` still returns the session as it was (`state: detached`, `lost_stop`, `history`, `debugger_events`), and `close` ends it. A failed probe carries `debugger_events` too; keep it for #27.

When a session ends, on an error or not, Peeky ends every process it started (#143):
- **What it knows.** While the session runs, Peeky lists its processes: every second, soon after each stop, when the adapter announces the program (DAP `process`, java-debug `processid`), and once more at the end. It records each by pid and start time:
  - the adapter's descendants, and the descendants of every process recorded before that is still the same one, so a child whose parent died, and was reparented, stays known;
  - every member of a recorded process's group (a group outlives its leader, and its id is never reused while it has members), except the adapter's group and Peeky's own;
  - every process still in the adapter's session, with its group: the adapter leads a session of its own, and a process can only inherit a session, never join one, so it was started from the adapter, whatever became of its parents and whenever the listing comes (#168);
  - the program the adapter announced, with its process group, while it is the same process (its start), even if it left that session (`setsid`).
- **The adapter's own ids** (PR #170 review). Peeky never reaps the adapter before the end: a dead adapter stays a zombie, which keeps its pid, and so its group's and its session's ids, from naming anyone else's, and `close` reaps it last.
  - Every Peeky command that starts adapters (`peeky`, `peeky-worker`, `peeky-probe`, `peeky-agent`) first sets an inherited ignored `SIGCHLD` back to the default; with it ignored, the kernel would reap the adapter itself.
  - An adapter reaped anyway (by a caller, or by the kernel) counts as gone: its session and group no longer count, unless a process Peeky recorded, still the same one, is still in them.
  - Where Peeky cannot read the process table and Python has no `waitid` (macOS before 3.13), the adapter's group is not signalled, and `cleanup` says `nothing was ended`.
- **How it ends them.** It asks the debugger first (`disconnect` with `terminateDebuggee`). Then whatever it knows that still runs gets SIGTERM, along with those processes' groups and the adapter's group (a launched JVM shares it), and SIGKILL after 1 s. So a program in a process group of its own (debugpy's launcher puts it there), one that never reached the debugger, children in other groups and sessions, children a program left behind, and those of a program whose adapter died are all ended, and a JVM's shutdown hooks run.
- **What it cannot know.** A process started after the last listing, in a session of its own (it called `setsid`), whose parent has exited, cannot be told from anyone else's. Once the debugger is gone, its tree is gone too, so `cleanup` then says so: `…; the debugger was gone: a process the program started in the last N s, in a session of its own and whose parent had exited, may survive`. It never claims more than it knows: `nothing Peeky could identify was still running`.
- **When.** A failed or timed-out launch, a failed request, `close`, an idle worker and a probe's end all go through it. The error's `cleanup`, the `close` result and `peeky session close` say what happened. A session that failed with no request in flight (the pause guard, a lost stop, the adapter's fatal event) keeps it too: its repeated error, its `report` and its `close` carry the same `cleanup`. An attached program is only detached, never ended.

The CLI's exit status (`peeky probe` and the `peeky-probe` script alike): 0 success; 1 a completed command with a negative result (a probe that failed, timed out or never stopped, a verify that did not pass, a `session get` with a failed selector); 2 an error, or a refusal: anything Peeky turned down, including a probe refused before it ran (`refused_before_launch`, a `--set` or `--set-at` its build rules out: nothing ran, so its text is only the error and its hint); 3 not there yet (a background job still running, `session start --hit`, `continue --count` or `continue --to` stopped before the stop asked for); 4 a probe that completed but did not make a change it was asked for (`--set`, `--set-at`: its stop never came; `unapplied` in JSON), whose values are evidence of a run without that change.

`POST /version` returns the protocol version, supported operations and the error codes. Every HTTP response carries `X-Peeky-Protocol: 1`.
