"""Stable, machine-readable errors for agents.

Every failure carries a `code` an agent can branch on, a `hint` saying what to do
next, whether retrying the same request can help, and whether the session was
closed. Codes are part of the protocol; messages are for humans and may change.
"""

import contextlib
import io
import json
import re
from urllib.error import HTTPError

from peeky.textlines import split_lines

# 4: a probe's `--set-at` value `@before` restores the value from before a change;
# the worker's `launch` takes `entry_lines` and its `set_variable` `restorable` and
# `target`, and returns `restore`. A protocol 3 worker would take `@before` for a value.
# 5: the worker withholds what a C or C++ pointer one past an array's end points
# at (`past_end`); a protocol 4 worker reads `end[0]` and LLDB's text there as values.
# 6: what is read through a C or C++ pointer Peeky can't show points at a live
# object is marked `unproven`, with why (`unproven_read`); a protocol 5 worker shows it
# as a plain value, and a protocol 5 client would take the mark for a Rust pointer's.
# 7: a C or C++ pointer's value extent says `indirect` where it points at a
# pointer; a protocol 6 worker leaves it out, so `pp[0].uses` of an `Item **pp` would be
# said to be read at the address `pp`'s alias holds.
# 8: a GCC lambda's captures come in a `Captures` scope, and a stop says why each
# one it doesn't list isn't read (`closure_unread`); a protocol 7 worker reads none, and
# a protocol 7 client would say lldb-dap doesn't list one where Peeky read none.
# 9: a probe's `1:i` reads `i` in caller frame 1, and its rows say `frame` and
# `stop_line`; a protocol 8 worker refuses `1:i` as no selector.
# 10: `expand` takes `key`, the entry of that key among those Peeky read from an
# unordered container's nodes, past 256 entries too; a protocol 9 worker ignores `key`
# and gives the first entries, which a client would take for the key's. Each request
# names its client's protocol (`X-Peeky-Protocol`), which the worker checks before it
# runs anything but `version`.
# 11: a client sends a worker nothing, and not its token, before the worker answers its
# `hello` with a proof that it holds the token (client.hello_proof). A protocol 10 worker
# has no `hello`: it asks for the token, so a client never learnt whether another process
# had taken the worker's port, and sent the token there.
# 12: a worker runs a Python program on its project's interpreter (the root's virtual
# environment, or the worker's `--python`), and names it in the launch's
# `target_identity`; a protocol 11 worker runs it on its own interpreter, and a protocol
# 11 client would not say which one ran it.
PROTOCOL_VERSION = 12

