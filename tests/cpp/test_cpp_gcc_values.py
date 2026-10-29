"""C++ built by GCC (#167), on real builds with clang and g++: what LLDB misreads in
GCC's debug information is withheld with a true note or said, never shown as the
program's. Each case reads correctly with clang (libstdc++ on Linux, libc++ on macOS).

- Unordered containers: LLDB's libstdc++ formatter finds no element type in GCC's
  `std::allocator` (nor behind a typedef's name), so it counts each as empty. Peeky
  checks its count against the container's own `_M_element_count`. Where they agree, the
  entries are listed only where the formatter decodes them as the type the container
  holds, where it holds them: an allocator whose first template argument is another
  type, a 16-byte-aligned `long double`, or (LLDB 18) a map whose type's name holds `set`
  decode wrongly. Otherwise Peeky reads the entries from the container's own nodes
  (#360), where the debug information proves how and they are integers, bools and
  strings, in pairs and structs; else no preview, key or position read, or listing.
- A `std::string` set: LLDB reads GCC's `_M_local_buf` (`char[16]`) as `char[0]`; its
  length comes from the build's debug information, and lldb-dap 20, which has no
  writeMemory request, writes it with LLDB's `memory write` command.
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


def placed():
    """Whether Peeky can show a pointer at a local container points at a live object
    (#339): lldb-dap 20 gives each local's address. lldb-dap 18 gives none for one the
    debugger shows with a summary, so no node is read through such a pointer there."""
    return sys.platform != "linux" or bool(os.environ.get("PEEKY_EXPECT_READ_MEMORY"))


def withheld(row, count):
    """Whether a row is withheld as an unordered container LLDB miscounts: the container
    itself (`not listed: ...`), or a key, position or member read in it. Where Peeky
    would read its nodes but doesn't at this stop, the row says why (eval 13)."""
    error = str(row.get("error") or "")
    reason = MISCOUNTED.format(count)
    unread = re.compile(
        rf"the debugger counts 0 of (?:its {count} entries|the {count} entries it points at), "
        r"and Peeky \S.+"
    )
    if row.get("value") is not None:
        return False
    if not (error.endswith(reason) or unread.search(error)):
        return False
    pointed = f"not listed: it points at an unordered container, and {reason}"
    return (
        error in (f"not listed: {reason}", pointed)
        or bool(re.fullmatch(f"not listed: {unread.pattern}", error))
        or bool(re.fullmatch(r"\[[^\]]+\] not read: [\w\[\]#.]+ is not listed: .+", error))
    )


@pytest.mark.timeout(180)
def test_an_unordered_container_is_read_whole_or_not_listed(cpp, capsys):
    # #167: with GCC, `us{4}` read `size=0 {}`, `um[1]` "um is empty; in C++, um[1] would
    # insert the key", and `us[#0]` "us has 0 entries", with no note. Then each was
    # withheld as `not listed`; #360: each is read from its own nodes.
    compiler, root = cpp
    source = root / "unordered.cpp"
    source.write_text(UNORDERED)
    capture = ",".join(["none", "p", *READS, *HOLDING])
    code, result = probe(source, line_of(UNORDERED, "// HERE"), capture, capsys)
    assert "SIZES:1 1 0 2 2 2 1 1 " in result["output"], result
    rows = {row["selector"]: row for row in result["rows"]}
    # An empty one's counts agree, whatever the compiler.
    assert rows["none"]["value"] == "size=0 {}", rows["none"]
    # Every one reads, by key, by position and inside another value: GCC's from their
    # nodes (and clang's typedef'd one with LLDB 18, which counts it empty). Through a
    # pointer, only where Peeky can show it points at a live object (review of #360).
    pointed = compiler == "gcc" and not placed()
    for selector, (value, count) in READS.items():
        if pointed and selector.startswith("p"):
            assert withheld(rows[selector], count), (selector, rows[selector])
            continue
        assert rows[selector].get("value") == value, (selector, rows[selector])
        if compiler == "gcc":
            # Read from memory, and said so.
            assert rows[selector].get("view") == "memory", (selector, rows[selector])
    for selector, value in HOLDING.items():
        assert rows[selector].get("value") == value, (selector, rows[selector])
    pointer = rows["p"]
    assert re.fullmatch(r"0x[0-9a-f]+ size=1", str(pointer.get("value"))) or (
        pointed
        and withheld(pointer, 1)
        and pointer["error"].startswith("not listed: the debugger counts 0 of the 1 entries it ")
    ), pointer
    # The evidence keeps what the rows showed.
    assert main(["sql", "SELECT selector, value, error FROM last_run", "--json"]) == 0
    stored = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    for selector in READS:
        assert stored[selector]["value"] == rows[selector].get("value"), stored[selector]
        assert stored[selector]["error"] == rows[selector].get("error"), stored[selector]


@pytest.mark.timeout(180)
def test_a_listing_and_a_callers_reference_follow_the_same_rule(cpp, capsys):
    compiler, root = cpp
    source = root / "unordered.cpp"
    source.write_text(UNORDERED)
    assert main(["session", "start", str(source), "--at", str(line_of(UNORDERED, "IN_LOOK"))]) == 0
    capsys.readouterr()
    try:
        main(["session", "get", "ref", "ptr", "ptr[0]", "--json"])
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
        main(["session", "get", "um", "--frame", "1", "--json"])
        rows.update({row["selector"]: row for row in json.loads(capsys.readouterr().out)})
    finally:
        main(["session", "close"])
        capsys.readouterr()
    # A reference shows its address and the container; its entries are listed by key. A
    # pointer shows its address and count, and what it points at is the container.
    if compiler == "gcc" and not placed():
        # Peeky can't show that `ref` and `ptr` refer to a live object here (#339): no
        # node is read through either (review of #360).
        for selector in ("ref", "ptr", "ptr[0]"):
            assert withheld(rows[selector], 1), (selector, rows[selector])
    else:
        assert re.fullmatch(r"0x[0-9a-f]+ size=1 \{1: 2\}", rows["ref"]["value"]), rows
        listed = {c["selector"]: c["value"] for c in rows["ref"]["children"]}
        assert listed == {"ref[1]": "2"}, listed
        assert re.fullmatch(r"0x[0-9a-f]+ size=1", rows["ptr"]["value"]), rows["ptr"]
        assert rows["ptr[0]"]["value"] == "size=1 {4}", rows["ptr[0]"]
        listed = {c["selector"]: c["value"] for c in rows["ptr[0]"]["children"]}
        assert listed == {"ptr[0][#0]": "4"}, listed
    # A caller's `um` may be withheld for its own line (not finished); never miscounted.
    assert rows["um"].get("value") != "size=0 {}", rows["um"]


KINDS = """#include <cstdio>
#include <string>
#include <unordered_map>
#include <unordered_set>
struct Pt { int x; long y; };
struct Tagged { int id; std::string name; };
int main() {
    std::unordered_map<std::string, int> words{{"al", 1}};
    std::unordered_map<int, std::string> longs{{7, std::string(40, 'x')}};
    std::unordered_map<int, Pt> points{{1, {5, -6}}};
    std::unordered_set<bool> flags{true};
    std::unordered_map<int, Tagged> tagged{{3, {9, "nine"}}};
    std::unordered_map<int, double> ratios{{1, 0.5}};
    std::unordered_set<unsigned long> big;
    for (unsigned long i = 0; i < 300; ++i)
        big.insert(i);
    std::printf("KINDS:%zu %zu %zu %zu %zu %zu %zu\\n", words.size(), longs.size(), points.size(),
                flags.size(), tagged.size(), ratios.size(), big.size()); // HERE
    return 0;
}
"""
# Each selector's value, whatever the compiler and debugger.
KIND_READS = {
    "words": 'size=1 {"al": 1}',
    'words["al"]': "1",
    "words[#0].first": '"al"',
    "points": "size=1 {1: {x = 5, y = -6}}",
    "points[1]": "{x = 5, y = -6}",
    "points[1].y": "-6",
    "flags": "size=1 {true}",
    "flags[#0]": "true",
}
# A string in a struct, which clang's build leaves to LLDB's summary: with LLDB 18 that
# fails there (`<no value>`).
TAGGED = {"tagged": 'size=1 {3: {id = 9, name = "nine"}}', "tagged[3].name": '"nine"'}
LONG = '"' + "x" * 40 + '"'


@pytest.mark.timeout(180)
def test_entries_are_read_from_the_nodes_only_where_peeky_reads_their_type(cpp, capsys):
    # #360: with GCC, each of these was `not listed` (LLDB counts them empty). Peeky now
    # reads integers, bools and strings, in pairs and structs, from the nodes. A string's
    # text outside its node needs `readMemory` (lldb-dap 20). #374: a `double`, and more
    # than 256 entries, read too.
    compiler, root = cpp
    source = root / "kinds.cpp"
    source.write_text(KINDS)
    capture = ",".join([*KIND_READS, *TAGGED, "longs", "longs[7]", "ratios", "big"])
    code, result = probe(source, line_of(KINDS, "// HERE"), capture, capsys)
    assert "KINDS:1 1 1 1 1 1 300" in result["output"], result
    rows = {row["selector"]: row for row in result["rows"]}
    for selector, value in {**KIND_READS, **TAGGED}.items():
        if sys.platform != "linux" and "{" in value:
            # Apple's LLDB shows a struct in libc++'s map as `Pt`, or `{…}` in a preview.
            continue
        if selector in TAGGED and compiler == "clang" and rows[selector].get("value") != value:
            shown = rows[selector].get("value")
            assert shown is None or shown == value.replace('"nine"', "<no value>"), shown
        else:
            assert rows[selector].get("value") == value, (selector, rows[selector])
    reads = compiler == "clang" or bool(os.environ.get("PEEKY_EXPECT_READ_MEMORY"))
    if reads or sys.platform != "linux":
        assert rows["longs[7]"].get("value") == LONG, rows["longs[7]"]
        assert rows["longs"].get("value") == 'size=1 {7: "' + "x" * 35 + '"...}', rows["longs"]
    else:
        assert withheld(rows["longs"], 1) and withheld(rows["longs[7]"], 1), rows
    assert rows["ratios"].get("value") == "size=1 {1: 0.5}", rows["ratios"]
    # Where LLDB counts `big` empty (GCC's; clang's with LLDB 18), from its 300 nodes.
    assert str(rows["big"].get("value")).startswith("size=300 {"), rows["big"]
    if compiler == "gcc":
        assert rows["ratios"]["view"] == rows["big"]["view"] == "memory", rows
    # A listing names each entry by its key, as for any map.
    assert main(["session", "start", str(source), "--at", str(line_of(KINDS, "// HERE"))]) == 0
    capsys.readouterr()
    try:
        main(["session", "get", "words", "tagged", "--json"])
        listed = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
    finally:
        main(["session", "close"])
        capsys.readouterr()
    children = {c["selector"]: c["value"] for c in listed["words"]["children"]}
    assert children == {'words["al"]': "1"}, listed["words"]
    children = {c["selector"]: c["value"] for c in listed["tagged"]["children"]}
    if compiler == "gcc":
        assert children == {"tagged[3]": '{id = 9, name = "nine"}'}, listed["tagged"]


MORE_KINDS = """#include <cstdio>
#include <unordered_map>
#include <unordered_set>
enum Color { Red, Green = 5, Blue = 5, Teal = 6 };
enum class Shade : unsigned char { Dark = 1, Light = 200 };
int main() {
    double d[8] = {0.1, 1e10, 100.0, 1e-7, -2.5, 1.0 / 3, 5e-324, -0.0};
    float f[3] = {0.1f, 1e10f, 1e-3f};
    char c[4] = {'a', '\\n', (char)0xc8, '\\''};
    std::unordered_map<int, double> ds;
    for (int i = 0; i < 8; ++i)
        ds[i] = d[i];
    std::unordered_map<int, float> fs{{0, f[0]}, {1, f[1]}, {2, f[2]}};
    std::unordered_map<int, char> cs{{0, c[0]}, {1, c[1]}, {2, c[2]}, {3, c[3]}};
    std::unordered_set<wchar_t> ws{L'a'};
    std::unordered_set<char16_t> u16{u'\\n'};
    std::unordered_set<char32_t> u32{U'\\U0001F600'};
    std::unordered_map<int, Color> colors{{0, Green}, {1, Teal}, {2, (Color)9}};
    std::unordered_set<Shade> shades{Shade::Light};
    int x = 5;
    int *px = &x;
    std::unordered_set<int *> ptrs{px};
    std::printf("MORE:%zu %zu %zu %zu\\n", ds.size(), cs.size(), colors.size(), ptrs.size()); // HERE
    return 0;
}
"""


@pytest.mark.timeout(240)
def test_numbers_characters_enums_and_pointers_are_read_from_the_nodes(cpp, capsys):
    # #374: with GCC, each of these was `not listed`, though Peeky read the nodes of an
    # `int` set. Now each value reads as the debugger shows the same value in a local.
    compiler, root = cpp
    source = root / "more.cpp"
    source.write_text(MORE_KINDS)
    line = line_of(MORE_KINDS, "// HERE")
    pairs = [(f"ds[{i}]", f"d[{i}]") for i in range(8)] + [
        (f"fs[{i}]", f"f[{i}]") for i in range(3)
    ]
    pairs += [(f"cs[{i}]", f"c[{i}]") for i in range(4)]
    code, result = probe(source, line, ",".join(x for pair in pairs for x in pair), capsys)
    assert "MORE:8 4 3 1" in result["output"], result
    rows = {row["selector"]: row for row in result["rows"]}
    for read, local in pairs:
        # The debugger's own text for the same value in a local: `0.10000000000000001`,
        # `1.0E+10`, `'\xc8'`, `'''`.
        assert rows[read].get("value") == rows[local]["value"], (read, rows[read], rows[local])
        if compiler == "gcc":
            assert rows[read].get("view") == "memory", rows[read]
    capture = "ws,ws[#0],u16,u32,colors[0],colors[1],colors[2],shades,ptrs,ptrs[#0],px,ptrs[#0][0]"
    code, result = probe(source, line, capture, capsys)
    rows = {row["selector"]: row for row in result["rows"]}
    shown = {selector: rows[selector].get("value") for selector in ("ws", "u16", "u32", "shades")}
    assert shown == {
        "ws": "size=1 {L'a'}",
        "u16": "size=1 {u'\\n'}",
        "u32": "size=1 {U'\U0001f600'}",
        "shades": "size=1 {Light}",
    }, shown
    # An enum's one enumerator of that value; else the number (two hold 5).
    colors = [rows[f"colors[{i}]"].get("value") for i in range(3)]
    assert colors == ["5" if compiler == "gcc" else "Green", "Teal", "9"], colors
    # A pointer: its address, as the local's; what it points at is never read from a node.
    assert rows["ptrs[#0]"]["value"] == rows["px"]["value"], rows
    if compiler == "gcc":
        assert rows["ws[#0]"]["value"] == "L'a'", rows["ws[#0]"]
        assert rows["ptrs"]["value"] == "size=1 {" + rows["px"]["value"] + "}", rows["ptrs"]
        assert rows["ptrs[#0][0]"]["error"] == (
            "[0] not read: ptrs[#0] (int *) is a pointer read from an unordered container's "
            "node, shown by its address alone; Peeky never follows one, since nothing shows "
            "that what it points at is alive; print what it points at from the program"
        ), rows["ptrs[#0][0]"]


IN_NODES = """#include <array>
#include <cstdio>
#include <map>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>
int main() {
    std::unordered_map<int, std::vector<int>> lists{{1, {2, 3}}};
    std::unordered_map<int, std::vector<std::string>> names{{1, {"al", std::string(20, 'z')}}};
    std::unordered_map<int, std::array<int, 2>> pairs{{1, {4, 5}}};
    std::unordered_map<int, std::unordered_set<int>> groups{{1, {6}}};
    std::unordered_map<int, std::map<int, int>> trees{{1, {{7, 8}}}};
    std::unordered_map<int, int> big;
    for (int i = 0; i < 700; ++i)
        big[i] = 2 * i;
    std::unordered_map<std::string, int> words;
    for (int i = 0; i < 300; ++i)
        words["k" + std::to_string(i)] = i;
    std::printf("NESTED:%zu %zu %zu\\n", lists.size(), big.size(), words.size()); // HERE
    return 0;
}
"""
NO_MEMORY = (
    "not read: a container held in a node is read only with a debugger that reads memory, as "
    "lldb-dap 20 does: its elements are outside the node; print it from the program"
)


@pytest.mark.timeout(300)
def test_a_nested_container_reads_where_proven_and_a_big_one_by_position_or_key(cpp, capsys):
    # #374: with GCC, each of these was `not listed`. A container held in a node now reads
    # where its own layout is proven, with a debugger that reads memory for one whose
    # elements are outside the node; else it alone is withheld, with why. A container of
    # more than 256 entries reads too: its count proven by its whole chain, an entry past
    # the first 256 read when its position or key asks for it.
    compiler, root = cpp
    source = root / "held.cpp"
    source.write_text(IN_NODES)
    line = line_of(IN_NODES, "// HERE")
    capture = (
        "lists,lists[1],lists[1][1],names,pairs,pairs[1],groups,trees,trees[1],big,big[699],"
        'big[#650].second,big[#700],big[5000],words,words["k299"],words["nope"]'
    )
    code, result = probe(source, line, capture, capsys)
    assert "NESTED:1 700 300" in result["output"], result
    rows = {row["selector"]: row for row in result["rows"]}
    if compiler == "clang" or placed():
        assert rows["lists"]["value"] == "size=1 {1: size=2 [2, 3]}", rows["lists"]
        assert rows["lists[1]"]["value"] == "size=2 [2, 3]", rows["lists[1]"]
        assert rows["lists[1][1]"]["value"] == "3", rows["lists[1][1]"]
        assert rows["names"]["value"] == 'size=1 {1: size=2 ["al", "' + "z" * 20 + '"]}', rows
    else:
        # lldb-dap 18 reads no memory outside the nodes: those alone are withheld.
        assert rows["lists"]["value"] == "size=1 {1: <not read>}", rows["lists"]
        assert rows["lists[1]"]["error"] == NO_MEMORY, rows["lists[1]"]
        assert rows["names"]["value"] == "size=1 {1: <not read>}", rows["names"]
    # LLDB 18 reads clang's `groups` as a set too (its name holds `set`): from its nodes.
    if placed():
        assert rows["groups"]["value"] == "size=1 {1: size=1 {6}}", rows["groups"]
    else:
        assert rows["groups"]["value"] == "size=1 {1: <not read>}", rows["groups"]
    if compiler == "clang":
        assert rows["trees"]["value"] == "size=1 {1: size=1 {7: 8}}", rows["trees"]
        assert rows["big"]["value"].startswith("size=700 {"), rows["big"]
        return
    # A `std::array` is inside the node; a `std::map` isn't read in one.
    assert rows["pairs"]["value"] == "size=1 {1: size=2 [4, 5]}", rows["pairs"]
    assert rows["pairs[1]"]["value"] == "size=2 [4, 5]", rows["pairs[1]"]
    assert rows["trees"]["value"] == "size=1 {1: <not read>}", rows["trees"]
    assert rows["trees[1]"]["error"] == (
        "not read: Peeky doesn't read a `std::map` held in an unordered container's node; "
        "print it from the program"
    ), rows["trees[1]"]
    # Past 256 entries, and past the inspection window's 512: by position and by key.
    assert rows["big"]["value"].startswith("size=700 {"), rows["big"]
    assert rows["big[699]"]["value"] == "1398", rows["big[699]"]
    assert int(rows["big[#650].second"]["value"]) % 2 == 0, rows["big[#650].second"]
    assert rows["big[#700]"]["error"] == (
        "[#700] not found: big has 700 entries: big[#0] to big[#699]"
    ), rows["big[#700]"]
    assert re.fullmatch(
        r"\[5000\] not found: big has no key 5000 \(keys: (?:[0-9]+, ){20}\.\.\. "
        r"\(700 total\)\); in C\+\+, big\[5000\] would insert it with a default value",
        rows["big[5000]"]["error"],
    ), rows["big[5000]"]
    assert rows['words["k299"]']["value"] == "299", rows['words["k299"]']
    assert "(300 total)" in rows['words["nope"]']["error"], rows['words["nope"]']


CHANGING = """#include <cstddef>
#include <cstdio>
#include <unordered_map>
static bool armed = false;
static int marks = 0;
static void mark() {
    ++marks; // IN_MARK
}
struct Loud {
    std::size_t operator()(int k) const {
        if (armed)
            mark();
        return static_cast<std::size_t>(k);
    }
};
int main() {
    std::unordered_map<int, int, Loud> um{{1, 2}};
    armed = true;
    bool found = um.find(1) != um.end();
    armed = false;
    std::printf("FOUND:%d %zu\\n", found ? 1 : 0, um.size()); // AFTER
    return marks - 1;
}
"""


@pytest.mark.timeout(180)
def test_no_entry_is_read_from_the_nodes_while_a_library_call_runs(cpp, capsys):
    # #360: stopped in a hash function `um.find(1)` calls, `main`'s `um` may be part-way
    # through a change (#181), so its nodes are not read: GCC's stays withheld as LLDB
    # miscounts it, and clang's keeps LLDB's summary.
    compiler, root = cpp
    source = root / "changing.cpp"
    source.write_text(CHANGING)
    # Once `find` has returned, it reads.
    code, result = probe(source, line_of(CHANGING, "// AFTER"), "um,um[1]", capsys)
    assert "FOUND:1 1" in result["output"], result
    after = {row["selector"]: row.get("value") for row in result["rows"]}
    assert after == {"um": "size=1 {1: 2}", "um[1]": "2"}, result["rows"]
    assert main(["session", "start", str(source), "--at", str(line_of(CHANGING, "IN_MARK"))]) == 0
    capsys.readouterr()
    try:
        main(["session", "inspect", "--json"])
        frames = json.loads(capsys.readouterr().out)["frames"]
        caller = next(i for i, f in enumerate(frames) if f.get("name") == "main")
        main(["session", "get", "um", "um[1]", "--frame", str(caller), "--json"])
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
    finally:
        main(["session", "close"])
        capsys.readouterr()
    if compiler == "gcc":
        assert withheld(rows["um"], 1) and withheld(rows["um[1]"], 1), rows
        # It names the call it waits for (eval 13 prep, finding 3).
        assert re.fullmatch(
            r"not listed: the debugger counts 0 of its 1 entries, and Peeky doesn't read them "
            r"from its nodes while a standard-library call below this frame \(std::\S+\) may be "
            r"part-way through changing it; retry after that call returns",
            rows["um"]["error"],
        ), rows["um"]
    else:
        # LLDB's summary alone, or `no value yet` for the line under way (clang's).
        assert rows["um"].get("value") in ("size=1", None), rows["um"]
    assert rows["um"].get("view") != "memory" and rows["um[1]"].get("value") is None, rows


POINTERS = """#include <cstdio>
#include <new>
#include <unordered_set>
static std::unordered_set<int> *gone;
__attribute__((noinline)) static void keep() {
    alignas(std::unordered_set<int>) unsigned char buf[sizeof(std::unordered_set<int>)];
    // Never destroyed: once `keep` returns, its dead frame still reads as a set of {6}.
    gone = new (buf) std::unordered_set<int>{6};
}
int main() {
    std::unordered_set<int> local{3};
    std::unordered_set<int> *mine = &local;
    auto *heap = new std::unordered_set<int>{4};
    keep();
    std::unordered_set<int> *dangling = gone;
    std::printf("P:%zu %zu %d\\n", mine->size(), heap->size(), dangling != nullptr); // HERE
    delete heap;
    return 0;
}
"""
# The note on a pointer to an unordered container whose count LLDB gets wrong, where
# Peeky can't show it points at a live object, to read its nodes (eval 13 prep, finding 2).
POINTED = (
    "not listed: the debugger counts 0 of the 1 entries it points at, and Peeky reads its "
    "nodes only through a pointer it proves points at a live object, not `{}`; print its "
    "entries from the program"
)


@pytest.mark.timeout(180)
def test_no_node_is_read_through_a_pointer_not_proven_to_point_at_a_live_object(cpp, capsys):
    # Review of #360: a heap block's set, and a dead frame's (whose bytes still read as
    # one), showed their entries from their nodes as `[memory]`, with no note: `heap`
    # and `dangling` are pointers Peeky can't show point at a live object (#339).
    compiler, root = cpp
    source = root / "pointers.cpp"
    source.write_text(POINTERS)
    capture = "mine,mine[0],heap,heap[0],heap[0][#0],dangling,dangling[0]"
    code, result = probe(source, line_of(POINTERS, "// HERE"), capture, capsys)
    assert "P:1 1 1" in result["output"], result
    rows = {row["selector"]: row for row in result["rows"]}
    for selector in ("heap", "heap[0]", "heap[0][#0]", "dangling", "dangling[0]"):
        assert rows[selector].get("view") != "memory", (selector, rows[selector])
    if compiler == "gcc":
        assert rows["heap"]["error"] == POINTED.format("heap"), rows["heap"]
        assert rows["dangling"]["error"] == POINTED.format("dangling"), rows["dangling"]
        for selector in ("heap[0]", "heap[0][#0]", "dangling[0]"):
            assert rows[selector].get("value") is None, (selector, rows[selector])
        if placed():
            # A pointer at the frame's own local, which Peeky places, is read.
            assert re.fullmatch(r"0x[0-9a-f]+ size=1", rows["mine"]["value"]), rows["mine"]
            assert rows["mine[0]"]["value"] == "size=1 {3}", rows["mine[0]"]
            assert rows["mine[0]"]["view"] == "memory", rows["mine[0]"]
        else:
            # lldb-dap 18 gives `local` no address: what `mine` points at isn't placed.
            assert rows["mine"]["error"] == (
                "not listed: the debugger counts 0 of the 1 entries it points at, and Peeky "
                "reads its nodes only through a pointer it proves points at a live object, not "
                "`mine`; inspect this candidate local, `local` in main (`--frame 0`): it may be "
                "what `mine` points at"
            ), rows["mine"]


REFERENCES = """#include <cstdio>
#include <new>
#include <unordered_set>
using Ref = std::unordered_set<int> &;
struct Holder { std::unordered_set<int> us; int n; };
struct Viewer { std::unordered_set<int> &seen; };
static std::unordered_set<int> *gone;
__attribute__((noinline)) static void keep() {
    alignas(std::unordered_set<int>) unsigned char buf[sizeof(std::unordered_set<int>)];
    // Never destroyed: once `keep` returns, its dead frame still reads as a set of {6}.
    gone = new (buf) std::unordered_set<int>{6};
}
int main() {
    std::unordered_set<int> local{3};
    auto &mine = local;
    Ref aliased = local;
    Viewer view{local};
    auto *heap = new std::unordered_set<int>{4};
    auto &onheap = *heap;
    Viewer far{*heap};
    auto *box = new Holder{{5}, 1};
    Holder &boxed = *box;
    keep();
    std::unordered_set<int> &dangling = *gone;
    std::printf("R:%zu %zu %zu %zu %zu %d\\n", mine.size(), aliased.size(), view.seen.size(),
                onheap.size() + far.seen.size(), boxed.us.size(), &dangling != nullptr); // HERE
    delete box;
    delete heap;
    return 0;
}
"""
# Each reference to an unordered container: (its entries, whether Peeky can show it
# refers to a live object where the debugger gives each local's address).
REFERS = {
    "mine": ("{3}", True),
    "aliased": ("{3}", True),
    "view.seen": ("{3}", True),
    "onheap": ("{4}", False),
    "far.seen": ("{4}", False),
    "boxed.us": ("{5}", False),
    "dangling": ("{6}", False),
}


@pytest.mark.timeout(180)
def test_no_node_is_read_through_a_reference_not_proven_to_refer_to_a_live_object(cpp, capsys):
    # Review 2 of #360: a reference bypassed the rule on pointers, so `onheap` (`*heap`),
    # `far.seen`, `boxed.us` (through `Holder &` to a heap block) and `dangling` (a dead
    # frame's set) showed their entries from their nodes as `[memory]`, with no note.
    compiler, root = cpp
    source = root / "references.cpp"
    source.write_text(REFERENCES)
    capture = ",".join([*REFERS, *(f"{name}[#0]" for name in REFERS)])
    code, result = probe(source, line_of(REFERENCES, "// HERE"), capture, capsys)
    assert "R:1 1 1 2 1 1" in result["output"], result
    rows = {row["selector"]: row for row in result["rows"]}
    for name, (entries, live) in REFERS.items():
        row, entry = rows[name], rows[f"{name}[#0]"]
        if not live:
            assert row.get("view") != "memory" and entry.get("view") != "memory", (name, rows)
        if compiler != "gcc":
            continue
        if live and placed():
            # Its address, then the container, read from its nodes.
            assert re.fullmatch(r"0x[0-9a-f]+ size=1 " + re.escape(entries), row["value"]), row
            assert row["view"] == "memory" and entry["value"] == entries[1:-1], (row, entry)
        else:
            assert withheld(row, 1) and withheld(entry, 1), (name, row, entry)


DEPOT = """#include <cstdio>
#include <string>
#include <unordered_map>
using Stock = std::unordered_map<std::string, int>;
struct Move {
    std::string part;
    int units;
    ~Move() {
        units = 0; // IN_DTOR
    }
};
static bool enough(Stock &stock, const std::string &part, int units) {
    return stock.count(part) > 0 && stock.at(part) >= units; // IN_CALLEE
}
using Prices = std::unordered_map<int, double>;
static double priced(const Prices &prices) {
    return prices.at(1); // IN_PRICED
}
int main() {
    Stock on_hand;
    on_hand["a"] = 1;
    on_hand["b"] = 2;
    Prices prices;
    prices[1] = 1.5;
    int shipped = priced(prices) > 1.0 ? 0 : 1;
    {
        Move move{"a", 1};
        if (enough(on_hand, move.part, move.units))
            shipped++;
    }
    std::printf("S:%d %zu\\n", shipped, on_hand.size());
    return 0;
}
"""
# Eval 13 prep, finding 2: with lldb-dap 18, which gives a container local no address,
# Peeky can't place what a reference parameter refers to; the row offers the caller's
# local, which reads in its own frame, as a candidate, never as proven (#393 review).
UNPLACED = (
    "not listed: the debugger counts 0 of its 2 entries, and Peeky reads its nodes only "
    "through a reference it proves refers to a live object, not `stock`; inspect this "
    "candidate local, `on_hand` in main (`--frame 1`): it may be what `stock` refers to"
)
# Finding 3: below a destructor, a caller's container is said to wait for that call.
BELOW_DESTRUCTOR = (
    "not listed: the debugger counts 0 of its 2 entries, and Peeky doesn't read them from "
    "its nodes while a constructor, destructor or assignment operator below this frame "
    "(Move::~Move) may be part-way through changing it; retry after that call returns"
)


def got(capsys):
    """A `session get --json`'s rows, by selector."""
    return {row["selector"]: row for row in json.loads(capsys.readouterr().out)}


@pytest.mark.timeout(240)
def test_an_unordered_container_peeky_reads_no_node_of_says_why_and_where_it_reads(cpp, capsys):
    # Eval 13 prep, findings 2 and 3: a reference parameter to an unordered container
    # (lldb-dap 18), and a caller's container below a destructor, were withheld with the
    # debugger's miscount alone and "print its entries from the program", though the
    # frame that holds it, or the same frame once the call returns, reads it.
    compiler, root = cpp
    source = root / "depot.cpp"
    source.write_text(DEPOT)
    code, result = probe(source, line_of(DEPOT, "// IN_CALLEE"), 'stock,stock["a"]', capsys)
    assert "S:1 2" in result["output"], result
    rows = {row["selector"]: row for row in result["rows"]}
    if compiler == "gcc" and not placed():
        assert rows["stock"]["error"] == UNPLACED, rows["stock"]
        assert rows['stock["a"]']["error"].endswith(UNPLACED[len("not listed: ") :]), rows
    elif compiler == "gcc" or "value" in rows["stock"]:
        # Read from its nodes (GCC), or listed by the debugger.
        assert re.fullmatch(r'0x[0-9a-f]+ size=2 \{.*"a": 1.*\}', rows["stock"]["value"]), rows
        assert rows['stock["a"]']["value"] == "1", rows
    else:
        # LLDB miscounts clang's typedef'd one on Linux, and Peeky reads no node of it:
        # never sent to a frame where it wouldn't read either.
        assert "--frame" not in rows["stock"]["error"], rows["stock"]
    # The frame that holds it reads it, as the row says.
    assert main(["session", "start", str(source), "--at", str(line_of(DEPOT, "IN_CALLEE"))]) == 0
    capsys.readouterr()
    try:
        main(["session", "get", "on_hand", "--frame", "1", "--json"])
        held = got(capsys)["on_hand"]
    finally:
        main(["session", "close"])
        capsys.readouterr()
    reads = bool(re.fullmatch(r"size=2 \{.*\}", str(held.get("value"))))
    assert reads or compiler == "clang", held
    # Entries Peeky never reads from the nodes (a `double`): no pointer or reference is
    # blamed, whatever proves it (review).
    code, result = probe(source, line_of(DEPOT, "// IN_PRICED"), "prices", capsys)
    (row,) = result["rows"]
    if compiler == "gcc":
        assert row["error"] == f"not listed: {MISCOUNTED.format(1)}", row
    assert "Peeky reads its nodes" not in str(row.get("error")), row
    # Below `Move::~Move()`, `main`'s `on_hand` waits for that call, and reads after it.
    assert main(["session", "start", str(source), "--at", str(line_of(DEPOT, "IN_DTOR"))]) == 0
    capsys.readouterr()
    try:
        main(["session", "inspect", "--json"])
        frames = json.loads(capsys.readouterr().out)["frames"]
        caller = next(i for i, f in enumerate(frames) if f.get("name") == "main")
        main(["session", "get", "on_hand", "--frame", str(caller), "--json"])
        below = got(capsys)["on_hand"]
        # Once that call has returned (clang's `~Move` runs in two frames).
        assert main(["session", "continue", "--to", str(line_of(DEPOT, "S:%d"))]) == 0
        capsys.readouterr()
        main(["session", "get", "on_hand", "--json"])
        after = got(capsys)["on_hand"]
    finally:
        main(["session", "close"])
        capsys.readouterr()
    assert bool(re.fullmatch(r"size=2 \{.*\}", str(after.get("value")))) is reads, after
    if compiler == "gcc":
        assert below["error"] == BELOW_DESTRUCTOR, below
    elif reads:
        # The debugger's summary alone: its elements get no preview of their own (#181).
        assert below.get("value") == "size=2" and "Peeky" not in str(below.get("error")), below
    else:
        # Never "retry after that call returns" where it wouldn't read then either.
        assert below["error"] == after["error"] and "returns" not in below["error"], below


OPTIMIZED = """#include <cstdio>
#include <unordered_map>
__attribute__((noinline)) static void observe(int v) { std::printf("V:%d\\n", v); }
int main(int argc, char **) {
    std::unordered_map<int, int> um{{1, 2}};
    int &slot = um[1];
    for (int i = 0; i < 3; ++i) {
        slot += argc;
        observe(slot); // HERE
    }
    std::printf("UM:%zu %d\\n", um.size(), um[1]);
    return 0;
}
"""


@pytest.mark.timeout(300)
@pytest.mark.parametrize("optimization", ["O1", "O2"])
def test_no_node_is_read_in_an_optimized_build(cpp, optimization):
    # Review of #360: optimized, `slot += argc` may keep `um[1]`'s new value in a
    # register while its node and the count stay as they were, so the nodes would show
    # a stale value as `[memory]`. No entry is read from them in such a build.
    from peeky.native import NativeTarget
    from peeky.probe import Probe, run_probe

    compiler, root = cpp
    (root / "optimized.cpp").write_text(OPTIMIZED)
    adapter = NativeTarget(
        sources=("optimized.cpp",),
        language="cpp",
        compiler=os.environ["PEEKY_NATIVE_CXX"],
        adapter=os.environ["PEEKY_NATIVE_DAP"],
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
        optimization=optimization,
    )
    line = line_of(OPTIMIZED, "// HERE")
    probed = Probe("optimized.cpp", line, ("um", "um[1]", "um[#0]"), hits="all")
    result = run_probe(root, probed, adapter=adapter)
    assert result["status"] == "completed", result.get("error")
    assert "UM:1 5" in result["output"], result["output"]
    assert result["rows"], result
    for row in result["rows"]:
        assert row.get("view") != "memory", row
        if compiler == "gcc":
            # LLDB counts GCC's as empty: withheld, or no value at all here.
            assert row.get("value") is None, row


STALE = """#include <cstdio>
#include <unordered_map>
__attribute__((noinline, const)) static int twice(int x) {
    return x * 2; // IN_TWICE
}
[[using gnu: noinline, optimize("O2")]] static int bump(std::unordered_map<int, int> &m, int k) {
    int &slot = m[1];
    slot = k;
    int t = twice(k);
    slot = t;
    return t;
}
int main(int argc, char **) {
    std::unordered_map<int, int> um{{1, 2}};
    int got = bump(um, argc + 40);
    std::printf("UM:%zu %d %d\\n", um.size(), um[1], got);
    return 0;
}
"""


@pytest.mark.timeout(180)
def test_no_node_is_read_where_a_using_attribute_asks_gcc_to_optimize(cpp, capsys):
    # Review 2 of #360: `[[using gnu: optimize("O2")]]` escaped the detector, and GCC 13
    # optimizes `bump` under -O0. Stopped in `twice`, `bump` has stored 41 in `m[1]`, but
    # GCC drops that store (`twice` is `const`): `main`'s `um` read `size=1 {1: 2}` from
    # its node as `[memory]`.
    compiler, root = cpp
    source = root / "stale.cpp"
    source.write_text(STALE)
    assert main(["session", "start", str(source), "--at", str(line_of(STALE, "IN_TWICE"))]) == 0
    capsys.readouterr()
    try:
        main(["session", "inspect", "--json"])
        frames = json.loads(capsys.readouterr().out)["frames"]
        caller = next(i for i, f in enumerate(frames) if f.get("name") == "main")
        main(["session", "get", "um", "um[1]", "--frame", str(caller), "--json"])
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
    finally:
        main(["session", "close"])
        capsys.readouterr()
    assert all(row.get("view") != "memory" for row in rows.values()), rows
    if compiler == "gcc":
        assert withheld(rows["um"], 1) and withheld(rows["um[1]"], 1), rows


ASKED = """#include <cstdio>
#include <unordered_map>
{ask}
static int bump(std::unordered_map<int, int> &m) {{ return ++m[1]; }}
{after}
int main() {{
    std::unordered_map<int, int> um{{{{1, 2}}}};
    int got = bump(um);
    std::printf("UM:%zu %d\\n", um.size(), got); // HERE
    return 0;
}}
"""


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    "ask",
    [
        '__attribute__((noinline, optimize("O2")))',
        '#define FAST __attribute__((optimize("O2")))\nFAST __attribute__((noinline))',
        '#pragma GCC push_options\n#pragma GCC optimize ("O2")\n__attribute__((noinline))',
        '[[using gnu: noinline, optimize("O2")]]',
    ],
)
def test_no_node_is_read_where_the_source_asks_gcc_to_optimize_a_function(cpp, capsys, ask):
    # Review of #360: an unoptimized build whose source asks GCC to optimize `bump` (an
    # attribute, behind a macro too, or a pragma) may hold a value it stores in a
    # register, as an optimized build does: its nodes are not read.
    compiler, root = cpp
    after = "#pragma GCC pop_options" if "push_options" in ask else ""
    program = ASKED.format(ask=ask, after=after)
    source = root / "asked.cpp"
    source.write_text(program)
    code, result = probe(source, line_of(program, "// HERE"), "um,um[1]", capsys)
    assert "UM:1 3" in result["output"], result
    rows = {row["selector"]: row for row in result["rows"]}
    assert all(row.get("view") != "memory" for row in rows.values()), rows
    if compiler == "gcc":
        assert withheld(rows["um"], 1) and withheld(rows["um[1]"], 1), rows
    else:
        # Clang ignores GCC's attribute, and LLDB reads its build: as before.
        assert rows["um[1]"]["value"] == "3", rows


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
    # container does, and shows `ws[#0]` as the double 3.4584595208887258E-323. Since
    # #360, Peeky reads the `int` from the node itself.
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
        # libstdc++ here: LLDB's provider decodes `ws` and `ld` wrongly. Peeky reads `ws`'s
        # `int`s from its nodes (#360), and no `long double`.
        assert rows["ws"].get("view") == "memory" and rows["ws[#0]"]["value"] == "7", rows
        assert rows["ld"].get("value") is None and rows["ld[#0]"].get("value") is None, rows


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


