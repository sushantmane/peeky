"""libstdc++ strings with a NUL byte (#128), as lldb-dap shows them and Peeky reads them."""

import struct

import pytest

from peeky.adapters import ViewBudgetExhausted
from peeky.cxx_strings import libstdcxx_string, render, string_class, summary_units


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
    # Checked: map keys compare it (#119).
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
    native.typedefs = {"namespace::__1": ("other", "")}
    assert native.string_value(variable('"a"'), lambda reference: libstdcxx(3), None) is None
    native.typedefs = {"namespace::__cxx11": ("other", "")}
    shown = native.string_value(variable('"a"'), lambda reference: libstdcxx(3), None)
    assert shown == {"value": '"a" (shown up to a NUL at byte 1 of 3)'}
