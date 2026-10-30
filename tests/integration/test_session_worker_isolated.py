"""A session's own worker runs this Peeky, whatever the current directory holds (security
review before going public).

`peeky session start` ran `python -m peeky.worker` in the current directory, without
`-I`, so a `peeky/` package there ran instead of Peeky's worker, with the session's
token. It is now started as the probe process and background jobs are: isolated,
importing this Peeky by absolute path, in Peeky's own directory.
"""

import json

from peeky.cli import main

DECOY = """\
import pathlib
pathlib.Path({marker!r}).write_text("the decoy peeky package ran")
raise SystemExit(7)
"""


def test_a_peeky_package_in_the_current_directory_never_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    marker = tmp_path / "decoy-ran"
    decoy = tmp_path / "cwd" / "peeky"
    decoy.mkdir(parents=True)
    for name in ("__init__.py", "worker.py", "__main__.py"):
        (decoy / name).write_text(DECOY.format(marker=str(marker)))
    program = tmp_path / "app" / "main.py"
    program.parent.mkdir()
    program.write_text("x = 1\nprint(x)\n")
    monkeypatch.chdir(decoy.parent)
    try:
        code = main(["session", "start", str(program), "--at", "2", "--json"])
        out = capsys.readouterr().out
        assert not marker.exists(), "the decoy peeky package ran as the session's worker"
        assert code == 0, out
        assert json.loads(out)["state"] == "stopped"
    finally:
        main(["session", "close"])
