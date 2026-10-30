"""libstdc++ strings with a NUL byte, as lldb-dap shows them and Peeky reads them."""

import struct
from types import SimpleNamespace

import pytest

from peeky.adapters import ViewBudgetExhausted
from peeky.cxx_strings import (
    libstdcxx_string,
    render,
    string_class,
    summary_units,
    text_like,
    tidy,
)


@pytest.mark.parametrize(
    "kind, found",
    [
        ("std::string", (1, "", "")),
        ("const std::string &", (1, "", "&")),
        ("std::basic_string<char>", (1, "", "")),
        (
            "std::__cxx11::basic_string<char, std::char_traits<char>, std::allocator<char> >",
            (1, "", ""),
        ),
        ("std::string *", (1, "", "*")),
        ("std::wstring", (4, "L", "")),
        ("std::basic_string<char16_t>", (2, "u", "")),
        ("const std::u32string &", (4, "U", "&")),
        ("std::vector<std::string>", None),
        ("std::string_view", None),
        ("std::string **", None),
        ("std::basic_string<unsigned char>", None),
    ],
)
def test_standard_string_types(kind, found):
    assert string_class(kind) == found


@pytest.mark.parametrize(
    "text, width, prefix, shown",
    [
        ('"a"', 1, "", (1, False)),
        ('"a\\0b"', 1, "", (3, False)),
        # LLDB writes a control character as \U, an invalid byte as \x, é as itself.
        ('"\\U00000001\\xff\\t\\"\\\\z"', 1, "", (6, False)),
        ('"é\\U0000001b"', 1, "", (3, False)),
        ('"yyy"...', 1, "", (3, True)),
        ('L"x\\0y"', 4, "L", (3, False)),
        ('u"😀"', 2, "u", (2, False)),
        ("error: summary string parsing error", 1, "", None),
        ('"a"b"', 1, "", None),
        ('"\\q"', 1, "", None),
        ('L"x"', 1, "", None),
    ],
)
def test_the_debuggers_text_is_counted_in_characters(text, width, prefix, shown):
    assert summary_units(text, width, prefix) == shown


def test_bytes_are_shown_as_lldb_shows_a_libcxx_string():
    # What LLDB's libc++ summary showed for the same bytes on macOS.
    assert render(b"a\x00b", 1, "", False) == '"a\\0b"'
    assert render(b'\x01\xff\t"\\z', 1, "", False) == '"\\U00000001\\xff\\t\\"\\\\z"'
    assert render("é".encode() + b"\x1b\x7f\n\r\a\b\f\v", 1, "", False) == (
        '"é\\U0000001b\\U0000007f\\n\\r\\a\\b\\f\\v"'
    )
    assert render("a\0b".encode("utf-32-le"), 4, "L", False) == 'L"a\\0b"'
    assert render("x😀".encode("utf-16-le"), 2, "u", False) == 'u"x😀"'
    # LLDB's own mark of a string longer than its summary, and a shown text kept bounded.
    assert render(b"y" * 3, 1, "", True) == '"yyy"...'
    assert render(b"\x01" * 400, 1, "", False).endswith('"...')


def variable(value, kind="std::basic_string<char>", reference=7):
    return {"name": "s", "type": kind, "value": value, "variablesReference": reference}


def libstdcxx(length, address="0x1000"):
    """A libstdc++ string's members as lldb-dap 20 lists them."""
    return [
        {"name": "_M_dataplus", "value": "<incomplete type>", "memoryReference": address},
        {"name": "_M_string_length", "value": str(length)},
        {"name": "", "value": "(anonymous union) @ 0x1010"},
    ]


class Memory:
    """readMemory over a few words: the string at 0x1000, its bytes at 0x2000."""

    def __init__(self, data, length=None):
        self.cells = {
            "0x1000": struct.pack("<QQ", 0x2000, len(data) if length is None else length),
            "0x2000": data,
        }

    def __call__(self, address, count):
        return self.cells[address][:count]


