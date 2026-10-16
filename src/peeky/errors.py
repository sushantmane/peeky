"""Stable, machine-readable errors for agents (protocol version 1).

Every failure carries a `code` an agent can branch on, a `hint` saying what to do
next, whether retrying the same request can help, and whether the session was
closed. Codes are part of the protocol; messages are for humans and may change.
"""

import io
import json
import re
from urllib.error import HTTPError

PROTOCOL_VERSION = 1

# (pattern on the message, code, hint, retryable). First match wins.
_RULES = (
    (
        r"^Evidence query failed",
        "INVALID_QUERY",
        "Fix the SQL or the --param values; `peeky sql --schema` lists the tables, columns and "
        "canned queries. Queries are one read-only SELECT over captured evidence.",
        False,
    ),
    (
        r"^Evidence store ",
        "INVALID_REQUEST",
        "The evidence store at the path in the message cannot be used; move it aside (a new "
        "one is created) or pass --store PATH.",
        False,
    ),
    (
        r"is unreachable \(.*the request was not sent",
        "UNREACHABLE",
        "No request reached the worker. Restore the worker or SSH tunnel and retry; the worker "
        "keeps a session until its idle timeout. If the worker is gone for good, use "
        "`peeky session close --forget`.",
        True,
    ),
    (
        r"broke during .*its outcome is unknown",
        "TRANSPORT_LOST",
        "The request may or may not have been applied. Restore the tunnel, then `inspect`/`wait` "
        "before anything else; never repeat a mutation blindly.",
        False,
    ),
    (
        r"^Job [0-9a-f]{16} was lost",
        "JOB_LOST",
        "The background job process died; rerun the command. The message has its log.",
        False,
    ),
    (r"^No job named", "NOT_FOUND", "Use an id from `peeky job list`.", False),
    (
        r"Stale stop_id|program is not stopped|not stopped; run",
        "STALE_STOP",
        "Call wait until state is stopped, then inspect the latest stop.",
        False,
    ),
    (
        r"Missing or stale session_id|No session named",
        "NO_SESSION",
        "Start a new session (launch or `peeky session start`).",
        False,
    ),
    (
        r"Close the current session before launching",
        "SESSION_ACTIVE",
        "Close the existing session first.",
        False,
    ),
    (
        r"Inspect the scope before changing|mutation_blocked|pending; inspect again",
        "INSPECT_REQUIRED",
        "Run inspect again, then use the new scope reference.",
        True,
    ),
    (
        r"stale expansion handle|Expansion .*changed|Expansion .*ambiguous|changed during value views"
        r"|Duplicate scope identities",
        "STALE_HANDLE",
        "Inspect again and use the newly issued handles.",
        True,
    ),
    (
        r"budget exhausted|budget exceeded|exceeds bounded expansion|exceeds limit|beyond the bounded",
        "BUDGET_EXHAUSTED",
        "Narrow the request (fewer selectors, smaller expansion) or inspect again.",
        False,
    ),
    (r"Another probe is running", "BUSY", "Wait for the running probe, then retry.", True),
    (
        r"Only writable|mutation requires|mutation is outside|Only .*locals support|"
        r"numeric or boolean scalar|scalar literal|bounded decimal integer|must be a literal"
        r"|has no readable value here|refused in an optimized build",
        "MUTATION_REJECTED",
        "Only root locals accept literal values (Python: literals incl. containers; compiled "
        "languages: scalars); observe instead or edit the source and run verify.",
        False,
    ),
    (
        r"not a visible local|Expected exactly one existing local|not found in the inspected",
        "NOT_FOUND",
        "Inspect to list visible names; selectors are dotted paths.",
        False,
    ),
    (
        r"[Bb]reakpoint|executable lines|line must be",
        "INVALID_BREAKPOINT",
        "Choose a line from `peeky describe` breakpoint_lines.",
        False,
    ),
    (
        r"cannot verify edits|Attached JVMs cannot",
        "UNSUPPORTED",
        "Verify on a fresh-build target, not an attached JVM.",
        False,
    ),
    (
        r"Unknown operation|Unknown .*field|unsupported command",
        "UNSUPPORTED",
        "Call `version` for supported operations.",
        False,
    ),
    (
        r"[Pp]rovenance|changed during|changed; re-attach|identity changed|differ from",
        "PROVENANCE_CHANGED",
        "Sources, build outputs or tools changed; start a new session on a fresh build.",
        False,
    ),
    (
        r"deadline|timed out|did not stop|HTTP cap",
        "TIMEOUT",
        "The operation hit its time bound; the session was discarded. Start again; never "
        "blindly retry a mutation.",
        False,
    ),
)


def classify(exc, *, session_closed=None):
    """Map an exception to the protocol error object."""
    message = str(exc) or type(exc).__name__
    if isinstance(exc, PeekyError):
        return exc.payload
    closed = (
        isinstance(exc, (RuntimeError, TimeoutError, OSError))
        if session_closed is None
        else session_closed
    )
    if isinstance(exc, FileNotFoundError):
        # A missing target, config, checker or tool: the request, not the debugger.
        return {
            "code": "INVALID_REQUEST",
            "message": message[:2000],
            "hint": "A path does not exist; check the target, --at, --config, --check and "
            "PEEKY_* tool paths.",
            "retryable": False,
            # Fail safe: the worker closes sessions on any OSError.
            "session_closed": bool(closed),
        }
    for pattern, code, hint, retryable in _RULES:
        if re.search(pattern, message):
            if code in ("UNREACHABLE", "TRANSPORT_LOST"):
                # A client-side transport failure never closes the worker's session.
                closed = False
            break
    else:
        if isinstance(exc, TimeoutError):
            code, hint, retryable = "TIMEOUT", _RULES[-1][2], False
        elif isinstance(exc, (RuntimeError, OSError)):
            code, hint, retryable = (
                "DEBUGGER_ERROR",
                "The debugger failed and the session was closed; start a new one.",
                False,
            )
        elif isinstance(exc, (ValueError, KeyError, TypeError)):
            # Validation errors, and malformed request shapes surfacing as Key/TypeError.
            code, hint, retryable = (
                "INVALID_REQUEST",
                "Fix the request arguments; see `peeky <command> --help`.",
                False,
            )
        else:
            message = f"{type(exc).__name__}: {message}"
            code, hint, retryable = (
                "INTERNAL_ERROR",
                "This is a Peeky bug, not a problem with your request; report it with the "
                "message. Retrying will not help.",
                False,
            )
    return {
        "code": code,
        "message": message[:2000],
        "hint": hint,
        "retryable": retryable,
        "session_closed": bool(closed),
    }


class PeekyError(HTTPError):
    """Raised by the SDK; still an HTTPError (``.code`` is the HTTP status)."""

    def __init__(self, url, status, headers, payload, body=None):
        self.payload = payload
        self.error_code = payload.get("code", "INVALID_REQUEST")
        self.hint = payload.get("hint", "")
        self.retryable = payload.get("retryable", False)
        self.session_closed = payload.get("session_closed", False)
        # Keep the original JSON body readable for callers that inspect the response.
        raw = body if body is not None else json.dumps({"error": payload}).encode()
        super().__init__(
            url,
            status,
            f"[{self.error_code}] {payload.get('message', '')}",
            headers,
            io.BytesIO(raw),
        )

    def __str__(self):
        return f"[{self.error_code}] {self.payload.get('message', '')}"
