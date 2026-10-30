"""Error classification: codes and hints."""

import re
from pathlib import Path

import pytest

import peeky
from peeky.errors import ERROR_CODES, Refused, classify


@pytest.mark.parametrize(
    ("message", "action"),
    [
        ("'s.a' cannot be changed here: the stop is part-way through line 9", "first line"),
        ("`ps.start` cannot be changed: `ps` is a pointer: Peeky changes only", "--set-hit N"),
        (
            "Rust mutation requires a local the program keeps in memory; 'width' in f is "
            "kept in a register and the debugger only shows a copy",
            "black_box",
        ),
        ("Native integer mutation is outside the range of `s.end` (int: 0 to 9)", "within the"),
        (
            "`later.end` cannot be changed: the build's MIR does not prove `later` "
            "initialized here",
            "stop after its",
        ),
        ("'s' in a caller's frame, whose values cannot be changed", "--set-hit N"),
        ("Changing values is refused in an optimized build (-O1)", "unoptimized build"),
        ("`f.ch` cannot be changed: `f.ch` is a character (char)", "candidate edit"),
        (
            # Not "Stop on the statement's first line", where this stop already is.
            "'a' cannot be changed here: the stop is at its function's start, before the "
            "prologue stores the parameters, and the function's code may overwrite a change",
            "argument at the call",
        ),
        (
            "'a' cannot be changed here: the stop is in its function's prologue, before it "
            "has stored the parameters, and the function's code may overwrite a change",
            "past the function's prologue",
        ),
        (
            "'a' cannot be changed here: Peeky cannot tell whether the stop is before or after "
            "its function's prologue stores the parameters: the debugger did not report where "
            "the program is loaded, so it cannot place the stop's instruction",
            "argument at the call",
        ),
    ],
)
def test_a_refused_change_hints_the_one_action_its_rule_leaves(message, action):
    # The hint repeated about 80 words of general rules.
    for error in (ValueError(message), Refused("MUTATION_REJECTED", message)):
        payload = classify(error)
        assert payload["code"] == "MUTATION_REJECTED" and action in payload["hint"], payload
        assert len(payload["hint"]) < 200, payload["hint"]


@pytest.mark.parametrize(
    "message",
    [
        "The debugger refused to change `ranking[1]` (Peeky experimental Java adapter: the "
        "array is in a list held by java.util.Collections$UnmodifiableList.list, a JDK "
        "class's field; Peeky never changes the JDK's own state); it is unchanged",
        "Intervention target 'xs[1]': The debugger refused to change `xs[1]` (in a caller's "
        "frame); it is unchanged",
    ],
)
def test_a_change_the_debugger_refused_promises_nothing_of_a_settable_mark(message):
    # The hint ended "rows marked [settable] can be changed here", right after the
    # debugger refused a row marked [settable]. What it quotes never picks the hint.
    for error in (ValueError(message), Refused("MUTATION_REJECTED", message)):
        payload = classify(error)
        assert payload["code"] == "MUTATION_REJECTED", payload
        assert "can be changed here" not in payload["hint"], payload
        assert "no [settable] mark can promise" in payload["hint"], payload
        assert len(payload["hint"]) <= 150, payload["hint"]


def test_a_missing_name_hints_what_reads_and_changes_take():
    # The NOT_FOUND hint keeps the scope of a change, the frame's own
    # locals and, in Java, Rust, C and C++, their fields and elements.
    payload = classify(ValueError("'nope' is not a visible local"))
    assert payload["code"] == "NOT_FOUND", payload
    assert "changes take the frame's own locals" in payload["hint"], payload["hint"]
    assert "in Java, Rust, C and C++ their fields and elements" in payload["hint"], payload
    assert len(payload["hint"]) <= 150, payload["hint"]


def test_a_python_sessions_field_set_hint_names_no_settable_rows():
    # The same refusal in a session, from the worker.
    error = classify(
        ValueError(
            "Only the stopped function's own locals can be changed for this language "
            "(fields and elements cannot)"
        )
    )
    assert error["code"] == "MUTATION_REJECTED" and "[settable]" not in error["hint"], error
    # A native debugger too old for field changes keeps the hint: its rows are marked.
    old = classify(
        ValueError(
            "Only the stopped function's own locals can be changed for this language with "
            "this debugger, which cannot change fields and elements"
        )
    )
    assert "[settable]" in old["hint"], old


def test_every_code_the_sources_send_is_one_peeky_lists():
    # The worker's `version` gives ERROR_CODES; a code sent outside the rules was left out.
    source = "\n".join(
        path.read_text() for path in sorted((Path(peeky.__file__).parent).rglob("*.py"))
    )
    sent = set(
        re.findall(
            r'(?:"code":\s*|Refused\(\s*|code, hint, retryable = \(?\s*)"([A-Z][A-Z_]+)"', source
        )
    )
    assert {"BUILD_FAILED", "NO_STOP", "PROGRAM_CRASHED", "UNAUTHORIZED", "INTERNAL_ERROR"} <= sent
    assert sent <= ERROR_CODES, sent - ERROR_CODES
