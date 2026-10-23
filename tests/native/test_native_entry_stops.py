"""A stop at a function's start, before its prologue stores the parameters (#279), on
real builds. GCC marks no prologue end in the line table, so LLDB stops a function whose
code is all on its first line at its first instruction, and a step into it stops there
too: `a` read the slot's leftovers (`a = 2` for `one(3)`), with no note. Each value must be
the one passed, or withheld with the note; with clang, which marks the prologue's end,
every value reads. With clang and GCC; lldb-dap 18 or 20, as the lane sets it."""

import json
import os
import shutil
import subprocess

import pytest

from peeky.cli import main

SOURCE = """#include <stdio.h>
struct big { long a, b, c, d, e; };
static int one(int a) { int b = a + 1; return a * b; } // ONE
static long sum(struct big s) { return s.a + s.e; } // SUM
static int twice(int v) { return v * 2; } static int quad(int w) { return twice(twice(w)); } // PAIR
static long down(long n) { return n < 2 ? 1 : n * down(n - 1); } // DOWN
static long many(long a1, long a2, long a3, long a4, long a5, long a6, long a7, long a8, long a9) { return a1 + a9; } // MANY
static int multi(int m) { // MULTI
    return m + 5; // MULTI_BODY
}
static int braced(int k)
{ // BRACED
    return k + 1;
}
static void noop(void) {}
int main(void) {
    struct big s = {1, 2, 3, 4, 5};
    noop();
    long r = one(3) + one(4) + sum(s) + quad(5) + down(3) + multi(8) + braced(2) + many(1, 2, 3, 4, 5, 6, 7, 8, 9); // MAIN
    printf("r=%ld\\n", r);
    return 0;
}
"""
OUTPUT = "r=90"
WHERE = (
    "no value yet: the stop is at its function's start, before the prologue stores the "
    "parameters: its storage holds leftovers (the build marks no prologue end, as GCC's never "
    "do, "
)
NOTE = (
    WHERE + "so LLDB stops a one-line function here). Read the arguments at the call, or put "
    "the body on a line of its own"
)
# A function whose body is on lines of its own (`braced`) can stop there too, where LLDB
# takes the next function's merged rows for a prologue's end (arm64: `noop`'s).
LATER = (
    WHERE + "and LLDB did not find it). Read the arguments at the call, or stop on a later line "
    "of the function"
)
# What each function's parameter holds, call by call, in the order the calls run.
PASSED = {
    "one": ("a", [3, 4]),
    "sum": ("s", ["{a = 1, b = 2, c = 3, d = 4, e = 5}"]),
    "quad": ("w", [5]),
    "twice": ("v", [5, 10]),
    "down": ("n", [3, 2, 1]),
    "multi": ("m", [8]),
    "braced": ("k", [2]),
    "many": ("a1", [1]),
}


def _is_gcc(compiler):
    try:
        version = subprocess.run([compiler, "--version"], capture_output=True, text=True)
    except OSError:
        return False
    return "Free Software Foundation" in version.stdout


def _gcc():
    gcc = shutil.which("gcc")
    return gcc if gcc and _is_gcc(gcc) else None


@pytest.fixture(params=["clang", "gcc"])
def entries(request, tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    compiler = os.environ.get("PEEKY_NATIVE_CC", "clang") if request.param == "clang" else _gcc()
    if compiler is None:
        pytest.skip("needs GCC")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "entry.c").write_text(SOURCE)
    config = tmp_path / "entry.json"
    config.write_text(
        json.dumps(
            {
                "language": "c",
                "sources": ["entry.c"],
                "compiler": compiler,
                "adapter": os.environ["PEEKY_NATIVE_DAP"],
                "dwarfdump": os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
                "dsymutil": os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
                "sysroot": os.environ.get("PEEKY_NATIVE_SYSROOT")
                if request.param == "clang"
                else None,
            }
        )
    )
    # What the build does, not the parameter's name: a lane may set PEEKY_NATIVE_CC to GCC.
    return "gcc" if _is_gcc(compiler) else "clang", tmp_path, config


def line_of(marker):
    return next(i for i, text in enumerate(SOURCE.splitlines(), 1) if f"// {marker}" in text)


def probe(root, config, capsys, marker, capture, *extra):
    command = ["probe", str(root), "--config", str(config), "--at", f"entry.c:{line_of(marker)}"]
    code = main([*command, "--capture", capture, "--hits", "all", "--json", "--quiet", *extra])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(300)
