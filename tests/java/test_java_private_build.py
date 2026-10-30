"""A Java build where no other user can write (security review before going public).

The build ran in a directory under the system's /tmp. A program that reads above its own
classes (the build's directory, then the one it was made in) read a file another user could
put in /tmp. The build now goes only where no other user can write.
"""

import os
import tempfile
from pathlib import Path

import pytest

from peeky.probe import Probe, run_probe

SOURCE = """package demo;
import java.nio.file.*;
public class Up {
  public static void main(String[] args) throws Exception {
    Path classes = Paths.get(Up.class.getProtectionDomain().getCodeSource().getLocation().toURI());
    Path planted = classes.getParent().getParent().resolve("planted.txt");
    String found = Files.exists(planted) ? Files.readString(planted).trim() : "none";
    System.out.println("READ:" + found); // OBSERVE
  }
}
"""


@pytest.mark.timeout(120)
def test_a_file_above_the_build_is_not_read_from_a_shared_directory(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a prepared Java adapter bundle")
    from peeky.java import JavaTarget

    shared = (tmp_path / "shared").resolve()
    shared.mkdir()
    shared.chmod(0o1777)
    (shared / "planted.txt").write_text("planted\n")
    monkeypatch.setattr(tempfile, "tempdir", str(shared))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr("peeky.build.RUNTIME_DIRS", str(tmp_path / "no-runtime"), raising=False)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "program"
    (root / "demo").mkdir(parents=True)
    (root / "demo/Up.java").write_text(SOURCE)
    line = next(i for i, text in enumerate(SOURCE.splitlines(), 1) if "OBSERVE" in text)
    target = JavaTarget(bundle=Path(bundle), sources=("demo/Up.java",), main_class="demo.Up")
    result = run_probe(root, Probe("demo/Up.java", line, ("found",)), adapter=target)
    assert result["status"] == "completed", result.get("error")
    assert "READ:none" in result["output"], result["output"]
    assert [path.name for path in shared.iterdir()] == ["planted.txt"]
