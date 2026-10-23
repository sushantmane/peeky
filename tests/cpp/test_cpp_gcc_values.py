"""C++ built by GCC (#167), on real builds with clang and g++: what LLDB misreads in
GCC's debug information is withheld with a true note or said, never shown as the
program's. Each case reads correctly with clang (libstdc++ on Linux, libc++ on macOS).

- Unordered containers: LLDB's libstdc++ formatter finds no element type in GCC's
  `std::allocator` (nor behind a typedef's name), so it counts each as empty. Peeky
  checks its count against the container's own `_M_element_count`: where they differ,
  no preview, key or position read, or listing. Where they agree, the entries are listed
  only where the formatter decodes them as the type the container holds, where it holds
  them: an allocator whose first template argument is another type, a 16-byte-aligned
  `long double`, or (LLDB 18) a map whose type's name holds `set` decode wrongly.
- A `std::string` set: LLDB reads GCC's `_M_local_buf` (`char[16]`) as `char[0]`; its
  length comes from the build's debug information, and a refusal says what LLDB misreads.
- LLDB 18's errors about that debug information come on its own stderr, apart from the
  program's output: they are a warning, not output.
- A structured binding of a bitfield, which GCC gives no location: a true note.

GCC is refused for C++ targets; the g++ cases run only under the test-only hatch
(PEEKY_TEST_CPP_GCC=1, tests/cpp_gcc_survey.py), else they skip. lldb-dap 18 or 20, as
the lane sets it."""

import json
import os
import re
import shutil
import subprocess
import sys

import cpp_gcc_survey
import pytest

from peeky.cli import main


def _is_gcc(compiler):
    try:
        version = subprocess.run([compiler, "--version"], capture_output=True, text=True)
    except OSError:
        return False
    return "Free Software Foundation" in version.stdout


def _gxx():
    """A g++ that is GCC, where the test-only hatch lets Peeky build C++ with it."""
    if not cpp_gcc_survey.wanted():
        return None
    for name in ("g++", "g++-13", "g++-14"):
        if (found := shutil.which(name)) and _is_gcc(found):
            return found
    return None


@pytest.fixture(params=["clang", "gcc"])
def cpp(request, tmp_path, monkeypatch):
    """(the build's compiler family, a directory) with PEEKY_NATIVE_CXX set to it."""
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    compiler = os.environ["PEEKY_NATIVE_CXX"] if request.param == "clang" else _gxx()
    if compiler is None:
        pytest.skip("needs g++, and PEEKY_TEST_CPP_GCC=1: GCC is refused for C++ targets")
    monkeypatch.setenv("PEEKY_NATIVE_CXX", compiler)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    # What the build does, not the parameter's name: a lane may set PEEKY_NATIVE_CXX to g++.
    return ("gcc" if _is_gcc(compiler) else "clang"), tmp_path


def line_of(text, marker):
    return next(i for i, line in enumerate(text.splitlines(), 1) if marker in line)


def probe(source, line, capture, capsys, *extra):
    command = ["probe", str(source), "--at", str(line), "--capture", capture]
    code = main([*command, *extra, "--json", "--quiet"])
    return code, json.loads(capsys.readouterr().out)


