"""Rust builds where no other user can write (security review before going public).

Cargo reads `.cargo/config.toml` in every directory above its build. Peeky checked those
directories, then ran Cargo later, in a directory under the system's /tmp. On a shared
Linux host, another user could add `/tmp/.cargo/config.toml` in between, with a
`build.rustc-wrapper` that runs their program as you. Builds now go only where no other
user can write, along the whole path, and each Cargo command is checked again.
"""

import ast
import os
import sys
import tempfile
from pathlib import Path

import pytest

from peeky.rust import RustTarget

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_rust_worker import example, scripted_toolchain  # noqa: E402

RECORD = (
    "import os\n"
    "with open({path!r}, 'a') as f:\n"
    "    f.write(repr((str(pathlib.Path.cwd()), os.environ.get('RUSTC_WRAPPER'))) + '\\n')\n"
)


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """A directory anyone may add entries to, as /tmp: the system's temporary directory.
    The user has no runtime directory."""
    path = (tmp_path / "shared").resolve()
    path.mkdir()
    path.chmod(0o1777)
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr("peeky.build.RUNTIME_DIRS", str(tmp_path / "no-runtime"), raising=False)
    return path


def test_a_sticky_directory_others_can_write_is_not_private(tmp_path, shared):
    from peeky.build import writable_by_others

    assert writable_by_others(shared) == shared.resolve()
    inside = shared / "mine"
    inside.mkdir(mode=0o700)
    assert writable_by_others(inside) == shared.resolve()
    assert writable_by_others(tmp_path) is None


def test_builds_go_to_the_state_directory_where_the_temporary_one_is_shared(
    tmp_path, shared, monkeypatch
):
    from peeky.build import private_base

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    assert private_base() == (tmp_path / "state" / "scratch").resolve()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert private_base() == tmp_path.resolve()


def test_rust_builds_where_no_other_user_can_write(tmp_path, shared, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = example(tmp_path)
    seen = tmp_path / "seen"
    config, marker = scripted_toolchain(tmp_path, cargo_build=RECORD.format(path=str(seen)))
    # The scripted cargo builds nothing, so the build fails after it ran.
    with pytest.raises((RuntimeError, ValueError)):
        RustTarget(**config).prepare(root / "src/main.rs", root)
    assert marker.read_text() == "build\ntest\n"
    scratch = (tmp_path / "state" / "scratch").resolve()
    for cwd, wrapper in map(ast.literal_eval, seen.read_text().splitlines()):
        assert Path(cwd).is_relative_to(scratch) and not Path(cwd).is_relative_to(shared)
        # Empty: Cargo runs no `build.rustc-wrapper` from any configuration.
        assert wrapper == ""
    assert list(scratch.iterdir()) == [] and list(shared.iterdir()) == []


def test_rust_refuses_to_build_where_others_can_write(tmp_path, shared, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(shared / "state"))
    root = example(tmp_path)
    config, marker = scripted_toolchain(tmp_path)
    with pytest.raises(ValueError) as refused:
        RustTarget(**config).prepare(root / "src/main.rs", root)
    message = str(refused.value)
    assert message.startswith("Peeky builds only where no other user can write")
    assert str(shared.resolve()) in message and "PEEKY_STATE_DIR" in message
    assert not marker.exists()


def test_rust_checks_again_before_each_cargo_command(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(mode=0o700)
    root = example(tmp_path)
    opened = "pathlib.Path.cwd().parent.chmod(0o777)\n"
    config, marker = scripted_toolchain(tmp_path, cargo_build=opened)
    with pytest.raises(ValueError, match="^Peeky builds only where no other user can write"):
        RustTarget(**config).prepare(root / "src/main.rs", root)
    assert marker.read_text() == "build\n"
    assert [path.name for path in (tmp_path / "state").iterdir()] == []


def test_the_state_directory_must_be_private_too(tmp_path, monkeypatch):
    from peeky.build import private_base

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    os.chmod(tmp_path, 0o777)
    try:
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        with pytest.raises(ValueError, match="no other user can write"):
            private_base()
    finally:
        os.chmod(tmp_path, 0o700)


def test_the_state_directory_is_private_whatever_the_umask(tmp_path, shared, monkeypatch):
    # Debian and Ubuntu give each user a group of their own, and a umask of 002: the state
    # directory Peeky made was group-writable, and builds under it were refused.
    import stat

    from peeky.build import private_base

    state = tmp_path / "state"
    monkeypatch.setenv("PEEKY_STATE_DIR", str(state))
    umask = os.umask(0o002)
    try:
        assert private_base() == (state / "scratch").resolve()
    finally:
        os.umask(umask)
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    # One that an earlier Peeky made group-writable loses that.
    state.chmod(0o775)
    assert private_base() == (state / "scratch").resolve()
    assert stat.S_IMODE(state.stat().st_mode) == 0o755


def test_a_crate_builds_in_the_runtime_directory_if_cargo_reads_configuration_above_scratch(
    tmp_path, shared, monkeypatch
):
    # Linux: /tmp is shared, and the state directory is under a home that holds the user's
    # own ~/.cargo/config.toml, which Cargo would read above a build there.
    from peeky.build import private_base
    from peeky.rust import _cargo_free

    home = tmp_path / "home"
    (home / ".cargo").mkdir(parents=True)
    (home / ".cargo" / "config.toml").write_text('[build]\nrustc-wrapper = "sccache"\n')
    monkeypatch.setenv("PEEKY_STATE_DIR", str(home / ".peeky"))
    with pytest.raises(ValueError) as refused:
        private_base(_cargo_free)
    message = str(refused.value)
    assert message.startswith("Peeky builds a Rust crate only where Cargo reads no configuration")
    assert str(home / ".cargo" / "config.toml") in message and "Set TMPDIR" in message
    # C and C++ build there.
    assert private_base() == (home / ".peeky" / "scratch").resolve()
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    assert private_base(_cargo_free) == private_base() == runtime.resolve()
