"""#330 (found preparing the twelfth evaluation): after the program copied a local, a change to
it was `[settable]` where the local was then replaced or ended before the copy was used.
`u.push(y); let t = n; y = t;`, then `u` printed: `--set y=100` at `let t = n;` was reported,
and the output kept the old value. The copy rule (#192) counted a copy only where it was used
before the local was replaced or ended. Now a use after counts too, and an argument ends where
the function returns. A value no path reads again (it is overwritten, or it ends, first) is
refused too, saying where; a local the program reads again before replacing it stays
settable."""

import re
from types import SimpleNamespace

from peeky.rust import RustSession
from peeky.rust_mir import CrateType, IrFunction, OwnedLocals, TypeLayouts, parse_body

# The crate the fixtures below are rustc 1.96.0's MIR of (`-Zmir-opt-level=0
# -Zdump-mir=PreCodegen -Zmir-include-spans=yes`, as Peeky builds it; its comment lines left
# out).
SOURCE = """#[derive(Clone, Copy)]
struct Lap {
    start: u32,
    end: u32,
}

fn pushed(n: u32) -> u32 {
    let mut y = n + 7;
    let mut u: Vec<u32> = Vec::new();
    u.push(y);
    let t = n;
    y = t;
    y + u[0]
}

fn replaced(n: u32) -> u32 {
    let mut x = n + 9;
    let z = x;
    let k = n;
    x = k;
    x + z
}

fn later(n: u32) -> u32 {
    let mut x = n + 5;
    let mut w: Vec<u32> = Vec::new();
    w.push(x);
    let k = n;
    let s = x + k;
    x = s;
    x + w[0]
}

fn lapped(n: u32) -> u32 {
    let mut laps: Vec<Lap> = Vec::new();
    let mut lap = Lap { start: n, end: n + 10 };
    laps.push(lap);
    let finish = lap.end;
    lap.start = finish;
    laps[0].end - laps[0].start
}

fn unread(n: u32) -> u32 {
    let mut total = n + 1;
    let k = n * 2;
    total = k + 3;
    let mut b = 0;
    if n > 0 {
        let mut a = n + 1;
        a *= 3;
        b = a;
        let r = &a;
        b += *r;
        b += 1;
    }
    total + b
}

fn param(x: u32) -> u32 {
    let z = x;
    let k = n2();
    z + k
}

fn n2() -> u32 {
    2
}

fn main() {
    let n = std::env::args().count() as u32;
    println!("{} {} {} {} {} {}", pushed(n), replaced(n), later(n), lapped(n), unread(n), param(n));
}
"""


PUSHED = """// MIR for `pushed` after PreCodegen

fn pushed(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:7:11: 7:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:7:22: 7:25
    let mut _2: u32;                     // in scope 0 at src/main.rs:8:9: 8:14
    let mut _3: u32;                     // in scope 0 at src/main.rs:8:17: 8:18
    let mut _4: (u32, bool);             // in scope 0 at src/main.rs:8:17: 8:22
    let _6: ();                          // in scope 0 at src/main.rs:10:5: 10:14
    let mut _7: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:10:5: 10:6
    let mut _8: u32;                     // in scope 0 at src/main.rs:10:12: 10:13
    let mut _10: u32;                    // in scope 0 at src/main.rs:12:9: 12:10
    let mut _11: u32;                    // in scope 0 at src/main.rs:13:5: 13:6
    let mut _12: u32;                    // in scope 0 at src/main.rs:13:9: 13:13
    let mut _13: &u32;                   // in scope 0 at src/main.rs:13:10: 13:13
    let mut _14: &std::vec::Vec<u32>;    // in scope 0 at src/main.rs:13:9: 13:10
    let mut _15: (u32, bool);            // in scope 0 at src/main.rs:13:5: 13:13
    scope 1 {
        debug y => _2;                   // in scope 1 at src/main.rs:8:9: 8:14
        let mut _5: std::vec::Vec<u32>;  // in scope 1 at src/main.rs:9:9: 9:14
        scope 2 {
            debug u => _5;               // in scope 2 at src/main.rs:9:9: 9:14
            let _9: u32;                 // in scope 2 at src/main.rs:11:9: 11:10
            scope 3 {
                debug t => _9;           // in scope 3 at src/main.rs:11:9: 11:10
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:8:9: 8:14
        StorageLive(_3);                 // scope 0 at src/main.rs:8:17: 8:18
        _3 = copy _1;                    // scope 0 at src/main.rs:8:17: 8:18
        _4 = AddWithOverflow(copy _3, const 7_u32); // scope 0 at src/main.rs:8:17: 8:22
        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, const 7_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:8:17: 8:22
    }
    bb1: {
        _2 = move (_4.0: u32);           // scope 0 at src/main.rs:8:17: 8:22
        StorageDead(_3);                 // scope 0 at src/main.rs:8:21: 8:22
        StorageLive(_5);                 // scope 1 at src/main.rs:9:9: 9:14
        _5 = Vec::<u32>::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:9:27: 9:37
    }
    bb2: {
        StorageLive(_6);                 // scope 2 at src/main.rs:10:5: 10:14
        StorageLive(_7);                 // scope 2 at src/main.rs:10:5: 10:6
        _7 = &mut _5;                    // scope 2 at src/main.rs:10:5: 10:6
        StorageLive(_8);                 // scope 2 at src/main.rs:10:12: 10:13
        _8 = copy _2;                    // scope 2 at src/main.rs:10:12: 10:13
        _6 = Vec::<u32>::push(move _7, move _8) -> [return: bb3, unwind: bb7]; // scope 2 at src/main.rs:10:5: 10:14
    }
    bb3: {
        StorageDead(_8);                 // scope 2 at src/main.rs:10:13: 10:14
        StorageDead(_7);                 // scope 2 at src/main.rs:10:13: 10:14
        StorageDead(_6);                 // scope 2 at src/main.rs:10:14: 10:15
        StorageLive(_9);                 // scope 2 at src/main.rs:11:9: 11:10
        _9 = copy _1;                    // scope 2 at src/main.rs:11:13: 11:14
        StorageLive(_10);                // scope 3 at src/main.rs:12:9: 12:10
        _10 = copy _9;                   // scope 3 at src/main.rs:12:9: 12:10
        _2 = move _10;                   // scope 3 at src/main.rs:12:5: 12:10
        StorageDead(_10);                // scope 3 at src/main.rs:12:9: 12:10
        StorageLive(_11);                // scope 3 at src/main.rs:13:5: 13:6
        _11 = copy _2;                   // scope 3 at src/main.rs:13:5: 13:6
        StorageLive(_12);                // scope 3 at src/main.rs:13:9: 13:13
        StorageLive(_13);                // scope 3 at src/main.rs:13:10: 13:13
        StorageLive(_14);                // scope 3 at src/main.rs:13:9: 13:10
        _14 = &_5;                       // scope 3 at src/main.rs:13:9: 13:10
        _13 = <Vec<u32> as Index<usize>>::index(move _14, const 0_usize) -> [return: bb4, unwind: bb7]; // scope 3 at src/main.rs:13:10: 13:13
    }
    bb4: {
        _12 = copy (*_13);               // scope 3 at src/main.rs:13:9: 13:13
        StorageDead(_14);                // scope 3 at src/main.rs:13:12: 13:13
        _15 = AddWithOverflow(copy _11, copy _12); // scope 3 at src/main.rs:13:5: 13:13
        assert(!move (_15.1: bool), "attempt to compute `{} + {}`, which would overflow", move _11, move _12) -> [success: bb5, unwind: bb7]; // scope 3 at src/main.rs:13:5: 13:13
    }
    bb5: {
        _0 = move (_15.0: u32);          // scope 3 at src/main.rs:13:5: 13:13
        StorageDead(_13);                // scope 3 at src/main.rs:13:12: 13:13
        StorageDead(_12);                // scope 3 at src/main.rs:13:12: 13:13
        StorageDead(_11);                // scope 3 at src/main.rs:13:12: 13:13
        StorageDead(_9);                 // scope 2 at src/main.rs:14:1: 14:2
        drop(_5) -> [return: bb6, unwind: bb8]; // scope 1 at src/main.rs:14:1: 14:2
    }
    bb6: {
        StorageDead(_5);                 // scope 1 at src/main.rs:14:1: 14:2
        StorageDead(_2);                 // scope 0 at src/main.rs:14:1: 14:2
        return;                          // scope 0 at src/main.rs:14:2: 14:2
    }
    bb7 (cleanup): {
        drop(_5) -> [return: bb8, unwind terminate(cleanup)]; // scope 1 at src/main.rs:14:1: 14:2
    }
    bb8 (cleanup): {
        resume;                          // scope 0 at src/main.rs:7:1: 14:2
    }
}
"""

REPLACED = """// MIR for `replaced` after PreCodegen

fn replaced(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:16:13: 16:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:16:24: 16:27
    let mut _2: u32;                     // in scope 0 at src/main.rs:17:9: 17:14
    let mut _3: u32;                     // in scope 0 at src/main.rs:17:17: 17:18
    let mut _4: (u32, bool);             // in scope 0 at src/main.rs:17:17: 17:22
    let mut _7: u32;                     // in scope 0 at src/main.rs:20:9: 20:10
    let mut _8: u32;                     // in scope 0 at src/main.rs:21:5: 21:6
    let mut _9: u32;                     // in scope 0 at src/main.rs:21:9: 21:10
    let mut _10: (u32, bool);            // in scope 0 at src/main.rs:21:5: 21:10
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:17:9: 17:14
        let _5: u32;                     // in scope 1 at src/main.rs:18:9: 18:10
        scope 2 {
            debug z => _5;               // in scope 2 at src/main.rs:18:9: 18:10
            let _6: u32;                 // in scope 2 at src/main.rs:19:9: 19:10
            scope 3 {
                debug k => _6;           // in scope 3 at src/main.rs:19:9: 19:10
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:17:9: 17:14
        StorageLive(_3);                 // scope 0 at src/main.rs:17:17: 17:18
        _3 = copy _1;                    // scope 0 at src/main.rs:17:17: 17:18
        _4 = AddWithOverflow(copy _3, const 9_u32); // scope 0 at src/main.rs:17:17: 17:22
        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, const 9_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:17:17: 17:22
    }
    bb1: {
        _2 = move (_4.0: u32);           // scope 0 at src/main.rs:17:17: 17:22
        StorageDead(_3);                 // scope 0 at src/main.rs:17:21: 17:22
        StorageLive(_5);                 // scope 1 at src/main.rs:18:9: 18:10
        _5 = copy _2;                    // scope 1 at src/main.rs:18:13: 18:14
        StorageLive(_6);                 // scope 2 at src/main.rs:19:9: 19:10
        _6 = copy _1;                    // scope 2 at src/main.rs:19:13: 19:14
        StorageLive(_7);                 // scope 3 at src/main.rs:20:9: 20:10
        _7 = copy _6;                    // scope 3 at src/main.rs:20:9: 20:10
        _2 = move _7;                    // scope 3 at src/main.rs:20:5: 20:10
        StorageDead(_7);                 // scope 3 at src/main.rs:20:9: 20:10
        StorageLive(_8);                 // scope 3 at src/main.rs:21:5: 21:6
        _8 = copy _2;                    // scope 3 at src/main.rs:21:5: 21:6
        StorageLive(_9);                 // scope 3 at src/main.rs:21:9: 21:10
        _9 = copy _5;                    // scope 3 at src/main.rs:21:9: 21:10
        _10 = AddWithOverflow(copy _8, copy _9); // scope 3 at src/main.rs:21:5: 21:10
        assert(!move (_10.1: bool), "attempt to compute `{} + {}`, which would overflow", move _8, move _9) -> [success: bb2, unwind continue]; // scope 3 at src/main.rs:21:5: 21:10
    }
    bb2: {
        _0 = move (_10.0: u32);          // scope 3 at src/main.rs:21:5: 21:10
        StorageDead(_9);                 // scope 3 at src/main.rs:21:9: 21:10
        StorageDead(_8);                 // scope 3 at src/main.rs:21:9: 21:10
        StorageDead(_6);                 // scope 2 at src/main.rs:22:1: 22:2
        StorageDead(_5);                 // scope 1 at src/main.rs:22:1: 22:2
        StorageDead(_2);                 // scope 0 at src/main.rs:22:1: 22:2
        return;                          // scope 0 at src/main.rs:22:2: 22:2
    }
}
"""

LATER = """// MIR for `later` after PreCodegen

fn later(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:24:10: 24:11
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:24:21: 24:24
    let mut _2: u32;                     // in scope 0 at src/main.rs:25:9: 25:14
    let mut _3: u32;                     // in scope 0 at src/main.rs:25:17: 25:18
    let mut _4: (u32, bool);             // in scope 0 at src/main.rs:25:17: 25:22
    let _6: ();                          // in scope 0 at src/main.rs:27:5: 27:14
    let mut _7: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:27:5: 27:6
    let mut _8: u32;                     // in scope 0 at src/main.rs:27:12: 27:13
    let mut _11: u32;                    // in scope 0 at src/main.rs:29:13: 29:14
    let mut _12: u32;                    // in scope 0 at src/main.rs:29:17: 29:18
    let mut _13: (u32, bool);            // in scope 0 at src/main.rs:29:13: 29:18
    let mut _14: u32;                    // in scope 0 at src/main.rs:30:9: 30:10
    let mut _15: u32;                    // in scope 0 at src/main.rs:31:5: 31:6
    let mut _16: u32;                    // in scope 0 at src/main.rs:31:9: 31:13
    let mut _17: &u32;                   // in scope 0 at src/main.rs:31:10: 31:13
    let mut _18: &std::vec::Vec<u32>;    // in scope 0 at src/main.rs:31:9: 31:10
    let mut _19: (u32, bool);            // in scope 0 at src/main.rs:31:5: 31:13
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:25:9: 25:14
        let mut _5: std::vec::Vec<u32>;  // in scope 1 at src/main.rs:26:9: 26:14
        scope 2 {
            debug w => _5;               // in scope 2 at src/main.rs:26:9: 26:14
            let _9: u32;                 // in scope 2 at src/main.rs:28:9: 28:10
            scope 3 {
                debug k => _9;           // in scope 3 at src/main.rs:28:9: 28:10
                let _10: u32;            // in scope 3 at src/main.rs:29:9: 29:10
                scope 4 {
                    debug s => _10;      // in scope 4 at src/main.rs:29:9: 29:10
                }
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:25:9: 25:14
        StorageLive(_3);                 // scope 0 at src/main.rs:25:17: 25:18
        _3 = copy _1;                    // scope 0 at src/main.rs:25:17: 25:18
        _4 = AddWithOverflow(copy _3, const 5_u32); // scope 0 at src/main.rs:25:17: 25:22
        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, const 5_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:25:17: 25:22
    }
    bb1: {
        _2 = move (_4.0: u32);           // scope 0 at src/main.rs:25:17: 25:22
        StorageDead(_3);                 // scope 0 at src/main.rs:25:21: 25:22
        StorageLive(_5);                 // scope 1 at src/main.rs:26:9: 26:14
        _5 = Vec::<u32>::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:26:27: 26:37
    }
    bb2: {
        StorageLive(_6);                 // scope 2 at src/main.rs:27:5: 27:14
        StorageLive(_7);                 // scope 2 at src/main.rs:27:5: 27:6
        _7 = &mut _5;                    // scope 2 at src/main.rs:27:5: 27:6
        StorageLive(_8);                 // scope 2 at src/main.rs:27:12: 27:13
        _8 = copy _2;                    // scope 2 at src/main.rs:27:12: 27:13
        _6 = Vec::<u32>::push(move _7, move _8) -> [return: bb3, unwind: bb8]; // scope 2 at src/main.rs:27:5: 27:14
    }
    bb3: {
        StorageDead(_8);                 // scope 2 at src/main.rs:27:13: 27:14
        StorageDead(_7);                 // scope 2 at src/main.rs:27:13: 27:14
        StorageDead(_6);                 // scope 2 at src/main.rs:27:14: 27:15
        StorageLive(_9);                 // scope 2 at src/main.rs:28:9: 28:10
        _9 = copy _1;                    // scope 2 at src/main.rs:28:13: 28:14
        StorageLive(_10);                // scope 3 at src/main.rs:29:9: 29:10
        StorageLive(_11);                // scope 3 at src/main.rs:29:13: 29:14
        _11 = copy _2;                   // scope 3 at src/main.rs:29:13: 29:14
        StorageLive(_12);                // scope 3 at src/main.rs:29:17: 29:18
        _12 = copy _9;                   // scope 3 at src/main.rs:29:17: 29:18
        _13 = AddWithOverflow(copy _11, copy _12); // scope 3 at src/main.rs:29:13: 29:18
        assert(!move (_13.1: bool), "attempt to compute `{} + {}`, which would overflow", move _11, move _12) -> [success: bb4, unwind: bb8]; // scope 3 at src/main.rs:29:13: 29:18
    }
    bb4: {
        _10 = move (_13.0: u32);         // scope 3 at src/main.rs:29:13: 29:18
        StorageDead(_12);                // scope 3 at src/main.rs:29:17: 29:18
        StorageDead(_11);                // scope 3 at src/main.rs:29:17: 29:18
        StorageLive(_14);                // scope 4 at src/main.rs:30:9: 30:10
        _14 = copy _10;                  // scope 4 at src/main.rs:30:9: 30:10
        _2 = move _14;                   // scope 4 at src/main.rs:30:5: 30:10
        StorageDead(_14);                // scope 4 at src/main.rs:30:9: 30:10
        StorageLive(_15);                // scope 4 at src/main.rs:31:5: 31:6
        _15 = copy _2;                   // scope 4 at src/main.rs:31:5: 31:6
        StorageLive(_16);                // scope 4 at src/main.rs:31:9: 31:13
        StorageLive(_17);                // scope 4 at src/main.rs:31:10: 31:13
        StorageLive(_18);                // scope 4 at src/main.rs:31:9: 31:10
        _18 = &_5;                       // scope 4 at src/main.rs:31:9: 31:10
        _17 = <Vec<u32> as Index<usize>>::index(move _18, const 0_usize) -> [return: bb5, unwind: bb8]; // scope 4 at src/main.rs:31:10: 31:13
    }
    bb5: {
        _16 = copy (*_17);               // scope 4 at src/main.rs:31:9: 31:13
        StorageDead(_18);                // scope 4 at src/main.rs:31:12: 31:13
        _19 = AddWithOverflow(copy _15, copy _16); // scope 4 at src/main.rs:31:5: 31:13
        assert(!move (_19.1: bool), "attempt to compute `{} + {}`, which would overflow", move _15, move _16) -> [success: bb6, unwind: bb8]; // scope 4 at src/main.rs:31:5: 31:13
    }
    bb6: {
        _0 = move (_19.0: u32);          // scope 4 at src/main.rs:31:5: 31:13
        StorageDead(_17);                // scope 4 at src/main.rs:31:12: 31:13
        StorageDead(_16);                // scope 4 at src/main.rs:31:12: 31:13
        StorageDead(_15);                // scope 4 at src/main.rs:31:12: 31:13
        StorageDead(_10);                // scope 3 at src/main.rs:32:1: 32:2
        StorageDead(_9);                 // scope 2 at src/main.rs:32:1: 32:2
        drop(_5) -> [return: bb7, unwind: bb9]; // scope 1 at src/main.rs:32:1: 32:2
    }
    bb7: {
        StorageDead(_5);                 // scope 1 at src/main.rs:32:1: 32:2
        StorageDead(_2);                 // scope 0 at src/main.rs:32:1: 32:2
        return;                          // scope 0 at src/main.rs:32:2: 32:2
    }
    bb8 (cleanup): {
        drop(_5) -> [return: bb9, unwind terminate(cleanup)]; // scope 1 at src/main.rs:32:1: 32:2
    }
    bb9 (cleanup): {
        resume;                          // scope 0 at src/main.rs:24:1: 32:2
    }
}
"""

