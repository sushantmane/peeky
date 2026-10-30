"""A refused change to a field through a C pointer parameter names the one call that passed
the pointer, on real builds, and calls the field settable there only where a change at that
call's line reaches the callee."""

import json
import os

import pytest

from peeky.cli import main

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
# The same ring, its line 36 reading the digit without a call (a call in
# the arguments, `atoi`'s too, gets no settable claim).
READING_RING = RING.replace("push(&r, atoi(argv[i] + 1));", "push(&r, argv[i][1] - '0');")

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
    # `MUTATION_REJECTED` names `ring.c:36`, push's one caller, as the call that passed the
    # pointer.
    source = c_program("ring.c", RING)
    code, out = run(
        capsys, "probe", source, "--at", "15", "--capture", "r.tail", "--set", "r.tail=0", *OPS
    )
    assert (
        "push has one caller: ring.c:36 in main, which passes `&r`; change the value where "
        "it is owned, before that call: Peeky cannot tell that a change at line 36 reaches it"
    ) in out, out
    assert code == 2, out
    # Without the call in its arguments, line 36 is where `r.tail` is settable.
    source = c_program("ring.c", READING_RING)
    code, out = run(
        capsys, "probe", source, "--at", "15", "--capture", "r.tail", "--set", "r.tail=0", *OPS
    )
    assert (
        "push has one caller: ring.c:36 in main, which passes `&r`, where `r.tail` is "
        "settable: a probe's `--set-at 36@N r.tail=<value>`"
    ) in out, out
    assert code == 2, out
    assert "hint: Change the value in the caller the message names, before that call" in out
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
    # One call, but of a pointer the caller does not own: no frame named for the change,
    # only where to find the one that owns it.
    code, out = run(
        capsys, "probe", source, "--at", line_of(CALLERS, "PASSED"), "--capture", "b.n",
        "--set", "b.n=7",
    )  # fmt: skip
    called = line_of(CALLERS, "int sum = ")
    assert code == 2, out
    assert (
        f"passed has one caller: callers.c:{called} in main, which passes on `p`, and Peeky "
        "cannot tell what that points to; change the value in the frame that owns the struct, "
        "before the calls that lead here: `describe`'s `mutable:` line lists the fields each "
        "function can set"
    ) in out, out
    assert "settable" not in out.splitlines()[0].split("one caller")[1], out


@pytest.mark.timeout(240)
def test_a_set_at_refused_before_the_run_names_the_caller_and_shows_no_table(c_program, capsys):
    # A `--set-at` judged at its own line before the run: the refusal names push's one
    # caller, prints no table and exits 2.
    source = c_program("ring.c", READING_RING)
    code, out = run(
        capsys, "probe", source, "--at", "36", "--capture", "r.tail",
        "--set-at", "15@2", "r.tail=0", *OPS,
    )  # fmt: skip
    assert out.startswith("error [MUTATION_REJECTED]: `r.tail` cannot be changed at ring.c:15")
    assert (
        "push has one caller: ring.c:36 in main, which passes `&r`, where `r.tail` is "
        "settable: a probe's `--set-at 36@N r.tail=<value>`"
    ) in out, out
    assert "hit  phase" not in out and "status:" not in out, out
    assert code == 2, out
    # The advice holds: at line 36's 2nd stop, a --set-at changes `r.tail` before push
    # reads it.
    code, out = run(
        capsys, "probe", source, "--at", "15", "--capture", "r.tail", "--hits", "2",
        "--set-at", "36@2", "r.tail=3", "--json", *OPS,
    )  # fmt: skip
    result = json.loads(out)
    assert code == 0 and [(i["before"], i["after"]) for i in result["interventions"]] == [
        ("1", "3")
    ], result
    assert [row["value"] for row in result["rows"]] == ["0", "3"], result["rows"]


# Calls a stop at their line cannot change the value before.
CALL_SITES = {
    # The line overwrites the field before the call.
    "overwrite": "    r.tail = 1; total += push(&r, 1);  /* CALL */",
    # So does the call's own argument.
    "argument": "    total += push(&r, r.tail = 3);  /* CALL */",
    # The call is on its statement's second line, and runs before the first's code.
    "continued": "    total += 1 +\n        push(&r, 1);  /* CALL */",
    # A clean one, for comparison.
    "clean": "    total += push(&r, 1);  /* CALL */",
}
SITE = """#include <stdio.h>
struct ring { int head; int tail; int buf[4]; };
static int push(struct ring *r, int v) {
    r->buf[r->tail % 4] = v; /* AT */
    r->tail++;
    return r->tail;
}
int main(void) {
    struct ring r = { 0, 0, { 0 } };
    int total = 0;
CALL
    printf("%d %d\\n", total, r.tail);
    return 0;
}
"""


