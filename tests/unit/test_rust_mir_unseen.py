"""#330 (found preparing the twelfth evaluation): after the program copied a local, a change to
it was `[settable]` where the local was then replaced or ended before the copy was used.
`u.push(y); let t = n; y = t;`, then `u` printed: `--set y=100` at `let t = n;` was reported,
and the output kept the old value. The copy rule (#192) counted a copy only where it was used
before the local was replaced or ended. Now a use after counts too, and an argument ends where
the function returns. A value no path reads again (it is overwritten, or it ends, first) is
refused too, saying where; a local the program reads again before replacing it stays
settable."""

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
    name: CrateType(payloads=frozenset(), parts=frozenset({("prim", "u32")}))
    for name in ("Lap", "D")
}


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


def owned_of(text, name, statement, unsafe=None, source=None, parent="units"):
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
    owned = OwnedLocals([body], {HASH: function}, ("src/main.rs",), texts=texts)
    owned.unsafe_code = unsafe
    owned.interior = LAYOUTS
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


def why(text, name, line, statement, variable, projection=(), unsafe=None, source=None):
    """Why a change to ``variable`` (the part ``projection`` of it) is refused at a stop on
    ``line`` of the function ``name`` of ``text``, before its statement starting
    ``statement`` (``mid_statement``'s reason): (the reason, its row's short form), or (None,
    None) where it is allowed."""
    owned = owned_of(text, name, statement, unsafe, source)
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
    # from line 45, nor from line 46's start, reads `total` before line 46 assigns it.
    for line, statement in ((45, "StorageLive(_5)"), (46, "StorageLive(_8)")):
        assert why(UNREAD, "unread", line, stop(UNREAD, line, statement), "total") == (
            "the program does not read it after this stop, so a change would never be seen: it "
            "is overwritten at line 46 first",
            "not read after the stop (overwritten at line 46)",
        ), line
    body = body_of(UNREAD)
    write = place(body, "_2 = move (_9.0: u32);")
    assert body.unread_ends(2, place(body, "_6 = copy _1;")) == {("write", write)}
    # Past line 46, line 56 reads it.
    assert body.unread_ends(2, place(body, "_10 = const 0_u32;")) is None
    # `a` (49) is read through `r` on line 53: not while `r` may still be used.
    owned = owned_of(UNREAD, "unread", "_20 = copy (*_19);")
    assert body.unread_ends(14, place(body, "_20 = copy (*_19);")) == {
        ("scope", place(body, "StorageDead(_14);"))
    }
    assert owned.unread_after(HASH, "a", 53, "/w/src/main.rs") is None
    assert body.unread_ends(14, place(body, "_19 = &_14;")) is None
    # On line 54, `r` is done with: `a` goes out of scope at line 55, unread.
    owned = owned_of(UNREAD, "unread", "_22 = AddWithOverflow")
    assert owned.unread_after(HASH, "a", 54, "/w/src/main.rs") == [("scope", "src/main.rs", 55)]
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
        ("drop(_1) -> [return: bb1, unwind: bb2];", 1, field, "read"),
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
        "{} {} {} {} {} {} {} {}",
        scoped(n),
        taken(D(n, n)),
        branch(n),
        spin(n),
        cleaned(n),
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

TAKEN = """// MIR for `taken` after PreCodegen

fn taken(_1: D) -> u32 {
    debug d => _1;                       // in scope 0 at src/main.rs:23:10: 23:11
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:23:19: 23:22
    let _2: u32;                         // in scope 0 at src/main.rs:24:9: 24:10
    let mut _4: u32;                     // in scope 0 at src/main.rs:26:5: 26:6
    let mut _5: u32;                     // in scope 0 at src/main.rs:26:9: 26:10
    let mut _6: (u32, bool);             // in scope 0 at src/main.rs:26:5: 26:10
    scope 1 {
        debug z => _2;                   // in scope 1 at src/main.rs:24:9: 24:10
        let _3: u32;                     // in scope 1 at src/main.rs:25:9: 25:10
        scope 2 {
            debug k => _3;               // in scope 2 at src/main.rs:25:9: 25:10
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:24:9: 24:10
        _2 = copy (_1.0: u32);           // scope 0 at src/main.rs:24:13: 24:16
        StorageLive(_3);                 // scope 1 at src/main.rs:25:9: 25:10
        _3 = other() -> [return: bb1, unwind: bb4]; // scope 1 at src/main.rs:25:13: 25:20
    }
    bb1: {
        StorageLive(_4);                 // scope 2 at src/main.rs:26:5: 26:6
        _4 = copy _2;                    // scope 2 at src/main.rs:26:5: 26:6
        StorageLive(_5);                 // scope 2 at src/main.rs:26:9: 26:10
        _5 = copy _3;                    // scope 2 at src/main.rs:26:9: 26:10
        _6 = AddWithOverflow(copy _4, copy _5); // scope 2 at src/main.rs:26:5: 26:10
        assert(!move (_6.1: bool), "attempt to compute `{} + {}`, which would overflow", move _4, move _5) -> [success: bb2, unwind: bb4]; // scope 2 at src/main.rs:26:5: 26:10
    }
    bb2: {
        _0 = move (_6.0: u32);           // scope 2 at src/main.rs:26:5: 26:10
        StorageDead(_5);                 // scope 2 at src/main.rs:26:9: 26:10
        StorageDead(_4);                 // scope 2 at src/main.rs:26:9: 26:10
        StorageDead(_3);                 // scope 1 at src/main.rs:27:1: 27:2
        StorageDead(_2);                 // scope 0 at src/main.rs:27:1: 27:2
        drop(_1) -> [return: bb3, unwind: bb5]; // scope 0 at src/main.rs:27:1: 27:2
    }
    bb3: {
        return;                          // scope 0 at src/main.rs:27:2: 27:2
    }
    bb4 (cleanup): {
        drop(_1) -> [return: bb5, unwind terminate(cleanup)]; // scope 0 at src/main.rs:27:1: 27:2
    }
    bb5 (cleanup): {
        resume;                          // scope 0 at src/main.rs:23:1: 27:2
    }
}
"""