LAPPED = """// MIR for `lapped` after PreCodegen

fn lapped(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:34:11: 34:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:34:22: 34:25
    let mut _2: std::vec::Vec<Lap>;      // in scope 0 at src/main.rs:35:9: 35:17
    let mut _4: u32;                     // in scope 0 at src/main.rs:36:32: 36:33
    let mut _5: u32;                     // in scope 0 at src/main.rs:36:40: 36:46
    let mut _6: u32;                     // in scope 0 at src/main.rs:36:40: 36:41
    let mut _7: (u32, bool);             // in scope 0 at src/main.rs:36:40: 36:46
    let _8: ();                          // in scope 0 at src/main.rs:37:5: 37:19
    let mut _9: &mut std::vec::Vec<Lap>; // in scope 0 at src/main.rs:37:5: 37:9
    let mut _10: Lap;                    // in scope 0 at src/main.rs:37:15: 37:18
    let mut _12: u32;                    // in scope 0 at src/main.rs:39:17: 39:23
    let mut _13: u32;                    // in scope 0 at src/main.rs:40:5: 40:16
    let mut _14: &Lap;                   // in scope 0 at src/main.rs:40:9: 40:12
    let mut _15: &std::vec::Vec<Lap>;    // in scope 0 at src/main.rs:40:5: 40:9
    let mut _16: u32;                    // in scope 0 at src/main.rs:40:19: 40:32
    let mut _17: &Lap;                   // in scope 0 at src/main.rs:40:23: 40:26
    let mut _18: &std::vec::Vec<Lap>;    // in scope 0 at src/main.rs:40:19: 40:23
    let mut _19: (u32, bool);            // in scope 0 at src/main.rs:40:5: 40:32
    scope 1 {
        debug laps => _2;                // in scope 1 at src/main.rs:35:9: 35:17
        let mut _3: Lap;                 // in scope 1 at src/main.rs:36:9: 36:16
        scope 2 {
            debug lap => _3;             // in scope 2 at src/main.rs:36:9: 36:16
            let _11: u32;                // in scope 2 at src/main.rs:38:9: 38:15
            scope 3 {
                debug finish => _11;     // in scope 3 at src/main.rs:38:9: 38:15
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:35:9: 35:17
        _2 = Vec::<Lap>::new() -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:35:30: 35:40
    }
    bb1: {
        StorageLive(_3);                 // scope 1 at src/main.rs:36:9: 36:16
        StorageLive(_4);                 // scope 1 at src/main.rs:36:32: 36:33
        _4 = copy _1;                    // scope 1 at src/main.rs:36:32: 36:33
        StorageLive(_5);                 // scope 1 at src/main.rs:36:40: 36:46
        StorageLive(_6);                 // scope 1 at src/main.rs:36:40: 36:41
        _6 = copy _1;                    // scope 1 at src/main.rs:36:40: 36:41
        _7 = AddWithOverflow(copy _6, const 10_u32); // scope 1 at src/main.rs:36:40: 36:46
        assert(!move (_7.1: bool), "attempt to compute `{} + {}`, which would overflow", move _6, const 10_u32) -> [success: bb2, unwind: bb8]; // scope 1 at src/main.rs:36:40: 36:46
    }
    bb2: {
        _5 = move (_7.0: u32);           // scope 1 at src/main.rs:36:40: 36:46
        StorageDead(_6);                 // scope 1 at src/main.rs:36:45: 36:46
        _3 = Lap { start: move _4, end: move _5 }; // scope 1 at src/main.rs:36:19: 36:48
        StorageDead(_5);                 // scope 1 at src/main.rs:36:47: 36:48
        StorageDead(_4);                 // scope 1 at src/main.rs:36:47: 36:48
        StorageLive(_8);                 // scope 2 at src/main.rs:37:5: 37:19
        StorageLive(_9);                 // scope 2 at src/main.rs:37:5: 37:9
        _9 = &mut _2;                    // scope 2 at src/main.rs:37:5: 37:9
        StorageLive(_10);                // scope 2 at src/main.rs:37:15: 37:18
        _10 = copy _3;                   // scope 2 at src/main.rs:37:15: 37:18
        _8 = Vec::<Lap>::push(move _9, move _10) -> [return: bb3, unwind: bb8]; // scope 2 at src/main.rs:37:5: 37:19
    }
    bb3: {
        StorageDead(_10);                // scope 2 at src/main.rs:37:18: 37:19
        StorageDead(_9);                 // scope 2 at src/main.rs:37:18: 37:19
        StorageDead(_8);                 // scope 2 at src/main.rs:37:19: 37:20
        StorageLive(_11);                // scope 2 at src/main.rs:38:9: 38:15
        _11 = copy (_3.1: u32);          // scope 2 at src/main.rs:38:18: 38:25
        StorageLive(_12);                // scope 3 at src/main.rs:39:17: 39:23
        _12 = copy _11;                  // scope 3 at src/main.rs:39:17: 39:23
        (_3.0: u32) = move _12;          // scope 3 at src/main.rs:39:5: 39:23
        StorageDead(_12);                // scope 3 at src/main.rs:39:22: 39:23
        StorageLive(_13);                // scope 3 at src/main.rs:40:5: 40:16
        StorageLive(_14);                // scope 3 at src/main.rs:40:9: 40:12
        StorageLive(_15);                // scope 3 at src/main.rs:40:5: 40:9
        _15 = &_2;                       // scope 3 at src/main.rs:40:5: 40:9
        _14 = <Vec<Lap> as Index<usize>>::index(move _15, const 0_usize) -> [return: bb4, unwind: bb8]; // scope 3 at src/main.rs:40:9: 40:12
    }
    bb4: {
        StorageDead(_15);                // scope 3 at src/main.rs:40:11: 40:12
        _13 = copy ((*_14).1: u32);      // scope 3 at src/main.rs:40:5: 40:16
        StorageLive(_16);                // scope 3 at src/main.rs:40:19: 40:32
        StorageLive(_17);                // scope 3 at src/main.rs:40:23: 40:26
        StorageLive(_18);                // scope 3 at src/main.rs:40:19: 40:23
        _18 = &_2;                       // scope 3 at src/main.rs:40:19: 40:23
        _17 = <Vec<Lap> as Index<usize>>::index(move _18, const 0_usize) -> [return: bb5, unwind: bb8]; // scope 3 at src/main.rs:40:23: 40:26
    }
    bb5: {
        StorageDead(_18);                // scope 3 at src/main.rs:40:25: 40:26
        _16 = copy ((*_17).0: u32);      // scope 3 at src/main.rs:40:19: 40:32
        _19 = SubWithOverflow(copy _13, copy _16); // scope 3 at src/main.rs:40:5: 40:32
        assert(!move (_19.1: bool), "attempt to compute `{} - {}`, which would overflow", move _13, move _16) -> [success: bb6, unwind: bb8]; // scope 3 at src/main.rs:40:5: 40:32
    }
    bb6: {
        _0 = move (_19.0: u32);          // scope 3 at src/main.rs:40:5: 40:32
        StorageDead(_17);                // scope 3 at src/main.rs:40:31: 40:32
        StorageDead(_16);                // scope 3 at src/main.rs:40:31: 40:32
        StorageDead(_14);                // scope 3 at src/main.rs:40:31: 40:32
        StorageDead(_13);                // scope 3 at src/main.rs:40:31: 40:32
        StorageDead(_11);                // scope 2 at src/main.rs:41:1: 41:2
        StorageDead(_3);                 // scope 1 at src/main.rs:41:1: 41:2
        drop(_2) -> [return: bb7, unwind: bb9]; // scope 0 at src/main.rs:41:1: 41:2
    }
    bb7: {
        StorageDead(_2);                 // scope 0 at src/main.rs:41:1: 41:2
        return;                          // scope 0 at src/main.rs:41:2: 41:2
    }
    bb8 (cleanup): {
        drop(_2) -> [return: bb9, unwind terminate(cleanup)]; // scope 0 at src/main.rs:41:1: 41:2
    }
    bb9 (cleanup): {
        resume;                          // scope 0 at src/main.rs:34:1: 41:2
    }
}
"""

UNREAD = """// MIR for `unread` after PreCodegen

fn unread(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:43:11: 43:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:43:22: 43:25
    let mut _2: u32;                     // in scope 0 at src/main.rs:44:9: 44:18
    let mut _3: u32;                     // in scope 0 at src/main.rs:44:21: 44:22
    let mut _4: (u32, bool);             // in scope 0 at src/main.rs:44:21: 44:26
    let mut _6: u32;                     // in scope 0 at src/main.rs:45:13: 45:14
    let mut _7: (u32, bool);             // in scope 0 at src/main.rs:45:13: 45:18
    let mut _8: u32;                     // in scope 0 at src/main.rs:46:13: 46:14
    let mut _9: (u32, bool);             // in scope 0 at src/main.rs:46:13: 46:18
    let _11: ();                         // in scope 0 at src/main.rs:48:5: 55:6
    let mut _12: bool;                   // in scope 0 at src/main.rs:48:8: 48:13
    let mut _13: u32;                    // in scope 0 at src/main.rs:48:8: 48:9
    let mut _15: u32;                    // in scope 0 at src/main.rs:49:21: 49:22
    let mut _16: (u32, bool);            // in scope 0 at src/main.rs:49:21: 49:26
    let mut _17: (u32, bool);            // in scope 0 at src/main.rs:50:9: 50:15
    let mut _18: u32;                    // in scope 0 at src/main.rs:51:13: 51:14
    let mut _20: u32;                    // in scope 0 at src/main.rs:53:14: 53:16
    let mut _21: (u32, bool);            // in scope 0 at src/main.rs:53:9: 53:16
    let mut _22: (u32, bool);            // in scope 0 at src/main.rs:54:9: 54:15
    let mut _23: u32;                    // in scope 0 at src/main.rs:56:5: 56:10
    let mut _24: u32;                    // in scope 0 at src/main.rs:56:13: 56:14
    let mut _25: (u32, bool);            // in scope 0 at src/main.rs:56:5: 56:14
    scope 1 {
        debug total => _2;               // in scope 1 at src/main.rs:44:9: 44:18
        let _5: u32;                     // in scope 1 at src/main.rs:45:9: 45:10
        scope 2 {
            debug k => _5;               // in scope 2 at src/main.rs:45:9: 45:10
            let mut _10: u32;            // in scope 2 at src/main.rs:47:9: 47:14
            scope 3 {
                debug b => _10;          // in scope 3 at src/main.rs:47:9: 47:14
                let mut _14: u32;        // in scope 3 at src/main.rs:49:13: 49:18
                scope 4 {
                    debug a => _14;      // in scope 4 at src/main.rs:49:13: 49:18
                    let _19: &u32;       // in scope 4 at src/main.rs:52:13: 52:14
                    scope 5 {
                        debug r => _19;  // in scope 5 at src/main.rs:52:13: 52:14
                    }
                }
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:44:9: 44:18
        StorageLive(_3);                 // scope 0 at src/main.rs:44:21: 44:22
        _3 = copy _1;                    // scope 0 at src/main.rs:44:21: 44:22
        _4 = AddWithOverflow(copy _3, const 1_u32); // scope 0 at src/main.rs:44:21: 44:26
        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:44:21: 44:26
    }
    bb1: {
        _2 = move (_4.0: u32);           // scope 0 at src/main.rs:44:21: 44:26
        StorageDead(_3);                 // scope 0 at src/main.rs:44:25: 44:26
        StorageLive(_5);                 // scope 1 at src/main.rs:45:9: 45:10
        StorageLive(_6);                 // scope 1 at src/main.rs:45:13: 45:14
        _6 = copy _1;                    // scope 1 at src/main.rs:45:13: 45:14
        _7 = MulWithOverflow(copy _6, const 2_u32); // scope 1 at src/main.rs:45:13: 45:18
        assert(!move (_7.1: bool), "attempt to compute `{} * {}`, which would overflow", move _6, const 2_u32) -> [success: bb2, unwind continue]; // scope 1 at src/main.rs:45:13: 45:18
    }
    bb2: {
        _5 = move (_7.0: u32);           // scope 1 at src/main.rs:45:13: 45:18
        StorageDead(_6);                 // scope 1 at src/main.rs:45:17: 45:18
        StorageLive(_8);                 // scope 2 at src/main.rs:46:13: 46:14
        _8 = copy _5;                    // scope 2 at src/main.rs:46:13: 46:14
        _9 = AddWithOverflow(copy _8, const 3_u32); // scope 2 at src/main.rs:46:13: 46:18
        assert(!move (_9.1: bool), "attempt to compute `{} + {}`, which would overflow", move _8, const 3_u32) -> [success: bb3, unwind continue]; // scope 2 at src/main.rs:46:13: 46:18
    }
    bb3: {
        _2 = move (_9.0: u32);           // scope 2 at src/main.rs:46:5: 46:18
        StorageDead(_8);                 // scope 2 at src/main.rs:46:17: 46:18
        StorageLive(_10);                // scope 2 at src/main.rs:47:9: 47:14
        _10 = const 0_u32;               // scope 2 at src/main.rs:47:17: 47:18
        StorageLive(_11);                // scope 3 at src/main.rs:48:5: 55:6
        StorageLive(_12);                // scope 3 at src/main.rs:48:8: 48:13
        StorageLive(_13);                // scope 3 at src/main.rs:48:8: 48:9
        _13 = copy _1;                   // scope 3 at src/main.rs:48:8: 48:9
        _12 = Gt(move _13, const 0_u32); // scope 3 at src/main.rs:48:8: 48:13
        switchInt(move _12) -> [0: bb9, otherwise: bb4]; // scope 3 at src/main.rs:48:8: 48:13
    }
    bb4: {
        StorageDead(_13);                // scope 3 at src/main.rs:48:12: 48:13
        StorageLive(_14);                // scope 3 at src/main.rs:49:13: 49:18
        StorageLive(_15);                // scope 3 at src/main.rs:49:21: 49:22
        _15 = copy _1;                   // scope 3 at src/main.rs:49:21: 49:22
        _16 = AddWithOverflow(copy _15, const 1_u32); // scope 3 at src/main.rs:49:21: 49:26
        assert(!move (_16.1: bool), "attempt to compute `{} + {}`, which would overflow", move _15, const 1_u32) -> [success: bb5, unwind continue]; // scope 3 at src/main.rs:49:21: 49:26
    }
    bb5: {
        _14 = move (_16.0: u32);         // scope 3 at src/main.rs:49:21: 49:26
        StorageDead(_15);                // scope 3 at src/main.rs:49:25: 49:26
        _17 = MulWithOverflow(copy _14, const 3_u32); // scope 4 at src/main.rs:50:9: 50:15
        assert(!move (_17.1: bool), "attempt to compute `{} * {}`, which would overflow", copy _14, const 3_u32) -> [success: bb6, unwind continue]; // scope 4 at src/main.rs:50:9: 50:15
    }
    bb6: {
        _14 = move (_17.0: u32);         // scope 4 at src/main.rs:50:9: 50:15
        StorageLive(_18);                // scope 4 at src/main.rs:51:13: 51:14
        _18 = copy _14;                  // scope 4 at src/main.rs:51:13: 51:14
        _10 = move _18;                  // scope 4 at src/main.rs:51:9: 51:14
        StorageDead(_18);                // scope 4 at src/main.rs:51:13: 51:14
        StorageLive(_19);                // scope 4 at src/main.rs:52:13: 52:14
        _19 = &_14;                      // scope 4 at src/main.rs:52:17: 52:19
        StorageLive(_20);                // scope 5 at src/main.rs:53:14: 53:16
        _20 = copy (*_19);               // scope 5 at src/main.rs:53:14: 53:16
        _21 = AddWithOverflow(copy _10, copy _20); // scope 5 at src/main.rs:53:9: 53:16
        assert(!move (_21.1: bool), "attempt to compute `{} + {}`, which would overflow", copy _10, move _20) -> [success: bb7, unwind continue]; // scope 5 at src/main.rs:53:9: 53:16
    }
    bb7: {
        _10 = move (_21.0: u32);         // scope 5 at src/main.rs:53:9: 53:16
        StorageDead(_20);                // scope 5 at src/main.rs:53:15: 53:16
        _22 = AddWithOverflow(copy _10, const 1_u32); // scope 5 at src/main.rs:54:9: 54:15
        assert(!move (_22.1: bool), "attempt to compute `{} + {}`, which would overflow", copy _10, const 1_u32) -> [success: bb8, unwind continue]; // scope 5 at src/main.rs:54:9: 54:15
    }
    bb8: {
        _10 = move (_22.0: u32);         // scope 5 at src/main.rs:54:9: 54:15
        _11 = const ();                  // scope 3 at src/main.rs:48:14: 55:6
        StorageDead(_19);                // scope 4 at src/main.rs:55:5: 55:6
        StorageDead(_14);                // scope 3 at src/main.rs:55:5: 55:6
        goto -> bb10;                    // scope 3 at src/main.rs:48:5: 55:6
    }
    bb9: {
        StorageDead(_13);                // scope 3 at src/main.rs:48:12: 48:13
        _11 = const ();                  // scope 3 at src/main.rs:55:6: 55:6
        goto -> bb10;                    // scope 3 at src/main.rs:48:5: 55:6
    }
    bb10: {
        StorageDead(_12);                // scope 3 at src/main.rs:55:5: 55:6
        StorageDead(_11);                // scope 3 at src/main.rs:55:5: 55:6
        StorageLive(_23);                // scope 3 at src/main.rs:56:5: 56:10
        _23 = copy _2;                   // scope 3 at src/main.rs:56:5: 56:10
        StorageLive(_24);                // scope 3 at src/main.rs:56:13: 56:14
        _24 = copy _10;                  // scope 3 at src/main.rs:56:13: 56:14
        _25 = AddWithOverflow(copy _23, copy _24); // scope 3 at src/main.rs:56:5: 56:14
        assert(!move (_25.1: bool), "attempt to compute `{} + {}`, which would overflow", move _23, move _24) -> [success: bb11, unwind continue]; // scope 3 at src/main.rs:56:5: 56:14
    }
    bb11: {
        _0 = move (_25.0: u32);          // scope 3 at src/main.rs:56:5: 56:14
        StorageDead(_24);                // scope 3 at src/main.rs:56:13: 56:14
        StorageDead(_23);                // scope 3 at src/main.rs:56:13: 56:14
        StorageDead(_10);                // scope 2 at src/main.rs:57:1: 57:2
        StorageDead(_5);                 // scope 1 at src/main.rs:57:1: 57:2
        StorageDead(_2);                 // scope 0 at src/main.rs:57:1: 57:2
        return;                          // scope 0 at src/main.rs:57:2: 57:2
    }
}
"""

PARAM = """// MIR for `param` after PreCodegen

fn param(_1: u32) -> u32 {
    debug x => _1;                       // in scope 0 at src/main.rs:59:10: 59:11
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:59:21: 59:24
    let _2: u32;                         // in scope 0 at src/main.rs:60:9: 60:10
    let mut _4: u32;                     // in scope 0 at src/main.rs:62:5: 62:6
    let mut _5: u32;                     // in scope 0 at src/main.rs:62:9: 62:10
    let mut _6: (u32, bool);             // in scope 0 at src/main.rs:62:5: 62:10
    scope 1 {
        debug z => _2;                   // in scope 1 at src/main.rs:60:9: 60:10
        let _3: u32;                     // in scope 1 at src/main.rs:61:9: 61:10
        scope 2 {
            debug k => _3;               // in scope 2 at src/main.rs:61:9: 61:10
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:60:9: 60:10
        _2 = copy _1;                    // scope 0 at src/main.rs:60:13: 60:14
        StorageLive(_3);                 // scope 1 at src/main.rs:61:9: 61:10
        _3 = n2() -> [return: bb1, unwind continue]; // scope 1 at src/main.rs:61:13: 61:17
    }
    bb1: {
        StorageLive(_4);                 // scope 2 at src/main.rs:62:5: 62:6
        _4 = copy _2;                    // scope 2 at src/main.rs:62:5: 62:6
        StorageLive(_5);                 // scope 2 at src/main.rs:62:9: 62:10
        _5 = copy _3;                    // scope 2 at src/main.rs:62:9: 62:10
        _6 = AddWithOverflow(copy _4, copy _5); // scope 2 at src/main.rs:62:5: 62:10
        assert(!move (_6.1: bool), "attempt to compute `{} + {}`, which would overflow", move _4, move _5) -> [success: bb2, unwind continue]; // scope 2 at src/main.rs:62:5: 62:10
    }
    bb2: {
        _0 = move (_6.0: u32);           // scope 2 at src/main.rs:62:5: 62:10
        StorageDead(_5);                 // scope 2 at src/main.rs:62:9: 62:10
        StorageDead(_4);                 // scope 2 at src/main.rs:62:9: 62:10
        StorageDead(_3);                 // scope 1 at src/main.rs:63:1: 63:2
        StorageDead(_2);                 // scope 0 at src/main.rs:63:1: 63:2
        return;                          // scope 0 at src/main.rs:63:2: 63:2
    }
}
"""

HASH = "h0123456789abcdef"
# `Lap`, as the build's debug information shows it: two `u32`s, no cell (#321).
LAYOUTS = {
    **{
        name: CrateType(payloads=frozenset(), parts=frozenset({("prim", "u32")}))
        for name in ("Lap", "D")
    },
    "S": CrateType(
        payloads=frozenset(), parts=frozenset({("std", "string::String", ()), ("prim", "u32")})
    ),
    "Holder": CrateType(
        payloads=frozenset(), parts=frozenset({("type", "D", ()), ("prim", "u32")})
    ),
    "Wrap": CrateType(
        payloads=frozenset(),
        parts=frozenset({("std", "vec::Vec", (("type", "D", ()),)), ("prim", "u32")}),
    ),
}
# Where the toolchain's own `println!` is written, as the dump of `D`'s `drop` gives it.
STD_MACROS = "/rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs"


def body_of(text):
    """The body of ``text``, as matched to its IR: no type parameters, in a crate that names
    no `std`, `core` or `alloc` of its own."""
    body = parse_body(text)
    body.generics = ()
    body.std_paths = frozenset({"std", "core", "alloc"})
    body.layouts = TypeLayouts(None, body.closure_captures())
    return body


def place(body, statement):
    """Where the one statement starting ``statement`` is: (block, index)."""
    found = [
        (number, index)
        for number, block in body.blocks.items()
        for index, (text, _, _) in enumerate(block.statements)
        if text.startswith(statement)
    ]
    assert len(found) == 1, (statement, found)
    return found[0]


def owned_of(text, name, statement, unsafe=None, source=None, parent="units", extra=()):
    """OwnedLocals over ``text``, the function ``name`` of ``source`` (``SOURCE``'s by
    default), whose stop is only ever before its statement starting ``statement``."""
    body = parse_body(text)
    blocks = {}
    for number, block in body.blocks.items():
        lines = {at for _, file, at in block.statements if file == "src/main.rs" and at}
        blocks["start" if number == 0 else f"bb{number}"] = lines
    declared = min(at for _, file, at in body.blocks[0].statements if file == "src/main.rs")
    function = IrFunction(name, parent, "src/main.rs", blocks, {}, declared - 1)
    texts = {"src/main.rs": source or SOURCE}
    bodies = [body, *(parse_body(each) for each in extra)]
    owned = OwnedLocals(bodies, {HASH: function}, ("src/main.rs",), texts=texts)
    owned.unsafe_code = unsafe
    owned.interior = LAYOUTS
    owned.std_macros = frozenset({STD_MACROS})
    position = place(body, statement)
    owned._positions = lambda site, line, facts, block=None: [position]
    for check in ("copy_hazards", "unread_after"):
        judge = getattr(owned, check)
        setattr(
            owned,
            check,
            lambda *args, judge=judge, **asked: judge(*args, **{**asked, "breakpoint": False}),
        )
    return owned


