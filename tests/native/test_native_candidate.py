"""Probing a candidate fix (`probe --old/--new`) on a real lldb-dap, locally and remotely."""

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from peeky.cli import WORKER_TOKEN_ENV, main

LOOP = """#include <stdio.h>
int main(void) {
  int total = 0;
  for (int i = 0; i < 3; i++) {
    total += i;
  }
  printf("TOTAL:%d\\n", total);
  return 0;
}
"""
TOKEN = "candidate-test-secret-token-123-0123456789abcdef"


@pytest.fixture
def loop(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "loop"
    root.mkdir()
    (root / "loop.c").write_text(LOOP)
    return root


def probe(capsys, *argv):
    code = main(["probe", *map(str, argv), "--json", "--quiet"])
    return code, json.loads(capsys.readouterr().out)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def values(result, name):
    return [row["value"] for row in result["rows"] if row["selector"] == name]


@pytest.mark.timeout(120)
def test_a_candidate_probe_runs_the_edit_and_never_touches_the_file(loop, capsys):
    source = loop / "loop.c"
    before = source.read_bytes()
    common = (loop, "--at", "loop.c:5", "--capture", "total,i", "--hits", "all")
    code, original = probe(capsys, *common)
    assert code == 0 and values(original, "total") == ["0", "0", "1"]
    edit = ("--old", "total += i;", "--new", "total += 2 * i;")
    code, candidate = probe(capsys, *common, *edit)
    assert code == 0, candidate.get("error")
    assert values(candidate, "total") == ["0", "0", "2"] and "TOTAL:6" in candidate["output"]
    expected = sha(LOOP.replace("total += i;", "total += 2 * i;").encode())
    assert candidate["candidate"]["sha256"] == expected
    assert candidate["candidate"]["line"] == candidate["candidate"]["original_line"] == 5
    assert "+    total += 2 * i;" in candidate["candidate"]["diff"]
    assert "edit" not in candidate["probe"] and candidate["probe"]["line"] == 5
    assert source.read_bytes() == before

    # Lines after an edit that adds lines are mapped into the candidate's numbering.
    code, shifted = probe(
        capsys,
        loop,
        "--at",
        "loop.c:5",
        "--capture",
        "total,extra",
        "--old",
        "  int total = 0;\n",
        "--new",
        "  int total = 0;\n  int extra = 41;\n  extra += 1;\n",
    )
    assert code == 0, shifted.get("error")
    assert shifted["candidate"]["line"] == 7 and shifted["rows"][0]["line"] == 7
    assert values(shifted, "extra") == ["42"]
    assert source.read_bytes() == before

    store = sqlite3.connect(original["evidence"]["store"])
    runs = store.execute("SELECT source_sha256, edit FROM runs ORDER BY run_id").fetchall()
    assert runs[0] == (sha(before), None)
    assert runs[1][0] == expected and "+    total += 2 * i;" in runs[1][1]
    assert main(["queries", "diff", "--param", "a=1", "--param", "b=2", "--json"]) == 0
    diff = json.loads(capsys.readouterr().out)["rows"]
    assert {(row["selector"], row["value_a"], row["value_b"]) for row in diff} >= {
        ("total", "1", "2")
    }


@pytest.mark.timeout(120)
def test_a_line_inside_an_edit_that_moves_lines_is_refused_before_building(loop, capsys):
    code, result = probe(
        capsys, loop, "--at", "loop.c:5", "--old", "total += i;", "--new", "total += i;\n    i++;"
    )
    assert code == 2 and result["error"]["code"] == "INVALID_REQUEST"
    assert "inside the edit" in result["error"]["message"]


@pytest.mark.timeout(120)
def test_a_candidate_that_does_not_compile_is_build_failed(loop, capsys):
    before = (loop / "loop.c").read_bytes()
    code, result = probe(
        capsys, loop, "--at", "loop.c:5", "--old", "total += i;", "--new", "total += ;"
    )
    # Refused before the launch request (#190): exit 2, no table.
    assert code == 2 and result["error"]["code"] == "BUILD_FAILED"
    assert result["refused_before_launch"] is True
    assert "error" in result["error"]["message"] and "loop.c:5" in result["error"]["message"]
    assert "edit" in result["error"]["hint"] and result["rows"] == []
    assert (loop / "loop.c").read_bytes() == before


@pytest.mark.timeout(180)
def test_a_remote_worker_probes_the_candidate(loop, capsys, monkeypatch):
    from peeky.native import NativeTarget
    from peeky.worker import make_server

    target = NativeTarget(
        sources=("loop.c",),
        compiler=os.environ.get("PEEKY_NATIVE_CC", "clang"),
        adapter=os.environ["PEEKY_NATIVE_DAP"],
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
    )
    server = make_server(loop, TOKEN, 0, adapter=target)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(WORKER_TOKEN_ENV, TOKEN)
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        code, result = probe(
            capsys,
            loop,
            "--at",
            "loop.c:5",
            "--capture",
            "total",
            "--hits",
            "3",
            "--old",
            "total += i;",
            "--new",
            "total += 10 * i;",
            "--worker",
            url,
        )
        assert code == 0, result.get("error")
        assert values(result, "total") == ["0", "0", "10"] and "TOTAL:30" in result["output"]
        assert result["candidate"]["sha256"] == sha(
            LOOP.replace("total += i;", "total += 10 * i;").encode()
        )
        # Several edits over HTTP: the worker gets a list and builds their combined effect.
        code, result = probe(capsys, loop, "--at", "loop.c:5", *TWO_EDITS, "--worker", url)
        assert code == 0, result.get("error")
        assert result["candidate"]["line"] == 6 and "TOTAL:103" in result["output"]
        # The edited file is the worker's copy, whose line 6 this is (#268 review).
        where = result["breakpoint"]["source"]["path"]
        assert where.startswith("worker:") and where.endswith("/loop.c"), where
        assert "peeky-build-" in where and result["breakpoint"]["line"] == 6
        # Without an edit, the worker names the compared source, shown as your file.
        code, plain = probe(capsys, loop, "--at", "loop.c:5", "--capture", "total", "--worker", url)
        assert code == 0, plain.get("error")
        assert Path(plain["breakpoint"]["source"]["path"]).resolve() == (loop / "loop.c").resolve()
    finally:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_a_candidates_breakpoint_names_the_copy_its_line_is_from(loop, capsys):
    # #268 review: the breakpoint named your loop.c with the candidate's line 6, which is
    # `}` in your file: an edit that adds a line above moves the line it means.
    from peeky.cli import CANDIDATE_LOCATIONS

    source = loop / "loop.c"
    code, plain = probe(capsys, loop, "--at", "loop.c:5", "--capture", "total")
    assert code == 0, plain.get("error")
    assert Path(plain["breakpoint"]["source"]["path"]).resolve() == source.resolve()
    assert plain["breakpoint"]["line"] == 5 and "candidate" not in plain
    edit = ("--old", "int total = 0;\n", "--new", "int total = 0;\n  int bonus = 100;\n")
    code, result = probe(capsys, loop, "--at", "loop.c:5", "--capture", "total", *edit)
    assert code == 0, result.get("error")
    where = Path(result["breakpoint"]["source"]["path"])
    assert where.name == "loop.c" and where.resolve() != source.resolve(), where
    assert "peeky-build-" in str(where)
    assert result["breakpoint"]["line"] == result["candidate"]["line"] == 6
    assert result["candidate"]["note"] == CANDIDATE_LOCATIONS
    code = main(["probe", str(loop), "--at", "loop.c:5", "--capture", "total", *edit, "--quiet"])
    text = capsys.readouterr().out
    assert code == 0 and f"candidate: {CANDIDATE_LOCATIONS}" in text, text


TWO_EDITS = (
    "--capture",
    "total,bonus",
    "--hits",
    "3",
    "--old",
    "int total = 0;\n",
    "--new",
    "int total = 0;\n  int bonus = 100;\n",
    "--old",
    'printf("TOTAL:%d\\n", total);',
    "--new",
    'printf("TOTAL:%d\\n", total + bonus);',
)


def test_a_candidate_with_several_edits_maps_the_line_through_them(loop, capsys):
    # #76: one edit adds a declaration above the probed line, another uses it below.
    source = loop / "loop.c"
    before = source.read_bytes()
    code, result = probe(capsys, loop, "--at", "loop.c:5", *TWO_EDITS)
    assert code == 0, result.get("error")
    assert result["candidate"]["line"] == 6 and result["candidate"]["original_line"] == 5
    assert values(result, "total") == ["0", "0", "1"] and values(result, "bonus") == ["100"] * 3
    assert "TOTAL:103" in result["output"] and source.read_bytes() == before
    assert "+  int bonus = 100;" in result["candidate"]["diff"]
    assert '+  printf("TOTAL:%d\\n", total + bonus);' in result["candidate"]["diff"]