PANICKED = """// MIR for `panicked` after PreCodegen

fn panicked(_1: u32) -> u32 {
    debug x => _1;                       // in scope 0 at src/main.rs:29:13: 29:18
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:29:28: 29:31
    let mut _2: !;                       // in scope 0 at src/main.rs:29:32: 35:2
    let _3: &mut u32;                    // in scope 0 at src/main.rs:30:5: 30:22
    let mut _4: &mut u32;                // in scope 0 at src/main.rs:30:15: 30:21
    let _5: u32;                         // in scope 0 at src/main.rs:31:9: 31:10
    let _7: u32;                         // in scope 0 at src/main.rs:33:5: 33:21
    let mut _8: u32;                     // in scope 0 at src/main.rs:33:15: 33:20
    let mut _9: u32;                     // in scope 0 at src/main.rs:33:15: 33:16
    let mut _10: u32;                    // in scope 0 at src/main.rs:33:19: 33:20
    let mut _11: (u32, bool);            // in scope 0 at src/main.rs:33:15: 33:20
    let _12: !;                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:59:21: 63:6
    let _13: !;                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
    let mut _14: std::fmt::Arguments<'_>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
    scope 1 {
        debug z => _5;                   // in scope 1 at src/main.rs:31:9: 31:10
        let _6: u32;                     // in scope 1 at src/main.rs:32:9: 32:10
        scope 2 {
            debug k => _6;               // in scope 2 at src/main.rs:32:9: 32:10
        }
    }
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:30:5: 30:22
        StorageLive(_4);                 // scope 0 at src/main.rs:30:15: 30:21
        _4 = &mut _1;                    // scope 0 at src/main.rs:30:15: 30:21
        _3 = std::hint::black_box::<&mut u32>(move _4) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:30:5: 30:22
    }
    bb1: {
        StorageDead(_4);                 // scope 0 at src/main.rs:30:21: 30:22
        StorageDead(_3);                 // scope 0 at src/main.rs:30:22: 30:23
        StorageLive(_5);                 // scope 0 at src/main.rs:31:9: 31:10
        _5 = copy _1;                    // scope 0 at src/main.rs:31:13: 31:14
        StorageLive(_6);                 // scope 1 at src/main.rs:32:9: 32:10
        _6 = other() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:32:13: 32:20
    }
    bb2: {
        StorageLive(_7);                 // scope 2 at src/main.rs:33:5: 33:21
        StorageLive(_8);                 // scope 2 at src/main.rs:33:15: 33:20
        StorageLive(_9);                 // scope 2 at src/main.rs:33:15: 33:16
        _9 = copy _5;                    // scope 2 at src/main.rs:33:15: 33:16
        StorageLive(_10);                // scope 2 at src/main.rs:33:19: 33:20
        _10 = copy _6;                   // scope 2 at src/main.rs:33:19: 33:20
        _11 = AddWithOverflow(copy _9, copy _10); // scope 2 at src/main.rs:33:15: 33:20
        assert(!move (_11.1: bool), "attempt to compute `{} + {}`, which would overflow", move _9, move _10) -> [success: bb3, unwind continue]; // scope 2 at src/main.rs:33:15: 33:20
    }
    bb3: {
        _8 = move (_11.0: u32);          // scope 2 at src/main.rs:33:15: 33:20
        StorageDead(_10);                // scope 2 at src/main.rs:33:19: 33:20
        StorageDead(_9);                 // scope 2 at src/main.rs:33:19: 33:20
        _7 = std::hint::black_box::<u32>(move _8) -> [return: bb4, unwind continue]; // scope 2 at src/main.rs:33:5: 33:21
    }
    bb4: {
        StorageDead(_8);                 // scope 2 at src/main.rs:33:20: 33:21
        StorageDead(_7);                 // scope 2 at src/main.rs:33:21: 33:22
        StorageLive(_13);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
        StorageLive(_14);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
        _14 = Arguments::<'_>::from_str(const "stop") -> [return: bb5, unwind continue]; // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
    }
    bb5: {
        _13 = std::rt::panic_fmt(move _14) -> unwind continue; // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
    }
}
alloc2 (size: 4, align: 1) {
    73 74 6f 70                                     │ stop
}
"""