def why(text, name, line, statement, variable, projection=(), unsafe=None, source=None, extra=()):
    """Why a change to ``variable`` (the part ``projection`` of it) is refused at a stop on
    ``line`` of the function ``name`` of ``text``, before its statement starting
    ``statement`` (``mid_statement``'s reason): (the reason, its row's short form), or (None,
    None) where it is allowed."""
    owned = owned_of(text, name, statement, unsafe, source, extra=extra)
    session = object.__new__(RustSession)
    session.owned = owned
    session.prepared = SimpleNamespace(source_root="/w")
    session._optimized = lambda: False
    session._stop_block = lambda hashed, frame: None
    session._projection = lambda *args: projection
    session._frame_scope = lambda frame, variable: None
    session._settable_names = lambda source, line: []
    session._inlined_below = lambda frame: None
    frame = {"name": f"units::{name}::{HASH}", "line": line, "column": 5}
    frame["source"] = {"path": "/w/src/main.rs"}
    steps = ["start"] if projection else None
    found = session.mid_statement({"name": variable}, frame, "breakpoint", steps=steps)
    return (None, None) if found is None else (str(found), found.row)


SEEN = (
    "so a change to `{}` may never be seen: Peeky proves no read of the changed part first (a "
    "reference lent to a call, or `{{:?}}`, is not one)"
)


def stop(text, line, statement):
    """The place before the statement of ``text`` on ``line`` that starts ``statement``."""
    body = parse_body(text)
    found = [
        text
        for block in body.blocks.values()
        for text, file, at in block.statements
        if at == line and text.startswith(statement)
    ]
    assert found, (line, statement)
    return found[0]


def test_a_copy_used_after_the_local_is_replaced_still_keeps_the_old_value():
    # The issue: `u.push(y);` (line 10), `let t = n;` (11), `y = t;` (12), then `y + u[0]`
    # reads `u`. Line 12 replaces `y` before the program uses `u`: the copy rule looked for
    # a use first, so `y` was settable at lines 11 and 12, and a change there was lost.
    body = body_of(PUSHED)
    at_11 = place(body, "_9 = copy _1;")
    assert 5 in body.direct_copies_at(2, at_11)
    assert body.copy_undoes(5, 2, at_11)
    for line, statement in ((11, "StorageLive(_9)"), (12, "StorageLive(_10)")):
        assert why(PUSHED, "pushed", line, stop(PUSHED, line, statement), "y") == (
            "the program's own `u` (at lines 9 and 10) may hold a copy of it that it goes on "
            "with, " + SEEN.format("y"),
            "the program may hold a copy (lines 9 and 10)",
        ), line
    # Before the push, the change reaches it.
    assert why(PUSHED, "pushed", 10, stop(PUSHED, 10, "StorageLive(_6)"), "y") == (None, None)
    # A plain copy: `let z = x;` (18), `let k = n;` (19), `x = k;` (20), then `x + z`.
    assert why(REPLACED, "replaced", 19, stop(REPLACED, 19, "StorageLive("), "x") == (
        "the program has already copied its value into `z` (at line 18) and goes on with that "
        "copy, " + SEEN.format("x"),
        "copied into `z` (line 18)",
    )


def test_a_struct_pushed_then_written_is_the_vecs_copy():
    # The laps shape: `laps.push(lap);` (37), `let finish = lap.end;` (38), `lap.start =
    # finish;` (39), and `lap` ends before `laps` is read. `lap.start` is never read again.
    start, end = (("field", 0),), (("field", 1),)
    body = body_of(LAPPED)
    at_38 = place(body, "_11 = copy (_3.1: u32);")
    assert body.copy_undoes(2, 3, at_38, start)
    for line, statement in ((38, "StorageLive(_11)"), (39, "StorageLive(_12)")):
        assert why(LAPPED, "lapped", line, stop(LAPPED, line, statement), "lap", start) == (
            "the program's own `laps` (at lines 35 and 37) may hold a copy of it that it goes on "
            "with, " + SEEN.format("lap"),
            "the program may hold a copy (lines 35 and 37)",
        ), line
    # Line 38 reads `lap.end`: a change to it is seen.
    assert not body.copy_undoes(2, 3, at_38, end)
    assert why(LAPPED, "lapped", 38, stop(LAPPED, 38, "StorageLive(_11)"), "lap", end) == (
        None,
        None,
    )


def test_an_argument_ends_where_the_function_returns():
    # `let z = x;` (60), `let k = n2();` (61), `z + k`: the argument `x` has no end of its
    # own, but it is never read again before the function returns.
    body = body_of(PARAM)
    assert body.copy_undoes(2, 1, place(body, "_3 = n2()"))
    assert why(PARAM, "param", 61, stop(PARAM, 61, "StorageLive("), "x") == (
        "the program has already copied its value into `z` (at line 60) and goes on with that "
        "copy, " + SEEN.format("x"),
        "copied into `z` (line 60)",
    )


def test_a_local_read_again_before_it_is_replaced_stays_settable():
    # `w.push(x);` (27), `let k = n;` (28), `let s = x + k;` (29), `x = s;` (30): line 29
    # reads `x`, so a change at line 28 is seen, whatever `w` holds.
    body = body_of(LATER)
    at_28 = place(body, stop(LATER, 28, "_"))
    assert 5 in body.direct_copies_at(2, at_28)
    assert not body.copy_undoes(5, 2, at_28)
    assert body.unread_ends(2, at_28) is None
    assert why(LATER, "later", 28, stop(LATER, 28, "StorageLive("), "x") == (None, None)


def test_a_value_never_read_again_is_refused_saying_where_it_ends():
    # `let mut total = n + 1;` (44), `let k = n * 2;` (45), `total = k + 3;` (46): no path
    # from line 45, nor from line 46's start, reads `total` before line 46 assigns it. Each
    # overflow check on the way may panic first (review 2), and the refusal says so.
    for line, statement in ((45, "StorageLive(_5)"), (46, "StorageLive(_8)")):
        assert why(UNREAD, "unread", line, stop(UNREAD, line, statement), "total") == (
            "the program does not read it after this stop, so a change would never be seen: it "
            "is overwritten at line 46 first, unless the program panics or exits before that",
            "not read after the stop (overwritten at line 46, or the program panics or exits)",
        ), line
    body = body_of(UNREAD)
    write = place(body, "_2 = move (_9.0: u32);")
    checks = {
        ("stop", (number, len(block.statements) - 1))
        for number, block in body.blocks.items()
        if number in (1, 2)
    }
    assert body.unread_ends(2, place(body, "_6 = copy _1;")) == {("write", write), *checks}
    # Past line 46, line 56 reads it.
    assert body.unread_ends(2, place(body, "_10 = const 0_u32;")) is None
    # `a` (49) is read through `r` on line 53: not while `r` may still be used.
    owned = owned_of(UNREAD, "unread", "_20 = copy (*_19);")
    assert {kind for kind, _ in body.unread_ends(14, place(body, "_20 = copy (*_19);"))} == {
        "scope",
        "stop",
    }
    assert owned.unread_after(HASH, "a", 53, "/w/src/main.rs") is None
    assert body.unread_ends(14, place(body, "_19 = &_14;")) is None
    # On line 54, `r` is done with: `a` goes out of scope at line 55, unread.
    owned = owned_of(UNREAD, "unread", "_22 = AddWithOverflow")
    assert owned.unread_after(HASH, "a", 54, "/w/src/main.rs") == [
        ("scope", "src/main.rs", 55),
        ("stop", "", 0),
    ]
    # Where the crate has unsafe code, a pointer may be kept where Peeky can't see.
    owned = owned_of(UNREAD, "unread", "_22 = AddWithOverflow", unsafe="the crate has unsafe code")
    assert owned.unread_after(HASH, "a", 54, "/w/src/main.rs") is None


def test_what_may_read_a_part_of_a_local_is_a_read():
    body = body_of(UNREAD)
    body.types[1] = "Lap"
    body.types[3] = "std::string::String"
    field, other = (("field", 0),), (("field", 1),)
    index = (("index", 2),)
    for text, local, projection, expected in (
        ("StorageDead(_1);", 1, (), "scope"),
        ("Deinit(_1);", 1, (), "write"),
        ("PlaceMention(_1);", 1, (), None),
        ("_5 = &_1;", 1, other, "read"),
        ("_5 = &raw const (_1.1: u32);", 1, field, "read"),
        ("_5 = copy (_1.1: u32);", 1, field, None),
        ("_5 = copy (_1.0: u32);", 1, field, "read"),
        ("_5 = copy _1;", 1, field, "read"),
        ("_5 = copy (*_1);", 1, (), "read"),
        ("_5 = Add(copy (_1.1: u32), const 1_u32);", 1, field, None),
        ("(_1.0: u32) = const 3_u32;", 1, field, "write"),
        ("(_1.0: u32) = const 3_u32;", 1, other, None),
        ("(_1.1: u32) = copy (_1.0: u32);", 1, field, "read"),
        ("(_1.1: u32) = copy (_1.0: u32);", 1, other, "write"),
        ("_1 = Lap { start: const 0_u32, end: copy (_1.1: u32) };", 1, field, "write"),
        ("_1 = Lap { start: const 0_u32, end: copy (_1.1: u32) };", 1, other, "read"),
        ("_1[_5] = const 0_u32;", 1, index, None),
        ("switchInt(copy _1) -> [0: bb1, otherwise: bb2];", 1, (), "read"),
        ("_5 = discriminant(_1);", 1, (), "read"),
        ("_5 = f(copy (_1.1: u32)) -> [return: bb1, unwind continue];", 1, field, None),
        ("_5 = f(move _1) -> [return: bb1, unwind continue];", 1, field, "read"),
        # A String's drop frees what it owns; a crate type's may run code that reads it.
        ("drop(_3) -> [return: bb1, unwind: bb2];", 3, (), None),
        # Not known which `Drop` impls the crate has: one may read it.
        ("drop(_1) -> [return: bb1, unwind: bb2];", 1, field, "drop"),
        ("drop((*_3)) -> [return: bb1, unwind: bb2];", 3, (), "read"),
        ("_5 = copy _7;", 1, (), None),
    ):
        assert body._unread_access(text, local, projection) == expected, (text, projection)


def test_a_refusal_of_a_value_never_read_again_hints_where_to_change_it():
    from peeky.errors import _mutation_hint

    hint = (
        "Change it where the program still reads it: after the line that overwrites it, or "
        "before its last read (a stop on that line, or `--set-at` there)."
    )
    reason, _ = why(UNREAD, "unread", 45, stop(UNREAD, 45, "StorageLive(_5)"), "total")
    for message in (
        f"'total' cannot be changed here: {reason}. Change it on a later line, after the "
        "program overwrites it, or on an earlier line, before the program's last read of it, "
        "where `inspect` marks it [settable]",
        "`total` cannot be changed at src/main.rs:45, at any stop of this build: in "
        "units::unread, the program does not read `total` after any stop of line 45, so a "
        "change there would never be seen: it is overwritten at line 46 first",
    ):
        assert _mutation_hint(message) == hint, message
    # Java's own refusal of a local no path reads keeps its hint.
    java = (
        "x cannot be changed at this stop: the program does not read it after this stop (a "
        "lambda or inner class that captured it before keeps its own copy; javac compiles a "
        "constant local into its uses), so a change would never be seen"
    )
    assert _mutation_hint(java) != hint


# Review of PR #334: a type's own `Drop`, frame exits other than a return, and where each path
# ends. rustc 1.96.0's MIR of this crate, as above.
REVIEW_SOURCE = """use std::hint::black_box;

struct D(u32, u32);

impl Drop for D {
    fn drop(&mut self) {
        println!("D {}", self.0);
    }
}

#[inline(never)]
fn other() -> u32 {
    1
}

fn scoped(n: u32) -> u32 {
    let d = D(n + 1, n);
    let z = d.0;
    let k = other();
    z + k
}

struct S {
    name: String,
    n: u32,
}

fn second(n: u32) -> u32 {
    let d = D(n, n + 1);
    let z = d.1;
    let k = other();
    z + k
}

struct Holder {
    d: D,
    n: u32,
}

fn held(n: u32) -> u32 {
    let h = Holder { d: D(n, n), n: n + 1 };
    let z = h.n;
    let k = other();
    z + k
}

struct Wrap {
    v: Vec<D>,
    n: u32,
}

fn wrapped(n: u32) -> u32 {
    let w = Wrap { v: vec![D(n, n)], n: n + 1 };
    let z = w.n;
    let k = other();
    z + k
}

fn named(n: u32) -> u32 {
    let s = S { name: n.to_string(), n: n + 1 };
    let z = s.n;
    let k = other();
    z + k
}

fn taken(d: D) -> u32 {
    let z = d.0;
    let k = other();
    z + k
}

fn panicked(mut x: u32) -> u32 {
    black_box(&mut x);
    let z = x;
    let k = other();
    black_box(z + k);
    panic!("stop");
}

fn unwound(n: u32) -> u32 {
    let label = n.to_string();
    let mut x = n + 1;
    black_box(&mut x);
    let z = x;
    let k = other();
    black_box(z + k);
    black_box(&label);
    panic!("stop");
}

fn pointed(n: u32) -> u32 {
    let x = n + 1;
    let r = &x;
    let z = x;
    let k = other();
    black_box(z + k);
    black_box(r);
    std::process::exit(0);
}

async fn exited(n: u32) {
    let mut x = n + 1;
    black_box(&mut x);
    let z = x;
    let k = other();
    black_box(z + k);
    std::process::exit(0);
}

#[inline(never)]
fn record(z: u32) {
    black_box(z);
}

#[inline(never)]
fn may_panic(k: u32) {
    if k > 100 {
        panic!("too big");
    }
}

#[inline(never)]
fn look(x: &u32) -> u32 {
    *x
}

fn caught(n: u32) -> u32 {
    let mut x = n + 1;
    black_box(&mut x);
    let z = x;
    let k = other();
    record(z);
    may_panic(k);
    x
}

fn checked(n: u32) -> u32 {
    let mut x = n + 1;
    black_box(&mut x);
    let z = x;
    let k = other();
    let w = z * 10;
    w + x + k
}

fn printed(n: u32) -> u32 {
    let mut x = n + 1;
    black_box(&mut x);
    let z = x;
    let k = other();
    println!("{z} {x}");
    k
}

fn lent(n: u32) -> u32 {
    let mut x = n + 1;
    black_box(&mut x);
    let z = x;
    let k = other();
    let seen = look(&x);
    record(z);
    may_panic(k);
    seen + x
}

fn branch(n: u32) -> u32 {
    let mut total = n + 1;
    let k = other();
    if n > 100 {
        panic!("too many");
    }
    total = k + n;
    total
}

fn spin(n: u32) -> u32 {
    let mut total = n + 1;
    let k = other();
    if n > 100 {
        loop {
            black_box(k);
        }
    }
    total = k + n;
    total
}

fn cleaned(n: u32) -> u32 {
    let mut total = n + 1;
    let label = n.to_string();
    let k = other();
    total = k + n;
    label.len() as u32 + total
}

fn main() {
    let n = std::env::args().count() as u32;
    let fut = exited(n);
    black_box(fut);
    println!(
        "{} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {}",
        scoped(n),
        taken(D(n, n)),
        named(n),
        second(n),
        held(n),
        wrapped(n),
        branch(n),
        spin(n),
        cleaned(n),
        caught(n),
        checked(n),
        printed(n),
        lent(n),
        pointed(n),
        panicked(n),
        unwound(n)
    );
}
"""


SCOPED = """// MIR for `scoped` after PreCodegen

fn scoped(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:16:11: 16:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:16:22: 16:25
    let _2: D;                           // in scope 0 at src/main.rs:17:9: 17:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:17:15: 17:20
    let mut _4: u32;                     // in scope 0 at src/main.rs:17:15: 17:16
    let mut _5: (u32, bool);             // in scope 0 at src/main.rs:17:15: 17:20
    let mut _6: u32;                     // in scope 0 at src/main.rs:17:22: 17:23
    let mut _9: u32;                     // in scope 0 at src/main.rs:20:5: 20:6
    let mut _10: u32;                    // in scope 0 at src/main.rs:20:9: 20:10
    let mut _11: (u32, bool);            // in scope 0 at src/main.rs:20:5: 20:10
    scope 1 {
        debug d => _2;                   // in scope 1 at src/main.rs:17:9: 17:10
        let _7: u32;                     // in scope 1 at src/main.rs:18:9: 18:10
        scope 2 {
            debug z => _7;               // in scope 2 at src/main.rs:18:9: 18:10
            let _8: u32;                 // in scope 2 at src/main.rs:19:9: 19:10
            scope 3 {
                debug k => _8;           // in scope 3 at src/main.rs:19:9: 19:10
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:17:9: 17:10
        StorageLive(_3);                 // scope 0 at src/main.rs:17:15: 17:20
        StorageLive(_4);                 // scope 0 at src/main.rs:17:15: 17:16
        _4 = copy _1;                    // scope 0 at src/main.rs:17:15: 17:16
        _5 = AddWithOverflow(copy _4, const 1_u32); // scope 0 at src/main.rs:17:15: 17:20
        assert(!move (_5.1: bool), "attempt to compute `{} + {}`, which would overflow", move _4, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:17:15: 17:20
    }
    bb1: {
        _3 = move (_5.0: u32);           // scope 0 at src/main.rs:17:15: 17:20
        StorageDead(_4);                 // scope 0 at src/main.rs:17:19: 17:20
        StorageLive(_6);                 // scope 0 at src/main.rs:17:22: 17:23
        _6 = copy _1;                    // scope 0 at src/main.rs:17:22: 17:23
        _2 = D(move _3, move _6);        // scope 0 at src/main.rs:17:13: 17:24
        StorageDead(_6);                 // scope 0 at src/main.rs:17:23: 17:24
        StorageDead(_3);                 // scope 0 at src/main.rs:17:23: 17:24
        StorageLive(_7);                 // scope 1 at src/main.rs:18:9: 18:10
        _7 = copy (_2.0: u32);           // scope 1 at src/main.rs:18:13: 18:16
        StorageLive(_8);                 // scope 2 at src/main.rs:19:9: 19:10
        _8 = other() -> [return: bb2, unwind: bb5]; // scope 2 at src/main.rs:19:13: 19:20
    }
    bb2: {
        StorageLive(_9);                 // scope 3 at src/main.rs:20:5: 20:6
        _9 = copy _7;                    // scope 3 at src/main.rs:20:5: 20:6
        StorageLive(_10);                // scope 3 at src/main.rs:20:9: 20:10
        _10 = copy _8;                   // scope 3 at src/main.rs:20:9: 20:10
        _11 = AddWithOverflow(copy _9, copy _10); // scope 3 at src/main.rs:20:5: 20:10
        assert(!move (_11.1: bool), "attempt to compute `{} + {}`, which would overflow", move _9, move _10) -> [success: bb3, unwind: bb5]; // scope 3 at src/main.rs:20:5: 20:10
    }
    bb3: {
        _0 = move (_11.0: u32);          // scope 3 at src/main.rs:20:5: 20:10
        StorageDead(_10);                // scope 3 at src/main.rs:20:9: 20:10
        StorageDead(_9);                 // scope 3 at src/main.rs:20:9: 20:10
        StorageDead(_8);                 // scope 2 at src/main.rs:21:1: 21:2
        StorageDead(_7);                 // scope 1 at src/main.rs:21:1: 21:2
        drop(_2) -> [return: bb4, unwind: bb6]; // scope 0 at src/main.rs:21:1: 21:2
    }
    bb4: {
        StorageDead(_2);                 // scope 0 at src/main.rs:21:1: 21:2
        return;                          // scope 0 at src/main.rs:21:2: 21:2
    }
    bb5 (cleanup): {
        drop(_2) -> [return: bb6, unwind terminate(cleanup)]; // scope 0 at src/main.rs:21:1: 21:2
    }
    bb6 (cleanup): {
        resume;                          // scope 0 at src/main.rs:16:1: 21:2
    }
}
"""

