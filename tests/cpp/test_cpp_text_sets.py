"""Setting a C++ `std::string` to a text (#137 part 2), on real builds: libc++ (macOS)
and libstdc++ (Linux). The text, its NUL and the new size go in the string's own buffer
or its heap buffer, within its current capacity: never reallocating, never past it."""

import json
import os
import sys

import pytest

from peeky.cli import main


def _refused_code(result):
    # #165: a probe refused before it ran exits 2; a refusal at a stop, 1.
    return 2 if result.get("refused_before_launch") else 1


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
    assert code == _refused_code(result) and result["error"]["code"] == "MUTATION_REJECTED", result
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


VIEWS = """#include <cstdio>
#include <string>
#include <string_view>
typedef const std::string CStr;
using Name = std::string;
int main() {
  std::string s;
  s.reserve(40);
  s = "hello world";
  std::string_view sv = s;
  const std::string cs = "konst";
  CStr ct = "ctype";
  Name nm = "named";
  std::printf("SV:%zu S:[%s] CS:[%s] CT:[%s] NM:[%s] CAP:%zu\\n", sv.size(), s.c_str(), cs.c_str(), ct.c_str(), nm.c_str(), s.capacity()); // LOOK
  return 0;
}
"""


@pytest.mark.timeout(300)
def test_a_const_string_is_never_changed_and_a_change_warns_of_earlier_views(texts, capsys):
    # #164 review: `const std::string cs` was changed and marked [settable] (undefined
    # behavior); a string_view taken before a change keeps the old size, unseen.
    source = texts.parent / "views.cpp"
    source.write_text(VIEWS)
    look = next(i for i, text in enumerate(VIEWS.splitlines(), 1) if "// LOOK" in text)
    at = ["probe", str(source), "--at", str(look), "--capture", "s,cs,ct,nm"]
    assert main([*at, "--json", "--quiet"]) == 0
    result = json.loads(capsys.readouterr().out)
    reads = sys.platform == "darwin" or any(row.get("view") == "memory" for row in result["rows"])
    marks = {row["selector"]: row.get("settable") for row in result["rows"]}
    # A string in its own buffer is changed element by element where the debugger reads
    # but does not write memory (lldb-dap 20): `nm`; `s` is on the heap.
    assert marks == {
        "s": sys.platform == "darwin",
        "cs": False,
        "ct": False,
        "nm": reads,
    }, marks
    for name in ("cs", "ct"):
        code = main([*at, "--set", f'{name}="x"', "--json", "--quiet"])
        result = json.loads(capsys.readouterr().out)
        assert code == _refused_code(result) and result["error"]["code"] == "MUTATION_REJECTED", (
            result
        )
        assert (
            f"`{name}` cannot be changed to a text: a const object" in (result["error"]["message"])
        ), result["error"]
    code = main([*at, "--set", 'nm="renamed"', "--json", "--quiet"])
    result = json.loads(capsys.readouterr().out)
    if not reads:
        assert code == 1 and "cannot read memory" in result["error"]["message"], result
        return
    assert code == 0 and "NM:[renamed]" in result["output"], result
    if sys.platform != "darwin":
        return
    code = main([*at, "--set", 's="bye"', "--json", "--quiet"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    # The view taken before keeps the old size: the program says so, and so does Peeky.
    assert "SV:11 S:[bye]" in result["output"], result["output"]
    note = result["interventions"][0]["note"]
    assert "std::string_view" in note and "old size" in note, note
    assert f"Changed `s`: {note}" in result["warnings"], result["warnings"]
    # libc++ keeps the allocation's size in `__cap_`: capacity() 47 for reserve(40),
    # all of it usable (one byte short before #164's review).
    assert "CAP:47" in result["output"], result["output"]
    code = main([*at, "--set", f's="{"c" * 47}"', "--json", "--quiet"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and f"S:[{'c' * 47}]" in result["output"], result
    code = main([*at, "--set", f's="{"c" * 48}"', "--json", "--quiet"])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and "with room for 47 bytes" in result["error"]["message"], result
