"""native_startup: a startup object's code matched as the linker relocated it."""

import struct

import pytest

from peeky.native_startup import (
    EM_AARCH64,
    EM_X86_64,
    Relocation,
    StartupPiece,
    archive_members,
    place,
)

RET = struct.pack("<I", 0xD65F03C0)
MOV = struct.pack("<I", 0xAA0903E9)  # mov x9, x9
BR_X0 = struct.pack("<I", 0xD61F0000)  # br x0


def _bl(place_, target, opcode=0x94000000):
    return struct.pack("<I", opcode | ((target - place_) >> 2) & 0x3FFFFFF)


def _adrp(place_, target, register=0):
    pages = ((target & ~0xFFF) - (place_ & ~0xFFF)) >> 12
    return struct.pack(
        "<I", 0x90000000 | (pages & 0x3) << 29 | ((pages >> 2) & 0x7FFFF) << 5 | register
    )


def _low(opcode, value, scale=1):
    return struct.pack("<I", opcode | ((value & 0xFFF) // scale) << 10)


class Image:
    """A linked binary, as native_startup.Image reads it: its code at ``base``, and
    ``code``, (low, high) of the rest of its code."""

    def __init__(self, machine, base, data, addresses=None, slots=None, stubs=None, code=()):
        self.machine, self.base, self.data = machine, base, data
        self.addresses = addresses or {}
        self.slots, self.stubs = slots or {}, stubs or {}
        self.code = [(base, base + len(data)), *code]

    def runs(self, address):
        return any(low <= address < high for low, high in self.code)

    def read(self, address, length):
        at = address - self.base
        return self.data[at : at + length] if 0 <= at and at + length <= len(self.data) else None

    def got(self, name, weak=False):
        return self.slots.get(name, set())

    def stub(self, address):
        return self.stubs.get(address)


def _piece(data, *relocations, machine=EM_AARCH64, unread=None, **symbols):
    return StartupPiece("crtbeginS.o", ".text", data, machine, relocations, symbols, unread)


def _external(offset, kind, name, addend=0, weak=False):
    return Relocation(offset, kind, addend, None, name, 0, False, None, weak)


BASE = 0x1000


def test_a_call_s_word_is_its_own_but_its_target_field():
    # A call to __cxa_finalize, then ret: the linker fills in only the call's offset.
    piece = _piece(_bl(0, 0) + RET, _external(0, 283, "__cxa_finalize"), frame_dummy=0)
    stub, slot = 0x5D0, 0x20008
    linked = Image(EM_AARCH64, BASE, _bl(BASE, stub) + RET, slots={"__cxa_finalize": {slot}})
    linked.stubs[stub] = slot
    assert place(piece, BASE, linked)
    # Another instruction where the call was: not the toolchain's.
    for other in (MOV, BR_X0, _bl(BASE, stub, opcode=0x14000000)):
        assert not place(piece, BASE, Image(EM_AARCH64, BASE, other + RET, stubs={stub: slot}))
    # The same call to another function: its target is checked.
    elsewhere = Image(EM_AARCH64, BASE, _bl(BASE, 0x700) + RET, addresses={"exit": {0x700}})
    assert not place(piece, BASE, elsewhere)
    # Where the binary defines the function, the call goes to it.
    defined = Image(EM_AARCH64, BASE, _bl(BASE, 0x700) + RET, addresses={"__cxa_finalize": {0x700}})
    assert place(piece, BASE, defined)


def test_a_branch_within_the_piece_must_land_where_it_does_in_the_object():
    # crtbegin.o's frame_dummy: `b register_tm_clones`, at offset 0 of the same section.
    piece = _piece(RET + _bl(4, 0, 0x14000000), Relocation(4, 282, 0, 0, None, 0, True, None))
    assert place(piece, BASE, Image(EM_AARCH64, BASE, RET + _bl(BASE + 4, BASE, 0x14000000)))
    shifted = Image(EM_AARCH64, BASE, RET + _bl(BASE + 4, BASE + 8, 0x14000000))
    assert not place(piece, BASE, shifted)


def test_an_address_and_a_got_slot_are_checked_by_page_and_low_bits():
    tmc, slot = 0x20030, 0x1FFC0
    code = _adrp(0, 0) + _low(0x91000000, 0) + _adrp(0, 0, 1) + _low(0xF9400021, 0, 8)
    piece = _piece(
        code,
        # .tm_clone_table, empty: the binary keeps no symbol of it, but __TMC_END__.
        Relocation(0, 275, 0, None, "__TMC_LIST__", 0, True, ".tm_clone_table"),
        Relocation(4, 277, 0, None, "__TMC_LIST__", 0, True, ".tm_clone_table"),
        _external(8, 311, "_ITM_deregisterTMCloneTable"),
        _external(12, 312, "_ITM_deregisterTMCloneTable"),
    )

    def linked(address, got):
        return (
            _adrp(BASE, address)
            + _low(0x91000000, address)
            + _adrp(BASE + 8, got, 1)
            + _low(0xF9400021, got, 8)
        )

    slots = {"_ITM_deregisterTMCloneTable": {slot}}
    good = Image(EM_AARCH64, BASE, linked(tmc, slot), {"__TMC_END__": {tmc}}, slots)
    assert place(piece, BASE, good)
    wrong = Image(EM_AARCH64, BASE, linked(tmc + 8, slot), {"__TMC_END__": {tmc}}, slots)
    assert not place(piece, BASE, wrong)
    other_slot = Image(EM_AARCH64, BASE, linked(tmc, slot + 8), {"__TMC_END__": {tmc}}, slots)
    assert not place(piece, BASE, other_slot)


def test_x86_64_s_got_load_may_be_relaxed_but_must_still_reach_its_symbol():
    # crti.o's `mov __gmon_start__@GOTPCREL(%rip), %rax`, a REX_GOTPCRELX.
    code = b"\x48\x8b\x05\0\0\0\0\x48\x85\xc0"
    piece = _piece(code, _external(3, 42, "__gmon_start__", -4, weak=True), machine=EM_X86_64)
    slot = 0x3FD0

    def linked(head, field):
        return head + struct.pack("<i", field) + b"\x48\x85\xc0"

    slots = {"__gmon_start__": {slot}}
    through_got = linked(b"\x48\x8b\x05", slot - (BASE + 3) - 4)
    assert place(piece, BASE, Image(EM_X86_64, BASE, through_got, slots=slots))
    assert not place(piece, BASE, Image(EM_X86_64, BASE, through_got))  # no slot for it
    # A static link: `mov $0x0, %rax`, as nothing defines it.
    assert place(piece, BASE, Image(EM_X86_64, BASE, linked(b"\x48\xc7\xc0", 0)))
    assert not place(piece, BASE, Image(EM_X86_64, BASE, linked(b"\x48\xc7\xc0", 0x400)))
    # lea of a symbol the binary defines.
    defined = {"__gmon_start__": {0x2000}}
    lea = linked(b"\x48\x8d\x05", 0x2000 - (BASE + 3) - 4)
    assert place(piece, BASE, Image(EM_X86_64, BASE, lea, addresses=defined))
    # Another opcode there: not the toolchain's.
    assert not place(piece, BASE, Image(EM_X86_64, BASE, linked(b"\x48\x8b\x0d", 0), slots=slots))


def test_x86_64_s_call_reaches_its_symbol_through_its_plt_stub():
    code = b"\xe8\0\0\0\0\xc3"
    piece = _piece(code, _external(1, 4, "__cxa_finalize", -4), machine=EM_X86_64)
    stub, slot = 0x1030, 0x3FE0
    call = b"\xe8" + struct.pack("<i", stub - (BASE + 5)) + b"\xc3"
    slots = {"__cxa_finalize": {slot}}
    assert place(piece, BASE, Image(EM_X86_64, BASE, call, slots=slots, stubs={stub: slot}))
    assert not place(piece, BASE, Image(EM_X86_64, BASE, call, slots=slots, stubs={stub: slot + 8}))


def test_a_relocation_peeky_doesnt_read_never_matches():
    piece = _piece(RET, unread="a relocation of kind 257 at 0x0 of its .text")
    assert not place(piece, BASE, Image(EM_AARCH64, BASE, RET))
    assert place(_piece(RET), BASE, Image(EM_AARCH64, BASE, RET))
    assert not place(_piece(RET), BASE, Image(EM_X86_64, BASE, RET))


def test_an_archive_past_the_read_limit_says_so(tmp_path):
    archive = tmp_path / "libgcc.a"
    archive.write_bytes(b"!<arch>\n" + b"\0" * 100)
    with pytest.raises(ValueError, match="exceeds Peeky's read limit"):
        list(archive_members(archive, b"init_have_lse_atomics", limit=64))


def test_an_address_split_over_adrp_and_its_low_bits_is_checked_whole():
    # Two GOT slots, 0x1000 and 0x2008: an ADRP to the first's page and a load of the
    # second's low bits address 0x1008, neither.
    code = _adrp(0, 0) + _low(0xF9400000, 0, 8)
    piece = _piece(code, _external(0, 311, "__gmon_start__"), _external(4, 312, "__gmon_start__"))

    def linked(page, low):
        return Image(EM_AARCH64, BASE, _adrp(BASE, page) + _low(0xF9400000, low, 8), slots=slots)

    slots = {"__gmon_start__": {0x1000, 0x2008}}
    assert place(piece, BASE, linked(0x2000, 0x2008))
    assert place(piece, BASE, linked(0x1000, 0x1000))
    assert not place(piece, BASE, linked(0x1000, 0x2008))
    # The same for an address: __TMC_END__ at 0x20030, a symbol at 0x31008.
    code = _adrp(0, 0) + _low(0x91000000, 0)
    tmc = Relocation(0, 275, 0, None, "__TMC_END__", 0, True, ".data")
    piece = _piece(code, tmc, Relocation(4, 277, 0, None, "__TMC_END__", 0, True, ".data"))
    defined = {"__TMC_END__": {0x20030, 0x31008}}

    def addressing(page, low):
        data = _adrp(BASE, page) + _low(0x91000000, low)
        return Image(EM_AARCH64, BASE, data, addresses=defined)

    assert place(piece, BASE, addressing(0x20000, 0x30))
    assert not place(piece, BASE, addressing(0x20000, 0x008))
    # A low part read through another register than the ADRP wrote: no pair.
    other = _piece(
        _adrp(0, 0, 1) + _low(0x91000000, 0),
        tmc,
        Relocation(4, 277, 0, None, "__TMC_END__", 0, True, ".data"),
    )
    data = _adrp(BASE, 0x20000, 1) + _low(0x91000000, 0x30)
    assert not place(other, BASE, Image(EM_AARCH64, BASE, data, addresses=defined))


def test_only_a_weak_symbol_nothing_defines_is_0():
    # `mov $0x0, %rax` where crti.o loads __gmon_start__: a static link of a weak symbol.
    code = b"\x48\x8b\x05\0\0\0\0"
    linked = Image(EM_X86_64, BASE, b"\x48\xc7\xc0" + struct.pack("<i", 0))
    weak = _piece(code, _external(3, 42, "__gmon_start__", -4, weak=True), machine=EM_X86_64)
    assert place(weak, BASE, linked)
    strong = _piece(code, _external(3, 42, "__gmon_start__", -4), machine=EM_X86_64)
    assert not place(strong, BASE, linked)
    # A call to a strong symbol nothing defines: nowhere.
    call = _piece(_bl(0, 0) + RET, _external(0, 283, "exit"))
    assert not place(call, BASE, Image(EM_AARCH64, BASE, _bl(BASE, 0) + RET))
    weak_call = _piece(_bl(0, 0) + RET, _external(0, 283, "exit", weak=True))
    assert place(weak_call, BASE, Image(EM_AARCH64, BASE, _bl(BASE, 0) + RET))


def _member(name, body):
    header = f"{name:<16}{0:<12}{0:<6}{0:<6}{644:<8}{len(body):<10}`\n".encode()
    return header + body + (b"\n" if len(body) % 2 else b"")


def test_an_archive_s_members_are_found_by_the_names_its_symbol_index_says_they_define(tmp_path):
    # A GNU archive's `/` member: a big-endian count, each name's member offset, the names.
    atexit = b"\x7fELF" + b"\0atexit\0" + b"\0" * 4
    extend = b"\x7fELF" + b"\0__extenddftf2\0\0"
    offsets = []
    at = 8 + 60 + (4 + 4 * 2 + len(b"atexit\0__extenddftf2\0") + 1) // 2 * 2
    for body in (atexit, extend):
        offsets.append(at)
        at += 60 + len(body) + len(body) % 2
    index = struct.pack(">I", 2) + b"".join(struct.pack(">I", each) for each in offsets)
    index += b"atexit\0__extenddftf2\0"
    archive = tmp_path / "libc_nonshared.a"
    archive.write_bytes(
        b"!<arch>\n"
        + _member("/", index)
        + _member("atexit.oS/", atexit)
        + _member("extenddftf2.o/", extend)
    )
    assert [name for name, _ in archive_members(archive, defining={"atexit"})] == ["atexit.oS"]
    assert [name for name, _ in archive_members(archive, defining={"__extenddftf2"})] == [
        "extenddftf2.o"
    ]
    assert list(archive_members(archive, defining={"nlr_push"})) == []
    # By bytes, as libgcc's local constructor is found.
    assert [name for name, _ in archive_members(archive, b"__extenddftf2")] == ["extenddftf2.o"]


def test_why_a_piece_isn_t_the_code_says_which_way():
    from peeky.native_startup import BYTES, MACHINE, TARGET, UNREAD

    piece = _piece(_bl(0, 0) + RET, _external(0, 283, "__cxa_atexit"), atexit=0)
    linked = _bl(BASE, 0x700) + RET
    # Its bytes, its call to where the binary defines it: the code, reaching that.
    found = place(piece, BASE, Image(EM_AARCH64, BASE, linked, {"__cxa_atexit": {0x700}}))
    assert found and found.reaches == ()
    inside = Image(EM_AARCH64, BASE, linked, {"__cxa_atexit": {0x700}}, code=[(0x700, 0x800)])
    assert place(piece, BASE, inside).reaches == ((0x700, "__cxa_atexit"),)
    # Its bytes, but its call elsewhere: the relocation, not the bytes.
    missed = place(piece, BASE, Image(EM_AARCH64, BASE, linked, {"__cxa_atexit": {0x800}}))
    assert not missed and missed.kind == TARGET
    assert missed.why == (
        "its bytes are crtbeginS.o's, but its relocation at +0x0, to __cxa_atexit, points elsewhere"
    )
    assert place(piece, BASE, Image(EM_AARCH64, BASE, MOV + RET)).kind == BYTES
    # Before any byte is compared: a relocation Peeky doesn't read, another machine.
    unread = _piece(RET, unread="a relocation of kind 257 at 0x0 of its .text")
    assert place(unread, BASE, Image(EM_AARCH64, BASE, RET)).kind == UNREAD
    assert place(_piece(RET), BASE, Image(EM_X86_64, BASE, RET)).why == (
        "Peeky can't compare it with crtbeginS.o, built for AArch64, not x86-64"
    )
    assert place(_piece(RET), BASE, Image(EM_X86_64, BASE, RET)).kind == MACHINE
    # x86-64: other code where a call's field is, though its field points nowhere, is
    # other bytes.
    call = _piece(b"\xe8\0\0\0\0\xc3", _external(1, 4, "f", -4), machine=EM_X86_64)
    other = Image(EM_X86_64, BASE, b"\xe9\x10\0\0\0\xc3")
    assert place(call, BASE, other).kind == BYTES
    assert place(call, BASE, Image(EM_X86_64, BASE, b"\xe8\x10\0\0\0\xc3")).kind == TARGET
    # A GOT load on an instruction Peeky doesn't read: it can't compare it.
    test = _piece(b"\x48\x85\x05\0\0\0\0", _external(3, 42, "g", -4), machine=EM_X86_64)
    assert place(test, BASE, Image(EM_X86_64, BASE, b"\x48\x85\x05\0\0\0\0")).kind == UNREAD


def _compiler(tmp_path, answers, status=0):
    """A compiler answering `-print-file-name=…` and `-print-libgcc-file-name` from
    ``answers`` ({option: path}), logging its arguments to ``log``."""
    log = tmp_path / "log"
    lines = [f'printf "%s\\n" "$*" >> "{log}"']
    for option, answer in answers.items():
        lines.append(f'case "$*" in *{option}) echo "{answer}"; exit {status};; esac')
    lines.append(f'echo "${{@: -1}}" | sed "s/.*=//"; exit {status}')
    compiler = tmp_path / "clang"
    compiler.write_text("#!/bin/bash\n" + "\n".join(lines) + "\n")
    compiler.chmod(0o755)
    return compiler, log


def test_the_toolchain_is_asked_with_the_units_options_and_says_what_it_can_t_search(tmp_path):
    from peeky.native_startup import toolchain_startup

    bad = tmp_path / "libc_nonshared.a"
    bad.write_bytes(b"not an archive")
    compiler, log = _compiler(tmp_path, {"-print-file-name=libc_nonshared.a": bad})
    env = {"PATH": "/usr/bin:/bin"}
    options = ("--target=aarch64-linux-gnu", "--sysroot=/opt/sysroot")
    pieces, why, unsearched = toolchain_startup(compiler, env, 10, ["atexit"], options)
    assert pieces == []
    asked = log.read_text().splitlines()
    assert asked and all(
        line.startswith("--no-default-config --target=aarch64-linux-gnu --sysroot=/opt/sysroot")
        for line in asked
    ), asked
    # An archive the compiler finds none of isn't linked; one it can't read can't be
    # searched.
    assert f"{compiler} finds no libgcc" in why and "finds no crti.o" in why
    assert unsearched == f"Peeky can't read {bad} (not an ar archive)"
    # A compiler that fails can't say.
    failing, _ = _compiler(tmp_path, {}, status=1)
    _, _, unsearched = toolchain_startup(failing, env, 10, ["atexit"])
    assert unsearched.startswith(f"{failing} -print-libgcc-file-name failed (exit status 1)")
