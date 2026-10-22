"""`probe --set-at`: several interventions at their own lines and hits, on real C builds
(#137)."""

import json
import os
import threading

import pytest

from peeky.cli import WORKER_TOKEN_ENV, main

LINKED = """#include <stdio.h>
static int widths[4];
int main(void) {
  int total = 0;
  for (int i = 0; i < 4; i++) {
    int width = i + 1; // DECLARE

    total += width; // ADD
    widths[i] = width * 10; // REUSE
  }
  printf("TOTAL:%d W:%d,%d,%d,%d\\n", total, widths[0], widths[1], widths[2], widths[3]);
  return 0;
}
"""
TOKEN = "set-at-test-secret-token-1234"


def line_of(marker):
    return next(i for i, text in enumerate(LINKED.splitlines(), 1) if marker in text)


DECLARE, ADD, REUSE = line_of("DECLARE"), line_of("ADD"), line_of("REUSE")
BLANK = DECLARE + 1


@pytest.fixture
def linked(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "linked"
    root.mkdir()
    (root / "linked.c").write_text(LINKED)
    return root


def probe(capsys, root, *argv):
    code = main(["probe", str(root), "--at", f"linked.c:{ADD}", *argv, "--json", "--quiet"])
    return code, json.loads(capsys.readouterr().out)


def queried(capsys, *argv):
    assert main(["queries", *argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)["rows"]


def changes(result):
    return [
        (change["name"], change["before"], change["after"], change["line"], change["hit"])
        for change in result["interventions"]
    ]


@pytest.mark.timeout(240)
def test_a_change_at_one_line_is_restored_at_the_next_in_one_probe(linked, capsys):
    # #137 (sixth evaluation, `stock`): two linked changes, one to test and one to undo
    # it before a later use, took a session of 14 calls.
    code, plain = probe(capsys, linked, "--capture", "total,width", "--hits", "all")
    assert code == 0 and "TOTAL:10 W:10,20,30,40" in plain["output"]
    # Changed at ADD's 3rd stop and restored at REUSE's 3rd, which is not captured.
    restore = ("--set-at", f"{REUSE}@3", "width=3")
    code, result = probe(
        capsys,
        linked,
        "--capture",
        "total,width",
        "--hits",
        "all",
        "--set-at",
        f"linked.c:{ADD}@3",
        "width=100",
        *restore,
    )
    assert code == 0, result.get("error")
    assert "TOTAL:107 W:10,20,30,40" in result["output"]
    assert changes(result) == [("width", "3", "100", ADD, 3), ("width", "100", "3", REUSE, 3)]
    assert [change["capture_hit"] for change in result["interventions"]] == [3, 3]
    observed = {
        (row["hit"], row["selector"]): row["value"]
        for row in result["rows"]
        if row["phase"] == "observed"
    }
    assert observed[3, "width"] == "3" and observed[4, "total"] == "103"
    shown = [row for row in result["rows"] if row["phase"] == "after-set"]
    assert [(row["hit"], row["selector"], row["value"]) for row in shown] == [
        (3, "total", "3"),
        (3, "width", "100"),
    ]
    assert "unapplied" not in result and not any("not applied" in w for w in result["warnings"])
    # Without the restore, line REUSE reads the change too.
    code, alone = probe(capsys, linked, "--capture", "total", "--set-at", f"{ADD}@3", "width=100")
    assert code == 0 and "TOTAL:107 W:10,20,1000,40" in alone["output"]

    # The evidence: each change with its line and hit, in the order applied.
    stored = queried(capsys, "run", "--param", "run=2")
    assert {row["phase"] for row in stored} == {"observed", "after-set"}
    query = (
        "SELECT name, before, after, line, hit, capture_hit FROM interventions "
        "WHERE run_id = 2 ORDER BY rowid"
    )
    assert main(["sql", query, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert [tuple(row.values()) for row in rows] == [
        ("width", "3", "100", ADD, 3, 3),
        ("width", "100", "3", REUSE, 3, 3),
    ]
    effect = queried(capsys, "set_effect", "--param", "run=2")
    assert [
        (row["hit"], row["line"], row["selector"], row["observed"], row["after_set"])
        for row in effect
    ] == [
        (3, ADD, "total", "3", "3"),
        (3, ADD, "width", "3", "100"),
        (3, REUSE, "width", "100", "3"),
    ]
    wide = queried(capsys, "wide", "--param", "run=2")
    sets = {(row["hit"], row["phase"]): row["sets"] for row in wide}
    assert json.loads(sets[3, "after-set"]) == [f"width: 3 -> 100 (line {ADD}, hit 3, in main)"]
    assert json.loads(sets[4, "observed"]) == [f"width: 100 -> 3 (line {REUSE}, hit 3, in main)"]
    assert sets[1, "observed"] is None and sets[3, "observed"] is None
    # diff against the run without them: their (sets) row, the change itself, then its
    # effect (tenth evaluation: the change came after its effects).
    diff = queried(capsys, "diff", "--param", "a=1", "--param", "b=2")
    assert [row["selector"] for row in diff[:2]] == ["(output)", "(sets)"]
    # runs.sets holds requests: each says where it was asked for (review of PR #214).
    assert json.loads(diff[1]["value_b"]) == [
        ["width", "100", f"requested at {ADD}@3"],
        ["width", "3", f"requested at {REUSE}@3"],
    ]
    shown = [
        (row["hit"], row["phase"], row["selector"], row["value_a"], row["value_b"], row["note"])
        for row in diff[2:]
    ]
    # `total`'s after-set value is its observed one (the change was to `width`): no
    # difference of its own (seventh evaluation, finding 10).
    assert shown == [
        (3, "after-set", "width", None, "100", "intervention only in run b"),
        (4, "observed", "total", "6", "103", None),
    ], shown


@pytest.mark.timeout(240)
def test_a_set_at_its_line_never_reaches_is_reported_not_dropped(linked, capsys):
    code, result = probe(
        capsys, linked, "--capture", "total", "--hits", "2", "--set-at", f"{REUSE}@7", "width=1"
    )
    # Its values are evidence, but the experiment did not happen: exit code 4 (#151).
    assert code == 4 and result["status"] == "completed" and result["interventions"] == []
    assert "TOTAL:10 W:10,20,30,40" in result["output"]
    assert result["unapplied"] == [
        {"option": "--set-at", "at": f"{REUSE}@7", "name": "width", "value": "1", "stops": 4}
    ]
    assert (
        f"--set-at linked.c:{REUSE}@7 width=1 was not applied: line {REUSE} stopped 4 time(s) "
        "before the probe ended, so its hit 7 never came." in result["warnings"]
    )
    # The probe ran past its captured hits for it: line REUSE's stops were all counted.
    assert result["hits"] == 2 and len(result.get("set_pauses_ms", [])) == 4
    runs = queried(capsys, "outputs", "--param", "b=1", "--param", "a=1")
    # A request whose hit never came says so (review of PR #214); `outputs` no longer
    # has `set_hit` (ninth evaluation).
    assert json.loads(runs[0]["sets"]) == [
        ["width", "1", f"requested at {REUSE}@7, not made: that hit never came"]
    ]
    assert "set_hit" not in runs[0] and queried(capsys, "set_effect") == []


@pytest.mark.timeout(240)
def test_one_refused_set_at_fails_the_probe_and_names_its_stop(linked, capsys):
    # `width` is declared on line DECLARE: not initialized at its 3rd stop there. The
    # change at ADD's 2nd stop (earlier) was made; the run is not the one asked for.
    code, result = probe(
        capsys,
        linked,
        "--capture",
        "total",
        "--set-at",
        f"{ADD}@2",
        "width=50",
        "--set-at",
        f"{DECLARE}@3",
        "width=9",
    )
    assert code == 1 and result["status"] == "failed"
    error = result["error"]
    assert error["code"] == "MUTATION_REJECTED", error
    assert error["message"].startswith(
        f"--set-at linked.c:{DECLARE}@3 width=9 was refused at that stop (in main): 'width' is "
        f"declared on line {DECLARE}"
    ), error
    assert (
        "(1 earlier intervention(s) applied; the run is not the experiment asked for)"
        in (error["message"])
    )
    assert changes(result) == [("width", "2", "50", ADD, 2)]
    failed = queried(capsys, "failures")
    assert failed[0]["error_code"] == "MUTATION_REJECTED"
    # A line with no code of its own is refused, never moved to the next line.
    code, result = probe(capsys, linked, "--capture", "total", "--set-at", f"{BLANK}", "width=1")
    assert code == 1 and result["error"]["code"] == "INVALID_REQUEST"
    assert f"line {BLANK} has no code of its own" in result["error"]["message"]
    assert result["interventions"] == []
    # Another file is refused before anything runs.
    code, result = probe(capsys, linked, "--set-at", "other.c:3", "x=1")
    assert code == 2 and "the --at one (linked.c)" in result["error"]["message"]


@pytest.mark.timeout(240)
def test_set_at_lines_map_through_a_candidate_and_run_on_a_remote_worker(
    linked, capsys, monkeypatch
):
    from peeky.native import NativeTarget
    from peeky.worker import make_server

    # Lines in the original's numbering: the edit adds a line above them.
    edit = ("--old", "  int total = 0;\n", "--new", "  int total = 0;\n  int unused = 0;\n")
    sets = ("--set-at", f"{ADD}@3", "width=100", "--set-at", f"{REUSE}@3", "width=3")
    code, result = probe(capsys, linked, "--capture", "width", *edit, *sets)
    assert code == 0, result.get("error")
    assert changes(result) == [
        ("width", "3", "100", ADD + 1, 3),
        ("width", "100", "3", REUSE + 1, 3),
    ]
    assert "TOTAL:107 W:10,20,30,40" in result["output"]

    target = NativeTarget(
        sources=("linked.c",),
        compiler=os.environ.get("PEEKY_NATIVE_CC", "clang"),
        adapter=os.environ["PEEKY_NATIVE_DAP"],
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
    )
    server = make_server(linked, TOKEN, 0, adapter=target)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(WORKER_TOKEN_ENV, TOKEN)
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        code, result = probe(capsys, linked, "--capture", "width", *sets, "--worker", url)
        assert code == 0, result.get("error")
        assert changes(result) == [("width", "3", "100", ADD, 3), ("width", "100", "3", REUSE, 3)]
        assert "TOTAL:107 W:10,20,30,40" in result["output"]
        assert result["probe"]["set_at"] == [[ADD, 3, "width", "100"], [REUSE, 3, "width", "3"]]
    finally:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


POINT = """#include <stdio.h>
struct pt { int x; int y; };
int main(void) {
  struct pt p = {0, 0};
  int sum = 0;
  for (int i = 0; i < 3; i++) {
    p.x = i;
    sum += p.x + p.y; // SUM
    p.y += 1; // STEP
  }
  printf("SUM:%d\\n", sum);
  return 0;
}
"""


@pytest.mark.timeout(240)
def test_struct_previews_read_at_captures_and_after_set_with_set_at(linked, capsys):
    # #136's previews in a --set-at probe: captured rows and after-set rows show the
    # struct's fields; the stop at STEP, which captures nothing, reads none.
    (linked / "linked.c").write_text(POINT)
    at = {
        marker: next(i for i, text in enumerate(POINT.splitlines(), 1) if marker in text)
        for marker in ("SUM", "STEP")
    }
    argv = ["probe", str(linked), "--at", f"linked.c:{at['SUM']}", "--capture", "p,p.y"]
    sets = ["--set-at", f"{at['SUM']}@2", "p.y=10", "--set-at", f"{at['STEP']}@2", "p.y=1"]
    assert main([*argv, "--hits", "all", *sets, "--json", "--quiet"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "SUM:15" in result["output"], result["output"]
    shown = [
        (row["hit"], row["phase"], row["value"]) for row in result["rows"] if row["selector"] == "p"
    ]
    assert shown == [
        (1, "observed", "{x = 0, y = 0}"),
        (2, "observed", "{x = 1, y = 1}"),
        (2, "after-set", "{x = 1, y = 10}"),
        (3, "observed", "{x = 2, y = 2}"),
    ], shown
    assert [(c["line"], c["hit"], c["after"]) for c in result["interventions"]] == [
        (at["SUM"], 2, "10"),
        (at["STEP"], 2, "1"),
    ]


THROUGH = """#include <stdio.h>
struct P { long x; long y; };
struct Q { struct P *pp; long z; };
__attribute__((noinline)) long fc(struct Q a) { return a.pp->y; } __attribute__((noinline)) long fd(struct Q a) { return a.pp->x * 3; } // BOTH
int main(void) {
  struct P p = {1, 2};
  long total = 0;
  struct Q q = {&p, 0};
  total += fc(q) + fd(q); // CALL
  printf("T %ld\\n", total);
  return 0;
}
"""


@pytest.mark.timeout(240)
def test_an_unmade_set_beside_a_made_set_at_is_named_and_refusals_group(linked, capsys):
    # #151 review 2: "No intervention was applied" while a --set-at was made.
    code, result = probe(
        capsys,
        linked,
        "--capture",
        "total",
        "--hits",
        "all",
        "--set",
        "total=5",
        "--set-hit",
        "9",
        "--set-at",
        f"{ADD}@2",
        "width=7",
    )
    assert code == 4 and [c["at"] for c in result["interventions"]] == [f"{ADD}@2"], result
    assert not any("No intervention" in warning for warning in result["warnings"])
    assert (
        f"--set total=5 was not applied: the program stopped 4 time(s) at line {ADD}, before "
        "hit 9." in result["warnings"]
    ), result["warnings"]
    # A refusal before the run names the functions sharing a reason once (#149's format).
    (linked / "linked.c").write_text(THROUGH)
    both = next(i for i, text in enumerate(THROUGH.splitlines(), 1) if "// BOTH" in text)
    call = next(i for i, text in enumerate(THROUGH.splitlines(), 1) if "// CALL" in text)
    argv = ["probe", str(linked), "--at", f"linked.c:{call}", "--set-at", f"{both}@1"]
    # Refused before it ran: exit 2 (seventh evaluation, finding 14).
    assert main([*argv, "a.pp.x=1", "--json", "--quiet"]) == 2
    message = json.loads(capsys.readouterr().out)["error"]["message"]
    assert f"cannot be changed at linked.c:{both}, at any stop of this build: in fc and fd, " in (
        message
    ), message
    assert message.count("is a pointer") == 1 and message.count("change the value where") == 1
