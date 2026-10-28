"""Whether a loop header's breakpoint stops only before its loop (#161): the control
flow read from LLDB's disassembly or Java bytecode, never from the source's shape."""

from types import SimpleNamespace

from peeky.loop_headers import (
    disassembly,
    java_instructions,
    java_line_table,
    line_table,
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


def judged(listing, line, locations, info=SOURCE_INFO):
    return stops_before_loop(disassembly(listing), line_table(info), line, locations)


def test_a_header_whose_location_runs_before_its_loop_stops_once():
    # `for`: its location (`i = 0`) is outside the loop the condition heads.
    assert judged(ARM64, 4, [0x100000474]) == (1, True)
    # `while`: the jump into the loop, outside it.
    assert judged(ARM64, 8, [0x1000004B4]) == (1, True)
    # The body, the `do`'s `while` (in its loop) and a plain line stop at each pass.
    assert judged(ARM64, 5, [0x10000048C]) is None
    assert judged(ARM64, 16, [0x1000004E4]) is None
    assert judged(ARM64, 7, [0x1000004B0]) is None
    # A location the listing does not hold, or a line with no code: no answer.
    assert judged(ARM64, 4, [0x100000999]) is None
    assert judged(ARM64, 99, [0x100000474]) is None
    # Before the program runs, LLDB lists `module[address]`: the same answer.
    unslid = ARM64.replace("    0x1", "    loops[0x1").replace(" <+", "] <+")
    assert judged(unslid, 4, [0x100000474]) == (1, True)


# Review of #166: clang -O0's `main` of this, as LLDB lists it before the program runs,
# and its `source info -f u.c -l 1` (the whole file's line table):
#  3    for (int r = 0; r < 3; r++) {
#  4        if (r == 2) for (int i = 0; i < 2; i++) {
#  5            total += i;
#  6        }
#  7        for (int j = 0; j < 4; j++) a[j] = r;
#  8    }
#  9    goto inside;
# 10    while (g < 3) {
# 11        total++;
# 12inside:
# 13        g++;
# 14    }
# 15    for (int k = 0; k < 2; k++) {
NESTED = """u`main:
u[0x100000410] <+0>:   sub    sp, sp, #0x50
u[0x100000414] <+4>:   stp    x29, x30, [sp, #0x40]
u[0x100000418] <+8>:   add    x29, sp, #0x40
u[0x10000041c] <+12>:  adrp   x8, 4
u[0x100000420] <+16>:  ldr    x8, [x8]
u[0x100000424] <+20>:  ldr    x8, [x8]
u[0x100000428] <+24>:  stur   x8, [x29, #-0x8]
u[0x10000042c] <+28>:  stur   wzr, [x29, #-0x1c]
u[0x100000430] <+32>:  str    wzr, [sp, #0x20]
u[0x100000434] <+36>:  str    wzr, [sp, #0x1c]
u[0x100000438] <+40>:  str    wzr, [sp, #0x18]
u[0x10000043c] <+44>:  b      0x100000440               ; <+48> at u.c:3:21
u[0x100000440] <+48>:  ldr    w8, [sp, #0x18]
u[0x100000444] <+52>:  subs   w8, w8, #0x3
u[0x100000448] <+56>:  b.ge   0x1000004f0               ; <+224> at u.c:9:5
u[0x10000044c] <+60>:  b      0x100000450               ; <+64> at u.c:4:13
u[0x100000450] <+64>:  ldr    w8, [sp, #0x18]
u[0x100000454] <+68>:  subs   w8, w8, #0x2
u[0x100000458] <+72>:  b.ne   0x1000004a0               ; <+144> at u.c:7:18
u[0x10000045c] <+76>:  b      0x100000460               ; <+80> at u.c:4:30
u[0x100000460] <+80>:  str    wzr, [sp, #0x14]
u[0x100000464] <+84>:  b      0x100000468               ; <+88> at u.c:4:37
u[0x100000468] <+88>:  ldr    w8, [sp, #0x14]
u[0x10000046c] <+92>:  subs   w8, w8, #0x2
u[0x100000470] <+96>:  b.ge   0x10000049c               ; <+140> at u.c:6:9
u[0x100000474] <+100>: b      0x100000478               ; <+104> at u.c:5:22
u[0x100000478] <+104>: ldr    w9, [sp, #0x14]
u[0x10000047c] <+108>: ldr    w8, [sp, #0x20]
u[0x100000480] <+112>: add    w8, w8, w9
u[0x100000484] <+116>: str    w8, [sp, #0x20]
u[0x100000488] <+120>: b      0x10000048c               ; <+124> at u.c:4:45
u[0x10000048c] <+124>: ldr    w8, [sp, #0x14]
u[0x100000490] <+128>: add    w8, w8, #0x1
u[0x100000494] <+132>: str    w8, [sp, #0x14]
u[0x100000498] <+136>: b      0x100000468               ; <+88> at u.c:4:37
u[0x10000049c] <+140>: b      0x1000004a0               ; <+144> at u.c:7:18
u[0x1000004a0] <+144>: str    wzr, [sp, #0x10]
u[0x1000004a4] <+148>: b      0x1000004a8               ; <+152> at u.c:7:25
u[0x1000004a8] <+152>: ldr    w8, [sp, #0x10]
u[0x1000004ac] <+156>: subs   w8, w8, #0x4
u[0x1000004b0] <+160>: b.ge   0x1000004dc               ; <+204> at u.c:8:5
u[0x1000004b4] <+164>: b      0x1000004b8               ; <+168> at u.c:7:44
u[0x1000004b8] <+168>: ldr    w8, [sp, #0x18]
u[0x1000004bc] <+172>: ldrsw  x10, [sp, #0x10]
u[0x1000004c0] <+176>: sub    x9, x29, #0x18
u[0x1000004c4] <+180>: str    w8, [x9, x10, lsl #2]
u[0x1000004c8] <+184>: b      0x1000004cc               ; <+188> at u.c:7:33
u[0x1000004cc] <+188>: ldr    w8, [sp, #0x10]
u[0x1000004d0] <+192>: add    w8, w8, #0x1
u[0x1000004d4] <+196>: str    w8, [sp, #0x10]
u[0x1000004d8] <+200>: b      0x1000004a8               ; <+152> at u.c:7:25
u[0x1000004dc] <+204>: b      0x1000004e0               ; <+208> at u.c:3:29
u[0x1000004e0] <+208>: ldr    w8, [sp, #0x18]
u[0x1000004e4] <+212>: add    w8, w8, #0x1
u[0x1000004e8] <+216>: str    w8, [sp, #0x18]
u[0x1000004ec] <+220>: b      0x100000440               ; <+48> at u.c:3:21
u[0x1000004f0] <+224>: b      0x100000514               ; <+260> at u.c:13:10
u[0x1000004f4] <+228>: ldr    w8, [sp, #0x1c]
u[0x1000004f8] <+232>: subs   w8, w8, #0x3
u[0x1000004fc] <+236>: b.ge   0x100000524               ; <+276> at u.c:15:14
u[0x100000500] <+240>: b      0x100000504               ; <+244> at u.c:11:14
u[0x100000504] <+244>: ldr    w8, [sp, #0x20]
u[0x100000508] <+248>: add    w8, w8, #0x1
u[0x10000050c] <+252>: str    w8, [sp, #0x20]
u[0x100000510] <+256>: b      0x100000514               ; <+260> at u.c:13:10
u[0x100000514] <+260>: ldr    w8, [sp, #0x1c]
u[0x100000518] <+264>: add    w8, w8, #0x1
u[0x10000051c] <+268>: str    w8, [sp, #0x1c]
u[0x100000520] <+272>: b      0x1000004f4               ; <+228> at u.c:10:12
u[0x100000524] <+276>: str    wzr, [sp, #0xc]
u[0x100000528] <+280>: b      0x10000052c               ; <+284> at u.c:15:21
u[0x10000052c] <+284>: ldr    w8, [sp, #0xc]
u[0x100000530] <+288>: subs   w8, w8, #0x2
u[0x100000534] <+292>: b.ge   0x100000560               ; <+336> at u.c:18:12
u[0x100000538] <+296>: b      0x10000053c               ; <+300> at u.c:16:18
u[0x10000053c] <+300>: ldr    w9, [sp, #0xc]
u[0x100000540] <+304>: ldr    w8, [sp, #0x20]
u[0x100000544] <+308>: add    w8, w8, w9
u[0x100000548] <+312>: str    w8, [sp, #0x20]
u[0x10000054c] <+316>: b      0x100000550               ; <+320> at u.c:15:29
u[0x100000550] <+320>: ldr    w8, [sp, #0xc]
u[0x100000554] <+324>: add    w8, w8, #0x1
u[0x100000558] <+328>: str    w8, [sp, #0xc]
u[0x10000055c] <+332>: b      0x10000052c               ; <+284> at u.c:15:21
u[0x100000560] <+336>: ldr    w8, [sp, #0x20]
u[0x100000564] <+340>: ldur   w9, [x29, #-0xc]
u[0x100000568] <+344>: add    w8, w8, w9
u[0x10000056c] <+348>: str    w8, [sp, #0x8]
u[0x100000570] <+352>: ldur   x9, [x29, #-0x8]
u[0x100000574] <+356>: adrp   x8, 4
u[0x100000578] <+360>: ldr    x8, [x8]
u[0x10000057c] <+364>: ldr    x8, [x8]
u[0x100000580] <+368>: subs   x8, x8, x9
u[0x100000584] <+372>: b.eq   0x100000590               ; <+384> at u.c
u[0x100000588] <+376>: b      0x10000058c               ; <+380> at u.c:18:5
u[0x10000058c] <+380>: bl     0x1000005a0               ; symbol stub for: __stack_chk_fail
u[0x100000590] <+384>: ldr    w0, [sp, #0x8]
u[0x100000594] <+388>: ldp    x29, x30, [sp, #0x40]
u[0x100000598] <+392>: add    sp, sp, #0x50
u[0x10000059c] <+396>: ret    
"""
NESTED_INFO = """[0x0000000100000410-0x0000000100000430): u.c:1
[0x0000000100000430-0x0000000100000434): u.c:2:9
[0x0000000100000434-0x0000000100000438): u.c:2:26
[0x0000000100000438-0x000000010000043c): u.c:3:14
[0x000000010000043c-0x0000000100000440): u.c:3:10
[0x0000000100000440-0x0000000100000444): u.c:3:21
[0x0000000100000444-0x0000000100000450): u.c:3:5
[0x00000001000004e0-0x00000001000004ec): u.c:3:29
[0x00000001000004ec-0x00000001000004f0): u.c:3:5
[0x0000000100000450-0x0000000100000454): u.c:4:13
[0x0000000100000454-0x0000000100000460): u.c:4:15
[0x0000000100000460-0x0000000100000464): u.c:4:30
[0x0000000100000464-0x0000000100000468): u.c:4:26
[0x0000000100000468-0x000000010000046c): u.c:4:37
[0x000000010000046c-0x0000000100000478): u.c:4:21
[0x000000010000048c-0x0000000100000498): u.c:4:45
[0x0000000100000498-0x000000010000049c): u.c:4:21
[0x0000000100000478-0x000000010000047c): u.c:5:22
[0x000000010000047c-0x0000000100000488): u.c:5:19
[0x0000000100000488-0x000000010000048c): u.c:6:9
[0x000000010000049c-0x00000001000004a0): u.c:6:9
[0x00000001000004a0-0x00000001000004a4): u.c:7:18
[0x00000001000004a4-0x00000001000004a8): u.c:7:14
[0x00000001000004a8-0x00000001000004ac): u.c:7:25
[0x00000001000004ac-0x00000001000004b8): u.c:7:9
[0x00000001000004b8-0x00000001000004bc): u.c:7:44
[0x00000001000004bc-0x00000001000004c0): u.c:7:39
[0x00000001000004c0-0x00000001000004c4): u.c:7:37
[0x00000001000004c4-0x00000001000004c8): u.c:7:42
[0x00000001000004c8-0x00000001000004cc): u.c:7:37
[0x00000001000004cc-0x00000001000004d8): u.c:7:33
[0x00000001000004d8-0x00000001000004dc): u.c:7:9
[0x00000001000004dc-0x00000001000004e0): u.c:8:5
[0x00000001000004f0-0x00000001000004f4): u.c:9:5
[0x00000001000004f4-0x00000001000004f8): u.c:10:12
[0x00000001000004f8-0x0000000100000504): u.c:10:5
[0x0000000100000520-0x0000000100000524): u.c:10:5
[0x0000000100000504-0x0000000100000510): u.c:11:14
[0x0000000100000510-0x0000000100000514): u.c:11:9
[0x0000000100000514-0x0000000100000520): u.c:13:10
[0x0000000100000524-0x0000000100000528): u.c:15:14
[0x0000000100000528-0x000000010000052c): u.c:15:10
[0x000000010000052c-0x0000000100000530): u.c:15:21
[0x0000000100000530-0x000000010000053c): u.c:15:5
[0x0000000100000550-0x000000010000055c): u.c:15:29
[0x000000010000055c-0x0000000100000560): u.c:15:5
[0x000000010000053c-0x0000000100000540): u.c:16:18
[0x0000000100000540-0x000000010000054c): u.c:16:15
[0x000000010000054c-0x0000000100000550): u.c:17:5
[0x0000000100000560-0x0000000100000564): u.c:18:12
[0x0000000100000564-0x0000000100000568): u.c:18:20
[0x0000000100000568-0x0000000100000570): u.c:18:18
[0x0000000100000570-0x0000000100000590): u.c:18:5
[0x0000000100000594-0x00000001000005a0): u.c:18:5
"""


def test_lines_that_run_at_each_pass_of_a_loop_around_them_get_no_note():
    def stops(line, *locations):
        return judged(NESTED, line, list(locations), NESTED_INFO)

    # Headers alone on their line, bound before their loop: once each time it starts.
    assert (stops(3, 0x100000438), stops(15, 0x100000524)) == ((1, True), (1, True))
    # `if (r == 2) for (...)`: the condition runs at each pass of the `r` loop, entering
    # the `for` or not, so the line stops at each (the note said "never at the passes").
    assert stops(4, 0x100000450, 0x100000460) is None
    # The inner `for`'s own location alone: every path from it enters its loop.
    assert stops(4, 0x100000460) == (1, True)
    # A line holding a whole loop (`for (...) a[j] = r;`, a macro's): its body too.
    assert stops(7, 0x1000004A0) is None
    # `goto inside;` jumps into a loop whose header holds none of its code (Low 1).
    assert stops(9, 0x1000004F0) is None
    # The `while` the goto enters: its condition is in its loop, at each pass after the first.
    assert stops(10, 0x1000004F4) is None


def test_a_header_line_holding_a_body_statement_gets_no_note():
    # `for (int r = 0; r < 3; r++) {` with `if (r == 1) continue;` written on it too: the
    # statement's code (further right than the condition) is the body's.
    table = [
        (row[0], row[1], 3 if row[2] == 4 and row[3] >= 13 else row[2], row[3] + 40 * (row[2] == 4))
        for row in line_table(NESTED_INFO)
    ]
    found = disassembly(NESTED)
    assert stops_before_loop(found, line_table(NESTED_INFO), 3, [0x100000438]) == (1, True)
    assert stops_before_loop(found, table, 3, [0x100000438]) is None


# Review 2 of #166: clang -O0's `main` of this, with LLDB's texts as before:
#  3    int total = 0, z = argc - 1;
#  4    if (z) { int w = 5; total += w; } for (int i = 0; i < 2; i++) { // CONDLOC
#  5        total += i;
#  6    }
#  7    for (int r = 0; r < 4; r++) {
#  8        if (r % 2) { int w = r; total += w; } for (int i = 0; i < 2; i++) { // CONDLOC2
#  9            total += i;
# 10        }
# 11    }
CONDITIONAL = """condloc`main:
condloc[0x100000460] <+0>:   sub    sp, sp, #0x50
condloc[0x100000464] <+4>:   stp    x29, x30, [sp, #0x40]
condloc[0x100000468] <+8>:   add    x29, sp, #0x40
condloc[0x10000046c] <+12>:  stur   wzr, [x29, #-0x4]
condloc[0x100000470] <+16>:  stur   w0, [x29, #-0x8]
condloc[0x100000474] <+20>:  stur   x1, [x29, #-0x10]
condloc[0x100000478] <+24>:  stur   wzr, [x29, #-0x14]
condloc[0x10000047c] <+28>:  ldur   w8, [x29, #-0x8]
condloc[0x100000480] <+32>:  subs   w8, w8, #0x1
condloc[0x100000484] <+36>:  stur   w8, [x29, #-0x18]
condloc[0x100000488] <+40>:  ldur   w8, [x29, #-0x18]
condloc[0x10000048c] <+44>:  cbz    w8, 0x1000004b0           ; <+80> at condloc.c:4:48
condloc[0x100000490] <+48>:  b      0x100000494               ; <+52> at condloc.c:4:18
condloc[0x100000494] <+52>:  mov    w8, #0x5                  ; =5 
condloc[0x100000498] <+56>:  stur   w8, [x29, #-0x1c]
condloc[0x10000049c] <+60>:  ldur   w9, [x29, #-0x1c]
condloc[0x1000004a0] <+64>:  ldur   w8, [x29, #-0x14]
condloc[0x1000004a4] <+68>:  add    w8, w8, w9
condloc[0x1000004a8] <+72>:  stur   w8, [x29, #-0x14]
condloc[0x1000004ac] <+76>:  b      0x1000004b0               ; <+80> at condloc.c:4:48
condloc[0x1000004b0] <+80>:  str    wzr, [sp, #0x20]
condloc[0x1000004b4] <+84>:  b      0x1000004b8               ; <+88> at condloc.c:4:55
condloc[0x1000004b8] <+88>:  ldr    w8, [sp, #0x20]
condloc[0x1000004bc] <+92>:  subs   w8, w8, #0x2
condloc[0x1000004c0] <+96>:  b.ge   0x1000004ec               ; <+140> at condloc.c:7:14
condloc[0x1000004c4] <+100>: b      0x1000004c8               ; <+104> at condloc.c:5:18
condloc[0x1000004c8] <+104>: ldr    w9, [sp, #0x20]
condloc[0x1000004cc] <+108>: ldur   w8, [x29, #-0x14]
condloc[0x1000004d0] <+112>: add    w8, w8, w9
condloc[0x1000004d4] <+116>: stur   w8, [x29, #-0x14]
condloc[0x1000004d8] <+120>: b      0x1000004dc               ; <+124> at condloc.c:4:63
condloc[0x1000004dc] <+124>: ldr    w8, [sp, #0x20]
condloc[0x1000004e0] <+128>: add    w8, w8, #0x1
condloc[0x1000004e4] <+132>: str    w8, [sp, #0x20]
condloc[0x1000004e8] <+136>: b      0x1000004b8               ; <+88> at condloc.c:4:55
condloc[0x1000004ec] <+140>: str    wzr, [sp, #0x1c]
condloc[0x1000004f0] <+144>: b      0x1000004f4               ; <+148> at condloc.c:7:21
condloc[0x1000004f4] <+148>: ldr    w8, [sp, #0x1c]
condloc[0x1000004f8] <+152>: subs   w8, w8, #0x4
condloc[0x1000004fc] <+156>: b.ge   0x10000058c               ; <+300> at condloc.c:12:26
condloc[0x100000500] <+160>: b      0x100000504               ; <+164> at condloc.c:8:13
condloc[0x100000504] <+164>: ldr    w8, [sp, #0x1c]
condloc[0x100000508] <+168>: mov    w10, #0x2                 ; =2 
condloc[0x10000050c] <+172>: sdiv   w9, w8, w10
condloc[0x100000510] <+176>: mul    w9, w9, w10
condloc[0x100000514] <+180>: subs   w8, w8, w9
condloc[0x100000518] <+184>: cbz    w8, 0x10000053c           ; <+220> at condloc.c:8:56
condloc[0x10000051c] <+188>: b      0x100000520               ; <+192> at condloc.c:8:30
condloc[0x100000520] <+192>: ldr    w8, [sp, #0x1c]
condloc[0x100000524] <+196>: str    w8, [sp, #0x18]
condloc[0x100000528] <+200>: ldr    w9, [sp, #0x18]
condloc[0x10000052c] <+204>: ldur   w8, [x29, #-0x14]
condloc[0x100000530] <+208>: add    w8, w8, w9
condloc[0x100000534] <+212>: stur   w8, [x29, #-0x14]
condloc[0x100000538] <+216>: b      0x10000053c               ; <+220> at condloc.c:8:56
condloc[0x10000053c] <+220>: str    wzr, [sp, #0x14]
condloc[0x100000540] <+224>: b      0x100000544               ; <+228> at condloc.c:8:63
condloc[0x100000544] <+228>: ldr    w8, [sp, #0x14]
condloc[0x100000548] <+232>: subs   w8, w8, #0x2
condloc[0x10000054c] <+236>: b.ge   0x100000578               ; <+280> at condloc.c:11:5
condloc[0x100000550] <+240>: b      0x100000554               ; <+244> at condloc.c:9:22
condloc[0x100000554] <+244>: ldr    w9, [sp, #0x14]
condloc[0x100000558] <+248>: ldur   w8, [x29, #-0x14]
condloc[0x10000055c] <+252>: add    w8, w8, w9
condloc[0x100000560] <+256>: stur   w8, [x29, #-0x14]
condloc[0x100000564] <+260>: b      0x100000568               ; <+264> at condloc.c:8:71
condloc[0x100000568] <+264>: ldr    w8, [sp, #0x14]
condloc[0x10000056c] <+268>: add    w8, w8, #0x1
condloc[0x100000570] <+272>: str    w8, [sp, #0x14]
condloc[0x100000574] <+276>: b      0x100000544               ; <+228> at condloc.c:8:63
condloc[0x100000578] <+280>: b      0x10000057c               ; <+284> at condloc.c:7:29
condloc[0x10000057c] <+284>: ldr    w8, [sp, #0x1c]
condloc[0x100000580] <+288>: add    w8, w8, #0x1
condloc[0x100000584] <+292>: str    w8, [sp, #0x1c]
condloc[0x100000588] <+296>: b      0x1000004f4               ; <+148> at condloc.c:7:21
condloc[0x10000058c] <+300>: ldur   w8, [x29, #-0x14]
condloc[0x100000590] <+304>: mov    x9, sp
condloc[0x100000594] <+308>: str    x8, [x9]
condloc[0x100000598] <+312>: adrp   x0, 0
condloc[0x10000059c] <+316>: add    x0, x0, #0x5c0            ; "total=%d\n"
condloc[0x1000005a0] <+320>: bl     0x1000005b4               ; symbol stub for: printf
condloc[0x1000005a4] <+324>: mov    w0, #0x0                  ; =0 
condloc[0x1000005a8] <+328>: ldp    x29, x30, [sp, #0x40]
condloc[0x1000005ac] <+332>: add    sp, sp, #0x50
condloc[0x1000005b0] <+336>: ret    
"""
CONDITIONAL_INFO = """[0x0000000100000460-0x0000000100000478): condloc.c:2
[0x0000000100000478-0x000000010000047c): condloc.c:3:9
[0x000000010000047c-0x0000000100000480): condloc.c:3:24
[0x0000000100000480-0x0000000100000484): condloc.c:3:29
[0x0000000100000484-0x0000000100000488): condloc.c:3:20
[0x0000000100000488-0x0000000100000494): condloc.c:4:9
[0x0000000100000494-0x000000010000049c): condloc.c:4:18
[0x000000010000049c-0x00000001000004a0): condloc.c:4:34
[0x00000001000004a0-0x00000001000004ac): condloc.c:4:31
[0x00000001000004ac-0x00000001000004b0): condloc.c:4:37
[0x00000001000004b0-0x00000001000004b4): condloc.c:4:48
[0x00000001000004b4-0x00000001000004b8): condloc.c:4:44
[0x00000001000004b8-0x00000001000004bc): condloc.c:4:55
[0x00000001000004bc-0x00000001000004c8): condloc.c:4:39
[0x00000001000004dc-0x00000001000004e8): condloc.c:4:63
[0x00000001000004e8-0x00000001000004ec): condloc.c:4:39
[0x00000001000004c8-0x00000001000004cc): condloc.c:5:18
[0x00000001000004cc-0x00000001000004d8): condloc.c:5:15
[0x00000001000004d8-0x00000001000004dc): condloc.c:6:5
[0x00000001000004ec-0x00000001000004f0): condloc.c:7:14
[0x00000001000004f0-0x00000001000004f4): condloc.c:7:10
[0x00000001000004f4-0x00000001000004f8): condloc.c:7:21
[0x00000001000004f8-0x0000000100000504): condloc.c:7:5
[0x000000010000057c-0x0000000100000588): condloc.c:7:29
[0x0000000100000588-0x000000010000058c): condloc.c:7:5
[0x0000000100000504-0x0000000100000508): condloc.c:8:13
[0x0000000100000508-0x0000000100000520): condloc.c:8:15
[0x0000000100000520-0x0000000100000524): condloc.c:8:30
[0x0000000100000524-0x0000000100000528): condloc.c:8:26
[0x0000000100000528-0x000000010000052c): condloc.c:8:42
[0x000000010000052c-0x0000000100000538): condloc.c:8:39
[0x0000000100000538-0x000000010000053c): condloc.c:8:45
[0x000000010000053c-0x0000000100000540): condloc.c:8:56
[0x0000000100000540-0x0000000100000544): condloc.c:8:52
[0x0000000100000544-0x0000000100000548): condloc.c:8:63
[0x0000000100000548-0x0000000100000554): condloc.c:8:47
[0x0000000100000568-0x0000000100000574): condloc.c:8:71
[0x0000000100000574-0x0000000100000578): condloc.c:8:47
[0x0000000100000554-0x0000000100000558): condloc.c:9:22
[0x0000000100000558-0x0000000100000564): condloc.c:9:19
[0x0000000100000564-0x0000000100000568): condloc.c:10:9
[0x0000000100000578-0x000000010000057c): condloc.c:11:5
[0x000000010000058c-0x0000000100000590): condloc.c:12:26
[0x0000000100000590-0x00000001000005a4): condloc.c:12:5
[0x00000001000005a4-0x00000001000005a8): condloc.c:13:5
[0x00000001000005a8-0x00000001000005b4): condloc.c:13:5
"""


def test_a_location_that_may_not_run_makes_the_count_an_upper_bound():
    # Three locations (`if (z)`, the block's `int w = 5`, `i = 0`) each stop at most once
    # per start, but the block's runs only when z: "3 times" stopped twice.
    def stops(line, *locations):
        return judged(CONDITIONAL, line, list(locations), CONDITIONAL_INFO)

    assert stops(4, 0x100000488, 0x100000494, 0x1000004B0) == (3, False)
    assert stops(8, 0x100000504, 0x100000520, 0x10000053C) == (3, False)
    # Those on every path into the loop alone: exact.
    assert stops(4, 0x100000488, 0x1000004B0) == (2, True)
    assert loop_header_note(4, 3, False).startswith(
        "Line 4 is a loop header: it stops up to 3 times each time its loop starts"
    )
    assert "it stops at most once each" in loop_header_note(4, 1, False)


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
    # GCC's layout: `i = 0` and the jump to the condition at the bottom, both before it;
    # the body (line 5), then the increment (line 4) falling into the condition.
    table = [
        (0x401121, 0x40112A, 4, 14),
        (0x40112A, 0x40112F, 5, 9),
        (0x40112F, 0x401138, 4, 28),
        (0x401138, 0x40113E, 4, 19),
    ]
    assert stops_before_loop(found, table, 4, [0x401121]) == (1, True)
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
        table = java_line_table(JAVA_LINES, len(JAVA))
        return stops_before_loop(instructions, table, line, [min(pcs)])

    # java-debug binds the line's first bytecode: `i = 0` and the array's copy run once.
    assert (stops(6), stops(16)) == ((1, True), (1, True))
    # The condition the loop returns to: every pass.
    assert (stops(10), stops(15), stops(20), stops(7)) == (None, None, None, None)
    # A switch's targets, `wide`, and code that does not decode.
    switch = bytes([0x1B, 0xAA, 0, 0, 0, 0, 0, 0x17, 0, 0, 0, 0, 0, 0, 0, 1])
    switch += bytes([0, 0, 0, 0x18, 0, 0, 0, 0x19, 0xB1, 0xB1, 0xB1])
    assert java_instructions(switch)[1] == (1, "jump", (24, 25, 26))
    # A branch into the middle of an instruction is no bytecode a JVM runs.
    assert java_instructions(switch[:7] + bytes([0x13]) + switch[8:]) is None
    assert java_instructions(bytes([0xC4, 0x84, 0, 1, 0, 1, 0xB1]))[1] == (6, "return", ())
    assert java_instructions(bytes([0x01, 0xBF]))[1] == (1, "stop", ())
    assert java_instructions(bytes([0xCB])) is None
    assert java_instructions(bytes([0xA7, 0x7F, 0x00])) is None


def test_the_worker_says_it_once_at_launch_and_drops_its_per_pass_warning(tmp_path):
    listing = """  1.1: where = loops`loops::main::h0123456789abcdef + 20 at main.rs:4:14, address = 0x0000000100000474, resolved
  1.2: where = loops`loops::main::h0123456789abcdef + 24 at main.rs:4:5, address = 0x0000000100000478, resolved
"""
    from peeky.native import breakpoint_locations

    asked = []

    def stops(dap, source, line, groups, cache):
        asked.append((line, groups))
        return 2, True

    runtime = SimpleNamespace(
        breakpoint_locations=lambda dap, ids, deadline=None: breakpoint_locations(
            listing, set(ids)
        ),
        loop_header_stops=stops,
        source_path=lambda program: program,
    )
    worker = DebugWorker(tmp_path)
    worker.dap, worker.program = object(), tmp_path / "main.rs"
    placed = {"breakpoints": [{"id": 1, "line": 4, "verified": True}]}
    worker.note_places(runtime, [{"line": 4}], placed)
    # At launch, once.
    worker.loop_lines = worker.loop_headers(runtime)
    assert asked == [(4, [(0x100000474, 0x100000478)])]
    assert worker.loop_warnings() == [
        loop_header_note(4, 2) + " (each hit's stop, 1 to 2, says which)."
    ]
    assert worker.place_warnings() == []
    # Review of #166: breakpoints set again (a probe re-arming after a --set-at) or
    # asked again keep the launch's answer, never analysing while the program waits.
    worker.note_places(runtime, [{"line": 4}], placed)
    worker.stop_reason = "breakpoint"
    worker.place_of({"line": 4, "instructionPointerReference": "0x100000999"})
    assert len(asked) == 1 and worker.loop_warnings()
    # A runtime that cannot say: #157's warning stands.
    del runtime.loop_header_stops
    worker.loop_lines = worker.loop_headers(runtime)
    assert worker.loop_warnings() == []
    assert worker.place_warnings()[0].startswith("Line 4 stops up to 2 times per pass")


def test_the_debugger_is_asked_once_per_file_and_function(tmp_path):
    # Review of #166: each line and each place disassembled its function and read its
    # line again (240 inlined copies: 1.1 s at launch).
    from peeky.native import NativeSession

    source = tmp_path / "u.c"
    source.write_text("\n".join(f"line {n}" for n in range(1, 20)).replace("line 3", "for (;;) {"))
    text = source.read_text().splitlines()
    text[14] = "for (int k = 0; k < 2; k++) {"
    text[6] = "x = 1;"
    source.write_text("\n".join(text) + "\n")
    commands = []

    class Dap:
        def request(self, command, arguments, timeout=None):
            commands.append(arguments["expression"].split()[1])
            return {"result": NESTED if "disassemble" in arguments["expression"] else NESTED_INFO}

    cache = {}

    def stops(line, *groups):
        session = object.__new__(NativeSession)
        return session.loop_header_stops(Dap(), source, line, list(groups), cache)

    assert stops(3, (0x100000438,)) == (1, True) and stops(15, (0x100000524,)) == (1, True)
    # One line table for the file, one disassembly for the function.
    assert commands == ["info", "-a"]
    # A line naming no loop is not analysed; nor one in more than 8 places.
    assert stops(7, (0x1000004A0,)) is None and commands == ["info", "-a"]
    assert stops(15, *[(0x100000524 + 4 * n,) for n in range(9)]) is None
    assert commands == ["info", "-a"]
