"""Text changes (#137 part 2): what text_sets writes, refuses and reads back, against a
fake debugger whose variables are read from a fake memory."""

import re

import pytest

from peeky.text_sets import Memory, change, parse_char, parse_text, text_kind


def char_shown(byte):
    if 0x20 <= byte < 0x7F:
        return f"'{chr(byte)}'"
    return "'\\0'" if byte == 0 else f"'\\x{byte:02x}'"


class Fake:
    """A debugger over a dict of {address: byte}: ``rows`` builds each variable's
    children from memory at every listing, so a re-listing sees every write."""

    def __init__(self, reads=True, writes=True):
        self.memory, self.trees, self.sets, self.writes = {}, {}, [], []
        self.reads, self.can_write, self.echo = reads, writes, None

    def put(self, address, data):
        for offset, byte in enumerate(data):
            self.memory[address + offset] = byte

    def get(self, address, count):
        return bytes(self.memory.get(address + offset, 0) for offset in range(count))

    def interface(self):
        return Memory(
            self.children,
            self.set,
            self.read if self.reads else None,
            self.write if self.can_write else None,
        )

    def children(self, reference, count=None):
        rows = self.trees[reference]()
        return rows[:count] if count else rows

    def read(self, address, count):
        return self.get(address, count)

    def write(self, address, data):
        self.writes.append((address, bytes(data)))
        self.put(address, data)

    def set(self, reference, name, value):
        self.sets.append((reference, name, value))
        address, size = self.places[(reference, name)]
        number = int(value)
        self.put(address, number.to_bytes(size, "little"))
        shown = self.echo or (char_shown(number) if size == 1 else str(number))
        return shown


def array(fake, address=0x1000, size=8, text=b"apple"):
    fake.put(address, text.ljust(size, b"\0"))
    fake.places = {(1, f"[{i}]"): (address + i, 1) for i in range(size)}
    fake.trees[1] = lambda: [
        {"name": f"[{i}]", "type": "char", "value": char_shown(fake.memory.get(address + i, 0))}
        for i in range(size)
    ]

    def listed():
        shown = fake.get(address, size).rstrip(b"\0").decode("latin-1")
        return {
            "name": "name",
            "type": f"char[{size}]",
            "value": f'"{shown}"',
            "memoryReference": f"0x{address:X}",
            "variablesReference": 1,
        }

    return listed


def test_text_values_are_json_strings():
    assert parse_text('"apple"') == "apple"
    assert parse_text('"say \\"hi\\""') == 'say "hi"'
    assert parse_text('"caf\\u00e9"') == "café"
    assert parse_text("5") is None and parse_text("apple") is None
    for bad in ('"open', '"a" "b"', '"\\ud800"', '"' + "x" * 4097 + '"'):
        with pytest.raises(ValueError):
            parse_text(bad)


def test_only_strings_a_frame_holds_take_a_text():
    def kind(type_, language="c"):
        return text_kind({"name": "v", "type": type_}, language)

    assert kind("char[16]") == "array"
    assert kind("std::string") == kind("std::__cxx11::basic_string<char>") == "std"
    assert kind("alloc::string::String", "rust") == "rust"
    for type_, reason in (
        ("const char *", "cannot prove the memory it points to is writable"),
        ("char *", "cannot prove the memory it points to is writable"),
        ("const std::string &", "does not write through a reference or pointer"),
        ("std::string *", "does not write through a reference or pointer"),
        ("std::wstring", "a char array (char[N]) or a std::string"),
        ("int", "a char array (char[N]) or a std::string"),
        ("unsigned char[4]", "a char array"),
    ):
        with pytest.raises(ValueError, match=re.escape(reason)):
            kind(type_)
    for type_, reason in (
        ("&str", "text it borrows, which may be read-only"),
        ("&alloc::string::String", "does not write through a reference"),
        ("i32", "a text changes a Rust String"),
    ):
        with pytest.raises(ValueError, match=reason):
            kind(type_, "rust")


def test_chars_are_read_as_lldb_shows_them():
    assert [parse_char(s) for s in ("'a'", "'''", "'\\'", "'\\0'", "'\\n'", "'\\x06'")] == [
        97,
        39,
        92,
        0,
        10,
        6,
    ]
    assert parse_char("97") is None and parse_char("'ab'") is None


