"""Whose build's code a stop in a Rust workspace's analysed crate is in (ADR 0008, phase 2):
the crate's own, another crate's copy of its generic or inlined function, or one Peeky can't
tell, which is never read as the crate's. Without a real toolchain."""

import json

import pytest

from peeky.rust_symbols import UNREAD_UNIT, CodeBlock
from peeky.rust_workspace_session import _session_class

OWN = "h0123456789abcdef"
# `util::boxed`, as the binary's build compiled it for its own type: a symbol none of
# `util`'s units defines.
FRAME = {"name": "util::boxed::h00000000000000ff", "instructionPointerReference": "0x9000"}


def session(blocks, pc=0x9000):
    """A workspace session of `util` whose units define ``blocks``, stopped on line 196 of
    util/src/lib.rs at ``pc`` (None: the debugger gives no address)."""
    found = object.__new__(_session_class())
    found.crate_name = "util"
    # The prepared symbols' line table, as NativeSession keeps it.
    found._lines_data = json.dumps({"util/src/lib.rs": {"196": []}})
    found.code_blocks = blocks
    found._place = lambda frame: ("util/src/lib.rs", 196)
    found._unslid = lambda frame: pc
    # No macro's stop: the crate's own code is read as RustSession reads it.
    found.macro_stop = lambda frame: None
    return found


def own():
    return {OWN: (CodeBlock(parent=None, ranges=((0x1000, 0x1100),), names=()),)}


def test_a_stop_in_another_crates_copy_reads_nothing_and_the_crates_own_is_read():
    note = session(own()).macro_stop_note(FRAME)
    assert note.endswith(
        "in code another crate's build compiled (a generic or inlined function of `util`'s, "
        "for that crate's types). Peeky reads only `util`'s own build, which holds none of it. "
        "A stop where `util`'s own code runs it is read"
    )
    # In `util`'s own function, by its address (an inlined call's frame), or its symbol.
    assert session(own(), pc=0x1010).macro_stop_note(FRAME) is None
    assert session(own(), pc=None).macro_stop_note({**FRAME, "name": f"util::f::{OWN}"}) is None


def test_a_callers_address_is_its_call_not_where_it_returns_to():
    # The call is the function's last instruction: it returns to just past its end.
    assert session(own(), pc=0x1100).macro_stop_note(FRAME, 1) is None
    assert session(own(), pc=0x1100).macro_stop_note(FRAME, 0) is not None


@pytest.mark.parametrize(
    ("blocks", "pc", "why"),
    [
        (own(), None, "The debugger gives no address for the frame."),
        (
            {**own(), UNREAD_UNIT: None},
            0x9000,
            "Peeky couldn't read the debug information of one of `util`'s units, so it doesn't "
            "know where all of their code is.",
        ),
        (
            {UNREAD_UNIT: None},
            0x9000,
            "Peeky couldn't read the debug information of one of `util`'s units, so it doesn't "
            "know where all of their code is.",
        ),
    ],
    ids=["no-address", "one-unit-unread", "every-unit-unread"],
)
def test_a_stop_peeky_cant_tell_is_the_crates_own_reads_nothing(blocks, pc, why):
    note = session(blocks, pc).macro_stop_note(FRAME, 2)
    assert note == (
        "Peeky reads no value of frame 2 (util::boxed) here: the debugger shows the stop on line "
        "196 of util/src/lib.rs. Peeky can't tell whether that is `util`'s own build of the "
        f"code, or another crate's copy of a generic or inlined function. {why} A stop in a "
        "function Peeky finds among `util`'s is read: try a line where `util`'s own code calls "
        "this one"
    )


def test_a_stop_withheld_for_want_of_an_address_is_so_only_once_the_debugger_is_checked():
    checked = []
    found = session(own(), pc=None)
    found.validate_debugger = lambda dap, frame: checked.append(frame)
    # The stop's state: the address is read once the debugger is checked; still none there,
    # it is withheld.
    assert "gives no address" in found.macro_stop_state(None, FRAME)
    assert checked == [FRAME]
