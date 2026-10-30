"""C struct, union and array previews, on real builds: `bins` shows its elements' fields,
where `bins  bin[4] @ 0x16bb9f278` would need eight selectors to read the bins' bounds.
"""

import contextlib
import json
import os
import shutil
import subprocess

import pytest

from peeky.cli import main

HISTO = """#include <stdio.h>
#include <stdlib.h>

#define BINS 4

struct bin {
    int lo;
    int hi;
    int count;
};

/* histo LO HI V1 V2 ...: counts the values in BINS equal-width bins covering
   [LO, HI]; every value in that range lands in exactly one bin. */
int main(int argc, char **argv) {
    if (argc < 3) return 2;
    int lo = atoi(argv[1]);
    int hi = atoi(argv[2]);
    int width = (hi - lo + BINS) / BINS;
    struct bin bins[BINS];
    for (int i = 0; i < BINS; i++) {
        bins[i].lo = lo + i * width;
        bins[i].hi = i == BINS - 1 ? hi : lo + (i + 1) * width;
        bins[i].count = 0;
    }
    for (int a = 3; a < argc; a++) {
        int v = atoi(argv[a]);
        for (int i = 0; i < BINS; i++) {
            if (v >= bins[i].lo && v < bins[i].hi) {
                bins[i].count++;
                break;
            }
        }
    }
    printf("HISTO:");
    for (int i = 0; i < BINS; i++) printf("%s%d", i ? "," : "", bins[i].count);
    printf("\\n");
    return 0;
}
"""
BINS = (
    "[{lo = 0, hi = 2, count = 0}, {lo = 2, hi = 4, count = 0}, {lo = 4, hi = 6, count = 0}, "
    "{lo = 6, hi = 7, count = 0}]"
)

