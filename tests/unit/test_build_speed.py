"""Build speed without new trust (#398): toolchain probes run on every build, and only
their tools' digests are kept across processes; a parallel group of independent build
commands starts them in order, and raises the earliest failure, as running them in order
does."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from peeky import build, digest
from peeky.build import ArtifactSpec, BuildCommandError, BuildRecipe, Command, prepare


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("PEEKY_NO_DIGEST_CACHE", raising=False)
    monkeypatch.delenv("PEEKY_BUILD_JOBS", raising=False)
    digest.clear()
    yield
    digest.clear()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "main.c").write_text("int main(void) { return 0; }\n")
    return root


def python_tool(folder):
    """A copy of this Python's executable at ``folder``/bin/tool: a real binary (a
    script's probe is never reused), whose bytes a test can change."""
    real = Path(os.path.realpath(sys.executable))
    (folder / "bin").mkdir(parents=True)
    tool = folder / "bin" / "tool"
    shutil.copy2(real, tool)
    # It finds its library and standard library beside it (`../lib`).
    (folder / "lib").symlink_to(real.parent.parent / "lib")
    if subprocess.run([tool, "-I", "-c", "pass"], check=False).returncode:
        pytest.skip("a copy of this Python's executable does not run here")
    return tool


def probe_recipe(tool, script):
    return BuildRecipe(
        language="fixture",
        inputs=("main.c",),
        build_commands=(
            Command(
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('out').write_bytes(Path('main.c').read_bytes())",
                )
            ),
        ),
        artifacts=(ArtifactSpec("out"),),
        toolchain_probes=(Command((str(tool), "-I", "-c", script)),),
    )


def probed(recipe, source):
    with prepare(recipe, source, keep_tool_digests=True) as run:
        return run.manifest.probes[0].stdout, run.manifest.build_id


def test_a_config_file_rewritten_in_place_is_probed_again(source, tmp_path):
    # Review of #400: a probe's output was reused while the tool's bytes and its
    # directory's entries were unchanged, so a clang.cfg rewritten in place went unseen.
    tool = python_tool(tmp_path / "tools")
    config = tool.parent / "clang.cfg"
    config.write_text("-O0\n")
    recipe = probe_recipe(tool, f"print('config:', open({str(config)!r}).read().strip())")
    first = probed(recipe, source)
    with open(config, "r+") as stream:
        stream.write("-O2\n")
    second = probed(recipe, source)
    assert (first[0], second[0]) == ("config: -O0\n", "config: -O2\n")
    assert first[1] != second[1]


def test_a_wrappers_probe_follows_the_program_it_runs(source, tmp_path):
    # Review of #400: a compiled wrapper's own bytes don't change when the program it
    # runs does; the probe must ask again.
    tool = python_tool(tmp_path / "tools")
    delegate = tmp_path / "real-clang"
    delegate.write_text("#!/bin/sh\necho clang version 18\n")
    delegate.chmod(0o755)
    script = (
        "import subprocess, sys; "
        f"sys.stdout.write(subprocess.run([{str(delegate)!r}], capture_output=True, text=True).stdout)"
    )
    recipe = probe_recipe(tool, script)
    first = probed(recipe, source)
    delegate.write_text("#!/bin/sh\necho clang version 19\n")
    second = probed(recipe, source)
    assert (first[0], second[0]) == ("clang version 18\n", "clang version 19\n")
    assert first[1] != second[1]


def test_a_tools_kept_digest_is_reused_by_a_new_process(source, tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "SETTLE_SECONDS", -1.0)
    tool = python_tool(tmp_path / "tools")
    recipe = probe_recipe(tool, "print('probe')")
    probed(recipe, source)
    store = tmp_path / "state/cache/digests"
    assert {path.stat().st_mode & 0o777 for path in store.iterdir()} == {0o600}
    digest.clear()  # a new process
    hashed = []
    real = digest.hashlib.sha256
    monkeypatch.setattr(digest.hashlib, "sha256", lambda *a: hashed.append(a) or real(*a))
    assert digest.sha256_file(tool.resolve(), store=store) == real(tool.read_bytes()).hexdigest()
    assert [args for args in hashed if not args] == []
    # Without opting in, or with the cache off, nothing is kept.
    for entry in store.iterdir():
        entry.unlink()
    with prepare(recipe, source):
        pass
    monkeypatch.setenv("PEEKY_NO_DIGEST_CACHE", "1")
    probed(recipe, source)
    assert list(store.iterdir()) == []


