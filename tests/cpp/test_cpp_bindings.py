"""Locals the frame reaches through a pointer or reference are never changed, as roots
or by their fields (#186's review, after round 1): clang describes a structured binding
of a reference (`auto &[at, dropped] = *owner`) as a variable of the member's own type,
at a location read through the hidden reference (`DW_OP_fbreg -16, DW_OP_deref`)."""

import contextlib
import json
import os

import pytest

from peeky.cli import main

BINDINGS = """#include <cstdio>
struct Pt { int x; int y; };
struct Owner { Pt at; int dropped; };
static int bump(Owner *owner) {
    auto &[at, dropped] = *owner;
    auto [a, b] = Pt{3, 4};
    int total = dropped + at.x + a + b; // HERE
    return total;
}
int main() {
    Owner o{{1, 2}, 5};
    int t = bump(&o);
    std::printf("T %d %d %d\\n", t, o.dropped, o.at.x);
    return 0;
}
"""
THROUGH = "is reached through a pointer or reference"


@pytest.fixture
def cpp(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "bindings.cpp"
    source.write_text(BINDINGS)
    return source


def line_of(text, marker):
    return next(i for i, line in enumerate(text.splitlines(), 1) if marker in line)


@contextlib.contextmanager
def session(source, line, capsys):
    assert main(["session", "start", str(source), "--at", str(line)]) == 0
    capsys.readouterr()
    try:
        yield
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(180)
def test_a_structured_binding_of_a_reference_is_never_changed(cpp, capsys):
    # `dropped` showed `[settable]`, and `--set dropped=50` wrote `o.dropped` in `main`
    # through the hidden reference (the program printed `T 58 50 1`).
    line = line_of(BINDINGS, "// HERE")
    probe = ["probe", str(cpp), "--at", str(line), "--json"]
    assert main([*probe, "--capture", "dropped,at.x,a,b"]) == 0
    rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    assert rows["dropped"]["value"] == "5" and rows["dropped"].get("settable") is not True, rows
    assert rows["at.x"].get("settable") is not True, rows["at.x"]
    assert rows["at.x"].get("not_settable") == "reference", rows["at.x"]
    # A by-value binding is the frame's own (`DW_OP_breg31 WSP+16, DW_OP_plus_uconst 4`).
    assert rows["a"].get("settable") is True and rows["b"].get("settable") is True, rows
    for assignment in ("dropped=50", "at.x=50"):
        code = main([*probe, "--capture", "dropped", "--set", assignment])
        result = json.loads(capsys.readouterr().out)
        error = result["error"]
        # Refused before the program runs: nothing ran.
        assert code != 0 and error["code"] == "MUTATION_REJECTED", result
        assert THROUGH in error["message"] and "before the program runs" in error["message"]
        assert "T " not in json.dumps(result), result
    assert main([*probe, "--capture", "b", "--set", "b=40"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "T 49 5 1" in result["output"], result["output"]
    # In a session, at the stop: the mark, and the refusal with the same reason.
    with session(cpp, line, capsys):
        assert main(["session", "get", "dropped", "at.x", "--json"]) == 0
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
        assert rows["dropped"].get("settable") is not True, rows["dropped"]
        assert rows["at.x"].get("not_settable") == "reference", rows["at.x"]
        for assignment in ("dropped=50", "at.x=50"):
            code = main(["session", "set", assignment, "--json"])
            shown = json.loads(capsys.readouterr().out)
            assert code != 0 and THROUGH in shown["error"]["message"], shown
