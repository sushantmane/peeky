"""Setting a C++ `std::string` to a text (#137 part 2), on real builds: libc++ (macOS)
and libstdc++ (Linux). The text, its NUL and the new size go in the string's own buffer
or its heap buffer, within its current capacity: never reallocating, never past it.

Built by clang and by g++ (#167). LLDB 18 and 20 read GCC's string buffer (`char[16]`)
as `char[0]`, so a text in it cannot be set character by character: lldb-dap 20, which
reads memory but has no writeMemory request, writes it with LLDB's `memory write`."""

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
def texts(cxx, tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "texts.cpp"
    source.write_text(TEXTS)
    return source


def probe(capsys, source, *argv):
    at = ["probe", str(source), "--at", str(OBSERVE), "--capture", "shortv,longv"]
    code = main([*at, *argv, "--json", "--quiet"])
    return code, json.loads(capsys.readouterr().out)


def reads_memory(rows):
    """Whether this debugger reads memory (lldb-dap 20 and 21): libc++'s strings show
    LLDB's summary on macOS, and libstdc++'s are read from memory, unless LLDB's summary
    is whole (GCC's: the lanes with such a debugger say so)."""
    viewed = any(row.get("view") == "memory" for row in rows)
    return sys.platform == "darwin" or viewed or bool(os.environ.get("PEEKY_EXPECT_READ_MEMORY"))


def debugger(capsys, source):
    """(reads, writes): whether this debugger reads memory (``reads_memory``) and writes
    it (lldb-dap 21, on macOS here)."""
    code, result = probe(capsys, source)
    assert code == 0, result.get("error")
    return reads_memory(result["rows"]), sys.platform == "darwin"


def refusal(code, result, *texts):
    assert code == _refused_code(result) and result["error"]["code"] == "MUTATION_REJECTED", result
    for text in texts:
        assert text in result["error"]["message"], result["error"]
    return result["error"]["message"]


@pytest.mark.timeout(300)
def test_a_std_string_takes_a_text_within_its_capacity(texts, cxx, capsys):
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
def test_a_const_string_is_never_changed_and_a_change_warns_of_earlier_views(texts, cxx, capsys):
    # #164 review: `const std::string cs` was changed and marked [settable] (undefined
    # behavior); a string_view taken before a change keeps the old size, unseen.
    source = texts.parent / "views.cpp"
    source.write_text(VIEWS)
    look = next(i for i, text in enumerate(VIEWS.splitlines(), 1) if "// LOOK" in text)
    at = ["probe", str(source), "--at", str(look), "--capture", "s,cs,ct,nm"]
    assert main([*at, "--json", "--quiet"]) == 0
    result = json.loads(capsys.readouterr().out)
    reads = reads_memory(result["rows"])
    # A string in its own buffer is changed where the debugger reads but does not write
    # memory (lldb-dap 20): `nm`, element by element, or with LLDB's `memory write` where
    # it misreads the buffer (GCC's); `s` is on the heap.
    elementwise = reads
    marks = {row["selector"]: row.get("settable") for row in result["rows"]}
    assert marks == {
        "s": sys.platform == "darwin",
        "cs": False,
        "ct": False,
        "nm": elementwise,
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
    if not elementwise:
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


# The evaluation's case, as the agent had it.
STOCK = """#include <cstdio>
#include <map>
#include <string>

// Parses "name:qty,name:qty,..." and prints the total per name, sorted by name.
static std::string trim_left(const std::string &s) {
    size_t i = 0;
    while (i < s.size() && s[i] == ' ') i++;
    return s.substr(i);
}

int main(int argc, char **argv) {
    std::string input = argc > 1 ? argv[1] : "";
    std::map<std::string, long> stock;
    size_t pos = 0;
    while (pos <= input.size()) {
        size_t comma = input.find(',', pos);
        if (comma == std::string::npos) comma = input.size();
        std::string entry = input.substr(pos, comma - pos);
        size_t colon = entry.find(':');
        if (colon != std::string::npos) {
            std::string name = trim_left(entry.substr(0, colon));
            long qty = std::stol(entry.substr(colon + 1));
            stock[name] += qty;
        }
        pos = comma + 1;
    }
    std::string out;
    for (const auto &item : stock) {
        if (!out.empty()) out += ",";
        out += item.first + "=" + std::to_string(item.second);
    }
    std::printf("STOCK:%s\\n", out.c_str());
    return 0;
}
"""


@pytest.fixture
def cpp(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))

    def write(name, text):
        (tmp_path / name).write_text(text)
        return tmp_path / name

    return write


def line_of(text, marker):
    return next(i for i, line in enumerate(text.splitlines(), 1) if marker in line)


@pytest.mark.timeout(240)
def test_a_name_set_to_its_trimmed_text_fixes_the_totals(cpp, cxx, capsys):
    # #137 part 2 (sixth evaluation, `stock`): the names with a trailing space were the
    # bug; setting `name` to the trimmed text, which a string's change now does, shows
    # that trimming them is the fix, without changing the parser's offsets.
    source = cpp("stock.cpp", STOCK)
    qty = line_of(STOCK, "long qty = ")
    add = line_of(STOCK, "stock[name] += qty")
    sets = ["--set-at", f"{qty}@3", 'name="apple"', "--set-at", f"{qty}@4", 'name="pear"']
    argv = ["probe", str(source), "--at", str(add), "--capture", "name", "--hits", "all"]
    code = main([*argv, *sets, "--json", "--quiet", "--", "apple:3, pear:2, apple :4,pear :1"])
    result = json.loads(capsys.readouterr().out)
    reads = sys.platform == "darwin" or any(row.get("view") == "memory" for row in result["rows"])
    # A GCC build's names read whole from LLDB's summary, with no memory read to see.
    reads = reads or bool(os.environ.get("PEEKY_EXPECT_READ_MEMORY"))
    if not reads:
        # lldb-dap 18 cannot tell whether a libstdc++ string's text is in the string.
        assert code == 1 and "cannot read memory" in result["error"]["message"], result
        return
    assert code == 0, result.get("error")
    assert "STOCK:apple=7,pear=3" in result["output"], result["output"]
    assert [
        (change["before"], change["after"], change["line"], change["hit"])
        for change in result["interventions"]
    ] == [('"apple "', '"apple"', qty, 3), ('"pear "', '"pear"', qty, 4)]