UNWOUND = """// MIR for `unwound` after PreCodegen

fn unwound(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:37:12: 37:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:37:23: 37:26
    let mut _2: !;                       // in scope 0 at src/main.rs:37:27: 46:2
    let _3: std::string::String;         // in scope 0 at src/main.rs:38:9: 38:14
    let mut _4: &u32;                    // in scope 0 at src/main.rs:38:17: 38:18
    let mut _6: u32;                     // in scope 0 at src/main.rs:39:17: 39:18
    let mut _7: (u32, bool);             // in scope 0 at src/main.rs:39:17: 39:22
    let _8: &mut u32;                    // in scope 0 at src/main.rs:40:5: 40:22
    let mut _9: &mut u32;                // in scope 0 at src/main.rs:40:15: 40:21
    let _12: u32;                        // in scope 0 at src/main.rs:43:5: 43:21
    let mut _13: u32;                    // in scope 0 at src/main.rs:43:15: 43:20
    let mut _14: u32;                    // in scope 0 at src/main.rs:43:15: 43:16
    let mut _15: u32;                    // in scope 0 at src/main.rs:43:19: 43:20
    let mut _16: (u32, bool);            // in scope 0 at src/main.rs:43:15: 43:20
    let _17: &std::string::String;       // in scope 0 at src/main.rs:44:5: 44:22
    let mut _18: &std::string::String;   // in scope 0 at src/main.rs:44:15: 44:21
    let _19: !;                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:59:21: 63:6
    let _20: !;                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
    let mut _21: std::fmt::Arguments<'_>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
    scope 1 {
        debug label => _3;               // in scope 1 at src/main.rs:38:9: 38:14
        let mut _5: u32;                 // in scope 1 at src/main.rs:39:9: 39:14
        scope 2 {
            debug x => _5;               // in scope 2 at src/main.rs:39:9: 39:14
            let _10: u32;                // in scope 2 at src/main.rs:41:9: 41:10
            scope 3 {
                debug z => _10;          // in scope 3 at src/main.rs:41:9: 41:10
                let _11: u32;            // in scope 3 at src/main.rs:42:9: 42:10
                scope 4 {
                    debug k => _11;      // in scope 4 at src/main.rs:42:9: 42:10
                }
            }
        }
    }
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:38:9: 38:14
        StorageLive(_4);                 // scope 0 at src/main.rs:38:17: 38:18
        _4 = &_1;                        // scope 0 at src/main.rs:38:17: 38:18
        _3 = <u32 as ToString>::to_string(move _4) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:38:17: 38:30
    }
    bb1: {
        StorageDead(_4);                 // scope 0 at src/main.rs:38:29: 38:30
        StorageLive(_5);                 // scope 1 at src/main.rs:39:9: 39:14
        StorageLive(_6);                 // scope 1 at src/main.rs:39:17: 39:18
        _6 = copy _1;                    // scope 1 at src/main.rs:39:17: 39:18
        _7 = AddWithOverflow(copy _6, const 1_u32); // scope 1 at src/main.rs:39:17: 39:22
        assert(!move (_7.1: bool), "attempt to compute `{} + {}`, which would overflow", move _6, const 1_u32) -> [success: bb2, unwind: bb9]; // scope 1 at src/main.rs:39:17: 39:22
    }
    bb2: {
        _5 = move (_7.0: u32);           // scope 1 at src/main.rs:39:17: 39:22
        StorageDead(_6);                 // scope 1 at src/main.rs:39:21: 39:22
        StorageLive(_8);                 // scope 2 at src/main.rs:40:5: 40:22
        StorageLive(_9);                 // scope 2 at src/main.rs:40:15: 40:21
        _9 = &mut _5;                    // scope 2 at src/main.rs:40:15: 40:21
        _8 = std::hint::black_box::<&mut u32>(move _9) -> [return: bb3, unwind: bb9]; // scope 2 at src/main.rs:40:5: 40:22
    }
    bb3: {
        StorageDead(_9);                 // scope 2 at src/main.rs:40:21: 40:22
        StorageDead(_8);                 // scope 2 at src/main.rs:40:22: 40:23
        StorageLive(_10);                // scope 2 at src/main.rs:41:9: 41:10
        _10 = copy _5;                   // scope 2 at src/main.rs:41:13: 41:14
        StorageLive(_11);                // scope 3 at src/main.rs:42:9: 42:10
        _11 = other() -> [return: bb4, unwind: bb9]; // scope 3 at src/main.rs:42:13: 42:20
    }
    bb4: {
        StorageLive(_12);                // scope 4 at src/main.rs:43:5: 43:21
        StorageLive(_13);                // scope 4 at src/main.rs:43:15: 43:20
        StorageLive(_14);                // scope 4 at src/main.rs:43:15: 43:16
        _14 = copy _10;                  // scope 4 at src/main.rs:43:15: 43:16
        StorageLive(_15);                // scope 4 at src/main.rs:43:19: 43:20
        _15 = copy _11;                  // scope 4 at src/main.rs:43:19: 43:20
        _16 = AddWithOverflow(copy _14, copy _15); // scope 4 at src/main.rs:43:15: 43:20
        assert(!move (_16.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _15) -> [success: bb5, unwind: bb9]; // scope 4 at src/main.rs:43:15: 43:20
    }
    bb5: {
        _13 = move (_16.0: u32);         // scope 4 at src/main.rs:43:15: 43:20
        StorageDead(_15);                // scope 4 at src/main.rs:43:19: 43:20
        StorageDead(_14);                // scope 4 at src/main.rs:43:19: 43:20
        _12 = std::hint::black_box::<u32>(move _13) -> [return: bb6, unwind: bb9]; // scope 4 at src/main.rs:43:5: 43:21
    }
    bb6: {
        StorageDead(_13);                // scope 4 at src/main.rs:43:20: 43:21
        StorageDead(_12);                // scope 4 at src/main.rs:43:21: 43:22
        StorageLive(_17);                // scope 4 at src/main.rs:44:5: 44:22
        StorageLive(_18);                // scope 4 at src/main.rs:44:15: 44:21
        _18 = &_3;                       // scope 4 at src/main.rs:44:15: 44:21
        _17 = std::hint::black_box::<&String>(move _18) -> [return: bb7, unwind: bb9]; // scope 4 at src/main.rs:44:5: 44:22
    }
    bb7: {
        StorageDead(_18);                // scope 4 at src/main.rs:44:21: 44:22
        StorageDead(_17);                // scope 4 at src/main.rs:44:22: 44:23
        StorageLive(_20);                // scope 4 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
        StorageLive(_21);                // scope 4 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
        _21 = Arguments::<'_>::from_str(const "stop") -> [return: bb8, unwind: bb9]; // scope 4 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
    }
    bb8: {
        _20 = std::rt::panic_fmt(move _21) -> bb9; // scope 4 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
    }
    bb9 (cleanup): {
        drop(_3) -> [return: bb10, unwind terminate(cleanup)]; // scope 0 at src/main.rs:46:1: 46:2
    }
    bb10 (cleanup): {
        resume;                          // scope 0 at src/main.rs:37:1: 46:2
    }
}
alloc2 (size: 4, align: 1) {
    73 74 6f 70                                     │ stop
}
"""

