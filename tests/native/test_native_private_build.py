"""C and C++ build where no other user can write (security review before going public).

The build ran in a directory under the system's /tmp. A source's `#include "../x.h"` reads
above the build's directory, so on a shared Linux host it read a header another user could
put in /tmp, and compiled it in. The build now goes only where no other user can write.
"""

import os
import tempfile

import pytest

from peeky.build import BuildCommandError

MAIN = '#include "../planted.h"\nint main(void) {\n  int x = PLANTED;\n  return x - 7;\n}\n'


def test_a_header_above_the_build_is_not_read_from_a_shared_directory(tmp_path, monkeypatch):
    from peeky.native import NativeTarget

    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    shared = (tmp_path / "shared").resolve()
    shared.mkdir()
    shared.chmod(0o1777)
    (shared / "planted.h").write_text("#define PLANTED 7\n")
    monkeypatch.setattr(tempfile, "tempdir", str(shared))
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    program = tmp_path / "program"
    program.mkdir()
    (program / "main.c").write_text(MAIN)
    target = NativeTarget(
        sources=("main.c",),
        compiler=os.environ.get("PEEKY_NATIVE_CC") or "clang",
        adapter=os.environ["PEEKY_NATIVE_DAP"],
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
    )
    try:
        session = target.prepare(program / "main.c", program)
    except BuildCommandError as failed:
        assert "planted.h" in failed.result["stderr"]
    else:
        session.close()
        pytest.fail("The build compiled planted.h from a directory others can write")
    assert [path.name for path in shared.iterdir()] == ["planted.h"]
