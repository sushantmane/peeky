"""Probing a candidate fix (`probe --old/--new`) on a real lldb-dap, locally and remotely."""

import hashlib
import json
import os
import sqlite3
import threading

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
TOKEN = "candidate-test-secret-token-123"


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
    assert code == 1 and result["error"]["code"] == "BUILD_FAILED"
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
    finally:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
