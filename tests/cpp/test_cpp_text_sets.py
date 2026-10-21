"""Setting a C++ `std::string` to a text (#137 part 2), on real builds: libc++ (macOS)
and libstdc++ (Linux). The text, its NUL and the new size go in the string's own buffer
or its heap buffer, within its current capacity: never reallocating, never past it."""

import json
import os
import sys

import pytest

from peeky.cli import main

TEXTS = """#include <cstdio>
#include <string>
int main() {
  std::string shortv = "apple ";
  std::string longv = "a fairly long string that is on the heap";
  longv.reserve(64);
  const std::string &ref = shortv;
  std::printf("S:[%s] L:[%s] R:%zu\\n", shortv.c_str(), longv.c_str(), ref.size()); // OBSERVE
  return 0;
}
"""
OBSERVE = next(i for i, text in enumerate(TEXTS.splitlines(), 1) if "// OBSERVE" in text)
LONG = "a heap string, changed in place"


@pytest.fixture
def texts(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "texts.cpp"
    source.write_text(TEXTS)
    return source


def probe(capsys, source, *argv):
    at = ["probe", str(source), "--at", str(OBSERVE), "--capture", "shortv,longv"]
    code = main([*at, *argv, "--json", "--quiet"])
    return code, json.loads(capsys.readouterr().out)


def debugger(capsys, source):
    """(reads, writes): whether this debugger reads memory (lldb-dap 20 and 21: libc++'s
    strings show LLDB's summary on macOS, libstdc++'s are read from memory) and writes
    it (lldb-dap 21, on macOS here)."""
    code, result = probe(capsys, source)
    assert code == 0, result.get("error")
    viewed = any(row.get("view") == "memory" for row in result["rows"])
    return sys.platform == "darwin" or viewed, sys.platform == "darwin"


def refusal(code, result, *texts):
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED", result
    for text in texts:
        assert text in result["error"]["message"], result["error"]
    return result["error"]["message"]


@pytest.mark.timeout(300)
def test_a_std_string_takes_a_text_within_its_capacity(texts, capsys):
    # #137 part 2: `--set shortv="pear"` was refused ("Native mutations require bounded
    # decimal integer literals").
    reads, writes = debugger(capsys, texts)
    code, result = probe(capsys, texts, "--set", 'shortv="pear"')
    if not reads:
        # lldb-dap 18 shows no address and reads no memory: where the text is held (in
        # the string or on the heap) cannot be told.
        refusal(code, result, "cannot read memory (lldb-dap 20 can)")
        return
    assert code == 0, result.get("error")
    # The reference sees the new size: the string itself changed, in place.
    assert "S:[pear] L:[a fairly long string that is on the heap] R:4" in result["output"]
    assert [(c["before"], c["after"]) for c in result["interventions"]] == [('"apple "', '"pear"')]
    after = [row for row in result["rows"] if row["phase"] == "after-set"]
    assert after[0]["value"].endswith('"pear"'), after
    code, result = probe(capsys, texts, "--set", f'longv="{LONG}"')
    if not writes:
        refusal(code, result, "its text is on the heap, and this debugger cannot write memory")
        return
    assert code == 0, result.get("error")
    assert f"L:[{LONG}]" in result["output"], result["output"]
    # Longer than the text it held, within the capacity `reserve(64)` gave.
    code, result = probe(capsys, texts, "--set", f'longv="{"x" * 64}"')
    assert code == 0 and f"L:[{'x' * 64}]" in result["output"], result


@pytest.mark.timeout(300)
def test_a_std_string_never_grows_past_its_capacity_or_through_a_reference(texts, capsys):
    reads, writes = debugger(capsys, texts)
    code, result = probe(capsys, texts, "--set", f'shortv="{"y" * 30}"')
    if reads:
        room = 22 if sys.platform == "darwin" else 15
        refusal(
            code,
            result,
            f"`shortv` holds its text in its own buffer, with room for {room} bytes, and the "
            "text is 30 bytes. Peeky never reallocates a string",
        )
        code, result = probe(capsys, texts, "--set", f'longv="{"z" * 200}"')
        refusal(code, result, "its heap buffer, with room for", "the text is 200 bytes")
    else:
        refusal(code, result, "cannot read memory")
    assert "S:[" not in (result.get("output") or ""), result
    code, result = probe(capsys, texts, "--set", 'ref="x"')
    refusal(code, result, "`ref` cannot be changed to a text", "does not write through a reference")