UNORDERED = """#include <cstdio>
#include <map>
#include <unordered_map>
#include <unordered_set>
#include <vector>
struct Holder { std::unordered_set<int> us; int n; };
using Ids = std::unordered_set<long>;
static int look(const std::unordered_map<int, int> &ref, std::unordered_set<int> *ptr) {
    return (int)ref.size() + (int)ptr->size(); // IN_LOOK
}
int main() {
    std::unordered_set<int> us{4};
    std::unordered_map<int, int> um{{1, 2}};
    std::unordered_set<int> none;
    std::unordered_multiset<int> ums{1, 1};
    std::unordered_multimap<int, int> umm{{1, 2}, {1, 3}};
    Holder h{{7}, 2};
    std::vector<std::unordered_set<int>> vs{{1}, {3}};
    std::map<int, std::unordered_set<int>> mus{{1, {5}}};
    Ids ids{9};
    std::unordered_set<int> *p = &us;
    int t = look(um, p); // HERE
    std::printf("SIZES:%zu %zu %zu %zu %zu %zu %zu %zu %d\\n", us.size(), um.size(), none.size(),
                ums.size(), umm.size(), vs.size(), mus.size(), ids.size(), t + h.n);
    return 0;
}
"""
# Each selector's value, and the own count of each unordered container it reads (its
# `_M_element_count`), which a withheld row names.
READS = {
    "us": ("size=1 {4}", 1),
    "um": ("size=1 {1: 2}", 1),
    "ums": ("size=2 {1, 1}", 2),
    "umm": ("size=2", 2),
    "ids": ("size=1 {9}", 1),
    "p[0]": ("size=1 {4}", 1),
    "um[1]": ("2", 1),
    "us[#0]": ("4", 1),
    "umm[#1].first": ("1", 2),
    "ids[#0]": ("9", 1),
    "h.us": ("size=1 {7}", 1),
    "h.us[#0]": ("7", 1),
    "vs[1]": ("size=1 {3}", 1),
    "vs[1][#0]": ("3", 1),
    "mus[1]": ("size=1 {5}", 1),
    "mus[1][#0]": ("5", 1),
}
# Previews holding an unordered container: as read, or with `<not listed>` in its place.
HOLDING = {
    "h": "{us = size=1 {7}, n = 2}",
    "vs": "size=2 [size=1 {1}, size=1 {3}]",
    "mus": "size=1 {1: size=1 {5}}",
}
MISCOUNTED = (
    "the debugger cannot list this build's unordered containers: it counts 0 entries, but "
    "the container's own count (`_M_element_count`) is {}; print its entries from the program"
)


def withheld(row, count):
    """Whether a row is withheld as an unordered container LLDB miscounts: the container
    itself (`not listed: ...`), or a key, position or member read in it."""
    error = str(row.get("error") or "")
    reason = MISCOUNTED.format(count)
    if row.get("value") is not None or not error.endswith(reason):
        return False
    pointed = f"not listed: it points at an unordered container, and {reason}"
    return error in (f"not listed: {reason}", pointed) or re.fullmatch(
        r"\[[^\]]+\] not read: [\w\[\]#.]+ is not listed: .+", error
    )


@pytest.mark.timeout(180)
def test_an_unordered_container_is_read_whole_or_not_listed(cpp, capsys):
    # #167: with GCC, `us{4}` read `size=0 {}`, `um[1]` "um is empty; in C++, um[1] would
    # insert the key", and `us[#0]` "us has 0 entries", with no note.
    compiler, root = cpp
    source = root / "unordered.cpp"
    source.write_text(UNORDERED)
    capture = ",".join(["none", "p", *READS, *HOLDING])
    code, result = probe(source, line_of(UNORDERED, "// HERE"), capture, capsys)
    assert "SIZES:1 1 0 2 2 2 1 1 " in result["output"], result
    rows = {row["selector"]: row for row in result["rows"]}
    # An empty one's counts agree, whatever the compiler.
    assert rows["none"]["value"] == "size=0 {}", rows["none"]
    shown = {}
    for selector, (value, count) in READS.items():
        row = rows[selector]
        shown[selector] = row.get("value") == value
        assert shown[selector] or withheld(row, count), (selector, row)
    for selector, value in HOLDING.items():
        row = rows[selector]
        hidden = re.sub(r"size=1 \{\d\}", "<not listed>", value)
        assert row.get("value") in (value, hidden), (selector, row)
    pointer = rows["p"]
    assert re.fullmatch(r"0x[0-9a-f]+ size=1", str(pointer.get("value"))) or (
        withheld(pointer, 1) and pointer["error"].startswith("not listed: it points at an")
    ), pointer
    if compiler == "gcc":
        # LLDB counts every one as empty: none reads, and none says it is empty.
        assert not any(shown.values()), shown
        assert rows["h"]["value"] == "{us = <not listed>, n = 2}", rows["h"]
    else:
        # Clang's debug information names the element types: all read, save a typedef's,
        # which LLDB 18's formatter does not look through (LLDB 20's does).
        assert all(read for selector, read in shown.items() if "ids" not in selector), shown
        assert rows["h"]["value"] == HOLDING["h"] and rows["vs"]["value"] == HOLDING["vs"]
    # The evidence keeps what the rows showed: no value, the same note.
    assert main(["sql", "SELECT selector, value, error FROM last_run", "--json"]) == 0
    stored = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    for selector in READS:
        if not shown[selector]:
            assert stored[selector]["value"] is None, stored[selector]
            assert stored[selector]["error"] == rows[selector]["error"]


