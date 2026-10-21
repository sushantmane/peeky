import struct
from pathlib import Path

import pytest

from peeky.java_metadata import _class_metadata, _modified_utf8, generate_source_map


def u2(value):
    return struct.pack(">H", value)


def u4(value):
    return struct.pack(">I", value)


def fixture_class(
    internal="demo/Main",
    source="Main.java",
    lines=(2, 3),
    *,
    debug=True,
    source_attribute=True,
    extra_class_attrs=(),
    line_body=None,
    major=61,
):
    names = [
        internal,
        "java/lang/Object",
        "run",
        "()V",
        "Code",
        "LineNumberTable",
        "SourceFile",
        source,
        "SourceDebugExtension",
        "UnknownAttribute",
    ]
    cp = b"".join(b"\x01" + u2(len(name.encode())) + name.encode() for name in names)
    cp += b"\x07" + u2(1) + b"\x07" + u2(2)

    def attr(index, data):
        return u2(index) + u4(len(data)) + data

    code = b"\x00" * max(1, len(lines) - 1) + b"\xb1"
    table = u2(len(lines)) + b"".join(u2(index) + u2(line) for index, line in enumerate(lines))
    attrs = attr(6, table if line_body is None else line_body) if debug else b""
    body = u2(0) + u2(0) + u4(len(code)) + code + u2(0) + u2(int(debug)) + attrs
    method = u2(9) + u2(3) + u2(4) + u2(1) + attr(5, body)
    class_attrs = [(7, u2(8))] if source_attribute else []
    class_attrs.extend(extra_class_attrs)
    return (
        b"\xca\xfe\xba\xbe"
        + u2(0)
        + u2(major)
        + u2(13)
        + cp
        + u2(0x21)
        + u2(11)
        + u2(12)
        + u2(0)
        + u2(0)
        + u2(1)
        + method
        + u2(len(class_attrs))
        + b"".join(attr(index, data) for index, data in class_attrs)
    )


def layout(tmp_path, internal="demo/Main", source="Main.java"):
    root = tmp_path / "workspace"
    classes = root / "classes"
    path = root / Path(*internal.split("/")[:-1]) / source
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(["// fixture"] * 10))
    binary = classes / (internal + ".class")
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(fixture_class(internal, source))
    return root, classes, path, binary


def test_exact_binary_names_and_compiler_lines(tmp_path):
    root, classes, path, binary = layout(tmp_path, "demo/Main$1$Nested")
    result = generate_source_map(classes, [path], root)
    assert result == {"classes": {"demo.Main$1$Nested": {"path": str(path), "lines": [2, 3]}}}
    assert binary.exists()


@pytest.mark.parametrize(
    "change,match",
    [
        ({"debug": False}, "line debug"),
        ({"source_attribute": False}, "SourceFile"),
        ({"extra_class_attrs": ((9, b"SMAP"),)}, "remapping"),
        ({"extra_class_attrs": ((7, u2(8)),)}, "Duplicate SourceFile"),
        ({"line_body": u2(1) + u2(99) + u2(2)}, "line location"),
        ({"line_body": u2(1) + u2(0) + u2(0)}, "line location"),
        ({"line_body": u2(2) + u2(0) + u2(1) + u2(0) + u2(2)}, "Ambiguous"),
        ({"line_body": u2(1)}, "Truncated"),
        ({"major": 66}, "version"),
        ({"internal": "../Main"}, "identity"),
        ({"source": "../Main.java"}, "SourceFile"),
    ],
)
def test_rejects_missing_unsupported_and_malformed_metadata(change, match):
    with pytest.raises(ValueError, match=match):
        _class_metadata(fixture_class(**change))


def test_all_truncated_prefixes_rejected():
    data = fixture_class()
    for end in range(len(data)):
        with pytest.raises(ValueError):
            _class_metadata(data[:end])
    with pytest.raises(ValueError, match="trailing"):
        _class_metadata(data + b"extra")