def test_a_stop_before_the_prologue_stores_the_parameters_withholds_them(entries, capsys):
    compiler, root, config = entries
    seen, calls, withheld = {}, {name: 0 for name in PASSED}, []
    for marker, capture in (
        ("ONE", "a,b"),
        ("SUM", "s"),
        ("PAIR", "v,w"),
        ("DOWN", "n"),
        ("MANY", "a1,a9"),
        ("MULTI", "m"),
        ("BRACED", "k"),
    ):
        code, result = probe(root, config, capsys, marker, capture)
        assert code == 0 and OUTPUT in result["output"], result
        seen[marker] = result["rows"]
    print(compiler, json.dumps(seen, indent=1))
    for marker, rows in seen.items():
        for hit in sorted({row["hit"] for row in rows}):
            function = next(row["function"] for row in rows if row["hit"] == hit)
            name, values = PASSED[function]
            expected = values[calls[function]]
            calls[function] += 1
            (row,) = [row for row in rows if row["hit"] == hit and row["selector"] == name]
            # The value passed, or withheld with the note: never the slot's leftovers.
            if row.get("unavailable"):
                note = LATER if function == "braced" else NOTE
                assert row["error"] == note and row["settable"] is False, (marker, row)
                withheld.append((function, expected))
            else:
                assert row["value"] == str(expected), (marker, hit, row)
            if function == "many":
                # Passed on the stack (the ninth argument on arm64, past the sixth on
                # x86-64): the caller's memory, stored before the call, reads at the start.
                (stacked,) = [row for row in rows if row["hit"] == hit and row["selector"] == "a9"]
                assert stacked["value"] == "9", (marker, stacked)
            if function == "one":
                # Declared on the stop's line: withheld at either stop, with its own note
                # (clang) or the prologue's (GCC).
                (local,) = [row for row in rows if row["hit"] == hit and row["selector"] == "b"]
                assert local.get("unavailable") is True, (marker, local)
                assert local["error"] == (
                    NOTE
                    if row.get("unavailable")
                    else f"no value yet: declared on line {line_of('ONE')}, which has not run "
                    "yet at this stop (its first instruction), so not initialized: its storage "
                    f"holds leftovers; step once, or stop after line {line_of('ONE')}, to read it"
                ), (marker, local)
    assert calls == {name: len(values) for name, (_, values) in PASSED.items()}, calls
    if compiler == "clang":
        # Clang marks the prologue's end, where LLDB stops: every value reads.
        assert withheld == [], withheld
    else:
        # `one`, `quad` and `many` end where the next line's function starts, so LLDB finds
        # no end of their prologue and stops at their first instruction. (`twice` is
        # followed by `quad` on its own line, so LLDB takes its second row.)
        assert {("one", 3), ("one", 4), ("quad", 5), ("many", 1)} <= set(withheld), withheld
    # The multi-line function moves to its body, past the prologue, with the debugger's
    # word for it.
    (row,) = seen["MULTI"]
    assert row["line"] == line_of("MULTI_BODY") and row["value"] == "8", row


@pytest.mark.timeout(300)
def test_a_change_before_the_prologue_stores_the_parameters_says_so(entries, capsys):
    compiler, root, config = entries
    code, result = probe(root, config, capsys, "ONE", "a", "--set", "a=9")
    if compiler == "clang":
        # Past the prologue: the change is made, and the program sees it (`one` returns
        # 9 * 10 for 3 * 4).
        assert code == 0, result
        assert "r=168" in result["output"], result
        return
    assert code != 0 and result["error"]["code"] == "MUTATION_REJECTED", result
    assert result["error"]["message"].startswith(
        "'a' cannot be changed here: the stop is at its function's start, before the prologue "
        "stores the parameters, and the function's code may overwrite a change. Change the "
        "argument at the call, or put the body on a line of its own and stop there"
    ), result
    assert result["error"]["hint"] == (
        "Change the argument at the call (a stop on the caller's line), or at a stop past the "
        "function's prologue, as the message says."
    ), result


@pytest.mark.timeout(300)
def test_a_step_into_a_one_line_function_withholds_what_is_not_stored(entries, capsys):
    compiler, root, config = entries
    start = ["session", "start", str(root), "--config", str(config)]
    assert main([*start, "--at", f"entry.c:{line_of('MAIN')}"]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "step", "--into", "--json"]) == 0
        state = json.loads(capsys.readouterr().out)
        assert state["location"].endswith(f":{line_of('ONE')}"), state
        code = main(["session", "get", "a", "--json"])
        (row,) = json.loads(capsys.readouterr().out)
        if compiler == "clang":
            assert code == 0 and row["value"] == "3", row
        else:
            # LLDB steps to the first instruction, as it sets a breakpoint there.
            assert code == 1 and row["error"] == NOTE, row
    finally:
        main(["session", "close"])


@pytest.mark.timeout(300)
def test_a_stop_whose_address_is_unknown_is_withheld_on_a_gcc_prologues_line(
    entries, capsys, monkeypatch
):
    # Review of #279: where the debugger reports no load address (a module with no
    # `addressRange`, which lldb-dap 20 and Apple's may send with a frame's `moduleId`),
    # no stop's address can be placed. The rule then fails closed on a GCC prologue's line.
    compiler, root, config = entries

    def unknown(binary, module):
        raise ValueError("Missing or invalid native module load address")

    monkeypatch.setattr("peeky.native.load_bias", unknown)
    code, result = probe(root, config, capsys, "ONE", "a")
    assert code == 0 and OUTPUT in result["output"], result
    values = [row.get("value") for row in result["rows"]]
    if compiler == "clang":
        # Clang's breakpoints stop past the prologue's mark: nothing to withhold.
        assert values == ["3", "4"], result["rows"]
    else:
        unplaced = (
            "no value yet: Peeky cannot tell whether the stop is past its function's prologue, "
            "which stores the parameters: the debugger did not report where the program is "
            "loaded, and the build marks no prologue end (GCC's never do). Read the arguments "
            "at the call, or put the body on a line of its own"
        )
        shown = [(row.get("value"), row.get("error")) for row in result["rows"]]
        assert shown == [(None, unplaced)] * 2, result["rows"]
    # A line no prologue runs on reads as before.
    code, result = probe(root, config, capsys, "MULTI", "m")
    assert code == 0 and [row["value"] for row in result["rows"]] == ["8"], result["rows"]