@pytest.mark.timeout(240)
@pytest.mark.parametrize("site", list(CALL_SITES))
def test_settable_is_claimed_only_where_the_call_sees_the_change(c_program, capsys, site):
    # `r.tail = 1; total += push(&r, 1);` overwrites the field before the call, so it is
    # not settable at line 11; nor with `push(&r, r.tail = 3)`. A call on its statement's
    # second line is not named by that line, where no stop is at the statement's start.
    text = SITE.replace("CALL\n", CALL_SITES[site] + "\n")
    source = c_program("m.c", text)
    at, call = line_of(text, "/* AT */"), line_of(text, "/* CALL */")
    code, out = run(capsys, "probe", source, "--at", at, "--capture", "v", "--set", "r.tail=0")
    assert code == 2, out
    if site == "clean":
        assert (
            f"push has one caller: m.c:{call} in main, which passes `&r`, where `r.tail` is "
            f"settable: a probe's `--set-at {call}@N r.tail=<value>`"
        ) in out, out
        return
    message = out.splitlines()[0]
    assert "settable" not in message.split("has one caller")[1], out
    assert "--set-at" not in message, out
    if site == "continued":
        start = call - 1
        assert f"push has one caller: m.c:{call} (its statement starts on line {start}) in " in out
        assert f"Peeky cannot tell that a change at line {start} reaches it" in out, out
    else:
        assert f"push has one caller: m.c:{call} in main, which passes `&r`; change the " in out
    # The claim would have been wrong: a change at that line's stop never reaches push.
    if site == "overwrite":
        code, out = run(
            capsys, "probe", source, "--at", at, "--capture", "r.tail", "--set-at",
            f"{call}@1", "r.tail=3", "--json",
        )  # fmt: skip
        assert json.loads(out)["rows"][0]["value"] == "1", out


OTHER_MAIN = """#include <stdio.h>
#include "ring.h"
int push(struct ring *r, int v) {
    r->buf[r->tail % 4] = v; /* AT */
    r->tail++;
    return r->tail;
}
int main(void) {
    int total = helper();
    printf("%d\\n", total);
    return 0;
}
"""
OTHER = """#include "ring.h"

int helper(void) {
    struct ring q = { 0, 0, { 0 } };
    int t = 0;
    t += push(&q, 2);
    return t;
}
"""
RING_H = """struct ring { int head; int tail; int buf[4]; };
int push(struct ring *r, int v);
int helper(void);
"""
HEADER_MAIN = OTHER_MAIN.replace(
    "int total = helper();", "struct ring r = { 0, 0, { 0 } };\n    int total = via(&r) + helper();"
)
HEADER_OTHER = """#include "ring.h"
int helper(void) {
    struct ring q = { 0, 0, { 0 } };
    return via(&q);
}
"""
HEADER_H = RING_H.replace(
    "int helper(void);",
    "static inline int via(struct ring *r) { return push(r, 7); }\nint helper(void);",
)
# via owns the ring it passes, in a header only main.c includes.
OWNED_MAIN = OTHER_MAIN.replace(
    '#include "ring.h"\n', '#include "ring.h"\n#include "via.h"\n'
).replace("int total = helper();", "int total = via() + helper();")
OWNED_OTHER = '#include "ring.h"\nint helper(void) { return 0; }\n'
VIA_H = "static inline int via(void) { struct ring q = { 0, 0, { 0 } }; return push(&q, 7); }\n"


@pytest.mark.timeout(240)
def test_a_caller_in_another_file_is_changed_by_a_probe_there(c_program, capsys):
    # push's one call is at other.c:6, but `--set-at 6@N` names main.c's line 6,
    # the probe's own file. A probe `--at other.c:6` with `--set` is what works.
    root = c_program("main.c", OTHER_MAIN).parent
    c_program("other.c", OTHER)
    c_program("ring.h", RING_H)
    code, out = run(
        capsys, "probe", root, "--at", "main.c:4", "--capture", "v", "--set", "r.tail=0"
    )
    advice = (
        "push has one caller: other.c:6 in helper, which passes `&q`, where `q.tail` is "
        "settable: `peeky probe <target> --at other.c:6 --set q.tail=<value> --set-hit N`"
    )
    assert code == 2 and advice in out and "--set-at" not in out, out
    # Followed, it changes what push sees: push returns tail + 1.
    code, out = run(
        capsys, "probe", root, "--at", "other.c:6", "--capture", "q.tail", "--set",
        "q.tail=3", "--set-hit", "1", "--json",
    )  # fmt: skip
    result = json.loads(out)
    assert code == 0 and [(i["before"], i["after"]) for i in result["interventions"]] == [
        ("0", "3")
    ], result
    assert result["output"].strip() == "4", result["output"]


