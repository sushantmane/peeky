"""The toolchain's startup objects and static archives, and whether a binary's code is
theirs as linked.

A program links code no unit of its debug information covers: crtbegin.o's, crti.o's and
crtn.o's, and the members of the toolchain's static archives it calls: libc_nonshared.a's
(`atexit`) and libgcc's (`__extenddftf2`). A program may use those names too (`frame_dummy`,
`atexit`), so such code is treated as equivalent to the toolchain's only where its bytes
are, as the linker relocates them. Each relocation's immediate field is decoded, and where
it points checked: into the matched code itself, at the binary's symbol of the name the
object names, at a GOT slot or PLT stub for it, or at 0 for a weak symbol nothing defines.
A relocation of a kind not seen in the real startup objects (``_AARCH64``, ``_X86_64``)
never matches. Only 64-bit little-endian ELF is read: Linux's toolchains.
"""

import mmap
import os
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

STARTUP_OBJECTS = ("crtbegin.o", "crtbeginS.o", "crtbeginT.o", "crti.o", "crtn.o")
# libgcc's AArch64 constructor, found in its archive by name.
LIBGCC_STARTUP = b"init_have_lse_atomics"
# The static archives besides libgcc's (`-print-libgcc-file-name`) a program links, as the
# compiler's `-print-file-name` finds them: glibc's functions every program links whole.
ARCHIVES = ("libc_nonshared.a",)
# Peeky reads at most this much of libgcc's archive.
MAX_ARCHIVE = 1 << 26

EM_AARCH64, EM_X86_64 = 183, 62
# AArch64 relocations in code, as the startup objects have them (glibc 2.39's and GCC
# 13's on Ubuntu 24.04): {kind: (immediate bits, how they are read, scale)}.
_AARCH64 = {
    283: (0x03FFFFFF, "branch", 4),  # CALL26
    282: (0x03FFFFFF, "branch", 4),  # JUMP26
    275: (0x60FFFFE0, "page", 1),  # ADR_PREL_PG_HI21
    277: (0x003FFC00, "low", 1),  # ADD_ABS_LO12_NC
    278: (0x003FFC00, "low", 1),  # LDST8_ABS_LO12_NC
    286: (0x003FFC00, "low", 8),  # LDST64_ABS_LO12_NC
    311: (0x60FFFFE0, "got page", 1),  # ADR_GOT_PAGE
    312: (0x003FFC00, "got low", 8),  # LD64_GOT_LO12_NC
}
# x86-64's: PC32, PLT32, GOTPCREL, REX_GOTPCRELX, 32, 32S.
_X86_64 = {2: "pc", 4: "pc", 9: "got", 42: "rex got", 10: "absolute", 11: "signed"}
# A dynamic relocation filling a GOT slot with a symbol's address: GLOB_DAT, JUMP_SLOT.
_SLOTS = {EM_AARCH64: (1025, 1026), EM_X86_64: (6, 7)}
# Where an empty section of a startup object is, as the binary keeps no symbol of it:
# crtbegin.o's `.tm_clone_table` starts the table crtend.o's __TMC_END__ ends, empty
# unless the program uses transactional memory.
_ANCHORS = {".tm_clone_table": ("__TMC_LIST__", "__TMC_END__")}


@dataclass(frozen=True)
class Relocation:
    """One relocation in a StartupPiece: at ``offset``, of ``kind``, with ``addend``, to
    a target: in this piece (``own``: its offset there), or ``delta`` past a symbol
    ``name``, which the object defines (``defined``; ``section`` holds it) or doesn't;
    ``weak``: a weak symbol, which may be left undefined (0)."""

    offset: int
    kind: int
    addend: int
    own: int | None
    name: str | None
    delta: int
    defined: bool
    section: str | None
    weak: bool = False


@dataclass(frozen=True)
class StartupPiece:
    """A code section of one of the toolchain's startup objects: where it comes from
    (``origin``), its name, its bytes, its machine, its relocations and its symbols
    {name: offset}. ``unread``: a relocation kind Peeky doesn't read, which no binary's
    code can be matched against."""

    origin: str
    section: str
    data: bytes
    machine: int
    relocations: tuple
    symbols: dict
    unread: str | None = None


