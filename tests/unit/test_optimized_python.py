import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[2] / "src")


@pytest.mark.parametrize("module", ["peeky.cli", "peeky.java_setup"])
def test_help_works_under_python_oo(module):
    # `python -OO` (or PYTHONOPTIMIZE=2) drops docstrings; the help text must not need them.
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONOPTIMIZE", None)
    done = subprocess.run(
        [sys.executable, "-OO", "-c", f"import {module} as m; m.main()", "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert "usage:" in done.stdout
