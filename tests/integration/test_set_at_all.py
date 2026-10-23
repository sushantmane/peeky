"""`--set-at LINE@all` against real debugpy (#265): a change at every stop of the line
reached only in the case to test, each read back; the cap; a field refused before the
run."""

import json

import pytest

from peeky.cli import main
from peeky.probe import MAX_HITS, _table

DEBOUNCE = """import sys


class Debounce:
    def __init__(self, threshold):
        self.stable = self.candidate = self.count = 0
        self.threshold = threshold


def debounce(threshold, signal):
    stable = candidate = count = 0
    events = []
    for ms, sample in enumerate(signal):
        level = int(sample)
        if level == stable:
            agrees = ms  # AGREES
            continue
        if level != candidate:
            candidate = level
            count = 0
        count += 1
        if count >= threshold:
            stable = level
            count = 0
            events.append(("P" if level else "R") + "@" + str(ms))
    return events


def main():
    events = debounce(int(sys.argv[1]), sys.argv[2])
    print("DEBOUNCE:%d:%s" % (len(events), ",".join(events)))
    db = Debounce(3)
    db.count += 1  # FIELD


main()
"""
ARGS = ("3", "00101101111110100100001101100000")
LOOP = """def main():
    total = 0
    for i in range(%d):
        total += i  # EACH
    print("TOTAL", total)


main()
"""


def line_of(text, marker):
    return next(i for i, line in enumerate(text.splitlines(), 1) if f"# {marker}" in line)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    return tmp_path


def probe(capsys, root, program, *argv, args=()):
    code = main(
        ["probe", str(root / program), *argv, "--json", "--quiet", "--no-save"]
        + [f"--arg={arg}" for arg in args]
    )
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(240)
def test_an_all_resets_a_python_count_wherever_the_input_agrees(state, capsys):
    (state / "debounce.py").write_text(DEBOUNCE)
    agrees = line_of(DEBOUNCE, "AGREES")
    at = ["--at", f"debounce.py:{agrees}"]
    code, plain = probe(capsys, state, "debounce.py", *at, args=ARGS)
    assert code == 0 and plain["output"].strip() == "DEBOUNCE:4:P@5,R@15,P@23,R@28", plain
    code, result = probe(
        capsys, state, "debounce.py", *at, "--capture", "ms,count", "--hits", "all",
        "--set-at", f"{agrees}@all", "count=0", args=ARGS,
    )  # fmt: skip
    assert code == 0, result
    assert result["output"].strip() == "DEBOUNCE:2:P@9,R@20", result["output"]
    changes = result["interventions"]
    assert [change["hit"] for change in changes] == list(range(1, result["hits"] + 1))
    assert [change["at"] for change in changes][:2] == [f"{agrees}@1", f"{agrees}@2"]
    assert all(change.get("all") and change["after"] == "0" for change in changes), changes
    assert {change["function"] for change in changes} == {"debounce"}
    assert any(change["before"] != "0" for change in changes), changes
    text = _table(result)
    assert f"set count: 0 -> 0  (--set-at line {agrees}@all, hit 1, in debounce)" in text
    assert f"more changes by --set-at line {agrees}@all, at hits 4 to {result['hits']}" in text
    # A field is refused before the run, @all or not: Python changes only a frame's own
    # variables.
    field = line_of(DEBOUNCE, "FIELD")
    code, refused = probe(
        capsys, state, "debounce.py", "--at", f"debounce.py:{field}",
        "--set-at", f"{field}@all", "db.count=0", args=ARGS,
    )  # fmt: skip
    assert code == 2 and refused["error"]["code"] == "MUTATION_REJECTED", refused


@pytest.mark.timeout(240)
def test_an_all_stops_at_the_cap_and_the_probe_exits_with_4(state, capsys):
    (state / "loop.py").write_text(LOOP % (MAX_HITS + 3))
    each = line_of(LOOP, "EACH")
    code, result = probe(
        capsys, state, "loop.py", "--at", f"loop.py:{each - 1}", "--set-at", f"{each}@all",
        "total=0",
    )  # fmt: skip
    assert code == 4 and result["status"] == "completed", result
    assert len(result["interventions"]) == MAX_HITS
    # Reset before each of the first 100 passes, then not: 99 + 100 + 101 + 102.
    assert result["output"].strip() == f"TOTAL {sum(range(MAX_HITS - 1, MAX_HITS + 3))}"
    assert result["unapplied"] == [
        {
            "option": "--set-at",
            "at": f"{each}@all",
            "name": "total",
            "value": "0",
            "stops": MAX_HITS + 1,
            "made": MAX_HITS,
        }
    ]
    assert any(
        f"was made only at the first {MAX_HITS} stops of line {each}, the cap" in warning
        for warning in result["warnings"]
    ), result["warnings"]
    assert "(1 --set-at @all cut short at the cap)" in _table(result)
