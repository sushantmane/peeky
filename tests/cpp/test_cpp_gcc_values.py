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
  where it names an object the frame keeps alive; optimized, never on its own line.

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
    use(poison[0].first + r + a + g); // AFTER
    return 0;
}
"""
DEAD = (
    "not shown: the binding refers to an object Peeky can't prove is still alive: `{}` binds "
    "`{}` by reference, and only an object of this frame, or a member of one, is proven. Read "
    "it where the code names that object, or bind by value (`auto [...]`, no `&`)"
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
    assert "U=8041" in result["output"], result["output"]
    rows = {row["selector"]: row for row in result["rows"]}
    # Never `poison`'s elements, nor any value, through a temporary the line destroyed;
    # nor through `one(argc)`'s, which `auto &&` keeps alive, but only by a rule Peeky
    # does not prove (a call).
    bound = {
        "t": "make(0)[0]",
        "u": "make(0)[0]",
        "c": "make(0)[1]",
        "d": "make(0)[1]",
        "e": "make(0)[0]",
        "f": "make(0)[0]",
        "g": "one(argc)",
        "h": "one(argc)",
    }
    for selector, initializer in bound.items():
        row = rows[selector]
        assert row.get("value") is None, row
        if not hidden(compiler, row):
            assert row["error"] == DEAD.format(selector, initializer), row
    # A local of this frame: read through.
    for selector, value in (("a", "5"), ("b", "6")):
        assert hidden(compiler, rows[selector]) or rows[selector]["value"] == value, rows
    # The code's own reference, though GCC types it as a binding's: a reference's row.
    assert rows["r"]["type"].endswith("&") and "debugger_type" not in rows["r"], rows["r"]
    assert re.fullmatch(r"0x[0-9a-f]+", rows["r"]["value"]), rows["r"]


# Round 2 of the #296 review: a union's member, whose life ends when another becomes
# active, and the boundaries checked by hand: a local reassigned, a vector's element, and a
# map's entries in a range `for`.
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
    use(u.q.first + c + e + g); // AFTER
    return 0;
}
"""


@pytest.mark.timeout(180)
def test_a_binding_through_a_union_a_subscript_or_a_map_is_withheld(cpp, capsys):
    # Round 2 of the #296 review: `auto &[a, b] = u.p;`, then a placement new made `u.q`
    # the union's active member: `a = 7, b = 8`, with no note.
    compiler, root = cpp
    source = root / "boundaries.cpp"
    source.write_text(BOUNDARIES)
    capture = "a,b,x,y,c,d,e,f,g,k"
    code, result = probe(source, line_of(BOUNDARIES, "// AFTER"), capture, capsys, "--hits", "1")
    assert "U=90" in result["output"], result["output"]
    rows = {row["selector"]: row for row in result["rows"]}
    withheld = {"a": "u.p", "b": "u.p", "x": "h.u.p", "y": "h.u.p", "g": "v[0]", "k": "v[0]"}
    for selector, initializer in withheld.items():
        row = rows[selector]
        assert row.get("value") is None, row
        assert hidden(compiler, row) or row["error"] == DEAD.format(selector, initializer), row
    # A struct's member, and a local reassigned after the binding: what it holds now.
    shown = {"c": "31", "d": "32", "e": "41", "f": "51"}
    for selector, value in shown.items():
        assert hidden(compiler, rows[selector]) or rows[selector]["value"] == value, rows
    # A map's entry in a range `for`: an element of `m`, which the body may erase.
    code, result = probe(
        source, line_of(BOUNDARIES, "// MAP_READ"), "key,value", capsys, "--hits", "all"
    )
    assert len(result["rows"]) == 4, result["rows"]
    for row in result["rows"]:
        assert row.get("value") is None, row
        assert hidden(compiler, row) or row["error"] == DEAD.format(row["selector"], "m").replace(
            "binds `m`", "binds an element of `m`"
        ), row


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


