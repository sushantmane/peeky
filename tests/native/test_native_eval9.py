"""The ninth agent evaluation's C findings (wrap), on real builds: a caller frame's loop
counter, a withheld local's reason, and `session get`'s exit status."""

import json
import os

import pytest

from peeky.cli import main

SOURCE = """#include <stdio.h>
#define BEGIN {
#define END }

static int twice(int v) {
    return 2 * v; /* TWICE */
}

int main(void) {
    int total = 0;
    for (int i = 0; i < 3; i++) {
        total += twice(i); /* CALL */
    }
    int len = 3;
    BEGIN len++; END
    printf("%d %d\\n", total, len); /* PRINT */
    return 0;
}
"""


@pytest.fixture
def program(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "loop.c"
    path.write_text(SOURCE)
    return path


def line_of(marker):
    return next(i for i, text in enumerate(SOURCE.splitlines(), 1) if f"/* {marker} */" in text)


@pytest.mark.timeout(120)
def test_a_callers_loop_counter_says_where_to_read_it_and_a_missing_name_is_named(program, capsys):
    # wrap 4: `session get --frame 1 i` in `wrap`, a caller inside its `for` loop, said
    # `no value yet: declared on line 35, whose code this caller frame has not
    # finished`. It stays withheld (review of PR #214: a call a macro hides in the
    # initializer, or a `goto` into the body, leaves leftovers there), and says why and
    # where the counter can be read.
    assert main(["session", "start", str(program), "--at", str(line_of("TWICE"))]) == 0
    capsys.readouterr()
    try:
        for _ in range(2):
            assert main(["session", "continue"]) == 0
        capsys.readouterr()
        assert main(["session", "get", "--frame", "1", "i", "total", "--json"]) == 1
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
        assert rows["total"].get("value") == "2" and "value" not in rows["i"], rows
        assert rows["i"]["error"] == (
            "no value yet: declared on line 11, in this caller frame's `for` header, and "
            "Peeky cannot prove the loop has run its initialization (a `goto` into the body "
            "or a macro's call can skip or delay it): its storage may hold leftovers; read it "
            "in the loop's body (a probe there, or `step --out` to this frame)"
        ), rows["i"]
        # wrap 6: exit 1 said nothing of which selector had no value.
        assert main(["session", "get", "--frame", "1", "total", "nosuch"]) == 1
        captured = capsys.readouterr()
        assert "total: int = 2" in captured.out
        assert (
            "peeky: no value for nosuch (each row above says why), so the exit status is 1"
            in captured.err
        ), captured.err
        # The advice holds: back in the loop's own frame, `i` reads.
        assert main(["session", "step", "--out"]) == 0
        capsys.readouterr()
        assert main(["session", "get", "i", "--json"]) == 0
        (row,) = json.loads(capsys.readouterr().out)
        assert row.get("value") == "2", row
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(120)
def test_a_withheld_locals_reason_names_no_jump_the_code_may_not_have(program, capsys):
    # wrap 5: a withheld `len` blamed "a jump (a `switch` or a `goto`)" in a function
    # with neither. Here macros open and close a block between the declaration and the
    # stop, which Peeky cannot read through.
    code = main(
        ["probe", str(program), "--at", str(line_of("PRINT")), "--capture", "len", "--json"]
        + ["--no-save", "--quiet"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    (row,) = result["rows"]
    assert "value" not in row, row
    assert row["error"].endswith(
        "declared on line 14, and Peeky cannot tell from the source whether this line can be "
        "reached without running the declaration, so it may not be initialized here: its "
        "storage may hold leftovers"
    ), row
    assert "switch" not in row["error"] and "goto" not in row["error"]


SKIPS = """#include <stdio.h>
#define INIT make_i()

static int make_i(void) {
    return 0; /* MAKE */
}

static int thrice(int v) {
    return 3 * v; /* THRICE */
}

int main(int argc, char **argv) {
    int total = 0, passes = 0;
    (void)argv;
    for (int i = INIT; i < 2; i++) total += i; /* ONELINE */
    if (argc > 0)
        goto inside;
    for (int j = 0; j < 2 && passes < 2; j++) {
        inside: passes++; total += thrice(j); /* INSIDE */
    }
    printf("%d\\n", total);
    return 0;
}
"""


@pytest.mark.timeout(180)
def test_a_loop_counter_whose_initialization_may_not_have_run_is_withheld(program, capsys):
    # Review of PR #214: a call a macro hides in the initializer (`int i = INIT`), and a
    # `goto` into the body past `int j = 0`, each left leftovers where the header's
    # text alone had been taken to prove the counter set.
    path = program.parent / "skips.c"
    path.write_text(SKIPS)
    lines = {
        marker: number
        for number, text in enumerate(SKIPS.splitlines(), 1)
        for marker in ("MAKE", "THRICE", "ONELINE", "INSIDE")
        if f"/* {marker} */" in text
    }
    assert main(["session", "start", str(path), "--at", str(lines["MAKE"])]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "get", "--frame", "1", "i", "--json"]) == 1
        (row,) = json.loads(capsys.readouterr().out)
        assert "value" not in row, row
        assert main(["session", "step", "--out", "--json"]) == 0
        capsys.readouterr()
        code = main(["session", "get", "i", "--json"])
        (row,) = json.loads(capsys.readouterr().out)
        assert code == 1 and "value" not in row, json.dumps(row)
    finally:
        main(["session", "close"])
        capsys.readouterr()
    assert main(["session", "start", str(path), "--at", str(lines["THRICE"])]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "get", "--frame", "1", "j", "--json"]) == 1
        (row,) = json.loads(capsys.readouterr().out)
        assert "value" not in row and "Peeky cannot prove the loop has run" in json.dumps(row), row
    finally:
        main(["session", "close"])
        capsys.readouterr()


MIDLINE = """#include <stdio.h>

static int done;

static void sink_a(int *p) {
    (void)p;
    done++; /* SINKA */
}

static void sink_b(int *p) {
    (void)p;
    done++; /* SINKB */
}

int main(int argc, char **argv) {
    (void)argv;
    for (int i; done < 3; ) { sink_a(&i); } /* NOINIT */
    if (argc > 0)
        goto inside;
    for (int k = 0; done < 6; ) { inside: sink_b(&k); } /* GOTO */
    printf("%d\\n", done);
    return 0;
}
"""


@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    ("sink", "line", "name"), [("SINKA", "NOINIT", "i"), ("SINKB", "GOTO", "k")]
)
def test_a_one_line_loops_variable_its_header_may_not_have_set_is_withheld_mid_line(
    program, capsys, sink, line, name
):
    # Review round 2 of PR #214: back from a call in a one-line loop's body (`step
    # --out`), a header's variable read as live mid-line though the header sets nothing
    # (`int i;`), or a `goto` into the body skipped `int k = 0`: both leftovers.
    path = program.parent / "midline.c"
    path.write_text(MIDLINE)
    lines = {
        marker: number
        for number, text in enumerate(MIDLINE.splitlines(), 1)
        for marker in ("SINKA", "SINKB", "NOINIT", "GOTO")
        if f"/* {marker} */" in text
    }
    assert main(["session", "start", str(path), "--at", str(lines[sink])]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "step", "--out", "--json"]) == 0
        state = json.loads(capsys.readouterr().out)
        assert state["location"].endswith(f":{lines[line]}"), state
        code = main(["session", "get", name, "--json"])
        (row,) = json.loads(capsys.readouterr().out)
        assert code == 1 and "value" not in row, json.dumps(row)
        assert "part-way through that line" in row["error"], row
    finally:
        main(["session", "close"])
        capsys.readouterr()


