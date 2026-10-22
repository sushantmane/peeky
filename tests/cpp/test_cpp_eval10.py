"""Review of #276 (tenth evaluation, prep 9): a refused change through a C++ pointer gets
the call advice only where the pointer is proven a parameter at the stop. A local that
shadows a parameter, and a static, get advice that claims no call."""

import json
import os

import pytest

from peeky.cli import main

SOURCE = """struct Acc {
    int total;
    int count;
};

static Acc global_acc;

static int shadow(Acc *a) { // SIGNATURE
    a->count += 1; // PARAM_USE
    Acc own{0, 0};
    {
        Acc *a = &own; // INNER
        a->count += 2; // SHADOWED
    }
    static Acc *kept = &global_acc;
    kept->count += 1; // STATIC
    return a->count + own.count + kept->count;
}

int main() {
    Acc box{0, 0};
    return shadow(&box) > 0 ? 0 : 1;
}
"""
NO_CALL = "if what it points at is also a root local in some frame, change it there"


@pytest.fixture
def program(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "acc.cpp"
    path.write_text(SOURCE)
    return path


def line_of(marker):
    return next(i for i, text in enumerate(SOURCE.splitlines(), 1) if text.endswith(f"// {marker}"))


def refusal(program, capsys, marker, assignment):
    at = f"acc.cpp:{line_of(marker)}"
    code = main(["probe", str(program), "--at", at, "--capture", "own", "--set", assignment])
    out = capsys.readouterr().out
    assert code == 2 and out.startswith("error [MUTATION_REJECTED]: "), out
    return out


@pytest.mark.timeout(180)
def test_only_a_proven_parameter_gets_the_call_advice(program, capsys):
    out = refusal(program, capsys, "SHADOWED", "a.count=5")
    assert f"`a` is a local here, not a parameter, so no call passed it: {NO_CALL}" in out, out
    assert "before the call" not in out, out
    out = refusal(program, capsys, "STATIC", "kept.count=5")
    assert f"`kept` isn't proven a parameter here, so Peeky names no call: {NO_CALL}" in out, out
    assert "before the call" not in out, out
    for marker, root in (("PARAM_USE", "a"), ("SHADOWED", f"a @ acc.cpp:{line_of('SIGNATURE')}")):
        out = refusal(program, capsys, marker, f"{root}.count=5")
        assert "before the call that passed it" in out, (marker, out)
        assert "not a parameter" not in out, (marker, out)


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    ("named", "said"),
    [
        ("INNER", "is a local here, not a parameter, so no call passed it"),
        ("SIGNATURE", "before the call that passed it"),
    ],
)
def test_a_sessions_set_says_what_the_shadowing_binding_is(program, capsys, named, said):
    assert main(["session", "start", str(program), "--at", str(line_of("SHADOWED"))]) == 0
    capsys.readouterr()
    try:
        root = f"a @ acc.cpp:{line_of(named)}"
        assert main(["session", "set", f"{root}.count=5", "--json"]) == 2
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "MUTATION_REJECTED" and said in error["message"], error
        if named == "INNER":
            assert "before the call" not in error["message"], error
    finally:
        main(["session", "close"])
        capsys.readouterr()
