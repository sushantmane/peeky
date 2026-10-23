"""C++ strings with a NUL byte (#128): shown whole, or with a note, never cut silently.

LLDB's summary of a libstdc++ (Linux) `std::string` is a C string, which ends at the
first NUL: `"a\\0b"` showed as `"a"`. A `std::string` LLDB names by that typedef got
`error: summary string parsing error` instead. libc++ (macOS) prints every byte, so
there the values are LLDB's own, unchanged. Where the debugger reads memory (lldb-dap
20), the worker reads a libstdc++ string's bytes; elsewhere it adds a note.

Built by clang and by g++ (#167). GCC describes `std::string` whole, where clang's default
leaves it bare, so with lldb-dap 18 LLDB's summary of a GCC build works: it ends at the
first NUL, and Peeky says where (`"a" (shown up to a NUL at byte 1 of 3)`).
"""

import contextlib
import json
import os
import sys

import pytest

STRINGS = """#include <cstdio>
#include <map>
#include <string>
#include <vector>
struct Rec { std::string name; int id; };
int main() {
    std::string sso("a\\0b", 3);
    std::string heap("abcdefghijklmnopqrstuvwxyz\\0tail", 31);
    std::string trail("q\\0", 2);
    std::string plain("hello");
    std::string grown(100, 'x');
    grown.assign("x\\0y", 3);
    Rec rec{std::string("r\\0s", 3), 7};
    std::vector<std::string> vs{std::string("v\\0w", 3), "ok"};
    std::map<int, std::string> ms{{1, std::string("m\\0n", 3)}};
    std::map<std::string, int> keys{{std::string("k\\0x", 3), 1}, {"zz", 2}};
    const std::string &ref = sso;
    std::string *ptr = &heap;
    std::wstring wide(L"a\\0b", 3);
    std::u16string u16(u"a\\0b", 3);
    size_t n = sso.size() + heap.size() + trail.size() + plain.size() + grown.size();
    n += rec.name.size() + vs.size() + ms.size() + keys.size() + ref.size() + ptr->size();
    std::printf("%zu %zu\\n", n, wide.size() + u16.size()); // OBSERVE
    return 0;
}
"""
LINE = next(i for i, text in enumerate(STRINGS.splitlines(), 1) if "// OBSERVE" in text)
# What LLDB's libc++ summary shows (macOS), and Peeky shows for libstdc++ where it reads
# the bytes from memory.
WHOLE = {
    "sso": '"a\\0b"',
    "heap": '"abcdefghijklmnopqrstuvwxyz\\0tail"',
    "trail": '"q\\0"',
    "plain": '"hello"',
    "grown": '"x\\0y"',
    "rec.name": '"r\\0s"',
    "vs[0]": '"v\\0w"',
    "vs[1]": '"ok"',
    "ms[1]": '"m\\0n"',
    "ptr[0]": '"abcdefghijklmnopqrstuvwxyz\\0tail"',
    "wide": 'L"a\\0b"',
    "u16": 'u"a\\0b"',
}
# What GCC's builds show where the debugger reads no memory (lldb-dap 18): LLDB's summary,
# which ends at the first NUL, and where it ends.
CUT = {
    "sso": '"a" (shown up to a NUL at byte 1 of 3)',
    "heap": '"abcdefghijklmnopqrstuvwxyz" (shown up to a NUL at byte 26 of 31)',
    "trail": '"q" (shown up to a NUL at byte 1 of 2)',
    "plain": '"hello"',
    "grown": '"x" (shown up to a NUL at byte 1 of 3)',
    "wide": 'L"a" (shown up to a NUL at character 1 of 3)',
}


