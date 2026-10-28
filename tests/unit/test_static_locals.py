"""Locals in static storage (#364): which one the code at a stop names, read from the debug
information and the source as the compiler preprocessed it; a C++ `static`'s guard; the
debugger's row that is it; the program's symbols for data; and what a probe says of one it
doesn't read."""

import struct
import sys
from pathlib import Path
from types import SimpleNamespace

from peeky.native import NativeSession, _static_locals, object_symbols
from peeky.probe import _static_hint
from peeky.worker import DebugWorker

UNIT = b"""# 1 "a.c"
long f(long x) {
    long seen = x;
    static long calls = 0;
    calls += x;
    {
        static long calls = 100;
        calls++;
    }
    {
        enum { calls = 3 };
        seen += calls;
    }
    {
        static long count = 7;
        seen += count;
    }
    return calls + seen;
}
"""
FUNCTION = ((0x100, 0x200),)
OUTER = ("calls", "a.c", 3, 0x4000, 0x10, FUNCTION, 0x30)
INNER = ("calls", "a.c", 6, 0x4008, 0x10, FUNCTION, 0x60)
COUNT = ("count", "a.c", 14, 0x4010, 0x10, FUNCTION, 0x140)


def session(tmp_path, statics=(OUTER, INNER, COUNT), unit=UNIT, language="c"):
    """A NativeSession whose function `f` (lines 1-18 of `a.c`, code 0x100-0x200)
    declares ``statics``, and whose "compiler" prints ``unit`` as its preprocessed text
    (None: it fails)."""
    native = NativeSession.__new__(NativeSession)
    native.load_bias = 0x10000
    types = {}
    for name, _, line, _, kind, *_ in statics:
        types.setdefault(name, set()).add((kind, "static", ("a.c", line)))
    function = {"source": "a.c", "span": (1, 18), "statics": tuple(statics), "types": types}
    native._function_at = lambda frame: function

    def preprocess_argv(name):
        if unit is None:
            return str(tmp_path / "missing-compiler"), [name]
        code = f"import sys; open(sys.argv[2], 'wb').write({unit!r})"
        return sys.executable, ["-c", code]

    native.target = SimpleNamespace(
        sources=("a.c",),
        verification_inputs=("a.c",),
        language=language,
        preprocess_argv=preprocess_argv,
    )
    native.prepared = SimpleNamespace(
        workspace=tmp_path, manifest=SimpleNamespace(effective_environment=())
    )
    return native


def frame(tmp_path, line, pc=0x150, name="f"):
    return {
        "id": 1,
        "name": name,
        "line": line,
        "source": {"path": str(tmp_path / "a.c")},
        "instructionPointerReference": hex(0x10000 + pc),
    }


def test_the_code_names_the_innermost_static_whose_scope_holds_the_stop(tmp_path):
    native = session(tmp_path)
    # In the inner block, its own `calls`; after it, the outer one's again.
    assert native._static_named(frame(tmp_path, 7))["calls"] == (INNER, None)
    assert native._static_named(frame(tmp_path, 17))["calls"] == (OUTER, None)
    assert native._static_named(frame(tmp_path, 4))["calls"] == (OUTER, None)
    named = native._static_named(frame(tmp_path, 17))
    assert named["count"] == (
        None,
        "at line 14, in a block this stop is outside of, so the code here doesn't name it; "
        "read it in that block, below line 14",
    )
    # Above its declaration, and on its line, the code doesn't name it yet.
    assert native._static_named(frame(tmp_path, 2))["calls"] == (
        None,
        "at line 3, below this stop, so the code here doesn't name it yet; read it below line 3",
    )
    assert native._static_named(frame(tmp_path, 3))["calls"][1].startswith(
        "on line 3, the stop's line, and Peeky reads it only below its declaration"
    )
    # A block's enumerator of its name hides it: the code names that.
    assert native._static_named(frame(tmp_path, 11))["calls"] == (
        None,
        "at line 3, but a block around this stop declares `calls` again, on line 10, and "
        "the code here names that",
    )


def test_a_static_is_named_only_where_its_block_holds_the_stop_too(tmp_path):
    # GCC gives the inner block's static that block's code: a stop the debug information
    # places outside it, though the source says the code names it, is not read.
    inner = (*INNER[:5], ((0x140, 0x160),), INNER[6])
    native = session(tmp_path, (OUTER, inner))
    assert native._static_named(frame(tmp_path, 7, pc=0x150))["calls"] == (inner, None)
    assert native._static_named(frame(tmp_path, 7, pc=0x170))["calls"] == (
        None,
        "at line 6, and Peeky can't tell whether the code here names it (the code of its "
        "block, as the debug information gives it, doesn't hold this stop); print it from "
        "the program",
    )
    # Outside it, the outer one, as the source says.
    assert native._static_named(frame(tmp_path, 17, pc=0x170))["calls"] == (OUTER, None)