def write(name, text, *, barrier=(), wait=0.0, fail=None):
    """A build command writing ``name``; it first waits for each file of ``barrier``
    (exit 3 after 20 s), sleeps ``wait`` s, and with ``fail`` writes it to stderr and
    exits 1."""
    script = (
        "import pathlib, sys, time\n"
        f"pathlib.Path({name!r} + '.started').write_text('')\n"
        f"end = time.monotonic() + 20\n"
        f"while not all(pathlib.Path(n).exists() for n in {list(barrier)!r}):\n"
        "    if time.monotonic() > end: sys.exit(3)\n"
        "    time.sleep(0.01)\n"
        f"time.sleep({wait})\n"
        f"if {fail!r}: sys.stderr.write({fail!r}); sys.exit(1)\n"
        f"pathlib.Path({name!r}).write_text({text!r} + pathlib.Path('main.c').read_text())\n"
    )
    return Command((sys.executable, "-c", script))


def group_recipe(commands):
    return BuildRecipe(
        language="fixture",
        inputs=("main.c",),
        build_commands=tuple(commands),
        artifacts=tuple(ArtifactSpec(f"out{index}") for index in range(len(commands))),
    )


def test_a_parallel_groups_commands_run_at_once(source, monkeypatch):
    monkeypatch.setenv("PEEKY_BUILD_JOBS", "4")
    names = [f"out{index}" for index in range(4)]
    # Each waits until all have started: run one at a time, the first never ends.
    commands = [write(name, name, barrier=[f"{n}.started" for n in names]) for name in names]
    recipe = group_recipe(commands)
    with prepare(recipe, source, parallel=(tuple(commands),)) as run:
        assert [item.path for item in run.manifest.artifacts] == names


def test_deterministic_commands_build_the_same_in_parallel_and_in_order(source, monkeypatch):
    commands = [
        write(f"out{index}", f"unit {index}\n", wait=0.05 * (3 - index)) for index in range(4)
    ]
    recipe = group_recipe(commands)
    manifests = []
    for workers in ("1", "4"):
        monkeypatch.setenv("PEEKY_BUILD_JOBS", workers)
        with prepare(recipe, source, parallel=(tuple(commands),)) as run:
            manifests.append(run.manifest)
            assert (run.workspace / "out2").read_text().startswith("unit 2\n")
    sequential, parallel = manifests
    assert parallel.artifacts == sequential.artifacts
    assert parallel.tools == sequential.tools
    assert parallel.build_id == sequential.build_id


def test_a_parallel_groups_failure_is_the_earliest_commands(source, tmp_path, monkeypatch):
    commands = [
        write("out0", "ok"),
        write("out1", "ok", wait=0.5, fail="first error"),
        # Stopped when the one before it fails: it is never waited for.
        write("out2", "ok", wait=30),
        write("out3", "ok", fail="second error"),
    ]
    recipe = group_recipe(commands)
    parent = tmp_path / "builds"
    parent.mkdir()
    failures = []
    for workers in ("1", "4"):
        monkeypatch.setenv("PEEKY_BUILD_JOBS", workers)
        started = time.monotonic()
        with pytest.raises(BuildCommandError) as caught:
            prepare(recipe, source, parent=parent, parallel=(tuple(commands),))
        assert time.monotonic() - started < 15
        failures.append((caught.value.command, str(caught.value), caught.value.result["stderr"]))
        assert list(parent.iterdir()) == []
    assert failures[1] == failures[0]
    assert failures[0][0] == commands[1].argv and failures[0][2] == "first error"


