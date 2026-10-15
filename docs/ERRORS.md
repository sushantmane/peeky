# Error codes (protocol 1)

Every Peeky failure, from the HTTP worker, the SDK (`peeky.errors.PeekyError`) or the CLI (`--json` prints `{"error": {...}}`; stderr shows `[CODE] message` plus a hint), carries:

| Field | Meaning |
| --- | --- |
| `code` | Stable identifier to branch on. Messages may change; codes do not. |
| `hint` | What to do next. |
| `retryable` | Whether repeating the same request can succeed after the hinted step. |
| `session_closed` | The worker discarded the session (provenance, debugger or time failures). Start a new one. |

| Code | Typical cause | Agent action |
| --- | --- | --- |
| `STALE_STOP` | The program moved on; `stop_id` is old | `wait` until stopped, then `inspect` |
| `INSPECT_REQUIRED` | Mutation without a fresh scope, or a value-view read still pending | `inspect` again, use the new reference |
| `STALE_HANDLE` | Expansion handle revoked or frame/scope changed | `inspect` again |
| `NOT_FOUND` | Name/selector not visible | `inspect` to list names |
| `MUTATION_REJECTED` | Not a root scalar local or not a literal | Observe instead, or edit and `verify` |
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
| `INVALID_REQUEST` | Malformed arguments | Fix the request |

`POST /version` returns the protocol version, supported operations and the error codes. Every HTTP response carries `X-Peeky-Protocol: 1`.