def test_unknown_attribute_is_length_bounded_and_skipped():
    assert _class_metadata(fixture_class(extra_class_attrs=((10, b"extension"),)))[2] == [2, 3]


def test_modified_utf8_handles_constants_and_rejects_invalid_encodings():
    assert _modified_utf8(b"A\xc0\x80B") == "A\x00B"
    assert _modified_utf8(b"\xed\xa0\xbd\xed\xb8\x80") == "\U0001f600"
    for invalid in [b"\x00", b"\xc0\x81", b"\xc2", b"\xe0\x80\x80", b"\xf0\x9f\x98\x80"]:
        with pytest.raises(ValueError):
            _modified_utf8(invalid)


def test_ambiguous_declared_package_source_rejected(tmp_path):
    root, classes, path, _ = layout(tmp_path)
    another = root / "src/demo/Main.java"
    another.parent.mkdir(parents=True)
    another.write_text(path.read_text())
    with pytest.raises(ValueError, match="ambiguous"):
        generate_source_map(classes, [path, another], root)


def test_source_layout_missing_duplicates_and_line_range_rejected(tmp_path):
    root, classes, path, _ = layout(tmp_path)
    with pytest.raises(ValueError, match="Duplicate"):
        generate_source_map(classes, [path, path], root)
    other = root / "Other.java"
    other.write_text(path.read_text())
    with pytest.raises(ValueError, match="Missing or ambiguous"):
        generate_source_map(classes, [other], root)
    path.write_text("short\n")
    with pytest.raises(ValueError, match="length"):
        generate_source_map(classes, [path], root)


