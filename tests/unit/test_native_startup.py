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
    """A linked binary, as native_startup.Image reads it: its code at ``base``."""

    def __init__(self, machine, base, data, addresses=None, slots=None, stubs=None):
        self.machine, self.base, self.data = machine, base, data
        self.addresses = addresses or {}
        self.slots, self.stubs = slots or {}, stubs or {}

    def read(self, address, length):
        at = address - self.base
        return self.data[at : at + length] if 0 <= at and at + length <= len(self.data) else None

    def targets(self, name):
        return self.addresses.get(name) or {0}

    def got(self, name):
        return self.slots.get(name, set())

    def stub(self, address):
        return self.stubs.get(address)


def _piece(data, *relocations, machine=EM_AARCH64, unread=None, **symbols):
    return StartupPiece("crtbeginS.o", ".text", data, machine, relocations, symbols, unread)


def _external(offset, kind, name, addend=0):
    return Relocation(offset, kind, addend, None, name, 0, False, None)


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
    piece = _piece(code, _external(3, 42, "__gmon_start__", -4), machine=EM_X86_64)
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
