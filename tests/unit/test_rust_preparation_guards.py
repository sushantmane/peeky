"""Rust preparation provenance guards (ported from the isolated 339def3 review fixes).

Adapted to main: retry classification is ``compiler_diagnostic`` (main's stricter
classifier, which also requires Cargo's exact stderr footer and a newline-terminated
JSON stream), so fixtures model a complete Cargo failure. Main rejects a declared
helper that PATH lookup cannot select by name when the target is configured, and
rechecks PATH resolution before every command during preparation.
"""

import json
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from peeky.build import ArtifactSpec, BuildCommandError, BuildRecipe, Command, prepare
from peeky.rust import RustTarget, compiler_diagnostic, libtest_summary


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
            text = (
                '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "cargo 1.96.0 fixture"; '
                f'else touch "{marker}"; exit 7; fi\n'
            )
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
    probed = marker.with_name("probed")
    Path(target.rustc).write_text(f'#!/bin/sh\ntouch "{probed}"\necho "rustc 1.96.0 fixture"\n')
    real_prepare = prepare

    def changed(recipe, source_root, **kwargs):
        manifest = source_root / "Cargo.toml"
        manifest.write_text(manifest.read_text().replace("build = false", "build = true"))
        return real_prepare(recipe, source_root, **kwargs)

    monkeypatch.setattr("peeky.rust.prepare", changed)
    with pytest.raises(ValueError, match="build scripts"):
        target.prepare(root / "src/main.rs", root)
    assert not marker.exists()
    assert not probed.exists(), "the staged Cargo policy must be checked before the first probe"


def test_cargo_ancestry_is_rechecked_before_each_probe(configured):
    root, target, marker = configured
    probed = marker.with_name("probed")
    # The adapter probe plants ambient Cargo configuration beside the fresh workspace.
    Path(target.adapter).write_text(
        '#!/bin/sh\nmkdir -p "$PWD/../.cargo"\n'
        'printf \'[build]\\nrustc-wrapper = "/x"\\n\' > "$PWD/../.cargo/config.toml"\n'
        'echo "adapter fixture"\n'
    )
    Path(target.dwarfdump).write_text(f'#!/bin/sh\ntouch "{probed}"\necho "dwarfdump"\n')
    with pytest.raises(ValueError, match="Ambient Cargo configuration"):
        target.prepare(root / "src/main.rs", root)
    assert not probed.exists() and not marker.exists()


def test_pinned_version_is_checked_before_compile(configured):
    root, target, marker = configured
    Path(target.rustc).write_text('#!/bin/sh\necho "rustc 1.95.0 fixture"\n')
    with pytest.raises(ValueError, match="pinned version"):
        target.prepare(root / "src/main.rs", root)
    assert not marker.exists()


def test_dsymutil_path_must_resolve_to_declared_executable(configured, monkeypatch):
    root, target, marker = configured
    monkeypatch.setattr("peeky.rust.sys.platform", "darwin")
    named = Path(target.dsymutil).with_name("other-dsymutil")
    named.write_text("#!/bin/sh\nexit 0\n")
    named.chmod(0o755)
    # Main rejects a helper rustc's PATH lookup could never select when configured.
    with pytest.raises(ValueError, match="named dsymutil"):
        replace(target, dsymutil=str(named))
    assert not marker.exists()


def test_dsymutil_path_drift_during_preparation_blocks_compile(configured, monkeypatch):
    root, target, marker = configured
    monkeypatch.setattr("peeky.rust.sys.platform", "darwin")
    link = Path(target.dsymutil)
    declared, substitute = (link.parents[2] / name / "dsymutil" for name in ("real", "other"))
    for tool in (declared, substitute):
        tool.parent.mkdir()
        tool.write_text(f'#!/bin/sh\necho "{tool.parent.name} dsymutil"\n')
        tool.chmod(0o755)
    link.unlink()
    link.symlink_to(declared)
    target = replace(target, dsymutil=str(link))
    # Retargeting the PATH entry changes no probed tool's bytes; only resolution reveals it.
    Path(target.linker).write_text(
        f'#!/bin/sh\nln -sfn "{substitute}" "{link}"\necho "linker fixture"\n'
    )
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
            f"from pathlib import Path; Path({str(probe)!r}).write_text('changed'); "
            "raise SystemExit(7)",
        )
    )
    recipe = BuildRecipe(
        "rust", ("main.rs",), (command,), (ArtifactSpec("binary"),), (Command((str(probe),)),)
    )
    with pytest.raises(RuntimeError, match="Toolchain changed during failed build") as caught:
        prepare(recipe, source)
    assert not isinstance(caught.value, BuildCommandError)


