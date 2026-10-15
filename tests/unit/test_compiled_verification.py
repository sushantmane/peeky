"""Cross-language acceptance gates independent of a real compiler or debugger."""

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from peeky.build import BuildCommandError
from peeky.compiled_verification import CompiledVerification


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Manifest:
    identity: str = "fixture-build"


class Runtime:
    def __init__(self, target, root, edit):
        self.owner = tempfile.TemporaryDirectory(dir=root.parent)
        self.prepared = SimpleNamespace(workspace=Path(self.owner.name), manifest=Manifest())
        for name in target.verification_inputs:
            path = self.prepared.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((root / name).read_bytes())
        if edit:
            path = self.prepared.workspace / edit.path
            path.write_text(path.read_text().replace(edit.old, edit.new, 1))
        self.binary = self.prepared.workspace / "program"
        self.binary.write_text("fresh compiled output")
        self.identities = {p: digest(p) for p in self.prepared.workspace.rglob("*") if p.is_file()}
        self.root = root
        self.environment = {}
        self.manifest_id = "fixture-native-manifest"
        self.verification_command = (str(self.binary),)
        self.closed = False

    def source_path(self, path):
        return self.prepared.workspace / path.relative_to(self.root)

    def assert_current(self):
        if any(not p.exists() or digest(p) != value for p, value in self.identities.items()):
            raise RuntimeError("prepared artifact provenance mismatch")

    def close(self):
        self.closed = True
        self.owner.cleanup()


class Target:
    verification_inputs = ("nested/main.c", "config.h")
    verification_sources = ("nested/main.c",)
    arguments = ()
    target_identity = {"language": "c", "sources": list(verification_inputs)}

    def __init__(self):
        self.runs = []
        self.failure = None
        self.before_prepare = None

    def prepare(self, source, root, *, edit=None):
        if self.before_prepare:
            self.before_prepare()
        if self.failure:
            raise self.failure
        runtime = Runtime(self, root, edit)
        self.runs.append(runtime)
        return runtime


class Verification(CompiledVerification):
    language = "c"


@pytest.fixture
def investigation(tmp_path, monkeypatch):
    root = tmp_path / "sources"
    (root / "nested").mkdir(parents=True)
    source = root / "nested/main.c"
    source.write_text("int main(void) { return 1; }\n")
    (root / "config.h").write_text("#define LIMIT 10\n")
    check = root / "check.py"
    check.write_text("# trusted fixture checker\n")
    target = Target()
    verifier = Verification(target, root)
    monkeypatch.setattr("peeky.agent.verify", lambda *args, **kwargs: {"passed": True})
    return verifier, target, source, check, tmp_path / "results"


def candidate(parts):
    verifier, _, source, check, destination = parts
    return verifier.candidate(source, check, "return 1", "return 0", destination)


def test_companion_header_is_frozen_at_investigation_creation(investigation):
    verifier, target, source, _, _ = investigation
    source.parent.parent.joinpath("config.h").write_text("#define LIMIT 99\n")
    with pytest.raises(RuntimeError, match="changed"):
        candidate(investigation)
    assert not target.runs
    assert "config.h" in verifier.source_manifest


def test_header_is_not_an_editable_candidate(investigation):
    verifier, target, source, check, destination = investigation
    with pytest.raises(ValueError, match="source|input"):
        verifier.candidate(source.parent.parent / "config.h", check, "10", "99", destination)
    assert not target.runs


def test_compile_failure_feedback_is_distinct_from_provenance_or_symbol_failure(investigation):
    _, target, _, _, _ = investigation
    target.failure = BuildCommandError(
        ("compiler",), {"exit_code": 7, "stdout": "", "stderr": "syntax error"}
    )
    result = candidate(investigation)
    assert result["passed"] is False and result["stage"] == "build"
    for message in (
        "symbol metadata missing",
        "Toolchain probe failed",
        "Toolchain executable changed",
    ):
        target.failure = RuntimeError(message)
        with pytest.raises(RuntimeError, match=message):
            candidate(investigation)


def test_original_header_change_during_failed_compile_remains_fatal(investigation):
    _, target, source, _, _ = investigation
    target.before_prepare = lambda: (source.parent.parent / "config.h").write_text("changed")
    target.failure = BuildCommandError(("compiler",), {"exit_code": 1, "stderr": "bad"})
    with pytest.raises(RuntimeError, match="changed"):
        candidate(investigation)


def test_fresh_builds_preserve_candidate_layout_and_cleanup(investigation):
    _, target, source, _, _ = investigation
    first, second = candidate(investigation), candidate(investigation)
    assert first["passed"] and second["passed"]
    assert Path(first["candidate"]).as_posix().endswith("candidate/nested/main.c")
    assert source.read_text() == "int main(void) { return 1; }\n"
    assert len({run.prepared.workspace for run in target.runs}) == 2
    assert all(run.closed and not run.prepared.workspace.exists() for run in target.runs)
    assert first["evidence_file"] != second["evidence_file"]


@pytest.mark.parametrize("tamper", ["descriptor", "binary", "staged-header", "checker"])
def test_passing_checker_cannot_accept_drift(investigation, monkeypatch, tamper):
    _, target, _, check, _ = investigation

    def verify(_check, descriptor, _cwd, **_kwargs):
        data = json.loads(descriptor.read_text())
        victim = {
            "descriptor": descriptor,
            "binary": Path(data["command"][0]),
            "staged-header": descriptor.parent / "config.h",
            "checker": check,
        }[tamper]
        victim.write_text("changed")
        return {"passed": True}

    monkeypatch.setattr("peeky.agent.verify", verify)
    with pytest.raises(RuntimeError, match="changed|provenance"):
        candidate(investigation)
    assert target.runs[-1].closed


def test_launch_requires_full_companion_manifest(investigation):
    verifier, target, _, _, _ = investigation
    launch = {
        "source_manifest": dict(verifier.source_manifest),
        "target_identity": target.target_identity,
    }
    verifier.validate_launch(launch)
    del launch["source_manifest"]["config.h"]
    with pytest.raises(ValueError, match="manifest"):
        verifier.validate_launch(launch)


@pytest.mark.parametrize(
    "checker, tests, passed", [(True, True, True), (True, False, False), (False, True, False)]
)
def test_session_test_hook_cannot_replace_or_bypass_the_checker(
    investigation, monkeypatch, checker, tests, passed
):
    _, target, _, _, _ = investigation
    calls = []
    original = target.prepare

    def prepare(*args, **kwargs):
        runtime = original(*args, **kwargs)
        runtime.run_tests = lambda: calls.append("tests") or {
            "passed": tests,
            "stage": "cargo-test",
        }
        return runtime

    target.prepare = prepare
    monkeypatch.setattr(
        "peeky.agent.verify",
        lambda *args, **kwargs: calls.append("checker") or {"passed": checker, "exit_code": 0},
    )
    result = candidate(investigation)
    assert calls == ["tests", "checker"]
    assert result["passed"] is passed and result["tests"]["passed"] is tests