@pytest.mark.timeout(240)
def test_a_string_set_is_made_in_the_buffer_the_debugger_misreads(cpp, capsys):
    # #167: with lldb-dap 20, GCC's string was refused as "its members are not laid out
    # as libstdc++'s string's are"; LLDB reads its `_M_local_buf` as `char[0]`. Then as
    # one whose characters cannot be set one by one: lldb-dap 20 has no writeMemory
    # request. Its text is now written with LLDB's `memory write`, and read back.
    compiler, root = cpp
    source = root / "text.cpp"
    source.write_text(TEXT)
    line = line_of(TEXT, "// OBSERVE")
    code, result = probe(source, line, "nm", capsys, "--set", 'nm="renamed"')
    if compiler == "gcc" and os.environ.get("PEEKY_EXPECT_READ_MEMORY"):
        assert code == 0, result.get("error")
    if code != 0:
        # lldb-dap 18 reads no memory, so it cannot tell where the text is.
        error = result["error"]
        assert error["code"] == "MUTATION_REJECTED", error
        assert error["message"].startswith(
            "`nm` cannot be changed: Peeky cannot tell whether its text is in the string "
            "itself or on the heap: this debugger cannot read memory (lldb-dap 20 can)"
        ), error
        # Never marked as one a set changes.
        assert result["rows"][0].get("settable") is not True, result["rows"]
        return
    assert "NM:[renamed]" in result["output"], result
    assert [(c["before"], c["after"]) for c in result["interventions"]] == [
        ('"named"', '"renamed"')
    ], result["interventions"]
    after = [row for row in result["rows"] if row["phase"] == "after-set"]
    assert after and after[0]["value"].endswith('"renamed"'), after
    # Marked as one a set changes, before it was.
    assert result["rows"][0].get("settable") is True, result["rows"]
    if compiler != "gcc" or sys.platform == "darwin":
        return
    # Its whole room, 15 bytes and the NUL, with a byte LLDB shows escaped.
    code, result = probe(source, line, "nm", capsys, "--set", 'nm="a\\tfull fifteen."')
    assert code == 0, result.get("error")
    assert "NM:[a\tfull fifteen.]" in result["output"], result["output"]
    code, result = probe(source, line, "nm", capsys, "--set", 'nm="sixteen bytes!!!"')
    assert code == 1 and result["error"]["message"].startswith(
        "`nm` cannot be changed: the text does not fit: `nm` holds its text in its own "
        "buffer, with room for 15 bytes, and the text is 16 bytes. Peeky never reallocates "
        "a string; a longer text needs an edit to the source"
    ), result.get("error")


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
        # Where its value is: what it binds, not the frame of some call (eval 13).
        assert "`a` is a structured binding by reference to `three`" in message, result
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
    "`{}` by reference, and Peeky proves only a local or its non-union member. Read it where "
    "the code names that object, or print it from the program"
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
    # A local of this frame: read through.
    for selector, value in (("a", "5"), ("b", "6")):
        assert hidden(compiler, rows[selector]) or rows[selector].get("value") == value, rows
    # `one(argc)`'s result, which `auto &&` keeps alive for its scope, but which the function
    # called may keep a pointer into (#297, review of #368 round 3): withheld.
    for selector in ("g", "h"):
        row = rows[selector]
        assert row.get("value") is None, row
        assert hidden(compiler, row) or row["error"] == unproven(selector, "`one(argc)`", CALLED), (
            row
        )
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
# (a range `for` over a map, a vector or a set; `v[1]`); and the same forms where the code
# may change the container, and a call's result, which is never proven.
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
    "READ": ("r,s", {1: {"r": "13", "s": "15"}}),
}