@pytest.mark.parametrize("writes", [True, False])
def test_a_char_array_takes_a_text_and_a_nul_that_fit(writes):
    fake = Fake(reads=writes, writes=writes)
    listed = array(fake)
    changed = change(listed(), "pear", fake.interface(), listed, "array")
    assert changed == {"before": '"apple"', "after": '"pear"'}
    assert fake.get(0x1000, 8) == b"pear\0\0\0\0"
    # Exactly full: seven bytes and the NUL.
    changed = change(listed(), "1234567", fake.interface(), listed, "array")
    assert fake.get(0x1000, 8) == b"1234567\0" and changed["after"] == '"1234567"'
    assert bool(fake.writes) == writes and bool(fake.sets) != writes


@pytest.mark.parametrize("text", ["12345678", "a\0b"])
def test_a_text_that_does_not_fit_writes_nothing(text):
    fake = Fake()
    listed = array(fake)
    with pytest.raises(ValueError, match="cannot be changed") as refused:
        change(listed(), text, fake.interface(), listed, "array")
    assert fake.get(0x1000, 8) == b"apple\0\0\0" and not fake.writes
    assert ("holds 7 bytes of text and a NUL" in str(refused.value)) == (len(text) == 8)


def test_without_write_memory_only_printable_ascii_goes_by_element():
    fake = Fake(reads=False, writes=False)
    listed = array(fake)
    with pytest.raises(ValueError, match="printable ASCII"):
        change(listed(), "tab\tx", fake.interface(), listed, "array")
    assert fake.get(0x1000, 8) == b"apple\0\0\0" and not fake.sets
    # A character that reads back otherwise, once one was written: the session ends.
    fake.echo = "'?'"
    with pytest.raises(RuntimeError, match="did not read back"):
        change(listed(), "pear", fake.interface(), listed, "array")


def libstdcxx(fake, text, heap=None, capacity=None, address=0x2000, buffer="char[16]"):
    """A libstdc++ string at ``address``: in its own buffer, or ``heap``'s. ``buffer``:
    the debugger's type for that buffer (LLDB reads GCC's as `char[0]`, listing none of
    its characters)."""
    pointer = heap or address + 16
    fake.put(address, pointer.to_bytes(8, "little") + len(text).to_bytes(8, "little"))
    if heap:
        fake.put(address + 16, capacity.to_bytes(8, "little"))
    fake.put(pointer, text + b"\0")
    fake.places = {
        (10, "_M_string_length"): (address + 8, 8),
        **{(12, f"[{i}]"): (address + 16 + i, 1) for i in range(16)},
    }
    fake.trees[10] = lambda: [
        {
            "name": "_M_dataplus",
            "type": "std::basic_string<char>::_Alloc_hider",
            "value": "<incomplete type>",
            "memoryReference": f"0x{address:X}",
            "variablesReference": 0,
        },
        {
            "name": "_M_string_length",
            "type": "size_type",
            "value": str(int.from_bytes(fake.get(address + 8, 8), "little")),
            "memoryReference": f"0x{address + 8:X}",
        },
        {
            "name": "",
            "type": "std::basic_string<char>::(anonymous union)",
            "variablesReference": 11,
        },
    ]
    fake.trees[11] = lambda: [
        {
            "name": "_M_local_buf",
            "type": buffer,
            "memoryReference": f"0x{address + 16:X}",
            "variablesReference": 12,
        },
        {
            "name": "_M_allocated_capacity",
            "type": "size_type",
            "value": str(int.from_bytes(fake.get(address + 16, 8), "little")),
        },
    ]
    fake.trees[12] = lambda: [
        {
            "name": f"[{i}]",
            "type": "char",
            "value": char_shown(fake.memory.get(address + 16 + i, 0)),
        }
        for i in range(16 if buffer == "char[16]" else 0)
    ]

    def listed():
        return {
            "name": "s",
            "type": "std::string",
            "value": "error: summary string parsing error",
            "memoryReference": f"0x{address:X}",
            "variablesReference": 10,
        }

    return listed


