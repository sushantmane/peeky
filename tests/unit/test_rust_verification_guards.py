"""Independent pre/post execution integrity checks for compiled Rust test artifacts.

The executor gets a bounded ``output_limit``, so overflow is an explicit failure.
"""

import sys

import pytest

from peeky.build import ArtifactSpec, BuildRecipe, Command, prepare
from peeky.rust import RustSession


@pytest.fixture
def session(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.rs").write_text("fn main() {}")
    script = (
        "from pathlib import Path; "
        "Path('binary').write_text('program'); "
        "Path('uplift').write_text('program'); "
        "Path('test-binary').write_text('compiled test')"
    )
    recipe = BuildRecipe(
        "rust",
        ("main.rs",),
        (Command((sys.executable, "-c", script)),),
        tuple(ArtifactSpec(name) for name in ("binary", "uplift", "test-binary")),
    )
    prepared = prepare(recipe, source)
    runtime = object.__new__(RustSession)
    runtime.prepared = prepared
    runtime.binary = prepared.workspace / "binary"
    runtime.test_target = prepared.workspace / "uplift"
    runtime.test_binary = prepared.workspace / "test-binary"
    runtime.home = prepared.workspace / "home"
    runtime.home.mkdir()
    calls = []

    def execute(command, cwd, timeout, *, env, output_limit):
        calls.append((command, cwd, timeout, env, output_limit))
        return {
            "exit_code": 0,
            "stdout": "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n",
            "stderr": "",
            "timed_out": False,
            "output_limit_exceeded": False,
        }

    monkeypatch.setattr("peeky.agent.execute", execute)
    try:
        yield runtime, calls, execute
    finally:
        prepared.cleanup()


@pytest.mark.parametrize("attribute", ["test_binary", "test_target", "binary"])
@pytest.mark.parametrize("action", ["change", "delete"])
def test_preexisting_artifact_drift_blocks_test_execution(session, attribute, action):
    runtime, calls, _ = session
    path = getattr(runtime, attribute)
    path.write_text("changed") if action == "change" else path.unlink()
    with pytest.raises(RuntimeError, match="provenance"):
        runtime.run_tests()
    assert calls == []


@pytest.mark.parametrize("attribute", ["test_binary", "test_target", "binary"])
def test_passing_test_cannot_hide_artifact_mutation(session, monkeypatch, attribute):
    runtime, calls, execute = session

    def changing(*args, **kwargs):
        result = execute(*args, **kwargs)
        getattr(runtime, attribute).write_text("changed during test")
        return result

    monkeypatch.setattr("peeky.agent.execute", changing)
    with pytest.raises(RuntimeError, match="provenance"):
        runtime.run_tests()
    assert len(calls) == 1


def test_test_execution_uses_fixed_prepared_executable_without_cargo(session):
    runtime, calls, _ = session
    result = runtime.run_tests()
    assert result["passed"] is True
    assert calls[0][0] == [str(runtime.test_binary), "--test-threads=1", "--color", "never"]
    assert calls[0][1] == runtime.prepared.workspace
    assert 0 < calls[0][4] <= 16000
    assert result["executable_sha256"]