POINTED = """// MIR for `pointed` after PreCodegen

fn pointed(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:48:12: 48:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:48:23: 48:26
    let mut _2: !;                       // in scope 0 at src/main.rs:48:27: 56:2
    let _3: u32;                         // in scope 0 at src/main.rs:49:9: 49:10
    let mut _4: u32;                     // in scope 0 at src/main.rs:49:13: 49:14
    let mut _5: (u32, bool);             // in scope 0 at src/main.rs:49:13: 49:18
    let _9: u32;                         // in scope 0 at src/main.rs:53:5: 53:21
    let mut _10: u32;                    // in scope 0 at src/main.rs:53:15: 53:20
    let mut _11: u32;                    // in scope 0 at src/main.rs:53:15: 53:16
    let mut _12: u32;                    // in scope 0 at src/main.rs:53:19: 53:20
    let mut _13: (u32, bool);            // in scope 0 at src/main.rs:53:15: 53:20
    let _14: &u32;                       // in scope 0 at src/main.rs:54:5: 54:17
    let mut _15: &u32;                   // in scope 0 at src/main.rs:54:15: 54:16
    let _16: !;                          // in scope 0 at src/main.rs:55:5: 55:26
    scope 1 {
        debug x => _3;                   // in scope 1 at src/main.rs:49:9: 49:10
        let _6: &u32;                    // in scope 1 at src/main.rs:50:9: 50:10
        scope 2 {
            debug r => _6;               // in scope 2 at src/main.rs:50:9: 50:10
            let _7: u32;                 // in scope 2 at src/main.rs:51:9: 51:10
            scope 3 {
                debug z => _7;           // in scope 3 at src/main.rs:51:9: 51:10
                let _8: u32;             // in scope 3 at src/main.rs:52:9: 52:10
                scope 4 {
                    debug k => _8;       // in scope 4 at src/main.rs:52:9: 52:10
                }
            }
        }
    }
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:49:9: 49:10
        StorageLive(_4);                 // scope 0 at src/main.rs:49:13: 49:14
        _4 = copy _1;                    // scope 0 at src/main.rs:49:13: 49:14
        _5 = AddWithOverflow(copy _4, const 1_u32); // scope 0 at src/main.rs:49:13: 49:18
        assert(!move (_5.1: bool), "attempt to compute `{} + {}`, which would overflow", move _4, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:49:13: 49:18
    }
    bb1: {
        _3 = move (_5.0: u32);           // scope 0 at src/main.rs:49:13: 49:18
        StorageDead(_4);                 // scope 0 at src/main.rs:49:17: 49:18
        StorageLive(_6);                 // scope 1 at src/main.rs:50:9: 50:10
        _6 = &_3;                        // scope 1 at src/main.rs:50:13: 50:15
        StorageLive(_7);                 // scope 2 at src/main.rs:51:9: 51:10
        _7 = copy _3;                    // scope 2 at src/main.rs:51:13: 51:14
        StorageLive(_8);                 // scope 3 at src/main.rs:52:9: 52:10
        _8 = other() -> [return: bb2, unwind continue]; // scope 3 at src/main.rs:52:13: 52:20
    }
    bb2: {
        StorageLive(_9);                 // scope 4 at src/main.rs:53:5: 53:21
        StorageLive(_10);                // scope 4 at src/main.rs:53:15: 53:20
        StorageLive(_11);                // scope 4 at src/main.rs:53:15: 53:16
        _11 = copy _7;                   // scope 4 at src/main.rs:53:15: 53:16
        StorageLive(_12);                // scope 4 at src/main.rs:53:19: 53:20
        _12 = copy _8;                   // scope 4 at src/main.rs:53:19: 53:20
        _13 = AddWithOverflow(copy _11, copy _12); // scope 4 at src/main.rs:53:15: 53:20
        assert(!move (_13.1: bool), "attempt to compute `{} + {}`, which would overflow", move _11, move _12) -> [success: bb3, unwind continue]; // scope 4 at src/main.rs:53:15: 53:20
    }
    bb3: {
        _10 = move (_13.0: u32);         // scope 4 at src/main.rs:53:15: 53:20
        StorageDead(_12);                // scope 4 at src/main.rs:53:19: 53:20
        StorageDead(_11);                // scope 4 at src/main.rs:53:19: 53:20
        _9 = std::hint::black_box::<u32>(move _10) -> [return: bb4, unwind continue]; // scope 4 at src/main.rs:53:5: 53:21
    }
    bb4: {
        StorageDead(_10);                // scope 4 at src/main.rs:53:20: 53:21
        StorageDead(_9);                 // scope 4 at src/main.rs:53:21: 53:22
        StorageLive(_14);                // scope 4 at src/main.rs:54:5: 54:17
        StorageLive(_15);                // scope 4 at src/main.rs:54:15: 54:16
        _15 = copy _6;                   // scope 4 at src/main.rs:54:15: 54:16
        _14 = std::hint::black_box::<&u32>(move _15) -> [return: bb5, unwind continue]; // scope 4 at src/main.rs:54:5: 54:17
    }
    bb5: {
        StorageDead(_15);                // scope 4 at src/main.rs:54:16: 54:17
        StorageDead(_14);                // scope 4 at src/main.rs:54:17: 54:18
        StorageLive(_16);                // scope 4 at src/main.rs:55:5: 55:26
        _16 = exit(const 0_i32) -> unwind continue; // scope 4 at src/main.rs:55:5: 55:26
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
    debug _task_context => _2;           // in scope 0 at src/main.rs:58:25: 65:2
    debug n => ((*_20).0: u32);          // in scope 0 at src/main.rs:58:17: 58:18
    let mut _0: std::task::Poll<()>;     // return place in scope 0 at src/main.rs:58:25: 65:2
    let _3: u32;                         // in scope 0 at src/main.rs:58:17: 58:18
    let mut _4: !;                       // in scope 0 at src/main.rs:58:25: 65:2
    let mut _6: u32;                     // in scope 0 at src/main.rs:59:17: 59:18
    let mut _7: (u32, bool);             // in scope 0 at src/main.rs:59:17: 59:22
    let _8: &mut u32;                    // in scope 0 at src/main.rs:60:5: 60:22
    let mut _9: &mut u32;                // in scope 0 at src/main.rs:60:15: 60:21
    let _12: u32;                        // in scope 0 at src/main.rs:63:5: 63:21
    let mut _13: u32;                    // in scope 0 at src/main.rs:63:15: 63:20
    let mut _14: u32;                    // in scope 0 at src/main.rs:63:15: 63:16
    let mut _15: u32;                    // in scope 0 at src/main.rs:63:19: 63:20
    let mut _16: (u32, bool);            // in scope 0 at src/main.rs:63:15: 63:20
    let _17: !;                          // in scope 0 at src/main.rs:64:5: 64:26
    let mut _18: ();                     // in scope 0 at src/main.rs:58:25: 65:2
    let mut _19: u32;                    // in scope 0 at src/main.rs:58:25: 65:2
    let mut _20: &mut {async fn body of exited()}; // in scope 0 at src/main.rs:58:25: 65:2
    scope 1 {
        debug n => _3;                   // in scope 1 at src/main.rs:58:17: 58:18
        let mut _5: u32;                 // in scope 1 at src/main.rs:59:9: 59:14
        scope 2 {
            debug x => _5;               // in scope 2 at src/main.rs:59:9: 59:14
            let _10: u32;                // in scope 2 at src/main.rs:61:9: 61:10
            scope 3 {
                debug z => _10;          // in scope 3 at src/main.rs:61:9: 61:10
                let _11: u32;            // in scope 3 at src/main.rs:62:9: 62:10
                scope 4 {
                    debug k => _11;      // in scope 4 at src/main.rs:62:9: 62:10
                }
            }
        }
    }
    bb0: {
        _20 = copy (_1.0: &mut {async fn body of exited()}); // scope 0 at src/main.rs:58:25: 65:2
        _19 = discriminant((*_20));      // scope 0 at src/main.rs:58:25: 65:2
        switchInt(move _19) -> [0: bb1, 2: bb14, otherwise: bb15]; // scope 0 at src/main.rs:58:25: 65:2
    }
    bb1: {
        StorageLive(_3);                 // scope 0 at src/main.rs:58:17: 58:18
        _3 = copy ((*_20).0: u32);       // scope 0 at src/main.rs:58:17: 58:18
        StorageLive(_4);                 // scope 1 at src/main.rs:58:25: 65:2
        StorageLive(_5);                 // scope 1 at src/main.rs:59:9: 59:14
        StorageLive(_6);                 // scope 1 at src/main.rs:59:17: 59:18
        _6 = copy _3;                    // scope 1 at src/main.rs:59:17: 59:18
        _7 = AddWithOverflow(copy _6, const 1_u32); // scope 1 at src/main.rs:59:17: 59:22
        assert(!move (_7.1: bool), "attempt to compute `{} + {}`, which would overflow", move _6, const 1_u32) -> [success: bb2, unwind: bb12]; // scope 1 at src/main.rs:59:17: 59:22
    }
    bb2: {
        _5 = move (_7.0: u32);           // scope 1 at src/main.rs:59:17: 59:22
        StorageDead(_6);                 // scope 1 at src/main.rs:59:21: 59:22
        StorageLive(_8);                 // scope 2 at src/main.rs:60:5: 60:22
        StorageLive(_9);                 // scope 2 at src/main.rs:60:15: 60:21
        _9 = &mut _5;                    // scope 2 at src/main.rs:60:15: 60:21
        _8 = std::hint::black_box::<&mut u32>(move _9) -> [return: bb3, unwind: bb11]; // scope 2 at src/main.rs:60:5: 60:22
    }
    bb3: {
        StorageDead(_9);                 // scope 2 at src/main.rs:60:21: 60:22
        StorageDead(_8);                 // scope 2 at src/main.rs:60:22: 60:23
        StorageLive(_10);                // scope 2 at src/main.rs:61:9: 61:10
        _10 = copy _5;                   // scope 2 at src/main.rs:61:13: 61:14
        StorageLive(_11);                // scope 3 at src/main.rs:62:9: 62:10
        _11 = other() -> [return: bb4, unwind: bb10]; // scope 3 at src/main.rs:62:13: 62:20
    }
    bb4: {
        StorageLive(_12);                // scope 4 at src/main.rs:63:5: 63:21
        StorageLive(_13);                // scope 4 at src/main.rs:63:15: 63:20
        StorageLive(_14);                // scope 4 at src/main.rs:63:15: 63:16
        _14 = copy _10;                  // scope 4 at src/main.rs:63:15: 63:16
        StorageLive(_15);                // scope 4 at src/main.rs:63:19: 63:20
        _15 = copy _11;                  // scope 4 at src/main.rs:63:19: 63:20
        _16 = AddWithOverflow(copy _14, copy _15); // scope 4 at src/main.rs:63:15: 63:20
        assert(!move (_16.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _15) -> [success: bb5, unwind: bb8]; // scope 4 at src/main.rs:63:15: 63:20
    }
    bb5: {
        _13 = move (_16.0: u32);         // scope 4 at src/main.rs:63:15: 63:20
        StorageDead(_15);                // scope 4 at src/main.rs:63:19: 63:20
        StorageDead(_14);                // scope 4 at src/main.rs:63:19: 63:20
        _12 = std::hint::black_box::<u32>(move _13) -> [return: bb6, unwind: bb9]; // scope 4 at src/main.rs:63:5: 63:21
    }
    bb6: {
        StorageDead(_13);                // scope 4 at src/main.rs:63:20: 63:21
        StorageDead(_12);                // scope 4 at src/main.rs:63:21: 63:22
        StorageLive(_17);                // scope 4 at src/main.rs:64:5: 64:26
        _17 = exit(const 0_i32) -> bb7;  // scope 4 at src/main.rs:64:5: 64:26
    }
    bb7 (cleanup): {
        StorageDead(_17);                // scope 4 at src/main.rs:64:26: 64:27
        goto -> bb10;                    // scope 4 at no-location
    }
    bb8 (cleanup): {
        StorageDead(_15);                // scope 4 at src/main.rs:63:19: 63:20
        StorageDead(_14);                // scope 4 at src/main.rs:63:19: 63:20
        goto -> bb9;                     // scope 4 at no-location
    }
    bb9 (cleanup): {
        StorageDead(_13);                // scope 4 at src/main.rs:63:20: 63:21
        StorageDead(_12);                // scope 4 at src/main.rs:63:21: 63:22
        goto -> bb10;                    // scope 4 at no-location
    }
    bb10 (cleanup): {
        StorageDead(_11);                // scope 3 at src/main.rs:65:1: 65:2
        StorageDead(_10);                // scope 2 at src/main.rs:65:1: 65:2
        goto -> bb13;                    // scope 2 at no-location
    }
    bb11 (cleanup): {
        StorageDead(_9);                 // scope 2 at src/main.rs:60:21: 60:22
        StorageDead(_8);                 // scope 2 at src/main.rs:60:22: 60:23
        goto -> bb13;                    // scope 2 at no-location
    }
    bb12 (cleanup): {
        StorageDead(_6);                 // scope 1 at src/main.rs:59:21: 59:22
        goto -> bb13;                    // scope 1 at no-location
    }
    bb13 (cleanup): {
        StorageDead(_5);                 // scope 1 at src/main.rs:65:1: 65:2
        StorageDead(_4);                 // scope 1 at src/main.rs:65:1: 65:2
        StorageDead(_3);                 // scope 0 at src/main.rs:65:1: 65:2
        discriminant((*_20)) = 2;        // scope 0 at src/main.rs:58:25: 65:2
        resume;                          // scope 0 at src/main.rs:58:25: 65:2
    }
    bb14: {
        assert(const false, "`async fn` resumed after panicking") -> [success: bb14, unwind continue]; // scope 0 at src/main.rs:58:25: 65:2
    }
    bb15: {
        unreachable;                     // scope 0 at src/main.rs:58:25: 65:2
    }
}
"""

