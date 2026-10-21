"""Whether a line breakpoint on a loop header stops only before its loop (#161).

A C, C++ or Java `for`/`while` header with one breakpoint location stops at its first
code, which runs once before the loop (`i = 0`, a jump to the condition), never at the
condition the loop returns to; Rust's `for` has two such locations. An agent that puts
`--at` on the header expecting one stop per pass gets one stop in all.

Decided from the code alone, never from the source's shape: the function's control
flow (LLDB's disassembly, or Java bytecode) gives its natural loops (dominator-based),
and its line table which line each instruction is. A loop is the line's where its
header holds code of the line. The line stops only before it where:

- every breakpoint location of the line is outside each such loop, and every path from
  it passes through that loop's header before it can come back to the location or
  return (so it runs once each time the loop starts, never on its own: not a condition
  the loop sits under, review of #166);
- the loop holds code of other lines (its body), and the line's code in it is the loop's
  own: its condition (the blocks the header starts, of the line's code alone), its
  increment (the line's code ending a block that jumps back to the header), or a
  binding starting the body (a for-each's element), no further right on the line than
  that condition or increment.

Anything unclear (an indirect jump, flow that is not a natural loop, a location or line
with no code found) gives no answer.
"""

import re

MAX_INSTRUCTIONS = 50_000

# One instruction of LLDB's `disassemble`: `    0x100003f78 <+24>:  b      0x100003f7c ;
# <+28> at m.c:4:21`, `->  0x…` at the pc, `m[0x3f78] <+24>: …` before the program runs.
_INSTRUCTION = re.compile(
    r"^\s*(?:->\s*)?(?:[^\s\[]*\[)?(0x[0-9a-fA-F]{1,16})\]?\s*<\+[0-9]+>:\s+(\S+)\s*([^;]*)"
)
# A line's code in `source info`: `[0x100003f74-0x100003f78): /w/m.c:4:14`.
_RANGE = re.compile(
    r"^\[(0x[0-9a-fA-F]{1,16})-(0x[0-9a-fA-F]{1,16})\): .*?:([0-9]{1,9})(?::([0-9]{1,9}))?$"
)
_ADDRESS = re.compile(r"(?<![\w#])0x[0-9a-fA-F]{1,16}\b")

# How each kind of instruction leaves (arm64 and x86-64): "return" leaves the function,
# "stop" traps (a panic's `brk`, never a return), "jump" goes to its target only,
# "branch" to its target or the next, "indirect" goes where the code cannot tell (a jump
# table).
_RETURNS = frozenset(("ret", "retq", "retl", "retaa", "retab"))
_STOPS = frozenset(("brk", "udf", "ud2", "hlt", "int3"))
_JUMPS = frozenset(("b", "jmp", "jmpq"))
_CONDITIONAL = frozenset(("cbz", "cbnz", "tbz", "tbnz"))
_CALLS = re.compile(r"^(?:bl|blr|blra\w*|callq?|calll)$")
_PREFIXES = frozenset(
    ("notrack", "bnd", "rep", "repe", "repz", "repne", "repnz", "lock", "ds", "cs")
)


def _kind(mnemonic, operands):
    if mnemonic in _RETURNS:
        return "return"
    if mnemonic in _STOPS:
        return "stop"
    if _CALLS.match(mnemonic):
        return "fall"
    if mnemonic in ("br", "jmpl") or re.fullmatch(r"bra[ab]z?", mnemonic):
        return "indirect"
    if mnemonic in _JUMPS:
        return "indirect" if "*" in operands or "%" in operands else "jump"
    if mnemonic in _CONDITIONAL or re.fullmatch(r"b\.\w+|bc\.\w+", mnemonic):
        return "branch"
    if re.fullmatch(r"j(?!mp)[a-z]{1,5}", mnemonic):
        return "indirect" if "*" in operands else "branch"
    return "fall"


def disassembly(text):
    """[(address, kind, targets)] of LLDB's `disassemble` text, in address order, or
    None where it lists no instruction or more than MAX_INSTRUCTIONS."""
    found = []
    for raw in text.splitlines():
        match = _INSTRUCTION.match(raw)
        if match is None:
            continue
        mnemonic, operands = match[2].lower(), match[3].strip()
        while mnemonic in _PREFIXES and operands:
            # x86 prefixes (`notrack jmpq *%rax`, a CET build's jump table).
            mnemonic, _, operands = operands.partition(" ")
            mnemonic, operands = mnemonic.lower(), operands.strip()
        kind = _kind(mnemonic, operands)
        targets = ()
        if kind in ("jump", "branch"):
            # The target is the last address among the operands (`tbz w8, #0x0, 0x…`).
            addresses = _ADDRESS.findall(operands)
            if not addresses:
                kind = "indirect"
            else:
                targets = (int(addresses[-1], 16),)
        found.append((int(match[1], 16), kind, targets))
        if len(found) > MAX_INSTRUCTIONS:
            return None
    found.sort()
    return found or None


