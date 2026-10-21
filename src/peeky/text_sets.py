"""Changing a string's text in place (#137 part 2): `--set 'name="apple"'`.

A `--set` value in double quotes is text, written as JSON writes a string (`"apple"`,
`"a \\"b\\""`, `"caf\\u00e9"`). Python's debugger evaluates it as Python does, and
java-debug makes a new String of it. In C, C++ and Rust, Peeky writes the bytes itself,
in the storage the value already has, and only where that is provably safe:

- a C `char[N]` array: the text and a NUL, if both fit in its N bytes (the debug
  information gives N); the bytes after the NUL stay as they were;
- a C++ `std::string` (libc++ or libstdc++): the text, a NUL and the new size, within
  the string's current capacity and never reallocating: in its own short buffer (SSO)
  or its heap buffer, where the layout read from the debugger's members and from
  memory agree;
- a Rust `String`: the text and the new `len`, within its capacity.

Anything else is refused before a byte is written: a pointer (`char *`, `&str`) or a
reference, whose memory Peeky cannot prove writable and large enough; a text longer
than the storage; a layout that does not check out. A debugger that cannot write memory
(lldb-dap before 21) changes an array's text one element at a time (printable ASCII
only, as it shows each element exactly), and nothing on the heap. Every byte written is
read back, and so is the size: what does not read back as asked was changed in a way
nobody asked for, so the session is closed (RuntimeError) rather than reported.
"""

import codecs
import json
import re

from peeky.cxx_strings import render, string_class

