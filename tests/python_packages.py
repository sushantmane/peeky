"""A package installed in a virtual environment by pip, not editable, for the Python
suites: its files are in the environment's site-packages, compiled there as pip does."""

import base64
import hashlib
import subprocess
import sys
import zipfile

import pytest

TALLY = """\
def total(items):
    acc = 0
    for x in items:
        step = x * 2
        acc += step
    return acc
"""
OUTER = """\
from tally import total


def twice(items):
    doubled = [n + n for n in items]
    return total(doubled)
"""


def wheel(where):
    """A wheel of the package `tally`, made by hand: nothing is downloaded or built."""
    files = {
        "tally/__init__.py": TALLY,
        "tally/outer.py": OUTER,
        "tally-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: tally\nVersion: 1.0\n",
        "tally-1.0.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: peeky-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = []
    for name, text in files.items():
        data = text.encode()
        sha = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{name},sha256={sha},{len(data)}")
    record.append("tally-1.0.dist-info/RECORD,,")
    path = where / "tally-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
        archive.writestr("tally-1.0.dist-info/RECORD", "\n".join(record) + "\n")
    return path


def install(root, where):
    """A `.venv` in ``root`` with `tally` installed by pip, not editable: its files are
    in the environment's site-packages, compiled to bytecode there as pip does."""
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")],
        check=True,
        capture_output=True,
        timeout=120,
    )
    python = root / ".venv" / "bin" / "python"
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "--version"], check=True, capture_output=True, timeout=60
        )
    except (subprocess.CalledProcessError, OSError):
        pytest.skip("pip is not installed for this Python")
    subprocess.run(
        [
            *(sys.executable, "-m", "pip", "--python", str(python), "install"),
            *("--no-index", "--no-deps", "--no-cache-dir", "--disable-pip-version-check"),
            str(wheel(where)),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    found = sorted((root / ".venv").glob("lib/python3*/site-packages/tally/__init__.py"))
    assert len(found) == 1, found
    return found[0].relative_to(root).as_posix()