def line_table(text):
    """[(start, end, line, column)] of LLDB's `source info` text, by start: which line
    (and column, 0 where none) each range of code is."""
    rows = []
    for raw in text.splitlines():
        match = _RANGE.match(raw.strip())
        if match:
            rows.append((int(match[1], 16), int(match[2], 16), int(match[3]), int(match[4] or 0)))
    return sorted(rows)


def _blocks(instructions, handlers=()):
    """({block start: [successor block starts]}, [block starts], {address: block start}).
    ``handlers``: (start, end, handler) of exception handlers (Java), each reached from
    the code from start to end."""
    addresses = [address for address, _, _ in instructions]
    inside = set(addresses)
    leaders = {addresses[0]}
    for index, (address, kind, targets) in enumerate(instructions):
        leaders.update(target for target in targets if target in inside)
        if kind != "fall" and index + 1 < len(instructions):
            leaders.add(addresses[index + 1])
    for start, end, handler in handlers:
        leaders.update(point for point in (start, end, handler) if point in inside)
    starts = sorted(leaders)
    owner, successors = {}, {start: [] for start in starts}
    current = None
    for index, (address, kind, targets) in enumerate(instructions):
        if address in leaders:
            current = address
        owner[address] = current
        following = addresses[index + 1] if index + 1 < len(addresses) else None
        last = following is None or following in leaders or kind != "fall"
        if not last:
            continue
        nexts = []
        if kind in ("fall", "branch") and following is not None:
            nexts.append(following)
        if kind in ("jump", "branch"):
            nexts.extend(target for target in targets if target in inside)
        successors[current] = list(dict.fromkeys(nexts))
    for start, end, handler in handlers:
        if handler in inside:
            for block in starts:
                if start <= block < end:
                    successors[block] = list(dict.fromkeys([*successors[block], handler]))
    return successors, starts, owner


def natural_loops(instructions, handlers=()):
    """{loop header block: blocks of the loop} of a function's instructions (the first
    is its entry), with ({address: block}, successors), or None where the flow cannot
    be read as natural loops: an indirect jump, or a cycle whose entry does not
    dominate it (irreducible flow)."""
    if not instructions or any(kind == "indirect" for _, kind, _ in instructions):
        return None
    successors, starts, owner = _blocks(instructions, handlers)
    entry = starts[0]
    # Depth-first: postorder, and the edges that go back to a block still on the path.
    order, on_path, seen, retreating = [], set(), {entry}, []
    stack = [(entry, iter(successors[entry]))]
    on_path.add(entry)
    while stack:
        block, pending = stack[-1]
        for following in pending:
            if following in on_path:
                retreating.append((block, following))
            elif following not in seen:
                seen.add(following)
                on_path.add(following)
                stack.append((following, iter(successors[following])))
                break
        else:
            stack.pop()
            on_path.discard(block)
            order.append(block)
    rpo = order[::-1]
    number = {block: index for index, block in enumerate(rpo)}
    predecessors = {block: [] for block in rpo}
    for block in rpo:
        for following in successors[block]:
            if following in predecessors:
                predecessors[following].append(block)
    # Cooper, Harvey and Kennedy's dominators.
    idom = {entry: entry}
    changed = True
    while changed:
        changed = False
        for block in rpo[1:]:
            done = [p for p in predecessors[block] if p in idom]
            if not done:
                continue
            new = done[0]
            for other in done[1:]:
                a, b = new, other
                while a != b:
                    while number[a] > number[b]:
                        a = idom[a]
                    while number[b] > number[a]:
                        b = idom[b]
                new = a
            if idom.get(block) != new:
                idom[block] = new
                changed = True

    def dominates(a, b):
        while True:
            if a == b:
                return True
            if b == entry:
                return False
            b = idom[b]

    loops = {}
    for source, header in retreating:
        if not dominates(header, source):
            return None
        body = loops.setdefault(header, {header})
        pending = [source]
        while pending:
            block = pending.pop()
            if block in body:
                continue
            body.add(block)
            pending.extend(predecessors[block])
    return loops, owner, successors


def _located(table):
    """address -> (line, column) of ``table`` (see ``line_table``), or None."""
    import bisect

    starts = [row[0] for row in table]

    def at(address):
        index = bisect.bisect_right(starts, address) - 1
        if index < 0 or not table[index][0] <= address < table[index][1] or not table[index][2]:
            # No line (a call's return point, line 0): it belongs to none.
            return None
        return table[index][2], table[index][3]

    return at