BRANCH = """// MIR for `branch` after PreCodegen

fn branch(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:67:11: 67:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:67:22: 67:25
    let mut _2: u32;                     // in scope 0 at src/main.rs:68:9: 68:18
    let mut _3: u32;                     // in scope 0 at src/main.rs:68:21: 68:22
    let mut _4: (u32, bool);             // in scope 0 at src/main.rs:68:21: 68:26
    let _6: ();                          // in scope 0 at src/main.rs:70:5: 72:6
    let mut _7: bool;                    // in scope 0 at src/main.rs:70:8: 70:15
    let mut _8: u32;                     // in scope 0 at src/main.rs:70:8: 70:9
    let mut _9: !;                       // in scope 0 at src/main.rs:70:16: 72:6
    let _10: !;                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:59:21: 63:6
    let _11: !;                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
    let mut _12: std::fmt::Arguments<'_>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
    let mut _13: u32;                    // in scope 0 at src/main.rs:73:13: 73:14
    let mut _14: u32;                    // in scope 0 at src/main.rs:73:17: 73:18
    let mut _15: (u32, bool);            // in scope 0 at src/main.rs:73:13: 73:18
    scope 1 {
        debug total => _2;               // in scope 1 at src/main.rs:68:9: 68:18
        let _5: u32;                     // in scope 1 at src/main.rs:69:9: 69:10
        scope 2 {
            debug k => _5;               // in scope 2 at src/main.rs:69:9: 69:10
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:68:9: 68:18
        StorageLive(_3);                 // scope 0 at src/main.rs:68:21: 68:22
        _3 = copy _1;                    // scope 0 at src/main.rs:68:21: 68:22
        _4 = AddWithOverflow(copy _3, const 1_u32); // scope 0 at src/main.rs:68:21: 68:26
        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:68:21: 68:26
    }
    bb1: {
        _2 = move (_4.0: u32);           // scope 0 at src/main.rs:68:21: 68:26
        StorageDead(_3);                 // scope 0 at src/main.rs:68:25: 68:26
        StorageLive(_5);                 // scope 1 at src/main.rs:69:9: 69:10
        _5 = other() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:69:13: 69:20
    }
    bb2: {
        StorageLive(_6);                 // scope 2 at src/main.rs:70:5: 72:6
        StorageLive(_7);                 // scope 2 at src/main.rs:70:8: 70:15
        StorageLive(_8);                 // scope 2 at src/main.rs:70:8: 70:9
        _8 = copy _1;                    // scope 2 at src/main.rs:70:8: 70:9
        _7 = Gt(move _8, const 100_u32); // scope 2 at src/main.rs:70:8: 70:15
        switchInt(move _7) -> [0: bb5, otherwise: bb3]; // scope 2 at src/main.rs:70:8: 70:15
    }
    bb3: {
        StorageDead(_8);                 // scope 2 at src/main.rs:70:14: 70:15
        StorageLive(_11);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
        StorageLive(_12);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
        _12 = Arguments::<'_>::from_str(const "too many") -> [return: bb4, unwind continue]; // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:38: 62:72
    }
    bb4: {
        _11 = std::rt::panic_fmt(move _12) -> unwind continue; // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/panic.rs:62:9: 62:73
    }
    bb5: {
        StorageDead(_8);                 // scope 2 at src/main.rs:70:14: 70:15
        _6 = const ();                   // scope 2 at src/main.rs:72:6: 72:6
        StorageDead(_7);                 // scope 2 at src/main.rs:72:5: 72:6
        StorageDead(_6);                 // scope 2 at src/main.rs:72:5: 72:6
        StorageLive(_13);                // scope 2 at src/main.rs:73:13: 73:14
        _13 = copy _5;                   // scope 2 at src/main.rs:73:13: 73:14
        StorageLive(_14);                // scope 2 at src/main.rs:73:17: 73:18
        _14 = copy _1;                   // scope 2 at src/main.rs:73:17: 73:18
        _15 = AddWithOverflow(copy _13, copy _14); // scope 2 at src/main.rs:73:13: 73:18
        assert(!move (_15.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _14) -> [success: bb6, unwind continue]; // scope 2 at src/main.rs:73:13: 73:18
    }
    bb6: {
        _2 = move (_15.0: u32);          // scope 2 at src/main.rs:73:5: 73:18
        StorageDead(_14);                // scope 2 at src/main.rs:73:17: 73:18
        StorageDead(_13);                // scope 2 at src/main.rs:73:17: 73:18
        _0 = copy _2;                    // scope 2 at src/main.rs:74:5: 74:10
        StorageDead(_5);                 // scope 1 at src/main.rs:75:1: 75:2
        StorageDead(_2);                 // scope 0 at src/main.rs:75:1: 75:2
        return;                          // scope 0 at src/main.rs:75:2: 75:2
    }
}
alloc6 (size: 8, align: 1) {
    74 6f 6f 20 6d 61 6e 79                         │ too many
}
"""