SECOND = """// MIR for `second` after PreCodegen

fn second(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:28:11: 28:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:28:22: 28:25
    let _2: D;                           // in scope 0 at src/main.rs:29:9: 29:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:29:15: 29:16
    let mut _4: u32;                     // in scope 0 at src/main.rs:29:18: 29:23
    let mut _5: u32;                     // in scope 0 at src/main.rs:29:18: 29:19
    let mut _6: (u32, bool);             // in scope 0 at src/main.rs:29:18: 29:23
    let mut _9: u32;                     // in scope 0 at src/main.rs:32:5: 32:6
    let mut _10: u32;                    // in scope 0 at src/main.rs:32:9: 32:10
    let mut _11: (u32, bool);            // in scope 0 at src/main.rs:32:5: 32:10
    scope 1 {
        debug d => _2;                   // in scope 1 at src/main.rs:29:9: 29:10
        let _7: u32;                     // in scope 1 at src/main.rs:30:9: 30:10
        scope 2 {
            debug z => _7;               // in scope 2 at src/main.rs:30:9: 30:10
            let _8: u32;                 // in scope 2 at src/main.rs:31:9: 31:10
            scope 3 {
                debug k => _8;           // in scope 3 at src/main.rs:31:9: 31:10
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:29:9: 29:10
        StorageLive(_3);                 // scope 0 at src/main.rs:29:15: 29:16
        _3 = copy _1;                    // scope 0 at src/main.rs:29:15: 29:16
        StorageLive(_4);                 // scope 0 at src/main.rs:29:18: 29:23
        StorageLive(_5);                 // scope 0 at src/main.rs:29:18: 29:19
        _5 = copy _1;                    // scope 0 at src/main.rs:29:18: 29:19
        _6 = AddWithOverflow(copy _5, const 1_u32); // scope 0 at src/main.rs:29:18: 29:23
        assert(!move (_6.1: bool), "attempt to compute `{} + {}`, which would overflow", move _5, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:29:18: 29:23
    }
    bb1: {
        _4 = move (_6.0: u32);           // scope 0 at src/main.rs:29:18: 29:23
        StorageDead(_5);                 // scope 0 at src/main.rs:29:22: 29:23
        _2 = D(move _3, move _4);        // scope 0 at src/main.rs:29:13: 29:24
        StorageDead(_4);                 // scope 0 at src/main.rs:29:23: 29:24
        StorageDead(_3);                 // scope 0 at src/main.rs:29:23: 29:24
        StorageLive(_7);                 // scope 1 at src/main.rs:30:9: 30:10
        _7 = copy (_2.1: u32);           // scope 1 at src/main.rs:30:13: 30:16
        StorageLive(_8);                 // scope 2 at src/main.rs:31:9: 31:10
        _8 = other() -> [return: bb2, unwind: bb5]; // scope 2 at src/main.rs:31:13: 31:20
    }
    bb2: {
        StorageLive(_9);                 // scope 3 at src/main.rs:32:5: 32:6
        _9 = copy _7;                    // scope 3 at src/main.rs:32:5: 32:6
        StorageLive(_10);                // scope 3 at src/main.rs:32:9: 32:10
        _10 = copy _8;                   // scope 3 at src/main.rs:32:9: 32:10
        _11 = AddWithOverflow(copy _9, copy _10); // scope 3 at src/main.rs:32:5: 32:10
        assert(!move (_11.1: bool), "attempt to compute `{} + {}`, which would overflow", move _9, move _10) -> [success: bb3, unwind: bb5]; // scope 3 at src/main.rs:32:5: 32:10
    }
    bb3: {
        _0 = move (_11.0: u32);          // scope 3 at src/main.rs:32:5: 32:10
        StorageDead(_10);                // scope 3 at src/main.rs:32:9: 32:10
        StorageDead(_9);                 // scope 3 at src/main.rs:32:9: 32:10
        StorageDead(_8);                 // scope 2 at src/main.rs:33:1: 33:2
        StorageDead(_7);                 // scope 1 at src/main.rs:33:1: 33:2
        drop(_2) -> [return: bb4, unwind: bb6]; // scope 0 at src/main.rs:33:1: 33:2
    }
    bb4: {
        StorageDead(_2);                 // scope 0 at src/main.rs:33:1: 33:2
        return;                          // scope 0 at src/main.rs:33:2: 33:2
    }
    bb5 (cleanup): {
        drop(_2) -> [return: bb6, unwind terminate(cleanup)]; // scope 0 at src/main.rs:33:1: 33:2
    }
    bb6 (cleanup): {
        resume;                          // scope 0 at src/main.rs:28:1: 33:2
    }
}
"""

HELD = """// MIR for `held` after PreCodegen

fn held(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:40:9: 40:10
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:40:20: 40:23
    let _2: Holder;                      // in scope 0 at src/main.rs:41:9: 41:10
    let mut _3: D;                       // in scope 0 at src/main.rs:41:25: 41:32
    let mut _4: u32;                     // in scope 0 at src/main.rs:41:27: 41:28
    let mut _5: u32;                     // in scope 0 at src/main.rs:41:30: 41:31
    let mut _6: u32;                     // in scope 0 at src/main.rs:41:37: 41:42
    let mut _7: u32;                     // in scope 0 at src/main.rs:41:37: 41:38
    let mut _8: (u32, bool);             // in scope 0 at src/main.rs:41:37: 41:42
    let mut _11: u32;                    // in scope 0 at src/main.rs:44:5: 44:6
    let mut _12: u32;                    // in scope 0 at src/main.rs:44:9: 44:10
    let mut _13: (u32, bool);            // in scope 0 at src/main.rs:44:5: 44:10
    scope 1 {
        debug h => _2;                   // in scope 1 at src/main.rs:41:9: 41:10
        let _9: u32;                     // in scope 1 at src/main.rs:42:9: 42:10
        scope 2 {
            debug z => _9;               // in scope 2 at src/main.rs:42:9: 42:10
            let _10: u32;                // in scope 2 at src/main.rs:43:9: 43:10
            scope 3 {
                debug k => _10;          // in scope 3 at src/main.rs:43:9: 43:10
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:41:9: 41:10
        StorageLive(_3);                 // scope 0 at src/main.rs:41:25: 41:32
        StorageLive(_4);                 // scope 0 at src/main.rs:41:27: 41:28
        _4 = copy _1;                    // scope 0 at src/main.rs:41:27: 41:28
        StorageLive(_5);                 // scope 0 at src/main.rs:41:30: 41:31
        _5 = copy _1;                    // scope 0 at src/main.rs:41:30: 41:31
        _3 = D(move _4, move _5);        // scope 0 at src/main.rs:41:25: 41:32
        StorageDead(_5);                 // scope 0 at src/main.rs:41:31: 41:32
        StorageDead(_4);                 // scope 0 at src/main.rs:41:31: 41:32
        StorageLive(_6);                 // scope 0 at src/main.rs:41:37: 41:42
        StorageLive(_7);                 // scope 0 at src/main.rs:41:37: 41:38
        _7 = copy _1;                    // scope 0 at src/main.rs:41:37: 41:38
        _8 = AddWithOverflow(copy _7, const 1_u32); // scope 0 at src/main.rs:41:37: 41:42
        assert(!move (_8.1: bool), "attempt to compute `{} + {}`, which would overflow", move _7, const 1_u32) -> [success: bb1, unwind: bb6]; // scope 0 at src/main.rs:41:37: 41:42
    }
    bb1: {
        _6 = move (_8.0: u32);           // scope 0 at src/main.rs:41:37: 41:42
        StorageDead(_7);                 // scope 0 at src/main.rs:41:41: 41:42
        _2 = Holder { d: move _3, n: move _6 }; // scope 0 at src/main.rs:41:13: 41:44
        StorageDead(_6);                 // scope 0 at src/main.rs:41:43: 41:44
        StorageDead(_3);                 // scope 0 at src/main.rs:41:43: 41:44
        StorageLive(_9);                 // scope 1 at src/main.rs:42:9: 42:10
        _9 = copy (_2.1: u32);           // scope 1 at src/main.rs:42:13: 42:16
        StorageLive(_10);                // scope 2 at src/main.rs:43:9: 43:10
        _10 = other() -> [return: bb2, unwind: bb5]; // scope 2 at src/main.rs:43:13: 43:20
    }
    bb2: {
        StorageLive(_11);                // scope 3 at src/main.rs:44:5: 44:6
        _11 = copy _9;                   // scope 3 at src/main.rs:44:5: 44:6
        StorageLive(_12);                // scope 3 at src/main.rs:44:9: 44:10
        _12 = copy _10;                  // scope 3 at src/main.rs:44:9: 44:10
        _13 = AddWithOverflow(copy _11, copy _12); // scope 3 at src/main.rs:44:5: 44:10
        assert(!move (_13.1: bool), "attempt to compute `{} + {}`, which would overflow", move _11, move _12) -> [success: bb3, unwind: bb5]; // scope 3 at src/main.rs:44:5: 44:10
    }
    bb3: {
        _0 = move (_13.0: u32);          // scope 3 at src/main.rs:44:5: 44:10
        StorageDead(_12);                // scope 3 at src/main.rs:44:9: 44:10
        StorageDead(_11);                // scope 3 at src/main.rs:44:9: 44:10
        StorageDead(_10);                // scope 2 at src/main.rs:45:1: 45:2
        StorageDead(_9);                 // scope 1 at src/main.rs:45:1: 45:2
        drop(_2) -> [return: bb4, unwind: bb7]; // scope 0 at src/main.rs:45:1: 45:2
    }
    bb4: {
        StorageDead(_2);                 // scope 0 at src/main.rs:45:1: 45:2
        return;                          // scope 0 at src/main.rs:45:2: 45:2
    }
    bb5 (cleanup): {
        drop(_2) -> [return: bb7, unwind terminate(cleanup)]; // scope 0 at src/main.rs:45:1: 45:2
    }
    bb6 (cleanup): {
        drop(_3) -> [return: bb7, unwind terminate(cleanup)]; // scope 0 at src/main.rs:41:43: 41:44
    }
    bb7 (cleanup): {
        resume;                          // scope 0 at src/main.rs:40:1: 45:2
    }
}
"""

WRAPPED = """// MIR for `wrapped` after PreCodegen

fn wrapped(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:52:12: 52:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:52:23: 52:26
    let _2: Wrap;                        // in scope 0 at src/main.rs:53:9: 53:10
    let mut _3: std::vec::Vec<D>;        // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:58:9: 60:10
    let mut _4: std::boxed::Box<std::mem::MaybeUninit<[D; 1]>>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:13: 59:96
    let mut _5: std::boxed::Box<std::mem::MaybeUninit<[D; 1]>>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:52: 59:84
    let mut _6: D;                       // in scope 0 at src/main.rs:53:28: 53:35
    let mut _7: u32;                     // in scope 0 at src/main.rs:53:30: 53:31
    let mut _8: u32;                     // in scope 0 at src/main.rs:53:33: 53:34
    let mut _9: u32;                     // in scope 0 at src/main.rs:53:41: 53:46
    let mut _10: u32;                    // in scope 0 at src/main.rs:53:41: 53:42
    let mut _11: (u32, bool);            // in scope 0 at src/main.rs:53:41: 53:46
    let mut _14: u32;                    // in scope 0 at src/main.rs:56:5: 56:6
    let mut _15: u32;                    // in scope 0 at src/main.rs:56:9: 56:10
    let mut _16: (u32, bool);            // in scope 0 at src/main.rs:56:5: 56:10
    let mut _17: *const std::mem::MaybeUninit<[D; 1]>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:52: 59:84
    let mut _18: *const ();              // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
    let mut _19: usize;                  // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
    let mut _20: usize;                  // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
    let mut _21: usize;                  // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
    let mut _22: bool;                   // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
    let mut _23: *const ();              // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
    let mut _24: usize;                  // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
    let mut _25: bool;                   // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
    let mut _26: bool;                   // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
    let mut _27: bool;                   // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
    let mut _28: bool;                   // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
    scope 1 {
        debug w => _2;                   // in scope 1 at src/main.rs:53:9: 53:10
        let _12: u32;                    // in scope 1 at src/main.rs:54:9: 54:10
        scope 2 {
            debug z => _12;              // in scope 2 at src/main.rs:54:9: 54:10
            let _13: u32;                // in scope 2 at src/main.rs:55:9: 55:10
            scope 3 {
                debug k => _13;          // in scope 3 at src/main.rs:55:9: 55:10
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:53:9: 53:10
        StorageLive(_3);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:58:9: 60:10
        StorageLive(_4);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:13: 59:96
        StorageLive(_5);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:52: 59:84
        _5 = Box::<[D; 1]>::new_uninit() -> [return: bb1, unwind continue]; // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:52: 59:84
    }
    bb1: {
        StorageLive(_6);                 // scope 0 at src/main.rs:53:28: 53:35
        StorageLive(_7);                 // scope 0 at src/main.rs:53:30: 53:31
        _7 = copy _1;                    // scope 0 at src/main.rs:53:30: 53:31
        StorageLive(_8);                 // scope 0 at src/main.rs:53:33: 53:34
        _8 = copy _1;                    // scope 0 at src/main.rs:53:33: 53:34
        _6 = D(move _7, move _8);        // scope 0 at src/main.rs:53:28: 53:35
        StorageDead(_8);                 // scope 0 at src/main.rs:53:34: 53:35
        StorageDead(_7);                 // scope 0 at src/main.rs:53:34: 53:35
        _17 = copy ((_5.0: std::ptr::Unique<std::mem::MaybeUninit<[D; 1]>>).0: std::ptr::NonNull<std::mem::MaybeUninit<[D; 1]>>) as *const std::mem::MaybeUninit<[D; 1]> (Transmute); // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
        _18 = copy _17 as *const () (PtrToPtr); // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
        _19 = copy _18 as usize (Transmute); // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
        _20 = Sub(const <std::mem::MaybeUninit<[D; 1]> as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
        _21 = BitAnd(copy _19, copy _20); // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
        _22 = Eq(copy _21, const 0_usize); // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
        assert(copy _22, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <std::mem::MaybeUninit<[D; 1]> as std::mem::SizedTypeProperties>::ALIGN, copy _19) -> [success: bb10, unwind unreachable]; // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
    }
    bb2: {
        StorageDead(_4);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:60:9: 60:10
        StorageLive(_9);                 // scope 0 at src/main.rs:53:41: 53:46
        StorageLive(_10);                // scope 0 at src/main.rs:53:41: 53:42
        _10 = copy _1;                   // scope 0 at src/main.rs:53:41: 53:42
        _11 = AddWithOverflow(copy _10, const 1_u32); // scope 0 at src/main.rs:53:41: 53:46
        assert(!move (_11.1: bool), "attempt to compute `{} + {}`, which would overflow", move _10, const 1_u32) -> [success: bb3, unwind: bb8]; // scope 0 at src/main.rs:53:41: 53:46
    }
    bb3: {
        _9 = move (_11.0: u32);          // scope 0 at src/main.rs:53:41: 53:46
        StorageDead(_10);                // scope 0 at src/main.rs:53:45: 53:46
        _2 = Wrap { v: move _3, n: move _9 }; // scope 0 at src/main.rs:53:13: 53:48
        StorageDead(_9);                 // scope 0 at src/main.rs:53:47: 53:48
        StorageDead(_3);                 // scope 0 at src/main.rs:53:47: 53:48
        StorageLive(_12);                // scope 1 at src/main.rs:54:9: 54:10
        _12 = copy (_2.1: u32);          // scope 1 at src/main.rs:54:13: 54:16
        StorageLive(_13);                // scope 2 at src/main.rs:55:9: 55:10
        _13 = other() -> [return: bb4, unwind: bb7]; // scope 2 at src/main.rs:55:13: 55:20
    }
    bb4: {
        StorageLive(_14);                // scope 3 at src/main.rs:56:5: 56:6
        _14 = copy _12;                  // scope 3 at src/main.rs:56:5: 56:6
        StorageLive(_15);                // scope 3 at src/main.rs:56:9: 56:10
        _15 = copy _13;                  // scope 3 at src/main.rs:56:9: 56:10
        _16 = AddWithOverflow(copy _14, copy _15); // scope 3 at src/main.rs:56:5: 56:10
        assert(!move (_16.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _15) -> [success: bb5, unwind: bb7]; // scope 3 at src/main.rs:56:5: 56:10
    }
    bb5: {
        _0 = move (_16.0: u32);          // scope 3 at src/main.rs:56:5: 56:10
        StorageDead(_15);                // scope 3 at src/main.rs:56:9: 56:10
        StorageDead(_14);                // scope 3 at src/main.rs:56:9: 56:10
        StorageDead(_13);                // scope 2 at src/main.rs:57:1: 57:2
        StorageDead(_12);                // scope 1 at src/main.rs:57:1: 57:2
        drop(_2) -> [return: bb6, unwind: bb9]; // scope 0 at src/main.rs:57:1: 57:2
    }
    bb6: {
        StorageDead(_2);                 // scope 0 at src/main.rs:57:1: 57:2
        return;                          // scope 0 at src/main.rs:57:2: 57:2
    }
    bb7 (cleanup): {
        drop(_2) -> [return: bb9, unwind terminate(cleanup)]; // scope 0 at src/main.rs:57:1: 57:2
    }
    bb8 (cleanup): {
        drop(_3) -> [return: bb9, unwind terminate(cleanup)]; // scope 0 at src/main.rs:53:47: 53:48
    }
    bb9 (cleanup): {
        resume;                          // scope 0 at src/main.rs:52:1: 57:2
    }
    bb10: {
        _23 = copy _17 as *const () (PtrToPtr); // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
        _24 = copy _23 as usize (Transmute); // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
        _25 = Ne(const <std::mem::MaybeUninit<[D; 1]> as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
        _26 = Eq(copy _24, const 0_usize); // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
        _27 = BitAnd(copy _26, copy _25); // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
        _28 = Not(copy _27);             // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
        assert(copy _28, "null pointer dereference occurred") -> [success: bb11, unwind unreachable]; // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
    }
    bb11: {
        ((((*_17).1: std::mem::ManuallyDrop<[D; 1]>).0: std::mem::MaybeDangling<[D; 1]>).0: [D; 1]) = [move _6]; // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:86: 59:95
        StorageDead(_6);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:94: 59:95
        _4 = move _5;                    // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:13: 59:96
        StorageDead(_5);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:59:95: 59:96
        _3 = std::boxed::box_assume_init_into_vec_unsafe::<D, 1>(move _4) -> [return: bb2, unwind: bb9]; // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs:58:9: 60:10
    }
}
"""

NAMED = """// MIR for `named` after PreCodegen

fn named(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:59:10: 59:11
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:59:21: 59:24
    let _2: S;                           // in scope 0 at src/main.rs:60:9: 60:10
    let mut _3: std::string::String;     // in scope 0 at src/main.rs:60:23: 60:36
    let mut _4: &u32;                    // in scope 0 at src/main.rs:60:23: 60:24
    let mut _5: u32;                     // in scope 0 at src/main.rs:60:41: 60:46
    let mut _6: u32;                     // in scope 0 at src/main.rs:60:41: 60:42
    let mut _7: (u32, bool);             // in scope 0 at src/main.rs:60:41: 60:46
    let mut _10: u32;                    // in scope 0 at src/main.rs:63:5: 63:6
    let mut _11: u32;                    // in scope 0 at src/main.rs:63:9: 63:10
    let mut _12: (u32, bool);            // in scope 0 at src/main.rs:63:5: 63:10
    scope 1 {
        debug s => _2;                   // in scope 1 at src/main.rs:60:9: 60:10
        let _8: u32;                     // in scope 1 at src/main.rs:61:9: 61:10
        scope 2 {
            debug z => _8;               // in scope 2 at src/main.rs:61:9: 61:10
            let _9: u32;                 // in scope 2 at src/main.rs:62:9: 62:10
            scope 3 {
                debug k => _9;           // in scope 3 at src/main.rs:62:9: 62:10
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:60:9: 60:10
        StorageLive(_3);                 // scope 0 at src/main.rs:60:23: 60:36
        StorageLive(_4);                 // scope 0 at src/main.rs:60:23: 60:24
        _4 = &_1;                        // scope 0 at src/main.rs:60:23: 60:24
        _3 = <u32 as ToString>::to_string(move _4) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:60:23: 60:36
    }
    bb1: {
        StorageDead(_4);                 // scope 0 at src/main.rs:60:35: 60:36
        StorageLive(_5);                 // scope 0 at src/main.rs:60:41: 60:46
        StorageLive(_6);                 // scope 0 at src/main.rs:60:41: 60:42
        _6 = copy _1;                    // scope 0 at src/main.rs:60:41: 60:42
        _7 = AddWithOverflow(copy _6, const 1_u32); // scope 0 at src/main.rs:60:41: 60:46
        assert(!move (_7.1: bool), "attempt to compute `{} + {}`, which would overflow", move _6, const 1_u32) -> [success: bb2, unwind: bb7]; // scope 0 at src/main.rs:60:41: 60:46
    }
    bb2: {
        _5 = move (_7.0: u32);           // scope 0 at src/main.rs:60:41: 60:46
        StorageDead(_6);                 // scope 0 at src/main.rs:60:45: 60:46
        _2 = S { name: move _3, n: move _5 }; // scope 0 at src/main.rs:60:13: 60:48
        StorageDead(_5);                 // scope 0 at src/main.rs:60:47: 60:48
        StorageDead(_3);                 // scope 0 at src/main.rs:60:47: 60:48
        StorageLive(_8);                 // scope 1 at src/main.rs:61:9: 61:10
        _8 = copy (_2.1: u32);           // scope 1 at src/main.rs:61:13: 61:16
        StorageLive(_9);                 // scope 2 at src/main.rs:62:9: 62:10
        _9 = other() -> [return: bb3, unwind: bb6]; // scope 2 at src/main.rs:62:13: 62:20
    }
    bb3: {
        StorageLive(_10);                // scope 3 at src/main.rs:63:5: 63:6
        _10 = copy _8;                   // scope 3 at src/main.rs:63:5: 63:6
        StorageLive(_11);                // scope 3 at src/main.rs:63:9: 63:10
        _11 = copy _9;                   // scope 3 at src/main.rs:63:9: 63:10
        _12 = AddWithOverflow(copy _10, copy _11); // scope 3 at src/main.rs:63:5: 63:10
        assert(!move (_12.1: bool), "attempt to compute `{} + {}`, which would overflow", move _10, move _11) -> [success: bb4, unwind: bb6]; // scope 3 at src/main.rs:63:5: 63:10
    }
    bb4: {
        _0 = move (_12.0: u32);          // scope 3 at src/main.rs:63:5: 63:10
        StorageDead(_11);                // scope 3 at src/main.rs:63:9: 63:10
        StorageDead(_10);                // scope 3 at src/main.rs:63:9: 63:10
        StorageDead(_9);                 // scope 2 at src/main.rs:64:1: 64:2
        StorageDead(_8);                 // scope 1 at src/main.rs:64:1: 64:2
        drop(_2) -> [return: bb5, unwind: bb8]; // scope 0 at src/main.rs:64:1: 64:2
    }
    bb5: {
        StorageDead(_2);                 // scope 0 at src/main.rs:64:1: 64:2
        return;                          // scope 0 at src/main.rs:64:2: 64:2
    }
    bb6 (cleanup): {
        drop(_2) -> [return: bb8, unwind terminate(cleanup)]; // scope 0 at src/main.rs:64:1: 64:2
    }
    bb7 (cleanup): {
        drop(_3) -> [return: bb8, unwind terminate(cleanup)]; // scope 0 at src/main.rs:60:47: 60:48
    }
    bb8 (cleanup): {
        resume;                          // scope 0 at src/main.rs:59:1: 64:2
    }
}
"""

