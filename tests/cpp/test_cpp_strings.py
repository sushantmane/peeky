"""C++ strings with a NUL byte (#128): shown whole, or with a note, never cut silently.

LLDB's summary of a libstdc++ (Linux) `std::string` is a C string, which ends at the
first NUL: `"a\\0b"` showed as `"a"`. A `std::string` LLDB names by that typedef got
`error: summary string parsing error` instead. libc++ (macOS) prints every byte, so
there the values are LLDB's own, unchanged. Where the debugger reads memory (lldb-dap
20), the worker reads a libstdc++ string's bytes; elsewhere it adds a note.
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


@pytest.fixture
def strings(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
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


def test_a_string_with_a_nul_is_never_shown_cut_short(strings, capsys):
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
            else:
                # No readMemory: the debugger's text is never passed off as the value.
                assert row.get("unavailable") and "summary failed" in row["error"], row
                length = {"sso": 3, "heap": 31, "trail": 2, "plain": 5, "grown": 3}[name]
                assert f"it holds {length} bytes" in row["error"], row


def test_a_string_inside_a_struct_a_vector_or_a_map_is_never_cut_short(strings, capsys):
    # #128: `vs[0]` and `ms[1]` (LLDB names them `std::basic_string<char>`) showed "v"
    # and "m" on Linux, with no note; `rec.name` showed the summary's parsing error.
    with session(strings, capsys) as get:
        rows = get("rec.name", "vs[0]", "vs[1]", "ms[1]", "rec", "vs", "ms")
        for name in ("rec.name", "vs[0]", "vs[1]", "ms[1]"):
            row = rows[name]
            if whole(row):
                assert row["value"] == WHOLE[name], row
            elif name == "rec.name":
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


def test_a_strings_pointer_reference_and_wide_string_are_never_cut_short(strings, capsys):
    # #128: `ref` and `ptr` showed their address and the summary's parsing error; `wide`
    # (an std::wstring) too, and `u16` (std::u16string) showed no text where it can.
    with session(strings, capsys) as get:
        rows = get("ref", "ptr", "ptr[0]", "wide", "u16")
        if whole(rows["ptr[0]"]):
            assert rows["ref"]["value"].endswith(" " + WHOLE["sso"]), rows["ref"]
            assert rows["ptr"]["value"].endswith(" " + WHOLE["heap"]), rows["ptr"]
            assert rows["ptr[0]"]["value"] == WHOLE["heap"]
        else:
            assert "<no text: the string's summary failed" in rows["ref"]["value"]
            assert "it holds 31 bytes>" in rows["ptr"]["value"]
            assert "it holds 31 bytes>" in rows["ptr[0]"]["value"]
        assert rows["ref"]["value"].startswith("0x") and rows["ptr"]["value"].startswith("0x")
        wide = rows["wide"]
        if sys.platform == "darwin" or wide.get("view") == "memory":
            assert wide["value"] == WHOLE["wide"], wide
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