SPIN = """// MIR for `spin` after PreCodegen

fn spin(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:77:9: 77:10
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:77:20: 77:23
    let mut _2: u32;                     // in scope 0 at src/main.rs:78:9: 78:18
    let mut _3: u32;                     // in scope 0 at src/main.rs:78:21: 78:22
    let mut _4: (u32, bool);             // in scope 0 at src/main.rs:78:21: 78:26
    let _6: ();                          // in scope 0 at src/main.rs:80:5: 84:6
    let mut _7: bool;                    // in scope 0 at src/main.rs:80:8: 80:15
    let mut _8: u32;                     // in scope 0 at src/main.rs:80:8: 80:9
    let mut _9: !;                       // in scope 0 at src/main.rs:81:9: 83:10
    let mut _10: ();                     // in scope 0 at src/main.rs:77:1: 87:2
    let _11: u32;                        // in scope 0 at src/main.rs:82:13: 82:25
    let mut _12: u32;                    // in scope 0 at src/main.rs:82:23: 82:24
    let mut _13: u32;                    // in scope 0 at src/main.rs:85:13: 85:14
    let mut _14: u32;                    // in scope 0 at src/main.rs:85:17: 85:18
    let mut _15: (u32, bool);            // in scope 0 at src/main.rs:85:13: 85:18
    scope 1 {
        debug total => _2;               // in scope 1 at src/main.rs:78:9: 78:18
        let _5: u32;                     // in scope 1 at src/main.rs:79:9: 79:10
        scope 2 {
            debug k => _5;               // in scope 2 at src/main.rs:79:9: 79:10
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:78:9: 78:18
        StorageLive(_3);                 // scope 0 at src/main.rs:78:21: 78:22
        _3 = copy _1;                    // scope 0 at src/main.rs:78:21: 78:22
        _4 = AddWithOverflow(copy _3, const 1_u32); // scope 0 at src/main.rs:78:21: 78:26
        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:78:21: 78:26
    }
    bb1: {
        _2 = move (_4.0: u32);           // scope 0 at src/main.rs:78:21: 78:26
        StorageDead(_3);                 // scope 0 at src/main.rs:78:25: 78:26
        StorageLive(_5);                 // scope 1 at src/main.rs:79:9: 79:10
        _5 = other() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:79:13: 79:20
    }
    bb2: {
        StorageLive(_6);                 // scope 2 at src/main.rs:80:5: 84:6
        StorageLive(_7);                 // scope 2 at src/main.rs:80:8: 80:15
        StorageLive(_8);                 // scope 2 at src/main.rs:80:8: 80:9
        _8 = copy _1;                    // scope 2 at src/main.rs:80:8: 80:9
        _7 = Gt(move _8, const 100_u32); // scope 2 at src/main.rs:80:8: 80:15
        switchInt(move _7) -> [0: bb6, otherwise: bb3]; // scope 2 at src/main.rs:80:8: 80:15
    }
    bb3: {
        StorageDead(_8);                 // scope 2 at src/main.rs:80:14: 80:15
        StorageLive(_9);                 // scope 2 at src/main.rs:81:9: 83:10
        goto -> bb4;                     // scope 2 at src/main.rs:81:9: 83:10
    }
    bb4: {
        StorageLive(_11);                // scope 2 at src/main.rs:82:13: 82:25
        StorageLive(_12);                // scope 2 at src/main.rs:82:23: 82:24
        _12 = copy _5;                   // scope 2 at src/main.rs:82:23: 82:24
        _11 = std::hint::black_box::<u32>(move _12) -> [return: bb5, unwind continue]; // scope 2 at src/main.rs:82:13: 82:25
    }
    bb5: {
        StorageDead(_12);                // scope 2 at src/main.rs:82:24: 82:25
        StorageDead(_11);                // scope 2 at src/main.rs:82:25: 82:26
        _10 = const ();                  // scope 2 at src/main.rs:81:14: 83:10
        goto -> bb4;                     // scope 2 at src/main.rs:81:9: 83:10
    }
    bb6: {
        StorageDead(_8);                 // scope 2 at src/main.rs:80:14: 80:15
        _6 = const ();                   // scope 2 at src/main.rs:84:6: 84:6
        StorageDead(_7);                 // scope 2 at src/main.rs:84:5: 84:6
        StorageDead(_6);                 // scope 2 at src/main.rs:84:5: 84:6
        StorageLive(_13);                // scope 2 at src/main.rs:85:13: 85:14
        _13 = copy _5;                   // scope 2 at src/main.rs:85:13: 85:14
        StorageLive(_14);                // scope 2 at src/main.rs:85:17: 85:18
        _14 = copy _1;                   // scope 2 at src/main.rs:85:17: 85:18
        _15 = AddWithOverflow(copy _13, copy _14); // scope 2 at src/main.rs:85:13: 85:18
        assert(!move (_15.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _14) -> [success: bb7, unwind continue]; // scope 2 at src/main.rs:85:13: 85:18
    }
    bb7: {
        _2 = move (_15.0: u32);          // scope 2 at src/main.rs:85:5: 85:18
        StorageDead(_14);                // scope 2 at src/main.rs:85:17: 85:18
        StorageDead(_13);                // scope 2 at src/main.rs:85:17: 85:18
        _0 = copy _2;                    // scope 2 at src/main.rs:86:5: 86:10
        StorageDead(_5);                 // scope 1 at src/main.rs:87:1: 87:2
        StorageDead(_2);                 // scope 0 at src/main.rs:87:1: 87:2
        return;                          // scope 0 at src/main.rs:87:2: 87:2
    }
}
"""

