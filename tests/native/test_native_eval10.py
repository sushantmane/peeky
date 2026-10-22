"""The tenth agent evaluation's C finding (prep 9, seen in Rust): a refused field change
through a pointer that is a local, not a parameter, said to change it "before the call
that passed it", though no call passed it. Review of #276: only a pointer proven a
parameter at the stop gets call advice: not a local shadowing a parameter, a static, or
a parameter that the function points elsewhere (round 2)."""

import json
import os

import pytest

from peeky.cli import main

SOURCE = """#include <stdio.h>

struct acc {
    int total;
    int count;
};

static struct acc global;

static int add(struct acc *a, int v) {
    a->total += v; /* PARAM */
    return a->total;
}

static int shadow(struct acc *a) { /* SIGNATURE */
    a->count += 1; /* PARAM_USE */
    struct acc own = {0, 0};
    {
        struct acc *a = &own; /* INNER */
        a->count += 2; /* SHADOWED */
    }
    static struct acc *kept = &global;
    kept->count += 1; /* STATIC */
    return a->count + own.count + kept->count;
}

static int redirect(struct acc *a) {
    int first = a->count;
    a = &global;
    a->count += first; /* REDIRECTED */
    return a->count;
}

int main(int argc, char **argv) {
    struct acc box = {0, 0};
    struct acc *p = &box;
    for (int i = 1; i < argc; i++) {
        p->count += 1; /* LOCAL */
    }
    printf("%d %d\\n", add(&box, argc), box.count); /* CALL */
    printf("%d\\n", shadow(&box)); /* SHADOW_CALL */
    printf("%d\\n", redirect(&box)); /* REDIRECT_CALL */
    return 0;
}
"""


@pytest.fixture
def program(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "acc.c"
    path.write_text(SOURCE)
    return path


def line_of(marker):
    return next(i for i, text in enumerate(SOURCE.splitlines(), 1) if f"/* {marker} */" in text)


def refusal(program, capsys, marker, assignment):
    at = f"acc.c:{line_of(marker)}"
    code = main(
        ["probe", str(program), "--at", at, "--capture", "argc", "--set", assignment, "--json"]
        + ["--arg", "x", "--arg", "y"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result.get("refused_before_launch") is True, result
    return result["error"]


@pytest.mark.timeout(120)
def test_a_local_pointers_field_says_no_call_passed_it(program, capsys):
    error = refusal(program, capsys, "LOCAL", "p.count=5")
    assert error["code"] == "MUTATION_REJECTED", error
    message = error["message"]
    assert "`p` is a local here, not a parameter, so no call passed it" in message, message
    assert "before the call that passed it" not in message, message
    assert "call" not in error["hint"], error["hint"]
    # The struct it points at is `main`'s own, and changes as usual.
    at = f"acc.c:{line_of('LOCAL')}"
    code = main(
        ["probe", str(program), "--at", at, "--capture", "box.count", "--set", "box.count=5"]
        + ["--arg", "x", "--arg", "y", "--json", "--no-save"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["interventions"][0]["after"] == "5", result


@pytest.mark.timeout(120)
def test_a_pointer_parameter_still_names_its_caller(program, capsys):
    error = refusal(program, capsys, "PARAM", "a.total=5")
    message = error["message"]
    assert f"add has one caller: acc.c:{line_of('CALL')} in main" in message, message
    assert "not a parameter" not in message, message


NO_CALL = "if what it points at is also a root local in some frame, change it there"


@pytest.mark.timeout(120)
def test_a_local_shadowing_a_parameter_is_not_taken_for_it(program, capsys):
    error = refusal(program, capsys, "SHADOWED", "a.count=5")
    message = error["message"]
    assert f"`a` is a local here, not a parameter, so no call passed it: {NO_CALL}" in message
    assert "one caller" not in message and "before the call" not in message, message
    # The parameter above the inner block, and named by its declaration inside it: no
    # call either. Peeky reads the body by name, and `struct acc *a = &own;` reads as
    # pointing `a` elsewhere: it can't prove the parameter still holds what was passed.
    for marker, root in (("PARAM_USE", "a"), ("SHADOWED", f"a @ acc.c:{line_of('SIGNATURE')}")):
        message = refusal(program, capsys, marker, f"{root}.count=5")["message"]
        assert f"`{root}` isn't proven a parameter here, so Peeky names no call" in message
        assert "before the call" not in message, (marker, message)


@pytest.mark.timeout(120)
def test_a_parameter_pointed_elsewhere_gets_no_call_claim(program, capsys):
    # Review round 2 of #276: `a = &global;` before the stop, and `a` was still called a
    # parameter, with the caller's line.
    error = refusal(program, capsys, "REDIRECTED", "a.count=5")
    message = error["message"]
    assert f"`a` isn't proven a parameter here, so Peeky names no call: {NO_CALL}" in message
    assert "one caller" not in message and "before the call" not in message, message
    assert "call" not in error["hint"], error


@pytest.mark.timeout(120)
def test_a_static_pointer_gets_no_call_claim(program, capsys):
    error = refusal(program, capsys, "STATIC", "kept.count=5")
    message = error["message"]
    assert f"`kept` isn't proven a parameter here, so Peeky names no call: {NO_CALL}" in message
    assert "before the call" not in message and "call" not in error["hint"], error


@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    ("marker", "named", "said"),
    [
        ("SHADOWED", "INNER", "is a local here, not a parameter, so no call passed it"),
        ("SHADOWED", "SIGNATURE", "isn't proven a parameter here, so Peeky names no call"),
        ("REDIRECTED", None, "`a` isn't proven a parameter here, so Peeky names no call"),
        ("LOCAL", None, "`p` is a local here, not a parameter, so no call passed it"),
    ],
)
def test_a_sessions_set_through_a_pointer_says_what_the_binding_is(
    program, capsys, marker, named, said
):
    at = str(line_of(marker))
    assert main(["session", "start", str(program), "--at", at, "--arg", "x"]) == 0
    capsys.readouterr()
    root = f"a @ acc.c:{line_of(named)}" if named else "p" if marker == "LOCAL" else "a"
    try:
        assert main(["session", "set", f"{root}.count=5", "--json"]) == 2
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "MUTATION_REJECTED", error
        assert said in error["message"], error
        if "before the call" not in said:
            assert "before the call" not in error["message"], error
    finally:
        main(["session", "close"])
        capsys.readouterr()
