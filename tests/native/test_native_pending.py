"""C and C++ locals declared below the stop are never shown as values (#74)."""

import json
import os

import pytest

from peeky.cli import main

BUCKET = """#include <stdio.h>

struct pt { long x, y; };

static long take(long now_ms, long *last) {
    long elapsed = now_ms - *last;
    *last = now_ms;
    long refill = elapsed / 1000;
    struct pt p = {refill, elapsed};
    static int calls = 0;
    calls++;
    return p.x + p.y + calls;
}

int main(void) {
    long last = 0, total = 0;
    int x = 1;
    for (int i = 1; i <= 3; i++) {
        total += take(i * 600, &last) + x;
        int x = 2;
        total += x;
    }
    printf("TOTAL:%ld\\n", total);
    return 0;
}
"""


@pytest.fixture
def bucket(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "bucket"
    root.mkdir()
    (root / "bucket.c").write_text(BUCKET)
    return root / "bucket.c"


def probe(capsys, *argv):
    code = main(["probe", *map(str, argv), "--json", "--quiet", "--no-save"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(120)
def test_locals_declared_below_the_stop_are_withheld_until_their_line(bucket, capsys):
    code, result = probe(
        capsys, bucket, "--at", "7", "--capture", "elapsed,refill,p,p.x", "--hits", "2"
    )
    assert code == 0, result.get("error")
    rows = {(row["hit"], row["selector"]): row for row in result["rows"]}
    for hit in (1, 2):
        assert rows[hit, "elapsed"]["value"] == "600"
        # Hit 2 holds hit 1's leftovers in these slots: never shown as values.
        for name, line in (("refill", 8), ("p", 9), ("p.x", 9)):
            row = rows[hit, name]
            assert "value" not in row and f"declared at line {line}, below" in row["error"]
    code, after = probe(capsys, bucket, "--at", "10", "--capture", "refill,p.x")
    assert [row["value"] for row in after["rows"]] == ["0", "0"]


@pytest.mark.timeout(120)
def test_a_shadowed_local_declared_below_is_withheld_and_the_outer_one_shown(bucket, capsys):
    code, result = probe(capsys, bucket, "--at", "19", "--capture", "x", "--hits", "2")
    assert code == 0, result.get("error")
    main(["session", "start", str(bucket), "--at", "19", "--hit", "2"])
    capsys.readouterr()
    try:
        assert main(["session", "inspect", "--json"]) == 0
        snapshot = json.loads(capsys.readouterr().out)
    finally:
        main(["session", "close"])
        capsys.readouterr()
    shown = {v["name"]: v for s in snapshot["scopes"] for v in s["variables"]}
    inner = [v for name, v in shown.items() if name.startswith("x") and name.endswith(":20")]
    outer = [v for name, v in shown.items() if name.startswith("x") and name.endswith(":17")]
    # lldb-dap names both `x @ bucket.c:N` while the inner one is in scope.
    assert inner and inner[0]["unavailable"] and inner[0]["settable"] is False
    assert outer and outer[0]["value"] == "1"


@pytest.mark.timeout(120)
def test_a_local_declared_below_cannot_be_set(bucket, capsys):
    code, result = probe(capsys, bucket, "--at", "7", "--capture", "elapsed", "--set", "refill=5")
    assert code == 1 and result["error"]["code"] == "INVALID_REQUEST"
    assert "declared at line 8" in result["error"]["message"]
    code, result = probe(capsys, bucket, "--at", "7", "--capture", "elapsed", "--set", "elapsed=5")
    assert code == 0 and result["interventions"][0]["after"] == "5"


@pytest.mark.timeout(120)
def test_a_local_declared_on_the_stop_line_is_shown_with_a_note(bucket, capsys):
    # Line 8 declares `refill`: at its breakpoint the declaration has not run, so the
    # value is leftovers (hit 2: hit 1's). The note says so; `elapsed` gets none.
    code, result = probe(capsys, bucket, "--at", "8", "--capture", "refill,elapsed", "--hits", "2")
    assert code == 0, result.get("error")
    notes = {(row["hit"], row["selector"]): row.get("note") for row in result["rows"]}
    assert notes[1, "refill"] and "leftover storage" in notes[1, "refill"]
    assert notes[2, "refill"] and notes[1, "elapsed"] is None
    assert any(
        warning.startswith("refill: Declared on the stop line") for warning in result["warnings"]
    )
