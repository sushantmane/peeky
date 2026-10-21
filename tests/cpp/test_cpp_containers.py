"""Standard containers previewed by their elements (#181), on real builds: libc++ on
macOS, libstdc++ on Linux. Each element is shown as the class template's argument in the
build's debug information says, read from the debugger's children (its formatter reads
`size()` of them from the container's own storage)."""

import contextlib
import json
import os
import re
import sys

import pytest

from peeky.cli import main

SHAPES = """#include <array>
#include <cstdio>
#include <deque>
#include <list>
#include <map>
#include <set>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>
struct Pt { int x; int y; };
struct Bag { std::vector<int> v; int n; };
using Row = std::vector<int>;
static int total(const std::vector<int> &ref, const Row *ptr) {
    return (int)ref.size() + (int)ptr->size(); // IN_TOTAL
}
int main() {
    std::vector<std::string> have{"users", "42", ""};
    std::vector<int> big(20, 5);
    std::vector<Pt> pts{{1, 2}, {3, 4}};
    std::vector<bool> bits{true, false, true};
    std::vector<std::vector<int>> grid{{1, 2}, {3}};
    std::vector<int> source{7, 8};
    std::vector<int> taken = std::move(source);
    Row row{1, 2, 3};
    std::array<int, 3> arr{4, 5, 6};
    std::map<int, int> m{{2, 20}, {1, 10}, {5, 50}};
    std::map<std::string, Pt> named{{"b", {3, 4}}, {"a", {1, 2}}};
    std::unordered_map<int, std::string> um{{1, "one"}, {2, "two"}};
    std::set<int> s{3, 1};
    std::multiset<int> ms{1, 1, 2};
    std::unordered_set<int> us{4};
    std::multimap<int, int> mm{{1, 2}, {1, 3}};
    std::deque<int> dq{1, 2};
    std::list<int> li{9, 8};
    Bag bag{{1, 2}, 3};
    int t = total(row, &row); // HERE
    std::printf("moved=%zu %zu %zu %zu %zu %zu %zu %d\\n", source.size(), have.size(), big.size(),
                pts.size() + bits.size() + grid.size() + taken.size() + arr.size(), m.size() +
                named.size() + um.size() + s.size(), ms.size() + us.size() + mm.size(),
                dq.size() + li.size(), t + bag.n);
    return 0;
}
"""
# What each shows on both libraries (the strings' where they are read whole).
EXPECTED = {
    "big": "size=20 [5, 5, 5, 5, 5, 5, 5, 5, …]",
    "pts": "size=2 [{x = 1, y = 2}, {x = 3, y = 4}]",
    "bits": "size=3 [true, false, true]",
    "grid": "size=2 [size=2 [1, 2], size=1 [3]]",
    # Moved from: empty in both libraries, as the program's own `size()` says.
    "source": "size=0 []",
    "taken": "size=2 [7, 8]",
    "row": "size=3 [1, 2, 3]",
    "arr": "size=3 [4, 5, 6]",
    # A map's entries in its order, key: value; a set's elements.
    "m": "size=3 {1: 10, 2: 20, 5: 50}",
    "s": "size=2 {1, 3}",
    "ms": "size=3 {1, 1, 2}",
    "us": "size=1 {4}",
    "dq": "size=2 [1, 2]",
    "li": "size=2 [9, 8]",
    "bag": "{v = size=2 [1, 2], n = 3}",
    "pts[1]": "{x = 3, y = 4}",
    "m[5]": "50",
}
TEXTS = {
    "have": 'size=3 ["users", "42", ""]',
    "named": 'size=2 {"a": {x = 1, y = 2}, "b": {x = 3, y = 4}}',
    'named["a"]': "{x = 1, y = 2}",
}


