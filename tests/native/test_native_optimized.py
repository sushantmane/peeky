"""#12/#13: optimized-out locals are reported as unavailable, never as values."""

import os

import pytest

from peeky.native import NativeTarget
from peeky.probe import Probe, run_probe

SOURCE = """#include <stdio.h>

__attribute__((noinline)) static int mix(int a, int b) {
    int t = a * 3 + b; /* BEFORE_T: at -O2, t and r have no location yet */
    printf("t %d\\n", t);
    int r = t ^ (b << 2); /* AFTER_LAST_USE_OF_A: a is dead and its register clobbered */
    printf("mix %d\\n", r);
    return r;
}

int main(int argc, char **argv) {
    int total = 0;
    for (int i = 0; i < 3; i++) {
        total += mix(i + argc, i * 2);
    }
    printf("TOTAL %d\\n", total);
    return 0;
}
"""


def target(language, optimization):
    dap = os.environ.get("PEEKY_NATIVE_DAP")
    if not dap:
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    variable = "PEEKY_NATIVE_CXX" if language == "cpp" else "PEEKY_NATIVE_CC"
    compiler = os.environ.get(variable)
    if language == "cpp" and not compiler:
        pytest.skip("set PEEKY_NATIVE_CXX to enable C++ conformance")
    return NativeTarget(
        sources=(f"opt.{'cpp' if language == 'cpp' else 'c'}",),
        language=language,
        compiler=compiler or ("clang++" if language == "cpp" else "clang"),
        adapter=dap,
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
        optimization=optimization,
    )


@pytest.mark.parametrize("language", ["c", "cpp"])
def test_optimized_out_locals_are_unavailable_and_cannot_be_changed(tmp_path, language):
    adapter = target(language, "O2")
    name = adapter.sources[0]
    (tmp_path / name).write_text(SOURCE)
    line = next(i for i, text in enumerate(SOURCE.splitlines(), 1) if "BEFORE_T" in text)
    result = run_probe(tmp_path, Probe(name, line, ("a", "b", "t", "r")), adapter=adapter)
    assert result["status"] == "completed", result.get("error")
    assert "-O2" in result["target_identity"]["compiler_options"]
    rows = {row["selector"]: row for row in result["rows"]}
    for row in rows.values():
        assert ("value" in row) != bool(row.get("unavailable")), row
        if row.get("unavailable"):
            assert "optimized out" in row["error"] and "value" not in row
    assert rows["t"].get("unavailable") and rows["r"].get("unavailable"), rows
    assert any("no value at this line" in warning for warning in result["warnings"])
    assert result["optimized"] == "-O2"
    assert any("can still be stale or wrong" in warning for warning in result["warnings"])
    for local in ("t", "b"):  # unavailable, and available-but-optimized: both refused
        refused = run_probe(tmp_path, Probe(name, line, (local,), ((local, "5"),)), adapter=adapter)
        assert refused["status"] == "failed" and refused["error"]["code"] == "MUTATION_REJECTED"


@pytest.mark.parametrize("language", ["c", "cpp"])
def test_unoptimized_builds_remain_the_default_and_show_values(tmp_path, language):
    adapter = target(language, "O0")
    name = adapter.sources[0]
    (tmp_path / name).write_text(SOURCE)
    line = next(i for i, text in enumerate(SOURCE.splitlines(), 1) if "AFTER_LAST" in text)
    result = run_probe(tmp_path, Probe(name, line, ("a", "t"), (("a", "5"),)), adapter=adapter)
    assert result["status"] == "completed", result.get("error")
    assert all("value" in row for row in result["rows"]) and "optimized" not in result
    assert result["interventions"][0]["after"] == "5"
    with pytest.raises(ValueError, match="optimization"):
        target(language, "O3")


def test_optimized_sessions_say_so_and_never_show_placeholders_as_values(
    tmp_path, capsys, monkeypatch
):
    import json

    from peeky.cli import main

    adapter = target("c", "O2")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "opt.c").write_text(SOURCE)
    config = tmp_path / "native.json"
    config.write_text(
        json.dumps(
            {
                "language": "c",
                "sources": ["opt.c"],
                "compiler": adapter.compiler,
                "adapter": adapter.adapter,
                "dwarfdump": adapter.dwarfdump,
                "dsymutil": adapter.dsymutil,
                "sysroot": adapter.sysroot,
                "optimization": "O2",
            }
        )
    )
    line = next(i for i, text in enumerate(SOURCE.splitlines(), 1) if "BEFORE_T" in text)
    args = ["session", "start", str(tmp_path), "--config", str(config), "--at", f"opt.c:{line}"]
    assert main(args) == 0
    assert "optimized build (-O2)" in capsys.readouterr().out
    assert main(["session", "inspect", "--json"]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["optimized"] == "-O2"
    # A run-level warning in JSON too, as the text gives it.
    assert snapshot["warning"].startswith("optimized build (-O2): shown values can be stale")
    assert main(["session", "inspect"]) == 0
    assert "warning: optimized build (-O2)" in capsys.readouterr().out
    assert main(["session", "get", "a"]) in (0, 1)
    assert "warning: optimized build (-O2)" in capsys.readouterr().out
    unavailable = [v for s in snapshot["scopes"] for v in s["variables"] if v.get("unavailable")]
    assert unavailable and all(v["value"] is None for v in unavailable)
    # An optimized-out value is a row without a value, so `get` exits 1.
    assert main(["session", "get", "t", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)[0]["optimized"] == "-O2"
    assert main(["session", "set", "b=5", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "MUTATION_REJECTED"
    assert main(["session", "close"]) == 0


def test_describe_and_agent_reflect_that_optimized_builds_refuse_changes(
    tmp_path, capsys, monkeypatch
):
    import json
    import sys

    from peeky import agent
    from peeky.cli import main

    adapter = target("c", "O2")
    (tmp_path / "opt.c").write_text(SOURCE)
    (tmp_path / "check.py").write_text("print('PASS')\\n")
    config = tmp_path / "native.json"
    config.write_text(
        json.dumps(
            {
                "language": "c",
                "sources": ["opt.c"],
                "compiler": adapter.compiler,
                "adapter": adapter.adapter,
                "dwarfdump": adapter.dwarfdump,
                "dsymutil": adapter.dsymutil,
                "sysroot": adapter.sysroot,
                "optimization": "O2",
            }
        )
    )
    assert (
        main(["describe", str(tmp_path), "--config", str(config), "--at", "opt.c", "--json"]) == 0
    )
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["optimized"] == "-O2" and manifest["mutable_types"].startswith("none")
    argv = ["peeky-agent", "--root", str(tmp_path), "--program", "opt.c", "--check", "check.py"]
    argv += ["--native-config", str(config), "--output", str(tmp_path / "out")]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/true")
    with pytest.raises(SystemExit) as stopped:
        agent.main()
    assert stopped.value.code == 2 and "needs an unoptimized build" in capsys.readouterr().err