def unproven(selector, what, why):
    # Eval 13: never "bind by value", which breaks a loop that writes through the binding.
    element = re.fullmatch(r"an element of `(.+)`", what)
    instead = (
        f"Read `{element[1]}` itself, or print the value from the program"
        if element
        else "Read it where the code names that object, or print it from the program"
    )
    return (
        "not shown: the binding refers to an object Peeky can't prove is still alive: "
        f"`{selector}` binds {what} by reference, {why}. {instead}"
    )


# Review of #368, round 3: the function called may keep a pointer into its result.
CALLED = "a call's result, which the function called may keep a pointer into"


@pytest.mark.timeout(300)
def test_a_binding_into_a_local_container_reads_where_proven(cpp, capsys):
    # #297: these were all `not shown` (only a local, or a member of one, was proven), under
    # GCC and Apple clang alike: a range `for` over a local map, `v[1]` of a local vector.
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
    # Where the code may change the container, and a call's result: withheld.
    changed = {
        "c": (
            "an element of `w`",
            "and while it is in scope the code may change `w` or keep a pointer or reference "
            "into it",
        ),
        "e": ("`pick(w[1])`", CALLED),
        "g": (
            "an element of `n`",
            "and while it is in scope the code may change `n` or keep a pointer or reference "
            "into it",
        ),
        "p": ("`one(argc)`", CALLED),
        "y": ("`one(argc + 1)`", CALLED),
    }
    code, result = probe(
        source, line_of(HELD, "// CHANGED"), "c,d,e,f,g,h,p,q,y,z", capsys, "--hits", "1"
    )
    assert "U=" in result["output"], result
    for row in result["rows"]:
        assert row.get("value") is None, row
        if hidden(compiler, row):
            continue
        twin = {"d": "c", "f": "e", "h": "g", "q": "p", "z": "y"}
        what, why = changed[twin.get(row["selector"], row["selector"])]
        assert row["error"] == unproven(row["selector"], what, why), row