@pytest.mark.parametrize("writes", [True, False])
def test_a_libstdcxx_string_changes_in_its_own_buffer(writes):
    fake = Fake(writes=writes)
    listed = libstdcxx(fake, b"apple ")
    changed = change(listed(), "apple", fake.interface(), listed, "std")
    assert changed == {"before": '"apple "', "after": '"apple"'}
    assert fake.get(0x2008, 8) == (5).to_bytes(8, "little")
    assert fake.get(0x2010, 6) == b"apple\0"
    with pytest.raises(ValueError, match="with room for 15 bytes"):
        change(listed(), "x" * 16, fake.interface(), listed, "std")


def test_a_libstdcxx_heap_string_needs_write_memory_and_its_capacity():
    fake = Fake(writes=False)
    listed = libstdcxx(fake, b"a heap string of some length", heap=0x9000, capacity=40)
    with pytest.raises(ValueError, match="on the heap, and this debugger cannot write memory"):
        change(listed(), "short", fake.interface(), listed, "std")
    fake = Fake()
    listed = libstdcxx(fake, b"a heap string of some length", heap=0x9000, capacity=40)
    change(listed(), "x" * 40, fake.interface(), listed, "std")
    assert fake.get(0x9000, 41) == b"x" * 40 + b"\0"
    with pytest.raises(ValueError, match="its heap buffer, with room for 40 bytes"):
        change(listed(), "x" * 41, fake.interface(), listed, "std")
    # Without readMemory, Peeky cannot tell where the text is.
    fake = Fake(reads=False)
    listed = libstdcxx(fake, b"apple")
    with pytest.raises(ValueError, match="cannot read memory"):
        change(listed(), "pear", fake.interface(), listed, "std")


def test_a_buffer_the_debugger_misreads_is_sized_by_the_debug_information():
    # #167: LLDB reads GCC's `_M_local_buf` as `char[0]`; the set was refused as "its
    # members are not laid out as libstdc++'s string's are".
    layout = {"local_buf": 16}
    fake = Fake()
    listed = libstdcxx(fake, b"named", buffer="char[0]")
    changed = change(listed(), "renamed", fake.interface(), listed, "std", layout)
    assert changed == {"before": '"named"', "after": '"renamed"'}
    assert fake.get(0x2010, 8) == b"renamed\0" and fake.get(0x2008, 8)[0] == 7
    with pytest.raises(ValueError, match="with room for 15 bytes"):
        change(listed(), "x" * 16, fake.interface(), listed, "std", layout)
    # A debugger that cannot write memory sets the characters one by one, which a
    # buffer it reads as `char[0]` lists none of: refused before anything is written.
    fake = Fake(writes=False)
    listed = libstdcxx(fake, b"named", buffer="char[0]")
    with pytest.raises(ValueError) as refused:
        change(listed(), "renamed", fake.interface(), listed, "std", layout)
    assert str(refused.value) == (
        "`s` cannot be changed: the debugger misreads this build's string buffer as char[0] "
        "(its debug information says char[16]), so it cannot set the text's characters one "
        "by one, and this debugger cannot write memory (lldb-dap 21 can)"
    )
    assert not fake.sets and fake.get(0x2010, 6) == b"named\0"
    # Without the debug information's size, the refusal says what the debugger misreads.
    with pytest.raises(ValueError, match="misreads this build's string buffer as char\\[0\\]"):
        change(listed(), "renamed", fake.interface(), listed, "std")
    # A size the debugger reads otherwise than the debug information: refused.
    fake = Fake()
    listed = libstdcxx(fake, b"named")
    with pytest.raises(ValueError, match="reads its buffer as char\\[16\\], and the build"):
        change(listed(), "renamed", fake.interface(), listed, "std", {"local_buf": 8})
    assert not fake.writes