TAKEN = """// MIR for `taken` after PreCodegen

fn taken(_1: D) -> u32 {
    debug d => _1;                       // in scope 0 at src/main.rs:66:10: 66:11
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:66:19: 66:22
    let _2: u32;                         // in scope 0 at src/main.rs:67:9: 67:10
    let mut _4: u32;                     // in scope 0 at src/main.rs:69:5: 69:6
    let mut _5: u32;                     // in scope 0 at src/main.rs:69:9: 69:10
    let mut _6: (u32, bool);             // in scope 0 at src/main.rs:69:5: 69:10
    scope 1 {
        debug z => _2;                   // in scope 1 at src/main.rs:67:9: 67:10
        let _3: u32;                     // in scope 1 at src/main.rs:68:9: 68:10
        scope 2 {
            debug k => _3;               // in scope 2 at src/main.rs:68:9: 68:10
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:67:9: 67:10
        _2 = copy (_1.0: u32);           // scope 0 at src/main.rs:67:13: 67:16
        StorageLive(_3);                 // scope 1 at src/main.rs:68:9: 68:10
        _3 = other() -> [return: bb1, unwind: bb4]; // scope 1 at src/main.rs:68:13: 68:20
    }
    bb1: {
        StorageLive(_4);                 // scope 2 at src/main.rs:69:5: 69:6
        _4 = copy _2;                    // scope 2 at src/main.rs:69:5: 69:6
        StorageLive(_5);                 // scope 2 at src/main.rs:69:9: 69:10
        _5 = copy _3;                    // scope 2 at src/main.rs:69:9: 69:10
        _6 = AddWithOverflow(copy _4, copy _5); // scope 2 at src/main.rs:69:5: 69:10
        assert(!move (_6.1: bool), "attempt to compute `{} + {}`, which would overflow", move _4, move _5) -> [success: bb2, unwind: bb4]; // scope 2 at src/main.rs:69:5: 69:10
    }
    bb2: {
        _0 = move (_6.0: u32);           // scope 2 at src/main.rs:69:5: 69:10
        StorageDead(_5);                 // scope 2 at src/main.rs:69:9: 69:10
        StorageDead(_4);                 // scope 2 at src/main.rs:69:9: 69:10
        StorageDead(_3);                 // scope 1 at src/main.rs:70:1: 70:2
        StorageDead(_2);                 // scope 0 at src/main.rs:70:1: 70:2
        drop(_1) -> [return: bb3, unwind: bb5]; // scope 0 at src/main.rs:70:1: 70:2
    }
    bb3: {
        return;                          // scope 0 at src/main.rs:70:2: 70:2
    }
    bb4 (cleanup): {
        drop(_1) -> [return: bb5, unwind terminate(cleanup)]; // scope 0 at src/main.rs:70:1: 70:2
    }
    bb5 (cleanup): {
        resume;                          // scope 0 at src/main.rs:66:1: 70:2
    }
}
"""

PANICKED = """// MIR for `panicked` after PreCodegen

fn panicked(_1: u32) -> u32 {
    debug x => _1;                       // in scope 0 at src/main.rs:72:13: 72:18
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:72:28: 72:31
    let mut _2: !;                       // in scope 0 at src/main.rs:72:32: 78:2
    let _3: &mut u32;                    // in scope 0 at src/main.rs:73:5: 73:22
    let mut _4: &mut u32;                // in scope 0 at src/main.rs:73:15: 73:21
    let _5: u32;                         // in scope 0 at src/main.rs:74:9: 74:10
    let _7: u32;                         // in scope 0 at src/main.rs:76:5: 76:21
    let mut _8: u32;                     // in scope 0 at src/main.rs:76:15: 76:20
    let mut _9: u32;                     // in scope 0 at src/main.rs:76:15: 76:16
    let mut _10: u32;                    // in scope 0 at src/main.rs:76:19: 76:20
    let mut _11: (u32, bool);            // in scope 0 at src/main.rs:76:15: 76:20
    let _12: !;                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:59:21: 63:6
    let _13: !;                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
    let mut _14: std::fmt::Arguments<'_>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
    scope 1 {
        debug z => _5;                   // in scope 1 at src/main.rs:74:9: 74:10
        let _6: u32;                     // in scope 1 at src/main.rs:75:9: 75:10
        scope 2 {
            debug k => _6;               // in scope 2 at src/main.rs:75:9: 75:10
        }
    }
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:73:5: 73:22
        StorageLive(_4);                 // scope 0 at src/main.rs:73:15: 73:21
        _4 = &mut _1;                    // scope 0 at src/main.rs:73:15: 73:21
        _3 = std::hint::black_box::<&mut u32>(move _4) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:73:5: 73:22
    }
    bb1: {
        StorageDead(_4);                 // scope 0 at src/main.rs:73:21: 73:22
        StorageDead(_3);                 // scope 0 at src/main.rs:73:22: 73:23
        StorageLive(_5);                 // scope 0 at src/main.rs:74:9: 74:10
        _5 = copy _1;                    // scope 0 at src/main.rs:74:13: 74:14
        StorageLive(_6);                 // scope 1 at src/main.rs:75:9: 75:10
        _6 = other() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:75:13: 75:20
    }
    bb2: {
        StorageLive(_7);                 // scope 2 at src/main.rs:76:5: 76:21
        StorageLive(_8);                 // scope 2 at src/main.rs:76:15: 76:20
        StorageLive(_9);                 // scope 2 at src/main.rs:76:15: 76:16
        _9 = copy _5;                    // scope 2 at src/main.rs:76:15: 76:16
        StorageLive(_10);                // scope 2 at src/main.rs:76:19: 76:20
        _10 = copy _6;                   // scope 2 at src/main.rs:76:19: 76:20
        _11 = AddWithOverflow(copy _9, copy _10); // scope 2 at src/main.rs:76:15: 76:20
        assert(!move (_11.1: bool), "attempt to compute `{} + {}`, which would overflow", move _9, move _10) -> [success: bb3, unwind continue]; // scope 2 at src/main.rs:76:15: 76:20
    }
    bb3: {
        _8 = move (_11.0: u32);          // scope 2 at src/main.rs:76:15: 76:20
        StorageDead(_10);                // scope 2 at src/main.rs:76:19: 76:20
        StorageDead(_9);                 // scope 2 at src/main.rs:76:19: 76:20
        _7 = std::hint::black_box::<u32>(move _8) -> [return: bb4, unwind continue]; // scope 2 at src/main.rs:76:5: 76:21
    }
    bb4: {
        StorageDead(_8);                 // scope 2 at src/main.rs:76:20: 76:21
        StorageDead(_7);                 // scope 2 at src/main.rs:76:21: 76:22
        StorageLive(_13);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
        StorageLive(_14);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
        _14 = Arguments::<'_>::from_str(const "stop") -> [return: bb5, unwind continue]; // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
    }
    bb5: {
        _13 = std::rt::panic_fmt(move _14) -> unwind continue; // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
    }
}
alloc5 (size: 4, align: 1) {
    73 74 6f 70                                     │ stop
}
"""

UNWOUND = """// MIR for `unwound` after PreCodegen

fn unwound(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:80:12: 80:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:80:23: 80:26
    let mut _2: !;                       // in scope 0 at src/main.rs:80:27: 89:2
    let _3: std::string::String;         // in scope 0 at src/main.rs:81:9: 81:14
    let mut _4: &u32;                    // in scope 0 at src/main.rs:81:17: 81:18
    let mut _6: u32;                     // in scope 0 at src/main.rs:82:17: 82:18
    let mut _7: (u32, bool);             // in scope 0 at src/main.rs:82:17: 82:22
    let _8: &mut u32;                    // in scope 0 at src/main.rs:83:5: 83:22
    let mut _9: &mut u32;                // in scope 0 at src/main.rs:83:15: 83:21
    let _12: u32;                        // in scope 0 at src/main.rs:86:5: 86:21
    let mut _13: u32;                    // in scope 0 at src/main.rs:86:15: 86:20
    let mut _14: u32;                    // in scope 0 at src/main.rs:86:15: 86:16
    let mut _15: u32;                    // in scope 0 at src/main.rs:86:19: 86:20
    let mut _16: (u32, bool);            // in scope 0 at src/main.rs:86:15: 86:20
    let _17: &std::string::String;       // in scope 0 at src/main.rs:87:5: 87:22
    let mut _18: &std::string::String;   // in scope 0 at src/main.rs:87:15: 87:21
    let _19: !;                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:59:21: 63:6
    let _20: !;                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
    let mut _21: std::fmt::Arguments<'_>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
    scope 1 {
        debug label => _3;               // in scope 1 at src/main.rs:81:9: 81:14
        let mut _5: u32;                 // in scope 1 at src/main.rs:82:9: 82:14
        scope 2 {
            debug x => _5;               // in scope 2 at src/main.rs:82:9: 82:14
            let _10: u32;                // in scope 2 at src/main.rs:84:9: 84:10
            scope 3 {
                debug z => _10;          // in scope 3 at src/main.rs:84:9: 84:10
                let _11: u32;            // in scope 3 at src/main.rs:85:9: 85:10
                scope 4 {
                    debug k => _11;      // in scope 4 at src/main.rs:85:9: 85:10
                }
            }
        }
    }
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:81:9: 81:14
        StorageLive(_4);                 // scope 0 at src/main.rs:81:17: 81:18
        _4 = &_1;                        // scope 0 at src/main.rs:81:17: 81:18
        _3 = <u32 as ToString>::to_string(move _4) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:81:17: 81:30
    }
    bb1: {
        StorageDead(_4);                 // scope 0 at src/main.rs:81:29: 81:30
        StorageLive(_5);                 // scope 1 at src/main.rs:82:9: 82:14
        StorageLive(_6);                 // scope 1 at src/main.rs:82:17: 82:18
        _6 = copy _1;                    // scope 1 at src/main.rs:82:17: 82:18
        _7 = AddWithOverflow(copy _6, const 1_u32); // scope 1 at src/main.rs:82:17: 82:22
        assert(!move (_7.1: bool), "attempt to compute `{} + {}`, which would overflow", move _6, const 1_u32) -> [success: bb2, unwind: bb9]; // scope 1 at src/main.rs:82:17: 82:22
    }
    bb2: {
        _5 = move (_7.0: u32);           // scope 1 at src/main.rs:82:17: 82:22
        StorageDead(_6);                 // scope 1 at src/main.rs:82:21: 82:22
        StorageLive(_8);                 // scope 2 at src/main.rs:83:5: 83:22
        StorageLive(_9);                 // scope 2 at src/main.rs:83:15: 83:21
        _9 = &mut _5;                    // scope 2 at src/main.rs:83:15: 83:21
        _8 = std::hint::black_box::<&mut u32>(move _9) -> [return: bb3, unwind: bb9]; // scope 2 at src/main.rs:83:5: 83:22
    }
    bb3: {
        StorageDead(_9);                 // scope 2 at src/main.rs:83:21: 83:22
        StorageDead(_8);                 // scope 2 at src/main.rs:83:22: 83:23
        StorageLive(_10);                // scope 2 at src/main.rs:84:9: 84:10
        _10 = copy _5;                   // scope 2 at src/main.rs:84:13: 84:14
        StorageLive(_11);                // scope 3 at src/main.rs:85:9: 85:10
        _11 = other() -> [return: bb4, unwind: bb9]; // scope 3 at src/main.rs:85:13: 85:20
    }
    bb4: {
        StorageLive(_12);                // scope 4 at src/main.rs:86:5: 86:21
        StorageLive(_13);                // scope 4 at src/main.rs:86:15: 86:20
        StorageLive(_14);                // scope 4 at src/main.rs:86:15: 86:16
        _14 = copy _10;                  // scope 4 at src/main.rs:86:15: 86:16
        StorageLive(_15);                // scope 4 at src/main.rs:86:19: 86:20
        _15 = copy _11;                  // scope 4 at src/main.rs:86:19: 86:20
        _16 = AddWithOverflow(copy _14, copy _15); // scope 4 at src/main.rs:86:15: 86:20
        assert(!move (_16.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _15) -> [success: bb5, unwind: bb9]; // scope 4 at src/main.rs:86:15: 86:20
    }
    bb5: {
        _13 = move (_16.0: u32);         // scope 4 at src/main.rs:86:15: 86:20
        StorageDead(_15);                // scope 4 at src/main.rs:86:19: 86:20
        StorageDead(_14);                // scope 4 at src/main.rs:86:19: 86:20
        _12 = std::hint::black_box::<u32>(move _13) -> [return: bb6, unwind: bb9]; // scope 4 at src/main.rs:86:5: 86:21
    }
    bb6: {
        StorageDead(_13);                // scope 4 at src/main.rs:86:20: 86:21
        StorageDead(_12);                // scope 4 at src/main.rs:86:21: 86:22
        StorageLive(_17);                // scope 4 at src/main.rs:87:5: 87:22
        StorageLive(_18);                // scope 4 at src/main.rs:87:15: 87:21
        _18 = &_3;                       // scope 4 at src/main.rs:87:15: 87:21
        _17 = std::hint::black_box::<&String>(move _18) -> [return: bb7, unwind: bb9]; // scope 4 at src/main.rs:87:5: 87:22
    }
    bb7: {
        StorageDead(_18);                // scope 4 at src/main.rs:87:21: 87:22
        StorageDead(_17);                // scope 4 at src/main.rs:87:22: 87:23
        StorageLive(_20);                // scope 4 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
        StorageLive(_21);                // scope 4 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
        _21 = Arguments::<'_>::from_str(const "stop") -> [return: bb8, unwind: bb9]; // scope 4 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
    }
    bb8: {
        _20 = std::rt::panic_fmt(move _21) -> bb9; // scope 4 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
    }
    bb9 (cleanup): {
        drop(_3) -> [return: bb10, unwind terminate(cleanup)]; // scope 0 at src/main.rs:89:1: 89:2
    }
    bb10 (cleanup): {
        resume;                          // scope 0 at src/main.rs:80:1: 89:2
    }
}
alloc5 (size: 4, align: 1) {
    73 74 6f 70                                     │ stop
}
"""

POINTED = """// MIR for `pointed` after PreCodegen

fn pointed(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:91:12: 91:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:91:23: 91:26
    let mut _2: !;                       // in scope 0 at src/main.rs:91:27: 99:2
    let _3: u32;                         // in scope 0 at src/main.rs:92:9: 92:10
    let mut _4: u32;                     // in scope 0 at src/main.rs:92:13: 92:14
    let mut _5: (u32, bool);             // in scope 0 at src/main.rs:92:13: 92:18
    let _9: u32;                         // in scope 0 at src/main.rs:96:5: 96:21
    let mut _10: u32;                    // in scope 0 at src/main.rs:96:15: 96:20
    let mut _11: u32;                    // in scope 0 at src/main.rs:96:15: 96:16
    let mut _12: u32;                    // in scope 0 at src/main.rs:96:19: 96:20
    let mut _13: (u32, bool);            // in scope 0 at src/main.rs:96:15: 96:20
    let _14: &u32;                       // in scope 0 at src/main.rs:97:5: 97:17
    let mut _15: &u32;                   // in scope 0 at src/main.rs:97:15: 97:16
    let _16: !;                          // in scope 0 at src/main.rs:98:5: 98:26
    scope 1 {
        debug x => _3;                   // in scope 1 at src/main.rs:92:9: 92:10
        let _6: &u32;                    // in scope 1 at src/main.rs:93:9: 93:10
        scope 2 {
            debug r => _6;               // in scope 2 at src/main.rs:93:9: 93:10
            let _7: u32;                 // in scope 2 at src/main.rs:94:9: 94:10
            scope 3 {
                debug z => _7;           // in scope 3 at src/main.rs:94:9: 94:10
                let _8: u32;             // in scope 3 at src/main.rs:95:9: 95:10
                scope 4 {
                    debug k => _8;       // in scope 4 at src/main.rs:95:9: 95:10
                }
            }
        }
    }
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:92:9: 92:10
        StorageLive(_4);                 // scope 0 at src/main.rs:92:13: 92:14
        _4 = copy _1;                    // scope 0 at src/main.rs:92:13: 92:14
        _5 = AddWithOverflow(copy _4, const 1_u32); // scope 0 at src/main.rs:92:13: 92:18
        assert(!move (_5.1: bool), "attempt to compute `{} + {}`, which would overflow", move _4, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:92:13: 92:18
    }
    bb1: {
        _3 = move (_5.0: u32);           // scope 0 at src/main.rs:92:13: 92:18
        StorageDead(_4);                 // scope 0 at src/main.rs:92:17: 92:18
        StorageLive(_6);                 // scope 1 at src/main.rs:93:9: 93:10
        _6 = &_3;                        // scope 1 at src/main.rs:93:13: 93:15
        StorageLive(_7);                 // scope 2 at src/main.rs:94:9: 94:10
        _7 = copy _3;                    // scope 2 at src/main.rs:94:13: 94:14
        StorageLive(_8);                 // scope 3 at src/main.rs:95:9: 95:10
        _8 = other() -> [return: bb2, unwind continue]; // scope 3 at src/main.rs:95:13: 95:20
    }
    bb2: {
        StorageLive(_9);                 // scope 4 at src/main.rs:96:5: 96:21
        StorageLive(_10);                // scope 4 at src/main.rs:96:15: 96:20
        StorageLive(_11);                // scope 4 at src/main.rs:96:15: 96:16
        _11 = copy _7;                   // scope 4 at src/main.rs:96:15: 96:16
        StorageLive(_12);                // scope 4 at src/main.rs:96:19: 96:20
        _12 = copy _8;                   // scope 4 at src/main.rs:96:19: 96:20
        _13 = AddWithOverflow(copy _11, copy _12); // scope 4 at src/main.rs:96:15: 96:20
        assert(!move (_13.1: bool), "attempt to compute `{} + {}`, which would overflow", move _11, move _12) -> [success: bb3, unwind continue]; // scope 4 at src/main.rs:96:15: 96:20
    }
    bb3: {
        _10 = move (_13.0: u32);         // scope 4 at src/main.rs:96:15: 96:20
        StorageDead(_12);                // scope 4 at src/main.rs:96:19: 96:20
        StorageDead(_11);                // scope 4 at src/main.rs:96:19: 96:20
        _9 = std::hint::black_box::<u32>(move _10) -> [return: bb4, unwind continue]; // scope 4 at src/main.rs:96:5: 96:21
    }
    bb4: {
        StorageDead(_10);                // scope 4 at src/main.rs:96:20: 96:21
        StorageDead(_9);                 // scope 4 at src/main.rs:96:21: 96:22
        StorageLive(_14);                // scope 4 at src/main.rs:97:5: 97:17
        StorageLive(_15);                // scope 4 at src/main.rs:97:15: 97:16
        _15 = copy _6;                   // scope 4 at src/main.rs:97:15: 97:16
        _14 = std::hint::black_box::<&u32>(move _15) -> [return: bb5, unwind continue]; // scope 4 at src/main.rs:97:5: 97:17
    }
    bb5: {
        StorageDead(_15);                // scope 4 at src/main.rs:97:16: 97:17
        StorageDead(_14);                // scope 4 at src/main.rs:97:17: 97:18
        StorageLive(_16);                // scope 4 at src/main.rs:98:5: 98:26
        _16 = exit(const 0_i32) -> unwind continue; // scope 4 at src/main.rs:98:5: 98:26
    }
}
"""