def test_a_static_is_named_in_whichever_copy_of_its_function_holds_the_stop(tmp_path):
    # Review of PR #370: a function inlined twice, or copied, declares its block's
    # `static` in each copy, each with its own code. Each copy whose code doesn't hold the
    # stop made the name unsure, though another copy holds it: one declaration, held
    # where any copy holds it.
    first = (*INNER[:5], ((0x140, 0x160),), INNER[6])
    second = (*INNER[:5], ((0x180, 0x1A0),), INNER[6])
    native = session(tmp_path, (OUTER, first, second))
    for pc in (0x150, 0x190):
        chosen, why = native._static_named(frame(tmp_path, 7, pc=pc))["calls"]
        assert why is None and chosen[:5] == INNER[:5], why
        assert chosen[5] == ((0x140, 0x160), (0x180, 0x1A0))
    # In no copy's code: Peeky can't tell.
    assert (
        "doesn't hold this stop" in native._static_named(frame(tmp_path, 7, pc=0x170))["calls"][1]
    )
    # Two declarations of it on its line are two, whatever their code: unsure.
    other = (*first[:6], 0x99)
    assert (
        "twice on line 6"
        in session(tmp_path, (OUTER, first, other))._static_named(frame(tmp_path, 7, pc=0x150))[
            "calls"
        ][1]
    )


def test_a_static_is_not_read_where_the_source_or_the_stop_cant_be_read(tmp_path):
    named = session(tmp_path, unit=None)._static_named(frame(tmp_path, 7))
    assert named["calls"][0] is None
    assert (
        "Peeky can't tell whether the code here names it (Peeky could not preprocess"
        in (named["calls"][1])
    )
    native = session(tmp_path)
    native.load_bias = None
    assert native._static_named(frame(tmp_path, 7))["calls"] == (
        None,
        "at lines 3 and 6, and Peeky can't place this stop in its function's code, so it "
        "can't tell whether the code here names it; print it from the program",
    )
    # Its line names it twice: which declaration there is which, Peeky can't tell.
    twice = UNIT.replace(b"static long calls = 0;", b"static long calls = 0, kept = calls;")
    (tmp_path / "twice").mkdir()
    named = session(tmp_path / "twice", unit=twice)._static_named(frame(tmp_path / "twice", 4))
    assert (
        named["calls"][0] is None and "Peeky can't read that from the source" in (named["calls"][1])
    )
    # Two of the name on one line: which one is named can't be told.
    twin = (*INNER[:2], 3, *INNER[3:6], 0x61)
    assert (
        "twice on line 3"
        in session(tmp_path, (OUTER, twin))._static_named(frame(tmp_path, 4))["calls"][1]
    )