def test_a_buffer_larger_than_its_string_is_never_written():
    # Review of #281: a graph of a 32-byte string, a 16-byte union and `_M_local_buf` as
    # `char[64]` gave a 64-byte buffer, and a 40-byte text was written past the object.
    from types import SimpleNamespace

    from peeky.native import NativeSession

    def graph(count):
        hider = (("_M_p", 7, 0, False, None, None),)
        inner = (
            ("_M_local_buf", 5, None, False, None, None),
            ("_M_allocated_capacity", 3, None, False, None, None),
        )
        members = (
            ("_M_dataplus", 2, 0, False, None, None),
            ("_M_string_length", 3, 8, False, None, None),
            (None, 4, 16, False, None, None),
        )
        string = ("record", "basic_string<char>", "class", 32, None, members, (2, 3, 4))
        return {
            1: (*string, False, False, "std"),
            2: ("record", "_Alloc_hider", "struct", 8, None, hider, (7,), False, False, "std"),
            3: ("base", "long unsigned int", "DW_ATE_unsigned", 8),
            4: ("record", "", "union", 16, None, inner, (5, 3), False, False, "std"),
            5: ("array", "", 6, (count,), False),
            6: ("base", "char", "DW_ATE_signed_char", 1),
            7: ("pointer", "", 6),
        }

    session = NativeSession.__new__(NativeSession)
    session.target = SimpleNamespace(language="cpp")
    variable = {"name": "s", "type": "std::string"}
    session._root_type = lambda frame, root: (1, True, graph(64))
    assert session.text_storage(variable, {"id": 1}) == ("std", None)
    session._root_type = lambda frame, root: (1, True, graph(16))
    assert session.text_storage(variable, {"id": 1}) == ("std", {"local_buf": 16})
    # Without the size, the buffer LLDB reads as `char[0]` is refused: nothing written.
    fake = Fake()
    listed = libstdcxx(fake, b"named", buffer="char[0]")
    with pytest.raises(ValueError, match="misreads this build's string buffer as char\\[0\\]"):
        change(listed(), "x" * 40, fake.interface(), listed, "std", None)
    assert not fake.writes and not fake.sets and fake.get(0x2010, 6) == b"named\0"
    # With the consistent size, the text must fit its 15 bytes.
    with pytest.raises(ValueError, match="with room for 15 bytes, and the text is 40 bytes"):
        change(listed(), "x" * 40, fake.interface(), listed, "std", {"local_buf": 16})
    assert not fake.writes


def test_a_libstdcxx_string_not_constructed_is_refused():
    fake = Fake()
    listed = libstdcxx(fake, b"x", heap=0x9000, capacity=40)
    fake.put(0x2008, (99).to_bytes(8, "little"))
    with pytest.raises(ValueError, match="does not hold a string here"):
        change(listed(), "y", fake.interface(), listed, "std")
    assert fake.get(0x9000, 2) == b"x\0" and not fake.writes


def libcxx(fake, text, heap=None, cap=None, address=0x3000):
    """libc++'s alternate layout (macOS arm64): the short buffer at +0, its size in the
    low bits of the last byte; a long string's data pointer at +0, size at +8."""
    long = heap is not None
    if long:
        fake.put(address, heap.to_bytes(8, "little") + len(text).to_bytes(8, "little"))
        fake.put(heap, text + b"\0")
        fake.put(address + 16, cap.to_bytes(8, "little"))
        fake.put(address + 23, bytes([0x80]))
    else:
        fake.put(address, text + b"\0")
        fake.put(address + 23, bytes([len(text)]))
    fake.places = {
        (22, "__size_"): (address + 23, 1),
        (23, "__size_"): (address + 8, 8),
        **{(24, f"[{i}]"): (address + i, 1) for i in range(23)},
    }
    fake.trees[20] = lambda: [{"name": "", "type": "(anonymous struct)", "variablesReference": 21}]
    fake.trees[21] = lambda: [{"name": "__rep_", "variablesReference": 25}]
    fake.trees[25] = lambda: [
        {"name": "__s", "variablesReference": 22},
        {"name": "__l", "variablesReference": 23},
    ]
    last = lambda: fake.memory.get(address + 23, 0)  # noqa: E731
    fake.trees[22] = lambda: [
        {
            "name": "__data_",
            "type": "value_type[23]",
            "memoryReference": f"0x{address:X}",
            "variablesReference": 24,
        },
        {"name": "__size_", "value": char_shown(last() & 0x7F)},
        {"name": "__is_long_", "value": char_shown(last() >> 7)},
    ]
    fake.trees[23] = lambda: [
        {"name": "__data_", "value": f"0x{int.from_bytes(fake.get(address, 8), 'little'):016x}"},
        {"name": "__size_", "value": str(int.from_bytes(fake.get(address + 8, 8), "little"))},
        {"name": "__cap_", "value": str(cap if long else 1234)},
        {"name": "__is_long_", "value": str(last() >> 7)},
    ]
    fake.trees[24] = lambda: [
        {"name": f"[{i}]", "value": char_shown(fake.memory.get(address + i, 0))} for i in range(23)
    ]

    def listed():
        return {
            "name": "s",
            "type": "std::string",
            "value": "<summary>",
            "memoryReference": f"0x{address:X}",
            "variablesReference": 20,
        }

    return listed


