"""C struct, union and array previews (#136), on real builds.

Found by the sixth agent evaluation's `histo` case: `bins  bin[4] @ 0x16bb9f278`, so the
agent listed eight selectors by hand to read the bins' bounds.
"""

import contextlib
import json
import os

import pytest

from peeky.cli import main

# The evaluation's case, as the agent had it.
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
    # #136: `bins  bin[4]  bin[4] @ 0x16bb9f278` and `bins[3]  bin  bin @ 0x…`.
    source = c_program("histo.c", HISTO)
    args = ["--arg", "0", "--arg", "7", "--arg", "7"]
    capture = "bins,bins[3],bins[3].hi"
    assert main(["probe", str(source), "--at", "28", *args, "--capture", capture, "--json"]) == 0
    rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    assert rows["bins"]["value"] == BINS, rows["bins"]
    assert rows["bins[3]"]["value"] == "{lo = 6, hi = 7, count = 0}", rows["bins[3]"]
    # A field stays selectable, and settable, as before (#105, #141).
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
        # shows as LLDB writes it (no NUL in `name`: its 8 bytes), cut at 40 characters.
        named = rows["nm"]["value"]
        assert named.startswith('{name = "abcdefgh", text = 0x'), named
        assert '"txt"' not in named and "where = 0x" in named and "x = 1" not in named
        assert 'long_name = "' + "z" * 35 + '"...}' in named, named
        # Declared below the stop: its storage holds leftovers, so nothing is shown.
        assert rows["later"].get("unavailable") and "value" not in rows["later"], rows["later"]
        # The listing of a 2-D array previews each row.
        listed = {c["selector"]: c["value"] for c in rows["grid"]["children"]}
        assert listed == {"grid[0]": "[1, 2, 3]", "grid[1]": "[4, 5, 6]"}


OPTIMIZED = """#include <stdio.h>
struct bin { int lo; int hi; int count; };
struct pair { long a; long b; };
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
    struct pair p = {argc, argc * 2};
    int t = total(bins, 3); // HERE
    printf("%d %ld\\n", t, p.a + p.b);
    return 0;
}
"""


@pytest.mark.timeout(120)
def test_an_optimized_build_previews_only_what_has_an_address(c_program, tmp_path, capsys):
    # A struct in memory is read as at -O0; one the build keeps in registers (pieces of
    # a location, no address) keeps the debugger's text: no preview is made of pieces.
    c_program("opt.c", OPTIMIZED)
    config = tmp_path / "native.json"
    config.write_text(
        json.dumps(
            {
                "language": "c",
                "sources": ["opt.c"],
                "compiler": os.environ.get("PEEKY_NATIVE_CC", "clang"),
                "adapter": os.environ["PEEKY_NATIVE_DAP"],
                "dwarfdump": os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
                "dsymutil": os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
                "sysroot": os.environ.get("PEEKY_NATIVE_SYSROOT"),
                "optimization": "O2",
            }
        )
    )
    line = line_of(OPTIMIZED, "// HERE")
    command = ["probe", str(tmp_path), "--config", str(config), "--at", f"opt.c:{line}"]
    assert main([*command, "--capture", "bins,p", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    rows = {row["selector"]: row for row in result["rows"]}
    assert result["warnings"] and any("optimized build" in w for w in result["warnings"]), result
    bins = rows["bins"]
    expected = "[{lo = 0, hi = 1, count = 0}, {lo = 1, hi = 2, count = 0}, "
    assert bins.get("value") == expected + "{lo = 2, hi = 3, count = 0}]", bins
    pair = rows["p"]
    assert pair.get("unavailable") or not str(pair.get("value")).startswith("{"), pair
