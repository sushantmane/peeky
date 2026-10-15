import os
import sys

import pytest

from peeky.native import MAX_METADATA, _metadata, parse_lines


def test_engineering_capture_does_not_relax_production_parser(tmp_path):
    writer = tmp_path / "writer.py"
    writer.write_text(
        'import pathlib, sys\npathlib.Path(sys.argv[-1]).write_bytes(b"x" * (3 * 1024 * 1024))\n'
    )
    with pytest.raises((RuntimeError, ValueError), match="budget|limit"):
        _metadata(sys.executable, [str(writer)], tmp_path, dict(os.environ))
    dump = _metadata(
        sys.executable, [str(writer)], tmp_path, dict(os.environ), max_bytes=4 * 1024 * 1024
    )
    assert len(dump) == 3 * 1024 * 1024 > MAX_METADATA
    with pytest.raises(ValueError, match="line metadata exceeds"):
        parse_lines(dump, tmp_path, ())
    assert not list(tmp_path.glob("peeky-symbols-*"))


@pytest.mark.parametrize("limit", [True, 0, -1, 16 * 1024 * 1024 + 1])
def test_capture_limit_cannot_be_unbounded(tmp_path, limit):
    with pytest.raises(ValueError, match="capture limit"):
        _metadata("unused", [], tmp_path, {}, max_bytes=limit)


def test_exited_process_group_permission_error_is_benign(tmp_path, monkeypatch):
    from peeky.agent import execute

    writer = tmp_path / "writer.py"
    writer.write_text('import pathlib, sys\npathlib.Path(sys.argv[-1]).write_text("ok")\n')

    def zombie_group(pid, sig):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr("peeky.native.os.killpg", zombie_group)
    assert _metadata(sys.executable, [str(writer)], tmp_path, dict(os.environ)) == "ok"
    monkeypatch.setattr("peeky.agent.os.killpg", zombie_group)
    assert execute([sys.executable, "-c", "print('done')"], tmp_path)["stdout"] == "done\n"