def test_a_string_the_debugger_cuts_at_a_nul_is_read_from_memory():
    fetch = lambda reference: libstdcxx(3)  # noqa: E731
    shown = libstdcxx_string(variable('"a"'), fetch, Memory(b"a\0b"))
    assert shown == {"value": '"a\\0b"', "view": "memory", "string_length": 3}
    # The `std::string` typedef's summary fails on this build: read it all the same.
    shown = libstdcxx_string(
        variable("error: summary string parsing error", "std::string"), fetch, Memory(b"a\0b")
    )
    assert shown == {"value": '"a\\0b"', "view": "memory", "string_length": 3}
    # A pointer keeps its address.
    shown = libstdcxx_string(variable('0x00007ff0 "a"', "std::string *"), fetch, Memory(b"a\0b"))
    assert shown == {"value": '0x00007ff0 "a\\0b"', "view": "memory", "string_length": 3}


def test_a_whole_text_is_left_as_the_debugger_shows_it():
    # Checked: map keys compare it.
    shown = libstdcxx_string(variable('"abc"'), lambda reference: libstdcxx(3), None)
    assert shown == {"string_length": 3}
    # LLDB's limit (1024 bytes), marked `...`, is no NUL.
    shown = variable('"' + "y" * 1024 + '"...')
    assert libstdcxx_string(shown, lambda reference: libstdcxx(1100), None) == {
        "string_length": 1100
    }
    # libc++'s members: its summary shows every byte.
    members = [{"name": "", "value": "(anonymous struct)"}]
    assert libstdcxx_string(variable('"a"'), lambda reference: members, None) is None


def test_without_memory_reads_the_text_gets_a_note():
    fetch = lambda reference: libstdcxx(3)  # noqa: E731
    shown = libstdcxx_string(variable('"a"'), fetch, None)
    assert shown == {"value": '"a" (shown up to a NUL at byte 1 of 3)'}
    # After LLDB's limit too: it adds `...` past a NUL in a longer string.
    shown = libstdcxx_string(
        variable('"' + "y" * 600 + '"...'), lambda reference: libstdcxx(1100), None
    )
    assert shown["value"].endswith('"... (shown up to a NUL at byte 600 of 1100)')
    shown = libstdcxx_string(
        variable("error: summary string parsing error", "std::string"), fetch, None
    )
    assert shown["unavailable"] and "it holds 3 bytes" in shown["reason"]
    shown = libstdcxx_string(
        variable("0x7ff0 error: summary string parsing error", "std::string &"), fetch, None
    )
    assert shown["value"].startswith("0x7ff0 <no text: the string's summary failed")
    # A read that fails, or a length that does not match the debugger's, is no value.
    shown = libstdcxx_string(variable('"a"'), fetch, Memory(b"a\0b", length=4))
    assert shown == {"value": '"a" (shown up to a NUL at byte 1 of 3)'}


def test_a_string_with_no_length_in_view_is_never_vouched_for():
    # clang leaves out the members of std::wstring where libstdc++'s own debug
    # information is expected to give them; the debugger then shows no length.
    none = lambda reference: []  # noqa: E731
    shown = libstdcxx_string(
        variable("error: summary string parsing error", "std::wstring"), none, None
    )
    assert shown["unavailable"] and "gives no length" in shown["reason"]
    shown = libstdcxx_string(variable('"a"', reference=0), none, None)
    assert shown == {
        "value": '"a" (not checked for a NUL byte: the debugger shows no length for this string)'
    }

    def spent(reference):
        raise ViewBudgetExhausted("Value view budget exhausted")

    assert "not checked for a NUL byte" in libstdcxx_string(variable('"a"'), spent, None)["value"]


def test_libcxx_builds_keep_lldbs_strings():
    from peeky.native import NativeSession

    native = NativeSession.__new__(NativeSession)
    native.target = SimpleNamespace(language="cpp")
    native.typedefs = {"namespace::__1": ("other", "")}
    # Its text, as LLDB shows it (members never read): only marked as text.
    fetch = lambda reference: pytest.fail("libc++'s members read")  # noqa: E731
    assert native.string_value(variable('"a"'), fetch, None) == {"text": True}
    native.typedefs = {"namespace::__cxx11": ("other", "")}
    shown = native.string_value(variable('"a"'), lambda reference: libstdcxx(3), None)
    assert shown == {"value": '"a" (shown up to a NUL at byte 1 of 3)', "text": True}