def test_class_layout_and_symlinks_rejected(tmp_path):
    root, classes, path, binary = layout(tmp_path)
    wrong = binary.with_name("Other.class")
    binary.rename(wrong)
    with pytest.raises(ValueError, match="binary identity"):
        generate_source_map(classes, [path], root)
    wrong.rename(binary)
    link = root / "linked.java"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlinks"):
        generate_source_map(classes, [link], root)
    (classes / "linked").symlink_to(root / "demo", target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        generate_source_map(classes, [path], root)


def test_overlapping_class_line_owners_preserved_for_breakpoint_rejection(tmp_path):
    root, classes, path, _ = layout(tmp_path)
    (classes / "demo/Main$1.class").write_bytes(fixture_class("demo/Main$1"))
    result = generate_source_map(classes, [path], root)
    assert set(result["classes"]) == {"demo.Main", "demo.Main$1"}
    assert all(entry["lines"] == [2, 3] for entry in result["classes"].values())


def test_bounds_and_empty_class_directory(tmp_path, monkeypatch):
    root, classes, path, binary = layout(tmp_path)
    monkeypatch.setattr("peeky.java_metadata.MAX_CLASS_BYTES", 10)
    with pytest.raises(ValueError, match="size limit"):
        _class_metadata(binary.read_bytes())
    monkeypatch.setattr("peeky.java_metadata.MAX_CLASS_BYTES", 10000)
    binary.unlink()
    with pytest.raises(ValueError, match="No executable"):
        generate_source_map(classes, [path], root)


def test_breakpoint_notes_say_which_method_stops_on_a_shared_line():
    from types import SimpleNamespace

    from peeky.java import JavaSession

    shared = {
        "7": {"stops": "main", "never": ["lambda$main$0"]},
        "4": {"stops": "lambda$static$0", "never": ["<clinit>"]},
        "9": {"stops": "<init>(I)V", "never": ["<init>()V"]},
    }
    session = SimpleNamespace(
        source_path=lambda program: program,
        mapping={
            "classes": {
                "demo.A": {"path": "/w/demo/A.java", "lines": [4, 7, 9], "shared_lines": shared},
                "demo.B": {"path": "/w/demo/B.java", "lines": [7]},
            }
        },
    )
    points = [{"line": 7}, {"line": 4}, {"line": 9}, {"line": 5}]
    notes = JavaSession.breakpoint_notes(session, "/w/demo/A.java", points)
    assert [note.split(" there")[0] for note in notes] == [
        "Line 7 runs in several methods; the debugger stops only in main",
        "Line 4 runs in several methods; the debugger stops only in lambda$static$0",
        "Line 9 runs in several methods; the debugger stops only in the constructor(I)V",
    ]
    assert "never in the static initializer" in notes[1]
    assert JavaSession.breakpoint_notes(session, "/w/demo/B.java", [{"line": 7}]) == []


def test_reads_after_follows_the_code_from_the_stop():
    # #164 review 2: a local a lambda captured before the stop is never read after it.
    from peeky.java_metadata import reads_after

    def method(code, handlers=()):
        return {"code": bytes(code), "handlers": tuple(handlers)}

    # 0: aload_1  1: astore_2  2: aload_2  3: areturn
    straight = method([0x2B, 0x4D, 0x2C, 0xB0])
    assert reads_after(straight, 2, (2,)) is True
    assert reads_after(straight, 2, (3,)) is False
    assert reads_after(straight, 1, (1,)) is False
    # A store before any read: the old value is never read.
    # 0: aload_1  1: astore_2  2: aload_2  3: astore_1  4: aload_1  5: areturn
    overwritten = method([0x2B, 0x4D, 0x2C, 0x4C, 0x2B, 0xB0])
    assert reads_after(overwritten, 1, (2,)) is False
    # A loop's back edge reads it again: 0: aload_2  1: pop  2: goto -2  (to 0).
    looped = method([0x2C, 0x57, 0xA7, 0xFF, 0xFE])
    assert reads_after(looped, 2, (1,)) is True
    # A handler covering the stop's code reads it: 0: nop  1: return  2: aload_3  3: athrow.
    handled = method([0x00, 0xB1, 0x2D, 0xBF], handlers=[(0, 1, 2)])
    assert reads_after(handled, 3, (0,)) is True
    # Code that does not decode, or a subroutine: not known.
    assert reads_after(method([0xA8, 0x00, 0x03, 0xB1]), 1, (0,)) is None
    assert reads_after(method([0x15]), 1, (0,)) is None


def test_one_bytecode_decoder_serves_loop_headers_and_reads_after():
    # #161 and #164 share java_bytecode.decode: lengths, branches, switches, slots.
    from peeky.java_bytecode import decode
    from peeky.java_metadata import reads_after
    from peeky.loop_headers import java_instructions

    # 0: iload 5  2: istore_1  3: iinc 1 1  6: wide iload 300  10: wide istore 2
    # 14: wide iinc 2 1  20: ifeq +6 (26)  23: goto -23 (0)  26: return
    code = bytes(
        [0x15, 5, 0x3C, 0x84, 1, 1, 0xC4, 0x15, 1, 44, 0xC4, 0x36, 0, 2]
        + [0xC4, 0x84, 0, 2, 0, 1, 0x99, 0, 6, 0xA7, 0xFF, 0xE9, 0xB1]
    )
    decoded = decode(code)
    assert [(each.pc, each.reads, each.writes) for each in decoded] == [
        (0, 5, None),
        (2, None, 1),
        (3, 1, 1),
        (6, 300, None),
        (10, None, 2),
        (14, 2, 2),
        (20, None, None),
        (23, None, None),
        (26, None, None),
    ]
    assert java_instructions(code) == [(each.pc, each.kind, each.targets) for each in decoded]
    assert [each.kind for each in decoded][-3:] == ["branch", "jump", "return"]
    method = {"code": code, "handlers": ()}
    # The back edge reads slot 5 again; slot 1 is stored before it is read.
    assert reads_after(method, 5, (20,)) is True
    assert reads_after(method, 1, (0,)) is False
    # A wide ret is a subroutine's (where it goes is not decoded); a wide form of
    # anything but a load, store, iinc or ret does not decode.
    assert decode(bytes([0xC4, 0xA9, 0, 1]))[0].kind == "indirect"
    assert reads_after({"code": bytes([0xC4, 0xA9, 0, 1]), "handlers": ()}, 1, (0,)) is None
    assert decode(bytes([0xC4, 0x60, 0, 1])) is None