CAPTURES = """#include <algorithm>
#include <cstdio>
#include <vector>

struct Pt { long x, y; };

int main() {
    std::vector<long> sizes{3, 1, 2};
    int bonus = 2;
    long count = 7;
    Pt pt{3, 4};
    long *heap = new long(40);
    long &held = *heap;
    char *word = new char[5]{'h', 'e', 'a', 'p', 0};
    auto m = [bonus, &count, pt, n = count + 1]() mutable {
        bonus += 1; // IN_MUTABLE
        count += n + pt.x;
        return bonus;
    };
    m();
    auto refs = [&](int k) {
        count += k + bonus + held + word[4]; // IN_REFS
        return count;
    };
    refs(2);
    std::sort(sizes.begin(), sizes.end(), [&sizes, bonus](long a, long b) {
        return a + bonus > b + (long)sizes.size(); // IN_SORT
    });
    auto one = [bonus](int k) { return k + bonus; }; // ONE_LINE
    long total = one(4);
    std::printf("CAPS:%d %ld %ld %ld\\n", bonus, count, total, sizes[0]);
    delete heap;
    delete[] word;
    return 0;
}
"""


def capture_rows(source, marker, names, capsys, hits="1"):
    code, result = probe(source, line_of(CAPTURES, f"// {marker}"), names, capsys, "--hits", hits)
    assert code == 0 and "CAPS:2 62 6 3" in result["output"], result
    return {(row["hit"], row["selector"]): row for row in result["rows"]}


@pytest.mark.timeout(300)
def test_a_lambdas_captures_are_read_through_its_object_and_never_changed(cpp, capsys):
    # #361: GCC passes a lambda's captures through `__closure`, which lldb-dap doesn't
    # list, so each was `not found` in the lambda. clang lists them as members of `this`.
    compiler, root = cpp
    source = root / "caps.cpp"
    source.write_text(CAPTURES)
    mutable = capture_rows(source, "IN_MUTABLE", "bonus,count,pt,pt.x,n", capsys)
    refs = capture_rows(source, "IN_REFS", "bonus,count,held,word,k", capsys)
    sort = capture_rows(source, "IN_SORT", "bonus,sizes", capsys)
    # A one-line lambda's first stop is in `main`; its second, before the lambda stores
    # `__closure`, where no capture is read.
    one = capture_rows(source, "ONE_LINE", "bonus", capsys, "all")
    if compiler == "clang":
        # Unchanged: a capture is a member of the lambda's `this`, which a selector names.
        for rows in (mutable, sort):
            assert all(
                row["error"].startswith("not found")
                and f"this has a member '{name}'" in row["error"]
                for (_, name), row in rows.items()
                if "." not in name
            ), rows
        assert "__closure" not in json.dumps([list(r.values()) for r in (mutable, refs, sort, one)])
        return
    # By value: the lambda's copy; by reference: what it refers to, the reference's type
    # kept as the debugger's. Never settable: the lambda's object holds them.
    assert {name: row["value"] for (_, name), row in mutable.items()} == {
        "bonus": "2",
        "count": "7",
        "pt": "{x = 3, y = 4}",
        "pt.x": "3",
        "n": "8",
    }, mutable
    assert (mutable[1, "count"]["type"], mutable[1, "count"]["debugger_type"]) == (
        "long",
        "long &",
    )
    for row in mutable.values():
        assert row["settable"] is False and row["not_settable"] == "capture", row
        assert "view" not in row and "note" not in row, row
    # The lambda's object is `refs` in `main`, and what `count` and `bonus` refer to are
    # `main`'s locals: proven. `held` refers to a heap block, which Peeky can't prove.
    assert [refs[1, name]["value"] for name in ("bonus", "count", "held")] == ["2", "18", "40"]
    assert "view" not in refs[1, "count"] and "view" not in refs[1, "bonus"], refs
    assert refs[1, "held"]["view"] == "unproven", refs
    assert refs[1, "held"]["note"].startswith(
        "read through `held`: Peeky can't show that `held` refers to a live object, so this "
        "value may be left over: its address is in no local, member or element"
    ), refs
    # A pointer captured by reference shows its value: its text is read through it.
    assert refs[1, "word"]["type"] == "char *" and refs[1, "word"]["view"] == "unproven", refs
    assert refs[1, "word"]["value"].endswith('"heap"'), refs
    assert refs[1, "word"]["note"].startswith(
        "Peeky can't show that `word` points at a live object, so its text may be left over"
    ), refs
    assert refs[1, "k"]["value"] == "2" and refs[1, "k"]["settable"] is True, refs
    # `std::sort` calls a copy of the lambda's object, which no frame Peeky lists holds.
    assert sort[1, "bonus"]["value"] == "2", sort
    assert sort[1, "sizes"]["type"] == "std::vector<long>", sort
    for row in sort.values():
        assert row["view"] == "unproven", row
        assert row["note"].startswith(
            "read through `__closure`: Peeky can't show that `__closure` points at a live "
            "object, so this value may be left over: it points into the stack"
        ), row
    assert one[1, "bonus"]["value"] == "2" and one[1, "bonus"]["function"] == "main", one
    # Why, as the worker says it (review of #361: not that lldb-dap doesn't list it).
    assert one[2, "bonus"]["error"] == (
        "not found: 'bonus' is a capture of this lambda, which GCC passes through "
        "`__closure`, and Peeky can't read it here: the stop is at the lambda's start, "
        "before the prologue stores `__closure`, so `__closure` holds leftovers (the build "
        "marks no prologue end, as GCC's never do, so LLDB stops a one-line function here); "
        "read it where the lambda is created, or print it from the program; visible: k"
    ), one


