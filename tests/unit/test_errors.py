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
    ],
)
def test_a_refused_change_hints_the_one_action_its_rule_leaves(message, action):
    # Sixth evaluation, finding 6: the hint repeated about 80 words of general rules.
    for error in (ValueError(message), Refused("MUTATION_REJECTED", message)):
        payload = classify(error)
        assert payload["code"] == "MUTATION_REJECTED" and action in payload["hint"], payload
        assert len(payload["hint"]) < 200, payload["hint"]


def test_a_missing_name_hints_what_reads_and_changes_take():
    # Review of #253: the NOT_FOUND hint keeps the scope of a change, the frame's own
    # locals and, in Java, Rust, C and C++, their fields and elements.
    payload = classify(ValueError("'nope' is not a visible local"))
    assert payload["code"] == "NOT_FOUND", payload
    assert "changes take the frame's own locals" in payload["hint"], payload["hint"]
    assert "in Java, Rust, C and C++ their fields and elements" in payload["hint"], payload
    assert len(payload["hint"]) <= 150, payload["hint"]