def stops_before_loop(instructions, table, line, locations, handlers=()):
    """How many of ``locations`` (a line's breakpoint addresses in this function) the
    line stops at each time its loop starts, before the first pass, where the line is
    that loop's header alone (see the module); else None. ``table``: which line each
    range of code is (``line_table``)."""
    analysed = natural_loops(instructions, handlers)
    if analysed is None or not locations or not table:
        return None
    loops, owner, successors = analysed
    at = _located(table)
    code = {}
    for address, kind, _ in instructions:
        code.setdefault(owner[address], []).append((address, kind, at(address)))

    def ours(where):
        return where is not None and where[0] == line

    headers = [header for header in loops if any(ours(w) for _, _, w in code[header])]
    if not headers:
        return None
    if not _own_code(headers, loops, code, successors, ours):
        return None
    inside = {block for header in headers for block in loops[header]}
    for location in locations:
        block = owner.get(location)
        if block is None or block in inside or block not in code:
            return None
        # Every path from it reaches a header before it comes back to it or returns.
        seen, pending = set(), list(successors.get(block, ()))
        if code[block][-1][1] == "return":
            return None
        while pending:
            current = pending.pop()
            if current in headers or current in seen:
                continue
            if current == block or code.get(current, [(0, "", None)])[-1][1] == "return":
                return None
            seen.add(current)
            pending.extend(successors.get(current, ()))
    return len(locations)


def _own_code(headers, loops, code, successors, ours):
    """Whether the line's code inside each of its loops is only that loop's own (see the
    module): never its body, and never all of the loop."""
    for header in headers:
        body = loops[header]
        if not any(
            where is not None and not ours(where) for block in body for _, _, where in code[block]
        ):
            # The whole loop is the line's (`for (...) total += i;`, a macro's loop).
            return False
        # The condition: the blocks the header starts, of the line's code alone and no
        # further right on it than the header's (a body statement written on the line,
        # `{ if (a[i]) continue;`, is not).
        reach = max((w[1] for _, _, w in code[header] if ours(w)), default=0)
        condition, pending = set(), [header]
        while pending:
            block = pending.pop()
            if block in condition or block not in body:
                continue
            if block != header and not all(
                where is None or (ours(where) and (not reach or not where[1] or where[1] <= reach))
                for _, _, where in code[block]
            ):
                continue
            condition.add(block)
            pending.extend(successors.get(block, ()))
        allowed = {address for block in condition for address, _, _ in code[block]}
        control = [where[1] for block in condition for _, _, where in code[block] if ours(where)]
        for block in body - condition:
            rows = code[block]
            if header in successors.get(block, ()):
                # The increment: the line's code ending a block that jumps back.
                for address, _, where in reversed(rows):
                    if not ours(where):
                        break
                    allowed.add(address)
                    control.append(where[1])
        widest = max(control, default=0)
        for block in body - condition:
            if not any(previous in condition for previous in body if block in successors[previous]):
                continue
            # A binding starting the body (a for-each's element), within the header.
            for address, _, where in code[block]:
                if not ours(where) or address in allowed:
                    break
                if where[1] and widest and where[1] > widest:
                    break
                allowed.add(address)
        for block in body:
            for address, _, where in code[block]:
                if ours(where) and address not in allowed:
                    return False
    return True


# Java bytecode: each opcode's length where it is fixed, by opcode.
_JAVA_LENGTHS = {
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


def java_instructions(code):
    """[(pc, kind, targets)] of a method's bytecode (as `disassembly` gives), or None
    where it does not decode (an unknown opcode, a target outside the code)."""
    code = bytes(code)
    size = len(code)

    def signed(at, width):
        if at + width > size:
            raise IndexError
        return int.from_bytes(code[at : at + width], "big", signed=True)

    found, pc = [], 0
    try:
        while pc < size:
            op = code[pc]
            targets = ()
            if op == 0xC4:
                length, kind = (6 if code[pc + 1] == 0x84 else 4), "fall"
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
            elif op not in _JAVA_LENGTHS:
                return None
            else:
                length = _JAVA_LENGTHS[op]
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
                else:
                    kind = "fall"
            if any(not 0 <= target < size for target in targets) or pc + length > size:
                return None
            found.append((pc, kind, targets))
            pc += length
    except IndexError:
        return None
    starts = {pc for pc, _, _ in found}
    if any(target not in starts for _, _, targets in found for target in targets):
        # A branch into the middle of an instruction: not bytecode a JVM runs.
        return None
    return found or None


def java_line_table(lines, size):
    """``line_table``'s rows for a method's LineNumberTable ({pc: line}): each entry's
    line holds from its pc to the next entry's (no columns in bytecode)."""
    starts = sorted(lines)
    return [
        (pc, starts[index + 1] if index + 1 < len(starts) else size, lines[pc], 0)
        for index, pc in enumerate(starts)
    ]


def loop_header_note(line, count):
    """The note for a loop header that stops ``count`` times each time its loop starts,
    before its first pass (see ``stops_before_loop``)."""
    times = {1: "once", 2: "twice"}.get(count, f"{count} times")
    return (
        f"Line {line} is a loop header: it stops {times} each time its loop starts, before "
        "the first pass, never at the passes; put --at (or --set-at) on the first line of "
        "the loop's body for one stop per pass"
    )