EXITED = """// MIR for `exited-{closure#0}` after PreCodegen

/* coroutine_layout = CoroutineLayout {
    field_tys: {},
    variant_fields: {
        Unresumed(0): [],
        Returned (1): [],
        Panicked (2): [],
    },
    storage_conflicts: BitMatrix(0x0) {},
} */
fn exited::{closure#0}(_1: Pin<&mut {async fn body of exited()}>, _2: &mut Context<'_>) -> Poll<()> {
    debug _task_context => _2;           // in scope 0 at src/main.rs:101:25: 108:2
    debug n => ((*_20).0: u32);          // in scope 0 at src/main.rs:101:17: 101:18
    let mut _0: std::task::Poll<()>;     // return place in scope 0 at src/main.rs:101:25: 108:2
    let _3: u32;                         // in scope 0 at src/main.rs:101:17: 101:18
    let mut _4: !;                       // in scope 0 at src/main.rs:101:25: 108:2
    let mut _6: u32;                     // in scope 0 at src/main.rs:102:17: 102:18
    let mut _7: (u32, bool);             // in scope 0 at src/main.rs:102:17: 102:22
    let _8: &mut u32;                    // in scope 0 at src/main.rs:103:5: 103:22
    let mut _9: &mut u32;                // in scope 0 at src/main.rs:103:15: 103:21
    let _12: u32;                        // in scope 0 at src/main.rs:106:5: 106:21
    let mut _13: u32;                    // in scope 0 at src/main.rs:106:15: 106:20
    let mut _14: u32;                    // in scope 0 at src/main.rs:106:15: 106:16
    let mut _15: u32;                    // in scope 0 at src/main.rs:106:19: 106:20
    let mut _16: (u32, bool);            // in scope 0 at src/main.rs:106:15: 106:20
    let _17: !;                          // in scope 0 at src/main.rs:107:5: 107:26
    let mut _18: ();                     // in scope 0 at src/main.rs:101:25: 108:2
    let mut _19: u32;                    // in scope 0 at src/main.rs:101:25: 108:2
    let mut _20: &mut {async fn body of exited()}; // in scope 0 at src/main.rs:101:25: 108:2
    scope 1 {
        debug n => _3;                   // in scope 1 at src/main.rs:101:17: 101:18
        let mut _5: u32;                 // in scope 1 at src/main.rs:102:9: 102:14
        scope 2 {
            debug x => _5;               // in scope 2 at src/main.rs:102:9: 102:14
            let _10: u32;                // in scope 2 at src/main.rs:104:9: 104:10
            scope 3 {
                debug z => _10;          // in scope 3 at src/main.rs:104:9: 104:10
                let _11: u32;            // in scope 3 at src/main.rs:105:9: 105:10
                scope 4 {
                    debug k => _11;      // in scope 4 at src/main.rs:105:9: 105:10
                }
            }
        }
    }
    bb0: {
        _20 = copy (_1.0: &mut {async fn body of exited()}); // scope 0 at src/main.rs:101:25: 108:2
        _19 = discriminant((*_20));      // scope 0 at src/main.rs:101:25: 108:2
        switchInt(move _19) -> [0: bb1, 2: bb14, otherwise: bb15]; // scope 0 at src/main.rs:101:25: 108:2
    }
    bb1: {
        StorageLive(_3);                 // scope 0 at src/main.rs:101:17: 101:18
        _3 = copy ((*_20).0: u32);       // scope 0 at src/main.rs:101:17: 101:18
        StorageLive(_4);                 // scope 1 at src/main.rs:101:25: 108:2
        StorageLive(_5);                 // scope 1 at src/main.rs:102:9: 102:14
        StorageLive(_6);                 // scope 1 at src/main.rs:102:17: 102:18
        _6 = copy _3;                    // scope 1 at src/main.rs:102:17: 102:18
        _7 = AddWithOverflow(copy _6, const 1_u32); // scope 1 at src/main.rs:102:17: 102:22
        assert(!move (_7.1: bool), "attempt to compute `{} + {}`, which would overflow", move _6, const 1_u32) -> [success: bb2, unwind: bb12]; // scope 1 at src/main.rs:102:17: 102:22
    }
    bb2: {
        _5 = move (_7.0: u32);           // scope 1 at src/main.rs:102:17: 102:22
        StorageDead(_6);                 // scope 1 at src/main.rs:102:21: 102:22
        StorageLive(_8);                 // scope 2 at src/main.rs:103:5: 103:22
        StorageLive(_9);                 // scope 2 at src/main.rs:103:15: 103:21
        _9 = &mut _5;                    // scope 2 at src/main.rs:103:15: 103:21
        _8 = std::hint::black_box::<&mut u32>(move _9) -> [return: bb3, unwind: bb11]; // scope 2 at src/main.rs:103:5: 103:22
    }
    bb3: {
        StorageDead(_9);                 // scope 2 at src/main.rs:103:21: 103:22
        StorageDead(_8);                 // scope 2 at src/main.rs:103:22: 103:23
        StorageLive(_10);                // scope 2 at src/main.rs:104:9: 104:10
        _10 = copy _5;                   // scope 2 at src/main.rs:104:13: 104:14
        StorageLive(_11);                // scope 3 at src/main.rs:105:9: 105:10
        _11 = other() -> [return: bb4, unwind: bb10]; // scope 3 at src/main.rs:105:13: 105:20
    }
    bb4: {
        StorageLive(_12);                // scope 4 at src/main.rs:106:5: 106:21
        StorageLive(_13);                // scope 4 at src/main.rs:106:15: 106:20
        StorageLive(_14);                // scope 4 at src/main.rs:106:15: 106:16
        _14 = copy _10;                  // scope 4 at src/main.rs:106:15: 106:16
        StorageLive(_15);                // scope 4 at src/main.rs:106:19: 106:20
        _15 = copy _11;                  // scope 4 at src/main.rs:106:19: 106:20
        _16 = AddWithOverflow(copy _14, copy _15); // scope 4 at src/main.rs:106:15: 106:20
        assert(!move (_16.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _15) -> [success: bb5, unwind: bb8]; // scope 4 at src/main.rs:106:15: 106:20
    }
    bb5: {
        _13 = move (_16.0: u32);         // scope 4 at src/main.rs:106:15: 106:20
        StorageDead(_15);                // scope 4 at src/main.rs:106:19: 106:20
        StorageDead(_14);                // scope 4 at src/main.rs:106:19: 106:20
        _12 = std::hint::black_box::<u32>(move _13) -> [return: bb6, unwind: bb9]; // scope 4 at src/main.rs:106:5: 106:21
    }
    bb6: {
        StorageDead(_13);                // scope 4 at src/main.rs:106:20: 106:21
        StorageDead(_12);                // scope 4 at src/main.rs:106:21: 106:22
        StorageLive(_17);                // scope 4 at src/main.rs:107:5: 107:26
        _17 = exit(const 0_i32) -> bb7;  // scope 4 at src/main.rs:107:5: 107:26
    }
    bb7 (cleanup): {
        StorageDead(_17);                // scope 4 at src/main.rs:107:26: 107:27
        goto -> bb10;                    // scope 4 at no-location
    }
    bb8 (cleanup): {
        StorageDead(_15);                // scope 4 at src/main.rs:106:19: 106:20
        StorageDead(_14);                // scope 4 at src/main.rs:106:19: 106:20
        goto -> bb9;                     // scope 4 at no-location
    }
    bb9 (cleanup): {
        StorageDead(_13);                // scope 4 at src/main.rs:106:20: 106:21
        StorageDead(_12);                // scope 4 at src/main.rs:106:21: 106:22
        goto -> bb10;                    // scope 4 at no-location
    }
    bb10 (cleanup): {
        StorageDead(_11);                // scope 3 at src/main.rs:108:1: 108:2
        StorageDead(_10);                // scope 2 at src/main.rs:108:1: 108:2
        goto -> bb13;                    // scope 2 at no-location
    }
    bb11 (cleanup): {
        StorageDead(_9);                 // scope 2 at src/main.rs:103:21: 103:22
        StorageDead(_8);                 // scope 2 at src/main.rs:103:22: 103:23
        goto -> bb13;                    // scope 2 at no-location
    }
    bb12 (cleanup): {
        StorageDead(_6);                 // scope 1 at src/main.rs:102:21: 102:22
        goto -> bb13;                    // scope 1 at no-location
    }
    bb13 (cleanup): {
        StorageDead(_5);                 // scope 1 at src/main.rs:108:1: 108:2
        StorageDead(_4);                 // scope 1 at src/main.rs:108:1: 108:2
        StorageDead(_3);                 // scope 0 at src/main.rs:108:1: 108:2
        discriminant((*_20)) = 2;        // scope 0 at src/main.rs:101:25: 108:2
        resume;                          // scope 0 at src/main.rs:101:25: 108:2
    }
    bb14: {
        assert(const false, "`async fn` resumed after panicking") -> [success: bb14, unwind continue]; // scope 0 at src/main.rs:101:25: 108:2
    }
    bb15: {
        unreachable;                     // scope 0 at src/main.rs:101:25: 108:2
    }
}
"""

CAUGHT = """// MIR for `caught` after PreCodegen

fn caught(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:127:11: 127:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:127:22: 127:25
    let mut _2: u32;                     // in scope 0 at src/main.rs:128:9: 128:14
    let mut _3: u32;                     // in scope 0 at src/main.rs:128:17: 128:18
    let mut _4: (u32, bool);             // in scope 0 at src/main.rs:128:17: 128:22
    let _5: &mut u32;                    // in scope 0 at src/main.rs:129:5: 129:22
    let mut _6: &mut u32;                // in scope 0 at src/main.rs:129:15: 129:21
    let _9: ();                          // in scope 0 at src/main.rs:132:5: 132:14
    let mut _10: u32;                    // in scope 0 at src/main.rs:132:12: 132:13
    let _11: ();                         // in scope 0 at src/main.rs:133:5: 133:17
    let mut _12: u32;                    // in scope 0 at src/main.rs:133:15: 133:16
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:128:9: 128:14
        let _7: u32;                     // in scope 1 at src/main.rs:130:9: 130:10
        scope 2 {
            debug z => _7;               // in scope 2 at src/main.rs:130:9: 130:10
            let _8: u32;                 // in scope 2 at src/main.rs:131:9: 131:10
            scope 3 {
                debug k => _8;           // in scope 3 at src/main.rs:131:9: 131:10
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:128:9: 128:14
        StorageLive(_3);                 // scope 0 at src/main.rs:128:17: 128:18
        _3 = copy _1;                    // scope 0 at src/main.rs:128:17: 128:18
        _4 = AddWithOverflow(copy _3, const 1_u32); // scope 0 at src/main.rs:128:17: 128:22
        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:128:17: 128:22
    }
    bb1: {
        _2 = move (_4.0: u32);           // scope 0 at src/main.rs:128:17: 128:22
        StorageDead(_3);                 // scope 0 at src/main.rs:128:21: 128:22
        StorageLive(_5);                 // scope 1 at src/main.rs:129:5: 129:22
        StorageLive(_6);                 // scope 1 at src/main.rs:129:15: 129:21
        _6 = &mut _2;                    // scope 1 at src/main.rs:129:15: 129:21
        _5 = std::hint::black_box::<&mut u32>(move _6) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:129:5: 129:22
    }
    bb2: {
        StorageDead(_6);                 // scope 1 at src/main.rs:129:21: 129:22
        StorageDead(_5);                 // scope 1 at src/main.rs:129:22: 129:23
        StorageLive(_7);                 // scope 1 at src/main.rs:130:9: 130:10
        _7 = copy _2;                    // scope 1 at src/main.rs:130:13: 130:14
        StorageLive(_8);                 // scope 2 at src/main.rs:131:9: 131:10
        _8 = other() -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:131:13: 131:20
    }
    bb3: {
        StorageLive(_9);                 // scope 3 at src/main.rs:132:5: 132:14
        StorageLive(_10);                // scope 3 at src/main.rs:132:12: 132:13
        _10 = copy _7;                   // scope 3 at src/main.rs:132:12: 132:13
        _9 = record(move _10) -> [return: bb4, unwind continue]; // scope 3 at src/main.rs:132:5: 132:14
    }
    bb4: {
        StorageDead(_10);                // scope 3 at src/main.rs:132:13: 132:14
        StorageDead(_9);                 // scope 3 at src/main.rs:132:14: 132:15
        StorageLive(_11);                // scope 3 at src/main.rs:133:5: 133:17
        StorageLive(_12);                // scope 3 at src/main.rs:133:15: 133:16
        _12 = copy _8;                   // scope 3 at src/main.rs:133:15: 133:16
        _11 = may_panic(move _12) -> [return: bb5, unwind continue]; // scope 3 at src/main.rs:133:5: 133:17
    }
    bb5: {
        StorageDead(_12);                // scope 3 at src/main.rs:133:16: 133:17
        StorageDead(_11);                // scope 3 at src/main.rs:133:17: 133:18
        _0 = copy _2;                    // scope 3 at src/main.rs:134:5: 134:6
        StorageDead(_8);                 // scope 2 at src/main.rs:135:1: 135:2
        StorageDead(_7);                 // scope 1 at src/main.rs:135:1: 135:2
        StorageDead(_2);                 // scope 0 at src/main.rs:135:1: 135:2
        return;                          // scope 0 at src/main.rs:135:2: 135:2
    }
}
"""

CHECKED = """// MIR for `checked` after PreCodegen

fn checked(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:137:12: 137:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:137:23: 137:26
    let mut _2: u32;                     // in scope 0 at src/main.rs:138:9: 138:14
    let mut _3: u32;                     // in scope 0 at src/main.rs:138:17: 138:18
    let mut _4: (u32, bool);             // in scope 0 at src/main.rs:138:17: 138:22
    let _5: &mut u32;                    // in scope 0 at src/main.rs:139:5: 139:22
    let mut _6: &mut u32;                // in scope 0 at src/main.rs:139:15: 139:21
    let mut _10: u32;                    // in scope 0 at src/main.rs:142:13: 142:14
    let mut _11: (u32, bool);            // in scope 0 at src/main.rs:142:13: 142:19
    let mut _12: u32;                    // in scope 0 at src/main.rs:143:5: 143:10
    let mut _13: u32;                    // in scope 0 at src/main.rs:143:5: 143:6
    let mut _14: u32;                    // in scope 0 at src/main.rs:143:9: 143:10
    let mut _15: (u32, bool);            // in scope 0 at src/main.rs:143:5: 143:10
    let mut _16: u32;                    // in scope 0 at src/main.rs:143:13: 143:14
    let mut _17: (u32, bool);            // in scope 0 at src/main.rs:143:5: 143:14
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:138:9: 138:14
        let _7: u32;                     // in scope 1 at src/main.rs:140:9: 140:10
        scope 2 {
            debug z => _7;               // in scope 2 at src/main.rs:140:9: 140:10
            let _8: u32;                 // in scope 2 at src/main.rs:141:9: 141:10
            scope 3 {
                debug k => _8;           // in scope 3 at src/main.rs:141:9: 141:10
                let _9: u32;             // in scope 3 at src/main.rs:142:9: 142:10
                scope 4 {
                    debug w => _9;       // in scope 4 at src/main.rs:142:9: 142:10
                }
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:138:9: 138:14
        StorageLive(_3);                 // scope 0 at src/main.rs:138:17: 138:18
        _3 = copy _1;                    // scope 0 at src/main.rs:138:17: 138:18
        _4 = AddWithOverflow(copy _3, const 1_u32); // scope 0 at src/main.rs:138:17: 138:22
        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:138:17: 138:22
    }
    bb1: {
        _2 = move (_4.0: u32);           // scope 0 at src/main.rs:138:17: 138:22
        StorageDead(_3);                 // scope 0 at src/main.rs:138:21: 138:22
        StorageLive(_5);                 // scope 1 at src/main.rs:139:5: 139:22
        StorageLive(_6);                 // scope 1 at src/main.rs:139:15: 139:21
        _6 = &mut _2;                    // scope 1 at src/main.rs:139:15: 139:21
        _5 = std::hint::black_box::<&mut u32>(move _6) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:139:5: 139:22
    }
    bb2: {
        StorageDead(_6);                 // scope 1 at src/main.rs:139:21: 139:22
        StorageDead(_5);                 // scope 1 at src/main.rs:139:22: 139:23
        StorageLive(_7);                 // scope 1 at src/main.rs:140:9: 140:10
        _7 = copy _2;                    // scope 1 at src/main.rs:140:13: 140:14
        StorageLive(_8);                 // scope 2 at src/main.rs:141:9: 141:10
        _8 = other() -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:141:13: 141:20
    }
    bb3: {
        StorageLive(_9);                 // scope 3 at src/main.rs:142:9: 142:10
        StorageLive(_10);                // scope 3 at src/main.rs:142:13: 142:14
        _10 = copy _7;                   // scope 3 at src/main.rs:142:13: 142:14
        _11 = MulWithOverflow(copy _10, const 10_u32); // scope 3 at src/main.rs:142:13: 142:19
        assert(!move (_11.1: bool), "attempt to compute `{} * {}`, which would overflow", move _10, const 10_u32) -> [success: bb4, unwind continue]; // scope 3 at src/main.rs:142:13: 142:19
    }
    bb4: {
        _9 = move (_11.0: u32);          // scope 3 at src/main.rs:142:13: 142:19
        StorageDead(_10);                // scope 3 at src/main.rs:142:18: 142:19
        StorageLive(_12);                // scope 4 at src/main.rs:143:5: 143:10
        StorageLive(_13);                // scope 4 at src/main.rs:143:5: 143:6
        _13 = copy _9;                   // scope 4 at src/main.rs:143:5: 143:6
        StorageLive(_14);                // scope 4 at src/main.rs:143:9: 143:10
        _14 = copy _2;                   // scope 4 at src/main.rs:143:9: 143:10
        _15 = AddWithOverflow(copy _13, copy _14); // scope 4 at src/main.rs:143:5: 143:10
        assert(!move (_15.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _14) -> [success: bb5, unwind continue]; // scope 4 at src/main.rs:143:5: 143:10
    }
    bb5: {
        _12 = move (_15.0: u32);         // scope 4 at src/main.rs:143:5: 143:10
        StorageDead(_14);                // scope 4 at src/main.rs:143:9: 143:10
        StorageDead(_13);                // scope 4 at src/main.rs:143:9: 143:10
        StorageLive(_16);                // scope 4 at src/main.rs:143:13: 143:14
        _16 = copy _8;                   // scope 4 at src/main.rs:143:13: 143:14
        _17 = AddWithOverflow(copy _12, copy _16); // scope 4 at src/main.rs:143:5: 143:14
        assert(!move (_17.1: bool), "attempt to compute `{} + {}`, which would overflow", move _12, move _16) -> [success: bb6, unwind continue]; // scope 4 at src/main.rs:143:5: 143:14
    }
    bb6: {
        _0 = move (_17.0: u32);          // scope 4 at src/main.rs:143:5: 143:14
        StorageDead(_16);                // scope 4 at src/main.rs:143:13: 143:14
        StorageDead(_12);                // scope 4 at src/main.rs:143:13: 143:14
        StorageDead(_9);                 // scope 3 at src/main.rs:144:1: 144:2
        StorageDead(_8);                 // scope 2 at src/main.rs:144:1: 144:2
        StorageDead(_7);                 // scope 1 at src/main.rs:144:1: 144:2
        StorageDead(_2);                 // scope 0 at src/main.rs:144:1: 144:2
        return;                          // scope 0 at src/main.rs:144:2: 144:2
    }
}
"""

PRINTED = """// MIR for `printed` after PreCodegen

fn printed(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:146:12: 146:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:146:23: 146:26
    let mut _2: u32;                     // in scope 0 at src/main.rs:147:9: 147:14
    let mut _3: u32;                     // in scope 0 at src/main.rs:147:17: 147:18
    let mut _4: (u32, bool);             // in scope 0 at src/main.rs:147:17: 147:22
    let _5: &mut u32;                    // in scope 0 at src/main.rs:148:5: 148:22
    let mut _6: &mut u32;                // in scope 0 at src/main.rs:148:15: 148:21
    let _9: ();                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
    let _10: ();                         // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
    let mut _11: std::fmt::Arguments<'_>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let mut _13: &u32;                   // in scope 0 at src/main.rs:151:16: 151:17
    let mut _14: &u32;                   // in scope 0 at src/main.rs:151:20: 151:21
    let mut _16: core::fmt::rt::Argument<'_>; // in scope 0 at src/main.rs:151:15: 151:18
    let mut _17: &u32;                   // in scope 0 at src/main.rs:151:15: 151:18
    let mut _18: core::fmt::rt::Argument<'_>; // in scope 0 at src/main.rs:151:19: 151:22
    let mut _19: &u32;                   // in scope 0 at src/main.rs:151:19: 151:22
    let mut _20: &[u8; 7];               // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let _21: &[u8; 7];                   // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let mut _22: &[core::fmt::rt::Argument<'_>; 2]; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let _23: &[core::fmt::rt::Argument<'_>; 2]; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let mut _24: &u32;                   // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let mut _25: &u32;                   // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:147:9: 147:14
        let _7: u32;                     // in scope 1 at src/main.rs:149:9: 149:10
        scope 2 {
            debug z => _7;               // in scope 2 at src/main.rs:149:9: 149:10
            let _8: u32;                 // in scope 2 at src/main.rs:150:9: 150:10
            scope 3 {
                debug k => _8;           // in scope 3 at src/main.rs:150:9: 150:10
                let _12: (&u32, &u32);   // in scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
                scope 4 {
                    debug args => _12;   // in scope 4 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
                    let _15: [core::fmt::rt::Argument<'_>; 2]; // in scope 4 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
                    scope 5 {
                        debug args => _15; // in scope 5 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
                    }
                }
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:147:9: 147:14
        StorageLive(_3);                 // scope 0 at src/main.rs:147:17: 147:18
        _3 = copy _1;                    // scope 0 at src/main.rs:147:17: 147:18
        _4 = AddWithOverflow(copy _3, const 1_u32); // scope 0 at src/main.rs:147:17: 147:22
        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:147:17: 147:22
    }
    bb1: {
        _2 = move (_4.0: u32);           // scope 0 at src/main.rs:147:17: 147:22
        StorageDead(_3);                 // scope 0 at src/main.rs:147:21: 147:22
        StorageLive(_5);                 // scope 1 at src/main.rs:148:5: 148:22
        StorageLive(_6);                 // scope 1 at src/main.rs:148:15: 148:21
        _6 = &mut _2;                    // scope 1 at src/main.rs:148:15: 148:21
        _5 = std::hint::black_box::<&mut u32>(move _6) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:148:5: 148:22
    }
    bb2: {
        StorageDead(_6);                 // scope 1 at src/main.rs:148:21: 148:22
        StorageDead(_5);                 // scope 1 at src/main.rs:148:22: 148:23
        StorageLive(_7);                 // scope 1 at src/main.rs:149:9: 149:10
        _7 = copy _2;                    // scope 1 at src/main.rs:149:13: 149:14
        StorageLive(_8);                 // scope 2 at src/main.rs:150:9: 150:10
        _8 = other() -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:150:13: 150:20
    }
    bb3: {
        StorageLive(_9);                 // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
        StorageLive(_10);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
        StorageLive(_11);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_12);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_13);                // scope 3 at src/main.rs:151:16: 151:17
        _13 = &_7;                       // scope 3 at src/main.rs:151:16: 151:17
        StorageLive(_14);                // scope 3 at src/main.rs:151:20: 151:21
        _14 = &_2;                       // scope 3 at src/main.rs:151:20: 151:21
        _12 = (move _13, move _14);      // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageDead(_14);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_13);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageLive(_15);                // scope 4 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_16);                // scope 4 at src/main.rs:151:15: 151:18
        StorageLive(_17);                // scope 4 at src/main.rs:151:15: 151:18
        _24 = copy (_12.0: &u32);        // scope 4 at src/main.rs:151:15: 151:18
        _17 = &(*_24);                   // scope 4 at src/main.rs:151:15: 151:18
        _16 = core::fmt::rt::Argument::<'_>::new_display::<u32>(move _17) -> [return: bb4, unwind continue]; // scope 4 at src/main.rs:151:15: 151:18
    }
    bb4: {
        StorageDead(_17);                // scope 4 at src/main.rs:151:17: 151:18
        StorageLive(_18);                // scope 4 at src/main.rs:151:19: 151:22
        StorageLive(_19);                // scope 4 at src/main.rs:151:19: 151:22
        _25 = copy (_12.1: &u32);        // scope 4 at src/main.rs:151:19: 151:22
        _19 = &(*_25);                   // scope 4 at src/main.rs:151:19: 151:22
        _18 = core::fmt::rt::Argument::<'_>::new_display::<u32>(move _19) -> [return: bb5, unwind continue]; // scope 4 at src/main.rs:151:19: 151:22
    }
    bb5: {
        StorageDead(_19);                // scope 4 at src/main.rs:151:21: 151:22
        _15 = [move _16, move _18];      // scope 4 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageDead(_18);                // scope 4 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_16);                // scope 4 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageLive(_20);                // scope 5 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_21);                // scope 5 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _21 = const b""; // scope 5 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _20 = &(*_21);                   // scope 5 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_22);                // scope 5 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_23);                // scope 5 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _23 = &_15;                      // scope 5 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _22 = &(*_23);                   // scope 5 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _11 = Arguments::<'_>::new::<7, 2>(move _20, move _22) -> [return: bb6, unwind continue]; // scope 5 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    }
    bb6: {
        StorageDead(_23);                // scope 5 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_22);                // scope 5 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_21);                // scope 5 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_20);                // scope 5 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        _10 = std::io::_print(move _11) -> [return: bb7, unwind continue]; // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
    }
    bb7: {
        StorageDead(_11);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:61: 146:62
        StorageDead(_15);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        StorageDead(_12);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        StorageDead(_10);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        _9 = const ();                   // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
        StorageDead(_9);                 // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:147:5: 147:6
        _0 = copy _8;                    // scope 3 at src/main.rs:152:5: 152:6
        StorageDead(_8);                 // scope 2 at src/main.rs:153:1: 153:2
        StorageDead(_7);                 // scope 1 at src/main.rs:153:1: 153:2
        StorageDead(_2);                 // scope 0 at src/main.rs:153:1: 153:2
        return;                          // scope 0 at src/main.rs:153:2: 153:2
    }
}
alloc7 (size: 7, align: 1) {
    c0 01 20 c0 01 0a 00                            │ .. ....
}
"""