# Review of #368: a pointer into the container made before the binding, through which the
# element is destroyed before the stop and made again after it; a tuple-like type of the
# program's whose `get<I>` returns what a pointer in it points at; and names passed by value.
REVIEWED = """#include <array>
#include <cstddef>
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
Sink operator""_sink(unsigned long long) { return Sink{}; }
long operator+(long &x, Sink) { escaped = &x; return x; }
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
using Four = std::array<long, 4>;
Four *saved = nullptr;
__attribute__((noinline)) Four make() {
    Four made{141, 142, 143, 144};
    saved = &made;
    return made;
}
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
    std::map<long, long> lm{{161, 162}};
    for (auto &[lk, lv] : lm) {
        use(lv + 1_sink); // LITERAL
    }
    auto &&[na, nb, nc, nd] = make();
    std::destroy_at(saved);
    use(argc); // RETURNED
    new (saved) Four{145, 146, 147, 148};
    use(na + nb + nc + nd);
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
        # Review of #368, round 3: with the named return value optimization, `make()` builds
        # `made` where the caller keeps its result (returned in memory: 32 bytes), and keeps
        # a pointer into it, through which the result is destroyed before the stop.
        "RETURNED": ("na,nb", "`make()`", CALLED),
        # A user-defined literal, `1_sink`, of a class with `operator+(long &, Sink)`.
        "LITERAL": (
            "lk,lv",
            "an element of `lm`",
            "and while it is in scope the code may change `lm` or keep a pointer or reference "
            "into it",
        ),
        "DESTROYED": (
            "a,b",
            "an element of `v`",
            "and while it is in scope the code may change `v` or keep a pointer or reference "
            "into it",
        ),
        "PROXY": ("vx,vy", "`view(&held)`", CALLED),
    }
    for marker, (capture, what, why) in reasons.items():
        found = bound_rows_of(source, REVIEWED, marker, capture, capsys)
        assert len(found) == 2, (marker, found)
        for row in found:
            assert row.get("value") is None, row
            assert hidden(compiler, row) or row["error"] == unproven(row["selector"], what, why), (
                row
            )
    # Passed by value (`use(long)`, `printf`'s `...`): still read.
    for row in bound_rows_of(source, REVIEWED, "PASSED", "k,x", capsys):
        assert hidden(compiler, row) or row.get("value") == {"k": "71", "x": "72"}[row["selector"]]


# Review of #368, round 3: `std::allocator` calls the global `operator new`, which this
# program replaces to keep where each element is.
REPLACED = """#include <cstdio>
#include <cstdlib>
#include <map>
#include <new>
void *captured = nullptr;
void *operator new(std::size_t n) {
    void *p = std::malloc(n ? n : 1);
    if (p == nullptr) throw std::bad_alloc();
    captured = p;
    return p;
}
void operator delete(void *p) noexcept { std::free(p); }
void operator delete(void *p, std::size_t) noexcept { std::free(p); }
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; }
int main() {
    std::map<long, long> m{{151, 152}};
    for (const auto &[k, x] : m) {
        use(k * x); // CAPTURED
    }
    return 0;
}
"""


@pytest.mark.timeout(180)
def test_a_containers_element_is_withheld_where_the_program_replaces_operator_new(cpp, capsys):
    # Review of #368, round 3: the map's loop read `k = 151, x = 152`, though the program's
    # own `operator new` kept where the map's node is.
    compiler, root = cpp
    source = root / "replaced.cpp"
    source.write_text(REPLACED)
    found = bound_rows_of(source, REPLACED, "CAPTURED", "k,x", capsys)
    assert len(found) == 2, found
    why = (
        "and the program replaces `operator new` or `operator delete`, which may keep where "
        "each element is"
    )
    for row in found:
        assert row.get("value") is None, row
        assert hidden(compiler, row) or row["error"] == unproven(
            row["selector"], "an element of `m`", why
        ), row


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
        # As the row marks it (eval 13), with the lambda's own copy to change instead.
        assert result["error"]["code"] == "MUTATION_REJECTED", result
        assert result["error"]["message"] == (
            f"`bonus` cannot be changed at caps.cpp:{line}, at any stop of this build: in "
            "main::operator(), `bonus` is a capture of this lambda, so it is reached through a "
            "pointer or reference: GCC keeps it in the lambda's object, which this frame reads "
            "through `__closure`, and Peeky changes only values the frame holds itself. Change "
            "the lambda's own copy instead, at a stop in main once the lambda is made: "
            "`m.__bonus` is settable there, and is what the lambda reads where the code calls "
            "`m` itself. Refused before the program runs: nothing ran"
        ), result["error"]
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
    assert "`m.__n` is settable there" in refused[0], refused
    assert "`m.__pt.x` is settable there" in refused[1], refused
    # A capture by reference has no copy of its own: where it is owned.
    code, result = probe(source, line, "count", capsys, "--set", "count=5")
    assert result["error"]["message"].endswith(
        "`count` is a capture of this lambda, so it is reached through a pointer or reference: "
        "GCC keeps it in the lambda's object, which this frame reads through `__closure`. "
        "Change the value where it is owned: Peeky changes only values the frame holds itself. "
        "Refused before the program runs: nothing ran"
    ), result["error"]
    assert result["error"]["hint"].startswith("Change it where the message says"), result
    # Nothing changed.
    assert [rows["n"]["value"], rows["pt.x"]["value"]] == ["8", "3"], rows


PRICED = """#include <cstdio>
struct Tariff { long cents; long peak; };
int main() {
    Tariff tariff{30, 2000};
    long over = 0;
    auto price = [=, &over](long wh) {
        long extra = wh > tariff.peak ? wh - tariff.peak : 0;
        if (extra > 0)
            over++;
        return wh * tariff.cents + extra; // IN_PRICE
    };
    long total = 0;
    for (long wh = 1000; wh <= 3000; wh += 1000) {
        total += price(wh); // CALL
    }
    std::printf("T:%ld %ld\\n", total, over);
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_a_lambdas_copy_is_changed_where_it_is_held_and_the_refusal_says_so(cpp, capsys):
    # Eval 13 (bill): in a lambda that isn't `mutable`, a change to a capture by value
    # was refused as "`tariff` is const", while its rows say `[not settable: capture]`;
    # and no message said that the lambda's own copy (GCC's `price.__tariff`) is
    # settable in the function that holds it. Nor did `describe` list the captures, nor
    # say that GCC makes the lambda on its `};` line.
    compiler, root = cpp
    source = root / "priced.cpp"
    source.write_text(PRICED)
    inside, header = line_of(PRICED, "// IN_PRICE"), line_of(PRICED, "auto price")
    made, call = line_of(PRICED, "    };"), line_of(PRICED, "// CALL")
    code, result = probe(source, inside, "tariff", capsys, "--set", "tariff.cents=45")
    assert code != 0 and result["status"] == "failed", result
    if compiler == "gcc":
        message = result["error"]["message"]
        assert "is const" not in message, message
        assert message == (
            f"`tariff.cents` cannot be changed at priced.cpp:{inside}, at any stop of this "
            "build: in main::operator(), `tariff` is a capture of this lambda, so it is reached "
            "through a pointer or reference: GCC keeps it in the lambda's object, which this "
            "frame reads through `__closure`, and Peeky changes only values the frame holds "
            "itself. Change the lambda's own copy instead, at a stop in main once the lambda is "
            "made: `price.__tariff.cents` is settable there, and is what the lambda reads where "
            "the code calls `price` itself. Refused before the program runs: nothing ran"
        ), message
        # The route works: the lambda prices every reading at 45 from the first call.
        code, result = probe(
            source, call, "wh", capsys, "--hits", "1", "--set", "price.__tariff.cents=45"
        )
        assert code == 0 and "T:271000 1" in result["output"], result
        # `describe` lists the captures, with how each is captured.
        assert main(["describe", str(source)]) == 0
        described = capsys.readouterr().out
        assert re.search(
            r"names in main::operator\(\) \(priced\.cpp:[0-9-]+\): extra \(long int[^)]*\), "
            r"over \(long int &, capture by reference\), tariff \(Tariff, capture by "
            r"value\), wh \(long int\)",
            described,
        ), described
    else:
        assert "__closure" not in json.dumps(result), result
    # GCC builds the lambda on its `};` line, and gives its first line only the lambda's
    # prologue: a stop there, before the copy, changes what the lambda copies.
    code, result = probe(source, header, "wh", capsys, "--hits", "1")
    note = (
        f"Line {header} starts a lambda, and this build gives it only the lambda's prologue: "
        f"a stop there is in the lambda, on its next line with code. main makes the lambda on "
        f"line {made}: stop there, in main, to read or change what it captures by value "
        "before the lambda copies it."
    )
    assert (note in result.get("warnings", [])) is (compiler == "gcc"), result.get("warnings")
    if compiler == "gcc":
        code, result = probe(
            source, made, "tariff", capsys, "--hits", "1", "--set", "tariff.cents=45"
        )
        assert code == 0 and "T:271000 1" in result["output"], result
        assert result["rows"][0]["function"] == "main", result["rows"]


HELD_CAPTURES = """#include <cstdio>
struct Acc {
    long total = 5;
    long run(long k) {
        auto held = [this](long d) {
            return total + d; // IN_THIS
        };
        auto implicit = [=](long d) {
            return total + d + k; // IN_IMPLICIT
        };
        auto copied = [*this](long d) {
            return total + d; // IN_COPY
        };
        return held(1) + implicit(2) + copied(3);
    }
};
int main() {
    Acc acc;
    long n = 4;
    auto init = [m = n + 1, &r = n](long d) {
        return m + r + d; // IN_INIT
    };
    auto generic = [n](auto d) {
        return n + d; // IN_GENERIC
    };
    std::printf("A:%ld %ld %ld\\n", acc.run(1), init(2), generic(3L));
    return 0;
}
"""


@pytest.mark.timeout(180)
def test_describe_says_how_a_lambda_captures_this_and_each_name(cpp, capsys):
    # Review of #393: `[this]` (and an implicit `this`) looked like an ordinary local;
    # `[*this]`'s `this` showed the variable's `const Acc *const`, and twice.
    compiler, root = cpp
    source = root / "held.cpp"
    source.write_text(HELD_CAPTURES)
    assert main(["describe", str(source), "--json"]) == 0
    scopes = json.loads(capsys.readouterr().out)["scopes"]
    assert main(["describe", str(source)]) == 0
    text = capsys.readouterr().out
    if compiler != "gcc":
        # clang's lambda lists its object as `this`: no capture is said of a name.
        assert not any(scope.get("captures") for scope in scopes), scopes
        assert "capture by" not in text and "capture of the object" not in text, text
        return
    lambdas = {
        marker: next(
            scope
            for scope in scopes
            if scope["scope"].endswith(">") or "operator()" in scope["scope"]
            if scope["lines"][0] <= line_of(HELD_CAPTURES, marker) <= scope["lines"][1]
        )
        for marker in ("IN_THIS", "IN_IMPLICIT", "IN_COPY", "IN_INIT", "IN_GENERIC")
    }
    how = {
        marker: {name: shown["how"] for name, shown in scope["captures"].items()}
        for marker, scope in lambdas.items()
    }
    assert how == {
        "IN_THIS": {"this": "by reference"},
        "IN_IMPLICIT": {"k": "by value", "this": "by reference"},
        "IN_COPY": {"this": "by value"},
        "IN_INIT": {"m": "by value", "r": "by reference"},
        "IN_GENERIC": {"n": "by value"},
    }, how
    # The type the lambda's object holds, not the body's `const` variable's.
    assert lambdas["IN_COPY"]["captures"]["this"]["type"] == "Acc", lambdas["IN_COPY"]
    assert lambdas["IN_INIT"]["captures"]["m"]["type"] == "long int", lambdas["IN_INIT"]
    assert lambdas["IN_INIT"]["captures"]["r"]["type"] == "long int &", lambdas["IN_INIT"]
    # `this` once, as the capture of the object.
    copied = next(line for line in text.splitlines() if "this (Acc, " in line)
    assert copied.endswith("d (long int), this (Acc, capture of the object by value)"), copied
    assert copied.count("this (") == 1, copied
    assert "this (Acc *, capture of the object by reference)" in text, text
    assert "m (long int, capture by value), r (long int &, capture by reference)" in text, text
    generic = next(line for line in text.splitlines() if "operator()<" in line)
    assert generic.endswith("n (long int, capture by value)"), generic


BOUND_SET = """#include <cstdio>
#include <map>
struct P { long a, b; };
int main() {
    std::map<int, long> m{{1, 10}, {2, 20}};
    long sum = 0;
    for (auto &[k, v] : m) {
        v += k; // IN_LOOP
        sum += v;
    }
    P p{3, 4};
    auto &[a, b] = p;
    sum += a + b; // AFTER
    std::printf("B:%ld\\n", sum);
    return 0;
}
"""


@pytest.mark.timeout(240)
def test_a_change_to_a_binding_by_reference_says_where_its_value_is(cpp, capsys):
    # Eval 13 prep, finding 5: `--set v=5` on a range `for`'s binding by reference was
    # refused as "`v` is a reference", with the hint to change it "before the call", where
    # no call passed it: what it binds holds the value.
    compiler, root = cpp
    source = root / "bound.cpp"
    source.write_text(BOUND_SET)
    looped, after = line_of(BOUND_SET, "// IN_LOOP"), line_of(BOUND_SET, "// AFTER")
    code, result = probe(source, looped, "v", capsys, "--set", "v=5")
    assert code != 0 and result["error"]["code"] == "MUTATION_REJECTED", result
    assert result["error"]["message"] == (
        f"`v` cannot be changed at bound.cpp:{looped}, at any stop of this build: in main, "
        "`v` is a structured binding by reference to an element of `m`, so it is reached "
        "through a pointer or reference, and Peeky changes only values the frame holds itself: "
        "change what fills `m`, before the loop, or edit the source. Refused before the "
        "program runs: nothing ran"
    ), result["error"]
    hint = result["error"]["hint"]
    assert "call" not in hint and hint.startswith("Change what the binding refers to"), hint
    code, result = probe(source, after, "b", capsys, "--set", "b=5")
    assert result["error"]["message"] == (
        f"`b` cannot be changed at bound.cpp:{after}, at any stop of this build: in main, `b` "
        "is a structured binding by reference to `p`, so it is reached through a pointer or "
        "reference, and Peeky changes only values the frame holds itself: change `p` itself "
        "where it is settable, or edit the source. Refused before the program runs: nothing ran"
    ), result["error"]
    # As it says: `p.b` is settable there.
    code, result = probe(source, after, "b", capsys, "--set", "p.b=5")
    assert code == 0 and "B:41" in result["output"], result


CAPTURED_CONTAINERS = """#include <cstdio>
#include <unordered_map>
std::unordered_map<int, int> um;
static void touch() { um[7] = 8; um[9] = 10; um[11] = 12; }
int main() {
    touch();
    std::unordered_map<int, int> local{{1, 2}};
    std::unordered_map<int, int> copy{{3, 4}, {5, 6}};
    // `um` is a global of three entries too: a capture's name may shadow one.
    auto f = [&um = local, copy]() {
        return (int)um.size() + (int)copy.size(); // IN_LAMBDA
    };
    std::printf("F:%d %zu\\n", f(), um.size());
    return 0;
}
"""


@pytest.mark.timeout(180)
def test_a_lambdas_capture_of_an_unordered_container_reads_or_withholds_truthfully(cpp, capsys):
    # #360 with #361: a GCC lambda's capture of an unordered container, by reference and
    # by value, is its own container: read whole, or withheld with its own count (never
    # the shadowed global's 3), and never from nodes Peeky can't show are live.
    compiler, root = cpp
    source = root / "captured.cpp"
    source.write_text(CAPTURED_CONTAINERS)
    line = line_of(CAPTURED_CONTAINERS, "// IN_LAMBDA")
    # Clang's lambda reads its captures as members of its object, `this.um`.
    at = "this." if compiler == "clang" else ""
    names = ["um", "um[1]", "copy", "copy[3]"]
    code, result = probe(source, line, ",".join(at + name for name in names), capsys)
    assert "F:3 3" in result["output"], result
    rows = {row["selector"].removeprefix(at): row for row in result["rows"]}
    expected = {"um": ("size=1 {1: 2}", 1), "um[1]": ("2", 1), "copy[3]": ("4", 2)}
    for selector, (value, count) in expected.items():
        row = rows[selector]
        shown = row.get("value")
        assert shown is None or shown == value or shown.endswith(" " + value), (selector, row)
        if shown is None:
            assert withheld(row, count), (selector, row)
    copy = rows["copy"]
    if copy.get("value") is None:
        assert withheld(copy, 2), copy
    else:
        assert re.search(r"size=2 \{(3: 4, 5: 6|5: 6, 3: 4)\}$", copy["value"]), copy
    if compiler == "gcc":
        # LLDB counts GCC's as empty: whatever shows is Peeky's own read, never `size=0`.
        assert all("size=0" not in str(row.get("value")) for row in rows.values()), rows


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