@pytest.fixture
def strings(cxx, tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "strings.cpp"
    source.write_text(STRINGS)
    return source


@contextlib.contextmanager
def session(source, capsys):
    from peeky.cli import main

    assert main(["session", "start", str(source), "--at", str(LINE)]) == 0
    capsys.readouterr()

    def get(*selectors):
        main(["session", "get", *selectors, "--json"])
        return {row["selector"]: row for row in json.loads(capsys.readouterr().out)}

    try:
        yield get
    finally:
        main(["session", "close"])
        capsys.readouterr()


def whole(row):
    """Whether this run shows strings whole: libc++'s summaries (macOS), or libstdc++'s
    bytes read from memory (a debugger with readMemory, as lldb-dap 20)."""
    return (
        sys.platform == "darwin"
        or bool(os.environ.get("PEEKY_EXPECT_READ_MEMORY"))
        or row.get("view") == "memory"
    )


def test_a_string_with_a_nul_is_never_shown_cut_short(strings, cxx, capsys):
    # #128: on Linux `sso` ("a\0b", in the local buffer), `heap` (31 bytes on the heap),
    # `trail` ("q\0") and `grown` (3 bytes in a heap buffer) showed LLDB's
    # `error: summary string parsing error`, and a `std::basic_string<char>` showed "a".
    with session(strings, capsys) as get:
        rows = get("sso", "heap", "trail", "plain", "grown")
        for name, row in rows.items():
            if whole(row):
                assert row["value"] == WHOLE[name], row
                if sys.platform == "darwin":
                    # libc++'s values are LLDB's own, as before.
                    assert "view" not in row, row
            elif cxx == "gcc":
                # LLDB's summary of GCC's string, cut at the first NUL: said.
                assert row["value"] == CUT[name], row
            else:
                # No readMemory: the debugger's text is never passed off as the value.
                assert row.get("unavailable") and "summary failed" in row["error"], row
                length = {"sso": 3, "heap": 31, "trail": 2, "plain": 5, "grown": 3}[name]
                assert f"it holds {length} bytes" in row["error"], row


def test_a_string_inside_a_struct_a_vector_or_a_map_is_never_cut_short(strings, cxx, capsys):
    # #128: `vs[0]` and `ms[1]` (LLDB names them `std::basic_string<char>`) showed "v"
    # and "m" on Linux, with no note; `rec.name` showed the summary's parsing error.
    with session(strings, capsys) as get:
        rows = get("rec.name", "vs[0]", "vs[1]", "ms[1]", "rec", "vs", "ms")
        for name in ("rec.name", "vs[0]", "vs[1]", "ms[1]"):
            row = rows[name]
            if whole(row):
                assert row["value"] == WHOLE[name], row
            elif name == "rec.name" and cxx == "clang":
                assert row.get("unavailable") and "it holds 3 bytes" in row["error"], row
            elif name == "vs[1]":
                assert row["value"] == '"ok"', row
            else:
                text = WHOLE[name][:2] + '"'
                assert row["value"] == f"{text} (shown up to a NUL at byte 1 of 3)", row
        # A listing shows each child as `get` reads it.
        listed = {
            child["selector"]: child
            for name in ("rec", "vs", "ms")
            for child in rows[name]["children"]
        }
        for name in ("rec.name", "vs[0]", "ms[1]"):
            assert listed[name].get("value") == rows[name].get("value"), (name, listed[name])


def test_a_strings_pointer_reference_and_wide_string_are_never_cut_short(strings, cxx, capsys):
    # #128: `ref` and `ptr` showed their address and the summary's parsing error; `wide`
    # (an std::wstring) too, and `u16` (std::u16string) showed no text where it can.
    with session(strings, capsys) as get:
        rows = get("ref", "ptr", "ptr[0]", "wide", "u16")
        if whole(rows["ptr[0]"]):
            assert rows["ref"]["value"].endswith(" " + WHOLE["sso"]), rows["ref"]
            assert rows["ptr"]["value"].endswith(" " + WHOLE["heap"]), rows["ptr"]
            assert rows["ptr[0]"]["value"] == WHOLE["heap"]
        elif cxx == "gcc":
            assert rows["ref"]["value"].endswith(" " + CUT["sso"]), rows["ref"]
            assert rows["ptr"]["value"].endswith(" " + CUT["heap"]), rows["ptr"]
            assert rows["ptr[0]"]["value"] == CUT["heap"]
        else:
            assert "<no text: the string's summary failed" in rows["ref"]["value"]
            assert "it holds 31 bytes>" in rows["ptr"]["value"]
            assert "it holds 31 bytes>" in rows["ptr[0]"]["value"]
        assert rows["ref"]["value"].startswith("0x") and rows["ptr"]["value"].startswith("0x")
        wide = rows["wide"]
        if sys.platform == "darwin" or wide.get("view") == "memory":
            assert wide["value"] == WHOLE["wide"], wide
        elif cxx == "gcc":
            assert wide["value"] == CUT["wide"], wide
        else:
            # This build leaves libstdc++'s std::wstring without members: no length to
            # check, and LLDB's summary of it fails.
            assert wide.get("unavailable") and "summary failed" in wide["error"], wide
        u16 = rows["u16"]
        if whole(u16):
            assert u16["value"] == WHOLE["u16"], u16
        else:
            # LLDB has no libstdc++ summary for it: no text to cut short.
            assert u16["value"].startswith("std::u16string @ "), u16


def test_a_probe_and_its_evidence_keep_the_whole_string(strings, capsys):
    # #128: probe rows, and the evidence store they are saved to, held "v" for "v\0w".
    from peeky.cli import main

    capture = "vs[0],ms[1],keys['zz'],keys['k']"
    assert main(["probe", str(strings), "--at", str(LINE), "--quiet", "--capture", capture]) == 0
    capsys.readouterr()
    assert main(["sql", "SELECT selector, value, view, error FROM last_run", "--json"]) == 0
    saved = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    for name in ("vs[0]", "ms[1]"):
        if whole(saved[name]):
            assert saved[name]["value"] == WHOLE[name], saved[name]
        else:
            assert saved[name]["value"].endswith("(shown up to a NUL at byte 1 of 3)")
    # A key "k\0x" is no key "k", however it is shown (#119).
    assert saved["keys['zz']"]["value"] == "2"
    assert 'no key "k"' in saved["keys['k']"]["error"], saved["keys['k']"]


# Review of #128 (PR #131): strings by a typedef's name, digits after a NUL, long text.
NAMED = """#include <cstdio>
#include <string>
#include <vector>
typedef std::basic_string<char> Bytes;
#if defined(__GLIBCXX__)
using Bytes2 = std::__cxx11::basic_string<char>;
#else
using Bytes2 = std::basic_string<char>;
#endif
using Name = std::string;
template <class T> struct Hold { using S = T; };
struct Box { Bytes b; };
struct Person { Name name; int age; };
static size_t take(const Bytes &br, const Bytes *bp, const Name &nref) {
    size_t n = br.size() + bp->size() + nref.size();
    return n; // IN_TAKE
}
int main() {
    Bytes b(std::string("b\\0y", 3));
    Bytes2 b2(std::string("c\\0y", 3));
    Box bx{Bytes(std::string("x\\0b", 3))};
    Name tn(std::string("t\\0n", 3));
    Person per{Name("p\\0e", 3), 3};
    Hold<std::string>::S hs(std::string("h\\0s", 3));
    Hold<int>::S hi = 4;
    std::vector<std::string> v;
    v.push_back(std::string("n\\0" "64", 4));
    v.push_back(std::string("\\xff" "a", 2));
    v.push_back(std::string(500, '\\x01') + std::string("\\0", 1) + "z");
    std::string digits("x\\0" "5", 3);
    size_t n = take(b, &b, tn) + b2.size() + bx.b.size() + per.name.size() + hs.size() + hi;
    std::printf("%zu %zu %zu\\n", n, v.size(), digits.size()); // OBSERVE
    return 0;
}
"""
NAMED_WHOLE = {
    "b": '"b\\0y"',
    "b2": '"c\\0y"',
    "bx.b": '"x\\0b"',
    "tn": '"t\\0n"',
    "per.name": '"p\\0e"',
    "hs": '"h\\0s"',
    # A NUL before a digit, and a byte before a hex digit, as C reads them back.
    "v[0]": '"n\\00064"',
    "digits": '"x\\0005"',
}


@contextlib.contextmanager
def named(tmp_path, capsys, marker="// OBSERVE"):
    from peeky.cli import main

    source = tmp_path / "named.cpp"
    source.write_text(NAMED)
    line = next(i for i, text in enumerate(NAMED.splitlines(), 1) if marker in text)
    assert main(["session", "start", str(source), "--at", str(line)]) == 0
    capsys.readouterr()

    def get(*selectors):
        main(["session", "get", *selectors, "--json"])
        return {row["selector"]: row for row in json.loads(capsys.readouterr().out)}

    try:
        yield get
    finally:
        main(["session", "close"])
        capsys.readouterr()


def test_a_string_by_a_typedefs_name_is_never_cut_short(strings, cxx, tmp_path, capsys):
    # Review of #128: `Bytes b` (typedef std::basic_string<char>) showed "b" for "b\0y",
    # and `bx.b` "x", `b2` (std::__cxx11::basic_string<char>) "c": no note. A typedef of
    # std::string (`Name`) showed `error: summary string parsing error`, as the value.
    with named(tmp_path, capsys) as get:
        rows = get("b", "b2", "bx.b", "tn", "per.name", "hs", "per")
        for name in ("b", "b2", "bx.b", "tn", "per.name", "hs"):
            row = rows[name]
            assert "summary string parsing error" not in str(row.get("value")), row
            if whole(row):
                assert row["value"] == NAMED_WHOLE[name], row
            elif name in ("b", "b2", "bx.b", "hs") or cxx == "gcc":
                # LLDB's libstdc++ summary, cut at the NUL: noted. GCC's names it too.
                text = NAMED_WHOLE[name][:2] + '"'
                assert row["value"] == f"{text} (shown up to a NUL at byte 1 of 3)", row
            else:
                # Named std::string: LLDB's summary fails, and lldb-dap 18 reads no memory.
                assert row.get("unavailable") and "it holds 3 bytes" in row["error"], row
        listed = {child["selector"]: child for child in rows["per"]["children"]}
        assert listed["per.name"].get("value") == rows["per.name"].get("value")
    with named(tmp_path, capsys, "// IN_TAKE") as get:
        rows = get("br", "bp", "bp[0]", "nref")
        for name, text in (("br", '"b\\0y"'), ("bp", '"b\\0y"'), ("nref", '"t\\0n"')):
            value = rows[name]["value"]
            # LLDB's error may be quoted in a note, never shown as the text.
            assert value.startswith("0x") and not value.split(" ", 1)[1].startswith("error:")
            if whole(rows[name]):
                assert value.endswith(" " + text), rows[name]
            elif name == "nref" and cxx == "clang":
                assert "<no text: the string's summary failed" in value, rows[name]
            else:
                cut = f' {text[:2]}" (shown up to a NUL at byte 1 of 3)'
                assert value.endswith(cut), rows[name]
        assert rows["bp[0]"]["value"] == rows["bp"]["value"].split(" ", 1)[1]


def test_a_nul_or_byte_before_a_digit_reads_back_as_the_same_bytes(strings, cxx, tmp_path, capsys):
    # Review of #128: "n\0" "64" showed `"n\064"`, which C reads as "n4", and "x\0" "5"
    # `"x\05"`; also LLDB's own libc++ text on macOS. A raw byte before a hex digit the
    # same: `"\xffa"` is one character to C.
    with named(tmp_path, capsys) as get:
        rows = get("v[0]", "v[1]", "digits")
        assert rows["v[1]"]["value"] == '"\\377a"', rows["v[1]"]
        for name in ("v[0]", "digits"):
            row = rows[name]
            if whole(row):
                assert row["value"] == NAMED_WHOLE[name], row
            elif name == "v[0]":
                assert row["value"] == '"n" (shown up to a NUL at byte 1 of 4)', row
            elif cxx == "gcc":
                assert row["value"] == '"x" (shown up to a NUL at byte 1 of 3)', row
            else:
                assert row.get("unavailable"), row


def test_a_long_string_is_cut_between_characters_and_says_so(strings, tmp_path, capsys):
    # Review of #128: 500 control bytes, a NUL and "z" showed 5041 characters on
    # lldb-dap 18 (the note's text past the worker's 2000), and macOS's 2000 ended inside
    # an escape with no `...`.
    with named(tmp_path, capsys) as get:
        value = get("v[2]")["v[2]"]["value"]
        assert len(value) <= 2000, len(value)
        literal = value.split('"...', 1)[0] + '"'
        assert value.startswith('"\\U00000001') and '"...' in value, value[-80:]
        # Cut between escapes, never inside one.
        assert literal.endswith('\\U00000001"'), literal[-30:]
        if not whole(get("v[0]")["v[0]"]):
            assert value.endswith("(shown up to a NUL at byte 500 of 502)"), value[-80:]


def test_a_named_strings_evidence_is_never_lldbs_error_text(strings, cxx, tmp_path, capsys):
    # Review of #128: `queries run` showed `tn  Name  error: summary string parsing error`.
    from peeky.cli import main

    source = tmp_path / "named.cpp"
    source.write_text(NAMED)
    line = next(i for i, text in enumerate(NAMED.splitlines(), 1) if "// OBSERVE" in text)
    assert main(["probe", str(source), "--at", str(line), "--quiet", "--capture", "tn,b"]) == 0
    capsys.readouterr()
    assert main(["sql", "SELECT selector, value, unavailable, view FROM last_run", "--json"]) == 0
    saved = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    for name in ("tn", "b"):
        assert "summary string parsing error" not in str(saved[name]["value"]), saved[name]
        if whole(saved[name]):
            assert saved[name]["value"] == NAMED_WHOLE[name], saved[name]
    if cxx == "gcc" and not whole(saved["tn"]):
        assert saved["tn"]["value"] == '"t" (shown up to a NUL at byte 1 of 3)', saved["tn"]
    else:
        assert saved["tn"]["value"] == NAMED_WHOLE["tn"] or saved["tn"]["unavailable"]
