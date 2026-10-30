"""Inline assembly that reads a file is refused before building, on real builds with clang
and GCC: the assembler's `.incbin` reads a file that no dependency file lists, so the program
would run on bytes no provenance check covers (`blob=SECRET`). A `.incbin` string outside
inline assembly builds."""

import json
import os
import shutil
import subprocess

import pytest

from peeky.assembler import ASSEMBLER_REFUSAL
from peeky.cli import main

INCBIN = """#include <stdio.h>
#ifdef __APPLE__
#define SYMBOL "_blob"
#else
#define SYMBOL "blob"
#endif
__asm__(".data\\n.globl " SYMBOL "\\n" SYMBOL ":\\n.incbin \\"DATA\\"\\n.byte 0\\n.text\\n");
extern const char blob[];
int main(void) {
    const char *p = blob;
    printf("blob=%s\\n", p); // OBSERVE
    return 0;
}
"""
PLAIN = """#include <stdio.h>
int main(void) {
    const char *p = ".incbin \\"DATA\\"";
    printf("text=%s\\n", p); // OBSERVE
    return 0;
}
"""


def _gcc():
    gcc = shutil.which("gcc")
    version = subprocess.run([gcc, "--version"], capture_output=True, text=True) if gcc else None
    return gcc if version is not None and "Free Software Foundation" in version.stdout else None


@pytest.fixture(params=["clang", "gcc"])
def build(request, tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    compiler = os.environ.get("PEEKY_NATIVE_CC", "clang") if request.param == "clang" else _gcc()
    if compiler is None:
        pytest.skip("needs GCC")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    # Outside the program's directory, as an absolute path: nothing declares it.
    data = tmp_path / "out" / "data.bin"
    data.parent.mkdir()
    data.write_bytes(b"SECRET")
    root = tmp_path / "program"
    root.mkdir()
    config = root / "program.json"

    def probe(capsys, text, save=False):
        (root / "program.c").write_text(text.replace("DATA", str(data)))
        config.write_text(
            json.dumps(
                {
                    "language": "c",
                    "sources": ["program.c"],
                    "compiler": compiler,
                    "adapter": os.environ["PEEKY_NATIVE_DAP"],
                    "dwarfdump": os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
                    "dsymutil": os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
                    "sysroot": os.environ.get("PEEKY_NATIVE_SYSROOT")
                    if request.param == "clang"
                    else None,
                }
            )
        )
        line = next(i for i, row in enumerate(text.splitlines(), 1) if "// OBSERVE" in row)
        command = ["probe", str(root), "--config", str(config), "--at", f"program.c:{line}"]
        code = main(
            [*command, "--capture", "p", "--json", "--quiet", *([] if save else ["--no-save"])]
        )
        return code, json.loads(capsys.readouterr().out)

    return probe, tmp_path


@pytest.mark.timeout(180)
def test_inline_assembly_that_reads_a_file_is_refused_before_building(build, capsys):
    probe, tmp_path = build
    code, result = probe(capsys, INCBIN, save=True)
    error = result.get("error") or {}
    assert code == 2 and error.get("code") == "UNSUPPORTED", result
    assert error["message"].startswith(
        f"{ASSEMBLER_REFUSAL}: the asm at program.c:7 uses `.incbin`, which reads a file"
    ), error
    assert "declared source or header" in error["hint"]
    # Nothing ran, so no run is kept as evidence.
    assert result.get("refused_before_launch") is True and "output" not in result, result
    assert not (tmp_path / "state" / "evidence" / "evidence.db").exists()


@pytest.mark.timeout(180)
def test_an_incbin_string_outside_inline_assembly_builds(build, capsys):
    probe, _ = build
    code, result = probe(capsys, PLAIN)
    assert code == 0 and result["status"] == "completed", result.get("error")
    assert '.incbin \\"' in result["rows"][0]["value"] and "text=.incbin" in result["output"]
