"""The sixth agent evaluation's C++ findings (15-17), on real builds: a string lists no
library members, a C++ frame's signature does not widen tables, and a block's locals
are not shown as visible on lines between its code and its cleanup."""

import contextlib
import json
import os

import pytest

from peeky.cli import main

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
ARGS = ["--arg", "apple:3, pear:2, apple :4"]

TEXTS = """#include <cstdio>
#include <map>
#include <string>
#include <vector>
using Name = std::string;
typedef std::string *StrPtr;
struct Rec { std::string name; int id; };
int main() {
    std::string name("apple");
    const std::string &ref = name;
    std::string *ptr = &name;
    StrPtr sp = &name;
    Name tn("typed");
    Rec rec{"r", 1};
    std::vector<std::string> vs{"a", "b"};
    std::map<std::string, std::string> ms{{"k", "v"}};
    size_t n = name.size() + ref.size() + ptr->size() + sp->size() + tn.size() + rec.name.size();
    std::printf("%zu %zu %zu\\n", n, vs.size(), ms.size()); // HERE
    return 0;
}
"""

# Two overloads on one line: one probe stops in both.
OVERLOADS = """#include <cstdio>
#include <string>
static int twice(int x) { return 2 * x; } static int twice(const std::string &s) { return 2 * (int)s.size(); }
int main() {
    int n = twice(3) + twice(std::string("ab"));
    std::printf("%d\\n", n);
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


def shows(row, text):
    """Whether a string's row shows ``text``, or, where it cannot (lldb-dap 18: LLDB's
    summary of a libstdc++ `std::string` fails, and no memory is read), says how many
    bytes it holds (#128)."""
    value = row.get("value") or ""
    return value.endswith(text) or "it holds" in (row.get("error") or value)


def line_of(text, marker):
    return next(i for i, line in enumerate(text.splitlines(), 1) if marker in line)


@contextlib.contextmanager
def session(source, line, capsys, *extra):
    assert main(["session", "start", str(source), "--at", str(line), *extra]) == 0
    started = capsys.readouterr().out

    def get(*selectors, text=False):
        main(["session", "get", *selectors, *([] if text else ["--json"])])
        out = capsys.readouterr().out
        return out if text else {row["selector"]: row for row in json.loads(out)}

    try:
        yield get, started
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(120)
def test_a_string_shows_its_text_and_no_library_members(cpp, capsys):
    # Eval 6, finding 15: `session get name` listed `[not selectable]:
    # std::basic_string<char>::(anonymous struct) = ... @ 0x16b24f238` (libc++), and
    # `_M_dataplus`, `_M_string_length` and an unnamed union on Linux (libstdc++).
    source = cpp("stock.cpp", STOCK)
    with session(source, 24, capsys, *ARGS) as (get, _):
        text = get("name", "entry", text=True)
        assert text.splitlines()[0].startswith("name: std::string = "), text
        assert "[not selectable]" not in text and "_M_" not in text, text
        assert len(text.splitlines()) == 2, text
        assert shows(get("name")["name"], '"apple"')
        main(["session", "inspect"])
        listed = capsys.readouterr().out
        assert "(children: peeky session get name)" not in listed, listed
        # A struct or container still lists what it holds.
        assert "(children: peeky session get stock)" in listed, listed
    source = cpp("texts.cpp", TEXTS)
    with session(source, line_of(TEXTS, "// HERE"), capsys) as (get, _):
        rows = get("name", "ref", "ptr", "ptr[0]", "tn", "rec", "vs", "ms", "ms[#0]")
        for selector in ("name", "ref", "ptr", "ptr[0]", "tn"):
            assert not rows[selector].get("children"), rows[selector]
            assert shows(rows[selector], '"typed"' if selector == "tn" else '"apple"')
        # Its members are the library's, as an element's or a field's.
        for selector, child in (("rec", "rec.name"), ("vs", "vs[0]"), ("ms", 'ms["k"]')):
            listed = {c.get("selector"): c for c in rows[selector]["children"]}
            assert not listed[child].get("expandable"), listed[child]
        assert {c.get("selector") for c in rows["rec"]["children"]} == {"rec.name", "rec.id"}
        first = {c.get("selector"): c for c in rows["ms[#0]"]["children"]}["ms[#0].first"]
        assert first["value"] == '"k"' and not first.get("expandable"), first
        # Selecting inside one says why, and what to do instead.
        # Element 0 however written (#117's `parr[00]`), through a typedef'd pointer too
        # (review of the eval-6 fixes: `ptr[00]` was refused as past the pointee).
        pointed = get("ptr[00]", "sp[0]", "sp[00]")
        for selector, row in pointed.items():
            assert "error" not in row and shows(row, '"apple"'), row
            assert row["value"] == rows["ptr[0]"]["value"], row
        errors = get("name[0]", "name.size", "ptr[1]")
        if rows["name"].get("unavailable"):
            # lldb-dap 18: no text to compare, and the reason says so.
            assert "it holds 5 bytes" in errors["name[0]"]["error"], errors["name[0]"]
        else:
            assert "is a C++ string, shown whole as its text" in errors["name[0]"]["error"]
            assert "capture name and compare its text" in errors["name.size"]["error"]
        assert "ptr[0] is the std::string it points at" in errors["ptr[1]"]["error"]


@pytest.mark.timeout(120)
def test_a_cpp_frames_signature_does_not_widen_the_table(cpp, capsys):
    # Eval 6, finding 16: the function column read `trim_left(std::__1::basic_string<char,
    # std::__1::char_traits<char>, std::__1::allocator<char>> const&)`, so the table was
    # about 200 columns wide. JSON and the evidence store keep the whole name.
    source = cpp("stock.cpp", STOCK)
    assert main(["probe", str(source), "--at", "9", *ARGS, "--capture", "i", "--hits", "2"]) == 0
    text = capsys.readouterr().out
    assert "trim_left " in text and "basic_string" not in text, text
    assert max(len(line) for line in text.splitlines() if line.startswith(("hit", "1 "))) < 80
    # Review of the eval-6 fixes: SQL compares the stored name, which `WHERE function =
    # 'trim_left'` does not match; the probe's table says how to match it.
    assert "note: function shows short frame names" in text, text
    assert "short_function(function) = 'trim_left'" in text, text
    assert main(["sql", "SELECT count(*) AS n FROM last_run WHERE function = 'trim_left'"]) == 0
    assert "\n0" in capsys.readouterr().out
    assert main(["sql", "SELECT DISTINCT function FROM last_run", "--json"]) == 0
    (stored,) = [row["function"] for row in json.loads(capsys.readouterr().out)["rows"]]
    assert stored.startswith("trim_left(") and "basic_string" in stored, stored
    assert main(["queries", "run"]) == 0
    text = capsys.readouterr().out
    assert "trim_left " in text and "basic_string" not in text, text
    assert "note: function shows short frame names" in text, text
    query = "SELECT count(*) AS n FROM last_run WHERE short_function(function) = 'trim_left'"
    assert main(["sql", query, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == [{"n": 2}]
    # Two overloads in one table keep their parameters, so they stay apart.
    source = cpp("overloads.cpp", OVERLOADS)
    assert main(["probe", str(source), "--at", "3", "--capture", "x", "--hits", "all"]) == 0
    text = capsys.readouterr().out
    assert "twice(int)" in text and "twice(std::string const&)" in text, text
    assert "basic_string" not in text, text
    # A probe's table says the note once per state directory (seventh evaluation,
    # finding 8); a query's table always does. Its example is what short_function
    # gives: both overloads' rows.
    assert "note: function shows short frame names" not in text, text
    assert main(["queries", "run"]) == 0
    text = capsys.readouterr().out
    assert "short_function(function) = 'twice'" in text, text
    query = "SELECT count(*) AS n FROM last_run WHERE short_function(function) = 'twice'"
    assert main(["sql", query, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == [{"n": 2}]
    # A session names the frame with its parameters, as code writes their types.
    with session(source, 3, capsys, "--hit", "2") as (_, started):
        assert " in twice(std::string const&), hit 2" in started, started


@pytest.mark.timeout(120)
def test_a_blocks_locals_are_not_listed_on_lines_outside_it(cpp, capsys):
    # Eval 6, finding 17: `describe` gave `name (lines 22-35)` for the `if` block's local
    # and `colon (lines 17-35)` for the `while` body's: C++ puts a block's cleanup code
    # (run when an exception leaves it) on the function's closing line, and the span ran
    # from the block to there. The lines between, where code outside the block runs,
    # are not the block's; its own lines, and that closing line, still are.
    source = cpp("stock.cpp", STOCK)
    assert main(["describe", str(source), "--json"]) == 0
    scopes = json.loads(capsys.readouterr().out)["scopes"]
    spans = next(scope for scope in scopes if scope["scope"] == "main")["spans"]

    def lines(name):
        return {line for first, last in spans[name] for line in range(first, last + 1)}

    for name in ("name", "qty"):
        assert {22, 23, 24} <= lines(name) and not lines(name) & set(range(26, 35)), spans
    for name in ("comma", "entry", "colon"):
        assert set(range(17, 25)) <= lines(name), spans
        assert not lines(name) & set(range(28, 35)), spans
    assert main(["describe", str(source)]) == 0
    text = capsys.readouterr().out
    assert "lines 22-35" not in text and "lines 17-35" not in text, text
    assert ", lines 22-2" in text, text


@pytest.mark.timeout(240)
def test_a_value_shortened_at_one_line_is_restored_at_the_next_in_one_probe(cpp, capsys):
    # #137 (sixth evaluation, `stock`): the agent shortened `colon` at line 22 for the
    # names with a trailing space and restored it at line 23, so `std::stol` still read
    # the quantity: 14 session calls, as one `--set colon=` crashed at line 23.
    source = cpp("stock.cpp", STOCK)
    name = line_of(STOCK, "std::string name = trim_left")
    qty = line_of(STOCK, "long qty = ")
    sets = []
    for hit, short, whole in ((3, 6, 7), (4, 4, 5)):
        sets += ["--set-at", f"stock.cpp:{name}@{hit}", f"colon={short}"]
        sets += ["--set-at", f"stock.cpp:{qty}@{hit}", f"colon={whole}"]
    argv = ["probe", str(source), "--at", str(name), "--capture", "colon", "--hits", "all"]
    code = main([*argv, *sets, "--json", "--quiet", "--", "apple:3, pear:2, apple :4,pear :1"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert "STOCK:apple=7,pear=3" in result["output"], result["output"]
    assert [
        (change["before"], change["after"], change["line"], change["hit"])
        for change in result["interventions"]
    ] == [("7", "6", name, 3), ("6", "7", qty, 3), ("5", "4", name, 4), ("4", "5", qty, 4)]


LAMBDA = """#include <cstdio>
int main() {
    long total = 0;
    for (long i = 0; i < 3; i++) {
        long v = i * 10;
        auto f = [](long v) { return v + 1; }; total += f(v); // BOTH
    }
    std::printf("T %ld\\n", total); // END
}
"""


@pytest.mark.timeout(240)
def test_a_set_at_line_that_stops_in_a_lambda_says_whose_variable_changed(cpp, capsys):
    # #151 review: line BOTH stops in `main` and in the lambda it declares, and @HIT counts
    # both, so `6@2` changed the lambda's `v`; nothing said so.
    source = cpp("lam.cpp", LAMBDA)
    both, end = line_of(LAMBDA, "BOTH"), line_of(LAMBDA, "END")
    argv = ["probe", str(source), "--at", str(end), "--capture", "total"]
    assert main([*argv, "--set-at", f"{both}@2", "v=100", "--quiet"]) == 0
    text = capsys.readouterr().out
    assert f"set v: 0 -> 100  (--set-at line {both}@2, in main::$_0::operator())" in text, text
    assert f"warning: --set-at line {both} stopped in 2 functions: main (stop 1); main::" in text
    assert "T 133" in text
    assert main(["sql", "SELECT name, line, hit, function FROM interventions", "--json"]) == 0
    (stored,) = json.loads(capsys.readouterr().out)["rows"]
    assert stored["line"] == both and stored["hit"] == 2
    assert stored["function"].startswith("main::$_0::operator()"), stored
    # main's own `v` on the second pass is that line's third stop.
    assert main([*argv, "--set-at", f"{both}@3", "v=100", "--json", "--quiet"]) == 0
    result = json.loads(capsys.readouterr().out)
    (change,) = result["interventions"]
    assert change["function"] == "main" and change["before"] == "10", change
    assert "T 123" in result["output"], result["output"]