def test_the_debuggers_row_of_a_static_is_its_declaration_and_address(tmp_path):
    native = session(tmp_path)

    def row(name, line, address=None, value="1"):
        found = {
            "name": name,
            "value": value,
            "type": "long",
            "variablesReference": 0,
            "$__lldb_extensions": {"declaration": {"path": "/w/a.c", "line": line}},
        }
        if address is not None:
            found["memoryReference"] = hex(address)
        return found

    listed = [row("calls @ a.c:3", 3, value="1"), row("calls @ a.c:6", 6, value="101")]
    listed.append(row("::calls", 1, value="9"))
    rows = native.static_rows(listed, frame(tmp_path, 7), ["x", "seen"])
    assert [each["name"] for each in rows] == ["calls @ a.c:6"]
    # A selector reads it by its plain name; never settable, and why.
    assert native.selected_as(rows[0], frame(tmp_path, 7), rows) == "calls"
    assert native.settable(rows[0], frame(tmp_path, 7)) is False
    assert native.never_settable_row(rows[0], frame(tmp_path, 7)) == "static"
    assert native.not_yet_declared(rows[0], frame(tmp_path, 7)) is None
    # `count` is declared below: not read, and the note says why.
    assert native.static_locals(frame(tmp_path, 7)) == ["count"]
    assert native.static_notes(frame(tmp_path, 7)) == {
        "count": "at line 14, below this stop, so the code here doesn't name it yet; read it "
        "below line 14"
    }
    # Where the debugger gives an address, it must be the declaration's.
    moved = [row("calls @ a.c:6", 6, address=0x14010)]
    assert native.static_rows(moved, frame(tmp_path, 7), []) == []
    assert native.static_notes(frame(tmp_path, 7))["calls"] == (
        "at line 6, but the debugger doesn't list it for this frame, so Peeky can't read it "
        "here; print it from the program"
    )
    placed = [row("calls @ a.c:6", 6, address=0x14008)]
    assert native.static_rows(placed, frame(tmp_path, 7), []) == placed
    # LLDB names one with a symbol of its own (a C++ inline function's) by it: read where
    # it says where it is declared, as `calls`, its type that declaration's.
    qualified = [row("f()::calls", 6, value="101")]
    assert native.static_rows(qualified, frame(tmp_path, 7, name="f()"), []) == qualified
    assert native.static_locals(frame(tmp_path, 7, name="f()")) == ["count"]
    assert native.selected_as(qualified[0], frame(tmp_path, 7, name="f()"), qualified) == "calls"
    assert native._static_declared(frame(tmp_path, 7, name="f()"), "f()::calls") == "calls @ a.c:6"
    del qualified[0]["$__lldb_extensions"]
    assert native.static_rows(qualified, frame(tmp_path, 7, name="f()"), []) == []

    # A template's instances share its file and line: a row its symbol names must be
    # this frame's function's own, by name or by address (review of PR #370).
    def instance(owner, address=None, value="1"):
        found = row(f"{owner}::calls", 6, address=address, value=value)
        return found

    instances = [instance("long f<int>(int)"), instance("long f<long>(long)", value="10")]
    here = frame(tmp_path, 7, name="long f<int>(int)")
    assert native.static_rows(instances, here, []) == instances[:1]
    assert native.selected_as(instances[0], here, instances) == "calls"
    there = frame(tmp_path, 7, name="long f<long>(long)")
    assert native.static_rows(instances, there, []) == instances[1:]
    # Named otherwise than the frame, it isn't proven its own, unless by its address.
    # Apple's LLDB marks an optimized frame's name.
    marked = frame(tmp_path, 7, name="long f<int>(int) [opt]")
    assert native.static_rows(instances, marked, []) == instances[:1]
    elsewhere = frame(tmp_path, 7, name="f<int>(int)")
    assert native.static_rows(instances, elsewhere, []) == []
    placed = [instance("long f<int>(int)", 0x14008), instance("long f<long>(long)", 0x14018)]
    assert native.static_rows(placed, elsewhere, []) == placed[:1]
    # Two rows it may be: said so.
    assert native.static_rows([*instances, instances[0]], here, []) == []
    assert "lists several values it may be" in native.static_notes(here)["calls"]
    # A local of its name is listed: the existing rules judge which one the code names.
    assert native.static_rows(placed, frame(tmp_path, 7), ["calls @ a.c:2"]) == []


def test_a_cpp_static_shows_only_once_its_guard_says_it_was_initialized(tmp_path):
    native = session(tmp_path, language="cpp")
    native.data_symbols = {
        "_ZZ1fE5calls": {0x4000},
        "_ZGVZ1fE5calls": {0x4100},
        "cs.0": {0x4008},
        "_ZZ1fE1w": {0x4010},
    }
    asked = []

    def command(value):
        def run(text):
            asked.append(text)
            return f"0x{0x14100:x}: 0x{value:02x}\n"

        return run

    assert native._static_guard(OUTER, command(1)) is None
    assert asked == ["memory read --size 1 --count 1 --format x 0x14100"]
    assert native._static_guard(OUTER, command(0)) == (
        "C++ runs its initializer the first time control passes its declaration, on line 3, "
        "and its guard variable says that hasn't run yet; read it after that"
    )
    # Being initialized by another thread, or unread: not shown, and why.
    for run in (command(2), lambda text: "error: memory read failed", None):
        found = native._static_guard(OUTER, run)
        assert found.label == "not shown" and "can't read its guard variable" in found
    # A `const` GCC builds at compile time (`cs.0`), and one with no guard: no initializer
    # to wait for.
    assert native._static_guard(INNER, None) is None
    assert native._static_guard(COUNT, None) is None
    unnamed = (*COUNT[:3], 0x4800, *COUNT[4:])
    found = native._static_guard(unnamed, None)
    assert "no symbol of the program names its address" in found
    # C has no initializer that runs.
    assert session(tmp_path)._static_guard(unnamed, None) is None