def test_probed_tool_drift_before_next_command_is_fatal(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.rs").write_text("fn main() {}")
    probe = tmp_path / "probe"
    probe.write_text("#!/bin/sh\necho version\n")
    probe.chmod(0o755)
    ran = tmp_path / "ran"
    drift = Command(
        (sys.executable, "-c", f"from pathlib import Path; Path({str(probe)!r}).write_text('x')")
    )
    after = Command(
        (sys.executable, "-c", f"from pathlib import Path; Path({str(ran)!r}).write_text('ran')")
    )
    recipe = BuildRecipe(
        "rust",
        ("main.rs",),
        (drift, after),
        (ArtifactSpec("binary"),),
        (Command((str(probe),)),),
    )
    with pytest.raises(RuntimeError, match="Toolchain changed during preparation"):
        prepare(recipe, source)
    assert not ran.exists()


def test_original_source_drift_before_next_command_is_fatal(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.rs").write_text("fn main() {}")
    ran = tmp_path / "ran"
    drift = Command(
        (
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(source / 'main.rs')!r}).write_text('changed')",
        )
    )
    after = Command(
        (sys.executable, "-c", f"from pathlib import Path; Path({str(ran)!r}).write_text('ran')")
    )
    recipe = BuildRecipe("rust", ("main.rs",), (drift, after), (ArtifactSpec("binary"),))
    with pytest.raises(RuntimeError, match="Source provenance changed"):
        prepare(recipe, source)
    assert not ran.exists()


def test_expired_prepare_budget_prevents_command(configured):
    root, target, marker = configured
    with pytest.raises(TimeoutError, match="deadline"):
        target.prepare(root / "src/main.rs", root, deadline=time.monotonic() - 1)
    assert not marker.exists()


STDERR = (
    "   Compiling peeky-pagination v0.1.0 (/tmp/w)\n"
    'error: could not compile `peeky-pagination` (bin "peeky-pagination") due to 1 previous error\n'
)


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
    return {
        "stdout": "".join(json.dumps(x) + "\n" for x in messages),
        "stderr": STDERR,
        "exit_code": 101,
        **changes,
    }


def test_retry_requires_complete_source_diagnostic():
    assert compiler_diagnostic(diagnostic(), ("src/main.rs",))
    for result in [
        diagnostic(timed_out=True),
        diagnostic(output_limit_exceeded=True),
        diagnostic(stdout="not JSON\n"),
        diagnostic(stdout=json.dumps({"reason": "build-finished", "success": False}) + "\n"),
    ]:
        assert not compiler_diagnostic(result, ("src/main.rs",))
    assert not compiler_diagnostic(diagnostic(), ("src/other.rs",))


def test_many_diagnostics_are_still_a_compile_failure():
    # Eval 3 follow-up: three errors and some warnings passed 16 KB of JSON, which used
    # to read as possibly truncated, so a plain type error became an infrastructure
    # failure.
    error = {
        "reason": "compiler-message",
        "message": {
            "level": "error",
            "message": "mismatched types",
            "rendered": "src/main.rs:3:9: error[E0308]: mismatched types\n",
            "spans": [{"file_name": "src/main.rs", "is_primary": True, "text": ["x" * 400]}],
        },
    }
    lines = [json.dumps(error)] * 60 + [json.dumps({"reason": "build-finished", "success": False})]
    result = diagnostic(stdout="\n".join(lines) + "\n")
    assert len(result["stdout"]) > 16000
    assert compiler_diagnostic(result, ("src/main.rs",))


@pytest.mark.parametrize("ignored,measured", [(1, 0), (0, 1)])
def test_libtest_ignored_or_measured_tests_are_not_clean_pass(ignored, measured):
    result = {
        "exit_code": 0,
        "stdout": f"test result: ok. 1 passed; 0 failed; {ignored} ignored; {measured} measured; "
        "0 filtered out; finished in 0.01s",
    }
    assert libtest_summary(result)["passed"] is False


SOURCE_SPAN = {"file_name": "src/main.rs", "is_primary": True}


@pytest.mark.parametrize(
    "extra",
    [
        {"reason": "compiler-message", "message": None},
        {"reason": "compiler-message", "message": "error: not an object"},
        {
            "reason": "compiler-message",
            "message": {"level": "error", "message": "internal compiler error", "spans": []},
        },
        {
            "reason": "compiler-message",
            "message": {
                "level": "error",
                "message": "internal compiler error: unexpected panic",
                "spans": [SOURCE_SPAN],
            },
        },
        {
            "reason": "compiler-message",
            "message": {"level": "error", "message": "linker failed", "spans": []},
        },
        {
            "reason": "compiler-message",
            "message": {"level": "error", "message": "malformed", "spans": None},
        },
        {
            "reason": "compiler-message",
            "message": {"level": "error", "message": "malformed", "spans": 5},
        },
        {
            "reason": "compiler-message",
            "message": {"level": "error", "message": "malformed", "spans": SOURCE_SPAN},
        },
        {
            "reason": "compiler-message",
            "message": {"level": "error", "message": "malformed", "spans": [SOURCE_SPAN, "x"]},
        },
        {"reason": "build-finished", "success": True},
    ],
)
def test_mixed_source_error_and_other_failure_is_not_retryable(extra):
    messages = [json.loads(line) for line in diagnostic()["stdout"].splitlines()]
    messages.insert(1, extra)  # after a qualified source error: later failures still count
    result = diagnostic(stdout="".join(json.dumps(item) + "\n" for item in messages))
    assert not compiler_diagnostic(result, ("src/main.rs",))
