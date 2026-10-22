"""#202, on real builds: a refusal for a field through a C pointer parameter follows the
calls that pass the pointer on as is (main -> wrap -> fits) to the frame that owns the
struct, and names no frame where the change would be refused when it cannot."""

import json
import os

import pytest

from peeky.cli import main

# fits reads the width through the pointer wrap passes on from main, which owns it.
CHAIN = """#include <stdio.h>
struct layout { int width; int indent; };
struct stats { int lines; };
static int fits(const struct layout *lay, int column, int word) {
    return column + 1 + word <= lay->width; /* AT */
}
static void wrap(const struct layout *lay, const int *words, int count, struct stats *st) {
    int column = 0;
    st->lines = 1;
    for (int i = 0; i < count; i++) {
        if (column > 0 && fits(lay, column, words[i])) { /* PASS */
            column += 1 + words[i];
        } else {
            if (column > 0) st->lines++;
            column = lay->indent + words[i];
        }
    }
}
int main(void) {
    struct layout lay = { 12, 2 };
    struct stats st = { 0 };
    int words[] = { 3, 5, 4, 2, 6 };
    wrap(&lay, words, 5, &st); /* CALL */
    printf("LINES:%d\\n", st.lines);
    return 0;
}
"""

# Where the calls cannot be followed to a local the caller holds: the old text, the new.
UNFOLLOWED = {
    # wrap has two callers.
    "two_callers": (
        "    wrap(&lay, words, 5, &st); /* CALL */",
        "    struct layout other = { 8, 0 };\n"
        "    wrap(&lay, words, 5, &st); /* CALL */\n"
        "    wrap(&other, words, 5, &st);",
    ),
    # wrap may point `lay` at another struct before it passes it on.
    "repointed": (
        "    int column = 0;\n",
        "    int column = 0;\n    static const struct layout narrow = { 4, 0 };\n"
        "    if (count > 9) lay = &narrow;\n",
    ),
    # main passes a pointer it keeps, not the address of its local.
    "stored": (
        "    wrap(&lay, words, 5, &st); /* CALL */",
        "    const struct layout *kept = &lay;\n    wrap(kept, words, 5, &st); /* CALL */",
    ),
    # The struct main passes is a static, which no change reaches.
    "static": (
        "    struct layout lay = { 12, 2 };",
        "    static struct layout lay = { 12, 2 };",
    ),
}


@pytest.fixture
def c_program(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))

    def write(name, text):
        (tmp_path / name).write_text(text)
        return tmp_path / name

    return write


def line_of(text, marker):
    return next(i for i, line in enumerate(text.splitlines(), 1) if marker in line)


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr().out


@pytest.mark.timeout(300)
def test_the_refusal_follows_a_pointer_passed_on_to_the_frame_that_owns_it(c_program, capsys):
    # #202: the advice named wrap's line 40, where `lay` is a pointer too and a change is
    # refused as well; main, which owns the struct, is where it can be made.
    source = c_program("m.c", CHAIN)
    at, passed, call = (line_of(CHAIN, mark) for mark in ("/* AT */", "/* PASS */", "/* CALL */"))
    code, out = run(capsys, "probe", source, "--at", at, "--set", "lay.width=4")
    assert code == 2, out
    assert (
        f"fits has one caller: m.c:{passed} in wrap, which passes it on (`lay`, its own "
        f"pointer parameter); wrap has one caller: m.c:{call} in main, which passes `&lay`, "
        f"where `lay.width` is settable: a probe's `--set-at {call}@N lay.width=<value>`"
    ) in out, out
    # The advice holds: the change at main's call is what fits reads, and wrap folds
    # the words at the new width.
    code, out = run(
        capsys, "probe", source, "--at", at, "--capture", "lay.width", "--hits", "1",
        "--set-at", f"{call}@1", "lay.width=4", "--json",
    )  # fmt: skip
    result = json.loads(out)
    assert code == 0 and result["status"] == "completed", result.get("error")
    assert [(i["before"], i["after"]) for i in result["interventions"]] == [("12", "4")]
    assert [row["value"] for row in result["rows"]] == ["4"], result["rows"]
    assert result["output"].strip() == "LINES:5", result["output"]
    # At a stop in fits too, through a session.
    code, out = run(capsys, "session", "start", source, "--at", at)
    try:
        code, out = run(capsys, "session", "set", "lay.width=4", "--json")
        error = json.loads(out)["error"]
        assert code == 2 and error["code"] == "MUTATION_REJECTED", error
        assert f"wrap has one caller: m.c:{call} in main, which passes `&lay`" in error["message"]
    finally:
        run(capsys, "session", "close")