def test_a_libcxx_string_changes_within_its_capacity():
    fake = Fake()
    listed = libcxx(fake, b"apple ")
    change(listed(), "x" * 22, fake.interface(), listed, "std")
    assert fake.get(0x3000, 24) == b"x" * 22 + b"\0" + bytes([22])
    with pytest.raises(ValueError, match="its own buffer, with room for 22 bytes"):
        change(listed(), "x" * 23, fake.interface(), listed, "std")
    fake = Fake()
    listed = libcxx(fake, b"a fairly long string that is on the heap", heap=0x9000, cap=72)
    # `__cap_` is the allocation (72) in the alternate layout: capacity() is 71 (#164).
    change(listed(), "y" * 71, fake.interface(), listed, "std")
    assert fake.get(0x9000, 72) == b"y" * 71 + b"\0"
    assert fake.get(0x3008, 8) == (71).to_bytes(8, "little")
    with pytest.raises(ValueError, match="its heap buffer, with room for 71 bytes"):
        change(listed(), "y" * 72, fake.interface(), listed, "std")
    # A short form whose bits disagree is no layout Peeky knows: nothing written.
    fake = Fake()
    listed = libcxx(fake, b"apple")
    fake.trees[23] = lambda: [{"name": "__is_long_", "value": "1"}]
    with pytest.raises(ValueError, match="does not check out"):
        change(listed(), "pear", fake.interface(), listed, "std")
    assert not fake.writes and not fake.sets


def rust(fake, text, capacity, address=0x4000, heap=0x9000):
    layout = {"kind": "string", "size": 24, "data": 8, "length": 16, "capacity": 0}
    fake.put(address, capacity.to_bytes(8, "little") + heap.to_bytes(8, "little"))
    fake.put(address + 16, len(text).to_bytes(8, "little"))
    fake.put(heap, text)
    fake.places = {(31, "len"): (address + 16, 8)}
    fake.trees[30] = lambda: [{"name": "vec", "variablesReference": 31}]

    def listed():
        return {
            "name": "name",
            "type": "alloc::string::String",
            "value": "alloc::string::String @ 0x4000",
            "memoryReference": f"0x{address:X}",
            "variablesReference": 30,
        }

    return listed, layout


def test_a_rust_string_changes_within_its_capacity():
    fake = Fake()
    listed, layout = rust(fake, b"apple ", 16)
    changed = change(listed(), "pear", fake.interface(), listed, "rust", layout)
    assert changed == {"before": '"apple "', "after": '"pear"'}
    assert fake.get(0x4010, 8) == (4).to_bytes(8, "little") and fake.get(0x9000, 4) == b"pear"
    with pytest.raises(ValueError, match="buffer holds 16 bytes, and the text is 17"):
        change(listed(), "x" * 17, fake.interface(), listed, "rust", layout)
    fake = Fake(writes=False)
    listed, layout = rust(fake, b"apple ", 16)
    with pytest.raises(ValueError, match="cannot write memory"):
        change(listed(), "pear", fake.interface(), listed, "rust", layout)