def test_no_command_after_a_failure_starts(source, tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_BUILD_JOBS", "2")
    parent = tmp_path / "builds"
    parent.mkdir()
    commands = [write("out0", "ok", fail="error"), write("out1", "ok", wait=30)]
    commands += [write(f"out{index}", "ok") for index in (2, 3)]
    started = time.monotonic()
    with pytest.raises(BuildCommandError) as caught:
        prepare(group_recipe(commands), source, parent=parent, parallel=(tuple(commands),))
    assert caught.value.command == commands[0].argv
    assert time.monotonic() - started < 15
    assert list(parent.iterdir()) == []


def test_a_command_claimed_before_an_earlier_failure_never_starts(source, monkeypatch):
    # Review of #400: a worker claimed a command, released the group's lock, and could
    # still start its process after a command before it had failed.
    from peeky import agent

    monkeypatch.setenv("PEEKY_BUILD_JOBS", "2")
    commands = [write("out0", "ok", fail="error"), write("out1", "ok")]
    real_run, real_popen = build._run, subprocess.Popen
    waited, started = [], []

    def run(command, *args, **kwargs):
        if command == commands[1]:
            # Claimed: held between the claim and its process's start until the
            # failure before it is recorded (which sets its stop).
            stop = kwargs["stop"] if "stop" in kwargs else args[3]
            waited.append(stop.wait(20))
        return real_run(command, *args, **kwargs)

    def popen(argv, *args, **kwargs):
        started.append(tuple(argv[1:]))
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(build, "_run", run)
    monkeypatch.setattr(agent.subprocess, "Popen", popen)
    with pytest.raises(BuildCommandError) as caught:
        prepare(group_recipe(commands), source, parallel=(tuple(commands),))
    assert caught.value.command == commands[0].argv
    assert waited == [True]
    assert commands[0].argv[1:] in started and commands[1].argv[1:] not in started


def test_a_parallel_group_past_the_deadline_is_a_timeout(source, monkeypatch):
    monkeypatch.setenv("PEEKY_BUILD_JOBS", "4")
    commands = [write(f"out{index}", "ok", wait=30) for index in range(3)]
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        prepare(
            group_recipe(commands),
            source,
            parallel=(tuple(commands),),
            deadline=started + 1,
        )
    assert time.monotonic() - started < 15


def test_parallel_groups_are_runs_of_the_build_commands(source):
    commands = [write(f"out{index}", "ok") for index in range(3)]
    recipe = group_recipe(commands)
    for groups in (
        ((commands[0], commands[2]),),
        ((commands[1], commands[0]),),
        ((commands[0], commands[1]), (commands[1], commands[2])),
        ((),),
    ):
        with pytest.raises(ValueError, match="parallel group"):
            prepare(recipe, source, parallel=groups)


@pytest.mark.parametrize("value", ["0", "x", "65", "-1"])
def test_build_jobs_must_be_a_count(monkeypatch, value):
    monkeypatch.setenv("PEEKY_BUILD_JOBS", value)
    with pytest.raises(ValueError, match="PEEKY_BUILD_JOBS"):
        build.jobs()


def test_build_jobs_are_bounded(monkeypatch):
    assert 1 <= build.jobs() <= build.MAX_JOBS
    monkeypatch.setenv("PEEKY_BUILD_JOBS", "1")
    assert build.jobs() == 1


def test_a_header_several_units_read_is_judged_once(tmp_path, monkeypatch):
    from peeky import native
    from peeky.build import UnreadDependencies

    workspace, outside = tmp_path / "ws", tmp_path / "out"
    (outside / "bs").mkdir(parents=True)
    workspace.mkdir()
    for name in ("main.c", "util.c", "a.h"):
        (workspace / name).write_text(name)
    for name in ("x.h", "y.h", "bs/z.h"):
        (outside / name).write_text(name)
    rules = {
        "0": f"main.c a.h {outside}/x.h {outside}/y.h",
        "1": f"util.c a.h {outside}/y.h {outside}/x.h",
    }
    depfiles = []
    for index, listed in rules.items():
        depfiles.append(workspace / f"peeky-object-{index}.o.d")
        depfiles[-1].write_text(f"peeky-object-{index}.o: {listed}\n")
    judged = []
    twin = native._backslash_twin
    monkeypatch.setattr(native, "_backslash_twin", lambda *a: judged.append(a[0]) or twin(*a))
    found = native.external_inputs(depfiles, workspace, ("main.c", "util.c", "a.h"))
    hashed = {str((outside / name).resolve()): name for name in ("x.h", "y.h")}
    assert {path: hashed[path] for path in found} == hashed
    assert sorted(found) == list(found)
    assert len(judged) == 2
    with pytest.raises(ValueError, match="util.c, which is not a declared"):
        native.external_inputs(depfiles, workspace, ("main.c", "a.h"))
    # clang prints `bs\z.h` as `bs/z.h`: either file may be the one it read.
    (outside / "bs\\z.h").write_text("twin")
    depfiles[0].write_text(f"peeky-object-0.o: main.c {outside}/bs/z.h\n")
    with pytest.raises(UnreadDependencies, match="may be"):
        native.external_inputs(depfiles[:1], workspace, ("main.c",))