@pytest.mark.timeout(240)
def test_a_caller_in_a_header_gets_no_advice_a_probe_cannot_follow(c_program, capsys):
    # push's call is in ring.h's `via`, where no probe stops. Main.c and other.c each
    # compile their own via: two calls, so the general advice.
    root = c_program("main.c", HEADER_MAIN).parent
    c_program("other.c", HEADER_OTHER)
    c_program("ring.h", HEADER_H)
    code, out = run(
        capsys, "probe", root, "--at", "main.c:4", "--capture", "v", "--set", "r.tail=0"
    )
    message = out.splitlines()[0]
    assert code == 2 and "before the call that passed it" in message, out
    assert "one caller" not in message, out
    # Where via, in a header only main.c includes, owns the ring, the call is named
    # alone: no probe stops in via.h.
    c_program("main.c", OWNED_MAIN)
    c_program("other.c", OWNED_OTHER)
    c_program("ring.h", RING_H)
    c_program("via.h", VIA_H)
    at = line_of(OWNED_MAIN, "/* AT */")
    code, out = run(
        capsys, "probe", root, "--at", f"main.c:{at}", "--capture", "v", "--set", "r.tail=0"
    )
    assert code == 2, out
    assert (
        "push has one caller: via.h:1 in via, which passes `&q`; change the value where it "
        "is owned, before that call: a probe stops only in the program's source files, not "
        "in via.h"
    ) in out, out
    assert "--set-at" not in out.split("has one caller")[1].splitlines()[0], out


# What a stop on the call's line cannot see changes the value first.
WRITERS = """#include <stdio.h>
#include <string.h>
struct ring { int head; int tail; int buf[4]; };
static struct ring *saved;
static int touch(void) { if (saved) saved->tail = 5; return 1; }
static int bump(int *p) { *p = 6; return 1; }
static void keep(struct ring *x) { saved = x; }
static int peek(struct ring *x) { return x->tail + x->buf[x->head]; }
#define RESET(x) ((x).tail = 0)
#define ZERO() (r.tail = 0)
static int push(struct ring *r, int v) {
    r->buf[r->tail % 4] = v; /* AT */
    r->tail++;
    return r->tail;
}
int main(void) {
    struct ring r = { 0, 0, { 0 } };
    int total = 0;
DECLARE
    STATEMENT  /* CALL */
    printf("%d %d %d %d\\n", total, r.tail, touch() + bump(&total), (int)(saved != 0));
    return 0;
}
"""
WRITTEN_FIRST = {
    "paren_root": ("", "total = ((r).tail = 2, push(&r, 1));"),
    "paren_field": ("", "total = ((r.tail) = 2, push(&r, 1));"),
    "macro_arg": ("", "total = push(&r, RESET(r));"),
    "macro_noroot": ("", "total = push(&r, ZERO());"),
    "alias_arg": ("struct ring *a = &r;", "total = push(&r, (a->tail = 5));"),
    "alias_field": ("int *p = &r.tail;", "total = push(&r, bump(p));"),
    "callee_global": ("saved = &r;", "total = push(&r, touch());"),
    # keep() leaves a pointer to r behind, which a thread or handler may write through.
    "escaping_callee": ("keep(&r);", 'total = push(&r, (int)strlen("ab"));'),
}


def writers(declare, call):
    return WRITERS.replace("DECLARE\n", f"    {declare}\n" if declare else "").replace(
        "STATEMENT", call
    )


@pytest.mark.timeout(240)
@pytest.mark.parametrize("site", list(WRITTEN_FIRST))
def test_no_settable_claim_where_the_statement_or_an_alias_writes_first(c_program, capsys, site):
    # A parenthesized root or member, a macro's assignment (the call is read as the
    # compiler preprocesses it), and the root's address taken elsewhere in the caller.
    text = writers(*WRITTEN_FIRST[site])
    source = c_program("m.c", text)
    at, call = line_of(text, "/* AT */"), line_of(text, "/* CALL */")
    code, out = run(capsys, "probe", source, "--at", at, "--capture", "v", "--set", "r.tail=0")
    message = out.splitlines()[0]
    assert code == 2 and f"push has one caller: m.c:{call} in main" in message, out
    assert "settable" not in message.split("has one caller")[1], out
    assert "--set-at" not in message, out