@pytest.mark.timeout(180)
def test_a_listing_and_a_callers_reference_follow_the_same_rule(cpp, capsys):
    compiler, root = cpp
    source = root / "unordered.cpp"
    source.write_text(UNORDERED)
    assert main(["session", "start", str(source), "--at", str(line_of(UNORDERED, "IN_LOOK"))]) == 0
    capsys.readouterr()
    try:
        main(["session", "get", "ref", "ptr", "--json"])
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
        main(["session", "get", "um", "--frame", "1", "--json"])
        rows.update({row["selector"]: row for row in json.loads(capsys.readouterr().out)})
    finally:
        main(["session", "close"])
        capsys.readouterr()
    if compiler == "gcc":
        for selector in ("ref", "ptr"):
            assert withheld(rows[selector], 1) and "children" not in rows[selector], rows
    else:
        assert re.fullmatch(r"0x[0-9a-f]+ size=1 \{1: 2\}", rows["ref"]["value"]), rows["ref"]
        listed = {c["selector"]: c["value"] for c in rows["ref"]["children"]}
        assert listed == {"ref[1]": "2"}, listed
    # A caller's `um` may be withheld for its own line (not finished); never miscounted.
    assert rows["um"].get("value") != "size=0 {}", rows["um"]


DECODED = """#include <cstddef>
#include <cstdio>
#include <memory>
#include <unordered_map>
#include <unordered_set>
// A conforming allocator whose first template argument is not its value type.
template <class T, class V> struct WeirdAlloc {
    using value_type = V;
    WeirdAlloc() = default;
    template <class U, class W> WeirdAlloc(const WeirdAlloc<U, W> &) {}
    template <class U> struct rebind { using other = WeirdAlloc<T, U>; };
    V *allocate(std::size_t n) { return std::allocator<V>().allocate(n); }
    void deallocate(V *p, std::size_t n) { std::allocator<V>().deallocate(p, n); }
    bool operator==(const WeirdAlloc &) const { return true; }
    bool operator!=(const WeirdAlloc &) const { return false; }
};
struct Asset { int v; };
int main() {
    std::unordered_set<int, std::hash<int>, std::equal_to<int>, WeirdAlloc<double, int>> ws{7};
    std::unordered_set<long double> ld{1.5L};
    std::unordered_map<int, Asset> assets{{1, {5}}};
    std::printf("DECODED:%zu %zu %zu\\n", ws.size(), ld.size(), assets.size()); // HERE
    return 0;
}
"""
ANOTHER_TYPE = (
    "the debugger would read its entries as another type than they are: it takes their type "
    "from the allocator's first template argument, not the container's own; print its "
    "entries from the program"
)
WRONG_PLACE = (
    "the debugger would read its entries from the wrong place: their type is aligned past "
    "where it reads in each node; print its entries from the program"
)


