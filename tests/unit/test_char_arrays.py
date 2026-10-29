"""A C or C++ character array's text up to its first NUL, read back from LLDB's summary
(thirteenth evaluation): `char name[32]` showed `"Adeyemi\\0\\x80\\xf8\\xe6\\xef\\U00000001..."`,
its leftovers as if part of its text, and byte 0x01 as `\\U00000001`."""

from types import SimpleNamespace

import pytest

from peeky.char_arrays import array_size, shown, summary_bytes
from peeky.cxx_strings import shorten

# LLDB's summary of the thirteenth evaluation's `char name[32]`: "Adeyemi", its NUL, and
# leftovers, the trailing NULs dropped.
WARD = (
    '"Adeyemi\\0\\x80\\xf8\\xe6\\xef\\U00000001\\0\\0\\0\\U00000001\\0\\0\\0\\U00000001\\0\\0\\0'
    '\\x80\\xc1\\xd2\\xf5\\U00000001"'
)


def test_the_text_ends_at_the_first_nul_and_the_rest_is_counted():
    assert shown(WARD, 32) == {
        "value": '"Adeyemi" (then a NUL and 24 bytes not shown)',
        # Every byte, the dropped NULs too, each spelled one way.
        "buffer": (
            '"Adeyemi\\0\\x80\\xf8\\xe6\\xef\\x01\\0\\0\\0\\x01\\0\\0\\0\\x01\\0\\0\\0\\x80\\xc1\\xd2'
            '\\xf5\\x01\\0\\0\\0"'
        ),
    }


@pytest.mark.parametrize(
    "summary, size, value, buffer",
    [
        # No NUL: every byte, and it says so.
        ('"abcdefgh"', 8, '"abcdefgh" (no NUL in its 8 bytes)', '"abcdefgh"'),
        ('"a"', 1, '"a" (no NUL in its 1 byte)', '"a"'),
        # A NUL first.
        ('"\\0xy"', 6, '"" (then a NUL and 5 bytes not shown)', '"\\0xy\\0\\0\\0"'),
        ('""', 4, '"" (then 4 NUL bytes)', '"\\0\\0\\0\\0"'),
        # Only NULs after the text: LLDB dropped them.
        ('"hello"', 8, '"hello" (then 3 NUL bytes)', '"hello\\0\\0\\0"'),
        ('"abcdefg"', 8, '"abcdefg" (then 1 NUL byte)', '"abcdefg\\0"'),
        # UTF-8 shows as its characters; a byte that starts none, as `\\xNN`.
        ('"café €\\0\\0\\0q"', 16, '"café €" (then a NUL and 6 bytes not shown)', None),
        (
            '"hi\\x80\\0\\U0000007f\\U0000001b"',
            6,
            '"hi\\x80" (then a NUL and 2 bytes not shown)',
            '"hi\\x80\\0\\x7f\\x1b"',
        ),
        # C's own escapes stay; a control character is its byte.
        (
            '"\\\\\\"\'\\n\\t\\a\\0\\U00000005"',
            8,
            '"\\\\\\"\'\\n\\t\\a" (then a NUL and 1 byte not shown)',
            '"\\\\\\"\'\\n\\t\\a\\0\\x05"',
        ),
        # A character that shows nothing (U+0085, U+200B) is its bytes; a byte before a hex
        # digit is octal, as C reads it back.
        ('"\\U00000085a\\xe2\\x82"', 5, '"\\xc2\\205a\\xe2\\x82" (no NUL in its 5 bytes)', None),
        ('"\\U0000200bz\\U00000001a"', 9, '"\\xe2\\x80\\x8bz\\001a" (then 3 NUL bytes)', None),
    ],
)
def test_each_shape_of_array(summary, size, value, buffer):
    found = shown(summary, size)
    assert found["value"] == value
    if buffer is not None:
        assert found["buffer"] == buffer


