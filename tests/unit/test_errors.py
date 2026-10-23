"""Error classification: codes and hints."""

import pytest

from peeky.errors import Refused, classify


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
            # #279: not "Stop on the statement's first line", where this stop already is.
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
            "'a' cannot be changed here: Peeky cannot tell whether the stop is past its "
            "function's prologue, which stores the parameters: the debugger did not report "
            "where the program is loaded",
            "argument at the call",
        ),
    ],
)
def test_a_refused_change_hints_the_one_action_its_rule_leaves(message, action):
    # Sixth evaluation, finding 6: the hint repeated about 80 words of general rules.
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
    # #261: the hint ended "rows marked [settable] can be changed here", right after the
    # debugger refused a row marked [settable]. What it quotes never picks the hint.
    for error in (ValueError(message), Refused("MUTATION_REJECTED", message)):
        payload = classify(error)
        assert payload["code"] == "MUTATION_REJECTED", payload
        assert "can be changed here" not in payload["hint"], payload
        assert "no [settable] mark can promise" in payload["hint"], payload
        assert len(payload["hint"]) <= 150, payload["hint"]


def test_a_missing_name_hints_what_reads_and_changes_take():
    # Review of #253: the NOT_FOUND hint keeps the scope of a change, the frame's own
    # locals and, in Java, Rust, C and C++, their fields and elements.
    payload = classify(ValueError("'nope' is not a visible local"))
    assert payload["code"] == "NOT_FOUND", payload
    assert "changes take the frame's own locals" in payload["hint"], payload["hint"]
    assert "in Java, Rust, C and C++ their fields and elements" in payload["hint"], payload
    assert len(payload["hint"]) <= 150, payload["hint"]