# (pattern on the message, code, hint, retryable). First match wins.
_RULES = (
    (
        # python_interpreter: refused before anything runs.
        r"^Peeky runs Python programs on CPython ",
        "UNSUPPORTED",
        "Pass --python with CPython 3.11 or later (the project's own), or --python peeky for "
        "Peeky's own.",
        False,
    ),
    (
        r"^The Python interpreter .* is a wrapper that changes how Python runs: ",
        "INVALID_REQUEST",
        "Pass the interpreter the wrapper runs as --python, and set its options in the "
        "environment (PYTHONOPTIMIZE, PYTHONPATH).",
        False,
    ),
    (
        r"^(--python .*: no executable file there|Peeky could not (run|ask) the Python "
        r"interpreter )",
        "INVALID_REQUEST",
        "Pass the program's interpreter with --python (such as .venv/bin/python), or "
        "--python peeky for Peeky's own.",
        False,
    ),
    (
        # A stop's read that timed out: before the rule that takes any "breakpoint".
        r"^A stop is never shown without (its line and function|the breakpoint it is at):",
        "TIMEOUT",
        "The debugger stalled at a stop. Start again once the machine is less busy; nothing "
        "of that stop was shown.",
        False,
    ),
    (
        r"^Evidence query failed: --schema has no ",
        "INVALID_QUERY",
        "Name a table, view or part the message lists, or --schema all for everything; "
        "`peeky sql --schema` alone lists them in short.",
        False,
    ),
    (
        r"^Evidence query failed: (no canned query |\w+ does not take |\w+ needs --param )",
        "INVALID_QUERY",
        "`peeky queries --help` lists each canned query's --param names: a and b pick two "
        "runs, run picks one.",
        False,
    ),
    (
        r"^Evidence query failed",
        "INVALID_QUERY",
        "Fix the SQL or the --param values: one read-only SELECT. `peeky sql --schema` lists "
        "the tables; `--schema NAME` gives one's columns.",
        False,
    ),
    (
        r"^Evidence store (?!needs SQLite )",
        "INVALID_REQUEST",
        "Run a probe first, or check --store. Move an unusable store aside (a new one is "
        "made), or pass --store PATH.",
        False,
    ),
    (
        r"is unreachable \(.*the request was not sent",
        "UNREACHABLE",
        "Restore the worker or SSH tunnel, and retry: it keeps the session until its idle "
        "timeout. If it is gone for good, run `peeky session close --forget`.",
        True,
    ),
    (
        r"did not prove it holds the worker's token",
        "UNAUTHORIZED",
        "Set PEEKY_WORKER_TOKEN to the worker's DEBUG_TOKEN. If it is, another program holds "
        "the port: stop it, or restart the worker.",
        False,
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
        "The protocol stream failed (a reply over 4 MiB, say) and closed the session: start "
        "a new one, with narrower selectors if values are huge.",
        False,
    ),
    (
        r"^java-debug's event thread died",
        "DEBUGGER_ERROR",
        "Retry. If it recurs, rebuild the adapter with `peeky setup-java`. The session is "
        "closed: an attached JVM is detached, a launched one ended.",
        False,
    ),
    (
        r"^Lost stop: ",
        "DEBUGGER_ERROR",
        "Save `peeky session report --json` for a bug report, then `peeky session close` and "
        "retry. The session's state cannot be trusted; the JVM runs on.",
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
        # A build whose symbols are over Peeky's fixed bounds (native.symbol_limit).
        r"^Peeky (?:reads|keeps) at most .* of a build's (?:debug information|line tables)",
        "BUDGET_EXHAUSTED",
        "Probe a smaller program: fewer source files or standard headers, or in Rust fewer "
        "std collections. These limits are fixed.",
        False,
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
        "Run `inspect` for names. Reads take dotted paths (a.b); changes take the frame's own "
        "locals, and in Java, Rust, C and C++ their fields and elements.",
        False,
    ),
    (
        r"^At most \d+ interventions \(--set and --set-at together\)",
        "INVALID_REQUEST",
        "Split the changes over several probes, or make them in the source with a candidate "
        "edit (`--old`/`--new`).",
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
        r"^Peeky reads no value of frame \d+ .* the debugger shows the stop on line",
        "INVALID_REQUEST",
        "Nothing in that frame is read or changed here: `session step` goes on to your code's "
        "next line, and `--frame N` reads a caller.",
        False,
    ),
    (
        r"adapter bundle at .* (predates Peeky's java-debug fixes|is not what this Peeky's setup "
        r"builds)",
        "UNSUPPORTED",
        "Rebuild it with `peeky setup-java`; for a bundle the config names, add --output <a "
        "new directory> and point the config's bundle at it.",
        False,
    ),
    (
        r"^The Java adapter runs only on the java that its bundle was built with: ",
        "UNSUPPORTED",
        "Nothing was launched or attached. Drop the config's java or set it to the one the "
        "message names, or rebuild with `peeky setup-java`.",
        False,
    ),
    (
        r"^A Java adapter on JDK 19 or later launches a program only on JDK 19 or later",
        "UNSUPPORTED",
        "Nothing was launched. Rebuild the bundle with JDK 17 first on PATH (`peeky setup-java`), "
        "or set the config's java to the adapter's.",
        False,
    ),
    (
        r"^Peeky runs a Java program on another JDK than the adapter's only from that JDK's "
        r"own bin/java",
        "UNSUPPORTED",
        "Nothing was launched. Put the JDK's own bin directory first on PATH, or set the "
        "config's java to the one the message names.",
        False,
    ),
    (
        # A backstop for what JDK checks miss: java-debug's own launch failure.
        r"VM initialization failed for: .*includevirtualthreads=",
        "UNSUPPORTED",
        "The adapter's JDK, 19 or later, can't start a JVM before 19. Rebuild the bundle with "
        "JDK 17 first on PATH, or run the program on the adapter's JDK.",
        False,
    ),
    (
        r"^No Java adapter bundle at ",
        "INVALID_REQUEST",
        "Build it with the `peeky setup-java` command the message gives; it needs JDK 17 or "
        "later, and access to GitHub and Maven Central.",
        False,
    ),
    (
        r"^Peeky's own Java adapter can't be used: ",
        "UNSUPPORTED",
        "Nothing ran. Do what the message says, or set PEEKY_STATE_DIR to a directory only you "
        "can use and run `peeky setup-java`.",
        False,
    ),
    (
        r"^JDK required: java and javac",
        "UNSUPPORTED",
        "Install JDK 17 or later, put its java and javac on PATH, and run `peeky setup-java` "
        "again.",
        False,
    ),
    (
        r"^Java adapter setup failed",
        "UNSUPPORTED",
        "Fix what the message names (network access to GitHub and Maven Central, or the JDK) "
        "and run it again. Report a checksum mismatch.",
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
        r"C\+\+ targets need clang\+\+ or g\+\+",
        "UNSUPPORTED",
        "Point PEEKY_NATIVE_CXX (or the target's `compiler`) at clang++ or g++.",
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
        r"^Evidence store needs SQLite ",
        "UNSUPPORTED",
        "Use a Python whose sqlite3 module has SQLite 3.30 or newer with JSON; "
        "python3 -c 'import sqlite3; print(sqlite3.sqlite_version)' shows it.",
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
        "Retry once it can read its path and every directory above it: a hung disk or macOS "
        "privacy prompt blocks it. `cleanup` says if Peeky ended it.",
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


# How long an error's text may be, as sent: its message, a build's output in it, and its
# cleanup. A worker's probe child cuts its errors at RELAYED_CHARS instead (``relayed``):
# the worker removes its token from them, which the child doesn't hold, then cuts them to
# these (``bound``). RELAYED_CHARS is far beyond these and any token's quoted form.
MESSAGE_CHARS, BUILD_OUTPUT_CHARS, CLEANUP_CHARS = 2000, 1500, 300
RELAYED_CHARS = 65536
_relayed = False


@contextlib.contextmanager
def relayed():
    """Within it, ``classify`` cuts no text below RELAYED_CHARS: in a worker's probe child,
    whose errors the worker bounds (``bound``) once it has removed its token."""
    global _relayed
    before, _relayed = _relayed, True
    try:
        yield
    finally:
        _relayed = before


def _limit(chars):
    return RELAYED_CHARS if _relayed else chars


def _cut_output(output, chars):
    return output[:chars] + "\n... (output truncated)" if len(output) > chars else output


def bound(error, secret=None):
    """An error object a worker's probe child sent (``relayed``), with ``secret`` removed
    from its text first, then its text cut as ``classify`` cuts it."""
    error = dict(redact(error, secret))
    message = error.get("message")
    if isinstance(message, str) and error.get("code") == "BUILD_FAILED":
        head, newline, output = message.partition("\n")
        error["message"] = head + newline + _cut_output(output, BUILD_OUTPUT_CHARS)
    elif isinstance(message, str):
        error["message"] = message[:MESSAGE_CHARS]
    if isinstance(error.get("cleanup"), str):
        error["cleanup"] = error["cleanup"][:CLEANUP_CHARS]
    return error


def _build_failure(exc, secret=None):
    """BUILD_FAILED with the compiler's own words, for a build command that failed.
    ``secret``: the worker's token, removed before the words are cut (``redact``)."""
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
    output = _cut_output(redact(output, secret), _limit(BUILD_OUTPUT_CHARS))
    return {
        "code": "BUILD_FAILED",
        "message": f"The build failed: {what}" + (f"\n{output}" if output else ""),
        "hint": "Fix the error the compiler reports and run again. With --old/--new it is the "
        "edit that does not build; your files were not changed.",
        "retryable": False,
    }


_FIX_ARGUMENTS = "Fix the request arguments; see `peeky <command> --help`."

# Every code Peeky sends: those of the rules, a build's failure, a probe result's no-stop or
# crash, the worker's token check, and the fallback for an unexpected exception. The worker's
# `version` lists them; test_errors checks that each code the sources send is here.
ERROR_CODES = frozenset(code for _, code, _, _ in _RULES) | {
    "BUILD_FAILED",
    "NO_STOP",
    "PROGRAM_CRASHED",
    "UNAUTHORIZED",
    "INTERNAL_ERROR",
}


# The one action each MUTATION_REJECTED rule leaves: the
# message names the rule, so the hint need not repeat every rule. First match wins.
_MUTATION_HINTS = (
    (
        # A `@before` restore Peeky cannot vouch for.
        r"@before",
        "Give the value itself at each hit (--set-at LINE@HIT name=VALUE), or try the change "
        "as a candidate edit (--old/--new) and verify it.",
    ),
    (
        # Python's field or element `--set`: Python shows no [settable].
        r"which only Java, Rust, C and C\+\+ targets allow"
        r"|can be changed for this language \(fields and elements cannot\)",
        "Set a local of the stopped function instead, or add one with a candidate edit "
        "(`--old`/`--new`) and set it in the same probe.",
    ),
    (
        # The debugger's own refusal, whatever it quotes: a row's mark can't foresee it.
        r"[Tt]he debugger refused to change",
        "Observe the value, or edit the source (`--old`/`--new`) and verify it. The debugger "
        "itself refused this write, which no [settable] mark can promise.",
    ),
    (
        # A static or thread-local may keep what was computed from it, out of reach.
        r"a static or thread-local may hold state derived from",
        "Change it before the program uses it (a stop on an earlier line, or `--set-at` there).",
    ),
    (
        # A static or thread-local may keep the copy, which no change Peeky makes reaches.
        r"a static or thread-local may hold a copy of",
        "Change it before the program copies it (a stop on an earlier line, or `--set-at` there).",
    ),
    (
        # A binding a call returned may hold a copy or only point into it.
        r"may hold a copy of (?:it|`[^`]+`) that it goes on with, unless [^.:]* only points? "
        r"into it",
        "Change it before the program copies or borrows it (a stop on an earlier line, or "
        "`--set-at` there).",
    ),
    (
        # What a binding keeps behind a cell may be computed from it, no copy.
        r"may hold state derived from (?:it|`[^`]+`) that it goes on with",
        "Change it before the program uses it (a stop on an earlier line, or `--set-at` there).",
    ),
    (
        # A binding that may keep the copy behind a cell or a channel, out of reach.
        r"may hold a copy of (?:it|`[^`]+`) that it goes on with, which may be out of Peeky's "
        r"reach",
        "Change it before the program copies it (a stop on an earlier line, or `--set-at` there).",
    ),
    (
        # A copy already made, or a binding's copy that a call since may have changed.
        r"has already copied (?:its value|`[^`]+`) into"
        r"|^(?:(?!statement under way).)*may hold a copy of (?:it|`[^`]+`) that it goes on with",
        "Change it before the program copies it (a stop on an earlier line, or `--set-at` "
        "there), or change the copy the message names instead.",
    ),
    (
        # A statement under way, or else a binding's copy: no one copy to change instead.
        r"statement under way .*may hold a copy of (?:it|`[^`]+`) that it goes on with",
        "Change it before the program copies it (a stop on an earlier line, or `--set-at` there).",
    ),
    (
        # Rust: no path from the stop reads it before it is overwritten or ends.
        r"(?:does|may) not read (?:it|`[^`]+`) after (?:this stop|any stop of line [0-9]+), so a "
        r"change",
        "Change it where the program still reads it: after the line that overwrites it, or "
        "before its last read (a stop on that line, or `--set-at` there).",
    ),
    (
        r"assigns it again \(lines? [0-9, ]+\) before reading it",
        "Change it after that assignment has run: a later `--at`, or `session continue "
        "--to LINE` to the line after it.",
    ),
    (
        r"Change it before that statement starts",
        "Change it before the statement under way starts: a stop on an earlier line (`--at`, "
        "`session continue --to LINE`), or an earlier stop of this line (`--set-hit`), where "
        "it is [settable].",
    ),
    (
        # A stop in a function's prologue (a GCC build's one-line function).
        r"before (?:the prologue stores|it has stored) the parameters"
        r"|whether the stop is before or after its function's prologue",
        "Change the argument at the call (a stop on the caller's line), or at a stop past the "
        "function's prologue, as the message says.",
    ),
    (
        # A frame whose function Peeky cannot place (GCC's lambdas, a `#line`).
        r"cannot place this function's declarations",
        "Change the value in a function `peeky describe` lists (the caller, before the call), "
        "or edit the source with `--old`/`--new`.",
    ),
    (
        # Code inlined just before the stop that Peeky can't place.
        r"code may already have read `[^`]+` before this stop: .*Peeky cannot tell",
        "Change it at a stop on an earlier line (`--at`, `session continue --to LINE`), "
        "where `inspect` marks it [settable].",
    ),
    (
        # Code the build inlined from a call on the line ran before the breakpoint.
        r"code may already have read `[^`]+` before this stop",
        "Change it at a stop on an earlier line (`--at`, `session continue --to LINE`), or "
        "`session step` onto the line from the one before it.",
    ),
    (
        r"cannot be changed here",
        "Stop on the statement's first line (`--at`, `session continue --to LINE`), or "
        "`session step` to it.",
    ),
    (
        # A GCC lambda's capture: the lambda's own copy, or what it refers to.
        r"is a capture of this lambda",
        "Change it where the message says (the lambda's own copy, or what a capture by "
        "reference refers to), or edit the source with `--old`/`--new`.",
    ),
    (
        # A structured binding by reference: no call passed it.
        r"is a structured binding by reference",
        "Change what the binding refers to where it is [settable], or what fills it, before "
        "this line; or edit the source with `--old`/`--new`.",
    ),
    (
        r"in a caller's frame",
        "Change it in its own frame, before the call: `--at <its line> --hits N --set-hit "
        "N`, N counting that line's stops.",
    ),
    (
        r"lists the fields each function can set",
        "Change the field where the struct is held, before the calls that lead here: "
        "`--set-at LINE@N` (`describe`'s `mutable:` line names the function).",
    ),
    (
        r"which Peeky does not change \(it changes only locals a frame holds\)",
        "Change what sets it with a candidate edit (`--old`/`--new`).",
    ),
    (
        r"has one caller: \S+:[0-9]+ in ",
        "Change the value in the caller the message names, before that call, as the message says.",
    ),
    (
        r"an element of a slice or Vec, which Peeky never changes",
        "Edit the source with `--old`/`--new`: Peeky never changes a slice's or Vec's elements.",
    ),
    (
        # A pointer not proven a parameter: no call is named.
        r"not a parameter, so no call passed it|proven a parameter here, so Peeky names no call",
        "If what it points at is a root local in some frame, change it there; otherwise edit "
        "the source with `--old`/`--new`.",
    ),
    (
        r"is a (?:pointer|reference)\b(?!;)",
        "Change the value where it is owned, before the call: `--at <call line> --hits N "
        "--set-hit N`, N counting that line's stops.",
    ),
    (
        r"is reached through a pointer or reference",
        "Change the value in the frame that owns it, or edit the source with `--old`/`--new`.",
    ),
    (
        r"another binding of|the binding of `[^`]+` declared on line",
        "With `--old`/`--new` in the same probe, add `std::hint::black_box(&mut x);` after "
        "each `let` the message names, or rename one of them.",
    ),
    (
        r"inlined where it is called",
        "Inlining prevents the change: change the value in the caller, or drop "
        "`#[inline(always)]` with a candidate edit (`--old`/`--new`).",
    ),
    (
        r"held in the async body's state",
        "Change what it is computed from, or copy it into a local after the `.await` with "
        "`--old`/`--new`, and `--set` that local in the same probe.",
    ),
    (
        r"kept in a register",
        "Keep it in memory with a candidate edit (`std::hint::black_box(&mut x);` after its "
        "`let`, by `--old`/`--new`) and `--set` it in that same probe.",
    ),
    (r"optimized build|unoptimized build", "Use an unoptimized build to change values."),
    (
        r"no location and no type of its own",
        "Print it from the program with a candidate edit (`--old`/`--new`): no stop of this "
        "build reads or changes it.",
    ),
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
        "Rebuild the Java adapter bundle with `peeky setup-java`.",
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
        "Pass a literal of the variable's Java type: a number, true or false, a char ('+') "
        "or code point, or a text in double quotes for a String.",
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


class UnplacedStop(ValueError):
    """An inspection's refusal of a frame the runtime can't place among its function's
    lines, of which nothing is read (the debugger shows a Rust stop inside a macro's
    definition). The session is kept. ``frame``: the frame, as an inspection lists it."""

    def __init__(self, message, frame=None):
        super().__init__(message)
        self.frame = dict(frame or {})


# What stands for the worker's token in the text of an error it sends (``redact``).
REDACTED_TOKEN = "[token removed]"


def redact(value, secret):
    """``value``, a JSON value such as an error object, with ``secret`` replaced in all its
    text, also as Python and JSON quote it. The worker's token is in no exception the worker
    raises, but a request may carry it: its error never sends it back."""
    if not secret:
        return value
    if isinstance(value, str):
        forms = {secret, repr(secret)[1:-1], json.dumps(secret)[1:-1]}
        for form in sorted(forms, key=len, reverse=True):
            value = value.replace(form, REDACTED_TOKEN)
        return value
    if isinstance(value, dict):
        return {redact(key, secret): redact(item, secret) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, secret) for item in value]
    return value


def classify(exc, *, session_closed=None, secret=None):
    """Map an exception to the protocol error object.

    An error that ended a session says what became of the processes it started
    (``cleanup``): "terminated pid N", "nothing Peeky could identify was still running", or
    why not. ``secret``: the worker's token, removed from every text (``redact``), before
    the message is cut to its bound too.
    """
    payload = _classify(exc, session_closed, secret)
    if isinstance(cleanup := getattr(exc, "cleanup", None), str) and "cleanup" not in payload:
        payload = {**payload, "cleanup": redact(cleanup, secret)[: _limit(CLEANUP_CHARS)]}
    return redact(payload, secret)


def _classify(exc, session_closed, secret=None):
    message = redact(str(exc) or type(exc).__name__, secret)
    if isinstance(exc, PeekyError):
        return exc.payload
    closed = (
        isinstance(exc, (RuntimeError, TimeoutError, OSError))
        if session_closed is None
        else session_closed
    )
    if failure := _build_failure(exc, secret):
        return {**failure, "session_closed": True}
    if isinstance(exc, FileNotFoundError):
        # A missing target, config, checker or tool: the request, not the debugger.
        return {
            "code": "INVALID_REQUEST",
            "message": message[: _limit(MESSAGE_CHARS)],
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
            "message": message[: _limit(MESSAGE_CHARS)],
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
        "message": message[: _limit(MESSAGE_CHARS)],
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