def test_a_summary_lldb_cut_at_its_limit():
    # LLDB reads a longer array's first 1024 bytes, and adds `...`.
    found = shown('"    The quick brown fox"...', 8192)
    # The NUL is within them: the bytes after it were not all read, so they are counted.
    assert found["value"] == '"    The quick brown fox" (then a NUL and 8168 bytes not shown)'
    assert found["buffer"] == '"    The quick brown fox' + "\\0" * 1001 + '"...'
    found = shown('"a\\0b"...', 2048)
    assert found["value"] == '"a" (then a NUL and 2046 bytes not shown)'
    # No NUL among them: cut, with no note, as LLDB showed it.
    full = '"' + "x" * 1024 + '"...'
    assert shown(full, 2048) == {"value": full, "buffer": full}


@pytest.mark.parametrize(
    "summary, size",
    [
        # More bytes than the array holds, or a cut where the array fits LLDB's limit.
        ('"abc"', 2),
        ('"abc"...', 8),
        ('"' + "x" * 1025 + '"...', 2048),
        ('"abc"', 2048),
        ('"abc"', 0),
        # Not LLDB's summary of a narrow array: a wide one, a pointer's, a note, a failure.
        ('L"abc"', 8),
        ('0x0000000100003f80 "abc"', 8),
        ('"abc" (note)', 8),
        ("error: summary string parsing error", 8),
        ("[1, 2]", 2),
        # A character LLDB escapes, shown as itself: not its summary.
        ('"a\x01b"', 8),
        ('"a\x85b"', 8),
        ('"a\ufffdb"', 8),
    ],
)
def test_anything_else_keeps_the_debuggers_text(summary, size):
    assert shown(summary, size) is None


def test_summary_bytes_reads_every_escape_lldb_writes():
    assert summary_bytes('"a\\0\\xff\\U00000001é\\t"') == (b"a\0\xff\x01\xc3\xa9\t", False)
    assert summary_bytes('"x"...') == (b"x", True)
    assert summary_bytes('"\\U0000d800"') is None


@pytest.mark.parametrize(
    "kind, size",
    [
        ("char[32]", 32),
        ("const char[8]", 8),
        ("signed char[6]", 6),
        ("unsigned char[6]", 6),
        ("volatile unsigned char [4]", 4),
        ("char8_t[4]", 4),
        ("char const[2]", 2),
        ("wchar_t[4]", None),
        ("char16_t[4]", None),
        ("char *", None),
        ("char[2][8]", None),
        ("char[]", None),
        ("Tag", None),
        ("uint8_t[16]", None),
    ],
)
def test_array_size_by_lldbs_name(kind, size):
    assert array_size(kind) == size


def test_a_preview_keeps_the_note_and_cuts_only_a_long_text():
    # A text that fits is never marked cut.
    assert shorten('"Adeyemi" (then a NUL and 24 bytes not shown)', 40) == (
        '"Adeyemi" (then a NUL and 24 bytes not shown)'
    )
    assert shorten('"' + "z" * 63 + '" (then 1 NUL byte)', 40) == (
        '"' + "z" * 17 + '"... (then 1 NUL byte)'
    )


def variable(value, kind):
    return {"name": "v", "value": value, "type": kind, "variablesReference": 1}


@pytest.mark.parametrize("language", ["c", "cpp"])
def test_the_native_session_shows_a_char_array_so(language):
    from peeky.native import NativeSession

    native = NativeSession.__new__(NativeSession)
    native.target = SimpleNamespace(language=language)
    native.typedefs = {"namespace::__1": ("other", "")}
    fetch = lambda reference: pytest.fail("a char array's members read")  # noqa: E731
    found = native.string_value(variable(WARD, "char[32]"), fetch, None)
    assert found["value"] == '"Adeyemi" (then a NUL and 24 bytes not shown)'
    assert found["buffer"].startswith('"Adeyemi\\0\\x80')
    # A pointer's text, and an array under a typedef's name, stay as LLDB shows them.
    assert native.string_value(variable('0x1 "a\\0b"', "char *"), fetch, None) is None
    assert native.string_value(variable('"a\\0b"', "Tag"), fetch, None) is None
    native.target = SimpleNamespace(language="rust")
    assert native.string_value(variable('"ab"', "char[4]"), fetch, None) is None