@pytest.fixture
def cpp(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def line_of(text, marker):
    return next(i for i, line in enumerate(text.splitlines(), 1) if marker in line)


def whole():
    """Whether strings read whole here: libc++'s summaries (macOS), or libstdc++'s bytes
    read from memory (a debugger with readMemory, as lldb-dap 20)."""
    return sys.platform == "darwin" or bool(os.environ.get("PEEKY_EXPECT_READ_MEMORY"))


def texts_match(shown, expected):
    """A preview with strings: as expected, or, where strings are not read whole (#128),
    with `<no value>` in place of any of them."""
    if shown == expected or whole():
        return shown == expected
    pattern = re.escape(expected)
    for literal in sorted(set(re.findall(r'"[^"]*"', expected)), key=len, reverse=True):
        escaped = re.escape(literal)
        pattern = pattern.replace(escaped, f"(?:{escaped}|<no value>)")
    return re.fullmatch(pattern, shown or "") is not None


@contextlib.contextmanager
def session(source, line, capsys):
    assert main(["session", "start", str(source), "--at", str(line)]) == 0
    capsys.readouterr()

    def get(*selectors, frame=0):
        main(["session", "get", *selectors, "--frame", str(frame), "--json"])
        return {row["selector"]: row for row in json.loads(capsys.readouterr().out)}

    try:
        yield get
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(120)
def test_probe_rows_show_a_containers_first_elements(cpp, capsys):
    # #181: `have` (`std::vector<std::string>`) showed only `size=3`; its `have[2] = ""`
    # was the bug's evidence. A struct holding a vector showed `{v = size=2, n = 3}`.
    source = cpp / "shapes.cpp"
    source.write_text(SHAPES)
    selectors = [*EXPECTED, *TEXTS, "um", "mm"]
    line = line_of(SHAPES, "// HERE")
    command = ["probe", str(source), "--at", str(line), "--capture", ",".join(selectors)]
    assert main([*command, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    rows = {row["selector"]: row.get("value") for row in result["rows"]}
    assert "moved=0 " in result["output"], result["output"]
    for selector, value in EXPECTED.items():
        assert rows[selector] == value, (selector, rows[selector])
    for selector, value in TEXTS.items():
        assert texts_match(rows[selector], value), (selector, rows[selector])
    # An unordered map's entries in its iteration order, whichever the library's is.
    assert any(
        texts_match(rows["um"], f"size=2 {{{a}, {b}}}")
        for a, b in (('1: "one"', '2: "two"'), ('2: "two"', '1: "one"'))
    ), rows["um"]
    # A multimap's keys repeat: its summary alone (#119 reads it by position).
    assert rows["mm"] == "size=2", rows["mm"]


@pytest.mark.timeout(120)
def test_session_rows_references_pointers_and_callers(cpp, capsys):
    source = cpp / "shapes.cpp"
    source.write_text(SHAPES)
    with session(source, line_of(SHAPES, "// IN_TOTAL"), capsys) as get:
        rows = get("ref", "ptr", "ptr[0]")
        # A reference: its address, then the container; a pointer only its address,
        # and what it points at where a selector asks (`ptr[0]`).
        ref = rows["ref"]["value"]
        assert re.fullmatch(r"0x[0-9a-f]+ size=3 \[1, 2, 3\]", ref), ref
        assert re.fullmatch(r"0x[0-9a-f]+ size=3", rows["ptr"]["value"]), rows["ptr"]
        assert rows["ptr[0]"]["value"] == "size=3 [1, 2, 3]", rows["ptr[0]"]
        # A caller's container, below a call of the program's own.
        rows = get("big", "m", frame=1)
        assert rows["big"]["value"] == EXPECTED["big"], rows["big"]
        assert rows["m"]["value"] == EXPECTED["m"], rows["m"]
    with session(source, line_of(SHAPES, "// HERE"), capsys) as get:
        rows = get("m", "named")
        # A map's entries keep #119's key selectors, now with their values' previews.
        listed = {c["selector"]: c["value"] for c in rows["m"]["children"]}
        assert listed == {"m[1]": "10", "m[2]": "20", "m[5]": "50"}, listed
        listed = {c["selector"]: c["value"] for c in rows["named"]["children"]}
        assert listed.get('named["b"]', "{x = 3, y = 4}") == "{x = 3, y = 4}", listed
        if whole():
            assert listed == {'named["a"]': "{x = 1, y = 2}", 'named["b"]': "{x = 3, y = 4}"}


MIDWAY = """#include <cstdio>
#include <string>
#include <vector>
struct Owner;
struct Noisy {
    std::string name;
    Owner *owner;
    Noisy(std::string n, Owner *o) : name(std::move(n)), owner(o) {}
    ~Noisy();
};
struct Owner { std::vector<Noisy> items; int dropped = 0; };
Noisy::~Noisy() {
    if (owner) owner->dropped++; // IN_DTOR
}
static int count(const Owner &owner) {
    return (int)owner.items.size(); // IN_COUNT
}
int main() {
    Owner o;
    o.items.reserve(4);
    o.items.emplace_back(std::string(30, 'a'), &o);
    o.items.emplace_back(std::string(30, 'b'), &o);
    int before = count(o);
    o.items.clear();
    std::printf("%d %d\\n", before, o.dropped);
    return 0;
}
"""


@pytest.mark.timeout(180)
def test_a_container_a_library_call_is_changing_keeps_its_summary(cpp, capsys):
    # #181: during `o.items.clear()`, the second element's destructor runs while the
    # vector still counts the first, already destroyed (its string's memory freed): the
    # debugger lists it, but no preview shows it. Below calls of the program's own, the
    # same vector reads.
    source = cpp / "midway.cpp"
    source.write_text(MIDWAY)
    with session(source, line_of(MIDWAY, "// IN_COUNT"), capsys) as get:
        rows = get("owner.items")
        items = rows["owner.items"]["value"]
        assert items.startswith('size=2 [{name = "aaaa') or not whole(), items
        assert items.startswith("size=2 [{name = "), items
        rows = get("o.items", frame=1)
        assert rows["o.items"]["value"] == items, rows["o.items"]
    with session(source, line_of(MIDWAY, "// IN_DTOR"), capsys) as get:
        assert main(["session", "continue"]) == 0
        capsys.readouterr()
        main(["session", "inspect", "--json"])
        frames = json.loads(capsys.readouterr().out)["frames"]
        caller = next(i for i, f in enumerate(frames) if f.get("name") == "main")
        assert any("std::" in f.get("name", "") for f in frames[:caller]), frames
        rows = get("o.items", frame=caller)
        shown = rows["o.items"]
        assert shown["value"] == "size=2", shown
        # Nor its elements, one of which is destroyed.
        for child in shown["children"]:
            assert re.fullmatch(r"Noisy @ 0x[0-9a-f]+", child["value"]), child
        # Reached through a pointer from the destructor's own frame: the same.
        rows = get("this.owner[0].items")
        assert rows["this.owner[0].items"]["value"] == "size=2", rows


GUARDS = """#include <cstdio>
#include <vector>
int main() {
    int total = 0;
    for (int pass = 0; pass < 3; pass++) {
        total += pass; // TOP
        std::vector<int> got(pass + 2, pass); // DECL
        total += (int)got.size(); // USE
    }
    std::printf("%d\\n", total);
    return 0;
}
"""


@pytest.mark.timeout(180)
def test_a_container_not_yet_constructed_or_in_an_optimized_build_is_not_previewed(cpp, capsys):
    source = cpp / "guards.cpp"
    source.write_text(GUARDS)
    probe = ["probe", str(source), "--capture", "got", "--hits", "3", "--json"]
    # Before its declaration has run, a later pass's storage holds the last pass's
    # vector, destroyed (#121, #140): withheld, never previewed.
    for marker in ("// TOP", "// DECL"):
        assert main([*probe, "--at", str(line_of(GUARDS, marker))]) == 0
        for row in json.loads(capsys.readouterr().out)["rows"]:
            assert row.get("unavailable") and "value" not in row, row
    assert main([*probe, "--at", str(line_of(GUARDS, "// USE"))]) == 0
    values = [row["value"] for row in json.loads(capsys.readouterr().out)["rows"]]
    assert values == ["size=2 [0, 0]", "size=3 [1, 1, 1]", "size=4 [2, 2, 2, 2]"], values
    # An optimized build: the debugger's summary alone, as for a struct (#136).
    config = cpp / "native.json"
    config.write_text(
        json.dumps(
            {
                "language": "cpp",
                "sources": ["guards.cpp"],
                "compiler": os.environ["PEEKY_NATIVE_CXX"],
                "adapter": os.environ["PEEKY_NATIVE_DAP"],
                "dwarfdump": os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
                "dsymutil": os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
                "sysroot": os.environ.get("PEEKY_NATIVE_SYSROOT"),
                "optimization": "O1",
            }
        )
    )
    at = f"guards.cpp:{line_of(GUARDS, '// USE')}"
    command = ["probe", str(cpp), "--config", str(config), "--at", at, "--capture", "got"]
    assert main([*command, "--hits", "3", "--json"]) == 0
    for row in json.loads(capsys.readouterr().out)["rows"]:
        assert "[" not in str(row.get("value")), row


THREADS = """#include <atomic>
#include <cstdio>
#include <thread>
#include <vector>
int main() {
    std::atomic<bool> go{false};
    std::vector<int> shared{1, 2, 3};
    std::thread worker([&] { while (!go.load()) std::this_thread::yield(); shared.push_back(4); });
    int n = (int)shared.size(); // HERE
    go = true;
    worker.join();
    std::printf("%d %zu\\n", n, shared.size());
    return 0;
}
"""


@pytest.mark.timeout(120)
def test_a_program_running_several_threads_shows_container_summaries(cpp, capsys):
    # #181: a stop pauses every thread wherever it is; another one may be inside a
    # `push_back` on a container this frame holds or shares (`shared`, which the worker
    # thread changes through its capture).
    source = cpp / "threads.cpp"
    source.write_text(THREADS)
    line = line_of(THREADS, "// HERE")
    command = ["probe", str(source), "--at", str(line), "--capture", "shared,go", "--json"]
    assert main(command) == 0
    rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    assert rows["shared"]["value"] == "size=3", rows["shared"]