def toolchain_startup(compiler, env, timeout=10, names=(), options=()):
    """(pieces, why, unsearched): the StartupPieces of the startup objects the config's
    ``compiler`` links (STARTUP_OBJECTS, as its `-print-file-name` finds them, and
    libgcc's archive member holding LIBGCC_STARTUP), and of each member of its static
    archives (libgcc's, and ARCHIVES) that defines one of ``names``; why some can't be
    read, or None; and why some archive can't be searched, or None: the compiler fails,
    or the archive can't be read. One the compiler finds none of isn't linked: it isn't
    the toolchain's. ``options``: what the binary's units record of the toolchain
    (`--target=…`), passed when asking."""

    def find(option):
        try:
            done = subprocess.run(
                [compiler, "--no-default-config", *options, option],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"{compiler} {option} failed ({exc})", True
        path = done.stdout.strip()
        if done.returncode:
            return None, f"{compiler} {option} failed (exit status {done.returncode})", True
        if not os.path.isabs(path) or not os.path.isfile(path):
            wanted = option.split("=", 1)[1] if "=" in option else "libgcc"
            return None, f"{compiler} finds no {wanted}", False
        return path, None, False

    pieces, missing, unsearched = [], [], []
    for name in STARTUP_OBJECTS:
        path, why, _ = find(f"-print-file-name={name}")
        if path is None:
            missing.append(why)
            continue
        try:
            with open(path, "rb") as stream:
                pieces += object_pieces(stream.read(1 << 22), name)
        except (OSError, ValueError, struct.error) as exc:
            missing.append(f"Peeky can't read {path} ({exc})")
    # LIBGCC_STARTUP is a local symbol, which the archive's symbol index leaves out.
    archives = [("-print-libgcc-file-name", (LIBGCC_STARTUP,))]
    archives += [(f"-print-file-name={name}", ()) for name in ARCHIVES if names]
    for option, wanted in archives:
        path, why, failed = find(option)
        if path is None:
            missing.append(why)
            if failed:
                unsearched.append(why)
            continue
        try:
            for member, data in archive_members(path, wanted, defining=names):
                pieces += object_pieces(data, f"{Path(path).name}({member})")
        except (OSError, ValueError, struct.error) as exc:
            missing.append(f"Peeky can't read {path} ({exc})")
            unsearched.append(missing[-1])
    return pieces, "; ".join(missing) or None, "; ".join(unsearched) or None


def archive_members(path, wanted=(), limit=MAX_ARCHIVE, defining=()):
    """(name, bytes) of each ELF member of the `ar` archive ``path`` whose bytes hold
    ``wanted`` (bytes, or a tuple of them: any), or that the archive's symbol index says
    defines one of ``defining`` (names). ValueError where it is no archive, or larger than
    ``limit``."""
    wanted = (wanted,) if isinstance(wanted, bytes) else tuple(wanted)
    defining = frozenset(defining)
    if os.path.getsize(path) > limit:
        raise ValueError(f"the archive exceeds Peeky's read limit, {limit >> 20} MiB")
    with open(path, "rb") as stream:
        data = stream.read(limit)
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("not an ar archive")
    at, names, index = 8, b"", {}
    while at + 60 <= len(data):
        header = data[at : at + 60]
        if header[58:60] != b"`\n":
            raise ValueError("invalid member header")
        name, size = header[:16].rstrip(), int(header[48:58])
        if at + 60 + size > len(data):
            raise ValueError("a member past the archive's end")
        body = data[at + 60 : at + 60 + size]
        if name == b"//":
            names = body
        elif name in (b"/", b"/SYM64/"):
            if defining:
                index = _symbol_index(body, 8 if name == b"/SYM64/" else 4)
        else:
            if name.startswith(b"/") and name[1:].isdigit():
                start = int(name[1:])
                name = names[start : names.find(b"/\n", start)]
            if body.startswith(b"\x7fELF") and (
                any(each in body for each in wanted) or index.get(at, frozenset()) & defining
            ):
                yield name.rstrip(b"/").decode("latin-1"), body
        at += 60 + size + (size & 1)


def _symbol_index(body, width):
    """{member header's offset: the global names it defines}, from a GNU archive's symbol
    index (`/`, or `/SYM64/` with 8-byte offsets): a big-endian count, the offsets, then the
    names."""
    form = ">I" if width == 4 else ">Q"
    if len(body) < width:
        raise ValueError("invalid symbol index")
    count = struct.unpack_from(form, body)[0]
    start = width * (count + 1)
    if start > len(body):
        raise ValueError("invalid symbol index")
    offsets = [struct.unpack_from(form, body, width * (at + 1))[0] for at in range(count)]
    strings = body[start:].split(b"\0")
    if len(strings) < count:
        raise ValueError("invalid symbol index")
    found = {}
    for offset, name in zip(offsets, strings, strict=False):
        found.setdefault(offset, set()).add(name.decode("latin-1"))
    return {offset: frozenset(each) for offset, each in found.items()}


def _headers(blob):
    if blob[:4] != b"\x7fELF" or blob[4:6] != b"\x02\x01" or len(blob) < 64:
        raise ValueError("not a 64-bit little-endian ELF file")
    machine = struct.unpack_from("<H", blob, 18)[0]
    offset = struct.unpack_from("<Q", blob, 40)[0]
    entry, count, names_at = struct.unpack_from("<HHH", blob, 58)
    if entry != 64 or not 0 < count <= 65535 or names_at >= count:
        raise ValueError("invalid section headers")
    if offset + count * 64 > len(blob):
        raise ValueError("section headers past the file's end")
    sections = [struct.unpack_from("<IIQQQQIIQQ", blob, offset + 64 * at) for at in range(count)]
    return machine, sections, names_at


def _string(table, at):
    end = table.find(b"\0", at)
    return table[at:end].decode("latin-1") if 0 <= at and end >= 0 else ""


def object_pieces(blob, origin):
    """The StartupPieces of a 64-bit little-endian ELF object's code sections."""
    machine, sections, names_at = _headers(blob)

    def body(index):
        _, kind, _, _, at, length, *_ = sections[index]
        if kind == 8:  # SHT_NOBITS
            return b""
        if at + length > len(blob):
            raise ValueError("a section past the object's end")
        return blob[at : at + length]

    titles = body(names_at)
    title = {index: _string(titles, header[0]) for index, header in enumerate(sections)}
    code = [
        index
        for index, (_, kind, flags, *_) in enumerate(sections)
        if kind == 1 and flags & 0x4 and sections[index][5]  # PROGBITS, SHF_EXECINSTR
    ]
    table = next((index for index, header in enumerate(sections) if header[1] == 2), None)
    symbols = []
    if table is not None:
        _, _, _, _, _, length, link, _, _, width = sections[table]
        if width != 24 or link >= len(sections):
            raise ValueError("invalid symbol table")
        strings = body(link)
        for name, info, _, shndx, value, _ in struct.iter_unpack(
            "<IBBHQQ", body(table)[: length - length % 24]
        ):
            symbols.append((_string(strings, name), info, shndx, value))
    # A named symbol of each section, nearest its start, to find the section by.
    named = {}
    for name, info, shndx, value in symbols:
        if name and not name.startswith("$") and info & 0xF not in (3, 4) and 0 < shndx < 0xFF00:
            named.setdefault(shndx, []).append((value, name))
    pieces = []
    for index in code:
        relocations, unread = [], None
        for at, (_, kind, _, _, _, length, link, info, _, width) in enumerate(sections):
            if kind != 4 or info != index or width != 24:  # SHT_RELA for this section
                continue
            for place, packed, addend in struct.iter_unpack(
                "<QQq", body(at)[: length - length % 24]
            ):
                kind_of, symbol = packed & 0xFFFFFFFF, packed >> 32
                known = (
                    _AARCH64 if machine == EM_AARCH64 else _X86_64 if machine == EM_X86_64 else {}
                )
                if kind_of not in known:
                    unread = f"a relocation of kind {kind_of} at 0x{place:x} of its {title[index]}"
                    continue
                name, sinfo, shndx, value = symbols[symbol]
                if shndx == index:
                    relocations.append(
                        Relocation(place, kind_of, addend, value, None, 0, True, None)
                    )
                elif shndx == 0:
                    weak = sinfo >> 4 == 2  # STB_WEAK
                    relocations.append(
                        Relocation(place, kind_of, addend, None, name, 0, False, None, weak)
                    )
                elif sinfo & 0xF == 3 or not name:
                    # A section's own symbol: by the section's named symbol nearest its start.
                    found = sorted(named.get(shndx, []))
                    anchor = found[0] if found else (0, None)
                    relocations.append(
                        Relocation(
                            place,
                            kind_of,
                            addend,
                            None,
                            anchor[1],
                            value - anchor[0],
                            True,
                            title[shndx],
                        )
                    )
                else:
                    relocations.append(
                        Relocation(place, kind_of, addend, None, name, 0, True, title[shndx])
                    )
        own = {
            name: value
            for name, info, shndx, value in symbols
            if shndx == index and name and not name.startswith("$") and info & 0xF in (0, 2)
        }
        pieces.append(
            StartupPiece(
                origin, title[index], body(index), machine, tuple(relocations), own, unread
            )
        )
    return pieces


class Image:
    """What placing a StartupPiece reads of a linked ELF executable: its bytes by
    address, its symbols' addresses by name, its GOT slots by the symbol their dynamic
    relocation names, and its PLT stubs."""

    def __init__(self, path):
        with open(path, "rb") as stream:
            blob = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        self.machine, sections, names_at = _headers(blob)
        titles = blob[sections[names_at][4] : sections[names_at][4] + sections[names_at][5]]
        self.blob = blob
        self.mapped = [
            (_string(titles, name), address, length, at, kind)
            for name, kind, flags, address, at, length, *_ in sections
            if flags & 0x2 and kind in (1, 8)  # SHF_ALLOC; PROGBITS, NOBITS
        ]
        self.addresses, self.slots = {}, {}
        for index, (_, kind, _, _, at, length, link, _, _, width) in enumerate(sections):
            if kind == 2 and width == 24 and link < len(sections):  # SHT_SYMTAB
                strings = blob[sections[link][4] : sections[link][4] + sections[link][5]]
                for name, info, _, shndx, value, _ in struct.iter_unpack(
                    "<IBBHQQ", blob[at : at + length - length % 24]
                ):
                    if shndx and info & 0xF not in (3, 4):
                        self.addresses.setdefault(
                            _string(strings, name).split("@", 1)[0], set()
                        ).add(value)
            elif kind == 4 and width == 24 and link < len(sections) and sections[link][1] == 11:
                # Dynamic relocations against the dynamic symbol table.
                symbols = sections[link]
                strings = sections[symbols[6]]
                names = blob[strings[4] : strings[4] + strings[5]]
                for place, packed, _ in struct.iter_unpack(
                    "<QQq", blob[at : at + length - length % 24]
                ):
                    if packed & 0xFFFFFFFF in _SLOTS.get(self.machine, ()):
                        entry = symbols[4] + (packed >> 32) * 24
                        name = _string(names, struct.unpack_from("<I", blob, entry)[0])
                        self.slots.setdefault(name, set()).add(place)
        self.plt = [
            (address, address + length)
            for name, address, length, _, _ in self.mapped
            if name.startswith((".plt", ".iplt"))
        ]
        # Code but the linker's stubs: SHF_EXECINSTR.
        self.code = [
            (address, address + length)
            for name, kind, flags, address, _, length, *_ in sections
            if flags & 0x6 == 0x6 and not _string(titles, name).startswith((".plt", ".iplt"))
        ]
        self.relocated = {slot for each in self.slots.values() for slot in each}
        self.cache = {}

    def read(self, address, length):
        """The ``length`` bytes at ``address``, within one section the file holds; or None."""
        for _, low, size, at, kind in self.mapped:
            if kind == 1 and low <= address and address + length <= low + size:
                return self.blob[at + address - low : at + address - low + length]
        return None

    def runs(self, address):
        """Whether ``address`` is in code, but the linker's stubs."""
        return any(low <= address < high for low, high in self.code)

    def got(self, name, weak=False):
        """The GOT slots holding ``name``'s address: those a dynamic relocation names it
        in, or, of those none names, whose contents are its address: 0 for a ``weak``
        symbol nothing defines. A slot filled at load (a dynamic link) holds 0 in the
        file: there only a relocation names a weak symbol's."""
        if (name, weak) not in self.cache:
            found = set(self.slots.get(name, ()))
            wanted = set(self.addresses.get(name) or ({0} if weak else ()))
            if self.relocated:
                wanted = wanted - {0}
            for section, low, size, at, kind in self.mapped:
                if section in (".got", ".got.plt") and kind == 1:
                    for slot in range(low, low + size - size % 8, 8):
                        if (
                            slot not in self.relocated
                            and struct.unpack_from("<Q", self.blob, at + slot - low)[0] in wanted
                        ):
                            found.add(slot)
            self.cache[(name, weak)] = found
        return self.cache[(name, weak)]

    def stub(self, address):
        """The GOT slot the PLT stub at ``address`` jumps through, or None."""
        section = next(((low, high) for low, high in self.plt if low <= address < high), None)
        if section is None:
            return None
        code = self.read(address, min(16, section[1] - address))
        if code is None or len(code) < 6:
            return None
        if self.machine == EM_AARCH64:
            if len(code) < 16:
                return None
            words = list(struct.unpack("<4I", code))
            if words[0] in (0xD503245F, 0xD503201F):  # bti c, nop
                more = self.read(address + 16, 4)
                words = words[1:] + list(struct.unpack("<I", more)) if more else words[1:]
                address += 4
            adrp, load = words[0], words[1]
            if adrp & 0x9F00001F != 0x90000010 or load & 0xFFC003FF != 0xF9400211:
                return None  # adrp x16; ldr x17, [x16, #lo]
            return _page(address, adrp) + ((load >> 10) & 0xFFF) * 8
        at = 4 if code.startswith(b"\xf3\x0f\x1e\xfa") else 0  # endbr64
        at += 1 if code[at : at + 1] == b"\xf2" else 0  # bnd
        if code[at : at + 2] != b"\xff\x25" or len(code) < at + 6:  # jmp *disp32(%rip)
            return None
        return address + at + 6 + struct.unpack_from("<i", code, at + 2)[0]


def _signed(value, bits):
    return value - (1 << bits) if value & 1 << (bits - 1) else value


def _page(place, word):
    immediate = (word >> 29) & 0x3 | ((word >> 5) & 0x7FFFF) << 2
    return (place & ~0xFFF) + (_signed(immediate, 21) << 12)


def place(piece, base, image):
    """Whether ``piece`` is the code at ``base`` in ``image``, as the linker relocated it:
    its bytes, but each relocation's immediate field, which must point where the
    relocation names. A Placed: true where it is; else why not."""
    if piece.unread is not None:
        return Placed(
            UNREAD,
            f"Peeky can't compare it with {piece.origin}, which holds {piece.unread}, "
            "a kind Peeky doesn't read",
        )
    if piece.machine != image.machine:
        return Placed(
            MACHINE,
            f"Peeky can't compare it with {piece.origin}, built for {_machine(piece.machine)}, "
            f"not {_machine(image.machine)}",
        )
    data = image.read(base, len(piece.data))
    if data is None or not _same(piece, data, image.machine):
        return Placed(BYTES, f"its bytes are none of the toolchain's ({piece.origin})")
    for relocation in piece.relocations:
        kind = _check(piece, relocation, base, image, data)
        if kind is not None:
            return _missed(piece, relocation, kind)
    if image.machine == EM_AARCH64:
        missed = _split(piece, base, image, data)
        if missed is not None:
            return _missed(piece, *missed)
    return Placed(reaches=tuple(sorted(_reaches(piece, base, image))))


# What placing a StartupPiece may find: it is built for another machine; it holds a
# relocation Peeky doesn't read; its bytes differ, but its relocations' immediate fields;
# or one of its relocations points elsewhere.
MACHINE, UNREAD, BYTES, TARGET = "machine", "unread", "bytes", "target"
# Why a relocation can't be checked: an AArch64 LO12 no ADRP begins, or an x86-64 GOT
# load on an instruction Peeky doesn't read (its relaxations are a `mov`'s).
_UNPAIRED, _FORM = "unpaired", "form"
_MACHINES = {EM_AARCH64: "AArch64", EM_X86_64: "x86-64"}


@dataclass(frozen=True)
class Placed:
    """What placing a StartupPiece found: where it isn't the code there, of what ``kind``
    (MACHINE, UNREAD, BYTES, TARGET) and ``why``, as a phrase; where it is, the code
    outside it its relocations point at by the binary's symbols (``reaches``: (address,
    name) pairs), which may be the program's. True where it is the code."""

    kind: str | None = None
    why: str | None = None
    reaches: tuple = ()

    def __bool__(self):
        return self.kind is None


def _machine(machine):
    return _MACHINES.get(machine, f"machine {machine}")


def _missed(piece, relocation, kind):
    at = f"+0x{relocation.offset:x}"
    if kind == BYTES:
        return Placed(BYTES, f"its bytes are none of the toolchain's ({piece.origin})")
    if kind in (_UNPAIRED, _FORM):
        what = (
            "pair it with the ADRP that begins its address"
            if kind == _UNPAIRED
            else "read the instruction it is in"
        )
        return Placed(
            UNREAD,
            f"Peeky can't compare it with {piece.origin}: of its relocation at {at}, Peeky "
            f"can't {what}",
        )
    to = "into its own code" if relocation.own is not None else f"to {_target(relocation)}"
    return Placed(
        TARGET,
        f"its bytes are {piece.origin}'s, but its relocation at {at}, {to}, points elsewhere",
    )


def _target(relocation):
    if relocation.name:
        return relocation.name
    return f"its {relocation.section}" if relocation.section else "a symbol it doesn't name"


def _same(piece, data, machine):
    """Whether ``data`` is the piece's bytes, but its relocations' immediate fields (and
    the instruction an x86-64 GOT load may be relaxed to)."""
    if len(data) != len(piece.data):
        return False
    linked, original = bytearray(data), bytearray(piece.data)
    for relocation in piece.relocations:
        at = relocation.offset
        if at + 4 > len(data):
            return False
        if machine == EM_AARCH64:
            mask = _AARCH64[relocation.kind][0]
            now = struct.unpack_from("<I", data, at)[0]
            if (now ^ struct.unpack_from("<I", piece.data, at)[0]) & ~mask & 0xFFFFFFFF:
                return False
            spots = range(at, at + 4)
        elif _X86_64[relocation.kind] == "rex got" and at >= 3:
            spots = range(at - 3, at + 4)
        else:
            spots = range(at, at + 4)
        for each in spots:
            linked[each] = original[each] = 0
    return linked == original


def _reaches(piece, base, image):
    """The code outside the piece its relocations name by the binary's symbols: (address,
    name) of each, but the linker's stubs."""
    for relocation in piece.relocations:
        if relocation.own is None and relocation.name:
            for address in _expected(relocation, base, image) - {0}:
                if image.runs(address):
                    yield address, relocation.name


# AArch64's split addresses: the ADRP whose page each LO12 completes.
_PAGES = {277: 275, 278: 275, 286: 275, 312: 311}


def _split(piece, base, image, data):
    """None where each address the piece splits over an ADRP and an LO12 (an ADD, a load
    or a store) is, whole, one the relocations name; else (relocation, TARGET), or
    _UNPAIRED where an LO12 completes no ADRP. Each LO12 completes the nearest ADRP before
    it to the same target, into the register it reads; one with none can't be checked.
    An ADRP no LO12 completes is checked by its page."""

    def word(blob, at):
        return struct.unpack_from("<I", blob, at)[0]

    def allowed(relocation):
        if _AARCH64[relocation.kind][1].startswith("got"):
            return image.got(relocation.name, relocation.weak) if relocation.name else set()
        return {target + relocation.addend for target in _expected(relocation, base, image)}

    def target(relocation):
        return (
            relocation.own,
            relocation.name,
            relocation.delta,
            relocation.section,
            relocation.addend,
        )

    pages = [each for each in piece.relocations if each.kind in (275, 311)]
    completed = set()
    for low in piece.relocations:
        if low.kind not in _PAGES:
            continue
        register = (word(piece.data, low.offset) >> 5) & 0x1F
        paired = [
            page
            for page in pages
            if page.kind == _PAGES[low.kind]
            and page.offset < low.offset
            and target(page) == target(low)
            and word(piece.data, page.offset) & 0x1F == register
        ]
        if not paired:
            return low, _UNPAIRED
        page = max(paired, key=lambda each: each.offset)
        completed.add(page.offset)
        address = _page(base + page.offset, word(data, page.offset))
        address += ((word(data, low.offset) >> 10) & 0xFFF) * _AARCH64[low.kind][2]
        if address not in allowed(low):
            return low, TARGET
    for page in pages:
        if page.offset not in completed and _page(
            base + page.offset, word(data, page.offset)
        ) not in {each & ~0xFFF for each in allowed(page)}:
            return page, TARGET
    return None


def _expected(relocation, base, image):
    """Where the relocation's target S may be: in the piece, at the binary's symbol of
    its name (by the section's anchor where the binary keeps no symbol of an empty
    section), or 0 for a weak symbol nothing defines. A strong symbol nothing defines
    is nowhere: the piece doesn't match."""
    if relocation.own is not None:
        return {base + relocation.own}
    names = (relocation.name,) if relocation.name else ()
    if relocation.section in _ANCHORS and not any(name in image.addresses for name in names):
        names = _ANCHORS[relocation.section]
    found = set()
    for name in names:
        if name in image.addresses:
            found |= {address + relocation.delta for address in image.addresses[name]}
    if not found and not relocation.defined and relocation.weak:
        found = {0}
    return found


def _check(piece, relocation, base, image, data):
    """None where the relocation's immediate field holds what it must (its bytes but
    that field are _same's); else TARGET, BYTES where an x86-64 GOT load is relaxed to
    no instruction the linker writes, or _FORM where the object's instruction isn't a
    `mov` Peeky reads."""
    at = relocation.offset
    place_ = base + at
    targets = _expected(relocation, base, image)
    if image.machine == EM_AARCH64:
        mask, how, _ = _AARCH64[relocation.kind]
        if how != "branch":
            return None  # An address split over ADRP and its LO12: checked whole, by _split.
        word = struct.unpack_from("<I", data, at)[0]
        lands = place_ + _signed(word & mask, 26) * 4
        stub = image.stub(lands)
        good = lands in {target + relocation.addend for target in targets} or (
            not relocation.defined
            and stub is not None
            and stub in image.got(relocation.name, relocation.weak)
        )
        return None if good else TARGET
    how = _X86_64[relocation.kind]
    field = struct.unpack_from("<i", data, at)[0]
    if how in ("absolute", "signed"):
        value = field & 0xFFFFFFFF if how == "absolute" else field
        return None if value in {t + relocation.addend for t in targets} else TARGET
    if how == "pc":
        lands = field + place_ - relocation.addend
        stub = image.stub(lands)
        good = lands in targets or (
            relocation.kind == 4
            and not relocation.defined
            and stub is not None
            and stub in image.got(relocation.name, relocation.weak)
        )
        return None if good else TARGET
    slots = image.got(relocation.name, relocation.weak) if relocation.name else set()
    if how == "got":
        return None if field + place_ - relocation.addend in slots else TARGET
    # REX_GOTPCRELX: `mov sym@GOTPCREL(%rip), %reg`, or what the linker relaxes it to.
    if at < 3:
        return _FORM
    rex, opcode, modrm = piece.data[at - 3 : at]
    if opcode != 0x8B or modrm & 0xC7 != 0x05:
        return _FORM
    now = data[at - 3 : at]
    if now == bytes((rex, 0x8B, modrm)):  # still through the GOT
        return None if field + place_ - relocation.addend in slots else TARGET
    if now == bytes((rex, 0x8D, modrm)):  # lea sym(%rip), %reg
        return None if field + place_ - relocation.addend in targets - {0} else TARGET
    register = (modrm >> 3) & 7
    moved = (rex & ~0x04) | (0x01 if rex & 0x04 else 0)
    if now == bytes((moved, 0xC7, 0xC0 | register)):  # mov $sym, %reg
        return None if field in targets else TARGET
    return BYTES