LENT = """// MIR for `lent` after PreCodegen

fn lent(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:155:9: 155:10
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:155:20: 155:23
    let mut _2: u32;                     // in scope 0 at src/main.rs:156:9: 156:14
    let mut _3: u32;                     // in scope 0 at src/main.rs:156:17: 156:18
    let mut _4: (u32, bool);             // in scope 0 at src/main.rs:156:17: 156:22
    let _5: &mut u32;                    // in scope 0 at src/main.rs:157:5: 157:22
    let mut _6: &mut u32;                // in scope 0 at src/main.rs:157:15: 157:21
    let mut _10: &u32;                   // in scope 0 at src/main.rs:160:21: 160:23
    let _11: &u32;                       // in scope 0 at src/main.rs:160:21: 160:23
    let _12: ();                         // in scope 0 at src/main.rs:161:5: 161:14
    let mut _13: u32;                    // in scope 0 at src/main.rs:161:12: 161:13
    let _14: ();                         // in scope 0 at src/main.rs:162:5: 162:17
    let mut _15: u32;                    // in scope 0 at src/main.rs:162:15: 162:16
    let mut _16: u32;                    // in scope 0 at src/main.rs:163:5: 163:9
    let mut _17: u32;                    // in scope 0 at src/main.rs:163:12: 163:13
    let mut _18: (u32, bool);            // in scope 0 at src/main.rs:163:5: 163:13
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:156:9: 156:14
        let _7: u32;                     // in scope 1 at src/main.rs:158:9: 158:10
        scope 2 {
            debug z => _7;               // in scope 2 at src/main.rs:158:9: 158:10
            let _8: u32;                 // in scope 2 at src/main.rs:159:9: 159:10
            scope 3 {
                debug k => _8;           // in scope 3 at src/main.rs:159:9: 159:10
                let _9: u32;             // in scope 3 at src/main.rs:160:9: 160:13
                scope 4 {
                    debug seen => _9;    // in scope 4 at src/main.rs:160:9: 160:13
                }
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:156:9: 156:14
        StorageLive(_3);                 // scope 0 at src/main.rs:156:17: 156:18
        _3 = copy _1;                    // scope 0 at src/main.rs:156:17: 156:18
        _4 = AddWithOverflow(copy _3, const 1_u32); // scope 0 at src/main.rs:156:17: 156:22
        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:156:17: 156:22
    }
    bb1: {
        _2 = move (_4.0: u32);           // scope 0 at src/main.rs:156:17: 156:22
        StorageDead(_3);                 // scope 0 at src/main.rs:156:21: 156:22
        StorageLive(_5);                 // scope 1 at src/main.rs:157:5: 157:22
        StorageLive(_6);                 // scope 1 at src/main.rs:157:15: 157:21
        _6 = &mut _2;                    // scope 1 at src/main.rs:157:15: 157:21
        _5 = std::hint::black_box::<&mut u32>(move _6) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:157:5: 157:22
    }
    bb2: {
        StorageDead(_6);                 // scope 1 at src/main.rs:157:21: 157:22
        StorageDead(_5);                 // scope 1 at src/main.rs:157:22: 157:23
        StorageLive(_7);                 // scope 1 at src/main.rs:158:9: 158:10
        _7 = copy _2;                    // scope 1 at src/main.rs:158:13: 158:14
        StorageLive(_8);                 // scope 2 at src/main.rs:159:9: 159:10
        _8 = other() -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:159:13: 159:20
    }
    bb3: {
        StorageLive(_9);                 // scope 3 at src/main.rs:160:9: 160:13
        StorageLive(_10);                // scope 3 at src/main.rs:160:21: 160:23
        StorageLive(_11);                // scope 3 at src/main.rs:160:21: 160:23
        _11 = &_2;                       // scope 3 at src/main.rs:160:21: 160:23
        _10 = &(*_11);                   // scope 3 at src/main.rs:160:21: 160:23
        _9 = look(move _10) -> [return: bb4, unwind continue]; // scope 3 at src/main.rs:160:16: 160:24
    }
    bb4: {
        StorageDead(_10);                // scope 3 at src/main.rs:160:23: 160:24
        StorageDead(_11);                // scope 3 at src/main.rs:160:24: 160:25
        StorageLive(_12);                // scope 4 at src/main.rs:161:5: 161:14
        StorageLive(_13);                // scope 4 at src/main.rs:161:12: 161:13
        _13 = copy _7;                   // scope 4 at src/main.rs:161:12: 161:13
        _12 = record(move _13) -> [return: bb5, unwind continue]; // scope 4 at src/main.rs:161:5: 161:14
    }
    bb5: {
        StorageDead(_13);                // scope 4 at src/main.rs:161:13: 161:14
        StorageDead(_12);                // scope 4 at src/main.rs:161:14: 161:15
        StorageLive(_14);                // scope 4 at src/main.rs:162:5: 162:17
        StorageLive(_15);                // scope 4 at src/main.rs:162:15: 162:16
        _15 = copy _8;                   // scope 4 at src/main.rs:162:15: 162:16
        _14 = may_panic(move _15) -> [return: bb6, unwind continue]; // scope 4 at src/main.rs:162:5: 162:17
    }
    bb6: {
        StorageDead(_15);                // scope 4 at src/main.rs:162:16: 162:17
        StorageDead(_14);                // scope 4 at src/main.rs:162:17: 162:18
        StorageLive(_16);                // scope 4 at src/main.rs:163:5: 163:9
        _16 = copy _9;                   // scope 4 at src/main.rs:163:5: 163:9
        StorageLive(_17);                // scope 4 at src/main.rs:163:12: 163:13
        _17 = copy _2;                   // scope 4 at src/main.rs:163:12: 163:13
        _18 = AddWithOverflow(copy _16, copy _17); // scope 4 at src/main.rs:163:5: 163:13
        assert(!move (_18.1: bool), "attempt to compute `{} + {}`, which would overflow", move _16, move _17) -> [success: bb7, unwind continue]; // scope 4 at src/main.rs:163:5: 163:13
    }
    bb7: {
        _0 = move (_18.0: u32);          // scope 4 at src/main.rs:163:5: 163:13
        StorageDead(_17);                // scope 4 at src/main.rs:163:12: 163:13
        StorageDead(_16);                // scope 4 at src/main.rs:163:12: 163:13
        StorageDead(_9);                 // scope 3 at src/main.rs:164:1: 164:2
        StorageDead(_8);                 // scope 2 at src/main.rs:164:1: 164:2
        StorageDead(_7);                 // scope 1 at src/main.rs:164:1: 164:2
        StorageDead(_2);                 // scope 0 at src/main.rs:164:1: 164:2
        return;                          // scope 0 at src/main.rs:164:2: 164:2
    }
}
"""

BRANCH = """// MIR for `branch` after PreCodegen

fn branch(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:166:11: 166:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:166:22: 166:25
    let mut _2: u32;                     // in scope 0 at src/main.rs:167:9: 167:18
    let mut _3: u32;                     // in scope 0 at src/main.rs:167:21: 167:22
    let mut _4: (u32, bool);             // in scope 0 at src/main.rs:167:21: 167:26
    let _6: ();                          // in scope 0 at src/main.rs:169:5: 171:6
    let mut _7: bool;                    // in scope 0 at src/main.rs:169:8: 169:15
    let mut _8: u32;                     // in scope 0 at src/main.rs:169:8: 169:9
    let mut _9: !;                       // in scope 0 at src/main.rs:169:16: 171:6
    let _10: !;                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:59:21: 63:6
    let _11: !;                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
    let mut _12: std::fmt::Arguments<'_>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
    let mut _13: u32;                    // in scope 0 at src/main.rs:172:13: 172:14
    let mut _14: u32;                    // in scope 0 at src/main.rs:172:17: 172:18
    let mut _15: (u32, bool);            // in scope 0 at src/main.rs:172:13: 172:18
    scope 1 {
        debug total => _2;               // in scope 1 at src/main.rs:167:9: 167:18
        let _5: u32;                     // in scope 1 at src/main.rs:168:9: 168:10
        scope 2 {
            debug k => _5;               // in scope 2 at src/main.rs:168:9: 168:10
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:167:9: 167:18
        StorageLive(_3);                 // scope 0 at src/main.rs:167:21: 167:22
        _3 = copy _1;                    // scope 0 at src/main.rs:167:21: 167:22
        _4 = AddWithOverflow(copy _3, const 1_u32); // scope 0 at src/main.rs:167:21: 167:26
        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:167:21: 167:26
    }
    bb1: {
        _2 = move (_4.0: u32);           // scope 0 at src/main.rs:167:21: 167:26
        StorageDead(_3);                 // scope 0 at src/main.rs:167:25: 167:26
        StorageLive(_5);                 // scope 1 at src/main.rs:168:9: 168:10
        _5 = other() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:168:13: 168:20
    }
    bb2: {
        StorageLive(_6);                 // scope 2 at src/main.rs:169:5: 171:6
        StorageLive(_7);                 // scope 2 at src/main.rs:169:8: 169:15
        StorageLive(_8);                 // scope 2 at src/main.rs:169:8: 169:9
        _8 = copy _1;                    // scope 2 at src/main.rs:169:8: 169:9
        _7 = Gt(move _8, const 100_u32); // scope 2 at src/main.rs:169:8: 169:15
        switchInt(move _7) -> [0: bb5, otherwise: bb3]; // scope 2 at src/main.rs:169:8: 169:15
    }
    bb3: {
        StorageDead(_8);                 // scope 2 at src/main.rs:169:14: 169:15
        StorageLive(_11);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
        StorageLive(_12);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
        _12 = Arguments::<'_>::from_str(const "too many") -> [return: bb4, unwind continue]; // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
    }
    bb4: {
        _11 = std::rt::panic_fmt(move _12) -> unwind continue; // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
    }
    bb5: {
        StorageDead(_8);                 // scope 2 at src/main.rs:169:14: 169:15
        _6 = const ();                   // scope 2 at src/main.rs:171:6: 171:6
        StorageDead(_7);                 // scope 2 at src/main.rs:171:5: 171:6
        StorageDead(_6);                 // scope 2 at src/main.rs:171:5: 171:6
        StorageLive(_13);                // scope 2 at src/main.rs:172:13: 172:14
        _13 = copy _5;                   // scope 2 at src/main.rs:172:13: 172:14
        StorageLive(_14);                // scope 2 at src/main.rs:172:17: 172:18
        _14 = copy _1;                   // scope 2 at src/main.rs:172:17: 172:18
        _15 = AddWithOverflow(copy _13, copy _14); // scope 2 at src/main.rs:172:13: 172:18
        assert(!move (_15.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _14) -> [success: bb6, unwind continue]; // scope 2 at src/main.rs:172:13: 172:18
    }
    bb6: {
        _2 = move (_15.0: u32);          // scope 2 at src/main.rs:172:5: 172:18
        StorageDead(_14);                // scope 2 at src/main.rs:172:17: 172:18
        StorageDead(_13);                // scope 2 at src/main.rs:172:17: 172:18
        _0 = copy _2;                    // scope 2 at src/main.rs:173:5: 173:10
        StorageDead(_5);                 // scope 1 at src/main.rs:174:1: 174:2
        StorageDead(_2);                 // scope 0 at src/main.rs:174:1: 174:2
        return;                          // scope 0 at src/main.rs:174:2: 174:2
    }
}
alloc8 (size: 8, align: 1) {
    74 6f 6f 20 6d 61 6e 79                         │ too many
}
"""

SPIN = """// MIR for `spin` after PreCodegen

fn spin(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:176:9: 176:10
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:176:20: 176:23
    let mut _2: u32;                     // in scope 0 at src/main.rs:177:9: 177:18
    let mut _3: u32;                     // in scope 0 at src/main.rs:177:21: 177:22
    let mut _4: (u32, bool);             // in scope 0 at src/main.rs:177:21: 177:26
    let _6: ();                          // in scope 0 at src/main.rs:179:5: 183:6
    let mut _7: bool;                    // in scope 0 at src/main.rs:179:8: 179:15
    let mut _8: u32;                     // in scope 0 at src/main.rs:179:8: 179:9
    let mut _9: !;                       // in scope 0 at src/main.rs:180:9: 182:10
    let mut _10: ();                     // in scope 0 at src/main.rs:176:1: 186:2
    let _11: u32;                        // in scope 0 at src/main.rs:181:13: 181:25
    let mut _12: u32;                    // in scope 0 at src/main.rs:181:23: 181:24
    let mut _13: u32;                    // in scope 0 at src/main.rs:184:13: 184:14
    let mut _14: u32;                    // in scope 0 at src/main.rs:184:17: 184:18
    let mut _15: (u32, bool);            // in scope 0 at src/main.rs:184:13: 184:18
    scope 1 {
        debug total => _2;               // in scope 1 at src/main.rs:177:9: 177:18
        let _5: u32;                     // in scope 1 at src/main.rs:178:9: 178:10
        scope 2 {
            debug k => _5;               // in scope 2 at src/main.rs:178:9: 178:10
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:177:9: 177:18
        StorageLive(_3);                 // scope 0 at src/main.rs:177:21: 177:22
        _3 = copy _1;                    // scope 0 at src/main.rs:177:21: 177:22
        _4 = AddWithOverflow(copy _3, const 1_u32); // scope 0 at src/main.rs:177:21: 177:26
        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:177:21: 177:26
    }
    bb1: {
        _2 = move (_4.0: u32);           // scope 0 at src/main.rs:177:21: 177:26
        StorageDead(_3);                 // scope 0 at src/main.rs:177:25: 177:26
        StorageLive(_5);                 // scope 1 at src/main.rs:178:9: 178:10
        _5 = other() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:178:13: 178:20
    }
    bb2: {
        StorageLive(_6);                 // scope 2 at src/main.rs:179:5: 183:6
        StorageLive(_7);                 // scope 2 at src/main.rs:179:8: 179:15
        StorageLive(_8);                 // scope 2 at src/main.rs:179:8: 179:9
        _8 = copy _1;                    // scope 2 at src/main.rs:179:8: 179:9
        _7 = Gt(move _8, const 100_u32); // scope 2 at src/main.rs:179:8: 179:15
        switchInt(move _7) -> [0: bb6, otherwise: bb3]; // scope 2 at src/main.rs:179:8: 179:15
    }
    bb3: {
        StorageDead(_8);                 // scope 2 at src/main.rs:179:14: 179:15
        StorageLive(_9);                 // scope 2 at src/main.rs:180:9: 182:10
        goto -> bb4;                     // scope 2 at src/main.rs:180:9: 182:10
    }
    bb4: {
        StorageLive(_11);                // scope 2 at src/main.rs:181:13: 181:25
        StorageLive(_12);                // scope 2 at src/main.rs:181:23: 181:24
        _12 = copy _5;                   // scope 2 at src/main.rs:181:23: 181:24
        _11 = std::hint::black_box::<u32>(move _12) -> [return: bb5, unwind continue]; // scope 2 at src/main.rs:181:13: 181:25
    }
    bb5: {
        StorageDead(_12);                // scope 2 at src/main.rs:181:24: 181:25
        StorageDead(_11);                // scope 2 at src/main.rs:181:25: 181:26
        _10 = const ();                  // scope 2 at src/main.rs:180:14: 182:10
        goto -> bb4;                     // scope 2 at src/main.rs:180:9: 182:10
    }
    bb6: {
        StorageDead(_8);                 // scope 2 at src/main.rs:179:14: 179:15
        _6 = const ();                   // scope 2 at src/main.rs:183:6: 183:6
        StorageDead(_7);                 // scope 2 at src/main.rs:183:5: 183:6
        StorageDead(_6);                 // scope 2 at src/main.rs:183:5: 183:6
        StorageLive(_13);                // scope 2 at src/main.rs:184:13: 184:14
        _13 = copy _5;                   // scope 2 at src/main.rs:184:13: 184:14
        StorageLive(_14);                // scope 2 at src/main.rs:184:17: 184:18
        _14 = copy _1;                   // scope 2 at src/main.rs:184:17: 184:18
        _15 = AddWithOverflow(copy _13, copy _14); // scope 2 at src/main.rs:184:13: 184:18
        assert(!move (_15.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _14) -> [success: bb7, unwind continue]; // scope 2 at src/main.rs:184:13: 184:18
    }
    bb7: {
        _2 = move (_15.0: u32);          // scope 2 at src/main.rs:184:5: 184:18
        StorageDead(_14);                // scope 2 at src/main.rs:184:17: 184:18
        StorageDead(_13);                // scope 2 at src/main.rs:184:17: 184:18
        _0 = copy _2;                    // scope 2 at src/main.rs:185:5: 185:10
        StorageDead(_5);                 // scope 1 at src/main.rs:186:1: 186:2
        StorageDead(_2);                 // scope 0 at src/main.rs:186:1: 186:2
        return;                          // scope 0 at src/main.rs:186:2: 186:2
    }
}
"""

