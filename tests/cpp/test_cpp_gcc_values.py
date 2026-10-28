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
- A structured binding of a pair or tuple, which GCC (and Apple clang 21) hold as a
  reference to the element: the element, never its address (#291). By reference, only
  where it names an object the frame keeps alive, an element of a local container the
  code leaves in place, or a call's own result (#297); optimized, never on its own line.

The g++ cases skip where no g++ is GCC (macOS), and the clang cases where the lane builds
C++ with g++. lldb-dap 18 or 20, as the lane sets it."""

import json
import os
import re
import sys

import pytest
from native_compilers import cxx_for, is_gcc

from peeky.cli import main


@pytest.fixture(params=["clang", "gcc"])
def cpp(request, tmp_path, monkeypatch):
    """(the build's compiler family, a directory) with PEEKY_NATIVE_CXX set to it."""
    compiler, why = cxx_for(request.param)
    if compiler is None:
        pytest.skip(why)
    monkeypatch.setenv("PEEKY_NATIVE_CXX", compiler)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return ("gcc" if is_gcc(compiler) else "clang"), tmp_path


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


TUPLES = """#include <cstdio>
#include <string>
#include <tuple>
#include <utility>
#include <vector>
struct Pt { long x; long y; };
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; }
__attribute__((noinline)) std::vector<std::pair<long, long>> make(long v) {
    return {{v + 1000, 1}, {v + 1001, 2}};
}
int main(int argc, char **argv) {
    long t = argc + 1; // OUTER
    for (long i = 0; i < t; i++) {
        auto [t, u] = make(i)[0]; // BY_VALUE
        use(t + u); // VALUE_READ
        std::pair<long, long> held = make(i)[1];
        auto &[r, s] = held; // BY_REFERENCE
        use(r * s); // REFERENCE_READ
    }
    for (long i = 0; i < t; i++) { auto [t, u] = make(i)[0]; use(t + u); } // ONE_LINE
    std::pair<Pt, std::string> both{{3, 4}, "apple"};
    std::tuple<long, double, char> three{7, 8.5, 'c'};
    auto &[pt, name] = both;
    const auto &[a, b, c] = three;
    use(pt.x + (long)name.size() + a + (long)b + c); // KINDS
    return 0;
}
"""


def hidden(compiler, row):
    """Whether a row is clang 18's for a binding: clang 18 marks the variable holding the
    reference artificial, and lldb-dap does not list it (`t` is then main's own, which
    the code there does not read). A newer clang's (Apple clang 21) is a plain reference
    (`long &&`), read as GCC's is."""
    return (
        compiler == "clang"
        and row.get("value") is None
        and row["error"].startswith(
            ("not found", "not what the code reads: the code here names the structured binding")
        )
    )


def bound_rows(source, marker, capture, capsys):
    """The rows of ``capture`` at every stop of ``marker``'s line of TUPLES."""
    code, result = probe(source, line_of(TUPLES, f"// {marker}"), capture, capsys, "--hits", "all")
    assert result["status"] == "completed", result
    return result["rows"]


@pytest.mark.timeout(300)
def test_a_pair_or_tuple_binding_shows_its_element_never_its_address(cpp, capsys):
    # #291: GCC holds `t` of `auto [t, u] = make(i)[0];` as a reference to the element
    # (`std::tuple_element<0, std::pair<long, long> >::type &&`), and every row showed
    # the element's address (`0x0000fffffffffb70`) as its value: before the declaration
    # ran (with a note at the loop's next pass, none on a one-line loop), and after. So
    # did Apple clang 21's (`long &&`) after it ran.
    compiler, root = cpp
    source = root / "tuples.cpp"
    source.write_text(TUPLES)
    declared = {
        "BY_VALUE": line_of(TUPLES, "// BY_VALUE"),
        "BY_REFERENCE": line_of(TUPLES, "// BY_REFERENCE"),
    }
    for marker, line in declared.items():
        found = bound_rows(source, marker, "t,u" if marker == "BY_VALUE" else "r,s", capsys)
        assert len(found) == 4, found
        for row in found:
            # Before its declaration runs: never a value, nor a note on a value.
            assert row.get("value") is None and "note" not in row, row
            if hidden(compiler, row):
                continue
            assert row["error"].startswith(f"no value yet: declared on line {line}"), row
            if "runs after this stop on every pass" in row["error"]:
                # GCC's loop returns to it: the reference may be the last pass's.
                assert row["error"].endswith(
                    "the build reads this structured binding through a reference its "
                    "declaration sets, which may point anywhere until then; read it after "
                    "its declaration"
                ), row
    reads = {
        "VALUE_READ": ("t,u", lambda i: {"t": str(1000 + i), "u": "1"}, " &&"),
        "REFERENCE_READ": ("r,s", lambda i: {"r": str(1001 + i), "s": "2"}, " &"),
    }
    for marker, (capture, expected, reference) in reads.items():
        found = bound_rows(source, marker, capture, capsys)
        assert len(found) == 4, found
        for row in found:
            if hidden(compiler, row):
                continue
            # The element, as the program reads it, and its type as the binding declares
            # it; LLDB's, of the reference, beside it.
            assert row["value"] == expected(row["hit"] - 1)[row["selector"]], row
            assert row["debugger_type"] == row["type"] + reference, row
            assert row["type"].startswith("std::tuple_element<") or compiler == "clang", row
            assert row.get("settable") is not True, row
    # A one-line loop: its first stop shows the outer `t` withheld; the next ones, the
    # binding before its declaration runs. No stop shows a value.
    for row in bound_rows(source, "ONE_LINE", "t", capsys):
        assert row.get("value") is None and "note" not in row, row
    # A struct, a string and a tuple's number, character and double: each the element.
    kinds = line_of(TUPLES, "// KINDS")
    code, result = probe(source, kinds, "pt,pt.x,name,a,b,c", capsys, "--hits", "1")
    rows = {row["selector"]: row for row in result["rows"]}
    elements = {
        "pt": "{x = 3, y = 4}",
        "pt.x": "3",
        "name": '"apple"',
        "a": "7",
        "b": "8.5",
        "c": "'c'",
    }
    for selector, row in rows.items():
        assert hidden(compiler, row) or row.get("value") == elements[selector], row
    assert compiler == "clang" or code == 0, result
    assert rows["pt.x"].get("settable") is not True, rows["pt.x"]
    # Never changed: a change would write through the reference.
    code, result = probe(source, kinds, "a", capsys, "--set", "a=9")
    assert code != 0 and result["error"]["code"] == "MUTATION_REJECTED", result
    if compiler == "gcc":
        assert rows["pt.x"].get("not_settable") == "reference", rows["pt.x"]
        message = result["error"]["message"]
        assert "is reached through a pointer or reference" in message, result
        assert "Refused before the program runs" in message, result


# Review of #296: bindings by reference that outlive what they refer to, and ones that
# do not; a reference the code declares with the element's type.
DANGLING = """#include <cstdio>
#include <tuple>
#include <utility>
#include <vector>
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; }
__attribute__((noinline)) std::vector<std::pair<long, long>> make(long v) {
    return {{v + 1000, 1}, {v + 1001, 2}};
}
__attribute__((noinline)) std::pair<long, long> one(long v) { return {v + 30, v + 40}; }
int main(int argc, char **argv) {
    auto &[t, u] = make(0)[0]; // REF
    const auto &[c, d] = make(0)[1]; // CONST_REF
    auto &&[e, f] = make(0)[0]; // FORWARD
    auto poison = make(7000);
    std::pair<long, long> p{argc + 4, argc + 5};
    std::tuple_element<0, std::pair<long, long>>::type &r = p.first; // EXPLICIT
    auto &[a, b] = p; // LOCAL
    auto &&[g, h] = one(argc); // EXTENDED
    use(poison[0].first + r + a); use(g + h); // AFTER
    return 0;
}
"""
DEAD = (
    "not shown: the binding refers to an object Peeky can't prove is still alive: `{}` binds "
    "`{}` by reference, and Peeky proves only a local or its non-union member, or a call's "
    "result. Read it where the code names that object, or bind by value (no `&`)"
)


@pytest.mark.timeout(180)
def test_a_binding_by_reference_reads_only_what_this_frame_keeps_alive(cpp, capsys):
    # Review of #296: `auto &[t, u] = make(0)[0];` refers into a temporary the line
    # destroyed, and `auto poison = make(7000);` took its memory: `t = 8000, u = 1`, no note.
    compiler, root = cpp
    source = root / "dangling.cpp"
    source.write_text(DANGLING)
    code, result = probe(
        source, line_of(DANGLING, "// AFTER"), "t,u,c,d,e,f,r,a,b,g,h", capsys, "--hits", "1"
    )
    assert "U=8010" in result["output"], result["output"]
    rows = {row["selector"]: row for row in result["rows"]}
    # A local of this frame: read through. So is `one(argc)`'s result, which `auto &&`
    # keeps alive for its scope (#297).
    for selector, value in (("a", "5"), ("b", "6"), ("g", "31"), ("h", "41")):
        assert hidden(compiler, rows[selector]) or rows[selector].get("value") == value, rows
    # Never `poison`'s elements, nor any value, through a temporary the line destroyed.
    bound = {
        "t": "make(0)[0]",
        "u": "make(0)[0]",
        "c": "make(0)[1]",
        "d": "make(0)[1]",
        "e": "make(0)[0]",
        "f": "make(0)[0]",
    }
    for selector, initializer in bound.items():
        row = rows[selector]
        assert row.get("value") is None, row
        if not hidden(compiler, row):
            assert row["error"] == DEAD.format(selector, initializer), row
    # The code's own reference, though GCC types it as a binding's: a reference's row.
    assert rows["r"]["type"].endswith("&") and "debugger_type" not in rows["r"], rows["r"]
    assert re.fullmatch(r"0x[0-9a-f]+", rows["r"]["value"]), rows["r"]


# Round 2 of the #296 review: a union's member, whose life ends when another becomes
# active, and the boundaries checked by hand: a local reassigned, a vector's element, and a
# map's entries in a range `for` (read since #297: the code leaves both in place).
BOUNDARIES = """#include <cstdio>
#include <map>
#include <new>
#include <utility>
#include <vector>
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; }
union U {
    std::pair<long, long> p;
    std::pair<long, long> q;
    U() : p{1, 2} {}
    ~U() {}
};
struct Holder { U u; std::pair<long, long> inner; };
int main(int argc, char **argv) {
    U u;
    auto &[a, b] = u.p; // UNION
    u.p.~pair();
    new (&u.q) std::pair<long, long>{7, 8};
    Holder h;
    h.inner = {31, 32};
    auto &[x, y] = h.u.p; // NESTED_UNION
    auto &[c, d] = h.inner; // MEMBER
    std::pair<long, long> p{argc + 4, argc + 5};
    auto &[e, f] = p; // REASSIGNED
    p = std::pair<long, long>{argc + 40, argc + 50};
    std::vector<std::pair<long, long>> v{{11, 12}};
    auto &[g, k] = v[0]; // SUBSCRIPT
    std::map<long, long> m{{21, 22}, {23, 24}};
    for (auto &[key, value] : m) { // MAP_LOOP
        use(key * value); // MAP_READ
    }
    use(u.q.first + c + e); use(g); // AFTER
    return 0;
}
"""


@pytest.mark.timeout(180)
def test_a_binding_through_a_union_is_withheld_a_local_containers_element_read(cpp, capsys):
    # Round 2 of the #296 review: `auto &[a, b] = u.p;`, then a placement new made `u.q`
    # the union's active member: `a = 7, b = 8`, with no note.
    compiler, root = cpp
    source = root / "boundaries.cpp"
    source.write_text(BOUNDARIES)
    capture = "a,b,x,y,c,d,e,f,g,k"
    code, result = probe(source, line_of(BOUNDARIES, "// AFTER"), capture, capsys, "--hits", "1")
    assert "U=79" in result["output"], result["output"]
    rows = {row["selector"]: row for row in result["rows"]}
    # A struct's member, and a local reassigned after the binding: what it holds now. And
    # since #297, an element of a local vector nothing changes later: `v[0]`.
    shown = {"c": "31", "d": "32", "e": "41", "f": "51", "g": "11", "k": "12"}
    for selector, value in shown.items():
        assert hidden(compiler, rows[selector]) or rows[selector].get("value") == value, rows
    withheld = {"a": "u.p", "b": "u.p", "x": "h.u.p", "y": "h.u.p"}
    for selector, initializer in withheld.items():
        row = rows[selector]
        assert row.get("value") is None, row
        assert hidden(compiler, row) or row["error"] == DEAD.format(selector, initializer), row
    # A map's entry in a range `for` whose body leaves the map alone (#297): each pass's.
    code, result = probe(
        source, line_of(BOUNDARIES, "// MAP_READ"), "key,value", capsys, "--hits", "all"
    )
    assert len(result["rows"]) == 4, result["rows"]
    entries = {(1, "key"): "21", (1, "value"): "22", (2, "key"): "23", (2, "value"): "24"}
    for row in result["rows"]:
        expected = entries[(row["hit"], row["selector"])]
        assert hidden(compiler, row) or row.get("value") == expected, row


# #297: bindings by reference of an element of a local container the code leaves in place
# (a range `for` over a map, a vector or a set; `v[1]`), and of a call's own result, which
# the binding keeps alive; and the same forms where the code may change the container, or
# the call returns a reference.
HELD = """#include <cstdio>
#include <map>
#include <set>
#include <utility>
#include <vector>
using P = std::pair<long, long>;
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; }
__attribute__((noinline)) P one(long v) { return {v + 30, v + 40}; }
__attribute__((noinline)) const P &pick(const P &p) { return p; }
__attribute__((noinline)) void fill(std::map<long, long> &m) { m[99] = 100; }
int main(int argc, char **argv) {
    std::map<long, long> m{{21, 22}, {23, 24}};
    for (const auto &[k, x] : m) {
        use(k * x); use((long)m.size()); // MAP_READ
    }
    std::vector<P> v{{11, 12}, {13, 14}};
    for (auto &[a, b] : v) {
        b += 1;
        use(a * b); // VECTOR_READ
    }
    std::set<P> ps{{5, 6}, {7, 8}};
    for (const auto &[i, j] : ps) {
        use(i * j); // SET_READ
    }
    auto &[r, s] = v[1];
    auto &&[p, q] = one(argc);
    const auto &[y, z] = one(argc + 1);
    use(r + s + p + q + y + z); // READ
    std::vector<P> w{{51, 52}};
    auto &[c, d] = w[0];
    long first = c;
    w.push_back({53, 54});
    w.push_back({55, 56});
    auto &&[e, f] = pick(w[1]);
    std::map<long, long> n{{61, 62}};
    fill(n);
    for (auto &[g, h] : n) {
        use(g + h + first + e); // CHANGED
    }
    return 0;
}
"""
HELD_ROWS = {
    "MAP_READ": ("k,x", {1: {"k": "21", "x": "22"}, 2: {"k": "23", "x": "24"}}),
    "VECTOR_READ": ("a,b", {1: {"a": "11", "b": "13"}, 2: {"a": "13", "b": "15"}}),
    "SET_READ": ("i,j", {1: {"i": "5", "j": "6"}, 2: {"i": "7", "j": "8"}}),
    "READ": (
        "r,s,p,q,y,z",
        {1: {"r": "13", "s": "15", "p": "31", "q": "41", "y": "32", "z": "42"}},
    ),
}
UNPROVEN = (
    "not shown: the binding refers to an object Peeky can't prove is still alive: `{}` binds "
    "{} by reference, {}. Read it where the code names that object, or bind by value (no `&`)"
)


@pytest.mark.timeout(300)
def test_a_binding_into_a_local_container_or_a_calls_result_reads_where_proven(cpp, capsys):
    # #297: these were all `not shown` (only a local, or a member of one, was proven), under
    # GCC and Apple clang alike: a range `for` over a local map, `v[1]` of a local vector,
    # and `auto &&[p, q] = one(argc);`, whose result C++ keeps alive for the scope.
    compiler, root = cpp
    source = root / "held.cpp"
    source.write_text(HELD)
    for marker, (capture, passes) in HELD_ROWS.items():
        found = bound_rows_of(source, HELD, marker, capture, capsys)
        assert len(found) == sum(len(each) for each in passes.values()), found
        for row in found:
            if hidden(compiler, row):
                continue
            assert row.get("value") == passes[row["hit"]][row["selector"]], row
            assert row.get("settable") is not True and "debugger_type" in row, row
    # Where the code may change the container, or the call returns a reference: withheld.
    changed = {
        "c": (
            "an element of `w`",
            "and while it is in scope the code may change `w` or keep a pointer or reference "
            "into it",
        ),
        "e": (
            "`pick(w[1])`",
            "and Peeky can't prove the call returns a new standard pair, tuple or array of "
            "numbers that the binding keeps alive",
        ),
        "g": (
            "an element of `n`",
            "and while it is in scope the code may change `n` or keep a pointer or reference "
            "into it",
        ),
    }
    code, result = probe(source, line_of(HELD, "// CHANGED"), "c,d,e,f,g,h", capsys, "--hits", "1")
    assert "U=" in result["output"], result
    for row in result["rows"]:
        assert row.get("value") is None, row
        if hidden(compiler, row):
            continue
        what, why = changed[{"d": "c", "f": "e", "h": "g"}.get(row["selector"], row["selector"])]
        assert row["error"] == UNPROVEN.format(row["selector"], what, why), row


# Review of #368: a pointer into the container made before the binding, through which the
# element is destroyed before the stop and made again after it; a tuple-like type of the
# program's whose `get<I>` returns what a pointer in it points at; and names passed by value.
REVIEWED = """#include <cstddef>
#include <cstdio>
#include <map>
#include <memory>
#include <new>
#include <tuple>
#include <utility>
#include <vector>
using P = std::pair<long, long>;
struct Pair { long x, y; };
struct View {
    Pair *p;
    template <std::size_t I> long &get() const { return I == 0 ? p->x : p->y; }
};
namespace std {
template <> struct tuple_size<View> : std::integral_constant<std::size_t, 2> {};
template <std::size_t I> struct tuple_element<I, View> { using type = long; };
}
struct Sink {};
long *escaped = nullptr;
Sink &operator<<(Sink &s, long &x) { escaped = &x; return s; }
long *made = nullptr;
struct E {
    long x, y;
    E(long a, long b) : x(a), y(b) { made = &x; }
    template <std::size_t I> long &get() { return I == 0 ? x : y; }
};
namespace std {
template <> struct tuple_size<E> : std::integral_constant<std::size_t, 2> {};
template <std::size_t I> struct tuple_element<I, E> { using type = long; };
}
void *kept = nullptr;
template <class T> struct Spy {
    using value_type = T;
    Spy() = default;
    template <class U> Spy(const Spy<U> &) {}
    T *allocate(std::size_t n) { T *p = std::allocator<T>{}.allocate(n); kept = p; return p; }
    void deallocate(T *p, std::size_t n) { std::allocator<T>{}.deallocate(p, n); }
};
template <class T, class U> bool operator==(const Spy<T> &, const Spy<U> &) { return true; }
template <class T, class U> bool operator!=(const Spy<T> &, const Spy<U> &) { return false; }
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; }
__attribute__((noinline)) View view(Pair *p) { return View{p}; }
int main(int argc, char **argv) {
    std::vector<P> v{{11, 12}};
    P *p = v.data();
    auto &[a, b] = v[0];
    std::destroy_at(p);
    use(argc); // DESTROYED
    new (p) P{13, 14};
    use(a + b);
    Pair held{41, 42};
    auto &&[vx, vy] = view(&held);
    use(vx + vy); // PROXY
    std::map<long, long> m{{71, 72}};
    for (const auto &[k, x] : m) {
        use(k);
        std::printf("%ld\\n", x); // PASSED
    }
    Sink sink;
    std::map<long, long> om{{101, 102}};
    for (auto &[ok, ov] : om) {
        sink << ov;
        use(ok); // OPERATOR
    }
    std::vector<E> es;
    es.emplace_back(111, 112);
    for (auto &[ex, ey] : es) {
        use(ex * ey); // CONSTRUCTED
    }
    std::vector<P, Spy<P>> sv{{121, 122}};
    for (auto &[sa, sb] : sv) {
        use(sa * sb); // ALLOCATED
    }
    std::map<long, long *> pointing{{131, &held.x}};
    for (auto &[pk, pp] : pointing) {
        use(pk); // POINTER
    }
    return 0;
}
"""


POLICY = (
    "and Peeky proves only standard vectors, maps and sets of numbers, with the default "
    "allocator and order"
)


@pytest.mark.timeout(300)
def test_a_binding_is_withheld_where_its_element_may_be_gone_or_another_objects(cpp, capsys):
    # Review of #368: `P *p = v.data(); auto &[a, b] = v[0]; std::destroy_at(p);` showed
    # `a = 11, b = 12` at a stop before `new (p) P{13, 14}`: the element was dead. And
    # `auto &&[vx, vy] = view(&held);` showed `held`'s members: `View`'s own `get<I>` names
    # what a pointer in the call's result points at, not that result's elements.
    compiler, root = cpp
    source = root / "reviewed.cpp"
    source.write_text(REVIEWED)
    reasons = {
        "DESTROYED": (
            "a,b",
            "an element of `v`",
            "and while it is in scope the code may change `v` or keep a pointer or reference "
            "into it",
        ),
        "PROXY": (
            "vx,vy",
            "`view(&held)`",
            "and Peeky can't prove the call returns a new standard pair, tuple or array of "
            "numbers that the binding keeps alive",
        ),
        # Review of #368, round 2: an overloaded operator that keeps the name's address;
        # an element whose own constructor keeps its address, or an allocator of the
        # program's, which sees where each element is; a pointer, which may point at
        # another element.
        "OPERATOR": (
            "ok,ov",
            "an element of `om`",
            "and while it is in scope the code may change `om` or keep a pointer or reference "
            "into it",
        ),
        "CONSTRUCTED": ("ex,ey", "an element of `es`", POLICY),
        "ALLOCATED": ("sa,sb", "an element of `sv`", POLICY),
        "POINTER": ("pk,pp", "an element of `pointing`", POLICY),
    }
    for marker, (capture, what, why) in reasons.items():
        found = bound_rows_of(source, REVIEWED, marker, capture, capsys)
        assert len(found) == 2, (marker, found)
        for row in found:
            assert row.get("value") is None, row
            assert hidden(compiler, row) or row["error"] == UNPROVEN.format(
                row["selector"], what, why
            ), row
    # Passed by value (`use(long)`, `printf`'s `...`): still read.
    for row in bound_rows_of(source, REVIEWED, "PASSED", "k,x", capsys):
        assert hidden(compiler, row) or row.get("value") == {"k": "71", "x": "72"}[row["selector"]]


def bound_rows_of(source, text, marker, capture, capsys):
    """The rows of ``capture`` at every stop of ``marker``'s line of ``text``."""
    code, result = probe(source, line_of(text, f"// {marker}"), capture, capsys, "--hits", "all")
    assert result["status"] == "completed", result
    return result["rows"]


@pytest.mark.timeout(300)
@pytest.mark.parametrize("optimization", ["O1", "O2"])
def test_an_optimized_bindings_own_line_never_reads_through_it(cpp, optimization):
    # Review of #296: optimized, a stop on a binding's own line may come before this
    # pass sets its reference, which Peeky cannot tell: no stop there reads through it.
    from peeky.native import NativeTarget
    from peeky.probe import Probe, run_probe

    compiler, root = cpp
    (root / "tuples.cpp").write_text(TUPLES)
    adapter = NativeTarget(
        sources=("tuples.cpp",),
        language="cpp",
        compiler=os.environ["PEEKY_NATIVE_CXX"],
        adapter=os.environ["PEEKY_NATIVE_DAP"],
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
        optimization=optimization,
    )
    for marker, names in (("BY_VALUE", ("t", "u")), ("BY_REFERENCE", ("r", "s"))):
        line = line_of(TUPLES, f"// {marker}")
        result = run_probe(root, Probe("tuples.cpp", line, names, hits="all"), adapter=adapter)
        assert result["status"] == "completed", result.get("error")
        for row in result["rows"]:
            assert row.get("value") is None and "note" not in row, row
            if row["error"].startswith("no value yet"):
                assert row["error"].endswith(
                    "the build reads this structured binding through a reference its "
                    "declaration sets, which may point anywhere until then; read it below "
                    "its declaration"
                ), row
    result = run_probe(
        root,
        Probe("tuples.cpp", line_of(TUPLES, "// ONE_LINE"), ("t", "u"), hits="all"),
        adapter=adapter,
    )
    assert result["status"] == "completed", result.get("error")
    assert all(row.get("value") is None for row in result["rows"]), result["rows"]


@pytest.mark.timeout(180)
@pytest.mark.parametrize("optimization", ["O1", "O2"])
def test_an_optimized_pair_binding_never_shows_its_elements_address(cpp, optimization):
    # #291: at -O1 GCC keeps the reference `r` in a register; its row was its address.
    from peeky.native import NativeTarget
    from peeky.probe import Probe, run_probe

    compiler, root = cpp
    (root / "tuples.cpp").write_text(TUPLES)
    adapter = NativeTarget(
        sources=("tuples.cpp",),
        language="cpp",
        compiler=os.environ["PEEKY_NATIVE_CXX"],
        adapter=os.environ["PEEKY_NATIVE_DAP"],
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
        optimization=optimization,
    )
    line = line_of(TUPLES, "// REFERENCE_READ")
    result = run_probe(root, Probe("tuples.cpp", line, ("r", "s"), hits="all"), adapter=adapter)
    assert result["status"] == "completed", result.get("error")
    for row in result["rows"]:
        if row.get("value") is not None:
            # The element, or nothing: never its address.
            assert row["value"] == {"r": str(1001 + row["hit"] - 1), "s": "2"}[row["selector"]]
