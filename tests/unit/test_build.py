import sys
from dataclasses import replace

import pytest

from peeky.build import ArtifactSpec, BuildRecipe, Command, SourceEdit, assert_current, prepare


def command(script, **kwargs):
    return Command((sys.executable, "-c", script), **kwargs)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "Main.java").write_text("class Main { int value = 1; }\n")
    return root


def recipe(**changes):
    base = BuildRecipe(
        language="fixture",
        inputs=("Main.java",),
        build_commands=(
            command(
                "from pathlib import Path; Path('out.bin').write_bytes(Path('Main.java').read_bytes())"
            ),
        ),
        artifacts=(ArtifactSpec("out.bin"),),
        toolchain_probes=(Command((sys.executable, "--version")),),
        environment=(("BUILD_MODE", "debug"),),
    )
    return replace(base, **changes)


def test_prepare_records_sources_tools_flags_and_clean_artifact(source):
    with prepare(recipe(), source, edit=SourceEdit("Main.java", "value = 1", "value = 2")) as run:
        assert run.workspace != source
        assert run.manifest.schema_version == 1
        assert len(run.manifest.manifest_id) == 64
        assert run.manifest.inputs != run.manifest.original_inputs
        assert run.manifest.artifacts[0].sha256 == run.manifest.inputs[0].sha256
        assert run.manifest.probes[0].stdout.startswith("Python ")
        assert run.manifest.tools
        assert "value = 1" in (source / "Main.java").read_text()
        assert "value = 2" in (run.workspace / "Main.java").read_text()
        assert_current(run)
        workspace = run.workspace
    assert not workspace.exists()
    assert (source / "Main.java").exists()


@pytest.mark.parametrize("which", ["source", "staged", "artifact", "recipe"])
def test_assert_rejects_changed_identity(source, which):
    with prepare(recipe(), source) as run:
        if which == "recipe":
            run = replace(run, recipe=replace(run.recipe, environment=(("BUILD_MODE", "release"),)))
        else:
            path = {
                "source": source / "Main.java",
                "staged": run.workspace / "Main.java",
                "artifact": run.workspace / "out.bin",
            }[which]
            path.write_text("different")
        with pytest.raises(RuntimeError, match="provenance mismatch"):
            assert_current(run)


def test_build_failure_cleans_workspace(source, tmp_path):
    parent = tmp_path / "builds"
    parent.mkdir()
    for failure in [
        command("raise SystemExit(3)"),
        command("import time; time.sleep(20)", timeout_s=0.05),
    ]:
        with pytest.raises(RuntimeError, match="Build command failed"):
            prepare(recipe(build_commands=(failure,)), source, parent=parent)
        assert list(parent.iterdir()) == []


def test_zero_exit_without_new_artifact_rejects_stale_source_output(source):
    (source / "out.bin").write_text("stale executable")
    with pytest.raises(ValueError, match="Missing required artifact"):
        prepare(recipe(build_commands=(command("pass"),)), source)
    with pytest.raises(ValueError, match="copied inputs"):
        prepare(recipe(inputs=("Main.java", "out.bin")), source)


def test_build_overwriting_input_rejected(source):
    overwrite = command(
        "from pathlib import Path; Path('Main.java').write_text('changed'); Path('out.bin').write_text('artifact')"
    )
    with pytest.raises(RuntimeError, match="provenance mismatch"):
        prepare(recipe(build_commands=(overwrite,)), source)
    assert "value = 1" in (source / "Main.java").read_text()


@pytest.mark.parametrize("path", ["../outside", "/outside", "nested/../outside", "a\\b", "a//b"])
def test_declared_input_cwd_artifact_traversal_rejected(source, path):
    for changed in [
        recipe(inputs=(path,)),
        recipe(artifacts=(ArtifactSpec(path),)),
        recipe(build_commands=(command("pass", cwd=path),)),
    ]:
        with pytest.raises(ValueError):
            prepare(changed, source)


def test_symlink_input_and_artifact_rejected(source, tmp_path):
    external = tmp_path / "external"
    external.write_text("secret")
    (source / "link").symlink_to(external)
    with pytest.raises(ValueError, match="Symlinks"):
        prepare(recipe(inputs=("link",)), source)
    link = command(f"from pathlib import Path; Path('out.bin').symlink_to({str(external)!r})")
    with pytest.raises(ValueError, match="Symlinks"):
        prepare(recipe(build_commands=(link,)), source)


def test_directory_membership_and_symbols_are_identified(source):
    build = command(
        "from pathlib import Path; Path('classes').mkdir(); Path('classes/Main.class').write_text('bytes'); Path('symbols').write_text('debug-info')"
    )
    configured = recipe(
        build_commands=(build,),
        artifacts=(
            ArtifactSpec("classes", "directory", "classes"),
            ArtifactSpec("symbols", role="symbols"),
        ),
    )
    with prepare(configured, source) as run:
        assert {f.path for f in run.manifest.artifacts} == {"classes/Main.class", "symbols"}
        (run.workspace / "classes/Extra.class").write_text("extra")
        with pytest.raises(RuntimeError, match="provenance mismatch"):
            assert_current(run)
        (run.workspace / "classes/Extra.class").unlink()
        (run.workspace / "symbols").unlink()
        with pytest.raises(RuntimeError, match="provenance mismatch"):
            assert_current(run)