@pytest.mark.timeout(180)
def test_entries_lldb_would_decode_as_another_type_or_place_are_not_listed(cpp, capsys):
    # Review of #281: `WeirdAlloc<double, int>` compiles, LLDB counts `size=1` as the
    # container does, and shows `ws[#0]` as the double 3.4584595208887258E-323.
    compiler, root = cpp
    source = root / "decoded.cpp"
    source.write_text(DECODED)
    capture = "ws,ws[#0],ld,ld[#0],assets,assets[1].v"
    code, result = probe(source, line_of(DECODED, "// HERE"), capture, capsys)
    assert "DECODED:1 1 1" in result["output"], result
    rows = {row["selector"]: row for row in result["rows"]}

    def read(selector, value, *reasons):
        """The value, or withheld for one of ``reasons``: never another value."""
        row = rows[selector]
        if row.get("value") is not None:
            assert row["value"] == value, (selector, row)
            return True
        prefix = "not listed: " if "[" not in selector else ""
        assert any(row["error"].endswith(prefix + reason) for reason in reasons), (selector, row)
        return False

    # Never a double for an int. libc++'s formatter (macOS) reads it right.
    if not read("ws", "size=1 {7}", ANOTHER_TYPE):
        assert rows["ws[#0]"]["error"] == f"[#0] not read: ws is not listed: {ANOTHER_TYPE}"
    read("ws[#0]", "7", ANOTHER_TYPE)
    # A `long double` is 16-byte aligned, past the 8 bytes LLDB's libstdc++ provider skips:
    # clang's read `size=1 {0}`. GCC's is counted empty first.
    if not read("ld", "size=1 {1.5}", WRONG_PLACE, MISCOUNTED.format(1)):
        assert rows["ld"]["error"].endswith(WRONG_PLACE) == (compiler == "clang"), rows["ld"]
    read("ld[#0]", "1.5", WRONG_PLACE, MISCOUNTED.format(1))
    # LLDB 18 takes `std::unordered_map<int, Asset>` for a set (its name holds `set`), and
    # decodes its pairs as `int`s; LLDB 20 reads clang's. GCC's may be counted empty.
    reasons = [ANOTHER_TYPE] + ([MISCOUNTED.format(1)] if compiler == "gcc" else [])
    if rows["assets"].get("value") is not None:
        assert rows["assets"]["value"].startswith("size=1 {1: {"), rows["assets"]
        assert rows["assets[1].v"]["value"] == "5", rows["assets[1].v"]
    else:
        error = rows["assets"]["error"]
        assert error in [f"not listed: {reason}" for reason in reasons], rows["assets"]
        reason = error.removeprefix("not listed: ")
        assert rows["assets[1].v"]["error"] == f"[1] not read: assets is not listed: {reason}"
    if sys.platform == "linux":
        # libstdc++ here: LLDB's provider decodes `ws` and `ld` wrongly, so neither reads.
        assert rows["ws"].get("value") is None and rows["ld"].get("value") is None, rows


TEXT = """#include <cstdio>
#include <string>
int main() {
  std::string nm = "named";
  std::fprintf(stderr, "error: fake 0x000000fc: DW_TAG_member 'x' refers to type 0x1\\n");
  std::printf("NM:[%s]\\n", nm.c_str()); // OBSERVE
  return 0;
}
"""
# LLDB 18's error about GCC's `std::string`, which lldb-dap sends on its own stderr.
DWARF = re.compile(
    r"error: \S+ 0x[0-9a-f]{8}: DW_TAG_member '_M_local_buf' refers to type 0x[0-9a-f]+ "
    r"which extends beyond the bounds of 0x[0-9a-f]+"
)


@pytest.mark.timeout(180)
def test_a_string_set_is_made_or_refused_for_what_the_debugger_misreads(cpp, capsys):
    # #167: with lldb-dap 20, GCC's string was refused as "its members are not laid out
    # as libstdc++'s string's are"; LLDB reads its `_M_local_buf` as `char[0]`.
    compiler, root = cpp
    source = root / "text.cpp"
    source.write_text(TEXT)
    line = line_of(TEXT, "// OBSERVE")
    code, result = probe(source, line, "nm", capsys, "--set", 'nm="renamed"')
    if code == 0:
        assert "NM:[renamed]" in result["output"], result
        return
    error = result["error"]
    assert error["code"] == "MUTATION_REJECTED", error
    assert "not laid out" not in error["message"], error
    misread = (
        "`nm` cannot be changed: the debugger misreads this build's string buffer as char[0] "
        "(its debug information says char[16]), so it cannot set the text's characters one "
        "by one, and this debugger cannot write memory (lldb-dap 21 can)"
    )
    unread = "this debugger cannot read memory (lldb-dap 20 can)"
    assert error["message"].startswith(misread) or unread in error["message"], error
    if compiler == "clang" or not os.environ.get("PEEKY_EXPECT_READ_MEMORY"):
        assert unread in error["message"], error
    else:
        assert error["message"].startswith(misread), error
    # Never marked as one a set changes.
    assert result["rows"][0].get("settable") is not True, result["rows"]