ASMGOTO = """#include <stdio.h>

static int done;

static void sink(int *p) {
    (void)p;
    done++; /* SINK */
}

int main(void) {
    __asm__ goto("" : : : : inside);
    for (int k = 0; done < 3; ) { inside: sink(&k); } /* ASMGOTO */
    printf("%d\\n", done);
    return 0;
}
"""


@pytest.mark.timeout(120)
def test_an_asm_goto_may_skip_a_one_line_loops_initialization(program, capsys):
    # Review round 3 of PR #214: a GNU `asm goto` names its labels as operands, so no
    # `goto inside` was seen and `k` read as live mid-line. This one never jumps, but
    # the source cannot show that: `k` is withheld.
    path = program.parent / "asmgoto.c"
    path.write_text(ASMGOTO)
    lines = {
        marker: number
        for number, text in enumerate(ASMGOTO.splitlines(), 1)
        for marker in ("SINK", "ASMGOTO")
        if f"/* {marker} */" in text
    }
    assert main(["session", "start", str(path), "--at", str(lines["SINK"])]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "step", "--out", "--json"]) == 0
        state = json.loads(capsys.readouterr().out)
        assert state["location"].endswith(f":{lines['ASMGOTO']}"), state
        code = main(["session", "get", "k", "--json"])
        (row,) = json.loads(capsys.readouterr().out)
        assert code == 1 and "value" not in row, json.dumps(row)
        assert "part-way through that line" in row["error"], row
    finally:
        main(["session", "close"])
        capsys.readouterr()


