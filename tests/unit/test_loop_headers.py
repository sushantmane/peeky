"""Whether a loop header's breakpoint stops only before its loop (#161): the control
flow read from LLDB's disassembly or Java bytecode, never from the source's shape."""

from types import SimpleNamespace

from peeky.loop_headers import (
    disassembly,
    java_instructions,
    java_line_ranges,
    line_ranges,
    loop_header_note,
    natural_loops,
    stops_before_loop,
)
from peeky.worker import DebugWorker

# clang -O0, arm64: `for (int i = 0; i < 3; i++) { total += i; }` (line 4, body 5), then
# `int k = 0; while (k < 3) { k++; }` (lines 7-10), then `do { total++; } while (total <
# 10);` (lines 14-16), as LLDB's `disassemble` lists it once the program runs.
ARM64 = """loops`main:
    0x100000460 <+0>:   sub    sp, sp, #0x40
    0x100000464 <+4>:   stp    x29, x30, [sp, #0x30]
    0x100000468 <+8>:   add    x29, sp, #0x30
    0x10000046c <+12>:  stur   wzr, [x29, #-0x4]
    0x100000470 <+16>:  stur   wzr, [x29, #-0x8]
    0x100000474 <+20>:  stur   wzr, [x29, #-0xc]
    0x100000478 <+24>:  b      0x10000047c               ; <+28> at loops.c:4:21
    0x10000047c <+28>:  ldur   w8, [x29, #-0xc]
    0x100000480 <+32>:  subs   w8, w8, #0x3
    0x100000484 <+36>:  b.ge   0x1000004b0               ; <+80> at loops.c:7:9
    0x100000488 <+40>:  b      0x10000048c               ; <+44> at loops.c:5:18
    0x10000048c <+44>:  ldur   w9, [x29, #-0xc]
    0x100000490 <+48>:  ldur   w8, [x29, #-0x8]
    0x100000494 <+52>:  add    w8, w8, w9
    0x100000498 <+56>:  stur   w8, [x29, #-0x8]
    0x10000049c <+60>:  b      0x1000004a0               ; <+64> at loops.c:4:29
    0x1000004a0 <+64>:  ldur   w8, [x29, #-0xc]
    0x1000004a4 <+68>:  add    w8, w8, #0x1
    0x1000004a8 <+72>:  stur   w8, [x29, #-0xc]
    0x1000004ac <+76>:  b      0x10000047c               ; <+28> at loops.c:4:21
    0x1000004b0 <+80>:  stur   wzr, [x29, #-0x10]
    0x1000004b4 <+84>:  b      0x1000004b8               ; <+88> at loops.c:8:12
    0x1000004b8 <+88>:  ldur   w8, [x29, #-0x10]
    0x1000004bc <+92>:  subs   w8, w8, #0x3
    0x1000004c0 <+96>:  b.ge   0x1000004d8               ; <+120> at loops.c:15:14
    0x1000004c4 <+100>: b      0x1000004c8               ; <+104> at loops.c:9:10
    0x1000004c8 <+104>: ldur   w8, [x29, #-0x10]
    0x1000004cc <+108>: add    w8, w8, #0x1
    0x1000004d0 <+112>: stur   w8, [x29, #-0x10]
->  0x1000004d4 <+116>: b      0x1000004b8               ; <+88> at loops.c:8:12
    0x1000004d8 <+120>: ldur   w8, [x29, #-0x8]
    0x1000004dc <+124>: add    w8, w8, #0x1
    0x1000004e0 <+128>: stur   w8, [x29, #-0x8]
    0x1000004e4 <+132>: b      0x1000004e8               ; <+136> at loops.c:16:14
    0x1000004e8 <+136>: ldur   w8, [x29, #-0x8]
    0x1000004ec <+140>: subs   w8, w8, #0xa
    0x1000004f0 <+144>: b.lt   0x1000004d8               ; <+120> at loops.c:15:14
    0x1000004f4 <+148>: bl     0x10000056c               ; symbol stub for: printf
    0x1000004f8 <+152>: mov    w0, #0x0                  ; =0
    0x1000004fc <+156>: ldp    x29, x30, [sp, #0x30]
    0x100000500 <+160>: add    sp, sp, #0x40
    0x100000504 <+164>: ret
"""
# `source info` for each line: its code's ranges.
SOURCE_INFO = """Lines found for file loops.c in compilation unit loops.c in `loops
[0x0000000100000474-0x0000000100000478): /w/loops.c:4:14
[0x0000000100000478-0x000000010000047c): /w/loops.c:4:10
[0x000000010000047c-0x0000000100000480): /w/loops.c:4:21
[0x0000000100000480-0x000000010000048c): /w/loops.c:4:5
[0x00000001000004a0-0x00000001000004ac): /w/loops.c:4:29
[0x00000001000004ac-0x00000001000004b0): /w/loops.c:4:5
[0x000000010000048c-0x00000001000004a0): /w/loops.c:5:18
[0x00000001000004b0-0x00000001000004b4): /w/loops.c:7:9
[0x00000001000004b4-0x00000001000004b8): /w/loops.c:8:5
[0x00000001000004b8-0x00000001000004c8): /w/loops.c:8:12
[0x00000001000004d4-0x00000001000004d8): /w/loops.c:8:5
[0x00000001000004c8-0x00000001000004d4): /w/loops.c:9:10
[0x00000001000004d8-0x00000001000004e4): /w/loops.c:15:14
[0x00000001000004e4-0x00000001000004f4): /w/loops.c:16:5
"""