MAX_TEXT_BYTES = 4096
# A `char[N]` array, as LLDB names its type.
_CHAR_ARRAY = re.compile(r"char\s*\[([1-9][0-9]{0,6})\]")
_COUNT = re.compile(r".*\[([1-9][0-9]{0,6})\]")
_POINTER = re.compile(r"(0x[0-9a-fA-F]{1,16})(?:\s.*)?", re.S)
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,19})")
# LLDB's spelling of a char's escapes (`'\0'`, `'\n'`; `'\x06'` for the rest).
_ESCAPES = {"0": 0, "a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, "e": 27}
_PRINTABLE = range(0x20, 0x7F)
_NO_WRITE = "this debugger cannot write memory (lldb-dap 21 can)"


class Memory:
    """What a text change may use, from the worker: ``children(reference, count=None)``
    lists a debugger variable's children (its first ``count``); ``set(reference, name,
    value)`` changes one through the debugger and returns the value it reports;
    ``read(address, count)`` and ``write(address, data)`` are None where the debugger
    cannot read or write memory."""

    def __init__(self, children, set, read=None, write=None):
        self.children, self.set, self.read, self.write = children, set, read, write


def parse_text(value):
    """The text a `--set` value gives in double quotes, as JSON writes a string; None
    for any other value. ValueError for a quoted value that is not such a string."""
    if not isinstance(value, str) or not value.startswith('"'):
        return None
    try:
        text = json.loads(value)
        size = len(text.encode("utf-8")) if isinstance(text, str) else None
    except (ValueError, UnicodeEncodeError):
        size = None
    if size is None:
        raise ValueError(
            f"{value[:80]!r} is not a text value: write text in double quotes as JSON writes "
            'a string (`"apple"`, `"say \\"hi\\""`, `"caf\\u00e9"`), quoted for the shell: '
            "--set 'name=\"apple\"'"
        )
    if size > MAX_TEXT_BYTES:
        raise ValueError(f"A text value is at most {MAX_TEXT_BYTES} bytes; this one is {size}")
    return text


def text_kind(variable, language):
    """Which storage a text change writes for this DAP ``variable``: `array` (a C
    `char[N]`), `std` (a `std::string`) or `rust` (a Rust `String`). ValueError
    naming why for anything else (a pointer, a reference, another type)."""
    name, kind = variable.get("name"), str(variable.get("type") or "").strip()
    refused = f"`{name}` cannot be changed to a text: it is a {kind or 'value of unknown type'}"
    if language == "rust":
        if kind == "alloc::string::String":
            return "rust"
        if kind in ("&str", "&mut str", "*const str", "*mut str"):
            raise ValueError(
                f"{refused}, text it borrows, which may be read-only (a literal) and is not "
                "the frame's to change. Change the String it borrows from, where that is owned"
            )
        if kind.startswith(("&", "*")):
            raise ValueError(
                f"{refused}, and Peeky does not write through a reference or pointer. Change "
                "the String where it is owned"
            )
        raise ValueError(f"{refused}; a text changes a Rust String")
    found = string_class(kind)
    qualified = re.search(r"\b(?:const|volatile)\b", kind)
    if qualified and not (found and found[2]) and (found or _CHAR_ARRAY.search(kind)):
        raise ValueError(f"{refused}: {qualifier_reason(qualified[0])}")
    if _CHAR_ARRAY.fullmatch(kind):
        return "array"
    if found is not None and found[2]:
        raise ValueError(
            f"{refused}, and Peeky does not write through a reference or pointer (the string "
            "may be freed or moved). Change it where it is owned"
        )
    if found is not None and found[0] == 1:
        return "std"
    if re.fullmatch(r"(?:(?:const|volatile|signed|unsigned)\s+)*char\b.*\*.*", kind, re.S):
        raise ValueError(
            f"{refused}, and Peeky cannot prove the memory it points to is writable (a "
            "string literal is read-only) or large enough, so it does not write through it. "
            "Change a char array or a std::string, or edit the source"
        )
    raise ValueError(f"{refused}; a text changes a char array (char[N]) or a std::string")


def qualifier_reason(qualifier):
    """Why a const or volatile string is never changed (#164)."""
    if qualifier == "const":
        return (
            "a const object, which the program may never change (the compiler may keep "
            "using what it held, or have placed it in read-only memory)"
        )
    return (
        f"a {qualifier} object, which the program may read in ways a debugger's write "
        "does not reach as a plain store"
    )


def held_in_place(variable, memory):
    """Whether a `std::string`'s text is in its own short buffer, as its members and
    memory tell (no write): a debugger without writeMemory (lldb-dap 20) changes it
    there, element by element, so its row is marked `[settable]`. False where any read
    fails or nothing checks out."""
    writer = _Writer(memory, f"`{variable.get('name')}`")
    try:
        members = _members(writer, variable)
        if "_M_dataplus" in members or "_M_string_length" in members:
            return bool(_libstdcxx_shape(variable, writer)["local"])
        return not _libcxx_shape(variable, writer)["long"]
    except ValueError:
        return False


def change(variable, text, memory, relist, kind, layout=None):
    """Write ``text`` into the string ``variable`` (a root local's DAP variable) of the
    ``kind`` ``text_kind`` gives; ``relist()`` lists it again (fresh references).
    ``layout``: a Rust String's, from the build's debug information. Returns
    {"before", "after"}: the value as it reads before and after. ValueError where
    nothing was written; RuntimeError once something was and it cannot be confirmed."""
    writer = _Writer(memory, f"`{variable.get('name')}`")
    data = text.encode("utf-8")
    if kind == "array":
        return _array(variable, data, writer, relist, layout)
    if kind == "rust":
        return _rust(variable, text, writer, relist, layout)
    members = _members(writer, variable)
    if "_M_dataplus" in members or "_M_string_length" in members:
        return _libstdcxx(variable, data, writer, relist)
    return _libcxx(variable, data, writer, relist)


class _Writer:
    """The debugger's requests for one change: a failure before anything was written
    refuses it (nothing changed); after, the program's state is unknown."""

    def __init__(self, memory, name):
        self.memory, self.name, self.written = memory, name, False
        self.can_read, self.can_write = memory.read is not None, memory.write is not None

    def failed(self, reason):
        if self.written:
            raise RuntimeError(
                f"The change to {self.name} was partly made and then failed ({reason}): the "
                "program's state is unknown, so the session is closed"
            )
        raise ValueError(f"{self.name} cannot be changed: {reason}")

    def check(self, condition, reason):
        """After writing: what does not read back as asked closes the session."""
        if not condition:
            raise RuntimeError(
                f"The change to {self.name} did not read back as asked ({reason}): the "
                "program's state is unknown, so the session is closed"
            )

    def children(self, reference, count=None):
        if type(reference) is not int or reference <= 0:
            self.failed("the debugger lists nothing in it")
        try:
            return self.memory.children(reference, count)
        except (OSError, ValueError, RuntimeError) as exc:
            self.failed(f"the debugger did not list its members: {str(exc)[:120]}")

    def read(self, address, count):
        if not count:
            return b""
        try:
            data = self.memory.read(address, count)
        except (OSError, ValueError, RuntimeError) as exc:
            self.failed(f"the debugger could not read its memory: {str(exc)[:120]}")
        if len(data) != count:
            self.failed("the debugger read only part of its memory")
        return data

    def write(self, address, data):
        try:
            self.memory.write(address, data)
        except TimeoutError:
            # It may yet be made: nothing says it was not.
            self.written = True
            self.failed("the debugger did not answer a write in time")
        except (OSError, ValueError, RuntimeError) as exc:
            self.failed(f"the debugger refused to write its memory: {str(exc)[:120]}")
        self.written = True

    def set(self, reference, name, value):
        try:
            reply = self.memory.set(reference, name, value)
        except TimeoutError:
            # It may yet be made: nothing says it was not.
            self.written = True
            self.failed("the debugger did not answer a write in time")
        except (OSError, ValueError, RuntimeError) as exc:
            self.failed(f"the debugger refused to change `{name}`: {str(exc)[:120]}")
        self.written = True
        return reply

    def elements(self, reference, count):
        """The first ``count`` elements of a char array, as bytes the debugger shows
        exactly (None where one shows otherwise)."""
        rows = self.children(reference, count)
        names = [row.get("name") for row in rows[:count]]
        if names != [f"[{index}]" for index in range(count)]:
            self.failed("the debugger does not list its characters one by one")
        return [parse_char(row.get("value")) for row in rows[:count]]

    def put(self, reference, payload):
        """``payload`` into a char array's first elements, one element at a time."""
        if any(byte not in _PRINTABLE for byte in payload[:-1]):
            self.failed(
                f"{_NO_WRITE}, so Peeky changes its characters one at a time, which it "
                "does only for printable ASCII text (no tabs, newlines or other bytes)"
            )
        if None in self.elements(reference, len(payload)):
            self.failed("the debugger does not show its characters as Peeky reads them")
        for index, byte in enumerate(payload):
            reply = self.set(reference, f"[{index}]", str(byte))
            self.check(parse_char(reply) == byte, f"character {index} reads {str(reply)[:20]}")


def parse_char(shown):
    """The byte LLDB shows as a char literal (`'a'`, `'\\0'`, `'\\x06'`), else None."""
    found = re.fullmatch(r"'(.+)'", str(shown or ""), re.S)
    if not found:
        return None
    body = found[1]
    if len(body) == 1 and ord(body) in _PRINTABLE:
        return ord(body)
    if len(body) == 2 and body[0] == "\\" and body[1] in _ESCAPES:
        return _ESCAPES[body[1]]
    if re.fullmatch(r"\\x[0-9a-fA-F]{2}", body):
        return int(body[2:], 16)
    return None


def _address(reference):
    found = re.fullmatch(r"0x[0-9a-fA-F]{1,16}", str(reference or ""))
    return int(found[0], 16) if found else None


def _decimal(shown):
    return int(shown) if isinstance(shown, str) and _DECIMAL.fullmatch(shown) else None


def _members(writer, variable):
    return {
        str(row.get("name")): row for row in writer.children(variable.get("variablesReference"))
    }


def _word(raw, offset):
    return int.from_bytes(raw[offset : offset + 8], "little")


def _array(variable, data, writer, relist, shape=None):
    name, kind = writer.name, str(variable.get("type"))
    # Its count from the build's debug information (a typedef's name tells none), else
    # from the debugger's type.
    named = _CHAR_ARRAY.fullmatch(kind.strip())
    size = (shape or {}).get("count") or int(named[1])
    if b"\0" in data:
        writer.failed("a NUL in the text would end the C string before the text does")
    if len(data) + 1 > size:
        writer.failed(
            f"the text does not fit: {name} is a {kind}, which holds {size - 1} bytes of text "
            f"and a NUL, and the text is {len(data)} bytes. Peeky never writes past an array"
        )
    payload = data + b"\0"
    address = _address(variable.get("memoryReference"))
    if writer.can_write and writer.can_read and address is not None:
        writer.write(address, payload)
        fresh = relist()
        writer.check(_address(fresh.get("memoryReference")) == address, "where it is")
        writer.check(writer.read(address, len(payload)) == payload, "its bytes")
    else:
        writer.put(variable.get("variablesReference"), payload)
        fresh = relist()
        shown = writer.elements(fresh.get("variablesReference"), len(payload))
        writer.check(shown == list(payload), "its characters")
        if writer.can_read and address is not None:
            writer.check(writer.read(address, len(payload)) == payload, "its bytes")
    return {"before": variable.get("value"), "after": fresh.get("value")}


def _text(data, length):
    return render(data, 1, "", length > len(data))


def _libstdcxx(variable, data, writer, relist):
    """libstdc++'s string: its data pointer (`_M_dataplus._M_p`), its length and a
    union of its own 16-byte buffer and the heap buffer's capacity, in that order. The
    text is in the string's own buffer exactly when the pointer points there."""
    name = writer.name
    if not writer.can_read:
        writer.failed(
            "Peeky cannot tell whether its text is in the string itself or on the heap: "
            "this debugger cannot read memory (lldb-dap 20 can)"
        )
    shape = _libstdcxx_shape(variable, writer)
    capacity, pointer, length = shape["capacity"], shape["pointer"], shape["length"]
    where = "its own buffer" if shape["local"] else "its heap buffer"
    if len(data) > capacity:
        writer.failed(
            f"the text does not fit: {name} holds its text in {where}, with room for "
            f"{capacity} bytes, and the text is {len(data)} bytes. Peeky never reallocates "
            "a string; a longer text needs an edit to the source"
        )
    if not shape["local"] and not writer.can_write:
        writer.failed(f"its text is on the heap, and {_NO_WRITE}")
    old = writer.read(pointer, min(length, 1024))
    payload = data + b"\0"
    # The buffer must read before it is written: a pointer to nothing is refused.
    writer.read(pointer, len(payload))
    if writer.can_write:
        writer.write(pointer, payload)
    else:
        # In its own buffer, through the debugger (checked above to be the one in use).
        writer.put(shape["buffer"], payload)
    fresh = relist()
    writer.set(fresh.get("variablesReference"), "_M_string_length", str(len(data)))
    fresh = relist()
    try:
        again = _libstdcxx_shape(fresh, writer)
    except ValueError as exc:
        again = {"error": str(exc)}
    writer.check(again.get("pointer") == pointer, "its data pointer")
    writer.check(again.get("length") == len(data), "its length")
    writer.check(writer.read(pointer, len(payload)) == payload, "its bytes")
    return {"before": _text(old, length), "after": _text(data[:1024], len(data))}


def _libstdcxx_shape(variable, writer):
    members = _members(writer, variable)
    address = _address(variable.get("memoryReference"))
    unions = [
        row
        for row in members.values()
        if row.get("name") in ("", "<null>") and "union" in str(row.get("type"))
    ]
    inner = _members(writer, unions[0]) if len(unions) == 1 else {}
    buffer, held = inner.get("_M_local_buf") or {}, inner.get("_M_allocated_capacity") or {}
    local = _address(buffer.get("memoryReference"))
    found = _COUNT.fullmatch(str(buffer.get("type") or ""))
    length = _decimal((members.get("_M_string_length") or {}).get("value"))
    plus = _address((members.get("_M_dataplus") or {}).get("memoryReference"))
    if address is None or local is None or plus is None:
        writer.failed(
            "the debugger gives no address for the string's members (lldb-dap 20 does), so "
            "Peeky cannot tell whether its text is in the string itself or on the heap"
        )
    if found is None or length is None or plus != address or local != address + 16:
        writer.failed("its members are not laid out as libstdc++'s string's are")
    raw = writer.read(address, 24)
    pointer, size = _word(raw, 0), _word(raw, 8)
    shown = (members.get("_M_dataplus") or {}).get("variablesReference")
    for row in writer.children(shown) if type(shown) is int and shown > 0 else ():
        # `_M_p` where the build describes `_Alloc_hider` (g++): it must agree.
        if row.get("name") == "_M_p":
            listed = _POINTER.fullmatch(str(row.get("value") or ""))
            if not listed or int(listed[1], 16) != pointer:
                writer.failed("its data pointer reads differently from the debugger and memory")
    if size != length:
        writer.failed("its length reads differently from the debugger and memory")
    if pointer == local:
        capacity = int(found[1]) - 1
    else:
        capacity = _word(raw, 16)
        if capacity != _decimal(held.get("value")) or not 15 < capacity < 2**40 or not pointer:
            writer.failed("its heap buffer's capacity does not read as libstdc++'s does")
    if length > capacity:
        writer.failed(
            f"its length ({length}) is more than its capacity ({capacity}): it does not "
            "hold a string here (not yet constructed, or already destroyed)"
        )
    return {
        "pointer": pointer,
        "length": length,
        "capacity": capacity,
        "local": pointer == local,
        "buffer": buffer.get("variablesReference"),
    }


def _libcxx(variable, data, writer, relist):
    """libc++'s string: a union of a short form (its size, a long bit and its own
    buffer, `__s`) and a long one (a heap pointer, size and capacity, `__l`), whose bit
    fields the debugger reads and writes as the library lays them out."""
    name = writer.name
    shape = _libcxx_shape(variable, writer)
    capacity, where = shape["capacity"], shape["where"]
    if len(data) > capacity:
        writer.failed(
            f"the text does not fit: {name} holds its text in {where}, with room for "
            f"{capacity} bytes, and the text is {len(data)} bytes. Peeky never reallocates "
            "a string; a longer text needs an edit to the source"
        )
    if shape["long"] and not writer.can_write:
        writer.failed(f"its text is on the heap, and {_NO_WRITE}")
    payload = data + b"\0"
    if writer.can_write and writer.can_read:
        # The buffer must read before it is written: a pointer to nothing is refused.
        writer.read(shape["data"], len(payload))
        writer.write(shape["data"], payload)
    else:
        writer.put(shape["buffer"], payload)
    fresh = _libcxx_shape(relist(), writer)
    writer.set(fresh["form"], "__size_", str(len(data)))
    fresh = relist()
    try:
        again = _libcxx_shape(fresh, writer)
    except ValueError as exc:
        again = {"error": str(exc)}
    writer.check(again.get("long") == shape["long"], "its form")
    writer.check(again.get("data") == shape["data"], "where its text is")
    writer.check(again.get("size") == len(data), "its size")
    if writer.can_read:
        writer.check(writer.read(shape["data"], len(payload)) == payload, "its bytes")
    else:
        writer.check(writer.elements(again["buffer"], len(payload)) == list(payload), "its text")
    return {"before": variable.get("value"), "after": fresh.get("value")}


def _libcxx_shape(variable, writer):
    members = _members(writer, variable)
    rep = members.get("__rep_")
    for row in members.values() if rep is None else ():
        if row.get("name") in ("", "<null>") or "anonymous" in str(row.get("type")):
            rep = _members(writer, row).get("__rep_") or rep
    forms = _members(writer, rep) if rep else {}
    short, long = forms.get("__s"), forms.get("__l")
    if short is None or long is None:
        writer.failed("its members are not laid out as a libc++ or libstdc++ string's are")
    shorts, longs = _members(writer, short), _members(writer, long)
    bits = (
        parse_char((shorts.get("__is_long_") or {}).get("value")),
        _decimal(_value(longs, "__is_long_")),
    )
    address = _address(variable.get("memoryReference"))
    if bits not in ((0, 0), (1, 1)) or address is None:
        writer.failed("the debugger's view of its form does not check out")
    buffer = shorts.get("__data_") or {}
    found = _COUNT.fullmatch(str(buffer.get("type") or ""))
    start = _address(buffer.get("memoryReference"))
    count = int(found[1]) if found else 0
    # The short buffer is inside the string's three words: first in libc++'s alternate
    # layout (macOS arm64), after the size byte in its standard one. Where it is tells
    # the layout, and so what `__cap_` holds (little-endian): the allocation's size in
    # the alternate layout, half of it in the standard one (libc++'s `__endian_factor`).
    factor = {0: 1, 1: 2}.get(start - address) if start is not None else None
    if not (found and factor and start + count <= address + 24):
        writer.failed("its short form is not laid out as libc++'s is")
    if bits == (0, 0):
        size = parse_char(_value(shorts, "__size_"))
        shape = {"data": start, "size": size, "capacity": count - 1, "form": short}
        shape.update(where="its own buffer", buffer=buffer.get("variablesReference"))
    else:
        pointer = _POINTER.fullmatch(str(_value(longs, "__data_") or ""))
        start = int(pointer[1], 16) if pointer else 0
        size, cap = _decimal(_value(longs, "__size_")), _decimal(_value(longs, "__cap_"))
        if (
            not start
            or address <= start < address + 24
            or size is None
            or cap is None
            or not 0 < cap < 2**40
        ):
            writer.failed("its long form does not read as libc++'s does")
        # capacity() is the allocation less the NUL's place.
        shape = {"data": start, "size": size, "capacity": cap * factor - 1, "form": long}
        shape.update(where="its heap buffer", buffer=None)
    shape["form"] = shape["form"].get("variablesReference")
    if shape["size"] is None or shape["size"] > shape["capacity"]:
        writer.failed(
            "its size is more than its capacity: it does not hold a string here (not yet "
            "constructed, or already destroyed)"
        )
    shape["long"] = bits == (1, 1)
    return shape


def _value(rows, name):
    return (rows.get(name) or {}).get("value")


def _rust(variable, text, writer, relist, layout):
    from peeky.rust_symbols import _shown_text

    name, data = writer.name, text.encode("utf-8")
    if not (writer.can_read and writer.can_write):
        writer.failed(f"a String's text is on the heap, and {_NO_WRITE}")
    if not isinstance(layout, dict) or layout.get("kind") != "string":
        writer.failed(
            "the build's debug information does not give String's layout as Peeky reads it"
        )
    address = _address(variable.get("memoryReference"))
    if address is None:
        writer.failed("the debugger gives no address for it")

    def parts():
        raw = writer.read(address, layout["size"])
        words = (_word(raw, layout[key]) for key in ("data", "length", "capacity"))
        return tuple(words)

    pointer, length, capacity = parts()
    if not length <= capacity < 2**63 or (capacity and not pointer):
        writer.failed(
            "its length is more than its capacity: it does not hold a String here (not yet "
            "assigned, or moved)"
        )
    if len(data) > capacity:
        writer.failed(
            f"the text does not fit: {name}'s buffer holds {capacity} bytes, and the text is "
            f"{len(data)} bytes. Peeky never reallocates a String; a longer text needs an "
            "edit to the source"
        )
    old = writer.read(pointer, min(length, MAX_TEXT_BYTES))
    # A `&str` borrowed from it still reads its old length: those bytes must stay UTF-8,
    # so the new text must end where a character of the old one starts, and the old
    # length must end where one of the new text starts.
    # (Read up to the longer of the two, within its capacity: the buffer must read.)
    ahead = writer.read(pointer, max(len(data), length))
    if (len(data) < length and 0x80 <= ahead[len(data)] < 0xC0) or (
        length < len(data) and 0x80 <= data[length] < 0xC0
    ):
        writer.failed(
            "a `&str` borrowed from its old text would read a character cut in two (not "
            "UTF-8): end the new text where a character of the old one starts, or change "
            "its length so the old length falls between two characters of the new text"
        )
    vectors = [
        row
        for row in writer.children(variable.get("variablesReference"))
        if row.get("name") == "vec"
    ]
    if len(vectors) != 1:
        writer.failed("the debugger does not show its Vec")
    if data:
        writer.write(pointer, data)
    fresh = [
        row
        for row in writer.children(relist().get("variablesReference"))
        if row.get("name") == "vec"
    ]
    writer.check(len(fresh) == 1, "its Vec")
    writer.set(fresh[0].get("variablesReference"), "len", str(len(data)))
    writer.check(parts() == (pointer, len(data), capacity), "its pointer, length and capacity")
    writer.check(writer.read(pointer, len(data)) == data, "its bytes")
    before = codecs.getincrementaldecoder("utf-8")("backslashreplace").decode(
        old, final=len(old) == length
    )
    return {"before": _shown_text(before, length), "after": _shown_text(text, len(data))}