def test_python_no_build_recipe_works(source):
    with prepare(BuildRecipe(language="python", inputs=("Main.java",)), source) as run:
        assert run.manifest.artifacts == ()
        assert_current(run)


def test_trusted_argv_has_no_shell_and_token_not_inherited(source, monkeypatch):
    monkeypatch.setenv("DEBUG_TOKEN", "synthetic-secret")
    script = "import os,sys; from pathlib import Path; assert 'DEBUG_TOKEN' not in os.environ; Path('out.bin').write_text(sys.argv[1])"
    literal = "$(touch unwanted); literal"
    configured = recipe(build_commands=(Command((sys.executable, "-c", script, literal)),))
    with prepare(configured, source) as run:
        assert (run.workspace / "out.bin").read_text() == literal
        assert not (run.workspace / "unwanted").exists()


def test_toolchain_mismatch_and_missing_tool(source, tmp_path):
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    with prepare(recipe(toolchain_probes=(Command((str(tool),)),)), source) as run:
        tool.write_text("#!/bin/sh\nexit 1\n")
        with pytest.raises(RuntimeError, match="provenance mismatch"):
            assert_current(run)
    with pytest.raises(ValueError, match="Missing toolchain"):
        prepare(recipe(toolchain_probes=(Command(("peeky-missing-compiler-12345",)),)), source)


def test_relative_tool_traversal_and_missing_cwd_rejected(source):
    with pytest.raises(ValueError, match="traversal"):
        prepare(recipe(build_commands=(Command(("../outside",)),)), source)
    with pytest.raises(ValueError, match="cwd"):
        prepare(recipe(build_commands=(command("pass", cwd="missing"),)), source)


def test_prebuilt_artifact_is_not_a_clean_build(source):
    (source / "out.bin").write_text("old")
    with pytest.raises(ValueError, match="fresh build command"):
        prepare(recipe(inputs=("Main.java", "out.bin"), build_commands=()), source)


def test_ambient_build_flags_not_inherited(source, monkeypatch):
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "ambient-unrecorded-flags")
    configured = recipe(
        build_commands=(
            command(
                "import os; from pathlib import Path; assert 'JAVA_TOOL_OPTIONS' not in os.environ; Path('out.bin').write_text(os.environ['BUILD_MODE'])"
            ),
        )
    )
    with prepare(configured, source) as run:
        env = dict(run.manifest.effective_environment)
        assert "JAVA_TOOL_OPTIONS" not in env
        assert env["BUILD_MODE"] == "debug"
        assert "DEBUG_TOKEN" not in env


def test_command_failure_is_typed_and_does_not_swallow_integrity_failure(source):
    from peeky.build import BuildCommandError

    failing = command("raise SystemExit(7)")
    with pytest.raises(BuildCommandError) as caught:
        prepare(recipe(build_commands=(failing,)), source)
    assert caught.value.command == failing.argv
    assert caught.value.result["exit_code"] == 7
    overwrite = command(
        "from pathlib import Path; Path('Main.java').write_text('changed'); Path('out.bin').write_text('output')"
    )
    with pytest.raises(RuntimeError, match="provenance") as caught:
        prepare(recipe(build_commands=(overwrite,)), source)
    assert not isinstance(caught.value, BuildCommandError)


@pytest.mark.parametrize("action", ["mutate", "delete"])
def test_failed_tool_integrity_change_is_not_retryable(source, tmp_path, action):
    from peeky.build import BuildCommandError

    tool = tmp_path / "self-changing-tool"
    mutation = 'printf "\\n# changed\\n" >> "$0"' if action == "mutate" else 'rm "$0"'
    tool.write_text(f"#!/bin/sh\n{mutation}\nexit 7\n")
    tool.chmod(0o755)
    parent = tmp_path / "builds"
    parent.mkdir()
    with pytest.raises(RuntimeError, match="Toolchain executable changed") as caught:
        prepare(recipe(build_commands=(Command((str(tool),)),)), source, parent=parent)
    assert not isinstance(caught.value, BuildCommandError)
    assert list(parent.iterdir()) == []


def test_failed_probe_is_not_candidate_compile_feedback(source, tmp_path):
    from peeky.build import BuildCommandError

    parent = tmp_path / "builds"
    parent.mkdir()
    with pytest.raises(RuntimeError, match="Toolchain probe failed") as caught:
        prepare(
            recipe(toolchain_probes=(command("raise SystemExit(7)"),)),
            source,
            parent=parent,
        )
    assert not isinstance(caught.value, BuildCommandError)
    assert isinstance(caught.value.__cause__, BuildCommandError)
    assert caught.value.__cause__.result["exit_code"] == 7
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("location", ["staged", "original"])
@pytest.mark.parametrize("action", ["modify", "delete"])
def test_failed_compile_input_drift_is_fatal(source, tmp_path, location, action):
    from peeky.build import BuildCommandError

    path = "Main.java" if location == "staged" else str(source / "Main.java")
    operation = "write_text('changed')" if action == "modify" else "unlink()"
    script = f"from pathlib import Path; Path({path!r}).{operation}; raise SystemExit(7)"
    parent = tmp_path / "builds"
    parent.mkdir()
    with pytest.raises(RuntimeError, match="Source provenance changed") as caught:
        prepare(recipe(build_commands=(command(script),)), source, parent=parent)
    assert not isinstance(caught.value, BuildCommandError)
    assert list(parent.iterdir()) == []