def test_a_rust_string_keeps_its_old_length_utf8_for_a_borrowed_str():
    # A `&str` taken before the change still reads the old length: "é" is two bytes, and
    # "a" there would leave its second byte alone, cut from the first.
    fake = Fake()
    listed, layout = rust(fake, "éé".encode(), 16)
    with pytest.raises(ValueError, match="a character cut in two"):
        change(listed(), "a", fake.interface(), listed, "rust", layout)
    with pytest.raises(ValueError, match="a character cut in two"):
        change(listed(), "abcé", fake.interface(), listed, "rust", layout)
    assert fake.get(0x9000, 4) == "éé".encode() and not fake.writes
    change(listed(), "ab", fake.interface(), listed, "rust", layout)
    assert fake.get(0x9000, 4) == b"ab" + "é".encode()


def test_libcxx_standard_layout_keeps_half_the_allocation_in_cap():
    # libc++'s standard layout (x86-64, Linux) puts the size byte first, the short
    # buffer at +1, and keeps half the allocation in `__cap_` (its low bit is the long
    # bit): `__cap_` 36 is a 72-byte buffer, capacity() 71.
    fake = Fake()
    listed = libcxx(fake, b"a fairly long string that is on the heap", heap=0x9000, cap=36)
    fake.trees[22] = lambda: [
        {
            "name": "__data_",
            "type": "value_type[23]",
            "memoryReference": "0x3001",
            "variablesReference": 24,
        },
        {"name": "__size_", "value": "'\\0'"},
        {"name": "__is_long_", "value": "'\\x01'"},
    ]
    fake.trees[23] = lambda: [
        {"name": "__data_", "value": "0x0000000000009000"},
        {"name": "__size_", "value": str(int.from_bytes(fake.get(0x3008, 8), "little"))},
        {"name": "__cap_", "value": "36"},
        {"name": "__is_long_", "value": "1"},
    ]
    change(listed(), "z" * 71, fake.interface(), listed, "std")
    assert fake.get(0x9000, 72) == b"z" * 71 + b"\0"
    with pytest.raises(ValueError, match="with room for 71 bytes"):
        change(listed(), "z" * 72, fake.interface(), listed, "std")


def test_a_later_breakpoint_location_is_not_a_lines_start(tmp_path):
    # #164 after #160: a line that stops several times per pass (a loop header) stops
    # past its first code at its later locations, where every place of it counts.
    from peeky.worker import DebugWorker

    worker = DebugWorker(tmp_path)
    worker.stop_reason, worker.stop_place = "breakpoint", (2, 2)
    assert worker.text_stop() == "breakpoint-later"
    worker.stop_place = (1, 2)
    assert worker.text_stop() == "breakpoint"
    worker.stop_reason, worker.stop_place = "step", None
    assert worker.text_stop() == "step"


def test_a_crate_peeky_cannot_read_as_rustc_does_counts_as_unsafe():
    # #164 review 4: a shebang's `"` hid an `unsafe` block from the scan.
    from peeky.rust import unsafe_source

    def why(text, other=None):
        texts = {"src/main.rs": text, **({"gen.inc": other} if other is not None else {})}
        return unsafe_source(texts, ("src/main.rs",))

    hidden = "#!/usr/bin/env run-it \"\nfn main() { unsafe { f() }; let q = '\"'; }\n"
    assert "has unsafe code (in src/main.rs)" in why(hidden)
    assert why("#!/usr/bin/env run-it \"\nfn main() { let q = '\"'; }\n") is None
    # `#![...]` is an attribute, not a shebang.
    assert why("#![allow(dead_code)]\nfn main() {}\n") is None
    assert "cannot rule out unsafe code" in why('fn main() { let s = "open; }\n')
    assert "brackets do not pair up" in why("fn main() { let s = ']'; ]\n")
    assert "frontmatter" in why("---\n[dependencies]\n---\nfn main() {}\n")
    assert "comment follows" in why("#! // x\nfn main() {}\n")
    for code in ('global_asm!("")', "#[no_mangle] fn f() {}", "#[unsafe(no_mangle)] fn f() {}"):
        assert "has unsafe code" in why(f"{code}\nfn main() {{}}\n"), code
    # Comments and strings are not code; a file `include!` reads is read as text.
    assert why('// unsafe\nfn main() { let s = "unsafe"; }\n') is None
    assert "(in gen.inc)" in why("fn main() {}\n", "unsafe { f() }")
