# Error codes (protocol 1)

Every Peeky failure, from the HTTP worker, the SDK (`peeky.errors.PeekyError`) or the CLI (`--json` prints `{"error": {...}}`; stderr shows `[CODE] message` plus a hint), carries the fields below. Probe results use the same object in their `error` field, with a `status` of `failed`, `no-stop` or `timeout`.

| Field | Meaning |
| --- | --- |
| `code` | Stable identifier to branch on. Messages may change; codes do not. |
| `hint` | What to do next. |
| `retryable` | Whether repeating the same request can succeed after the hinted step. |
| `session_closed` | The worker discarded the session (provenance, debugger or time failures). Start a new one. |

| Code | Typical cause | Agent action |
| --- | --- | --- |
| `STALE_STOP` | The program moved on; `stop_id` is old, or the stop was auto-resumed by `--max-pause` | `wait` until stopped, then `inspect`; on timed programs prefer a probe |
| `INSPECT_REQUIRED` | Mutation without a fresh scope, or a value-view read still pending | `inspect` again, use the new reference |
| `STALE_HANDLE` | Expansion handle revoked or frame/scope changed | `inspect` again |
| `NOT_FOUND` | Name/selector not visible, or (Python) a name missing from the locals that Peeky could not prove is a global there; before a probe runs, a `--set` name no function at its line declares (Rust) | Use a name from the message's `visible:` or `children:` list (or `inspect`, `session get <value>`); dict entries are `d['key']`; C++/Java members are `this.x`; a caller's locals need `--frame N` |
| `MUTATION_REJECTED` | Not a root local of the stopped (innermost) frame (Java: nor a scalar field or array element that is not final or static; Rust, C, C++: nor an integer field or element of a local the frame holds itself, reached without a pointer, reference or Box and outside unions, enums, packed and standard-library types; and only where no statement under way may hold a copy, else `cannot be changed here`: in C/C++ at a statement's first instruction, in Rust at a breakpoint or a step to its line's start where the MIR proves none), or the value is not a literal (Python) / scalar (compiled). A probe whose `--set` its build rules out at its line in every function there (any change in an optimized build, a Rust local kept in a register, a field through a pointer or of a type no change reaches) is refused before the program runs: `refused_before_launch`, no hits, no run saved (#138) | The hint names the one action the broken rule leaves (for a register: a candidate edit with `std::hint::black_box`, COOKBOOK 6.6) (stop at the statement's first line, change it in its own frame, pass a value in range, ...); otherwise observe, or edit and `verify` |
| `BUDGET_EXHAUSTED` | A bounded read limit was reached | Narrow the request |
| `INVALID_BREAKPOINT` | Line not executable or unsupported breakpoint field | Use `peeky describe` lines |
| `NO_STOP` (probe result) | Program ended without reaching the line: the line never runs, or the program failed first (`exit_code` when the debugger reports it) | Pick an executed line; after a failure, read the output and check the program's arguments |
| `BUILD_FAILED` | The program did not compile, or its build timed out (with `--old/--new`: the edit does not build); the message has the compiler's output | Fix what the compiler reports and run again; your files were not changed |
| `PROVENANCE_CHANGED` | Sources, build outputs or tools changed | New session on a fresh build |
| `TIMEOUT` | A time bound was hit; session discarded | Start again; never blindly retry a mutation |
| `DEBUGGER_ERROR` | Adapter/debugger failure: a broken protocol stream (for example a reply over 4 MiB), java-debug's event thread dying, or an attached JVM's stop the debugger never reported (`Lost stop:`); session discarded | Start again; capture narrower selectors if values are huge; for Java, keep `debugger_events` (#27) |
| `SESSION_ACTIVE` / `NO_SESSION` | Session lifecycle misuse | Close / start a session |
| `BUSY` | Another probe is running on this worker | Retry later |
| `UNSUPPORTED` | Operation or capability not offered, or a Java adapter bundle built before a required java-debug patch | Call `version`; rebuild the Java adapter with `scripts/setup_java_adapter.py` |
| `UNAUTHORIZED` | Missing or wrong bearer token | Use the worker's `DEBUG_TOKEN` |
| `INVALID_REQUEST` | Malformed arguments (such as a `--frame` past the stack, whose message says how many frames there are), or an evidence store that cannot be used | Fix the request; for a store, move it aside or pass `--store` |
| `INVALID_QUERY` | `peeky sql`/`peeky queries`: bad SQL, a write, a missing `--param`, or a query over its time bound | Fix the query; `peeky sql --schema` lists tables, columns and canned queries |
| `INTERNAL_ERROR` | A Peeky bug (unexpected exception type) | Report it with the message; do not retry or change your request |
| `UNREACHABLE` | No connection reached the worker (tunnel or worker down); nothing was applied | Restore the tunnel/worker and retry; the session is kept |
| `TRANSPORT_LOST` | The connection broke mid-request; outcome unknown | Reconnect, then `inspect`/`wait`; never repeat a mutation blindly |
| `JOB_LOST` | A `--background` job's process died before finishing | Rerun the command |

Two Java debugger failures (#27) end with `DEBUGGER_ERROR` at once instead of a timeout; see [JAVA_ATTACH.md](JAVA_ATTACH.md#lost-stop-events-27):
- **`java-debug's event thread died (…)`.** The adapter's stderr showed its event thread failing, so nothing more can be reported. Peeky detaches or ends the program, as on any debugger failure, and keeps the session as a failed one. Later requests repeat the error. `report` returns its final state (`state: closed`, `adapter_failure`, history `adapter_failed`), and `close` ends it. Its `debugger_events.stderr` keeps the adapter's last 16 KiB of log; a failed probe carries `debugger_events` too.
- **`Lost stop: …`.** An attached JVM's stop that the debugger never reported. The message names the suspended thread, its location, the session's breakpoint and how long the stop has gone unreported. Nothing is served from that stop, and Peeky detaches, so the JVM runs on. Later requests on the session repeat the error. `report` still returns the session as it was (`state: detached`, `lost_stop`, `history`, `debugger_events`), and `close` ends it. A failed probe carries `debugger_events` too; keep it for #27.

`POST /version` returns the protocol version, supported operations and the error codes. Every HTTP response carries `X-Peeky-Protocol: 1`.