def test_a_functions_statics_are_its_variables_at_a_fixed_address():
    def die(tag, parent, children=(), **attrs):
        return {"tag": tag, "attrs": attrs, "parent": parent, "children": list(children)}

    dies = {
        1: die(
            "DW_TAG_subprogram",
            0,
            (2, 3, 4, 5),
            DW_AT_low_pc="0x0000000000000100",
            DW_AT_high_pc="0x0000000000000200",
        ),
        2: die(
            "DW_TAG_variable",
            1,
            DW_AT_name='"calls"',
            DW_AT_decl_file='"/w/a.c"',
            DW_AT_decl_line="3",
            DW_AT_type="0x00000010",
            DW_AT_location="(DW_OP_addr 0x4000)",
        ),
        3: die(
            "DW_TAG_variable",
            1,
            DW_AT_name='"seen"',
            DW_AT_decl_file='"/w/a.c"',
            DW_AT_decl_line="2",
            DW_AT_location="(DW_OP_fbreg -8)",
        ),
        4: die(
            "DW_TAG_variable",
            1,
            DW_AT_name='"tls"',
            DW_AT_decl_file='"/w/a.c"',
            DW_AT_decl_line="4",
            DW_AT_location="(DW_OP_const8u 0x10, DW_OP_GNU_push_tls_address)",
        ),
        5: die(
            "DW_TAG_lexical_block",
            1,
            (6,),
            DW_AT_low_pc="0x0000000000000140",
            DW_AT_high_pc="0x0000000000000020",
        ),
        6: die(
            "DW_TAG_variable",
            5,
            DW_AT_name='"calls"',
            DW_AT_decl_file='"/w/a.c"',
            DW_AT_decl_line="6",
            DW_AT_type="0x00000010",
            DW_AT_location="(DW_OP_addr 0x4008)",
        ),
    }
    for each in dies.values():
        each["ranges"] = []
    files = ({str(Path("/w/a.c").resolve()): "a.c"}, {})
    found = _static_locals(dies, (1,), files, Path("/w"))
    assert found == (
        ("calls", "a.c", 3, 0x4000, 0x10, ((0x100, 0x200),), 2),
        ("calls", "a.c", 6, 0x4008, 0x10, ((0x140, 0x160),), 6),
    )