# Typedef'd names, the C reading of an escape, long text.
@pytest.mark.parametrize(
    "text, shown",
    [
        # LLDB's `\0` before a digit: C reads "\064" as one character, `4`.
        ('"n\\064"', '"n\\00064"'),
        ('"x\\05"', '"x\\0005"'),
        ('0x7ff0 "x\\05"', '0x7ff0 "x\\0005"'),
        (
            '"x\\05" (shown up to a NUL at byte 1 of 3)',
            '"x\\0005" (shown up to a NUL at byte 1 of 3)',
        ),
        # Its `\xff` before a hex digit: C reads "\xffa" as one character.
        ('"\\xffa"', '"\\377a"'),
        ('"\\xff\\xfe"', None),
        ('"\\0\\0"', None),
        ('"\\08"', None),
        ('"\\xffg"', None),
        ('L"a\\01"', 'L"a\\0001"'),
        ("hello", None),
        ('"a" b', None),
    ],
)
def test_a_literal_is_spelled_as_c_reads_it_back(text, shown):
    assert tidy(text) == (None if shown is None else {"value": shown})


def test_the_debuggers_failed_summary_is_never_a_value():
    for failure in ("summary string parsing error", "no target", "no ScriptInterpreter"):
        shown = tidy(f"error: {failure}")
        assert shown["unavailable"] and shown["value"] is None
        assert f"error: {failure}" in shown["reason"] and "select its members" in shown["reason"]
        shown = tidy(f"0x00007ff0 error: {failure}")
        assert shown["value"].startswith("0x00007ff0 <no text: LLDB's summary of this value")
    # Rust: the same backstop, but its literals are not respelled (`\0` is one NUL there).
    assert tidy("error: summary string parsing error", False)["unavailable"]
    assert tidy('"n\\064"', False) is None


def test_a_long_literal_is_cut_between_characters():
    text = '"' + "\\U00000001" * 500 + '\\0z"'
    shown = tidy(text)["value"]
    assert len(shown) <= 2000 and shown.endswith('\\U00000001"...'), shown[-40:]
    noted = tidy(text + " (shown up to a NUL at byte 500 of 502)")["value"]
    assert len(noted) <= 2000 and noted.endswith('"... (shown up to a NUL at byte 500 of 502)')
    # LLDB's own `...` is kept; a short literal is left as it is.
    assert tidy('"yyy"...') is None


def test_a_string_is_told_by_its_members_under_any_name():
    def members(length, owner="std::basic_string<char>::_Alloc_hider"):
        found = libstdcxx(length)
        found[0]["type"] = owner
        return found

    fetch = lambda reference: members(3)  # noqa: E731
    shown = libstdcxx_string(variable('"b"', "Bytes"), fetch, None, None)
    assert shown == {"value": '"b" (shown up to a NUL at byte 1 of 3)'}
    shown = libstdcxx_string(variable('"b"', "Bytes"), fetch, Memory(b"b\0y"), None)
    assert shown == {"value": '"b\\0y"', "view": "memory", "string_length": 3}
    shown = libstdcxx_string(variable('0x7ff0 "b"', "const Bytes &"), fetch, None, None)
    assert shown == {"value": '0x7ff0 "b" (shown up to a NUL at byte 1 of 3)'}
    wide = lambda reference: members(3, "std::basic_string<wchar_t>::_Alloc_hider")  # noqa: E731
    shown = libstdcxx_string(variable('L"b"', "Wide"), wide, None, None)
    assert shown == {"value": 'L"b" (shown up to a NUL at character 1 of 3)'}
    # Anything else with a `_M_dataplus`, or none, is left as the debugger shows it.
    other = lambda reference: members(3, "Mine::_Alloc_hider")  # noqa: E731
    assert libstdcxx_string(variable('"b"', "Mine"), other, None, None) is None
    plain = lambda reference: [{"name": "x", "value": "1"}]  # noqa: E731
    assert libstdcxx_string(variable('"b"', "Mine"), plain, None, None) is None


def test_only_text_with_members_is_worth_reading_them():
    assert text_like(variable('"b"', "Bytes"))
    assert text_like(variable("error: summary string parsing error", "Name"))
    assert text_like(variable('0x7ff0 "b"', "const Bytes &"))
    assert not text_like(variable('"b"', "Bytes", reference=0))
    assert not text_like(variable("3", "Count"))
    # C's own text: LLDB shows it as C has it.
    for kind in ("char *", "const char *", "char[16]", "unsigned char[4]", "const wchar_t *"):
        assert not text_like(variable('"b"', kind))


class Reads:
    """readMemory, recording each request."""

    def __init__(self, cells):
        self.cells, self.requests = cells, []

    def __call__(self, address, count):
        self.requests.append((address, count))
        return self.cells[address][:count]


