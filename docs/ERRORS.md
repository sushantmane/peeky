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
| `NOT_FOUND` | Name/selector not visible | Use a name from the message's `visible:` list (or `inspect`) |
| `MUTATION_REJECTED` | Not a root local, or the value is not a literal (Python) / scalar (compiled) | Use a literal, observe instead, or edit and `verify` |
| `BUDGET_EXHAUSTED` | A bounded read limit was reached | Narrow the request |
| `INVALID_BREAKPOINT` | Line not executable or unsupported breakpoint field | Use `peeky describe` lines |
| `NO_STOP` (probe result) | Program finished without reaching the line | Pick an executed line |
| `PROVENANCE_CHANGED` | Sources, build outputs or tools changed | New session on a fresh build |
| `TIMEOUT` | A time bound was hit; session discarded | Start again; never blindly retry a mutation |
| `DEBUGGER_ERROR` | Adapter/debugger failure; session discarded | Start again |
| `SESSION_ACTIVE` / `NO_SESSION` | Session lifecycle misuse | Close / start a session |
| `BUSY` | Another probe is running on this worker | Retry later |
| `UNSUPPORTED` | Operation or capability not offered | Call `version` |
| `UNAUTHORIZED` | Missing or wrong bearer token | Use the worker's `DEBUG_TOKEN` |
| `INVALID_REQUEST` | Malformed arguments, or an evidence store that cannot be used | Fix the request; for a store, move it aside or pass `--store` |
| `INVALID_QUERY` | `peeky sql`/`peeky queries`: bad SQL, a write, a missing `--param`, or a query over its time bound | Fix the query; `peeky sql --schema` lists tables, columns and canned queries |
| `INTERNAL_ERROR` | A Peeky bug (unexpected exception type) | Report it with the message; do not retry or change your request |
| `UNREACHABLE` | No connection reached the worker (tunnel or worker down); nothing was applied | Restore the tunnel/worker and retry; the session is kept |
| `TRANSPORT_LOST` | The connection broke mid-request; outcome unknown | Reconnect, then `inspect`/`wait`; never repeat a mutation blindly |
| `JOB_LOST` | A `--background` job's process died before finishing | Rerun the command |

`POST /version` returns the protocol version, supported operations and the error codes. Every HTTP response carries `X-Peeky-Protocol: 1`.
