import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from peeky.build import ArtifactSpec, BuildCommandError, BuildRecipe, Command, prepare
from peeky.rust import RustTarget, _candidate_diagnostic, libtest_summary


@pytest.fixture
def configured(tmp_path):
    root = tmp_path / "inputs"
    shutil.copytree(Path(__file__).resolve().parents[2] / "examples/rust", root)
    bin_dir = tmp_path / "toolchain/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir.parent / "lib/rustlib").mkdir(parents=True)
    marker = tmp_path / "compiled"
    for name in ("cargo", "rustc", "adapter", "dwarfdump", "linker", "dsymutil"):
        text = f'#!/bin/sh\necho "{name} 1.96.0 fixture"\n'
        if name == "cargo":
            text = f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "cargo 1.96.0 fixture"; else touch "{marker}"; exit 7; fi\n'
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    target = RustTarget(
        sources=("src/main.rs",),
        tests=("tests/pagination.rs",),
        **{
            name: str(bin_dir / name)
            for name in ("cargo", "rustc", "adapter", "dwarfdump", "linker", "dsymutil")
        },
    )
    return root, target, marker


def test_staged_manifest_is_validated_before_any_tool(configured, monkeypatch):
    root, target, marker = configured
    real_prepare = prepare

    def changed(recipe, source_root, **kwargs):
        manifest = source_root / "Cargo.toml"
        manifest.write_text(manifest.read_text().replace("build = false", "build = true"))
        return real_prepare(recipe, source_root, **kwargs)

    monkeypatch.setattr("peeky.rust.prepare", changed)
    with pytest.raises(ValueError, match="build scripts"):
        target.prepare(root / "src/main.rs", root)
    assert not marker.exists()


def test_pinned_version_is_checked_before_compile(configured):
    root, target, marker = configured
    Path(target.rustc).write_text('#!/bin/sh\necho "rustc 1.95.0 fixture"\n')
    with pytest.raises(ValueError, match="pinned version"):
        target.prepare(root / "src/main.rs", root)
    assert not marker.exists()


def test_dsymutil_path_must_resolve_to_declared_executable(configured, monkeypatch):
    from dataclasses import replace

    root, target, marker = configured
    monkeypatch.setattr("peeky.rust.sys.platform", "darwin")
    named = Path(target.dsymutil).with_name("other-dsymutil")
    named.write_text("#!/bin/sh\nexit 0\n")
    named.chmod(0o755)
    target = replace(target, dsymutil=str(named))
    with pytest.raises(RuntimeError, match="dsymutil PATH"):
        target.prepare(root / "src/main.rs", root)
    assert not marker.exists()


def test_probed_tool_drift_during_failed_command_is_fatal(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.rs").write_text("fn main() {}")
    probe = tmp_path / "probe"
    probe.write_text("#!/bin/sh\necho version\n")
    probe.chmod(0o755)
    command = Command(
        (
            sys.executable,
            "-c",
            f'from pathlib import Path; Path({str(probe)!r}).write_text("changed"); raise SystemExit(7)',
        )
    )
    recipe = BuildRecipe(
        "rust", ("main.rs",), (command,), (ArtifactSpec("binary"),), (Command((str(probe),)),)
    )
    with pytest.raises(RuntimeError, match="probed toolchain") as caught:
        prepare(recipe, source)
    assert not isinstance(caught.value, BuildCommandError)


def test_expired_prepare_budget_prevents_command(configured):
    root, target, marker = configured
    with pytest.raises(TimeoutError, match="deadline"):
        target.prepare(root / "src/main.rs", root, deadline=time.monotonic() - 1)
    assert not marker.exists()


def diagnostic(**changes):
    messages = [
        {
            "reason": "compiler-message",
            "message": {
                "level": "error",
                "message": "expected expression",
                "spans": [{"file_name": "src/main.rs", "is_primary": True}],
            },
        },
        {"reason": "build-finished", "success": False},
    ]
    return {"stdout": "\n".join(json.dumps(x) for x in messages), "exit_code": 101, **changes}


def test_retry_requires_complete_source_diagnostic():
    assert _candidate_diagnostic(diagnostic(), ("src/main.rs",))
    for result in [
        diagnostic(timed_out=True),
        diagnostic(output_limit_exceeded=True),
        diagnostic(stdout="not JSON"),
        diagnostic(stdout=json.dumps({"reason": "build-finished", "success": False})),
    ]:
        assert not _candidate_diagnostic(result, ("src/main.rs",))
    assert not _candidate_diagnostic(diagnostic(), ("src/other.rs",))


@pytest.mark.parametrize("ignored,measured", [(1, 0), (0, 1)])
def test_libtest_ignored_or_measured_tests_are_not_clean_pass(ignored, measured):
    result = {
        "exit_code": 0,
        "stdout": f"test result: ok. 1 passed; 0 failed; {ignored} ignored; {measured} measured; 0 filtered out; finished in 0.01s",
    }
    assert libtest_summary(result)["passed"] is False


@pytest.mark.parametrize(
    "extra",
    [
        {"reason": "compiler-message", "message": None},
        {
            "reason": "compiler-message",
            "message": {"level": "error", "message": "internal compiler error", "spans": []},
        },
        {
            "reason": "compiler-message",
            "message": {"level": "error", "message": "linker failed", "spans": []},
        },
        {
            "reason": "compiler-message",
            "message": {"level": "error", "message": "malformed", "spans": None},
        },
        {"reason": "build-finished", "success": True},
    ],
)
def test_mixed_source_error_and_other_failure_is_not_retryable(extra):
    messages = [json.loads(line) for line in diagnostic()["stdout"].splitlines()]
    messages.insert(1, extra)
    result = diagnostic(stdout="\n".join(json.dumps(item) for item in messages))
    assert not _candidate_diagnostic(result, ("src/main.rs",))