@pytest.mark.timeout(180)
def test_the_debuggers_errors_about_the_debug_information_are_not_program_output(cpp, capsys):
    # #167: LLDB 18 printed `error: program 0x000000fc: DW_TAG_member '_M_local_buf' ...`
    # into the program's output, as if the program had printed it.
    compiler, root = cpp
    source = root / "text.cpp"
    source.write_text(TEXT)
    code, result = probe(source, line_of(TEXT, "// OBSERVE"), "nm", capsys)
    assert code == 0, result.get("error")
    output = result["output"] + result.get("output_after_disconnect", "")
    assert "NM:[named]" in output and not DWARF.search(output), output
    # The program's own line that looks like one stays its output.
    assert "error: fake 0x000000fc: DW_TAG_member 'x' refers to type 0x1" in output, output
    diagnostics = result.get("debugger_diagnostics") or []
    warnings = [w for w in result["warnings"] if "debugger_diagnostics in --json" in w]
    if diagnostics:
        assert all(DWARF.fullmatch(line) for line in diagnostics), diagnostics
        assert len(warnings) == 1 and warnings[0].startswith("A `stderr` line with the shape of")
        assert (
            "a program line that looks exactly like one would be moved too: error: "
            in (warnings[0])
        )
    else:
        assert not warnings, result["warnings"]
    if compiler == "clang":
        assert not diagnostics


BITS = """#include <cstdio>
struct Bits {
    int lo : 3;
    unsigned short hi : 5;
};
int main() {
    Bits bits{1, 2};
    auto [lo, hi] = bits;
    int plain = 7;
    std::printf("BITS:%d %d %d\\n", lo, hi, plain); // OBSERVE
    return 0;
}
"""


@pytest.mark.timeout(180)
def test_a_bitfields_binding_with_no_location_says_so(cpp, capsys):
    # #167: GCC describes `auto [lo, hi] = bits` with no location and a 1-byte
    # `__unknown__` type; a set was refused with "stop after its declaration has run".
    compiler, root = cpp
    source = root / "bits.cpp"
    source.write_text(BITS)
    line = line_of(BITS, "// OBSERVE")
    assert main(["session", "start", str(source), "--at", str(line)]) == 0
    capsys.readouterr()
    try:
        main(["session", "get", "hi", "lo", "plain", "--json"])
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
        assert main(["session", "set", "hi=17"]) != 0
        refused = capsys.readouterr().err
    finally:
        main(["session", "close"])
        capsys.readouterr()
    assert rows["plain"]["value"] == "7", rows["plain"]
    assert "stop after its declaration" not in refused, refused
    if compiler == "gcc":
        note = (
            "the build gives it no location and no type of its own (`__unknown__`), as GCC "
            "does for a structured binding of a bitfield, so the debugger can neither read "
            "nor change it; print it from the program"
        )
        for name in ("hi", "lo"):
            assert rows[name]["error"] == f"not readable: {note}", rows[name]
        assert f"[MUTATION_REJECTED] 'hi' cannot be changed: {note}" in refused, refused
        assert "hint: Print it from the program with a candidate edit" in refused, refused
    else:
        # Clang before 19 describes no binding of a bitfield; 19 and later read them.
        assert rows["hi"].get("value") in (None, "2"), rows["hi"]
