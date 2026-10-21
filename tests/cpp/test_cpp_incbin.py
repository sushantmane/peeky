"""C++ inline assembly that reads a file is refused before building (#180), on a real
clang++ build: `.incbin` read a file no dependency file lists. A `.incbin` string
outside inline assembly builds as before."""

import json
import os

import pytest

from peeky.assembler import ASSEMBLER_REFUSAL
from peeky.cli import main

INCBIN = """#include <cstdio>
#ifdef __APPLE__
#define SYMBOL "_blob"
#else
#define SYMBOL "blob"
#endif
asm(R"(.data
.globl )" SYMBOL "\\n" SYMBOL R"(:
.incbin "DATA"
.byte 0
.text)");
extern "C" const char blob[];
int main() {
    const char *p = blob;
    std::printf("blob=%s\\n", p); // OBSERVE
}
"""
PLAIN = """#include <cstdio>
int main() {
    const char *p = R"(.incbin "DATA")";
    std::printf("text=%s\\n", p); // OBSERVE
}
"""


@pytest.mark.timeout(180)
def test_cpp_inline_assembly_that_reads_a_file_is_refused_before_building(
    tmp_path, monkeypatch, capsys
):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    data = tmp_path / "out" / "data.bin"
    data.parent.mkdir()
    data.write_bytes(b"SECRET")
    root = tmp_path / "program"
    root.mkdir()
    results = []
    for text in (INCBIN, PLAIN):
        (root / "program.cpp").write_text(text.replace("DATA", str(data)))
        line = next(i for i, row in enumerate(text.splitlines(), 1) if "// OBSERVE" in row)
        command = ["probe", str(root), "--at", str(line), "--capture", "p", "--json", "--quiet"]
        code = main([*command, "--no-save"])
        results.append((code, json.loads(capsys.readouterr().out)))
    (code, refused), (built, plain) = results
    error = refused.get("error") or {}
    assert code == 2 and error.get("code") == "UNSUPPORTED", refused
    assert error["message"].startswith(
        f"{ASSEMBLER_REFUSAL}: the asm at program.cpp:7 uses `.incbin`, which reads a file"
    ), error
    assert refused.get("refused_before_launch") is True and "output" not in refused, refused
    assert built == 0 and plain["status"] == "completed", plain.get("error")
    assert "text=.incbin" in plain["output"]