@pytest.mark.timeout(240)
@pytest.mark.parametrize(
    ("declare", "call", "selector", "claimed"),
    [
        # Reads of an integer field, in the arguments and in an `if`'s condition.
        ("", "total = push(&r, r.head + 1);", "r.tail", True),
        ("", "if (push(&r, 1) > r.head) total++;", "r.tail", True),
        ("", "int fresh = push(&r, 1); total = fresh;", "r.tail", True),
        # peek() reads `x->tail` and `x->buf[i]` only: it keeps no pointer to r.
        ("peek(&r);", "total = push(&r, 1);", "r.tail", True),
        # Any call in the arguments, a library's too; an array field passes a pointer.
        ("", 'total = push(&r, (int)strlen("ab"));', "r.tail", False),
        ("", "total = push(&r, r.buf[0]);", "r.tail", False),
        # A pointer into an array needs no `&`: sscanf may write r.buf[1] through q.
        ("int *q = r.buf;", 'total = push(&r, sscanf("7", "%d", q + 1));', "r.buf[1]", False),
        ("", "total = push(&r, 1);", "r.buf[1]", True),
    ],
)
def test_the_claim_kept_where_nothing_can_reach_the_value(
    c_program, capsys, declare, call, selector, claimed
):
    text = writers(declare, call)
    source = c_program("m.c", text)
    at, line = line_of(text, "/* AT */"), line_of(text, "/* CALL */")
    code, out = run(capsys, "probe", source, "--at", at, "--capture", "v", "--set", f"{selector}=0")
    message = out.splitlines()[0]
    assert code == 2 and f"push has one caller: m.c:{line} in main" in message, out
    claim = f"where `{selector}` is settable: a probe's `--set-at {line}@N"
    assert (claim in message) == claimed, out


# `buf` is the first field, so `(struct ring *)b` is `r`, and zap,
# reached through a table, a pointer or a member, changes `r.tail` before push reads it.
FIRST_FIELD = """#include <stdio.h>
#include <string.h>
struct ring { int buf[4]; int head; int tail; };
static int zap(int *b) { ((struct ring *)b)->tail = 5; return 1; }
static int (*ops[])(int *) = { zap };
static int (*fp)(int *) = zap;
struct vt { int (*fn)(int *); };
static int push(struct ring *r, int v) {
    int seen = r->tail + v; /* AT */
    r->tail++;
    return seen;
}
int main(void) {
    struct ring r = { { 0 }, 0, 0 };
    struct ring q = { { 0 }, 0, 3 };
    struct vt v = { zap };
    int total = 0;
    STATEMENT  /* CALL */
    printf("%d %d %d\\n", total, r.tail, q.tail);
    return 0;
}
"""
RESHAPED = {
    "table": "total = push(&r, ops[0](r.buf));",
    "deref": "total = push(&r, (*fp)(r.buf));",
    "member": "total = push(&r, v.fn(r.buf));",
    "library": "total = push(&r, (memset(r.buf, 0, sizeof r), 1));",
    "root_first": "r = q, push(&r, 1);",
}


@pytest.mark.timeout(240)
@pytest.mark.parametrize("site", list(RESHAPED))
def test_no_settable_claim_but_for_a_plain_call_of_reads(c_program, capsys, site):
    text = FIRST_FIELD.replace("STATEMENT", RESHAPED[site])
    source = c_program("m.c", text)
    at, call = line_of(text, "/* AT */"), line_of(text, "/* CALL */")
    code, out = run(capsys, "probe", source, "--at", at, "--capture", "v", "--set", "r.tail=0")
    message = out.splitlines()[0]
    assert code == 2 and f"push has one caller: m.c:{call} in main" in message, out
    assert "settable" not in message.split("has one caller")[1], out
    if site == "table":
        # The claim would have been wrong: push reads zap's 5, not the change.
        code, out = run(
            capsys, "probe", source, "--at", at, "--capture", "r.tail", "--set-at",
            f"{call}@1", "r.tail=3", "--json",
        )  # fmt: skip
        assert json.loads(out)["rows"][0]["value"] == "5", out