def test_a_string_in_its_own_buffer_is_read_in_one_request():
    local = struct.pack("<QQ", 0x1010, 3) + b"a\0b" + b"\0" * 13
    reads = Reads({"0x1000": local})
    shown = libstdcxx_string(variable('"a"'), lambda reference: libstdcxx(3), reads)
    assert shown["value"] == '"a\\0b"' and reads.requests == [("0x1000", 32)]
    reads = Reads({"0x1000": struct.pack("<QQ", 0x2000, 20) + b"\0" * 16, "0x2000": b"x\0" * 10})
    shown = libstdcxx_string(variable('"x"'), lambda reference: libstdcxx(20), reads)
    assert shown["value"] == '"' + "x\\0" * 10 + '"'
    assert reads.requests == [("0x1000", 32), ("0x2000", 20)]


def test_a_typedef_of_a_string_class_is_resolved():
    from peeky.native import NativeSession

    native = NativeSession.__new__(NativeSession)
    native.target = SimpleNamespace(language="cpp")
    native.typedefs = {
        "Bytes": ("other", "std::basic_string<char>"),
        "Name": ("other", "std::string"),
        "BytesRef": ("reference", "std::basic_string<char>"),
        "BytesPtr": ("pointer", "Bytes"),
        "Count": ("other", "int"),
        "namespace::__cxx11": ("other", ""),
    }
    assert native._string_class("Bytes") == (1, "", "")
    assert native._string_class("const Name &") == (1, "", "&")
    assert native._string_class("BytesRef") == (1, "", "&")
    assert native._string_class("Bytes *") == (1, "", "*")
    assert native._string_class("BytesPtr") == (1, "", "*")
    assert native._string_class("Count") is None
    assert native._string_class("Unknown") is None


def test_peekys_own_text_is_spelled_once():
    from peeky.native import NativeSession

    native = NativeSession.__new__(NativeSession)
    native.target = SimpleNamespace(language="cpp")
    native.typedefs = {"namespace::__cxx11": ("other", "")}
    fetch = lambda reference: libstdcxx(4)  # noqa: E731
    # Read from memory: `\000` then "64" (C reads `\00064` as NUL, '6', '4').
    shown = native.string_value(variable('"n"'), fetch, Memory(b"n\x0064"))
    assert shown["value"] == '"n\\00064"' and shown["view"] == "memory"
    # LLDB's whole text, respelled once; Rust's left as LLDB shows it.
    whole = lambda reference: libstdcxx(2)  # noqa: E731
    assert native.string_value(variable('"\\xffa"'), whole, None) == {
        "string_length": 2,
        "value": '"\\377a"',
        "text": True,
    }
    native.target = SimpleNamespace(language="rust")
    assert native.string_value(variable('"a\\064"', "&str"), whole, None) is None


def test_a_string_shown_as_its_text_is_marked_so():
    # Its members are the library's, never listed.
    from peeky.cxx_strings import shows_text
    from peeky.native import NativeSession

    assert shows_text('"apple"', (1, "", ""))
    assert shows_text('0x00007ff0 "apple"', (1, "", "&"))
    assert shows_text('L"w"', (4, "L", ""))
    assert not shows_text('"apple"', (1, "", "*"))
    assert not shows_text("std::u16string @ 0x1000", (2, "u", ""))
    assert not shows_text("0x0000000000000000", (1, "", "*"))
    native = NativeSession.__new__(NativeSession)
    native.target = SimpleNamespace(language="cpp")
    native.typedefs = {"namespace::__1": ("other", ""), "Name": ("other", "std::string")}
    none = lambda reference: []  # noqa: E731
    assert native.string_value(variable('"apple"', "std::string"), none, None) == {"text": True}
    assert native.string_value(variable('"typed"', "Name"), none, None) == {"text": True}
    assert native.string_value(variable("Rec @ 0x1", "Rec"), none, None) is None
    # libstdc++: each string the runtime checked.
    native.typedefs = {"namespace::__cxx11": ("other", "")}
    shown = native.string_value(variable('"abc"'), lambda reference: libstdcxx(3), None)
    assert shown == {"string_length": 3, "text": True}
    # Rust's strings are its own (views), and C has no string class.
    native.target = SimpleNamespace(language="rust")
    assert native.string_value(variable('"abc"', "&str"), none, None) is None