CLEANED = """// MIR for `cleaned` after PreCodegen

fn cleaned(_1: u32) -> u32 {
    debug n => _1;                       // in scope 0 at src/main.rs:89:12: 89:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:89:23: 89:26
    let mut _2: u32;                     // in scope 0 at src/main.rs:90:9: 90:18
    let mut _3: u32;                     // in scope 0 at src/main.rs:90:21: 90:22
    let mut _4: (u32, bool);             // in scope 0 at src/main.rs:90:21: 90:26
    let mut _6: &u32;                    // in scope 0 at src/main.rs:91:17: 91:18
    let mut _8: u32;                     // in scope 0 at src/main.rs:93:13: 93:14
    let mut _9: u32;                     // in scope 0 at src/main.rs:93:17: 93:18
    let mut _10: (u32, bool);            // in scope 0 at src/main.rs:93:13: 93:18
    let mut _11: u32;                    // in scope 0 at src/main.rs:94:5: 94:23
    let mut _12: usize;                  // in scope 0 at src/main.rs:94:5: 94:16
    let mut _13: &std::string::String;   // in scope 0 at src/main.rs:94:5: 94:10
    let mut _14: u32;                    // in scope 0 at src/main.rs:94:26: 94:31
    let mut _15: (u32, bool);            // in scope 0 at src/main.rs:94:5: 94:31
    scope 1 {
        debug total => _2;               // in scope 1 at src/main.rs:90:9: 90:18
        let _5: std::string::String;     // in scope 1 at src/main.rs:91:9: 91:14
        scope 2 {
            debug label => _5;           // in scope 2 at src/main.rs:91:9: 91:14
            let _7: u32;                 // in scope 2 at src/main.rs:92:9: 92:10
            scope 3 {
                debug k => _7;           // in scope 3 at src/main.rs:92:9: 92:10
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:90:9: 90:18
        StorageLive(_3);                 // scope 0 at src/main.rs:90:21: 90:22
        _3 = copy _1;                    // scope 0 at src/main.rs:90:21: 90:22
        _4 = AddWithOverflow(copy _3, const 1_u32); // scope 0 at src/main.rs:90:21: 90:26
        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, const 1_u32) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:90:21: 90:26
    }
    bb1: {
        _2 = move (_4.0: u32);           // scope 0 at src/main.rs:90:21: 90:26
        StorageDead(_3);                 // scope 0 at src/main.rs:90:25: 90:26
        StorageLive(_5);                 // scope 1 at src/main.rs:91:9: 91:14
        StorageLive(_6);                 // scope 1 at src/main.rs:91:17: 91:18
        _6 = &_1;                        // scope 1 at src/main.rs:91:17: 91:18
        _5 = <u32 as ToString>::to_string(move _6) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:91:17: 91:30
    }
    bb2: {
        StorageDead(_6);                 // scope 1 at src/main.rs:91:29: 91:30
        StorageLive(_7);                 // scope 2 at src/main.rs:92:9: 92:10
        _7 = other() -> [return: bb3, unwind: bb8]; // scope 2 at src/main.rs:92:13: 92:20
    }
    bb3: {
        StorageLive(_8);                 // scope 3 at src/main.rs:93:13: 93:14
        _8 = copy _7;                    // scope 3 at src/main.rs:93:13: 93:14
        StorageLive(_9);                 // scope 3 at src/main.rs:93:17: 93:18
        _9 = copy _1;                    // scope 3 at src/main.rs:93:17: 93:18
        _10 = AddWithOverflow(copy _8, copy _9); // scope 3 at src/main.rs:93:13: 93:18
        assert(!move (_10.1: bool), "attempt to compute `{} + {}`, which would overflow", move _8, move _9) -> [success: bb4, unwind: bb8]; // scope 3 at src/main.rs:93:13: 93:18
    }
    bb4: {
        _2 = move (_10.0: u32);          // scope 3 at src/main.rs:93:5: 93:18
        StorageDead(_9);                 // scope 3 at src/main.rs:93:17: 93:18
        StorageDead(_8);                 // scope 3 at src/main.rs:93:17: 93:18
        StorageLive(_11);                // scope 3 at src/main.rs:94:5: 94:23
        StorageLive(_12);                // scope 3 at src/main.rs:94:5: 94:16
        StorageLive(_13);                // scope 3 at src/main.rs:94:5: 94:10
        _13 = &_5;                       // scope 3 at src/main.rs:94:5: 94:10
        _12 = String::len(move _13) -> [return: bb5, unwind: bb8]; // scope 3 at src/main.rs:94:5: 94:16
    }
    bb5: {
        StorageDead(_13);                // scope 3 at src/main.rs:94:15: 94:16
        _11 = move _12 as u32 (IntToInt); // scope 3 at src/main.rs:94:5: 94:23
        StorageDead(_12);                // scope 3 at src/main.rs:94:22: 94:23
        StorageLive(_14);                // scope 3 at src/main.rs:94:26: 94:31
        _14 = copy _2;                   // scope 3 at src/main.rs:94:26: 94:31
        _15 = AddWithOverflow(copy _11, copy _14); // scope 3 at src/main.rs:94:5: 94:31
        assert(!move (_15.1: bool), "attempt to compute `{} + {}`, which would overflow", move _11, move _14) -> [success: bb6, unwind: bb8]; // scope 3 at src/main.rs:94:5: 94:31
    }
    bb6: {
        _0 = move (_15.0: u32);          // scope 3 at src/main.rs:94:5: 94:31
        StorageDead(_14);                // scope 3 at src/main.rs:94:30: 94:31
        StorageDead(_11);                // scope 3 at src/main.rs:94:30: 94:31
        StorageDead(_7);                 // scope 2 at src/main.rs:95:1: 95:2
        drop(_5) -> [return: bb7, unwind: bb9]; // scope 1 at src/main.rs:95:1: 95:2
    }
    bb7: {
        StorageDead(_5);                 // scope 1 at src/main.rs:95:1: 95:2
        StorageDead(_2);                 // scope 0 at src/main.rs:95:1: 95:2
        return;                          // scope 0 at src/main.rs:95:2: 95:2
    }
    bb8 (cleanup): {
        drop(_5) -> [return: bb9, unwind terminate(cleanup)]; // scope 1 at src/main.rs:95:1: 95:2
    }
    bb9 (cleanup): {
        resume;                          // scope 0 at src/main.rs:89:1: 95:2
    }
}
"""


