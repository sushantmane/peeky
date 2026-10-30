"""One decoder of a method's Java bytecode (JVMS 6.5), shared by the loop-header check
(``loop_headers.java_instructions``) and a local's reads after a stop
(``java_metadata.reads_after``): each instruction's length, how control leaves it (the
next instruction, a branch, a jump or switch, a return, a throw) and the local slot it
reads or writes. A method's exception handlers come from its Code attribute
(``java_metadata._class_metadata``)."""

from typing import NamedTuple


class Instruction(NamedTuple):
    pc: int
    # The next instruction's pc.
    end: int
    # "fall" (to the next), "branch" (to its target or the next), "jump" (to its
    # targets only: goto, a switch), "return", "stop" (athrow), "indirect" (a subroutine's
    # jsr or ret, which modern javac never emits: where it goes is not decoded).
    kind: str
    targets: tuple
    # The local slot it reads (a load, iinc, ret) or stores into (a store, iinc).
    reads: int | None
    writes: int | None


# Each opcode's length where it is fixed; tableswitch (0xaa), lookupswitch (0xab) and
# wide (0xc4) are measured from their operands, and an opcode not here does not decode.
_LENGTHS = {
    **dict.fromkeys(range(0x00, 0x10), 1),
    0x10: 2,
    0x11: 3,
    0x12: 2,
    0x13: 3,
    0x14: 3,
    **dict.fromkeys(range(0x15, 0x1A), 2),
    **dict.fromkeys(range(0x1A, 0x36), 1),
    **dict.fromkeys(range(0x36, 0x3B), 2),
    **dict.fromkeys(range(0x3B, 0x84), 1),
    0x84: 3,
    **dict.fromkeys(range(0x85, 0x99), 1),
    **dict.fromkeys(range(0x99, 0xA9), 3),
    0xA9: 2,
    **dict.fromkeys(range(0xAC, 0xB2), 1),
    **dict.fromkeys(range(0xB2, 0xB9), 3),
    0xB9: 5,
    0xBA: 5,
    0xBB: 3,
    0xBC: 2,
    0xBD: 3,
    0xBE: 1,
    0xBF: 1,
    0xC0: 3,
    0xC1: 3,
    0xC2: 1,
    0xC3: 1,
    0xC5: 4,
    0xC6: 3,
    0xC7: 3,
    0xC8: 5,
    0xC9: 5,
}
# Loads, stores, iinc and ret with a slot operand (one byte, or two after `wide`).
_LOADS = frozenset(range(0x15, 0x1A))
_STORES = frozenset(range(0x36, 0x3B))
_WIDENED = _LOADS | _STORES | {0x84, 0xA9}


def _access(op, slot):
    """(slot read, slot written) of an instruction with a slot operand."""
    if op in _LOADS or op == 0xA9:
        return slot, None
    if op in _STORES:
        return None, slot
    return (slot, slot) if op == 0x84 else (None, None)


def decode(code):
    """[Instruction] of a method's bytecode, in pc order, or None where it does not
    decode: an unknown opcode, a truncated instruction or switch, a target outside the
    code or inside an instruction."""
    code = bytes(code)
    size = len(code)

    def signed(at, width):
        if at < 0 or at + width > size:
            raise IndexError
        return int.from_bytes(code[at : at + width], "big", signed=True)

    found, pc = [], 0
    try:
        while pc < size:
            op = code[pc]
            targets, kind, reads, writes = (), "fall", None, None
            if op == 0xC4:
                inner = code[pc + 1]
                if inner not in _WIDENED:
                    return None
                length = 6 if inner == 0x84 else 4
                reads, writes = _access(inner, signed(pc + 2, 2) & 0xFFFF)
                if inner == 0xA9:
                    kind = "indirect"
            elif op in (0xAA, 0xAB):
                at = (pc + 4) & ~3
                if op == 0xAA:
                    low, high = signed(at + 4, 4), signed(at + 8, 4)
                    if not 0 <= high - low < 65536:
                        return None
                    offsets = [signed(at + 12 + 4 * i, 4) for i in range(high - low + 1)]
                    length = at + 12 + 4 * (high - low + 1) - pc
                else:
                    pairs = signed(at + 4, 4)
                    if not 0 <= pairs < 65536:
                        return None
                    offsets = [signed(at + 12 + 8 * i, 4) for i in range(pairs)]
                    length = at + 8 + 8 * pairs - pc
                targets = tuple(pc + offset for offset in (signed(at, 4), *offsets))
                kind = "jump"
            elif op not in _LENGTHS:
                return None
            else:
                length = _LENGTHS[op]
                if op in _WIDENED:
                    reads, writes = _access(op, code[pc + 1])
                elif 0x1A <= op <= 0x2D:
                    reads = (op - 0x1A) % 4
                elif 0x3B <= op <= 0x4E:
                    writes = (op - 0x3B) % 4
                if 0x99 <= op <= 0xA6 or op in (0xC6, 0xC7):
                    kind, targets = "branch", (pc + signed(pc + 1, 2),)
                elif op == 0xA7:
                    kind, targets = "jump", (pc + signed(pc + 1, 2),)
                elif op == 0xC8:
                    kind, targets = "jump", (pc + signed(pc + 1, 4),)
                elif op in (0xA8, 0xA9, 0xC9):
                    kind = "indirect"
                elif 0xAC <= op <= 0xB1:
                    kind = "return"
                elif op == 0xBF:
                    kind = "stop"
            if any(not 0 <= target < size for target in targets) or pc + length > size:
                return None
            found.append(Instruction(pc, pc + length, kind, targets, reads, writes))
            pc += length
    except IndexError:
        return None
    starts = {each.pc for each in found}
    if any(target not in starts for each in found for target in each.targets):
        # A branch into the middle of an instruction: not bytecode a JVM runs.
        return None
    return found or None