@pytest.mark.timeout(240)
def test_a_lambdas_capture_is_never_changed_and_says_why(cpp, capsys):
    # #361: a capture, or a member of one, is reached through `__closure`: refused before
    # the program runs, and at a stop, saying so.
    compiler, root = cpp
    source = root / "caps.cpp"
    source.write_text(CAPTURES)
    line = line_of(CAPTURES, "// IN_MUTABLE")
    code, result = probe(source, line, "bonus", capsys, "--set", "bonus=5")
    assert code != 0 and result["status"] == "failed", result
    if compiler == "gcc":
        assert result["error"]["code"] == "MUTATION_REJECTED", result
        assert (
            "is reached through a pointer or reference (a C++ structured binding of a "
            "reference, or of a pair or tuple, a GCC lambda's capture" in result["error"]["message"]
        )
    assert main(["session", "start", str(source), "--at", str(line)]) == 0
    capsys.readouterr()
    try:
        refused = []
        for change in ("n=9", "pt.x=9"):
            assert main(["session", "set", change]) != 0
            refused.append(capsys.readouterr().err)
        main(["session", "get", "n", "pt.x", "--json"])
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
    finally:
        main(["session", "close"])
        capsys.readouterr()
    if compiler == "clang":
        assert all("not" in text for text in refused), refused
        return
    assert (
        "MUTATION_REJECTED" in refused[0]
        and (
            "'n' cannot be changed: `n` is a capture of this lambda, so it is reached through a "
            "pointer or reference: GCC keeps it in the lambda's object"
        )
        in refused[0]
    ), refused
    assert "`pt.x` cannot be changed: `pt` is a capture of this lambda" in refused[1], refused
    # Nothing changed.
    assert [rows["n"]["value"], rows["pt.x"]["value"]] == ["8", "3"], rows


OPTIMIZED_CAPTURES = """#include <cstdio>
int main(int argc, char **) {
    int bonus = argc + 1;
    long count = argc * 7;
    auto f = [bonus, &count](int k) __attribute__((noinline)) {
        count += k + bonus; // IN_OPT
        std::printf("OPT:%ld\\n", count);
        return count;
    };
    long a = f(1);
    long b = f(2);
    std::printf("%ld %ld\\n", a, b);
    return 0;
}
"""


@pytest.mark.timeout(240)
@pytest.mark.parametrize("optimization", ["O1", "O2"])
def test_an_optimized_lambdas_captures_are_not_read(cpp, optimization):
    # #361: optimized, a `mutable` lambda may keep a capture in a register while its
    # object holds an older value: no capture is read through `__closure` there.
    from peeky.native import NativeTarget
    from peeky.probe import Probe, run_probe

    compiler, root = cpp
    (root / "opt.cpp").write_text(OPTIMIZED_CAPTURES)
    adapter = NativeTarget(
        sources=("opt.cpp",),
        language="cpp",
        compiler=os.environ["PEEKY_NATIVE_CXX"],
        adapter=os.environ["PEEKY_NATIVE_DAP"],
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
        optimization=optimization,
    )
    line = line_of(OPTIMIZED_CAPTURES, "// IN_OPT")
    result = run_probe(
        root, Probe("opt.cpp", line, ("bonus", "count"), hits="all"), adapter=adapter
    )
    assert result["status"] == "completed", result.get("error")
    said = 0
    for row in result["rows"]:
        assert row.get("not_settable") != "capture" and "__closure" not in row.get("note", ""), row
        if row.get("function", "").startswith("operator()"):
            assert row.get("value") is None, row
        if "is a capture of this lambda" in row.get("error", ""):
            # Why, as the worker says it (review of #361).
            said += 1
            assert (
                "and Peeky can't read it here: Peeky reads no capture in an optimized build, "
                "where a `mutable` lambda may keep one in a register while its object holds "
                "an older value; read it where the lambda is created"
            ) in row["error"], row
    if compiler == "gcc" and optimization == "O1":
        assert said, result["rows"]