def judged(listing, line, locations):
    return stops_before_loop(disassembly(listing), line_ranges(SOURCE_INFO, line), locations)


def test_a_header_whose_location_runs_before_its_loop_stops_once():
    # `for`: its location (`i = 0`) is outside the loop the condition heads.
    assert judged(ARM64, 4, [0x100000474]) == 1
    # `while`: the jump into the loop, outside it.
    assert judged(ARM64, 8, [0x1000004B4]) == 1
    # The body, the `do`'s `while` (in its loop) and a plain line stop at each pass.
    assert judged(ARM64, 5, [0x10000048C]) is None
    assert judged(ARM64, 16, [0x1000004E4]) is None
    assert judged(ARM64, 7, [0x1000004B0]) is None
    # A location the listing does not hold, or a line with no code: no answer.
    assert judged(ARM64, 4, [0x100000999]) is None
    assert judged(ARM64, 99, [0x100000474]) is None
    # Before the program runs, LLDB lists `module[address]`: the same answer.
    unslid = ARM64.replace("    0x1", "    loops[0x1").replace(" <+", "] <+")
    assert judged(unslid, 4, [0x100000474]) == 1


def test_flow_that_is_not_plain_natural_loops_gives_no_answer():
    # A jump table (`br x8`): where it goes is unknown.
    table = ARM64.replace("b      0x1000004b8 ", "br     x8          ")
    assert natural_loops(disassembly(table)) is None
    assert judged(table, 4, [0x100000474]) is None
    # A branch into the middle of a loop from outside it (irreducible flow).
    into = ARM64.replace(
        "0x100000488 <+40>:  b      0x10000048c", "0x100000488 <+40>:  b.eq   0x1000004c8"
    )
    assert natural_loops(disassembly(into)) is None
    assert judged(into, 8, [0x1000004B4]) is None


def test_x86_64_branches_calls_and_prefixed_jump_tables():
    listing = """a.out`main:
    0x401120 <+0>:  pushq  %rbp
    0x401121 <+1>:  movl   $0x0, -0x8(%rbp)
    0x401128 <+8>:  jmp    0x401138                  ; <+24> at m.c:4:3
    0x40112a <+10>: callq  0x401030                  ; puts
    0x40112f <+15>: addl   $0x1, -0x8(%rbp)
    0x401138 <+24>: cmpl   $0x2, -0x8(%rbp)
    0x40113c <+28>: jle    0x40112a                  ; <+10> at m.c:5:5
    0x40113e <+30>: retq
"""
    found = disassembly(listing)
    assert [(kind, targets) for _, kind, targets in found][2:7] == [
        ("jump", (0x401138,)),
        ("fall", ()),
        ("fall", ()),
        ("fall", ()),
        ("branch", (0x40112A,)),
    ]
    # GCC's layout: `i = 0` and the jump to the condition at the bottom, both before it.
    ranges = [(0x401121, 0x40112A), (0x401138, 0x40113E)]
    assert stops_before_loop(found, ranges, [0x401121]) == 1
    # A CET build's jump table: `notrack jmpq *%rax` goes where the code cannot tell.
    table = listing.replace("callq  0x401030 ", "notrack jmpq *%rax ")
    assert natural_loops(disassembly(table)) is None