CLEANED = """// MIR for `cleaned` after PreCodegen

fn cleaned(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:188:12: 188:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:188:23: 188:26
    let mut _2: u32;                     // in scope 0 at src/main.rs:189:9: 189:18
    let mut _3: u32;                     // in scope 0 at src/main.rs:189:21: 189:22
    let mut _4: (u32, bool);             // in scope 0 at src/main.rs:189:21: 189:26
    let mut _6: &u32;                    // in scope 0 at src/main.rs:190:17: 190:18
    let mut _8: u32;                     // in scope 0 at src/main.rs:192:13: 192:14
    let mut _9: u32;                     // in scope 0 at src/main.rs:192:17: 192:18
    let mut _10: (u32, bool);            // in scope 0 at src/main.rs:192:13: 192:18
    let mut _11: u32;                    // in scope 0 at src/main.rs:193:5: 193:23
    let mut _12: usize;                  // in scope 0 at src/main.rs:193:5: 193:16
    let mut _13: &std::string::String;   // in scope 0 at src/main.rs:193:5: 193:10
    let mut _14: u32;                    // in scope 0 at src/main.rs:193:26: 193:31
    let mut _15: (u32, bool);            // in scope 0 at src/main.rs:193:5: 193:31
    scope 1 {
        debug total => _2;               // in scope 1 at src/main.rs:189:9: 189:18
        let _5: std::string::String;     // in scope 1 at src/main.rs:190:9: 190:14
        scope 2 {
            debug label => _5;           // in scope 2 at src/main.rs:190:9: 190:14
            let _7: u32;                 // in scope 2 at src/main.rs:191:9: 191:10
            scope 3 {
                debug k => _7;           // in scope 3 at src/main.rs:191:9: 191:10
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:189:9: 189:18
        StorageLive(_3);                 // scope 0 at src/main.rs:189:21: 189:22
        _3 = copy _1;                    // scope 0 at src/main.rs:189:21: 189:22
        _4 = AddWithOverflow(copy _3, const 1_u32); // scope 0 at src/main.rs:189:21: 189:26
        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:189:21: 189:26
    }
    bb1: {
        _2 = move (_4.0: u32);           // scope 0 at src/main.rs:189:21: 189:26
        StorageDead(_3);                 // scope 0 at src/main.rs:189:25: 189:26
        StorageLive(_5);                 // scope 1 at src/main.rs:190:9: 190:14
        StorageLive(_6);                 // scope 1 at src/main.rs:190:17: 190:18
        _6 = &_1;                        // scope 1 at src/main.rs:190:17: 190:18
        _5 = <u32 as ToString>::to_string(move _6) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:190:17: 190:30
    }
    bb2: {
        StorageDead(_6);                 // scope 1 at src/main.rs:190:29: 190:30
        StorageLive(_7);                 // scope 2 at src/main.rs:191:9: 191:10
        _7 = other() -> [return: bb3, unwind: bb8]; // scope 2 at src/main.rs:191:13: 191:20
    }
    bb3: {
        StorageLive(_8);                 // scope 3 at src/main.rs:192:13: 192:14
        _8 = copy _7;                    // scope 3 at src/main.rs:192:13: 192:14
        StorageLive(_9);                 // scope 3 at src/main.rs:192:17: 192:18
        _9 = copy _1;                    // scope 3 at src/main.rs:192:17: 192:18
        _10 = AddWithOverflow(copy _8, copy _9); // scope 3 at src/main.rs:192:13: 192:18
        assert(!move (_10.1: bool), "attempt to compute `{} + {}`, which would overflow", move _8, move _9) -> [success: bb4, unwind: bb8]; // scope 3 at src/main.rs:192:13: 192:18
    }
    bb4: {
        _2 = move (_10.0: u32);          // scope 3 at src/main.rs:192:5: 192:18
        StorageDead(_9);                 // scope 3 at src/main.rs:192:17: 192:18
        StorageDead(_8);                 // scope 3 at src/main.rs:192:17: 192:18
        StorageLive(_11);                // scope 3 at src/main.rs:193:5: 193:23
        StorageLive(_12);                // scope 3 at src/main.rs:193:5: 193:16
        StorageLive(_13);                // scope 3 at src/main.rs:193:5: 193:10
        _13 = &_5;                       // scope 3 at src/main.rs:193:5: 193:10
        _12 = String::len(move _13) -> [return: bb5, unwind: bb8]; // scope 3 at src/main.rs:193:5: 193:16
    }
    bb5: {
        StorageDead(_13);                // scope 3 at src/main.rs:193:15: 193:16
        _11 = move _12 as u32 (IntToInt); // scope 3 at src/main.rs:193:5: 193:23
        StorageDead(_12);                // scope 3 at src/main.rs:193:22: 193:23
        StorageLive(_14);                // scope 3 at src/main.rs:193:26: 193:31
        _14 = copy _2;                   // scope 3 at src/main.rs:193:26: 193:31
        _15 = AddWithOverflow(copy _11, copy _14); // scope 3 at src/main.rs:193:5: 193:31
        assert(!move (_15.1: bool), "attempt to compute `{} + {}`, which would overflow", move _11, move _14) -> [success: bb6, unwind: bb8]; // scope 3 at src/main.rs:193:5: 193:31
    }
    bb6: {
        _0 = move (_15.0: u32);          // scope 3 at src/main.rs:193:5: 193:31
        StorageDead(_14);                // scope 3 at src/main.rs:193:30: 193:31
        StorageDead(_11);                // scope 3 at src/main.rs:193:30: 193:31
        StorageDead(_7);                 // scope 2 at src/main.rs:194:1: 194:2
        drop(_5) -> [return: bb7, unwind: bb9]; // scope 1 at src/main.rs:194:1: 194:2
    }
    bb7: {
        StorageDead(_5);                 // scope 1 at src/main.rs:194:1: 194:2
        StorageDead(_2);                 // scope 0 at src/main.rs:194:1: 194:2
        return;                          // scope 0 at src/main.rs:194:2: 194:2
    }
    bb8 (cleanup): {
        drop(_5) -> [return: bb9, unwind terminate(cleanup)]; // scope 1 at src/main.rs:194:1: 194:2
    }
    bb9 (cleanup): {
        resume;                          // scope 0 at src/main.rs:188:1: 194:2
    }
}
"""

DROPPER = """// MIR for `<impl at src/main.rs:5:1: 5:16>::drop` after PreCodegen

fn <impl at src/main.rs:5:1: 5:16>::drop(_1: &mut D) -> () {
    debug self => _1;                    // in scope 0 at src/main.rs:6:13: 6:22
    let mut _0: ();                      // return place in scope 0 at src/main.rs:6:23: 6:23
    let _2: ();                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
    let _3: ();                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
    let mut _4: std::fmt::Arguments<'_>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let _5: (&u32,);                     // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let mut _6: &u32;                    // in scope 0 at src/main.rs:7:26: 7:32
    let mut _8: core::fmt::rt::Argument<'_>; // in scope 0 at src/main.rs:7:21: 7:23
    let mut _9: &u32;                    // in scope 0 at src/main.rs:7:26: 7:32
    let mut _10: &[u8; 7];               // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let _11: &[u8; 7];                   // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let mut _12: &[core::fmt::rt::Argument<'_>; 1]; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let _13: &[core::fmt::rt::Argument<'_>; 1]; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let mut _14: &u32;                   // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    scope 1 {
        debug args => _5;                // in scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        let _7: [core::fmt::rt::Argument<'_>; 1]; // in scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        scope 2 {
            debug args => _7;            // in scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
        StorageLive(_3);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
        StorageLive(_4);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_5);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_6);                 // scope 0 at src/main.rs:7:26: 7:32
        _6 = &((*_1).0: u32);            // scope 0 at src/main.rs:7:26: 7:32
        _5 = (move _6,);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageDead(_6);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageLive(_7);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_8);                 // scope 1 at src/main.rs:7:21: 7:23
        StorageLive(_9);                 // scope 1 at src/main.rs:7:26: 7:32
        _14 = copy (_5.0: &u32);         // scope 1 at src/main.rs:7:26: 7:32
        _9 = &(*_14);                    // scope 1 at src/main.rs:7:26: 7:32
        _8 = core::fmt::rt::Argument::<'_>::new_display::<u32>(move _9) -> [return: bb1, unwind continue]; // scope 1 at src/main.rs:7:21: 7:23
    }
    bb1: {
        StorageDead(_9);                 // scope 1 at src/main.rs:7:22: 7:23
        _7 = [move _8];                  // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageDead(_8);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageLive(_10);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_11);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _11 = const b""; // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _10 = &(*_11);                   // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_12);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_13);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _13 = &_7;                       // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _12 = &(*_13);                   // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _4 = Arguments::<'_>::new::<7, 1>(move _10, move _12) -> [return: bb2, unwind continue]; // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    }
    bb2: {
        StorageDead(_13);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_12);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_11);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_10);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        _3 = std::io::_print(move _4) -> [return: bb3, unwind continue]; // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
    }
    bb3: {
        StorageDead(_4);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:61: 146:62
        StorageDead(_7);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        StorageDead(_5);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        StorageDead(_3);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        _2 = const ();                   // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
        StorageDead(_2);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:147:5: 147:6
        _0 = const ();                   // scope 0 at src/main.rs:6:24: 8:6
        return;                          // scope 0 at src/main.rs:8:6: 8:6
    }
}
alloc1 (size: 7, align: 1) {
    02 44 20 c0 01 0a 00                            │ .D ....
}
"""


def review_why(text, name, line, statement, variable, projection=(), unsafe=None):
    return why(text, name, line, statement, variable, projection, unsafe, REVIEW_SOURCE, [DROPPER])


def review_line(function, text):
    """The line of ``REVIEW_SOURCE`` in ``function`` that holds ``text``."""
    lines = REVIEW_SOURCE.splitlines()
    begin = next(
        i for i, line in enumerate(lines) if re.match(rf"(?:async )?fn {function}\(", line)
    )
    return next(i for i, line in enumerate(lines[begin:], begin + 1) if text in line)


def review_body(text):
    """``body_of``, matched as ``OwnedLocals`` matches it: `D` has a `Drop` impl of the
    crate's (``DROPPER``), and the crate's types hold what ``LAYOUTS`` says."""
    body = body_of(text)
    body.layouts = TypeLayouts(LAYOUTS, body.closure_captures())
    body.droppers = {"D": parse_body(DROPPER)}
    body.sinks = (frozenset({STD_MACROS}), frozenset())
    return body


def stop_text(text, line):
    """The first statement of ``text`` on ``line``."""
    return stop(text, line, "")


def dropped_at(text, local):
    """The line of the normal path's drop of ``local`` in ``text``."""
    return next(
        at
        for block in parse_body(text).blocks.values()
        for statement, _, at in block.statements
        if statement.startswith(f"drop(_{local}) -> [return:")
    )


def hedged(name, line, copy, drop):
    """A refusal by the copy `z` of ``name``'s field, where the drop may read it."""
    return (
        f"the program has already copied its value into `z` (at line {copy}) and goes on "
        f"with that copy, so a change to `{name}` may never be seen: `{name}` is dropped at "
        f"line {drop}, and a `Drop` it runs may read the changed part, which Peeky can't tell",
        f"copied into `z` (line {copy})",
    )


def test_a_drop_reads_a_part_only_where_its_drop_surely_reads_it():
    # Review 2 of PR #334: `impl Drop for D` prints `self.0` only. A drop counted as a read
    # wherever a `Drop` impl may run, so after `let z = d.1;` a change to `d.1` was
    # `[settable]`, and the program never saw it. Now only a proven read counts.
    first, second = (("field", 0),), (("field", 1),)
    drop = parse_body(DROPPER)
    body = review_body(SCOPED)
    assert body._drop_proves_read(drop, list(first))
    assert not body._drop_proves_read(drop, list(second))
    # `d.0`, a scoped local or a by-value argument: the drop reads it, and the change is
    # seen there.
    for text, name in ((SCOPED, "scoped"), (TAKEN, "taken")):
        line = review_line(name, "let k")
        body = review_body(text)
        at = place(body, stop_text(text, line))
        local = body.bindings["d"][0][0]
        (copy,) = body.direct_copies_at(local, at, projection=first)
        assert not body.copy_undoes(copy, local, at, first), name
        assert not body.doubtful_drops(local, at, first), name
        assert review_why(text, name, line, stop_text(text, line), "d", first) == (None, None)
    # `d.1`: the drop may read it or not, which Peeky can't tell. The copy's refusal stays,
    # and says so instead of proving no read.
    line = review_line("second", "let k")
    body = review_body(SECOND)
    at = place(body, stop_text(SECOND, line))
    local = body.bindings["d"][0][0]
    (copy,) = body.direct_copies_at(local, at, projection=second)
    assert body.copy_undoes(copy, local, at, second)
    assert {block for block, _ in body.doubtful_drops(local, at, second)}
    assert review_why(SECOND, "second", line, stop_text(SECOND, line), "d", second) == hedged(
        "d", line, line - 1, dropped_at(SECOND, local)
    )


def test_a_drop_on_another_field_or_none_keeps_the_refusal_as_it_was():
    # `Holder { d: D, n: u32 }`: `D`'s drop can't reach `h.n`. `Wrap { v: Vec<D>, n: u32 }`
    # and `S { name: String, n: u32 }` likewise. Each refusal is the plain one.
    for text, name, variable, part in (
        (HELD, "held", "h", (("field", 1),)),
        (WRAPPED, "wrapped", "w", (("field", 1),)),
        (NAMED, "named", "s", (("field", 1),)),
    ):
        line = review_line(name, "let k")
        body = review_body(text)
        local = body.bindings[variable][0][0]
        drop = f"drop(_{local}) -> [return: bb1, unwind: bb2];"
        assert body._drop_effect(drop, local, part) is None, name
        assert review_why(text, name, line, stop_text(text, line), variable, part) == (
            f"the program has already copied its value into `z` (at line {line - 1}) and goes "
            f"on with that copy, " + SEEN.format(variable),
            f"copied into `z` (line {line - 1})",
        ), name
    # Through the field path from the local: `h.d.0` is what `D`'s drop reads, so the change
    # is seen there; `h.d.1` it may read or not.
    body = review_body(HELD)
    local = body.bindings["h"][0][0]
    drop = f"drop(_{local}) -> [return: bb1, unwind: bb2];"
    assert body._drop_effect(drop, local, (("field", 0), ("field", 0))) == "read"
    assert body._drop_effect(drop, local, (("field", 0), ("field", 1))) == "maybe"
    line = review_line("held", "let k")
    assert review_why(
        HELD, "held", line, stop_text(HELD, line), "h", (("field", 0), ("field", 0))
    ) == (
        None,
        None,
    )
    reason, row = review_why(
        HELD, "held", line, stop_text(HELD, line), "h", (("field", 0), ("field", 1))
    )
    # `other()` may panic, but its cleanup drops `h` too: every path ends at a drop.
    assert reason == (
        "the program may not read it after this stop, so a change may never be seen: `h` is "
        f"dropped at line {dropped_at(HELD, local)} first; a `Drop` that drop runs may read the "
        "changed part, which Peeky can't tell"
    ), reason
    assert row == f"maybe not read after the stop (dropped at line {dropped_at(HELD, local)})"


def test_wrappers_that_drop_nothing_hold_no_drop():
    # Where Peeky can't tell which types are on the way to the part, it asks whether the
    # local holds a type with a `Drop` impl by value: through a field, a tuple, an array
    # or a standard container, but not `ManuallyDrop`, `MaybeUninit` or `PhantomData`.
    body = review_body(HELD)
    d = ("type", "D", ())
    for node, holds in (
        (("std", "mem::ManuallyDrop", (d,)), False),
        (("std", "mem::MaybeUninit", (d,)), False),
        (("std", "marker::PhantomData", (d,)), False),
        (("std", "vec::Vec", (d,)), True),
        (("std", "boxed::Box", (d,)), True),
        (("tuple", [("prim", "u32"), d]), True),
        (("array", d, 2), True),
        (("ref", False, d), False),
        (_parse("std::mem::ManuallyDrop<D>"), False),
        (_parse("core::mem::MaybeUninit<D>"), False),
        (_parse("std::marker::PhantomData<D>"), False),
        (_parse("std::vec::Vec<D>"), True),
        (_parse("Wrap"), True),
        (_parse("Holder"), True),
        (_parse("S"), False),
        (_parse("Other"), True),
    ):
        assert body._holds_dropper(node) == holds, node
    # So a part Peeky can't place is judged by it.
    body.types[40] = "S"
    assert body._drop_effect("drop(_40) -> [return: bb1, unwind: bb2];", 40, None) is None
    body.types[41] = "Wrap"
    assert body._drop_effect("drop(_41) -> [return: bb1, unwind: bb2];", 41, None) == "maybe"
    # Which types have a `Drop` impl: `D`'s, from its `drop`'s `self`.
    line = review_line("named", "let k")
    owned = owned_of(NAMED, "named", stop_text(NAMED, line), source=REVIEW_SOURCE, extra=[DROPPER])
    assert set(owned._droppers()) == {"D"}


def _parse(text):
    from peeky.rust_mir import _parse_type

    return _parse_type(text)


def test_every_exit_of_the_frame_ends_its_locals_and_arguments():
    # `let z = x;`, the stop, `black_box(z + k);`, then `panic!` or `process::exit`: `x` is
    # never read again, and the program goes on with `z`. Only a `return;` ended an
    # argument, and a local only its `StorageDead`, which a panic's path has none of.
    for text, name, unsafe in (
        (PANICKED, "panicked", None),
        # A `String` beside it: the panic goes through a cleanup block to `resume;`.
        (UNWOUND, "unwound", None),
        # Where the crate has unsafe code, the never-read rule says nothing.
        (PANICKED, "panicked", "the crate has unsafe code"),
        # And where a pointer into it may still be used (`black_box(r);`).
        (POINTED, "pointed", None),
    ):
        line = review_line(name, "let k")
        body = review_body(text)
        at = place(body, stop_text(text, line))
        local = body.bindings["x"][0][0]
        (copy,) = body.direct_copies_at(local, at)
        assert body.copy_undoes(copy, local, at), name
        assert review_why(text, name, line, stop_text(text, line), "x", unsafe=unsafe) == (
            f"the program has already copied its value into `z` (at line {line - 1}) and goes "
            "on with that copy, " + SEEN.format("x"),
            f"copied into `z` (line {line - 1})",
        ), name
    line = review_line("pointed", "let k")
    owned = owned_of(POINTED, "pointed", stop_text(POINTED, line), source=REVIEW_SOURCE)
    assert owned.unread_after(HASH, "x", line, "/w/src/main.rs") is None
    # An async fn's body (a coroutine), where the never-read rule says nothing either.
    body = review_body(EXITED)
    at = place(body, stop_text(EXITED, review_line("exited", "let k")))
    local = body.bindings["x"][0][0]
    (copy,) = body.direct_copies_at(local, at)
    assert body.copy_undoes(copy, local, at)


def test_a_panic_that_leaves_the_frame_ends_the_local():
    # Review 2: `let z = x;`, the stop, `use_copy(z); may_panic(); use_root(x);`. Where
    # `may_panic` panics and a caller's `catch_unwind` goes on, the program has used `z`
    # and never reads `x`: the panic ends `x` too, with no cleanup of its own (`unwind
    # continue`) or through one. Here `y + u[0]`'s overflow check comes after `u` is read.
    body = review_body(PUSHED)
    assert any(block.leaves for block in body.blocks.values())
    at_11 = place(body, "_9 = copy _1;")
    assert body.copy_undoes(5, 2, at_11)
    # `w.push(x)`, then `x + k` reads `x` before `w` is used: no panic between uses `w`.
    body = review_body(LATER)
    at = place(body, stop(LATER, 28, "_"))
    assert not body.copy_undoes(5, 2, at)
    # A panic before the copy is used ends nothing that matters.
    body = review_body(REPLACED)
    at = place(body, stop(REPLACED, 19, "StorageLive("))
    assert body.copy_undoes(body.bindings["z"][0][0], 2, at)


def test_where_a_value_ends_is_named_only_where_every_path_reaches_it():
    # Every path that may panic, exit or loop on the way is named as such: a `panic!`
    # branch, a loop with no way out, a call's panic through a cleanup block (`other()`,
    # beside a `String` to drop), and a loop with a way out, which Peeky can't prove ends.
    def reason(text, name):
        line = review_line(name, "let k")
        return review_why(text, name, line, stop_text(text, line), "total")

    for text, name, kinds, other in (
        (BRANCH, "branch", {"write", "stop"}, "panics or exits"),
        (SPIN, "spin", {"write", "stop", "loop"}, "panics, exits or loops forever"),
        (CLEANED, "cleaned", {"write", "stop"}, "panics or exits"),
    ):
        body = review_body(text)
        at = place(body, stop_text(text, review_line(name, "let k")))
        assert {each for each, _ in body.unread_ends(2, at)} == kinds, name
        write = review_line(name, "total = k + n;")
        assert reason(text, name) == (
            "the program does not read it after this stop, so a change would never be seen: it "
            f"is overwritten at line {write} first, unless the program {other} before that",
            f"not read after the stop (overwritten at line {write}, or the program {other})",
        ), name
    # From the write's own line, with nothing that may panic before it, the line is named
    # plainly.
    body = review_body(CLEANED)
    write = review_line("cleaned", "total = k + n;")
    at = place(body, stop_text(CLEANED, write))
    assert {each for each, _ in body.unread_ends(2, at)} <= {"write", "stop"}
    from peeky.rust import _unread_where

    assert _unread_where([("write", "src/main.rs", 7)], "src/main.rs") == (
        "it is overwritten at line 7 first"
    )
    assert _unread_where([("stop", "", 0)], "src/main.rs") == "the program panics or exits first"
    assert _unread_where([("stop", "", 0), ("loop", "", 0)], "src/main.rs") == (
        "the program panics, exits or loops forever first"
    )


def test_a_caught_panic_goes_on_only_with_what_left_the_frame():
    # Review 2: `let z = x;`, the stop, `record(z); may_panic(k); x`. Where `may_panic`
    # panics and a caller's `catch_unwind` goes on, `record` has taken `z`, and `x` is never
    # read: refused. A panic that leaves the frame ended nothing before.
    for text, name, settable in (
        (CAUGHT, "caught", False),
        # `z * 10`'s overflow check may panic, but nothing took `z` out of the frame first.
        (CHECKED, "checked", True),
        # `println!("{z} {x}")` reads `x` at the print, its formatting calls aside.
        (PRINTED, "printed", True),
        # `look(&x)` was given `x` before the panic, and may have read it.
        (LENT, "lent", True),
    ):
        line = review_line(name, "let k")
        body = review_body(text)
        at = place(body, stop_text(text, line))
        local = body.bindings["x"][0][0]
        (copy,) = body.direct_copies_at(local, at)
        # With std's printing known for what it reads (`_format_reads`'s sinks).
        assert body.copy_undoes(copy, local, at, (), body.sinks) is not settable, name
        reason = review_why(text, name, line, stop_text(text, line), "x")
        if settable:
            assert reason == (None, None), (name, reason)
        else:
            assert reason == (
                f"the program has already copied its value into `z` (at line {line - 1}) and "
                "goes on with that copy, " + SEEN.format("x"),
                f"copied into `z` (line {line - 1})",
            ), name
    # Which panics leave the frame: `unwind continue`, and a cleanup block's `resume`.
    assert any(block.leaves for block in review_body(CAUGHT).blocks.values())