NESTED = """#include <cstdio>

struct T { long a, b; };
struct Acc {
    long total = 5;
    long add(long v) {
        auto by_pointer = [this, v](long w) {
            return total + v + w; // BY_POINTER
        };
        auto by_copy = [*this, v](long w) {
            return total + v + w; // BY_COPY
        };
        auto named = [self = *this, v](long w) {
            return self.total + v + w; // NAMED
        };
        return by_pointer(1) + by_copy(2) + named(3);
    }
};

__attribute__((noinline)) static long id(long v) { return v; }
static void use(long v) { std::printf("USE:%ld\\n", v); }

int main() {
    T t{1, [&] {
        return [&] {
            return [&] {
                return [&] {
                    return [&] {
                        return id(t.b); // DEEPEST
                    }();
                }();
            }();
        }();
    }()};
    Acc acc;
    long r = acc.add(3);
    int x = 7;
    auto other = [x]() {
        use(x); // OTHER_OUTSIDE_BEFORE
        {
            use(x); // OTHER_INSIDE_BEFORE
            long x = 9;
            use(x); // OTHER_INSIDE_AFTER
        }
        use(x); // OTHER_OUTSIDE_AFTER
    };
    auto same = [x]() {
        use(x); // SAME_OUTSIDE_BEFORE
        {
            use(x); // SAME_INSIDE_BEFORE
            int x = 9;
            use(x); // SAME_INSIDE_AFTER
        }
        use(x); // SAME_OUTSIDE_AFTER
        { use(x); int x = 11; use(x); } // SAME_ONE_LINE
    };
    other();
    same();
    std::printf("NEST:%ld %ld %ld\\n", t.a, r, acc.total);
    return 0;
}
"""


def nested_rows(source, marker, names, capsys):
    code, result = probe(source, line_of(NESTED, f"// {marker}"), names, capsys)
    assert result["status"] == "completed" and "NEST:1 30 5" in result["output"], result
    return {row["selector"]: row for row in result["rows"] if row["hit"] == 1}


@pytest.mark.timeout(240)
def test_a_lambda_nested_five_deep_in_a_running_initializer_is_withheld(cpp, capsys):
    # Review of #361: only 4 functions around a lambda were read, then it counted as
    # finished: the fifth `[&]` inside `T t{1, …}` read `t.b` before `t` was built (GCC's
    # capture, and clang's `this` too).
    compiler, root = cpp
    source = root / "nest.cpp"
    source.write_text(NESTED)
    rows = nested_rows(source, "DEEPEST", "t,t.b,this", capsys)
    why = (
        f"this lambda is defined in the declaration of `t` (line {line_of(NESTED, 'T t{')}), "
        "which may still be running, so what it captures may be under construction"
    )
    for name in ("t", "t.b") if compiler == "gcc" else ("this",):
        assert "value" not in rows[name] and why in rows[name]["error"], rows


@pytest.mark.timeout(300)
def test_a_capture_and_a_local_of_its_name_are_told_apart_by_scope(cpp, capsys):
    # Review of #361: the capture was dropped wherever the lambda also declared its name
    # with another type, and hidden wherever lldb-dap listed a local of its name, as in
    # the local's block above its declaration, where the code names the capture.
    compiler, root = cpp
    source = root / "nest.cpp"
    source.write_text(NESTED)
    shown = {}
    for kind in ("OTHER", "SAME"):
        for place in ("OUTSIDE_BEFORE", "INSIDE_BEFORE", "INSIDE_AFTER", "OUTSIDE_AFTER"):
            shown[kind, place] = nested_rows(source, f"{kind}_{place}", "x", capsys)["x"]
    one = nested_rows(source, "SAME_ONE_LINE", "x", capsys)["x"]
    if compiler == "clang":
        # Unchanged: the capture is `this.x`; the local reads where the debugger lists it.
        assert all("__closure" not in json.dumps(row) for row in (*shown.values(), one))
        assert shown["SAME", "INSIDE_AFTER"]["value"] == "9", shown
        return
    for kind, local in (("OTHER", "long"), ("SAME", "int")):
        for place in ("OUTSIDE_BEFORE", "INSIDE_BEFORE", "OUTSIDE_AFTER"):
            row = shown[kind, place]
            assert (row["value"], row["type"], row["not_settable"]) == ("7", "int", "capture"), (
                kind,
                place,
                row,
            )
        row = shown[kind, "INSIDE_AFTER"]
        assert (row["value"], row["type"]) == ("9", local) and "not_settable" not in row, row
    # A use of the capture and its declaration on one line: Peeky can't tell which.
    assert "value" not in one and one["error"] == (
        "not shown: this lambda also declares a local `x` on line "
        f"{line_of(NESTED, '// SAME_ONE_LINE')}, and Peeky can't tell whether the code at this "
        "stop names that local or the capture; print it from the program"
    ), one


