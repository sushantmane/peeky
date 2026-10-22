"""The tenth agent evaluation's C finding (prep 9, seen in Rust): a refused field change
through a pointer that is a local, not a parameter, said to change it "before the call
that passed it", though no call passed it."""

import json
import os

import pytest

from peeky.cli import main

SOURCE = """#include <stdio.h>

struct acc {
    int total;
    int count;
};

static int add(struct acc *a, int v) {
    a->total += v; /* PARAM */
    return a->total;
}

int main(int argc, char **argv) {
    struct acc box = {0, 0};
    struct acc *p = &box;
    for (int i = 1; i < argc; i++) {
        p->count += 1; /* LOCAL */
    }
    printf("%d %d\\n", add(&box, argc), box.count); /* CALL */
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
