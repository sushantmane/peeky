"""Stable, machine-readable errors for agents (protocol version 1).

Every failure carries a `code` an agent can branch on, a `hint` saying what to do
next, whether retrying the same request can help, and whether the session was
closed. Codes are part of the protocol; messages are for humans and may change.
"""

import io
import json
import re
from urllib.error import HTTPError

from peeky.textlines import split_lines

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
        "A missing or empty store means no probe was saved there yet: run a probe, or check "
        "--store. An unusable store can be moved aside (a new one is created) or replaced "
        "with --store PATH.",
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
        r"^DAP (frame|headers)|^Duplicate DAP header|non-DAP line|^Outgoing DAP frame"
        r"|^Expected DAP object|^Incomplete DAP frame",
        "DEBUGGER_ERROR",
        "The debugger's protocol stream failed (for example a reply larger than 4 MiB) and the "
        "session was closed. Start a new one; capture narrower selectors if values are huge.",
        False,
    ),
    (
        r"^java-debug's event thread died",
        "DEBUGGER_ERROR",
        "java-debug stopped processing the JVM's events, so no stop, step or exit can be "
        "reported any more; the session was closed (an attached JVM is detached, a launched "
        "one ended). Retry. If it recurs, rebuild the adapter with scripts/setup_java_adapter.py "
        "and report the message (issue #27).",
        False,
    ),
    (
        r"^Lost stop: ",
        "DEBUGGER_ERROR",
        "java-debug did not report a stop it made (issue #27), so this session's state cannot "
        "be trusted: Peeky served nothing from it and detached, and the JVM runs on. Close the "
        "session (`peeky session close`) and retry. To help #27, first keep `peeky session "
        "report --json` (its `debugger_events`) or the probe's `debugger_events`.",
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
        r"|has no readable value here|refused in an optimized build|did not change "
        r"|has no memory location|refused to change|would overwrite a change"
        r"|cannot be changed|can be changed only|changes only the stopped|values can be changed"
        r"|locals can be changed "
        r"for this language|predates field and element changes|predates String changes",
        "MUTATION_REJECTED",
        "Observe the value instead, or change the source with a candidate edit "
        "(`--old`/`--new`) and verify it; rows marked [settable] can be changed here.",
        False,
    ),
    (
        r"not a visible local|Expected exactly one existing local|not found in the inspected"
        r"|not a field or element the debugger lists",
        "NOT_FOUND",
        "Inspect to list visible names. Reads take dotted paths (a.b, this.x); changes take "
        "the stopped function's own locals, and in Java, Rust, C and C++ their fields and "
        "array elements.",
        False,
    ),
    (
        r"Other threads stopped at this breakpoint",
        "INVALID_REQUEST",
        "`peeky session continue` serves the other threads' stops; step after that.",
        False,
    ),
    (
        r"has no code to stop at",
        "INVALID_BREAKPOINT",
        "Choose a line from `peeky describe` breakpoint_lines.",
        False,
    ),
    (
        r"debugger refused to step",
        "INVALID_REQUEST",
        "Nothing moved; `peeky session continue` instead.",
        False,
    ),
    (
        r"[Bb]reakpoint|executable lines|line must be",
        "INVALID_BREAKPOINT",
        "Choose a line from `peeky describe` breakpoint_lines.",
        False,
    ),
    (
        r"step stopped outside the program's sources",
        "INVALID_REQUEST",
        "Nothing outside your sources is read or changed; follow the message (step out "
        "while your code is on the stack, otherwise continue).",
        False,
    ),
    (
        r"adapter bundle at .* predates Peeky's java-debug fixes",
        "UNSUPPORTED",
        "Rebuild the Java adapter with scripts/setup_java_adapter.py (it applies Peeky's "
        "java-debug patches), then point the target's bundle or PEEKY_JAVA_ADAPTER at it.",
        False,
    ),
    (
        r"^Inline assembly that can read a file",
        "UNSUPPORTED",
        "Take the directive (or assembler macro) out of the inline assembly; data it "
        "embeds can go in a declared source or header, as an array.",
        False,
    ),
    (
        r"C\+\+ targets need clang\+\+",
        "UNSUPPORTED",
        "Install clang and point PEEKY_NATIVE_CXX (or the target's `compiler`) at its "
        "clang++; C targets build with GCC as well.",
        False,
    ),
    (
        r"Stepping is not supported",
        "UNSUPPORTED",
        "Use `peeky session continue --to LINE` to stop at another line.",
        False,
    ),
    (
        r"Python return values cannot be captured",
        "UNSUPPORTED",
        "Capture the value where the caller keeps it: probe the line after the call with "
        "--capture of the variable it is assigned to.",
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
        r"^The debuggee did not start",
        "TIMEOUT",
        "The debuggee did not start: check that it can read its own path and every directory "
        "above it (a hung or slow file system, or a pending macOS privacy prompt, blocks it "
        "before it reaches the debugger), then retry. `cleanup` says whether Peeky ended the "
        "process it started.",
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


def _compiler_errors(stdout):
    """rustc's rendered error diagnostics from Cargo's JSON messages, in order."""
    found = []
    for line in split_lines(stdout):
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            continue
        diagnostic = message.get("message") if isinstance(message, dict) else None
        if (
            message.get("reason") == "compiler-message"
            and isinstance(diagnostic, dict)
            and diagnostic.get("level") == "error"
            and isinstance(diagnostic.get("rendered"), str)
        ):
            found.append(diagnostic["rendered"].strip())
    return found


def _build_failure(exc):
    """BUILD_FAILED with the compiler's own words, for a build command that failed."""
    from peeky.build import BuildCommandError

    if not isinstance(exc, BuildCommandError):
        return None
    result, tool = exc.result, (exc.command[0].rsplit("/", 1)[-1] if exc.command else "build")
    if result.get("timed_out"):
        what = f"{tool} timed out"
    elif result.get("output_limit_exceeded"):
        what = f"{tool} wrote more output than Peeky keeps"
    else:
        what = f"{tool} exited with code {result.get('exit_code')}"
    output = str(result.get("stderr") or result.get("stdout") or "").strip()
    if errors := _compiler_errors(str(result.get("stdout") or "")):
        # Cargo writes rustc's diagnostics to stdout as JSON and only a footer to stderr.
        footer = [
            line.strip()
            for line in split_lines(str(result.get("stderr") or ""))
            if line.strip() and not line.strip().startswith("Compiling ")
        ]
        output = "\n".join([*errors, *footer])
    if len(output) > 1500:
        output = output[:1500] + "\n... (output truncated)"
    return {
        "code": "BUILD_FAILED",
        "message": f"The build failed: {what}" + (f"\n{output}" if output else ""),
        "hint": "Fix the error the compiler reports and run again. With --old/--new it is the "
        "edit that does not build; your files were not changed.",
        "retryable": False,
    }


_FIX_ARGUMENTS = "Fix the request arguments; see `peeky <command> --help`."


# The one action each MUTATION_REJECTED rule leaves (sixth evaluation, finding 6): the
# message names the rule, so the hint need not repeat every rule. First match wins.
_MUTATION_HINTS = (
    (
        r"has already copied (?:its value|`[^`]+`) into",
        "Change it before the program copies it (a stop on an earlier line, or `--set-at` "
        "there), or change the copy the message names instead.",
    ),
    (
        r"assigns it again \(lines? [0-9, ]+\) before reading it",
        "Change it after that assignment has run: a later `--at`, or `session continue "
        "--to LINE` to the line after it.",
    ),
    (
        r"cannot be changed here",
        "Change it at the statement's first instruction: a breakpoint on the statement's "
        "first line (`--at`, `session continue --to LINE`), or a `session step` that lands "
        "there.",
    ),
    (
        r"in a caller's frame",
        "Change it in its own frame, before the call under way: `probe --at <its line> "
        "--hits N --set-hit N`, N counting that line's stops (`--capture <x> --hits all`).",
    ),
    (
        r"has one caller: \S+:[0-9]+ in ",
        "Change the value where it is owned, before the call the message names: as it "
        "says where the value is settable there, else at a statement's first line before it.",
    ),
    (
        r"is a (?:pointer|reference)\b(?!;)",
        "Change the value where it is owned, before the call: `probe --at <call line> --hits "
        "N --set-hit N`, N counting that line's stops (`--capture <arg> --hits all` there).",
    ),
    (
        r"is reached through a pointer or reference",
        "Change the value where it is owned (the struct a structured binding `auto &[a, b] "
        "= s` refers to, in the frame that holds it), or change the source with a candidate "
        "edit (`--old`/`--new`).",
    ),
    (
        r"another binding of|the binding of `[^`]+` declared on line",
        "Keep every binding of the name in memory with a candidate edit (`--old`/`--new`): "
        "`std::hint::black_box(&mut x);` after each `let` the message names, or rename one "
        "of them, and `--set` it in that same probe.",
    ),
    (
        r"inlined where it is called",
        "Inlining prevents the change: change the value in the caller, or drop "
        "`#[inline(always)]` with a candidate edit (`--old`/`--new`).",
    ),
    (
        r"held in the async body's state",
        "Change what it is computed from before it is assigned, or copy it into a local of "
        "the poll's own after the `.await` with a candidate edit (`--old`/`--new`) and "
        "`--set` that local in the same probe.",
    ),
    (
        r"kept in a register",
        "Keep it in memory with a candidate edit (`std::hint::black_box(&mut x);` after its "
        "`let`, by `--old`/`--new`) and `--set` it in that same probe.",
    ),
    (r"optimized build|unoptimized build", "Use an unoptimized build to change values."),
    (
        r"no value can be changed in this program",
        "Observe the value instead, or change the source with a candidate edit "
        "(`--old`/`--new`) and verify it.",
    ),
    (r"outside the range", "Pass a value within the range the message gives."),
    (
        r"declared at line|would overwrite a change|not prove .* initialized|not yet assigned"
        r"|has no readable value here|no value here",
        "Change it where it holds a value: stop after its declaration or assignment has run "
        "(a later `--at`, or `session continue --to LINE`).",
    ),
    (
        r"predates field and element changes|predates String changes"
        r"|with an adapter bundle built by this version",
        "Rebuild the Java adapter bundle with scripts/setup_java_adapter.py.",
    ),
    (
        r"the text does not fit",
        "Pass a shorter text (the message says how much fits), or change the source with a "
        "candidate edit (`--old`/`--new`).",
    ),
    (
        r"cannot write memory|cannot read memory|gives no address for",
        "This debugger cannot make this change: use lldb-dap 21 or later, or change the "
        "source with a candidate edit (`--old`/`--new`).",
    ),
    (
        r"does not write through",
        "Change the text where it is owned (a char array, std::string or String local), or "
        "change the source with a candidate edit (`--old`/`--new`).",
    ),
    (
        r"Java mutation requires",
        "Pass a literal of the variable's Java type: an integer, true or false, a number, a "
        "char in single quotes ('+') or a code point for a char, a text in double quotes for "
        "a String.",
    ),
    (
        r"literal|bounded decimal integer|numeric or boolean scalar",
        "Pass a literal value of the variable's type (C, C++ and Rust: a decimal integer, or "
        "text in double quotes for a string).",
    ),
)


def _mutation_hint(message):
    return next(
        (hint for pattern, hint in _MUTATION_HINTS if re.search(pattern, message)),
        next(h for _, c, h, _ in _RULES if c == "MUTATION_REJECTED"),
    )


def _refusal(code):
    """(hint, retryable) for a Refused code, from the rule that describes that refusal."""
    if code == "INVALID_REQUEST":
        return _FIX_ARGUMENTS, False
    marker = {"MUTATION_REJECTED": "cannot be changed", "NOT_FOUND": "not a visible local"}
    return next(
        ((h, r) for p, c, h, r in _RULES if c == code and marker.get(code, "") in p),
        (_FIX_ARGUMENTS, False),
    )


class Refused(ValueError):
    """A refusal whose code is fixed: the selector it quotes never decides it."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def classify(exc, *, session_closed=None):
    """Map an exception to the protocol error object.

    An error that ended a session says what became of the processes it started
    (``cleanup``, #143): "terminated pid N", "nothing Peeky could identify was still running", or
    why not.
    """
    payload = _classify(exc, session_closed)
    if isinstance(cleanup := getattr(exc, "cleanup", None), str) and "cleanup" not in payload:
        payload = {**payload, "cleanup": cleanup[:300]}
    return payload


def _classify(exc, session_closed):
    message = str(exc) or type(exc).__name__
    if isinstance(exc, PeekyError):
        return exc.payload
    closed = (
        isinstance(exc, (RuntimeError, TimeoutError, OSError))
        if session_closed is None
        else session_closed
    )
    if failure := _build_failure(exc):
        return {**failure, "session_closed": True}
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
    if isinstance(exc, Refused):
        code, (hint, retryable) = exc.code, _refusal(exc.code)
        if code == "MUTATION_REJECTED":
            hint = _mutation_hint(message)
        return {
            "code": code,
            "message": message[:2000],
            "hint": hint,
            "retryable": retryable,
            "session_closed": bool(closed),
        }
    for pattern, code, hint, retryable in _RULES:
        if re.search(pattern, message):
            if code in ("UNREACHABLE", "TRANSPORT_LOST"):
                # A client-side transport failure never closes the worker's session.
                closed = False
            if code == "MUTATION_REJECTED":
                hint = _mutation_hint(message)
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
            code, hint, retryable = "INVALID_REQUEST", _FIX_ARGUMENTS, False
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