@pytest.mark.timeout(300)
def test_a_function_on_the_way_that_writes_through_the_pointer_gets_no_claim(c_program, capsys):
    # wrap may change the width before fits reads it: main is named, without `--set-at`.
    text = CHAIN.replace("const struct layout *lay, const int", "struct layout *lay, const int")
    text = text.replace("    st->lines = 1;\n", "    st->lines = 1;\n    lay->width -= 1;\n")
    source = c_program("m.c", text)
    at, call = line_of(text, "/* AT */"), line_of(text, "/* CALL */")
    code, out = run(capsys, "probe", source, "--at", at, "--set", "lay.width=4")
    message = out.splitlines()[0]
    assert code == 2, out
    assert (
        f"wrap has one caller: m.c:{call} in main, which passes `&lay`; change the value where "
        f"it is owned, before that call: Peeky cannot tell that a change at line {call} "
        "reaches it (a statement there may change the value, or make the call, first, and so "
        "may wrap, on the way)"
    ) in message, out
    assert "settable" not in message.split("has one caller")[1] and "--set-at" not in message


@pytest.mark.timeout(300)
@pytest.mark.parametrize("case", list(UNFOLLOWED))
def test_no_frame_is_named_for_the_change_where_the_calls_cannot_be_followed(
    c_program, capsys, case
):
    old, new = UNFOLLOWED[case]
    assert old in CHAIN
    text = CHAIN.replace(old, new)
    source = c_program("m.c", text)
    at, passed, call = (line_of(text, mark) for mark in ("/* AT */", "/* PASS */", "/* CALL */"))
    code, out = run(capsys, "probe", source, "--at", at, "--set", "lay.width=4")
    message = out.splitlines()[0]
    assert code == 2 and message.startswith("error [MUTATION_REJECTED]"), out
    # Never a change to make at a frame that would refuse it.
    assert "--set-at" not in message and "settable" not in message.split("one caller")[1], out
    elsewhere = (
        "change the value in the frame that owns the struct, before the calls that lead "
        "here: `describe`'s `mutable:` line lists the fields each function can set"
    )
    onward = (
        f"fits has one caller: m.c:{passed} in wrap, which passes it on (`lay`, its own "
        "pointer parameter, where a change is refused too), and "
    )
    if case == "two_callers":
        assert f"{onward}Peeky cannot single out one call of wrap; {elsewhere}" in message, out
        assert "in main" not in message, out
        assert "hint: Change the field in the function that holds the struct itself" in out
        # Where it points: main's own fields.
        code, out = run(capsys, "describe", source)
        (mutable,) = [line for line in out.splitlines() if line.startswith("mutable:")]
        assert "main: lay.width, lay.indent, other.width, other.indent" in mutable, mutable
    elif case == "repointed":
        assert f"{onward}wrap may point `lay` elsewhere first; {elsewhere}" in message, out
    elif case == "stored":
        assert (
            f"wrap has one caller: m.c:{call} in main, which passes on `kept`, and Peeky "
            f"cannot tell what that points to; {elsewhere}"
        ) in message, out
    else:
        assert (
            f"wrap has one caller: m.c:{call} in main, which passes `&lay`, a static local of main, "
            "which Peeky does not change (it changes only locals a frame holds): change what "
            "sets it with a candidate edit (`--old`/`--new`)"
        ) in message, out
        assert "hint: Change what sets it with a candidate edit" in out, out
