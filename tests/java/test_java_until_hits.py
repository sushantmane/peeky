"""#203 on a launched JVM: resumed after its last hit, a `--until hits` probe's program
runs on until the probe ends it. Its output read meanwhile was "so far (the program was
still running)", even when it had exited."""

import json
import os
from pathlib import Path

import pytest

from peeky.cli import main
from peeky.probe import _table

RAN_ON = """package demo;

public class RanOn {
    public static void main(String[] args) throws Exception {
        System.out.println("BEFORE");
        int total = 0;
        for (int i = 0; i < 3; i++) {
            total += i; // HIT
        }
        System.out.println("AFTER " + total);
        Thread.sleep(Long.parseLong(args[0]) * 1000);
    }
}
"""


@pytest.fixture
def ran_on(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "ranon"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "RanOn.java").write_text(RAN_ON)
    config = root / "ranon.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["demo/RanOn.java"],
                "main_class": "demo.RanOn",
                "arguments": [],
            }
        )
    )
    return root, config


@pytest.mark.timeout(240)
@pytest.mark.parametrize("sleep", ["30", "0"])
def test_until_hits_says_the_output_can_follow_the_last_hit(ran_on, capsys, sleep):
    root, config = ran_on
    line = next(i for i, text in enumerate(RAN_ON.splitlines(), 1) if "HIT" in text)
    argv = ["probe", root, "--config", config, "--at", f"demo/RanOn.java:{line}"]
    argv += ["--capture", "i", "--hits", 2, "--until", "hits", "--arg", sleep, "--json"]
    assert main([str(arg) for arg in [*argv, "--no-save"]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed" and result["ended"] == "hits"
    output = result["output"].replace("\r\n", "\n")
    text = _table(result)
    assert "still running" not in text and output.startswith("BEFORE\n")
    assert result.get("output_after_last_hit") is True
    if result["output_complete"]:
        # It exited before Peeky stopped reading: all of its output, and its exit.
        assert sleep == "0" and result["exit_code"] == 0 and output == "BEFORE\nAFTER 3\n"
        assert "output (all of it: the program ran on after the last hit, to its exit):" in text
    else:
        assert "exit_code" not in result or sleep == "0"
        assert (
            "output so far (the program ran on after the last hit, so this can include text "
            "written after it):\n  BEFORE"
        ) in text