SHAPES = """#include <stdio.h>
#include <string.h>
struct pt { int x; int y; };
typedef struct { struct pt a; struct pt b; } seg;
union num { int i; float f; };
typedef union { int i; char c[4]; } word_t;
struct flags { unsigned on : 1; unsigned level : 3; int rest; };
struct tagged { int kind; union { int i; double d; } u; struct { int lo, hi; }; };
struct named { char name[8]; const char *text; struct pt *where; char long_name[64]; };
struct wide { int a, b, c, d, e, f, g, h, i, j; };
int main(void) {
    struct pt p = {1, 2};
    seg s = {{1, 2}, {3, 4}};
    union num n; n.i = 7;
    word_t w; w.i = 0x41424344;
    struct flags f = {1, 5, 9};
    struct tagged t; t.kind = 2; t.u.i = 3; t.lo = 4; t.hi = 5;
    struct named nm; memcpy(nm.name, "abcdefgh", 8); nm.text = "txt"; nm.where = &p;
    memset(nm.long_name, 'z', 63); nm.long_name[63] = 0;
    struct wide wd = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10};
    struct pt many[1000];
    for (int i = 0; i < 1000; i++) { many[i].x = i; many[i].y = -i; }
    int grid[2][3] = {{1, 2, 3}, {4, 5, 6}};
    int nums[3] = {7, 8, 9};
    union num ns[2]; ns[0].i = 1; ns[1].i = 2;
    printf("%d %d %d %d %d\\n", p.x, s.a.y, n.i, w.i + f.level + t.kind, nm.name[0] + wd.j); // HERE
    struct pt later = {8, 9};
    printf("%d %d %d %d\\n", later.x, many[999].x, grid[1][2] + nums[2], ns[1].i);
    return 0;
}
"""
EXPECTED = {
    "p": "{x = 1, y = 2}",
    # A nested struct, and a typedef of an unnamed one.
    "s": "{a = {x = 1, y = 2}, b = {x = 3, y = 4}}",
    "f": "{on = 1, level = 5, rest = 9}",
    # A union member is not read as any one of its members; an anonymous struct's
    # members show in braces, with no name.
    "t": "{kind = 2, u = <union>, {lo = 4, hi = 5}}",
    # Eight members, then the rest elided.
    "wd": "{a = 1, b = 2, c = 3, d = 4, e = 5, f = 6, g = 7, h = 8, …}",
    "many": (
        "[{x = 0, y = 0}, {x = 1, y = -1}, {x = 2, y = -2}, {x = 3, y = -3}, {x = 4, y = -4}, "
        "{x = 5, y = -5}, {x = 6, y = -6}, {x = 7, y = -7}, …]"
    ),
    "grid": "[[1, 2, 3], [4, 5, 6]]",
    "nums": "[7, 8, 9]",
    "ns": "[<union>, <union>]",
    "s.a": "{x = 1, y = 2}",
    "many[999]": "{x = 999, y = -999}",
    "grid[1]": "[4, 5, 6]",
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


@contextlib.contextmanager
def session(source, line, capsys, *extra):
    assert main(["session", "start", str(source), "--at", str(line), *extra]) == 0
    capsys.readouterr()

    def get(*selectors, text=False):
        main(["session", "get", *selectors, *([] if text else ["--json"])])
        out = capsys.readouterr().out
        return out if text else {row["selector"]: row for row in json.loads(out)}

    try:
        yield get
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(120)
def test_the_histogram_bins_read_in_one_capture(c_program, capsys):
    # Not `bins  bin[4]  bin[4] @ 0x16bb9f278` and `bins[3]  bin  bin @ 0x…`.
    source = c_program("histo.c", HISTO)
    args = ["--arg", "0", "--arg", "7", "--arg", "7"]
    capture = "bins,bins[3],bins[3].hi"
    assert main(["probe", str(source), "--at", "28", *args, "--capture", capture, "--json"]) == 0
    rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    assert rows["bins"]["value"] == BINS, rows["bins"]
    assert rows["bins[3]"]["value"] == "{lo = 6, hi = 7, count = 0}", rows["bins[3]"]
    # A field stays selectable, and settable.
    assert rows["bins[3].hi"]["value"] == "7" and rows["bins[3].hi"]["settable"] is True
    # The evidence store keeps the preview as the value.
    assert main(["sql", "SELECT value FROM last_run WHERE selector = 'bins'", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == [{"value": BINS}]
    # A change shows in the preview read after it.
    set_ = ["--set", "bins[3].hi=8", "--capture", "bins[3]", "--json"]
    assert main(["probe", str(source), "--at", "28", *args, *set_]) == 0
    after = json.loads(capsys.readouterr().out)["rows"][-1]
    assert after["phase"] == "after-set" and after["value"] == "{lo = 6, hi = 8, count = 0}"
    with session(source, 28, capsys, *args) as get:
        text = get("bins", text=True)
        assert text.splitlines()[0] == f"bins: bin[4] = {BINS}", text
        # `session get` inlines each element's fields, and still selects them.
        assert "  bins[0]: bin = {lo = 0, hi = 2, count = 0} (children: peeky session get" in text
        main(["session", "inspect"])
        listed = capsys.readouterr().out
        assert f"  bins: bin[4] = {BINS} (children: peeky session get bins)" in listed, listed
        fields = get("bins[3]")["bins[3]"]["children"]
        assert [(c["selector"], c["value"]) for c in fields] == [
            ("bins[3].lo", "6"),
            ("bins[3].hi", "7"),
            ("bins[3].count", "0"),
        ]


@pytest.mark.timeout(120)
def test_structs_unions_and_arrays_preview_without_guessing(c_program, capsys):
    source = c_program("shapes.c", SHAPES)
    with session(source, line_of(SHAPES, "// HERE"), capsys) as get:
        rows = get(*EXPECTED, "n", "w", "nm", "later")
        for selector, value in EXPECTED.items():
            assert rows[selector].get("value") == value, (selector, rows[selector])
        # A union is not previewed: only one member holds what was stored.
        for selector in ("n", "w"):
            assert " @ 0x" in rows[selector]["value"], rows[selector]
        # Pointers inside show their addresses, never what they point at; a char array
        # shows its text up to its NUL and a note (no NUL in `name`: its 8 bytes), the text
        # cut to keep the note within 40 characters.
        named = rows["nm"]["value"]
        assert named.startswith('{name = "abcdefgh" (no NUL in its 8 bytes), text = 0x'), named
        assert '"txt"' not in named and "where = 0x" in named and "x = 1" not in named
        assert 'long_name = "' + "z" * 17 + '"... (then 1 NUL byte)}' in named, named
        # Declared below the stop: its storage holds leftovers, so nothing is shown.
        assert rows["later"].get("unavailable") and "value" not in rows["later"], rows["later"]
        # The listing of a 2-D array previews each row.
        listed = {c["selector"]: c["value"] for c in rows["grid"]["children"]}
        assert listed == {"grid[0]": "[1, 2, 3]", "grid[1]": "[4, 5, 6]"}


OPTIMIZED = """#include <stdio.h>
struct bin { int lo; int hi; int count; };
struct trip { int x; int y; int z; };
__attribute__((noinline)) void fill(struct bin *b, int n) {
    for (int i = 0; i < n; i++) { b[i].lo = i; b[i].hi = i + 1; b[i].count = 0; }
}
__attribute__((noinline)) int total(const struct bin *b, int n) {
    int t = 0;
    for (int i = 0; i < n; i++) t += b[i].hi;
    return t;
}
int main(int argc, char **argv) {
    struct bin bins[3];
    fill(bins, 3);
    struct trip t = {argc + 4, argc + 5, argc + 6};
    int s = total(bins, 3); // HERE
    printf("%d %d %d %d\\n", s, t.x, t.y, t.z);
    return 0;
}
"""


def _gcc():
    gcc = shutil.which("gcc")
    version = subprocess.run([gcc, "--version"], capture_output=True, text=True) if gcc else None
    return gcc if version is not None and "Free Software Foundation" in version.stdout else None


def configured(tmp_path, name, compiler, optimization="O0"):
    config = tmp_path / f"{name}-{optimization}.json"
    config.write_text(
        json.dumps(
            {
                "language": "c",
                "sources": [name],
                "compiler": compiler,
                "adapter": os.environ["PEEKY_NATIVE_DAP"],
                "dwarfdump": os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
                "dsymutil": os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
                "sysroot": os.environ.get("PEEKY_NATIVE_SYSROOT") if compiler != _gcc() else None,
                "optimization": optimization,
            }
        )
    )
    return config


@pytest.mark.timeout(240)
@pytest.mark.parametrize("compiler", ["clang", "gcc"])
@pytest.mark.parametrize("optimization", ["O1", "O2"])
def test_an_optimized_build_previews_nothing(c_program, tmp_path, capsys, compiler, optimization):
    # At -O1 and -O2 a struct with an address may hold values the optimizer has not stored
    # yet (GCC: `t = {x = 64, y = 0, z = 0}` where the program prints {5, 6, 7}). As for
    # Rust, an optimized build shows the debugger's `T @ 0x…`.
    built = os.environ.get("PEEKY_NATIVE_CC", "clang") if compiler == "clang" else _gcc()
    if built is None:
        pytest.skip("needs GCC")
    c_program("opt.c", OPTIMIZED)
    config = configured(tmp_path, "opt.c", built, optimization)
    line = line_of(OPTIMIZED, "// HERE")
    command = ["probe", str(tmp_path), "--config", str(config), "--at", f"opt.c:{line}"]
    assert main([*command, "--capture", "bins,t,bins[1]", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert any("optimized build" in warning for warning in result["warnings"]), result
    for row in result["rows"]:
        if not row.get("unavailable"):
            assert not str(row.get("value")).startswith(("{", "[")), row


UNVOUCHED = """#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>
struct opaque;
struct node { int v; struct node *next; };
struct flex { int n; int data[]; };
enum mode { M0, M1, M2, M3 };
struct b2 { signed char sc : 3; unsigned char uc : 3; int : 0; char pc : 4; bool flag : 1; enum mode md : 2; short sh : 5; };
typedef int Row[3];
struct rec { Row r; int tail; };
int main(int argc, char **argv) {
    int k = argc + 2;
    int a[k];
    int b[k + 3];
    for (int i = 0; i < k; i++) a[i] = 10 + i;
    for (int i = 0; i < k + 3; i++) b[i] = 20 + i;
    int backing[4] = {1, 2, 3, 4};
    struct opaque *op = (struct opaque *)backing;
    struct node n2 = {2, NULL}, n1 = {1, &n2};
    struct node *np = &n1;
    struct node **npp = &np;
    struct flex *fx = malloc(sizeof(struct flex) + 3 * sizeof(int));
    fx->n = 3; fx->data[0] = 10; fx->data[1] = 11; fx->data[2] = 12;
    struct b2 bt = {-3, 6, -2, true, M3, -9};
    wchar_t w[4] = {L'a', 0, L'b', 0};
    Row r = {1, 2, 3};
    struct rec rc = {{7, 8, 9}, 4};
    printf("pc=%d %d %d %p %d %d %d %d %d\\n", bt.pc, a[k - 1], b[k + 2], (void *)op, (*npp)->v, fx->n, (int)w[2], r[2], rc.tail); // HERE
    return 0;
}
"""


@pytest.mark.timeout(240)
@pytest.mark.parametrize("compiler", ["clang", "gcc"])
def test_what_the_debug_information_cannot_vouch_for_is_not_previewed(
    c_program, tmp_path, capsys, compiler
):
    # With clang and GCC, previews that trust the debugger go wrong: a variable-length
    # array previewed another's length (clang: `a = [10, 11, 12, 0, -1882931200, 1]`) or
    # a wrong one (GCC: `b` as `int[5]`, holding 6); `npp[0]` on a pointer to a pointer
    # previewed the struct where the pointer belongs; `op[0]` on a pointer to an
    # incomplete type showed `{}`; a char-typed bitfield read as an unsigned character
    # (`sc = '\\x05'` for -3); a wide character array ended at its first NUL (`L"a"`); an
    # unnamed bitfield LLDB adds showed as `0`; a typedef'd array as `{[0] = 1, …}`.
    built = os.environ.get("PEEKY_NATIVE_CC", "clang") if compiler == "clang" else _gcc()
    if built is None:
        pytest.skip("needs GCC")
    c_program("unvouched.c", UNVOUCHED)
    config = configured(tmp_path, "unvouched.c", built)
    line = line_of(UNVOUCHED, "// HERE")
    selectors = "a,b,op[0],npp[0],npp[0][0],fx[0],bt,bt.sc,bt.uc,bt.pc,w,r,rc"
    command = ["probe", str(tmp_path), "--config", str(config), "--at", f"unvouched.c:{line}"]
    assert main([*command, "--capture", selectors, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    rows = {row["selector"]: row for row in result["rows"]}
    # No constant bound: the debugger's text, never a length it guessed.
    for name in ("a", "b"):
        assert " @ 0x" in rows[name]["value"] and "[1" not in rows[name]["value"], rows[name]
    # A flexible array member has no length either.
    assert rows["fx[0]"]["value"] == "{n = 3, data = […]}", rows["fx[0]"]
    # Declared, never defined: no members to show.
    assert " @ 0x" in rows["op[0]"]["value"] and "{" not in rows["op[0]"]["value"]
    # `npp[0]` is a pointer, shown as its address; `npp[0][0]` what it points at.
    assert rows["npp[0]"]["value"].startswith("0x") and "{" not in rows["npp[0]"]["value"]
    assert rows["npp[0][0]"]["value"].startswith("{v = 1, next = 0x"), rows["npp[0][0]"]
    # A plain `char` is signed or not as the platform's ABI says: the program's own
    # output tells which.
    pc = result["output"].split("pc=", 1)[1].split()[0]
    assert pc in ("-2", "14"), result["output"]
    expected = f"{{sc = -3, uc = 6, pc = {pc}, flag = true, md = M3, sh = -9}}"
    assert rows["bt"]["value"] == expected, rows["bt"]
    # The member rows too, where LLDB showed `'\\x05'`.
    assert (rows["bt.sc"]["value"], rows["bt.uc"]["value"], rows["bt.pc"]["value"]) == (
        "-3",
        "6",
        pc,
    )
    assert rows["w"]["value"] == "[L'a', L'\\0', L'b', L'\\0']", rows["w"]
    assert rows["r"]["value"] == "[1, 2, 3]" and rows["rc"]["value"] == "{r = [7, 8, 9], tail = 4}"
    # The evidence store keeps what the row shows.
    assert main(["sql", "SELECT value FROM last_run WHERE selector = 'npp[0]'", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"][0]["value"].startswith("0x")


NOTED = """#include <setjmp.h>
#include <stdio.h>
struct pt { int x, y; };
struct box { struct pt lo; struct pt hi; int tag; };
static jmp_buf env;
static int count;
static void jump(void) {
    if (count++ < 2) longjmp(env, 1);
}
__attribute__((noinline)) static int twice(int v) { return v * 2; }
static int looped(void) {
    int total = 0, x = 0;
    do {
        struct pt y = { twice(x), 2 }; // DO_STRUCT
        total += y.x + y.y;
        x++;
    } while (x < 3);
    return total;
}
int main(void) {
    struct pt before = {1, 2};
    int k = setjmp(env);
    struct pt mid = {k + count, 10 * count};
    printf("pass %d mid={%d %d}\\n", count, mid.x, mid.y); // STOP
    struct box late = {{count, count + 1}, {count * 2, count * 3}, 100 + count};
    printf("late={{%d %d} {%d %d} %d} %d\\n", late.lo.x, late.lo.y, late.hi.x, late.hi.y, late.tag, looped());
    jump();
    return before.x;
}
"""


@pytest.mark.timeout(120)
def test_what_is_read_through_a_noted_local_carries_its_note(c_program, capsys):
    # A local Peeky shows but cannot vouch for (declared below a stop after `setjmp`, a
    # `do` body's first line) carries its note, and so do its preview's fields and its
    # children: plain, `late.lo` would read `{x = 1073741824, y = 0}`. The notes (read
    # through the local) cover previews too, and the evidence keeps them with the
    # preview's text.
    source = c_program("noted.c", NOTED)
    for marker, root, fields in (
        ("// STOP", "late", ("late.lo", "late.lo.x", "late.tag")),
        ("// DO_STRUCT", "y", ("y.x",)),
    ):
        line = line_of(NOTED, marker)
        capture = ",".join((root, *fields))
        assert main(["probe", str(source), "--at", str(line), "--capture", capture, "--json"]) == 0
        result = json.loads(capsys.readouterr().out)["rows"]
        rows = {row["selector"]: row for row in result if row["hit"] == 1}
        note = rows[root].get("note")
        assert note and rows[root]["value"].startswith("{"), rows[root]
        through = f"read through `{root}`: {note}"
        for field in fields:
            assert rows[field].get("note") == through, rows[field]
        assert rows[fields[0]]["value"].startswith("{" if root == "late" else ""), rows[fields[0]]
        query = "SELECT selector, value, note FROM last_run WHERE hit = 1"
        assert main(["sql", query, "--json"]) == 0
        stored = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
        for selector in (root, *fields):
            assert stored[selector]["value"] == rows[selector]["value"], stored[selector]
            assert stored[selector]["note"] == rows[selector]["note"], stored[selector]
        with session(source, line, capsys) as get:
            shown = get(root)[root]
            assert shown.get("note") == note
            assert shown["children"] and all(c.get("note") == through for c in shown["children"])
