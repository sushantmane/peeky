"""The seventh agent evaluation's C findings, on real builds: a refusal through a pointer
parameter names the one call that passed it (13), and `describe` names C's struct,
union and enum types as C does (17) and says which locals have settable fields (16)."""

import json
import os

import pytest

from peeky.cli import main

# The evaluation's case, as the agent had it.
RING = """#include <stdio.h>
#include <stdlib.h>

#define CAP 4

struct ring {
    int buf[CAP];
    int head;   /* next slot to read */
    int tail;   /* next slot to write */
    int count;
};

static int push(struct ring *r, int v) {
    if (r->count == CAP) return 0;
    r->buf[r->tail] = v;
    r->tail = (r->tail + 1) % (CAP - 1);
    r->count++;
    return 1;
}

static int pop(struct ring *r, int *out) {
    if (r->count == 0) return 0;
    *out = r->buf[r->head];
    r->head = (r->head + 1) % CAP;
    r->count--;
    return 1;
}

/* ring OPS: each op is "+N" (push N) or "-" (pop); prints the popped values. */
int main(int argc, char **argv) {
    struct ring r = {{0}, 0, 0, 0};
    printf("POPPED:");
    int first = 1;
    for (int i = 1; i < argc; i++) {
        if (argv[i][0] == '+') {
            push(&r, atoi(argv[i] + 1));
        } else {
            int v;
            if (pop(&r, &v)) {
                printf("%s%d", first ? "" : ",", v);
                first = 0;
            }
        }
    }
    printf("\\n");
    return 0;
}
"""
OPS = ["--", "+1", "+2", "+3", "-", "-", "+4", "+5", "+6", "-", "-", "-", "-"]

# Callers Peeky cannot single out: twice, through a macro used twice, through a function
# pointer, and once but with a pointer it does not own.
CALLERS = """#include <stdio.h>

struct box { int n; int m; };
typedef struct box box_t;
union num { int i; float f; };
enum color { RED, GREEN };

static int twice(struct box *b) { return b->n; }  /* TWICE */
static int macro(struct box *b) { return b->n; }  /* MACRO */
static int aimed(struct box *b) { return b->n; }  /* AIMED */
static int passed(struct box *b) { return b->n; }  /* PASSED */
#define TWO(x) macro(&x)

int main(void) {
    struct box b = { 1, 2 };
    box_t t = { 3, 4 };
    union num u = { 5 };
    enum color c = GREEN;
    int arr[3] = { 1, 2, 3 };
    struct box *p = &b;
    int (*f)(struct box *) = aimed;
    int sum = twice(&b) + twice(&b) + TWO(b) + TWO(b) + f(&b) + passed(p);
    printf("%d %d %d %d %d\\n", sum, t.n, u.i, (int)c, arr[1]);
    return 0;
}
"""


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


@pytest.mark.timeout(240)
def test_a_pointer_parameters_one_caller_is_named(c_program, capsys):
    # Finding 13: `MUTATION_REJECTED` said to change the value "before the call that
    # passed it", but did not name `ring.c:36`, push's one caller.
    source = c_program("ring.c", RING)
    code, out = run(
        capsys, "probe", source, "--at", "15", "--capture", "r.tail", "--set", "r.tail=0", *OPS
    )
    assert (
        "push has one caller: ring.c:36 in main, which passes `&r`, where `r.tail` is "
        "settable: a probe `--at 36 --set r.tail=<value>`"
    ) in out, out
    assert code == 2, out
    assert "hint: Change the value at that call, where it is owned" in out, out
    # The claim holds: line 36 changes `r.tail` before push's 4th call reads it.
    code, out = run(
        capsys, "probe", source, "--at", "36", "--capture", "r.tail", "--hits", "4",
        "--set", "r.tail=3", "--set-hit", "4", "--json", *OPS,
    )  # fmt: skip
    result = json.loads(out)
    assert result["status"] == "completed", result.get("error")
    assert [(i["before"], i["after"]) for i in result["interventions"]] == [("0", "3")]
    # At a stop in push too, through a session.
    code, out = run(capsys, "session", "start", source, "--at", "16", *OPS)
    try:
        code, out = run(capsys, "session", "set", "r.tail=0", "--json")
        error = json.loads(out)["error"]
        assert code == 2 and error["code"] == "MUTATION_REJECTED", error
        assert "push has one caller: ring.c:36 in main, which passes `&r`" in error["message"]
    finally:
        run(capsys, "session", "close")


@pytest.mark.timeout(240)
def test_callers_peeky_cannot_single_out_keep_the_general_advice(c_program, capsys):
    source = c_program("callers.c", CALLERS)
    for marker in ("TWICE", "MACRO", "AIMED"):
        code, out = run(
            capsys, "probe", source, "--at", line_of(CALLERS, marker), "--capture", "b.n",
            "--set", "b.n=7",
        )  # fmt: skip
        assert code == 2 and "before the call that passed it" in out, (marker, out)
        assert "one caller" not in out, (marker, out)
    # One call, but of a pointer the caller does not own: the line, and no claim.
    code, out = run(
        capsys, "probe", source, "--at", line_of(CALLERS, "PASSED"), "--capture", "b.n",
        "--set", "b.n=7",
    )  # fmt: skip
    called = line_of(CALLERS, "int sum = ")
    assert code == 2, out
    assert f"passed has one caller: callers.c:{called} in main; change the value" in out, out
    assert "settable" not in out.split("one caller")[1], out


@pytest.mark.timeout(240)
def test_describe_names_c_structs_unions_and_enums_as_c_does(c_program, capsys):
    # Finding 17: `describe` printed `r (ring)` for a C `struct ring` with no typedef.
    source = c_program("ring.c", RING)
    code, out = run(capsys, "describe", source)
    assert code == 0, out
    assert "names in push (ring.c:13-19): r (struct ring *), v (int)" in out, out
    assert "argv (char **), first (int), i (int, lines 34-44), r (struct ring)," in out, out
    source = c_program("callers.c", CALLERS)
    code, out = run(capsys, "describe", source, "--json")
    (main_scope,) = [s for s in json.loads(out)["scopes"] if s["scope"] == "main"]
    types = main_scope["types"]
    # A typedef's name stays; a union and an enum get their tags.
    assert types["b"] == "struct box" and types["t"] == "box_t", types
    assert types["u"] == "union num" and types["c"] == "enum color", types
    assert types["p"] == "struct box *" and types["arr"] == "int[3]", types


@pytest.mark.timeout(240)
def test_describe_says_which_locals_have_settable_fields(c_program, capsys):
    # Finding 16: the `mutable:` line did not say that `r`'s fields are settable.
    source = c_program("ring.c", RING)
    code, out = run(capsys, "describe", source)
    (mutable,) = [line for line in out.splitlines() if line.startswith("mutable:")]
    assert mutable.endswith(". Locals with such fields or elements: main: r"), mutable
    source = c_program("callers.c", CALLERS)
    code, out = run(capsys, "describe", source, "--json")
    (main_scope,) = [s for s in json.loads(out)["scopes"] if s["scope"] == "main"]
    # A union, a pointer and a scalar have no settable fields; a typedef'd struct does.
    assert main_scope["settable_parts"] == {"arr": "elements", "b": "fields", "t": "fields"}