@pytest.mark.timeout(240)
def test_this_of_a_lambda_that_copies_the_object_is_its_copy(cpp, capsys):
    # Review of #361: GCC gives `[*this]`'s `this` no location, so it had no value; the
    # copy is the lambda's member `__this`. `[this]` and `[self = *this]` read as before.
    compiler, root = cpp
    source = root / "nest.cpp"
    source.write_text(NESTED)
    by_pointer = nested_rows(source, "BY_POINTER", "this.total,v", capsys)
    by_copy = nested_rows(source, "BY_COPY", "this,this.total,v", capsys)
    named = nested_rows(source, "NAMED", "self.total,v", capsys)
    if compiler == "clang":
        # Unchanged: clang's `this` is the lambda's object, which holds the copy as `this`.
        assert by_pointer["this.total"]["error"].startswith("'total' not found in 'this'")
        assert by_copy["this.total"]["error"].startswith("'total' not found in 'this'")
        assert "__closure" not in json.dumps([by_pointer, by_copy, named])
        return
    assert by_pointer["this.total"]["value"] == "5" and by_pointer["v"]["value"] == "3"
    assert (by_copy["this"]["value"], by_copy["this"]["type"]) == ("{total = 5}", "Acc")
    for row in (by_copy["this"], by_copy["this.total"], named["self.total"]):
        assert row["not_settable"] == "capture" and "view" not in row, row
    assert by_copy["this.total"]["value"] == "5" and named["self.total"]["value"] == "5"


SCOPED = """#include <cstdio>
#define DECLARE(n) if (int n = 9) use(n)
static void use(long v) { std::printf("USE:%ld\\n", v); }
int main() {
    int x = 7;
    auto macro = [x]() {
        DECLARE(x);
        use(x); // MACRO_AFTER
    };
    auto line = [x]() {
        use(x); { int x = 11; use(x); } // ONE_LINE_BLOCK
        use(x); // LINE_AFTER
    };
    macro();
    line();
    return 0;
}
"""


@pytest.mark.timeout(240)
def test_a_local_hides_a_capture_only_where_its_block_runs(cpp, capsys):
    # Review round 2 of #361: a macro's `if (int x = 9) use(x)` read, by its text, as in
    # scope to the lambda's end, though its block had ended: the capture was said hidden.
    # A line holding code before a block and the block itself stops in each.
    compiler, root = cpp
    source = root / "scoped.cpp"
    source.write_text(SCOPED)
    rows = {}
    for marker in ("MACRO_AFTER", "ONE_LINE_BLOCK", "LINE_AFTER"):
        code, result = probe(source, line_of(SCOPED, f"// {marker}"), "x", capsys, "--hits", "all")
        assert result["status"] == "completed", result
        rows[marker] = [row for row in result["rows"] if row["selector"] == "x"]
    if compiler == "clang":
        assert "__closure" not in json.dumps(rows), rows
        return
    for marker in ("MACRO_AFTER", "LINE_AFTER"):
        [row] = rows[marker]
        assert (row["value"], row["not_settable"]) == ("7", "capture"), (marker, row)
    # The line's first stop runs `use(x)` before the block: the capture. Its second is in
    # the block, on its declaration's line: Peeky can't tell.
    before, inside = rows["ONE_LINE_BLOCK"]
    assert (before["value"], before["not_settable"]) == ("7", "capture"), before
    assert "value" not in inside and inside["error"].startswith(
        "not shown: this lambda also declares a local `x` on line "
        f"{line_of(SCOPED, '// ONE_LINE_BLOCK')}, and Peeky can't tell"
    ), inside