def review_why(text, name, line, statement, variable, projection=(), unsafe=None):
    return why(text, name, line, statement, variable, projection, unsafe, REVIEW_SOURCE)


def test_a_drop_that_runs_the_types_own_code_reads_the_local():
    # `struct D(u32, u32)` whose `Drop` prints `self.0`: after `let z = d.0;`, the drop at
    # the end reads the change. The copy analysis took every drop for no read, so a change
    # to `d.0` was refused with "Peeky proves no read of the changed part first".
    start, other = (("field", 0),), (("field", 1),)
    for text, name, line in ((SCOPED, "scoped", 19), (TAKEN, "taken", 25)):
        body = body_of(text)
        at = place(body, stop_text(text, line))
        local = body.bindings["d"][0][0]
        (copy,) = body.direct_copies_at(local, at, projection=start)
        assert not body.copy_undoes(copy, local, at, start), name
        assert review_why(text, name, line, stop_text(text, line), "d", start) == (None, None)
        assert body._drop_reads(f"drop(_{local}) -> [return: bb1, unwind: bb2];", local)
    # A String's drop runs no code of the program's; a field surely apart from the part
    # changed is not read by its drop; another local's drop reads none of this one.
    body = body_of(SCOPED)
    body.types[9] = "std::string::String"
    assert not body._drop_reads("drop(_9) -> [return: bb1, unwind: bb2];", 9)
    assert not body._drop_reads("drop((_2.1: u32)) -> [return: bb1, unwind: bb2];", 2, start)
    assert body._drop_reads("drop((_2.0: u32)) -> [return: bb1, unwind: bb2];", 2, start)
    assert not body._drop_reads("drop(_3) -> [return: bb1, unwind: bb2];", 2, other)


def stop_text(text, line):
    """The first statement of ``text`` on ``line``."""
    return stop(text, line, "")


def test_every_exit_of_the_frame_ends_its_locals_and_arguments():
    # `let z = x;`, the stop, `black_box(z + k);`, then `panic!` or `process::exit`: `x` is
    # never read again, and the program goes on with `z`. Only a `return;` ended an
    # argument, and a local only its `StorageDead`, which a panic's path has none of.
    for text, name, line, unsafe in (
        (PANICKED, "panicked", 32, None),
        # A `String` beside it: the panic goes through a cleanup block to `resume;`.
        (UNWOUND, "unwound", 42, None),
        # Where the crate has unsafe code, the never-read rule says nothing.
        (PANICKED, "panicked", 32, "the crate has unsafe code"),
        # And where a pointer into it may still be used (`black_box(r);`).
        (POINTED, "pointed", 52, None),
    ):
        body = body_of(text)
        at = place(body, stop_text(text, line))
        local = body.bindings["x"][0][0]
        (copy,) = body.direct_copies_at(local, at)
        assert body.copy_undoes(copy, local, at), name
        assert review_why(text, name, line, stop_text(text, line), "x", unsafe=unsafe) == (
            f"the program has already copied its value into `z` (at line {line - 1}) and goes "
            "on with that copy, " + SEEN.format("x"),
            f"copied into `z` (line {line - 1})",
        ), name
    owned = owned_of(POINTED, "pointed", stop_text(POINTED, 52), source=REVIEW_SOURCE)
    assert owned.unread_after(HASH, "x", 52, "/w/src/main.rs") is None
    # An async fn's body (a coroutine), where the never-read rule says nothing either.
    body = body_of(EXITED)
    at = place(body, stop_text(EXITED, 62))
    local = body.bindings["x"][0][0]
    (copy,) = body.direct_copies_at(local, at)
    assert body.copy_undoes(copy, local, at)
    # A path a call takes only when it panics ends nothing: `other()` may unwind to the
    # cleanup that drops `d`, which reads it there.
    body = body_of(SCOPED)
    assert body.blocks[1].unwinds == frozenset({5})


def test_where_a_value_ends_is_named_only_where_every_path_reaches_it():
    # `if n > 100 { panic!(…) }` before `total = k + n;`: that path never reaches line 73,
    # so "it is overwritten at line 73 first" alone was not true of every path.
    def reason(text, name, line):
        return review_why(text, name, line, stop_text(text, line), "total")

    body = body_of(BRANCH)
    kinds = {kind for kind, _ in body.unread_ends(2, place(body, stop_text(BRANCH, 69)))}
    assert kinds == {"write", "stop"}
    assert reason(BRANCH, "branch", 69) == (
        "the program does not read it after this stop, so a change would never be seen: it is "
        "overwritten at line 73 first, unless the program panics or exits before that",
        "not read after the stop (overwritten at line 73, or the program panics or exits)",
    )
    # A loop with no way out, on one path.
    body = body_of(SPIN)
    kinds = {kind for kind, _ in body.unread_ends(2, place(body, stop_text(SPIN, 79)))}
    assert kinds == {"write", "loop"}
    assert reason(SPIN, "spin", 79) == (
        "the program does not read it after this stop, so a change would never be seen: it is "
        "overwritten at line 85 first, unless the program loops forever before that",
        "not read after the stop (overwritten at line 85, or the program loops forever)",
    )
    # A call that may panic (`other()`, beside a `String` to drop) unwinds to a cleanup
    # block: that path reads nothing, and the line is named plainly.
    body = body_of(CLEANED)
    assert any(block.unwinds for block in body.blocks.values())
    assert reason(CLEANED, "cleaned", 92) == (
        "the program does not read it after this stop, so a change would never be seen: it is "
        "overwritten at line 93 first",
        "not read after the stop (overwritten at line 93)",
    )
    # Every path stops: no line to name.
    from peeky.rust import _unread_where

    assert _unread_where([("stop", "", 0)], "src/main.rs") == "the program panics or exits first"
    assert _unread_where([("stop", "", 0), ("loop", "", 0)], "src/main.rs") == (
        "the program panics or exits or loops forever first"
    )
