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


# Review of PR #211, 1: an inline-asm output operand writes through the pointer, which
# `p->width` inside the asm statement does not show as a write.
ASM = """#include <stdio.h>
struct layout { int width; };
static int fits(const struct layout *p) {
    return p->width; /* AT */
}
static int wrap(struct layout *p) {
    __asm__ volatile("" : "=r"(p->width) : "0"(5));
    return fits(p);
}
int main(void) {
    struct layout x = { 1 };
    int seen = wrap(&x); /* CALL */
    printf("SEEN:%d\\n", seen);
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_inline_assembly_on_the_way_is_never_followed(c_program, capsys):
    source = c_program("m.c", ASM)
    at, call = line_of(ASM, "/* AT */"), line_of(ASM, "/* CALL */")
    code, out = run(capsys, "probe", source, "--at", at, "--set", "p.width=7")
    message = out.splitlines()[0]
    assert code == 2 and message.startswith("error [MUTATION_REJECTED]"), out
    assert "settable" not in message.split("one caller")[1] and "--set-at" not in message, out
    assert (
        "which passes it on (`p`, its own pointer parameter, where a change is refused too), "
        "and wrap has inline assembly, which may change `p` or what it points to"
    ) in message, out
    # The claim would have been wrong: fits reads the asm's 5, whatever main set.
    code, out = run(
        capsys, "probe", source, "--at", at, "--capture", "p.width", "--set-at",
        f"{call}@1", "x.width=9", "--json",
    )  # fmt: skip
    result = json.loads(out)
    assert code == 0 and [(i["before"], i["after"]) for i in result["interventions"]] == [
        ("1", "9")
    ], result
    assert [row["value"] for row in result["rows"]] == ["5"], result["rows"]
    assert result["output"].strip() == "SEEN:5", result["output"]


# Review of PR #211, 2: a static function in a header is preprocessed in each unit that
# includes it, and b.c's CLOBBER makes its copy write the width; a.c's copy only reads.
HEADER = """struct layout { int width; };
int fits(const struct layout *p);
static int wrap(struct layout *p) {
#ifdef CLOBBER
    p->width = 5;
#endif
    return fits(p);
}
"""
# a.c's first line matches b.c's, so both units have w.h's text at the same offsets.
A_C = """#define KEEP
#include "w.h"
int fits(const struct layout *p) {
    return p->width; /* AT */
}
"""
B_C = """#define CLOBBER
#include "w.h"
#include <stdio.h>
int main(void) {
    struct layout x = { 1 };
    int seen = wrap(&x); /* CALL */
    printf("SEEN:%d\\n", seen);
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_a_header_function_is_not_read_from_another_units_copy(c_program, capsys):
    root = c_program("a.c", A_C).parent
    c_program("b.c", B_C)
    c_program("w.h", HEADER)
    at, call = line_of(A_C, "/* AT */"), line_of(B_C, "/* CALL */")
    code, out = run(capsys, "probe", root, "--at", f"a.c:{at}", "--set", "p.width=7")
    message = out.splitlines()[0]
    assert code == 2 and message.startswith("error [MUTATION_REJECTED]"), out
    # Each unit's copy of wrap calls fits: no one caller, so no frame and no claim.
    assert "one caller" not in message and "settable" not in message, out
    assert "before the call that passed it" in message, out
    # The claim would have been wrong: fits reads b.c's 5, whatever main set.
    code, out = run(capsys, "probe", root, "--at", f"a.c:{at}", "--capture", "p.width", "--json")
    assert [row["value"] for row in json.loads(out)["rows"]] == ["5"], out
    code, out = run(
        capsys, "probe", root, "--at", f"b.c:{call}", "--capture", "x.width", "--set",
        "x.width=9", "--set-hit", "1", "--json",
    )  # fmt: skip
    result = json.loads(out)
    assert code == 0 and result["output"].strip() == "SEEN:5", result


# Review of PR #211, round 2: a header included inside main is main's code too. keep()
# saves the pointer, and wrap writes the width through it before fits reads it.
INCLUDED = """#include <stdio.h>
struct layout { int width; };
static struct layout *saved;
static void keep(struct layout *p) { saved = p; }
static int fits(const struct layout *p) {
    return p->width; /* AT */
}
static int wrap(const struct layout *p) {
    if (saved) saved->width = 5;
    return fits(p);
}
int main(void) {
    struct layout x = { 1 };
#include "capture.h"
    int seen = 0;
    seen = wrap(&x); /* CALL */
    printf("SEEN:%d\\n", seen);
    return 0;
}
"""
CAPTURES = {
    "address": "keep(&x);\n",
    "assembly": '__asm__ volatile("" : : : "memory");\n',
}


@pytest.mark.timeout(300)
@pytest.mark.parametrize("case", list(CAPTURES))
def test_a_header_included_inside_the_owner_is_scanned_too(c_program, capsys, case):
    source = c_program("m.c", INCLUDED)
    c_program("capture.h", CAPTURES[case])
    at, call = line_of(INCLUDED, "/* AT */"), line_of(INCLUDED, "/* CALL */")
    code, out = run(capsys, "probe", source, "--at", at, "--set", "p.width=7")
    message = out.splitlines()[0]
    assert code == 2, out
    assert (
        f"wrap has one caller: m.c:{call} in main, which passes `&x`; change the value where "
        f"it is owned, before that call: Peeky cannot tell that a change at line {call} "
        "reaches it"
    ) in message, out
    assert "settable" not in message.split("one caller")[1] and "--set-at" not in message, out
    if case == "address":
        # The claim would have been wrong: wrap overwrites main's change through keep's
        # pointer, and fits reads 5.
        code, out = run(
            capsys, "probe", source, "--at", at, "--capture", "p.width", "--set-at",
            f"{call}@1", "x.width=9", "--json",
        )  # fmt: skip
        result = json.loads(out)
        assert code == 0 and [(i["before"], i["after"]) for i in result["interventions"]] == [
            ("1", "9")
        ], result
        assert [row["value"] for row in result["rows"]] == ["5"], result["rows"]
        assert result["output"].strip() == "SEEN:5", result["output"]


@pytest.mark.timeout(300)
def test_a_header_included_inside_a_function_on_the_way_is_scanned_too(c_program, capsys):
    # The same for wrap: its whole preprocessed body is read, the asm in clobber.h too.
    text = ASM.replace(
        '    __asm__ volatile("" : "=r"(p->width) : "0"(5));\n', '#include "clobber.h"\n'
    )
    source = c_program("m.c", text)
    c_program("clobber.h", '__asm__ volatile("" : "=r"(p->width) : "0"(5));\n')
    code, out = run(
        capsys, "probe", source, "--at", line_of(text, "/* AT */"), "--set", "p.width=7"
    )
    message = out.splitlines()[0]
    assert code == 2, out
    assert "and wrap has inline assembly, which may change `p`" in message, out
    assert "settable" not in message.split("one caller")[1] and "--set-at" not in message, out


# Review of PR #211, round 3: addresses no `&` shows. `saved` points at x (pad is its
# first member), and wrap writes the width through it before fits reads it.
UNSEEN = """#include <stdio.h>
struct layout { int pad[1]; int width; };
static void *saved;
static void lend(struct layout *q) { LEND }
static int fits(const struct layout *p) {
    return p->width; /* AT */
}
static int wrap(struct layout *p) {
    WRAP
    return fits(p);
}
int main(void) {
    struct layout x = { { 0 }, 1 };
    OWNER
    int seen = 0;
    seen = wrap(&x); /* CALL */
    printf("SEEN:%d\\n", seen);
    return 0;
}
"""
THROUGH = "((struct layout *)saved)->width = 5;"
ADDRESSES = {
    # The review's repro.
    "builtin": ("saved = __builtin_addressof(x);", "", THROUGH),
    # An array member is a pointer into x.
    "decayed": ("saved = x.pad;", "", THROUGH),
    # lend reads only q->width, but keeps its address.
    "lent": ("lend(&x);", "saved = __builtin_addressof(q->width);", "*(int *)saved = 5;"),
    # On the way, wrap writes through the address it takes.
    "on_the_way": ("", "", "int *w = __builtin_addressof(p->width);\n    *w = 5;"),
}


@pytest.mark.timeout(300)
@pytest.mark.parametrize("case", list(ADDRESSES))
def test_an_address_no_ampersand_shows_takes_the_claim_away(c_program, capsys, case):
    owner, lend, wrap = ADDRESSES[case]
    text = UNSEEN.replace("OWNER", owner).replace("LEND", lend).replace("WRAP", wrap)
    source = c_program("m.c", text)
    at, call = line_of(text, "/* AT */"), line_of(text, "/* CALL */")
    code, out = run(capsys, "probe", source, "--at", at, "--set", "p.width=7")
    message = out.splitlines()[0]
    assert code == 2, out
    assert "settable" not in message.split("one caller")[1] and "--set-at" not in message, out
    if case == "on_the_way":
        assert (
            "which passes it on (`p`, its own pointer parameter, where a change is refused "
            "too), and wrap uses `__builtin_addressof`, which may change `p` or what it points to"
        ) in message, out
    else:
        assert (
            f"wrap has one caller: m.c:{call} in main, which passes `&x`; change the value "
            f"where it is owned, before that call: Peeky cannot tell that a change at line "
            f"{call} reaches it"
        ) in message, out
    # The claim would have been wrong: fits reads wrap's 5, whatever main set.
    code, out = run(
        capsys, "probe", source, "--at", at, "--capture", "p.width", "--set-at",
        f"{call}@1", "x.width=9", "--json",
    )  # fmt: skip
    result = json.loads(out)
    assert code == 0 and [(i["before"], i["after"]) for i in result["interventions"]] == [
        ("1", "9")
    ], result
    assert [row["value"] for row in result["rows"]] == ["5"], result["rows"]
    assert result["output"].strip() == "SEEN:5", result["output"]


@pytest.mark.timeout(300)
def test_an_element_or_sizeof_of_an_array_member_keeps_the_claim(c_program, capsys):
    # Neither `x.pad[0]` nor `sizeof x.pad` is a pointer into x.
    owner = "x.pad[0] = (int)sizeof x.pad;"
    text = UNSEEN.replace("OWNER", owner).replace("LEND", "").replace("WRAP", "")
    source = c_program("m.c", text)
    at, call = line_of(text, "/* AT */"), line_of(text, "/* CALL */")
    code, out = run(capsys, "probe", source, "--at", at, "--set", "p.width=7")
    assert (
        code == 2
        and (
            f"wrap has one caller: m.c:{call} in main, which passes `&x`, where `x.width` is "
            f"settable: a probe's `--set-at {call}@N x.width=<value>`"
        )
        in out
    ), out
    code, out = run(
        capsys, "probe", source, "--at", at, "--capture", "p.width", "--set-at",
        f"{call}@1", "x.width=9", "--json",
    )  # fmt: skip
    result = json.loads(out)
    assert code == 0 and [row["value"] for row in result["rows"]] == ["9"], result
    assert result["output"].strip() == "SEEN:9", result["output"]