def test_the_programs_symbols_for_data_are_read_from_elf_and_mach_o(tmp_path):
    # An ELF64 with a `.symtab` of two objects and a function, and its `.strtab`.
    names = b"\0_ZZ1fE5calls\0_ZGVZ1fE5calls\0f\0"
    symbols = struct.pack("<IBBHQQ", 0, 0, 0, 0, 0, 0)
    symbols += struct.pack("<IBBHQQ", 1, 1, 0, 5, 0x4000, 8)
    symbols += struct.pack("<IBBHQQ", 14, 1, 0, 5, 0x4100, 8)
    symbols += struct.pack("<IBBHQQ", 29, 2, 0, 3, 0x100, 16)
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<Q", header, 40, 0x200)
    struct.pack_into("<HH", header, 58, 64, 3)
    body = bytearray(0x200 - 64)
    body[0x100 - 64 : 0x100 - 64 + len(symbols)] = symbols
    body[0x180 - 64 : 0x180 - 64 + len(names)] = names
    sections = struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    sections += struct.pack("<IIQQQQIIQQ", 0, 2, 0, 0, 0x100, len(symbols), 2, 0, 8, 24)
    sections += struct.pack("<IIQQQQIIQQ", 0, 3, 0, 0, 0x180, len(names), 0, 0, 1, 0)
    (tmp_path / "elf").write_bytes(bytes(header) + bytes(body) + sections)
    assert object_symbols(tmp_path / "elf") == {
        "_ZZ1fE5calls": {0x4000},
        "_ZGVZ1fE5calls": {0x4100},
    }
    # A Mach-O's: defined in a section, less its leading `_`; never a debugging entry.
    strings = b"\0__ZZ1fE5calls\0__ZGVZ1fE5calls\0_stab\0"
    table = struct.pack("<IBBHQ", 1, 0x0E, 1, 0, 0x100004000)
    table += struct.pack("<IBBHQ", 15, 0x0F, 1, 0, 0x100004100)
    table += struct.pack("<IBBHQ", 31, 0x24, 1, 0, 0x100004200)
    command = struct.pack("<IIIIII", 0x2, 24, 0x100, 3, 0x200, len(strings))
    macho = struct.pack("<IIIIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 1, len(command), 0, 0)
    data = bytearray(0x300)
    data[: len(macho) + len(command)] = macho + command
    data[0x100 : 0x100 + len(table)] = table
    data[0x200 : 0x200 + len(strings)] = strings
    (tmp_path / "macho").write_bytes(bytes(data))
    assert object_symbols(tmp_path / "macho") == {
        "_ZZ1fE5calls": {0x100004000},
        "_ZGVZ1fE5calls": {0x100004100},
    }


def test_a_static_not_read_says_why():
    snapshot = {
        "function": "tick",
        "statics": ["count", "old"],
        "static_notes": {"count": "at line 14, in a block this stop is outside of"},
    }
    assert _static_hint(snapshot, "count") == (
        ": tick declares 'count' in static storage (a `static` local, or a `const` one the "
        "compiler placed there), at line 14, in a block this stop is outside of"
    )
    # An earlier worker reads none: it says so.
    assert _static_hint(snapshot, "old").endswith(
        "which the debugger does not list among a frame's locals, so Peeky cannot read it "
        "here; print it from the program"
    )
    assert _static_hint(snapshot, "other") == ""


def test_the_frames_static_storage_is_listed_where_its_function_declares_some():
    scopes = [
        {"name": "Locals", "presentationHint": "locals", "variablesReference": 1},
        {"name": "Globals", "variablesReference": 2},
        {"name": "Registers", "presentationHint": "registers", "variablesReference": 3},
    ]
    native = NativeSession.__new__(NativeSession)
    native.target = SimpleNamespace(language="c")
    function = {"statics": (OUTER,)}
    native._function_at = lambda frame: function
    listed = DebugWorker.frame_scopes(native, {"name": "f"}, scopes)
    assert [scope["name"] for scope in listed] == ["Locals", "Globals"]
    function["statics"] = ()
    listed = DebugWorker.frame_scopes(native, {"name": "f"}, scopes)
    assert [scope["name"] for scope in listed] == ["Locals"]
    # Rust's sessions are native ones too: never there.
    function["statics"], native.target.language = (OUTER,), "rust"
    listed = DebugWorker.frame_scopes(native, {"name": "f"}, scopes)
    assert [scope["name"] for scope in listed] == ["Locals"]


def test_an_inlined_functions_static_is_held_by_each_copy_of_its_block():
    # Review of PR #370: GCC declares an inlined (or copied) function's block `static` in
    # its abstract instance, whose block has no code; each copy's block, which names it
    # as its origin, has. A copy may declare the variable again, as clang's does.
    def die(tag, parent, children=(), **attrs):
        return {
            "tag": tag,
            "attrs": attrs,
            "parent": parent,
            "children": list(children),
            "ranges": [],
        }

    hits = {
        "DW_AT_name": '"hits"',
        "DW_AT_decl_file": '"/w/a.c"',
        "DW_AT_decl_line": "6",
        "DW_AT_type": "0x00000010",
        "DW_AT_location": "(DW_OP_addr 0x4008)",
    }
    dies = {
        10: die("DW_TAG_subprogram", 0, (11,), DW_AT_name='"bump"'),
        11: die("DW_TAG_lexical_block", 10, (12,)),
        12: die("DW_TAG_variable", 11, **hits),
        20: die(
            "DW_TAG_inlined_subroutine",
            0,
            (21,),
            DW_AT_abstract_origin="0x0000000a",
            DW_AT_low_pc="0x0000000000000100",
            DW_AT_high_pc="0x0000000000000180",
        ),
        21: die(
            "DW_TAG_lexical_block",
            20,
            DW_AT_abstract_origin="0x0000000b",
            DW_AT_low_pc="0x0000000000000140",
            DW_AT_high_pc="0x0000000000000160",
        ),
        30: die(
            "DW_TAG_inlined_subroutine",
            0,
            (31,),
            DW_AT_abstract_origin="0x0000000a",
            DW_AT_low_pc="0x0000000000000200",
            DW_AT_high_pc="0x0000000000000280",
        ),
        31: die(
            "DW_TAG_lexical_block",
            30,
            (32,),
            DW_AT_abstract_origin="0x0000000b",
            DW_AT_low_pc="0x0000000000000240",
            DW_AT_high_pc="0x0000000000000260",
        ),
        32: die(
            "DW_TAG_variable",
            31,
            DW_AT_abstract_origin="0x0000000c",
            DW_AT_location="(DW_OP_addr 0x4008)",
        ),
    }
    files = ({str(Path("/w/a.c").resolve()): "a.c"}, {})
    found = _static_locals(dies, (20, 30, 10), files, Path("/w"))
    copies = ((0x140, 0x160), (0x240, 0x260))
    assert found == (
        ("hits", "a.c", 6, 0x4008, 0x10, copies, 12),
        ("hits", "a.c", 6, 0x4008, 0x10, ((0x240, 0x260),), 12),
    )