# javac -g's `main` of `for (int i = 0; i < 3; i++) {...}` (line 6), `while (k < 3)`
# (10), `do {...} while (total < 10);` (15), `for (String a : args)` (16) and
# `for (; j < 2; j++)` (20), with its LineNumberTable.
JAVA = bytes.fromhex(
    "033c033d1c06a2000d1b1c603c840201a7fff4033d1c06a20009840201a7fff88401011b100aa1fffa2a4e"
    "2dbe360403360515051504a200172d1505323a061b1906b60007603c840501a7ffe8033e1d05a2000d1b1d"
    "603c840301a7fff4b2000d1b1cba00130000b60017b1"
)
JAVA_LINES = {0: 5, 2: 6, 9: 7, 13: 6, 19: 9, 21: 10, 26: 11, 32: 14, 35: 15, 41: 16, 63: 17}
JAVA_LINES.update({71: 16, 77: 19, 79: 20, 84: 21, 88: 20, 94: 23, 107: 24})


def test_java_bytecode_headers_bound_before_their_loop():
    instructions = java_instructions(JAVA)

    def stops(line):
        pcs = [pc for pc, number in JAVA_LINES.items() if number == line]
        ranges = java_line_ranges(JAVA_LINES, len(JAVA), line)
        return stops_before_loop(instructions, ranges, [min(pcs)])

    # java-debug binds the line's first bytecode: `i = 0` and the array's copy run once.
    assert (stops(6), stops(16)) == (1, 1)
    # The condition the loop returns to: every pass.
    assert (stops(10), stops(15), stops(20), stops(7)) == (None, None, None, None)
    # A switch's targets, `wide`, and code that does not decode.
    switch = bytes([0x1B, 0xAA, 0, 0, 0, 0, 0, 0x17, 0, 0, 0, 0, 0, 0, 0, 1])
    switch += bytes([0, 0, 0, 0x18, 0, 0, 0, 0x19, 0xB1, 0xB1, 0xB1])
    assert java_instructions(switch)[1] == (1, "jump", (24, 25, 26))
    # A branch into the middle of an instruction is no bytecode a JVM runs.
    assert java_instructions(switch[:7] + bytes([0x13]) + switch[8:]) is None
    assert java_instructions(bytes([0xC4, 0x84, 0, 1, 0, 1, 0xB1]))[1] == (6, "stop", ())
    assert java_instructions(bytes([0xCB])) is None
    assert java_instructions(bytes([0xA7, 0x7F, 0x00])) is None


def test_the_worker_says_it_and_drops_its_per_pass_warning(tmp_path):
    listing = """  1.1: where = loops`loops::main::h0123456789abcdef + 20 at main.rs:4:14, address = 0x0000000100000474, resolved
  1.2: where = loops`loops::main::h0123456789abcdef + 24 at main.rs:4:5, address = 0x0000000100000478, resolved
"""
    from peeky.native import breakpoint_locations

    asked = []

    def stops(dap, source, line, addresses):
        asked.append((line, addresses))
        return 2

    runtime = SimpleNamespace(
        breakpoint_locations=lambda dap, ids: breakpoint_locations(listing, set(ids)),
        loop_header_stops=stops,
        source_path=lambda program: program,
    )
    worker = DebugWorker(tmp_path)
    worker.dap, worker.program = object(), tmp_path / "main.rs"
    worker.note_places(
        runtime, [{"line": 4}], {"breakpoints": [{"id": 1, "line": 4, "verified": True}]}
    )
    assert asked == [(4, (0x100000474, 0x100000478))]
    assert worker.loop_warnings() == [
        loop_header_note(4, 2) + " (each hit's stop, 1 to 2, says which)."
    ]
    assert worker.place_warnings() == []
    # A runtime that cannot say: #157's warning stands.
    del runtime.loop_header_stops
    worker.note_places(
        runtime, [{"line": 4}], {"breakpoints": [{"id": 1, "line": 4, "verified": True}]}
    )
    assert worker.loop_warnings() == []
    assert worker.place_warnings()[0].startswith("Line 4 stops up to 2 times per pass")