DECLARATORS = """#include <cstdio>

struct X {
    int v;
    X(int n) : v(n) {
        std::printf("made\\n"); /* CTOR */
    }
};

int main() {
    for (X j = 0, *i = nullptr; j.v < 1; j.v++) { std::printf("%p\\n", (void *)i); } /* DECL */
    return 0;
}
"""


@pytest.mark.timeout(120)
def test_a_cpp_declarator_after_one_that_constructs_is_withheld_mid_line(program, capsys):
    # Review round 3 of PR #214: `X j = 0` runs `X::X(int)` before `*i = nullptr` is
    # set, so back from it (`step --out`) `i` is not set yet: in C++ only a header's
    # first declarator counts as set by no call.
    path = program.parent / "declarators.cpp"
    path.write_text(DECLARATORS)
    lines = {
        marker: number
        for number, text in enumerate(DECLARATORS.splitlines(), 1)
        for marker in ("CTOR", "DECL")
        if f"/* {marker} */" in text
    }
    assert main(["session", "start", str(path), "--at", str(lines["CTOR"])]) == 0
    capsys.readouterr()
    try:
        # Out of the constructor clang emits as two (complete and base object).
        for _ in range(3):
            assert main(["session", "step", "--out", "--json"]) == 0
            state = json.loads(capsys.readouterr().out)
            if state["function"].startswith("main"):
                break
        assert state["location"].endswith(f":{lines['DECL']}"), state
        code = main(["session", "get", "i", "--json"])
        (row,) = json.loads(capsys.readouterr().out)
        assert code == 1 and "value" not in row, json.dumps(row)
        assert "part-way through that line" in row["error"], row
    finally:
        main(["session", "close"])
        capsys.readouterr()


TEXTS = """#include <stdio.h>
#include <string.h>

struct buffer { char text[8192]; size_t used; };

int main(void) {
    struct buffer out = {{0}, 0};
    char full[2048] = {0};
    strcpy(out.text, "    The quick brown fox");
    memset(full, 'x', sizeof full - 1);
    printf("%s %zu\\n", out.text, strlen(full)); /* SHOW */
    return 0;
}
"""


@pytest.mark.timeout(120)
def test_a_long_char_array_shows_where_its_text_ends(program, capsys):
    # wrap 7: `out->text` (a `char[8192]`) showed `"    The quick brown fox"...`, whose
    # `...` suggests a cut where the text ended at its NUL (LLDB adds it to any array
    # past its 1024-byte summary limit), and `<children: Variable collection exceeds
    # bounded expansion limit>`.
    path = program.with_name("texts.c")
    path.write_text(TEXTS)
    line = next(i for i, text in enumerate(TEXTS.splitlines(), 1) if "/* SHOW */" in text)
    assert main(["session", "start", str(path), "--at", str(line)]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "get", "out.text", "full", "--json"]) == 0
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
        assert rows["out.text"]["value"] == '"    The quick brown fox"', rows["out.text"]
        assert "children_error" not in rows["out.text"], rows["out.text"]
        # 2047 bytes with no NUL in LLDB's first 1024: cut, and it says so.
        assert rows["full"]["value"] == '"' + "x" * 1024 + '"...', rows["full"]["value"][-20:]
        assert main(["session", "get", "out.text"]) == 0
        assert "<children:" not in capsys.readouterr().out
    finally:
        main(["session", "close"])
        capsys.readouterr()
