"""A value a call returns, given a pointer into the local or to what holds a copy of it,
may be a copy. `let y = x.clone();`, `s.push_str(&x.to_string());` and `let y = unsafe {
std::ptr::read(&x) };` copied `x`, but `x` stayed `[settable]` where the program goes on
with the copy, and a change there never reached it. Only the standard library's
measures and comparisons (`len`, `contains`, `eq`, `cmp`) are proven to return none.

After `let mut a = x; let b = std::mem::take(&mut a);` the refusal named `a`, which holds
its default now, not `b`."""

from types import SimpleNamespace

from peeky.rust import RustSession
from peeky.rust_mir import (
    _STD_CRATES,
    _STD_TYPES,
    IrFunction,
    OwnedLocals,
    _may_hold_type,
    _pointer_only,
    parse_body,
)

ALLOC = "/rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs"
# The crate the fixtures below are rustc 1.96.0's MIR of (`-Zmir-opt-level=0
# -Zdump-mir=PreCodegen -Zmir-include-spans=yes`, as Peeky builds it; the comment lines of
# its constants left out, and the standard library's macro file written {ALLOC}).
SOURCE = """use std::env;

#[inline(never)]
fn other() -> u32 {
    1
}

fn cloned(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let j = other(); // BEFORE
    let y = x.clone(); // CLONE
    let k = other(); // CLONED
    let t = y + j + k;
    std::hint::black_box(&x);
    t
}

fn texted(arg: u32) -> String {
    let x = std::convert::identity(arg);
    let mut s = String::new();
    s.push_str(&x.to_string()); // TEXT
    let k = other(); // TEXTED
    let t = format!("{}+{}", s, k);
    std::hint::black_box(&x);
    t
}

fn read(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let y = unsafe { std::ptr::read(&x) }; // READ
    let k = other(); // READS
    let t = y + k;
    std::hint::black_box(&x);
    t
}

fn taken(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut a = x; // MOVE
    let b = std::mem::take(&mut a); // TAKE
    let k = other(); // TAKEN
    let t = a + b + k;
    std::hint::black_box(&x);
    t
}

fn formatted(arg: u32) -> String {
    let x = std::convert::identity(arg);
    let s = format!("<{}>", x); // FORMAT
    let k = other(); // FORMATTED
    let t = format!("{}{}", s, k);
    std::hint::black_box(&x);
    t
}

fn extended(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut v = Vec::new();
    v.extend_from_slice(std::slice::from_ref(&x)); // EXTEND
    let k = other(); // EXTENDED
    let t = v[0] + k;
    std::hint::black_box(&x);
    t
}

fn measured(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let a = [x, 2];
    let n = a.len() + a.contains(&5) as usize;
    let e = x.eq(&5);
    let c = x.cmp(&5);
    let p = x.partial_cmp(&5);
    let k = other(); // MEASURED
    let t = n as u32 + e as u32 + c as u32 + p.is_some() as u32 + k;
    std::hint::black_box(&x);
    t
}

#[inline(never)]
fn hybrid(x: &u32) -> (u32, &'static ()) {
    (*x, &())
}

struct Hybrid<'a> {
    v: u32,
    r: &'a u32,
}

#[inline(never)]
fn hybrid_struct(x: &u32) -> Hybrid<'_> {
    Hybrid { v: *x, r: x }
}

fn paired(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let h = hybrid(&x); // PAIR
    let k = other(); // PAIRED
    let t = h.0 + k;
    std::hint::black_box(&x);
    t
}

fn held(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let h = hybrid_struct(&x); // HOLD
    let k = other(); // HELD
    let t = h.v + k;
    std::hint::black_box(h.r);
    t
}

fn main() {
    let x: u32 = env::args().nth(1).unwrap().parse().unwrap();
    println!("CLONED {}", cloned(x));
    println!("TEXTED {}", texted(x));
    println!("READS {}", read(x));
    println!("TAKEN {}", taken(x));
    println!("FORMATTED {}", formatted(x));
    println!("EXTENDED {}", extended(x));
    println!("MEASURED {}", measured(x));
    println!("PAIRED {}", paired(x));
    println!("HELD {}", held(x));
}
"""

CLONED = r"""// MIR for `cloned` after PreCodegen

fn cloned(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:8:11: 8:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:8:24: 8:27
    let _2: u32;                         // in scope 0 at src/main.rs:9:9: 9:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:9:36: 9:39
    let mut _6: &u32;                    // in scope 0 at src/main.rs:11:13: 11:14
    let mut _9: u32;                     // in scope 0 at src/main.rs:13:13: 13:18
    let mut _10: u32;                    // in scope 0 at src/main.rs:13:13: 13:14
    let mut _11: u32;                    // in scope 0 at src/main.rs:13:17: 13:18
    let mut _12: (u32, bool);            // in scope 0 at src/main.rs:13:13: 13:18
    let mut _13: u32;                    // in scope 0 at src/main.rs:13:21: 13:22
    let mut _14: (u32, bool);            // in scope 0 at src/main.rs:13:13: 13:22
    let _15: &u32;                       // in scope 0 at src/main.rs:14:5: 14:29
    let mut _16: &u32;                   // in scope 0 at src/main.rs:14:26: 14:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:9:9: 9:10
        let _4: u32;                     // in scope 1 at src/main.rs:10:9: 10:10
        scope 2 {
            debug j => _4;               // in scope 2 at src/main.rs:10:9: 10:10
            let _5: u32;                 // in scope 2 at src/main.rs:11:9: 11:10
            scope 3 {
                debug y => _5;           // in scope 3 at src/main.rs:11:9: 11:10
                let _7: u32;             // in scope 3 at src/main.rs:12:9: 12:10
                scope 4 {
                    debug k => _7;       // in scope 4 at src/main.rs:12:9: 12:10
                    let _8: u32;         // in scope 4 at src/main.rs:13:9: 13:10
                    scope 5 {
                        debug t => _8;   // in scope 5 at src/main.rs:13:9: 13:10
                    }
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:9:9: 9:10
        StorageLive(_3);                 // scope 0 at src/main.rs:9:36: 9:39
        _3 = copy _1;                    // scope 0 at src/main.rs:9:36: 9:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:9:13: 9:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:9:39: 9:40
        StorageLive(_4);                 // scope 1 at src/main.rs:10:9: 10:10
        _4 = other() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:10:13: 10:20
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:11:9: 11:10
        StorageLive(_6);                 // scope 2 at src/main.rs:11:13: 11:14
        _6 = &_2;                        // scope 2 at src/main.rs:11:13: 11:14
        _5 = <u32 as Clone>::clone(move _6) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:11:13: 11:22
    }

    bb3: {
        StorageDead(_6);                 // scope 2 at src/main.rs:11:21: 11:22
        StorageLive(_7);                 // scope 3 at src/main.rs:12:9: 12:10
        _7 = other() -> [return: bb4, unwind continue]; // scope 3 at src/main.rs:12:13: 12:20
    }

    bb4: {
        StorageLive(_8);                 // scope 4 at src/main.rs:13:9: 13:10
        StorageLive(_9);                 // scope 4 at src/main.rs:13:13: 13:18
        StorageLive(_10);                // scope 4 at src/main.rs:13:13: 13:14
        _10 = copy _5;                   // scope 4 at src/main.rs:13:13: 13:14
        StorageLive(_11);                // scope 4 at src/main.rs:13:17: 13:18
        _11 = copy _4;                   // scope 4 at src/main.rs:13:17: 13:18
        _12 = AddWithOverflow(copy _10, copy _11); // scope 4 at src/main.rs:13:13: 13:18
        assert(!move (_12.1: bool), "attempt to compute `{} + {}`, which would overflow", move _10, move _11) -> [success: bb5, unwind continue]; // scope 4 at src/main.rs:13:13: 13:18
    }

    bb5: {
        _9 = move (_12.0: u32);          // scope 4 at src/main.rs:13:13: 13:18
        StorageDead(_11);                // scope 4 at src/main.rs:13:17: 13:18
        StorageDead(_10);                // scope 4 at src/main.rs:13:17: 13:18
        StorageLive(_13);                // scope 4 at src/main.rs:13:21: 13:22
        _13 = copy _7;                   // scope 4 at src/main.rs:13:21: 13:22
        _14 = AddWithOverflow(copy _9, copy _13); // scope 4 at src/main.rs:13:13: 13:22
        assert(!move (_14.1: bool), "attempt to compute `{} + {}`, which would overflow", move _9, move _13) -> [success: bb6, unwind continue]; // scope 4 at src/main.rs:13:13: 13:22
    }

    bb6: {
        _8 = move (_14.0: u32);          // scope 4 at src/main.rs:13:13: 13:22
        StorageDead(_13);                // scope 4 at src/main.rs:13:21: 13:22
        StorageDead(_9);                 // scope 4 at src/main.rs:13:21: 13:22
        StorageLive(_15);                // scope 5 at src/main.rs:14:5: 14:29
        StorageLive(_16);                // scope 5 at src/main.rs:14:26: 14:28
        _16 = &_2;                       // scope 5 at src/main.rs:14:26: 14:28
        _15 = std::hint::black_box::<&u32>(move _16) -> [return: bb7, unwind continue]; // scope 5 at src/main.rs:14:5: 14:29
    }

    bb7: {
        StorageDead(_16);                // scope 5 at src/main.rs:14:28: 14:29
        StorageDead(_15);                // scope 5 at src/main.rs:14:29: 14:30
        _0 = copy _8;                    // scope 5 at src/main.rs:15:5: 15:6
        StorageDead(_8);                 // scope 4 at src/main.rs:16:1: 16:2
        StorageDead(_7);                 // scope 3 at src/main.rs:16:1: 16:2
        StorageDead(_5);                 // scope 2 at src/main.rs:16:1: 16:2
        StorageDead(_4);                 // scope 1 at src/main.rs:16:1: 16:2
        StorageDead(_2);                 // scope 0 at src/main.rs:16:1: 16:2
        return;                          // scope 0 at src/main.rs:16:2: 16:2
    }
}
"""

TEXTED = r"""// MIR for `texted` after PreCodegen

fn texted(_1: u32) -> String {
    debug arg => _1;                     // in scope 0 at src/main.rs:18:11: 18:14
    let mut _0: std::string::String;     // return place in scope 0 at src/main.rs:18:24: 18:30
    let _2: u32;                         // in scope 0 at src/main.rs:19:9: 19:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:19:36: 19:39
    let _5: ();                          // in scope 0 at src/main.rs:21:5: 21:31
    let mut _6: &mut std::string::String; // in scope 0 at src/main.rs:21:5: 21:6
    let mut _7: &str;                    // in scope 0 at src/main.rs:21:16: 21:30
    let _8: &str;                        // in scope 0 at src/main.rs:21:16: 21:30
    let mut _9: &std::string::String;    // in scope 0 at src/main.rs:21:16: 21:30
    let _10: &std::string::String;       // in scope 0 at src/main.rs:21:16: 21:30
    let _11: std::string::String;        // in scope 0 at src/main.rs:21:17: 21:30
    let mut _12: &u32;                   // in scope 0 at src/main.rs:21:17: 21:18
    let mut _15: std::string::String;    // in scope 0 at {ALLOC}:113:36: 115:10
    let mut _16: std::fmt::Arguments<'_>; // in scope 0 at {ALLOC}:114:33: 114:73
    let mut _18: &std::string::String;   // in scope 0 at src/main.rs:23:30: 23:31
    let mut _19: &u32;                   // in scope 0 at src/main.rs:23:33: 23:34
    let mut _21: core::fmt::rt::Argument<'_>; // in scope 0 at src/main.rs:23:22: 23:24
    let mut _22: &std::string::String;   // in scope 0 at src/main.rs:23:30: 23:31
    let mut _23: core::fmt::rt::Argument<'_>; // in scope 0 at src/main.rs:23:25: 23:27
    let mut _24: &u32;                   // in scope 0 at src/main.rs:23:33: 23:34
    let mut _25: &[u8; 5];               // in scope 0 at {ALLOC}:114:33: 114:73
    let _26: &[u8; 5];                   // in scope 0 at {ALLOC}:114:33: 114:73
    let mut _27: &[core::fmt::rt::Argument<'_>; 2]; // in scope 0 at {ALLOC}:114:33: 114:73
    let _28: &[core::fmt::rt::Argument<'_>; 2]; // in scope 0 at {ALLOC}:114:33: 114:73
    let _29: &u32;                       // in scope 0 at src/main.rs:24:5: 24:29
    let mut _30: &u32;                   // in scope 0 at src/main.rs:24:26: 24:28
    let mut _31: &std::string::String;   // in scope 0 at {ALLOC}:114:33: 114:73
    let mut _32: &u32;                   // in scope 0 at {ALLOC}:114:33: 114:73
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:19:9: 19:10
        let mut _4: std::string::String; // in scope 1 at src/main.rs:20:9: 20:14
        scope 2 {
            debug s => _4;               // in scope 2 at src/main.rs:20:9: 20:14
            let _13: u32;                // in scope 2 at src/main.rs:22:9: 22:10
            scope 3 {
                debug k => _13;          // in scope 3 at src/main.rs:22:9: 22:10
                let _14: std::string::String; // in scope 3 at src/main.rs:23:9: 23:10
                let _17: (&std::string::String, &u32); // in scope 3 at {ALLOC}:114:33: 114:73
                scope 4 {
                    debug t => _14;      // in scope 4 at src/main.rs:23:9: 23:10
                }
                scope 5 {
                    debug args => _17;   // in scope 5 at {ALLOC}:114:33: 114:73
                    let _20: [core::fmt::rt::Argument<'_>; 2]; // in scope 5 at {ALLOC}:114:33: 114:73
                    scope 6 {
                        debug args => _20; // in scope 6 at {ALLOC}:114:33: 114:73
                    }
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:19:9: 19:10
        StorageLive(_3);                 // scope 0 at src/main.rs:19:36: 19:39
        _3 = copy _1;                    // scope 0 at src/main.rs:19:36: 19:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:19:13: 19:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:19:39: 19:40
        StorageLive(_4);                 // scope 1 at src/main.rs:20:9: 20:14
        _4 = String::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:20:17: 20:30
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:21:5: 21:31
        StorageLive(_6);                 // scope 2 at src/main.rs:21:5: 21:6
        _6 = &mut _4;                    // scope 2 at src/main.rs:21:5: 21:6
        StorageLive(_7);                 // scope 2 at src/main.rs:21:16: 21:30
        StorageLive(_8);                 // scope 2 at src/main.rs:21:16: 21:30
        StorageLive(_9);                 // scope 2 at src/main.rs:21:16: 21:30
        StorageLive(_10);                // scope 2 at src/main.rs:21:16: 21:30
        StorageLive(_11);                // scope 2 at src/main.rs:21:17: 21:30
        StorageLive(_12);                // scope 2 at src/main.rs:21:17: 21:18
        _12 = &_2;                       // scope 2 at src/main.rs:21:17: 21:18
        _11 = <u32 as ToString>::to_string(move _12) -> [return: bb3, unwind: bb17]; // scope 2 at src/main.rs:21:17: 21:30
    }

    bb3: {
        StorageDead(_12);                // scope 2 at src/main.rs:21:29: 21:30
        _10 = &_11;                      // scope 2 at src/main.rs:21:16: 21:30
        _9 = &(*_10);                    // scope 2 at src/main.rs:21:16: 21:30
        _8 = <String as Deref>::deref(move _9) -> [return: bb4, unwind: bb16]; // scope 2 at src/main.rs:21:16: 21:30
    }

    bb4: {
        _7 = &(*_8);                     // scope 2 at src/main.rs:21:16: 21:30
        StorageDead(_9);                 // scope 2 at src/main.rs:21:29: 21:30
        _5 = String::push_str(move _6, move _7) -> [return: bb5, unwind: bb16]; // scope 2 at src/main.rs:21:5: 21:31
    }

    bb5: {
        StorageDead(_7);                 // scope 2 at src/main.rs:21:30: 21:31
        StorageDead(_6);                 // scope 2 at src/main.rs:21:30: 21:31
        drop(_11) -> [return: bb6, unwind: bb17]; // scope 2 at src/main.rs:21:31: 21:32
    }

    bb6: {
        StorageDead(_11);                // scope 2 at src/main.rs:21:31: 21:32
        StorageDead(_10);                // scope 2 at src/main.rs:21:31: 21:32
        StorageDead(_8);                 // scope 2 at src/main.rs:21:31: 21:32
        StorageDead(_5);                 // scope 2 at src/main.rs:21:31: 21:32
        StorageLive(_13);                // scope 2 at src/main.rs:22:9: 22:10
        _13 = other() -> [return: bb7, unwind: bb17]; // scope 2 at src/main.rs:22:13: 22:20
    }

    bb7: {
        StorageLive(_14);                // scope 3 at src/main.rs:23:9: 23:10
        StorageLive(_15);                // scope 3 at {ALLOC}:113:36: 115:10
        StorageLive(_16);                // scope 3 at {ALLOC}:114:33: 114:73
        StorageLive(_17);                // scope 3 at {ALLOC}:114:33: 114:73
        StorageLive(_18);                // scope 3 at src/main.rs:23:30: 23:31
        _18 = &_4;                       // scope 3 at src/main.rs:23:30: 23:31
        StorageLive(_19);                // scope 3 at src/main.rs:23:33: 23:34
        _19 = &_13;                      // scope 3 at src/main.rs:23:33: 23:34
        _17 = (move _18, move _19);      // scope 3 at {ALLOC}:114:33: 114:73
        StorageDead(_19);                // scope 3 at {ALLOC}:114:72: 114:73
        StorageDead(_18);                // scope 3 at {ALLOC}:114:72: 114:73
        StorageLive(_20);                // scope 5 at {ALLOC}:114:33: 114:73
        StorageLive(_21);                // scope 5 at src/main.rs:23:22: 23:24
        StorageLive(_22);                // scope 5 at src/main.rs:23:30: 23:31
        _31 = copy (_17.0: &std::string::String); // scope 5 at src/main.rs:23:30: 23:31
        _22 = &(*_31);                   // scope 5 at src/main.rs:23:30: 23:31
        _21 = core::fmt::rt::Argument::<'_>::new_display::<String>(move _22) -> [return: bb8, unwind: bb17]; // scope 5 at src/main.rs:23:22: 23:24
    }

    bb8: {
        StorageDead(_22);                // scope 5 at src/main.rs:23:23: 23:24
        StorageLive(_23);                // scope 5 at src/main.rs:23:25: 23:27
        StorageLive(_24);                // scope 5 at src/main.rs:23:33: 23:34
        _32 = copy (_17.1: &u32);        // scope 5 at src/main.rs:23:33: 23:34
        _24 = &(*_32);                   // scope 5 at src/main.rs:23:33: 23:34
        _23 = core::fmt::rt::Argument::<'_>::new_display::<u32>(move _24) -> [return: bb9, unwind: bb17]; // scope 5 at src/main.rs:23:25: 23:27
    }

    bb9: {
        StorageDead(_24);                // scope 5 at src/main.rs:23:26: 23:27
        _20 = [move _21, move _23];      // scope 5 at {ALLOC}:114:33: 114:73
        StorageDead(_23);                // scope 5 at {ALLOC}:114:72: 114:73
        StorageDead(_21);                // scope 5 at {ALLOC}:114:72: 114:73
        StorageLive(_25);                // scope 6 at {ALLOC}:114:33: 114:73
        StorageLive(_26);                // scope 6 at {ALLOC}:114:33: 114:73
        _26 = const b"\xc0\x01+\xc0\x00"; // scope 6 at {ALLOC}:114:33: 114:73
        _25 = &(*_26);                   // scope 6 at {ALLOC}:114:33: 114:73
        StorageLive(_27);                // scope 6 at {ALLOC}:114:33: 114:73
        StorageLive(_28);                // scope 6 at {ALLOC}:114:33: 114:73
        _28 = &_20;                      // scope 6 at {ALLOC}:114:33: 114:73
        _27 = &(*_28);                   // scope 6 at {ALLOC}:114:33: 114:73
        _16 = Arguments::<'_>::new::<5, 2>(move _25, move _27) -> [return: bb10, unwind: bb17]; // scope 6 at {ALLOC}:114:33: 114:73
    }

    bb10: {
        StorageDead(_28);                // scope 6 at {ALLOC}:114:72: 114:73
        StorageDead(_27);                // scope 6 at {ALLOC}:114:72: 114:73
        StorageDead(_26);                // scope 6 at {ALLOC}:114:72: 114:73
        StorageDead(_25);                // scope 6 at {ALLOC}:114:72: 114:73
        _15 = format(move _16) -> [return: bb11, unwind: bb17]; // scope 3 at {ALLOC}:114:13: 114:74
    }

    bb11: {
        StorageDead(_20);                // scope 3 at {ALLOC}:114:73: 114:74
        StorageDead(_17);                // scope 3 at {ALLOC}:114:73: 114:74
        StorageDead(_16);                // scope 3 at {ALLOC}:114:73: 114:74
        _14 = must_use::<String>(move _15) -> [return: bb12, unwind: bb17]; // scope 3 at {ALLOC}:113:9: 115:11
    }

    bb12: {
        StorageDead(_15);                // scope 3 at {ALLOC}:115:10: 115:11
        StorageLive(_29);                // scope 4 at src/main.rs:24:5: 24:29
        StorageLive(_30);                // scope 4 at src/main.rs:24:26: 24:28
        _30 = &_2;                       // scope 4 at src/main.rs:24:26: 24:28
        _29 = std::hint::black_box::<&u32>(move _30) -> [return: bb13, unwind: bb15]; // scope 4 at src/main.rs:24:5: 24:29
    }

    bb13: {
        StorageDead(_30);                // scope 4 at src/main.rs:24:28: 24:29
        StorageDead(_29);                // scope 4 at src/main.rs:24:29: 24:30
        _0 = move _14;                   // scope 4 at src/main.rs:25:5: 25:6
        StorageDead(_14);                // scope 3 at src/main.rs:26:1: 26:2
        StorageDead(_13);                // scope 2 at src/main.rs:26:1: 26:2
        drop(_4) -> [return: bb14, unwind: bb18]; // scope 1 at src/main.rs:26:1: 26:2
    }

    bb14: {
        StorageDead(_4);                 // scope 1 at src/main.rs:26:1: 26:2
        StorageDead(_2);                 // scope 0 at src/main.rs:26:1: 26:2
        return;                          // scope 0 at src/main.rs:26:2: 26:2
    }

    bb15 (cleanup): {
        drop(_14) -> [return: bb17, unwind terminate(cleanup)]; // scope 3 at src/main.rs:26:1: 26:2
    }

    bb16 (cleanup): {
        drop(_11) -> [return: bb17, unwind terminate(cleanup)]; // scope 2 at src/main.rs:21:31: 21:32
    }

    bb17 (cleanup): {
        drop(_4) -> [return: bb18, unwind terminate(cleanup)]; // scope 1 at src/main.rs:26:1: 26:2
    }

    bb18 (cleanup): {
        resume;                          // scope 0 at src/main.rs:18:1: 26:2
    }
}
""".replace("{ALLOC}", ALLOC)

READ = r"""// MIR for `read` after PreCodegen

fn read(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:28:9: 28:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:28:22: 28:25
    let _2: u32;                         // in scope 0 at src/main.rs:29:9: 29:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:29:36: 29:39
    let mut _5: *const u32;              // in scope 0 at src/main.rs:30:37: 30:39
    let _6: &u32;                        // in scope 0 at src/main.rs:30:37: 30:39
    let mut _9: u32;                     // in scope 0 at src/main.rs:32:13: 32:14
    let mut _10: u32;                    // in scope 0 at src/main.rs:32:17: 32:18
    let mut _11: (u32, bool);            // in scope 0 at src/main.rs:32:13: 32:18
    let _12: &u32;                       // in scope 0 at src/main.rs:33:5: 33:29
    let mut _13: &u32;                   // in scope 0 at src/main.rs:33:26: 33:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:29:9: 29:10
        let _4: u32;                     // in scope 1 at src/main.rs:30:9: 30:10
        scope 2 {
            debug y => _4;               // in scope 2 at src/main.rs:30:9: 30:10
            let _7: u32;                 // in scope 2 at src/main.rs:31:9: 31:10
            scope 3 {
                debug k => _7;           // in scope 3 at src/main.rs:31:9: 31:10
                let _8: u32;             // in scope 3 at src/main.rs:32:9: 32:10
                scope 4 {
                    debug t => _8;       // in scope 4 at src/main.rs:32:9: 32:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:29:9: 29:10
        StorageLive(_3);                 // scope 0 at src/main.rs:29:36: 29:39
        _3 = copy _1;                    // scope 0 at src/main.rs:29:36: 29:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:29:13: 29:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:29:39: 29:40
        StorageLive(_4);                 // scope 1 at src/main.rs:30:9: 30:10
        StorageLive(_5);                 // scope 1 at src/main.rs:30:37: 30:39
        StorageLive(_6);                 // scope 1 at src/main.rs:30:37: 30:39
        _6 = &_2;                        // scope 1 at src/main.rs:30:37: 30:39
        _5 = &raw const (*_6);           // scope 1 at src/main.rs:30:37: 30:39
        _4 = std::ptr::read::<u32>(move _5) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:30:22: 30:40
    }

    bb2: {
        StorageDead(_6);                 // scope 1 at src/main.rs:30:39: 30:40
        StorageDead(_5);                 // scope 1 at src/main.rs:30:39: 30:40
        StorageLive(_7);                 // scope 2 at src/main.rs:31:9: 31:10
        _7 = other() -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:31:13: 31:20
    }

    bb3: {
        StorageLive(_8);                 // scope 3 at src/main.rs:32:9: 32:10
        StorageLive(_9);                 // scope 3 at src/main.rs:32:13: 32:14
        _9 = copy _4;                    // scope 3 at src/main.rs:32:13: 32:14
        StorageLive(_10);                // scope 3 at src/main.rs:32:17: 32:18
        _10 = copy _7;                   // scope 3 at src/main.rs:32:17: 32:18
        _11 = AddWithOverflow(copy _9, copy _10); // scope 3 at src/main.rs:32:13: 32:18
        assert(!move (_11.1: bool), "attempt to compute `{} + {}`, which would overflow", move _9, move _10) -> [success: bb4, unwind continue]; // scope 3 at src/main.rs:32:13: 32:18
    }

    bb4: {
        _8 = move (_11.0: u32);          // scope 3 at src/main.rs:32:13: 32:18
        StorageDead(_10);                // scope 3 at src/main.rs:32:17: 32:18
        StorageDead(_9);                 // scope 3 at src/main.rs:32:17: 32:18
        StorageLive(_12);                // scope 4 at src/main.rs:33:5: 33:29
        StorageLive(_13);                // scope 4 at src/main.rs:33:26: 33:28
        _13 = &_2;                       // scope 4 at src/main.rs:33:26: 33:28
        _12 = std::hint::black_box::<&u32>(move _13) -> [return: bb5, unwind continue]; // scope 4 at src/main.rs:33:5: 33:29
    }

    bb5: {
        StorageDead(_13);                // scope 4 at src/main.rs:33:28: 33:29
        StorageDead(_12);                // scope 4 at src/main.rs:33:29: 33:30
        _0 = copy _8;                    // scope 4 at src/main.rs:34:5: 34:6
        StorageDead(_8);                 // scope 3 at src/main.rs:35:1: 35:2
        StorageDead(_7);                 // scope 2 at src/main.rs:35:1: 35:2
        StorageDead(_4);                 // scope 1 at src/main.rs:35:1: 35:2
        StorageDead(_2);                 // scope 0 at src/main.rs:35:1: 35:2
        return;                          // scope 0 at src/main.rs:35:2: 35:2
    }
}
"""

TAKEN = r"""// MIR for `taken` after PreCodegen

fn taken(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:37:10: 37:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:37:23: 37:26
    let _2: u32;                         // in scope 0 at src/main.rs:38:9: 38:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:38:36: 38:39
    let mut _6: &mut u32;                // in scope 0 at src/main.rs:40:28: 40:34
    let mut _7: &mut u32;                // in scope 0 at src/main.rs:40:28: 40:34
    let mut _10: u32;                    // in scope 0 at src/main.rs:42:13: 42:18
    let mut _11: u32;                    // in scope 0 at src/main.rs:42:13: 42:14
    let mut _12: u32;                    // in scope 0 at src/main.rs:42:17: 42:18
    let mut _13: (u32, bool);            // in scope 0 at src/main.rs:42:13: 42:18
    let mut _14: u32;                    // in scope 0 at src/main.rs:42:21: 42:22
    let mut _15: (u32, bool);            // in scope 0 at src/main.rs:42:13: 42:22
    let _16: &u32;                       // in scope 0 at src/main.rs:43:5: 43:29
    let mut _17: &u32;                   // in scope 0 at src/main.rs:43:26: 43:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:38:9: 38:10
        let mut _4: u32;                 // in scope 1 at src/main.rs:39:9: 39:14
        scope 2 {
            debug a => _4;               // in scope 2 at src/main.rs:39:9: 39:14
            let _5: u32;                 // in scope 2 at src/main.rs:40:9: 40:10
            scope 3 {
                debug b => _5;           // in scope 3 at src/main.rs:40:9: 40:10
                let _8: u32;             // in scope 3 at src/main.rs:41:9: 41:10
                scope 4 {
                    debug k => _8;       // in scope 4 at src/main.rs:41:9: 41:10
                    let _9: u32;         // in scope 4 at src/main.rs:42:9: 42:10
                    scope 5 {
                        debug t => _9;   // in scope 5 at src/main.rs:42:9: 42:10
                    }
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:38:9: 38:10
        StorageLive(_3);                 // scope 0 at src/main.rs:38:36: 38:39
        _3 = copy _1;                    // scope 0 at src/main.rs:38:36: 38:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:38:13: 38:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:38:39: 38:40
        StorageLive(_4);                 // scope 1 at src/main.rs:39:9: 39:14
        _4 = copy _2;                    // scope 1 at src/main.rs:39:17: 39:18
        StorageLive(_5);                 // scope 2 at src/main.rs:40:9: 40:10
        StorageLive(_6);                 // scope 2 at src/main.rs:40:28: 40:34
        StorageLive(_7);                 // scope 2 at src/main.rs:40:28: 40:34
        _7 = &mut _4;                    // scope 2 at src/main.rs:40:28: 40:34
        _6 = &mut (*_7);                 // scope 2 at src/main.rs:40:28: 40:34
        _5 = std::mem::take::<u32>(move _6) -> [return: bb2, unwind continue]; // scope 2 at src/main.rs:40:13: 40:35
    }

    bb2: {
        StorageDead(_6);                 // scope 2 at src/main.rs:40:34: 40:35
        StorageDead(_7);                 // scope 2 at src/main.rs:40:35: 40:36
        StorageLive(_8);                 // scope 3 at src/main.rs:41:9: 41:10
        _8 = other() -> [return: bb3, unwind continue]; // scope 3 at src/main.rs:41:13: 41:20
    }

    bb3: {
        StorageLive(_9);                 // scope 4 at src/main.rs:42:9: 42:10
        StorageLive(_10);                // scope 4 at src/main.rs:42:13: 42:18
        StorageLive(_11);                // scope 4 at src/main.rs:42:13: 42:14
        _11 = copy _4;                   // scope 4 at src/main.rs:42:13: 42:14
        StorageLive(_12);                // scope 4 at src/main.rs:42:17: 42:18
        _12 = copy _5;                   // scope 4 at src/main.rs:42:17: 42:18
        _13 = AddWithOverflow(copy _11, copy _12); // scope 4 at src/main.rs:42:13: 42:18
        assert(!move (_13.1: bool), "attempt to compute `{} + {}`, which would overflow", move _11, move _12) -> [success: bb4, unwind continue]; // scope 4 at src/main.rs:42:13: 42:18
    }

    bb4: {
        _10 = move (_13.0: u32);         // scope 4 at src/main.rs:42:13: 42:18
        StorageDead(_12);                // scope 4 at src/main.rs:42:17: 42:18
        StorageDead(_11);                // scope 4 at src/main.rs:42:17: 42:18
        StorageLive(_14);                // scope 4 at src/main.rs:42:21: 42:22
        _14 = copy _8;                   // scope 4 at src/main.rs:42:21: 42:22
        _15 = AddWithOverflow(copy _10, copy _14); // scope 4 at src/main.rs:42:13: 42:22
        assert(!move (_15.1: bool), "attempt to compute `{} + {}`, which would overflow", move _10, move _14) -> [success: bb5, unwind continue]; // scope 4 at src/main.rs:42:13: 42:22
    }

    bb5: {
        _9 = move (_15.0: u32);          // scope 4 at src/main.rs:42:13: 42:22
        StorageDead(_14);                // scope 4 at src/main.rs:42:21: 42:22
        StorageDead(_10);                // scope 4 at src/main.rs:42:21: 42:22
        StorageLive(_16);                // scope 5 at src/main.rs:43:5: 43:29
        StorageLive(_17);                // scope 5 at src/main.rs:43:26: 43:28
        _17 = &_2;                       // scope 5 at src/main.rs:43:26: 43:28
        _16 = std::hint::black_box::<&u32>(move _17) -> [return: bb6, unwind continue]; // scope 5 at src/main.rs:43:5: 43:29
    }

    bb6: {
        StorageDead(_17);                // scope 5 at src/main.rs:43:28: 43:29
        StorageDead(_16);                // scope 5 at src/main.rs:43:29: 43:30
        _0 = copy _9;                    // scope 5 at src/main.rs:44:5: 44:6
        StorageDead(_9);                 // scope 4 at src/main.rs:45:1: 45:2
        StorageDead(_8);                 // scope 3 at src/main.rs:45:1: 45:2
        StorageDead(_5);                 // scope 2 at src/main.rs:45:1: 45:2
        StorageDead(_4);                 // scope 1 at src/main.rs:45:1: 45:2
        StorageDead(_2);                 // scope 0 at src/main.rs:45:1: 45:2
        return;                          // scope 0 at src/main.rs:45:2: 45:2
    }
}
"""

FORMATTED = r"""// MIR for `formatted` after PreCodegen

fn formatted(_1: u32) -> String {
    debug arg => _1;                     // in scope 0 at src/main.rs:47:14: 47:17
    let mut _0: std::string::String;     // return place in scope 0 at src/main.rs:47:27: 47:33
    let _2: u32;                         // in scope 0 at src/main.rs:48:9: 48:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:48:36: 48:39
    let mut _5: std::string::String;     // in scope 0 at {ALLOC}:113:36: 115:10
    let mut _6: std::fmt::Arguments<'_>; // in scope 0 at {ALLOC}:114:33: 114:73
    let mut _8: &u32;                    // in scope 0 at src/main.rs:49:29: 49:30
    let mut _10: core::fmt::rt::Argument<'_>; // in scope 0 at src/main.rs:49:23: 49:25
    let mut _11: &u32;                   // in scope 0 at src/main.rs:49:29: 49:30
    let mut _12: &[u8; 6];               // in scope 0 at {ALLOC}:114:33: 114:73
    let _13: &[u8; 6];                   // in scope 0 at {ALLOC}:114:33: 114:73
    let mut _14: &[core::fmt::rt::Argument<'_>; 1]; // in scope 0 at {ALLOC}:114:33: 114:73
    let _15: &[core::fmt::rt::Argument<'_>; 1]; // in scope 0 at {ALLOC}:114:33: 114:73
    let mut _18: std::string::String;    // in scope 0 at {ALLOC}:113:36: 115:10
    let mut _19: std::fmt::Arguments<'_>; // in scope 0 at {ALLOC}:114:33: 114:73
    let mut _21: &std::string::String;   // in scope 0 at src/main.rs:51:29: 51:30
    let mut _22: &u32;                   // in scope 0 at src/main.rs:51:32: 51:33
    let mut _24: core::fmt::rt::Argument<'_>; // in scope 0 at src/main.rs:51:22: 51:24
    let mut _25: &std::string::String;   // in scope 0 at src/main.rs:51:29: 51:30
    let mut _26: core::fmt::rt::Argument<'_>; // in scope 0 at src/main.rs:51:24: 51:26
    let mut _27: &u32;                   // in scope 0 at src/main.rs:51:32: 51:33
    let mut _28: &[u8; 3];               // in scope 0 at {ALLOC}:114:33: 114:73
    let _29: &[u8; 3];                   // in scope 0 at {ALLOC}:114:33: 114:73
    let mut _30: &[core::fmt::rt::Argument<'_>; 2]; // in scope 0 at {ALLOC}:114:33: 114:73
    let _31: &[core::fmt::rt::Argument<'_>; 2]; // in scope 0 at {ALLOC}:114:33: 114:73
    let _32: &u32;                       // in scope 0 at src/main.rs:52:5: 52:29
    let mut _33: &u32;                   // in scope 0 at src/main.rs:52:26: 52:28
    let mut _34: &u32;                   // in scope 0 at {ALLOC}:114:33: 114:73
    let mut _35: &std::string::String;   // in scope 0 at {ALLOC}:114:33: 114:73
    let mut _36: &u32;                   // in scope 0 at {ALLOC}:114:33: 114:73
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:48:9: 48:10
        let _4: std::string::String;     // in scope 1 at src/main.rs:49:9: 49:10
        let _7: (&u32,);                 // in scope 1 at {ALLOC}:114:33: 114:73
        scope 2 {
            debug s => _4;               // in scope 2 at src/main.rs:49:9: 49:10
            let _16: u32;                // in scope 2 at src/main.rs:50:9: 50:10
            scope 5 {
                debug k => _16;          // in scope 5 at src/main.rs:50:9: 50:10
                let _17: std::string::String; // in scope 5 at src/main.rs:51:9: 51:10
                let _20: (&std::string::String, &u32); // in scope 5 at {ALLOC}:114:33: 114:73
                scope 6 {
                    debug t => _17;      // in scope 6 at src/main.rs:51:9: 51:10
                }
                scope 7 {
                    debug args => _20;   // in scope 7 at {ALLOC}:114:33: 114:73
                    let _23: [core::fmt::rt::Argument<'_>; 2]; // in scope 7 at {ALLOC}:114:33: 114:73
                    scope 8 {
                        debug args => _23; // in scope 8 at {ALLOC}:114:33: 114:73
                    }
                }
            }
        }
        scope 3 {
            debug args => _7;            // in scope 3 at {ALLOC}:114:33: 114:73
            let _9: [core::fmt::rt::Argument<'_>; 1]; // in scope 3 at {ALLOC}:114:33: 114:73
            scope 4 {
                debug args => _9;        // in scope 4 at {ALLOC}:114:33: 114:73
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:48:9: 48:10
        StorageLive(_3);                 // scope 0 at src/main.rs:48:36: 48:39
        _3 = copy _1;                    // scope 0 at src/main.rs:48:36: 48:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:48:13: 48:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:48:39: 48:40
        StorageLive(_4);                 // scope 1 at src/main.rs:49:9: 49:10
        StorageLive(_5);                 // scope 1 at {ALLOC}:113:36: 115:10
        StorageLive(_6);                 // scope 1 at {ALLOC}:114:33: 114:73
        StorageLive(_7);                 // scope 1 at {ALLOC}:114:33: 114:73
        StorageLive(_8);                 // scope 1 at src/main.rs:49:29: 49:30
        _8 = &_2;                        // scope 1 at src/main.rs:49:29: 49:30
        _7 = (move _8,);                 // scope 1 at {ALLOC}:114:33: 114:73
        StorageDead(_8);                 // scope 1 at {ALLOC}:114:72: 114:73
        StorageLive(_9);                 // scope 3 at {ALLOC}:114:33: 114:73
        StorageLive(_10);                // scope 3 at src/main.rs:49:23: 49:25
        StorageLive(_11);                // scope 3 at src/main.rs:49:29: 49:30
        _34 = copy (_7.0: &u32);         // scope 3 at src/main.rs:49:29: 49:30
        _11 = &(*_34);                   // scope 3 at src/main.rs:49:29: 49:30
        _10 = core::fmt::rt::Argument::<'_>::new_display::<u32>(move _11) -> [return: bb2, unwind continue]; // scope 3 at src/main.rs:49:23: 49:25
    }

    bb2: {
        StorageDead(_11);                // scope 3 at src/main.rs:49:24: 49:25
        _9 = [move _10];                 // scope 3 at {ALLOC}:114:33: 114:73
        StorageDead(_10);                // scope 3 at {ALLOC}:114:72: 114:73
        StorageLive(_12);                // scope 4 at {ALLOC}:114:33: 114:73
        StorageLive(_13);                // scope 4 at {ALLOC}:114:33: 114:73
        _13 = const b"\x01<\xc0\x01>\x00"; // scope 4 at {ALLOC}:114:33: 114:73
        _12 = &(*_13);                   // scope 4 at {ALLOC}:114:33: 114:73
        StorageLive(_14);                // scope 4 at {ALLOC}:114:33: 114:73
        StorageLive(_15);                // scope 4 at {ALLOC}:114:33: 114:73
        _15 = &_9;                       // scope 4 at {ALLOC}:114:33: 114:73
        _14 = &(*_15);                   // scope 4 at {ALLOC}:114:33: 114:73
        _6 = Arguments::<'_>::new::<6, 1>(move _12, move _14) -> [return: bb3, unwind continue]; // scope 4 at {ALLOC}:114:33: 114:73
    }

    bb3: {
        StorageDead(_15);                // scope 4 at {ALLOC}:114:72: 114:73
        StorageDead(_14);                // scope 4 at {ALLOC}:114:72: 114:73
        StorageDead(_13);                // scope 4 at {ALLOC}:114:72: 114:73
        StorageDead(_12);                // scope 4 at {ALLOC}:114:72: 114:73
        _5 = format(move _6) -> [return: bb4, unwind continue]; // scope 1 at {ALLOC}:114:13: 114:74
    }

    bb4: {
        StorageDead(_9);                 // scope 1 at {ALLOC}:114:73: 114:74
        StorageDead(_7);                 // scope 1 at {ALLOC}:114:73: 114:74
        StorageDead(_6);                 // scope 1 at {ALLOC}:114:73: 114:74
        _4 = must_use::<String>(move _5) -> [return: bb5, unwind: bb16]; // scope 1 at {ALLOC}:113:9: 115:11
    }

    bb5: {
        StorageDead(_5);                 // scope 1 at {ALLOC}:115:10: 115:11
        StorageLive(_16);                // scope 2 at src/main.rs:50:9: 50:10
        _16 = other() -> [return: bb6, unwind: bb15]; // scope 2 at src/main.rs:50:13: 50:20
    }

    bb6: {
        StorageLive(_17);                // scope 5 at src/main.rs:51:9: 51:10
        StorageLive(_18);                // scope 5 at {ALLOC}:113:36: 115:10
        StorageLive(_19);                // scope 5 at {ALLOC}:114:33: 114:73
        StorageLive(_20);                // scope 5 at {ALLOC}:114:33: 114:73
        StorageLive(_21);                // scope 5 at src/main.rs:51:29: 51:30
        _21 = &_4;                       // scope 5 at src/main.rs:51:29: 51:30
        StorageLive(_22);                // scope 5 at src/main.rs:51:32: 51:33
        _22 = &_16;                      // scope 5 at src/main.rs:51:32: 51:33
        _20 = (move _21, move _22);      // scope 5 at {ALLOC}:114:33: 114:73
        StorageDead(_22);                // scope 5 at {ALLOC}:114:72: 114:73
        StorageDead(_21);                // scope 5 at {ALLOC}:114:72: 114:73
        StorageLive(_23);                // scope 7 at {ALLOC}:114:33: 114:73
        StorageLive(_24);                // scope 7 at src/main.rs:51:22: 51:24
        StorageLive(_25);                // scope 7 at src/main.rs:51:29: 51:30
        _35 = copy (_20.0: &std::string::String); // scope 7 at src/main.rs:51:29: 51:30
        _25 = &(*_35);                   // scope 7 at src/main.rs:51:29: 51:30
        _24 = core::fmt::rt::Argument::<'_>::new_display::<String>(move _25) -> [return: bb7, unwind: bb15]; // scope 7 at src/main.rs:51:22: 51:24
    }

    bb7: {
        StorageDead(_25);                // scope 7 at src/main.rs:51:23: 51:24
        StorageLive(_26);                // scope 7 at src/main.rs:51:24: 51:26
        StorageLive(_27);                // scope 7 at src/main.rs:51:32: 51:33
        _36 = copy (_20.1: &u32);        // scope 7 at src/main.rs:51:32: 51:33
        _27 = &(*_36);                   // scope 7 at src/main.rs:51:32: 51:33
        _26 = core::fmt::rt::Argument::<'_>::new_display::<u32>(move _27) -> [return: bb8, unwind: bb15]; // scope 7 at src/main.rs:51:24: 51:26
    }

    bb8: {
        StorageDead(_27);                // scope 7 at src/main.rs:51:25: 51:26
        _23 = [move _24, move _26];      // scope 7 at {ALLOC}:114:33: 114:73
        StorageDead(_26);                // scope 7 at {ALLOC}:114:72: 114:73
        StorageDead(_24);                // scope 7 at {ALLOC}:114:72: 114:73
        StorageLive(_28);                // scope 8 at {ALLOC}:114:33: 114:73
        StorageLive(_29);                // scope 8 at {ALLOC}:114:33: 114:73
        _29 = const b"\xc0\xc0\x00";     // scope 8 at {ALLOC}:114:33: 114:73
        _28 = &(*_29);                   // scope 8 at {ALLOC}:114:33: 114:73
        StorageLive(_30);                // scope 8 at {ALLOC}:114:33: 114:73
        StorageLive(_31);                // scope 8 at {ALLOC}:114:33: 114:73
        _31 = &_23;                      // scope 8 at {ALLOC}:114:33: 114:73
        _30 = &(*_31);                   // scope 8 at {ALLOC}:114:33: 114:73
        _19 = Arguments::<'_>::new::<3, 2>(move _28, move _30) -> [return: bb9, unwind: bb15]; // scope 8 at {ALLOC}:114:33: 114:73
    }

    bb9: {
        StorageDead(_31);                // scope 8 at {ALLOC}:114:72: 114:73
        StorageDead(_30);                // scope 8 at {ALLOC}:114:72: 114:73
        StorageDead(_29);                // scope 8 at {ALLOC}:114:72: 114:73
        StorageDead(_28);                // scope 8 at {ALLOC}:114:72: 114:73
        _18 = format(move _19) -> [return: bb10, unwind: bb15]; // scope 5 at {ALLOC}:114:13: 114:74
    }

    bb10: {
        StorageDead(_23);                // scope 5 at {ALLOC}:114:73: 114:74
        StorageDead(_20);                // scope 5 at {ALLOC}:114:73: 114:74
        StorageDead(_19);                // scope 5 at {ALLOC}:114:73: 114:74
        _17 = must_use::<String>(move _18) -> [return: bb11, unwind: bb15]; // scope 5 at {ALLOC}:113:9: 115:11
    }

    bb11: {
        StorageDead(_18);                // scope 5 at {ALLOC}:115:10: 115:11
        StorageLive(_32);                // scope 6 at src/main.rs:52:5: 52:29
        StorageLive(_33);                // scope 6 at src/main.rs:52:26: 52:28
        _33 = &_2;                       // scope 6 at src/main.rs:52:26: 52:28
        _32 = std::hint::black_box::<&u32>(move _33) -> [return: bb12, unwind: bb14]; // scope 6 at src/main.rs:52:5: 52:29
    }

    bb12: {
        StorageDead(_33);                // scope 6 at src/main.rs:52:28: 52:29
        StorageDead(_32);                // scope 6 at src/main.rs:52:29: 52:30
        _0 = move _17;                   // scope 6 at src/main.rs:53:5: 53:6
        StorageDead(_17);                // scope 5 at src/main.rs:54:1: 54:2
        StorageDead(_16);                // scope 2 at src/main.rs:54:1: 54:2
        drop(_4) -> [return: bb13, unwind: bb16]; // scope 1 at src/main.rs:54:1: 54:2
    }

    bb13: {
        StorageDead(_4);                 // scope 1 at src/main.rs:54:1: 54:2
        StorageDead(_2);                 // scope 0 at src/main.rs:54:1: 54:2
        return;                          // scope 0 at src/main.rs:54:2: 54:2
    }

    bb14 (cleanup): {
        drop(_17) -> [return: bb15, unwind terminate(cleanup)]; // scope 5 at src/main.rs:54:1: 54:2
    }

    bb15 (cleanup): {
        drop(_4) -> [return: bb16, unwind terminate(cleanup)]; // scope 1 at src/main.rs:54:1: 54:2
    }

    bb16 (cleanup): {
        resume;                          // scope 0 at src/main.rs:47:1: 54:2
    }
}
""".replace("{ALLOC}", ALLOC)

EXTENDED = r"""// MIR for `extended` after PreCodegen

fn extended(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:56:13: 56:16
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:56:26: 56:29
    let _2: u32;                         // in scope 0 at src/main.rs:57:9: 57:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:57:36: 57:39
    let _5: ();                          // in scope 0 at src/main.rs:59:5: 59:50
    let mut _6: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:59:5: 59:6
    let mut _7: &[u32];                  // in scope 0 at src/main.rs:59:25: 59:49
    let _8: &[u32];                      // in scope 0 at src/main.rs:59:25: 59:49
    let mut _9: &u32;                    // in scope 0 at src/main.rs:59:46: 59:48
    let _10: &u32;                       // in scope 0 at src/main.rs:59:46: 59:48
    let mut _13: u32;                    // in scope 0 at src/main.rs:61:13: 61:17
    let mut _14: &u32;                   // in scope 0 at src/main.rs:61:14: 61:17
    let mut _15: &std::vec::Vec<u32>;    // in scope 0 at src/main.rs:61:13: 61:14
    let mut _16: u32;                    // in scope 0 at src/main.rs:61:20: 61:21
    let mut _17: (u32, bool);            // in scope 0 at src/main.rs:61:13: 61:21
    let _18: &u32;                       // in scope 0 at src/main.rs:62:5: 62:29
    let mut _19: &u32;                   // in scope 0 at src/main.rs:62:26: 62:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:57:9: 57:10
        let mut _4: std::vec::Vec<u32>;  // in scope 1 at src/main.rs:58:9: 58:14
        scope 2 {
            debug v => _4;               // in scope 2 at src/main.rs:58:9: 58:14
            let _11: u32;                // in scope 2 at src/main.rs:60:9: 60:10
            scope 3 {
                debug k => _11;          // in scope 3 at src/main.rs:60:9: 60:10
                let _12: u32;            // in scope 3 at src/main.rs:61:9: 61:10
                scope 4 {
                    debug t => _12;      // in scope 4 at src/main.rs:61:9: 61:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:57:9: 57:10
        StorageLive(_3);                 // scope 0 at src/main.rs:57:36: 57:39
        _3 = copy _1;                    // scope 0 at src/main.rs:57:36: 57:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:57:13: 57:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:57:39: 57:40
        StorageLive(_4);                 // scope 1 at src/main.rs:58:9: 58:14
        _4 = Vec::<u32>::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:58:17: 58:27
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:59:5: 59:50
        StorageLive(_6);                 // scope 2 at src/main.rs:59:5: 59:6
        _6 = &mut _4;                    // scope 2 at src/main.rs:59:5: 59:6
        StorageLive(_7);                 // scope 2 at src/main.rs:59:25: 59:49
        StorageLive(_8);                 // scope 2 at src/main.rs:59:25: 59:49
        StorageLive(_9);                 // scope 2 at src/main.rs:59:46: 59:48
        StorageLive(_10);                // scope 2 at src/main.rs:59:46: 59:48
        _10 = &_2;                       // scope 2 at src/main.rs:59:46: 59:48
        _9 = &(*_10);                    // scope 2 at src/main.rs:59:46: 59:48
        _8 = std::slice::from_ref::<u32>(move _9) -> [return: bb3, unwind: bb10]; // scope 2 at src/main.rs:59:25: 59:49
    }

    bb3: {
        _7 = &(*_8);                     // scope 2 at src/main.rs:59:25: 59:49
        StorageDead(_9);                 // scope 2 at src/main.rs:59:48: 59:49
        _5 = Vec::<u32>::extend_from_slice(move _6, move _7) -> [return: bb4, unwind: bb10]; // scope 2 at src/main.rs:59:5: 59:50
    }

    bb4: {
        StorageDead(_7);                 // scope 2 at src/main.rs:59:49: 59:50
        StorageDead(_6);                 // scope 2 at src/main.rs:59:49: 59:50
        StorageDead(_10);                // scope 2 at src/main.rs:59:50: 59:51
        StorageDead(_8);                 // scope 2 at src/main.rs:59:50: 59:51
        StorageDead(_5);                 // scope 2 at src/main.rs:59:50: 59:51
        StorageLive(_11);                // scope 2 at src/main.rs:60:9: 60:10
        _11 = other() -> [return: bb5, unwind: bb10]; // scope 2 at src/main.rs:60:13: 60:20
    }

    bb5: {
        StorageLive(_12);                // scope 3 at src/main.rs:61:9: 61:10
        StorageLive(_13);                // scope 3 at src/main.rs:61:13: 61:17
        StorageLive(_14);                // scope 3 at src/main.rs:61:14: 61:17
        StorageLive(_15);                // scope 3 at src/main.rs:61:13: 61:14
        _15 = &_4;                       // scope 3 at src/main.rs:61:13: 61:14
        _14 = <Vec<u32> as Index<usize>>::index(move _15, const 0_usize) -> [return: bb6, unwind: bb10]; // scope 3 at src/main.rs:61:14: 61:17
    }

    bb6: {
        _13 = copy (*_14);               // scope 3 at src/main.rs:61:13: 61:17
        StorageDead(_15);                // scope 3 at src/main.rs:61:16: 61:17
        StorageLive(_16);                // scope 3 at src/main.rs:61:20: 61:21
        _16 = copy _11;                  // scope 3 at src/main.rs:61:20: 61:21
        _17 = AddWithOverflow(copy _13, copy _16); // scope 3 at src/main.rs:61:13: 61:21
        assert(!move (_17.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _16) -> [success: bb7, unwind: bb10]; // scope 3 at src/main.rs:61:13: 61:21
    }

    bb7: {
        _12 = move (_17.0: u32);         // scope 3 at src/main.rs:61:13: 61:21
        StorageDead(_16);                // scope 3 at src/main.rs:61:20: 61:21
        StorageDead(_13);                // scope 3 at src/main.rs:61:20: 61:21
        StorageDead(_14);                // scope 3 at src/main.rs:61:21: 61:22
        StorageLive(_18);                // scope 4 at src/main.rs:62:5: 62:29
        StorageLive(_19);                // scope 4 at src/main.rs:62:26: 62:28
        _19 = &_2;                       // scope 4 at src/main.rs:62:26: 62:28
        _18 = std::hint::black_box::<&u32>(move _19) -> [return: bb8, unwind: bb10]; // scope 4 at src/main.rs:62:5: 62:29
    }

    bb8: {
        StorageDead(_19);                // scope 4 at src/main.rs:62:28: 62:29
        StorageDead(_18);                // scope 4 at src/main.rs:62:29: 62:30
        _0 = copy _12;                   // scope 4 at src/main.rs:63:5: 63:6
        StorageDead(_12);                // scope 3 at src/main.rs:64:1: 64:2
        StorageDead(_11);                // scope 2 at src/main.rs:64:1: 64:2
        drop(_4) -> [return: bb9, unwind: bb11]; // scope 1 at src/main.rs:64:1: 64:2
    }

    bb9: {
        StorageDead(_4);                 // scope 1 at src/main.rs:64:1: 64:2
        StorageDead(_2);                 // scope 0 at src/main.rs:64:1: 64:2
        return;                          // scope 0 at src/main.rs:64:2: 64:2
    }

    bb10 (cleanup): {
        drop(_4) -> [return: bb11, unwind terminate(cleanup)]; // scope 1 at src/main.rs:64:1: 64:2
    }

    bb11 (cleanup): {
        resume;                          // scope 0 at src/main.rs:56:1: 64:2
    }
}
"""

MEASURED = r"""// MIR for `measured` after PreCodegen

fn measured(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:66:13: 66:16
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:66:26: 66:29
    let _2: u32;                         // in scope 0 at src/main.rs:67:9: 67:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:67:36: 67:39
    let mut _5: u32;                     // in scope 0 at src/main.rs:68:14: 68:15
    let mut _7: usize;                   // in scope 0 at src/main.rs:69:13: 69:20
    let mut _8: &[u32];                  // in scope 0 at src/main.rs:69:13: 69:14
    let mut _9: &[u32; 2];               // in scope 0 at src/main.rs:69:13: 69:14
    let mut _10: usize;                  // in scope 0 at src/main.rs:69:23: 69:46
    let mut _11: bool;                   // in scope 0 at src/main.rs:69:23: 69:37
    let mut _12: &[u32];                 // in scope 0 at src/main.rs:69:23: 69:24
    let mut _13: &[u32; 2];              // in scope 0 at src/main.rs:69:23: 69:24
    let mut _14: &u32;                   // in scope 0 at src/main.rs:69:34: 69:36
    let _15: &u32;                       // in scope 0 at src/main.rs:69:34: 69:36
    let _16: u32;                        // in scope 0 at src/main.rs:69:35: 69:36
    let mut _17: (usize, bool);          // in scope 0 at src/main.rs:69:13: 69:46
    let mut _19: &u32;                   // in scope 0 at src/main.rs:70:13: 70:14
    let mut _20: &u32;                   // in scope 0 at src/main.rs:70:18: 70:20
    let _21: &u32;                       // in scope 0 at src/main.rs:70:18: 70:20
    let _22: u32;                        // in scope 0 at src/main.rs:70:19: 70:20
    let mut _24: &u32;                   // in scope 0 at src/main.rs:71:13: 71:14
    let mut _25: &u32;                   // in scope 0 at src/main.rs:71:19: 71:21
    let _26: &u32;                       // in scope 0 at src/main.rs:71:19: 71:21
    let _27: u32;                        // in scope 0 at src/main.rs:71:20: 71:21
    let mut _29: &u32;                   // in scope 0 at src/main.rs:72:13: 72:14
    let mut _30: &u32;                   // in scope 0 at src/main.rs:72:27: 72:29
    let _31: &u32;                       // in scope 0 at src/main.rs:72:27: 72:29
    let _32: u32;                        // in scope 0 at src/main.rs:72:28: 72:29
    let mut _35: u32;                    // in scope 0 at src/main.rs:74:13: 74:64
    let mut _36: u32;                    // in scope 0 at src/main.rs:74:13: 74:43
    let mut _37: u32;                    // in scope 0 at src/main.rs:74:13: 74:32
    let mut _38: u32;                    // in scope 0 at src/main.rs:74:13: 74:21
    let mut _39: usize;                  // in scope 0 at src/main.rs:74:13: 74:14
    let mut _40: u32;                    // in scope 0 at src/main.rs:74:24: 74:32
    let mut _41: bool;                   // in scope 0 at src/main.rs:74:24: 74:25
    let mut _42: (u32, bool);            // in scope 0 at src/main.rs:74:13: 74:32
    let mut _43: u32;                    // in scope 0 at src/main.rs:74:35: 74:43
    let _44: std::cmp::Ordering;         // in scope 0 at src/main.rs:74:35: 74:36
    let mut _45: i8;                     // in scope 0 at src/main.rs:74:35: 74:36
    let mut _46: (u32, bool);            // in scope 0 at src/main.rs:74:13: 74:43
    let mut _47: u32;                    // in scope 0 at src/main.rs:74:46: 74:64
    let mut _48: bool;                   // in scope 0 at src/main.rs:74:46: 74:57
    let mut _49: &std::option::Option<std::cmp::Ordering>; // in scope 0 at src/main.rs:74:46: 74:47
    let mut _50: (u32, bool);            // in scope 0 at src/main.rs:74:13: 74:64
    let mut _51: u32;                    // in scope 0 at src/main.rs:74:67: 74:68
    let mut _52: (u32, bool);            // in scope 0 at src/main.rs:74:13: 74:68
    let _53: &u32;                       // in scope 0 at src/main.rs:75:5: 75:29
    let mut _54: &u32;                   // in scope 0 at src/main.rs:75:26: 75:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:67:9: 67:10
        let _4: [u32; 2];                // in scope 1 at src/main.rs:68:9: 68:10
        scope 2 {
            debug a => _4;               // in scope 2 at src/main.rs:68:9: 68:10
            let _6: usize;               // in scope 2 at src/main.rs:69:9: 69:10
            let mut _58: &u32;           // in scope 2 at src/main.rs:69:34: 69:36
            scope 3 {
                debug n => _6;           // in scope 3 at src/main.rs:69:9: 69:10
                let _18: bool;           // in scope 3 at src/main.rs:70:9: 70:10
                let mut _57: &u32;       // in scope 3 at src/main.rs:70:18: 70:20
                scope 4 {
                    debug e => _18;      // in scope 4 at src/main.rs:70:9: 70:10
                    let _23: std::cmp::Ordering; // in scope 4 at src/main.rs:71:9: 71:10
                    let mut _56: &u32;   // in scope 4 at src/main.rs:71:19: 71:21
                    scope 5 {
                        debug c => _23;  // in scope 5 at src/main.rs:71:9: 71:10
                        let _28: std::option::Option<std::cmp::Ordering>; // in scope 5 at src/main.rs:72:9: 72:10
                        let mut _55: &u32; // in scope 5 at src/main.rs:72:27: 72:29
                        scope 6 {
                            debug p => _28; // in scope 6 at src/main.rs:72:9: 72:10
                            let _33: u32; // in scope 6 at src/main.rs:73:9: 73:10
                            scope 7 {
                                debug k => _33; // in scope 7 at src/main.rs:73:9: 73:10
                                let _34: u32; // in scope 7 at src/main.rs:74:9: 74:10
                                scope 8 {
                                    debug t => _34; // in scope 8 at src/main.rs:74:9: 74:10
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:67:9: 67:10
        StorageLive(_3);                 // scope 0 at src/main.rs:67:36: 67:39
        _3 = copy _1;                    // scope 0 at src/main.rs:67:36: 67:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:67:13: 67:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:67:39: 67:40
        StorageLive(_4);                 // scope 1 at src/main.rs:68:9: 68:10
        StorageLive(_5);                 // scope 1 at src/main.rs:68:14: 68:15
        _5 = copy _2;                    // scope 1 at src/main.rs:68:14: 68:15
        _4 = [move _5, const 2_u32];     // scope 1 at src/main.rs:68:13: 68:19
        StorageDead(_5);                 // scope 1 at src/main.rs:68:18: 68:19
        StorageLive(_6);                 // scope 2 at src/main.rs:69:9: 69:10
        StorageLive(_7);                 // scope 2 at src/main.rs:69:13: 69:20
        StorageLive(_8);                 // scope 2 at src/main.rs:69:13: 69:14
        StorageLive(_9);                 // scope 2 at src/main.rs:69:13: 69:14
        _9 = &_4;                        // scope 2 at src/main.rs:69:13: 69:14
        _8 = move _9 as &[u32] (PointerCoercion(Unsize, Implicit)); // scope 2 at src/main.rs:69:13: 69:14
        StorageDead(_9);                 // scope 2 at src/main.rs:69:13: 69:14
        _7 = core::slice::<impl [u32]>::len(move _8) -> [return: bb2, unwind continue]; // scope 2 at src/main.rs:69:13: 69:20
    }

    bb2: {
        StorageDead(_8);                 // scope 2 at src/main.rs:69:19: 69:20
        StorageLive(_10);                // scope 2 at src/main.rs:69:23: 69:46
        StorageLive(_11);                // scope 2 at src/main.rs:69:23: 69:37
        StorageLive(_12);                // scope 2 at src/main.rs:69:23: 69:24
        StorageLive(_13);                // scope 2 at src/main.rs:69:23: 69:24
        _13 = &_4;                       // scope 2 at src/main.rs:69:23: 69:24
        _12 = move _13 as &[u32] (PointerCoercion(Unsize, Implicit)); // scope 2 at src/main.rs:69:23: 69:24
        StorageDead(_13);                // scope 2 at src/main.rs:69:23: 69:24
        StorageLive(_14);                // scope 2 at src/main.rs:69:34: 69:36
        StorageLive(_15);                // scope 2 at src/main.rs:69:34: 69:36
        _58 = const measured::promoted[3]; // scope 2 at src/main.rs:69:34: 69:36
        _15 = &(*_58);                   // scope 2 at src/main.rs:69:34: 69:36
        _14 = &(*_15);                   // scope 2 at src/main.rs:69:34: 69:36
        _11 = core::slice::<impl [u32]>::contains(move _12, move _14) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:69:23: 69:37
    }

    bb3: {
        StorageDead(_14);                // scope 2 at src/main.rs:69:36: 69:37
        StorageDead(_12);                // scope 2 at src/main.rs:69:36: 69:37
        _10 = move _11 as usize (IntToInt); // scope 2 at src/main.rs:69:23: 69:46
        StorageDead(_11);                // scope 2 at src/main.rs:69:45: 69:46
        _17 = AddWithOverflow(copy _7, copy _10); // scope 2 at src/main.rs:69:13: 69:46
        assert(!move (_17.1: bool), "attempt to compute `{} + {}`, which would overflow", move _7, move _10) -> [success: bb4, unwind continue]; // scope 2 at src/main.rs:69:13: 69:46
    }

    bb4: {
        _6 = move (_17.0: usize);        // scope 2 at src/main.rs:69:13: 69:46
        StorageDead(_10);                // scope 2 at src/main.rs:69:45: 69:46
        StorageDead(_7);                 // scope 2 at src/main.rs:69:45: 69:46
        StorageDead(_15);                // scope 2 at src/main.rs:69:46: 69:47
        StorageLive(_18);                // scope 3 at src/main.rs:70:9: 70:10
        StorageLive(_19);                // scope 3 at src/main.rs:70:13: 70:14
        _19 = &_2;                       // scope 3 at src/main.rs:70:13: 70:14
        StorageLive(_20);                // scope 3 at src/main.rs:70:18: 70:20
        StorageLive(_21);                // scope 3 at src/main.rs:70:18: 70:20
        _57 = const measured::promoted[2]; // scope 3 at src/main.rs:70:18: 70:20
        _21 = &(*_57);                   // scope 3 at src/main.rs:70:18: 70:20
        _20 = &(*_21);                   // scope 3 at src/main.rs:70:18: 70:20
        _18 = <u32 as PartialEq>::eq(move _19, move _20) -> [return: bb5, unwind continue]; // scope 3 at src/main.rs:70:13: 70:21
    }

    bb5: {
        StorageDead(_20);                // scope 3 at src/main.rs:70:20: 70:21
        StorageDead(_19);                // scope 3 at src/main.rs:70:20: 70:21
        StorageDead(_21);                // scope 3 at src/main.rs:70:21: 70:22
        StorageLive(_23);                // scope 4 at src/main.rs:71:9: 71:10
        StorageLive(_24);                // scope 4 at src/main.rs:71:13: 71:14
        _24 = &_2;                       // scope 4 at src/main.rs:71:13: 71:14
        StorageLive(_25);                // scope 4 at src/main.rs:71:19: 71:21
        StorageLive(_26);                // scope 4 at src/main.rs:71:19: 71:21
        _56 = const measured::promoted[1]; // scope 4 at src/main.rs:71:19: 71:21
        _26 = &(*_56);                   // scope 4 at src/main.rs:71:19: 71:21
        _25 = &(*_26);                   // scope 4 at src/main.rs:71:19: 71:21
        _23 = <u32 as Ord>::cmp(move _24, move _25) -> [return: bb6, unwind continue]; // scope 4 at src/main.rs:71:13: 71:22
    }

    bb6: {
        StorageDead(_25);                // scope 4 at src/main.rs:71:21: 71:22
        StorageDead(_24);                // scope 4 at src/main.rs:71:21: 71:22
        StorageDead(_26);                // scope 4 at src/main.rs:71:22: 71:23
        StorageLive(_28);                // scope 5 at src/main.rs:72:9: 72:10
        StorageLive(_29);                // scope 5 at src/main.rs:72:13: 72:14
        _29 = &_2;                       // scope 5 at src/main.rs:72:13: 72:14
        StorageLive(_30);                // scope 5 at src/main.rs:72:27: 72:29
        StorageLive(_31);                // scope 5 at src/main.rs:72:27: 72:29
        _55 = const measured::promoted[0]; // scope 5 at src/main.rs:72:27: 72:29
        _31 = &(*_55);                   // scope 5 at src/main.rs:72:27: 72:29
        _30 = &(*_31);                   // scope 5 at src/main.rs:72:27: 72:29
        _28 = <u32 as PartialOrd>::partial_cmp(move _29, move _30) -> [return: bb7, unwind continue]; // scope 5 at src/main.rs:72:13: 72:30
    }

    bb7: {
        StorageDead(_30);                // scope 5 at src/main.rs:72:29: 72:30
        StorageDead(_29);                // scope 5 at src/main.rs:72:29: 72:30
        StorageDead(_31);                // scope 5 at src/main.rs:72:30: 72:31
        StorageLive(_33);                // scope 6 at src/main.rs:73:9: 73:10
        _33 = other() -> [return: bb8, unwind continue]; // scope 6 at src/main.rs:73:13: 73:20
    }

    bb8: {
        StorageLive(_34);                // scope 7 at src/main.rs:74:9: 74:10
        StorageLive(_35);                // scope 7 at src/main.rs:74:13: 74:64
        StorageLive(_36);                // scope 7 at src/main.rs:74:13: 74:43
        StorageLive(_37);                // scope 7 at src/main.rs:74:13: 74:32
        StorageLive(_38);                // scope 7 at src/main.rs:74:13: 74:21
        StorageLive(_39);                // scope 7 at src/main.rs:74:13: 74:14
        _39 = copy _6;                   // scope 7 at src/main.rs:74:13: 74:14
        _38 = move _39 as u32 (IntToInt); // scope 7 at src/main.rs:74:13: 74:21
        StorageDead(_39);                // scope 7 at src/main.rs:74:20: 74:21
        StorageLive(_40);                // scope 7 at src/main.rs:74:24: 74:32
        StorageLive(_41);                // scope 7 at src/main.rs:74:24: 74:25
        _41 = copy _18;                  // scope 7 at src/main.rs:74:24: 74:25
        _40 = move _41 as u32 (IntToInt); // scope 7 at src/main.rs:74:24: 74:32
        StorageDead(_41);                // scope 7 at src/main.rs:74:31: 74:32
        _42 = AddWithOverflow(copy _38, copy _40); // scope 7 at src/main.rs:74:13: 74:32
        assert(!move (_42.1: bool), "attempt to compute `{} + {}`, which would overflow", move _38, move _40) -> [success: bb9, unwind continue]; // scope 7 at src/main.rs:74:13: 74:32
    }

    bb9: {
        _37 = move (_42.0: u32);         // scope 7 at src/main.rs:74:13: 74:32
        StorageDead(_40);                // scope 7 at src/main.rs:74:31: 74:32
        StorageDead(_38);                // scope 7 at src/main.rs:74:31: 74:32
        StorageLive(_43);                // scope 7 at src/main.rs:74:35: 74:43
        StorageLive(_44);                // scope 7 at src/main.rs:74:35: 74:36
        _44 = copy _23;                  // scope 7 at src/main.rs:74:35: 74:36
        _45 = discriminant(_44);         // scope 7 at src/main.rs:74:35: 74:43
        _43 = move _45 as u32 (IntToInt); // scope 7 at src/main.rs:74:35: 74:43
        StorageDead(_44);                // scope 7 at src/main.rs:74:42: 74:43
        _46 = AddWithOverflow(copy _37, copy _43); // scope 7 at src/main.rs:74:13: 74:43
        assert(!move (_46.1: bool), "attempt to compute `{} + {}`, which would overflow", move _37, move _43) -> [success: bb10, unwind continue]; // scope 7 at src/main.rs:74:13: 74:43
    }

    bb10: {
        _36 = move (_46.0: u32);         // scope 7 at src/main.rs:74:13: 74:43
        StorageDead(_43);                // scope 7 at src/main.rs:74:42: 74:43
        StorageDead(_37);                // scope 7 at src/main.rs:74:42: 74:43
        StorageLive(_47);                // scope 7 at src/main.rs:74:46: 74:64
        StorageLive(_48);                // scope 7 at src/main.rs:74:46: 74:57
        StorageLive(_49);                // scope 7 at src/main.rs:74:46: 74:47
        _49 = &_28;                      // scope 7 at src/main.rs:74:46: 74:47
        _48 = Option::<std::cmp::Ordering>::is_some(move _49) -> [return: bb11, unwind continue]; // scope 7 at src/main.rs:74:46: 74:57
    }

    bb11: {
        StorageDead(_49);                // scope 7 at src/main.rs:74:56: 74:57
        _47 = move _48 as u32 (IntToInt); // scope 7 at src/main.rs:74:46: 74:64
        StorageDead(_48);                // scope 7 at src/main.rs:74:63: 74:64
        _50 = AddWithOverflow(copy _36, copy _47); // scope 7 at src/main.rs:74:13: 74:64
        assert(!move (_50.1: bool), "attempt to compute `{} + {}`, which would overflow", move _36, move _47) -> [success: bb12, unwind continue]; // scope 7 at src/main.rs:74:13: 74:64
    }

    bb12: {
        _35 = move (_50.0: u32);         // scope 7 at src/main.rs:74:13: 74:64
        StorageDead(_47);                // scope 7 at src/main.rs:74:63: 74:64
        StorageDead(_36);                // scope 7 at src/main.rs:74:63: 74:64
        StorageLive(_51);                // scope 7 at src/main.rs:74:67: 74:68
        _51 = copy _33;                  // scope 7 at src/main.rs:74:67: 74:68
        _52 = AddWithOverflow(copy _35, copy _51); // scope 7 at src/main.rs:74:13: 74:68
        assert(!move (_52.1: bool), "attempt to compute `{} + {}`, which would overflow", move _35, move _51) -> [success: bb13, unwind continue]; // scope 7 at src/main.rs:74:13: 74:68
    }

    bb13: {
        _34 = move (_52.0: u32);         // scope 7 at src/main.rs:74:13: 74:68
        StorageDead(_51);                // scope 7 at src/main.rs:74:67: 74:68
        StorageDead(_35);                // scope 7 at src/main.rs:74:67: 74:68
        StorageLive(_53);                // scope 8 at src/main.rs:75:5: 75:29
        StorageLive(_54);                // scope 8 at src/main.rs:75:26: 75:28
        _54 = &_2;                       // scope 8 at src/main.rs:75:26: 75:28
        _53 = std::hint::black_box::<&u32>(move _54) -> [return: bb14, unwind continue]; // scope 8 at src/main.rs:75:5: 75:29
    }

    bb14: {
        StorageDead(_54);                // scope 8 at src/main.rs:75:28: 75:29
        StorageDead(_53);                // scope 8 at src/main.rs:75:29: 75:30
        _0 = copy _34;                   // scope 8 at src/main.rs:76:5: 76:6
        StorageDead(_34);                // scope 7 at src/main.rs:77:1: 77:2
        StorageDead(_33);                // scope 6 at src/main.rs:77:1: 77:2
        StorageDead(_28);                // scope 5 at src/main.rs:77:1: 77:2
        StorageDead(_23);                // scope 4 at src/main.rs:77:1: 77:2
        StorageDead(_18);                // scope 3 at src/main.rs:77:1: 77:2
        StorageDead(_6);                 // scope 2 at src/main.rs:77:1: 77:2
        StorageDead(_4);                 // scope 1 at src/main.rs:77:1: 77:2
        StorageDead(_2);                 // scope 0 at src/main.rs:77:1: 77:2
        return;                          // scope 0 at src/main.rs:77:2: 77:2
    }
}
"""

PAIRED = r"""// MIR for `paired` after PreCodegen

fn paired(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:94:11: 94:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:94:24: 94:27
    let _2: u32;                         // in scope 0 at src/main.rs:95:9: 95:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:95:36: 95:39
    let mut _5: &u32;                    // in scope 0 at src/main.rs:96:20: 96:22
    let _6: &u32;                        // in scope 0 at src/main.rs:96:20: 96:22
    let mut _9: u32;                     // in scope 0 at src/main.rs:98:13: 98:16
    let mut _10: u32;                    // in scope 0 at src/main.rs:98:19: 98:20
    let mut _11: (u32, bool);            // in scope 0 at src/main.rs:98:13: 98:20
    let _12: &u32;                       // in scope 0 at src/main.rs:99:5: 99:29
    let mut _13: &u32;                   // in scope 0 at src/main.rs:99:26: 99:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:95:9: 95:10
        let _4: (u32, &());              // in scope 1 at src/main.rs:96:9: 96:10
        scope 2 {
            debug h => _4;               // in scope 2 at src/main.rs:96:9: 96:10
            let _7: u32;                 // in scope 2 at src/main.rs:97:9: 97:10
            scope 3 {
                debug k => _7;           // in scope 3 at src/main.rs:97:9: 97:10
                let _8: u32;             // in scope 3 at src/main.rs:98:9: 98:10
                scope 4 {
                    debug t => _8;       // in scope 4 at src/main.rs:98:9: 98:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:95:9: 95:10
        StorageLive(_3);                 // scope 0 at src/main.rs:95:36: 95:39
        _3 = copy _1;                    // scope 0 at src/main.rs:95:36: 95:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:95:13: 95:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:95:39: 95:40
        StorageLive(_4);                 // scope 1 at src/main.rs:96:9: 96:10
        StorageLive(_5);                 // scope 1 at src/main.rs:96:20: 96:22
        StorageLive(_6);                 // scope 1 at src/main.rs:96:20: 96:22
        _6 = &_2;                        // scope 1 at src/main.rs:96:20: 96:22
        _5 = &(*_6);                     // scope 1 at src/main.rs:96:20: 96:22
        _4 = hybrid(move _5) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:96:13: 96:23
    }

    bb2: {
        StorageDead(_5);                 // scope 1 at src/main.rs:96:22: 96:23
        StorageDead(_6);                 // scope 1 at src/main.rs:96:23: 96:24
        StorageLive(_7);                 // scope 2 at src/main.rs:97:9: 97:10
        _7 = other() -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:97:13: 97:20
    }

    bb3: {
        StorageLive(_8);                 // scope 3 at src/main.rs:98:9: 98:10
        StorageLive(_9);                 // scope 3 at src/main.rs:98:13: 98:16
        _9 = copy (_4.0: u32);           // scope 3 at src/main.rs:98:13: 98:16
        StorageLive(_10);                // scope 3 at src/main.rs:98:19: 98:20
        _10 = copy _7;                   // scope 3 at src/main.rs:98:19: 98:20
        _11 = AddWithOverflow(copy _9, copy _10); // scope 3 at src/main.rs:98:13: 98:20
        assert(!move (_11.1: bool), "attempt to compute `{} + {}`, which would overflow", move _9, move _10) -> [success: bb4, unwind continue]; // scope 3 at src/main.rs:98:13: 98:20
    }

    bb4: {
        _8 = move (_11.0: u32);          // scope 3 at src/main.rs:98:13: 98:20
        StorageDead(_10);                // scope 3 at src/main.rs:98:19: 98:20
        StorageDead(_9);                 // scope 3 at src/main.rs:98:19: 98:20
        StorageLive(_12);                // scope 4 at src/main.rs:99:5: 99:29
        StorageLive(_13);                // scope 4 at src/main.rs:99:26: 99:28
        _13 = &_2;                       // scope 4 at src/main.rs:99:26: 99:28
        _12 = std::hint::black_box::<&u32>(move _13) -> [return: bb5, unwind continue]; // scope 4 at src/main.rs:99:5: 99:29
    }

    bb5: {
        StorageDead(_13);                // scope 4 at src/main.rs:99:28: 99:29
        StorageDead(_12);                // scope 4 at src/main.rs:99:29: 99:30
        _0 = copy _8;                    // scope 4 at src/main.rs:100:5: 100:6
        StorageDead(_8);                 // scope 3 at src/main.rs:101:1: 101:2
        StorageDead(_7);                 // scope 2 at src/main.rs:101:1: 101:2
        StorageDead(_4);                 // scope 1 at src/main.rs:101:1: 101:2
        StorageDead(_2);                 // scope 0 at src/main.rs:101:1: 101:2
        return;                          // scope 0 at src/main.rs:101:2: 101:2
    }
}
"""

HELD = r"""// MIR for `held` after PreCodegen

fn held(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:103:9: 103:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:103:22: 103:25
    let _2: u32;                         // in scope 0 at src/main.rs:104:9: 104:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:104:36: 104:39
    let mut _5: &u32;                    // in scope 0 at src/main.rs:105:27: 105:29
    let _6: &u32;                        // in scope 0 at src/main.rs:105:27: 105:29
    let mut _9: u32;                     // in scope 0 at src/main.rs:107:13: 107:16
    let mut _10: u32;                    // in scope 0 at src/main.rs:107:19: 107:20
    let mut _11: (u32, bool);            // in scope 0 at src/main.rs:107:13: 107:20
    let _12: &u32;                       // in scope 0 at src/main.rs:108:5: 108:30
    let mut _13: &u32;                   // in scope 0 at src/main.rs:108:26: 108:29
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:104:9: 104:10
        let _4: Hybrid<'_>;              // in scope 1 at src/main.rs:105:9: 105:10
        scope 2 {
            debug h => _4;               // in scope 2 at src/main.rs:105:9: 105:10
            let _7: u32;                 // in scope 2 at src/main.rs:106:9: 106:10
            scope 3 {
                debug k => _7;           // in scope 3 at src/main.rs:106:9: 106:10
                let _8: u32;             // in scope 3 at src/main.rs:107:9: 107:10
                scope 4 {
                    debug t => _8;       // in scope 4 at src/main.rs:107:9: 107:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:104:9: 104:10
        StorageLive(_3);                 // scope 0 at src/main.rs:104:36: 104:39
        _3 = copy _1;                    // scope 0 at src/main.rs:104:36: 104:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:104:13: 104:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:104:39: 104:40
        StorageLive(_4);                 // scope 1 at src/main.rs:105:9: 105:10
        StorageLive(_5);                 // scope 1 at src/main.rs:105:27: 105:29
        StorageLive(_6);                 // scope 1 at src/main.rs:105:27: 105:29
        _6 = &_2;                        // scope 1 at src/main.rs:105:27: 105:29
        _5 = &(*_6);                     // scope 1 at src/main.rs:105:27: 105:29
        _4 = hybrid_struct(move _5) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:105:13: 105:30
    }

    bb2: {
        StorageDead(_5);                 // scope 1 at src/main.rs:105:29: 105:30
        StorageDead(_6);                 // scope 1 at src/main.rs:105:30: 105:31
        StorageLive(_7);                 // scope 2 at src/main.rs:106:9: 106:10
        _7 = other() -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:106:13: 106:20
    }

    bb3: {
        StorageLive(_8);                 // scope 3 at src/main.rs:107:9: 107:10
        StorageLive(_9);                 // scope 3 at src/main.rs:107:13: 107:16
        _9 = copy (_4.0: u32);           // scope 3 at src/main.rs:107:13: 107:16
        StorageLive(_10);                // scope 3 at src/main.rs:107:19: 107:20
        _10 = copy _7;                   // scope 3 at src/main.rs:107:19: 107:20
        _11 = AddWithOverflow(copy _9, copy _10); // scope 3 at src/main.rs:107:13: 107:20
        assert(!move (_11.1: bool), "attempt to compute `{} + {}`, which would overflow", move _9, move _10) -> [success: bb4, unwind continue]; // scope 3 at src/main.rs:107:13: 107:20
    }

    bb4: {
        _8 = move (_11.0: u32);          // scope 3 at src/main.rs:107:13: 107:20
        StorageDead(_10);                // scope 3 at src/main.rs:107:19: 107:20
        StorageDead(_9);                 // scope 3 at src/main.rs:107:19: 107:20
        StorageLive(_12);                // scope 4 at src/main.rs:108:5: 108:30
        StorageLive(_13);                // scope 4 at src/main.rs:108:26: 108:29
        _13 = copy (_4.1: &u32);         // scope 4 at src/main.rs:108:26: 108:29
        _12 = std::hint::black_box::<&u32>(move _13) -> [return: bb5, unwind continue]; // scope 4 at src/main.rs:108:5: 108:30
    }

    bb5: {
        StorageDead(_13);                // scope 4 at src/main.rs:108:29: 108:30
        StorageDead(_12);                // scope 4 at src/main.rs:108:30: 108:31
        _0 = copy _8;                    // scope 4 at src/main.rs:109:5: 109:6
        StorageDead(_8);                 // scope 3 at src/main.rs:110:1: 110:2
        StorageDead(_7);                 // scope 2 at src/main.rs:110:1: 110:2
        StorageDead(_4);                 // scope 1 at src/main.rs:110:1: 110:2
        StorageDead(_2);                 // scope 0 at src/main.rs:110:1: 110:2
        return;                          // scope 0 at src/main.rs:110:2: 110:2
    }
}
"""

# #320 review: a crate whose own `String` and `Vec` have a `len` that returns what it reads,
# and rustc 1.96.0's MIR of its `texts` and `items`.
OWNED = """use std::env;

// Types of the crate's own, named as the standard library's, with a `len` that returns
// what it reads.
struct String;

impl String {
    #[inline(never)]
    fn len(s: &std::string::String) -> usize {
        s.parse().unwrap()
    }
}

struct Vec;

impl Vec {
    #[inline(never)]
    fn len(v: &std::vec::Vec<u32>) -> usize {
        v[0] as usize
    }
}

#[inline(never)]
fn other() -> u32 {
    1
}

fn texts(arg: u32) -> usize {
    let x = std::convert::identity(arg);
    let n = String::len(&x.to_string()); // TEXT
    let k = other(); // TEXTS
    let t = n + k as usize;
    std::hint::black_box(&x);
    t
}

fn items(arg: u32) -> usize {
    let x = std::convert::identity(arg);
    let n = Vec::len(&vec![x, 2]); // ITEM
    let k = other(); // ITEMS
    let t = n + k as usize;
    std::hint::black_box(&x);
    t
}

fn main() {
    let x: u32 = env::args().nth(1).unwrap().parse().unwrap();
    println!("TEXTS {}", texts(x));
    println!("ITEMS {}", items(x));
}
"""

OWNED_TEXTS = r"""// MIR for `texts` after PreCodegen

fn texts(_1: u32) -> usize {
    debug arg => _1;                     // in scope 0 at src/main.rs:28:10: 28:13
    let mut _0: usize;                   // return place in scope 0 at src/main.rs:28:23: 28:28
    let _2: u32;                         // in scope 0 at src/main.rs:29:9: 29:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:29:36: 29:39
    let mut _5: &std::string::String;    // in scope 0 at src/main.rs:30:25: 30:39
    let _6: &std::string::String;        // in scope 0 at src/main.rs:30:25: 30:39
    let _7: std::string::String;         // in scope 0 at src/main.rs:30:26: 30:39
    let mut _8: &u32;                    // in scope 0 at src/main.rs:30:26: 30:27
    let mut _11: usize;                  // in scope 0 at src/main.rs:32:13: 32:14
    let mut _12: usize;                  // in scope 0 at src/main.rs:32:17: 32:27
    let mut _13: u32;                    // in scope 0 at src/main.rs:32:17: 32:18
    let mut _14: (usize, bool);          // in scope 0 at src/main.rs:32:13: 32:27
    let _15: &u32;                       // in scope 0 at src/main.rs:33:5: 33:29
    let mut _16: &u32;                   // in scope 0 at src/main.rs:33:26: 33:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:29:9: 29:10
        let _4: usize;                   // in scope 1 at src/main.rs:30:9: 30:10
        scope 2 {
            debug n => _4;               // in scope 2 at src/main.rs:30:9: 30:10
            let _9: u32;                 // in scope 2 at src/main.rs:31:9: 31:10
            scope 3 {
                debug k => _9;           // in scope 3 at src/main.rs:31:9: 31:10
                let _10: usize;          // in scope 3 at src/main.rs:32:9: 32:10
                scope 4 {
                    debug t => _10;      // in scope 4 at src/main.rs:32:9: 32:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:29:9: 29:10
        StorageLive(_3);                 // scope 0 at src/main.rs:29:36: 29:39
        _3 = copy _1;                    // scope 0 at src/main.rs:29:36: 29:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:29:13: 29:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:29:39: 29:40
        StorageLive(_4);                 // scope 1 at src/main.rs:30:9: 30:10
        StorageLive(_5);                 // scope 1 at src/main.rs:30:25: 30:39
        StorageLive(_6);                 // scope 1 at src/main.rs:30:25: 30:39
        StorageLive(_7);                 // scope 1 at src/main.rs:30:26: 30:39
        StorageLive(_8);                 // scope 1 at src/main.rs:30:26: 30:27
        _8 = &_2;                        // scope 1 at src/main.rs:30:26: 30:27
        _7 = <u32 as ToString>::to_string(move _8) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:30:26: 30:39
    }

    bb2: {
        StorageDead(_8);                 // scope 1 at src/main.rs:30:38: 30:39
        _6 = &_7;                        // scope 1 at src/main.rs:30:25: 30:39
        _5 = &(*_6);                     // scope 1 at src/main.rs:30:25: 30:39
        _4 = String::len(move _5) -> [return: bb3, unwind: bb8]; // scope 1 at src/main.rs:30:13: 30:40
    }

    bb3: {
        StorageDead(_5);                 // scope 1 at src/main.rs:30:39: 30:40
        drop(_7) -> [return: bb4, unwind: bb9]; // scope 1 at src/main.rs:30:40: 30:41
    }

    bb4: {
        StorageDead(_7);                 // scope 1 at src/main.rs:30:40: 30:41
        StorageDead(_6);                 // scope 1 at src/main.rs:30:40: 30:41
        StorageLive(_9);                 // scope 2 at src/main.rs:31:9: 31:10
        _9 = other() -> [return: bb5, unwind continue]; // scope 2 at src/main.rs:31:13: 31:20
    }

    bb5: {
        StorageLive(_10);                // scope 3 at src/main.rs:32:9: 32:10
        StorageLive(_11);                // scope 3 at src/main.rs:32:13: 32:14
        _11 = copy _4;                   // scope 3 at src/main.rs:32:13: 32:14
        StorageLive(_12);                // scope 3 at src/main.rs:32:17: 32:27
        StorageLive(_13);                // scope 3 at src/main.rs:32:17: 32:18
        _13 = copy _9;                   // scope 3 at src/main.rs:32:17: 32:18
        _12 = move _13 as usize (IntToInt); // scope 3 at src/main.rs:32:17: 32:27
        StorageDead(_13);                // scope 3 at src/main.rs:32:26: 32:27
        _14 = AddWithOverflow(copy _11, copy _12); // scope 3 at src/main.rs:32:13: 32:27
        assert(!move (_14.1: bool), "attempt to compute `{} + {}`, which would overflow", move _11, move _12) -> [success: bb6, unwind continue]; // scope 3 at src/main.rs:32:13: 32:27
    }

    bb6: {
        _10 = move (_14.0: usize);       // scope 3 at src/main.rs:32:13: 32:27
        StorageDead(_12);                // scope 3 at src/main.rs:32:26: 32:27
        StorageDead(_11);                // scope 3 at src/main.rs:32:26: 32:27
        StorageLive(_15);                // scope 4 at src/main.rs:33:5: 33:29
        StorageLive(_16);                // scope 4 at src/main.rs:33:26: 33:28
        _16 = &_2;                       // scope 4 at src/main.rs:33:26: 33:28
        _15 = std::hint::black_box::<&u32>(move _16) -> [return: bb7, unwind continue]; // scope 4 at src/main.rs:33:5: 33:29
    }

    bb7: {
        StorageDead(_16);                // scope 4 at src/main.rs:33:28: 33:29
        StorageDead(_15);                // scope 4 at src/main.rs:33:29: 33:30
        _0 = copy _10;                   // scope 4 at src/main.rs:34:5: 34:6
        StorageDead(_10);                // scope 3 at src/main.rs:35:1: 35:2
        StorageDead(_9);                 // scope 2 at src/main.rs:35:1: 35:2
        StorageDead(_4);                 // scope 1 at src/main.rs:35:1: 35:2
        StorageDead(_2);                 // scope 0 at src/main.rs:35:1: 35:2
        return;                          // scope 0 at src/main.rs:35:2: 35:2
    }

    bb8 (cleanup): {
        drop(_7) -> [return: bb9, unwind terminate(cleanup)]; // scope 1 at src/main.rs:30:40: 30:41
    }

    bb9 (cleanup): {
        resume;                          // scope 0 at src/main.rs:28:1: 35:2
    }
}
"""

OWNED_ITEMS = r"""// MIR for `items` after PreCodegen

fn items(_1: u32) -> usize {
    debug arg => _1;                     // in scope 0 at src/main.rs:37:10: 37:13
    let mut _0: usize;                   // return place in scope 0 at src/main.rs:37:23: 37:28
    let _2: u32;                         // in scope 0 at src/main.rs:38:9: 38:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:38:36: 38:39
    let mut _5: &std::vec::Vec<u32>;     // in scope 0 at src/main.rs:39:22: 39:33
    let _6: &std::vec::Vec<u32>;         // in scope 0 at src/main.rs:39:22: 39:33
    let _7: std::vec::Vec<u32>;          // in scope 0 at {ALLOC}:58:9: 60:10
    let mut _8: std::boxed::Box<std::mem::MaybeUninit<[u32; 2]>>; // in scope 0 at {ALLOC}:59:13: 59:96
    let mut _9: std::boxed::Box<std::mem::MaybeUninit<[u32; 2]>>; // in scope 0 at {ALLOC}:59:52: 59:84
    let mut _10: u32;                    // in scope 0 at src/main.rs:39:28: 39:29
    let mut _13: usize;                  // in scope 0 at src/main.rs:41:13: 41:14
    let mut _14: usize;                  // in scope 0 at src/main.rs:41:17: 41:27
    let mut _15: u32;                    // in scope 0 at src/main.rs:41:17: 41:18
    let mut _16: (usize, bool);          // in scope 0 at src/main.rs:41:13: 41:27
    let _17: &u32;                       // in scope 0 at src/main.rs:42:5: 42:29
    let mut _18: &u32;                   // in scope 0 at src/main.rs:42:26: 42:28
    let mut _19: *const std::mem::MaybeUninit<[u32; 2]>; // in scope 0 at {ALLOC}:59:52: 59:84
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:38:9: 38:10
        let _4: usize;                   // in scope 1 at src/main.rs:39:9: 39:10
        let mut _20: *const ();          // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _21: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _22: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _23: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _24: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _25: *const ();          // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _26: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _27: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _28: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _29: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _30: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        scope 2 {
            debug n => _4;               // in scope 2 at src/main.rs:39:9: 39:10
            let _11: u32;                // in scope 2 at src/main.rs:40:9: 40:10
            scope 3 {
                debug k => _11;          // in scope 3 at src/main.rs:40:9: 40:10
                let _12: usize;          // in scope 3 at src/main.rs:41:9: 41:10
                scope 4 {
                    debug t => _12;      // in scope 4 at src/main.rs:41:9: 41:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:38:9: 38:10
        StorageLive(_3);                 // scope 0 at src/main.rs:38:36: 38:39
        _3 = copy _1;                    // scope 0 at src/main.rs:38:36: 38:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:38:13: 38:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:38:39: 38:40
        StorageLive(_4);                 // scope 1 at src/main.rs:39:9: 39:10
        StorageLive(_5);                 // scope 1 at src/main.rs:39:22: 39:33
        StorageLive(_6);                 // scope 1 at src/main.rs:39:22: 39:33
        StorageLive(_7);                 // scope 1 at {ALLOC}:58:9: 60:10
        StorageLive(_8);                 // scope 1 at {ALLOC}:59:13: 59:96
        StorageLive(_9);                 // scope 1 at {ALLOC}:59:52: 59:84
        _9 = Box::<[u32; 2]>::new_uninit() -> [return: bb2, unwind continue]; // scope 1 at {ALLOC}:59:52: 59:84
    }

    bb2: {
        StorageLive(_10);                // scope 1 at src/main.rs:39:28: 39:29
        _10 = copy _2;                   // scope 1 at src/main.rs:39:28: 39:29
        _19 = copy ((_9.0: std::ptr::Unique<std::mem::MaybeUninit<[u32; 2]>>).0: std::ptr::NonNull<std::mem::MaybeUninit<[u32; 2]>>) as *const std::mem::MaybeUninit<[u32; 2]> (Transmute); // scope 1 at {ALLOC}:59:86: 59:95
        _20 = copy _19 as *const () (PtrToPtr); // scope 1 at {ALLOC}:59:86: 59:95
        _21 = copy _20 as usize (Transmute); // scope 1 at {ALLOC}:59:86: 59:95
        _22 = Sub(const <std::mem::MaybeUninit<[u32; 2]> as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 1 at {ALLOC}:59:86: 59:95
        _23 = BitAnd(copy _21, copy _22); // scope 1 at {ALLOC}:59:86: 59:95
        _24 = Eq(copy _23, const 0_usize); // scope 1 at {ALLOC}:59:86: 59:95
        assert(copy _24, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <std::mem::MaybeUninit<[u32; 2]> as std::mem::SizedTypeProperties>::ALIGN, copy _21) -> [success: bb11, unwind unreachable]; // scope 1 at {ALLOC}:59:86: 59:95
    }

    bb3: {
        StorageDead(_8);                 // scope 1 at {ALLOC}:60:9: 60:10
        _6 = &_7;                        // scope 1 at src/main.rs:39:22: 39:33
        _5 = &(*_6);                     // scope 1 at src/main.rs:39:22: 39:33
        _4 = Vec::len(move _5) -> [return: bb4, unwind: bb9]; // scope 1 at src/main.rs:39:13: 39:34
    }

    bb4: {
        StorageDead(_5);                 // scope 1 at src/main.rs:39:33: 39:34
        drop(_7) -> [return: bb5, unwind: bb10]; // scope 1 at src/main.rs:39:34: 39:35
    }

    bb5: {
        StorageDead(_7);                 // scope 1 at src/main.rs:39:34: 39:35
        StorageDead(_6);                 // scope 1 at src/main.rs:39:34: 39:35
        StorageLive(_11);                // scope 2 at src/main.rs:40:9: 40:10
        _11 = other() -> [return: bb6, unwind continue]; // scope 2 at src/main.rs:40:13: 40:20
    }

    bb6: {
        StorageLive(_12);                // scope 3 at src/main.rs:41:9: 41:10
        StorageLive(_13);                // scope 3 at src/main.rs:41:13: 41:14
        _13 = copy _4;                   // scope 3 at src/main.rs:41:13: 41:14
        StorageLive(_14);                // scope 3 at src/main.rs:41:17: 41:27
        StorageLive(_15);                // scope 3 at src/main.rs:41:17: 41:18
        _15 = copy _11;                  // scope 3 at src/main.rs:41:17: 41:18
        _14 = move _15 as usize (IntToInt); // scope 3 at src/main.rs:41:17: 41:27
        StorageDead(_15);                // scope 3 at src/main.rs:41:26: 41:27
        _16 = AddWithOverflow(copy _13, copy _14); // scope 3 at src/main.rs:41:13: 41:27
        assert(!move (_16.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _14) -> [success: bb7, unwind continue]; // scope 3 at src/main.rs:41:13: 41:27
    }

    bb7: {
        _12 = move (_16.0: usize);       // scope 3 at src/main.rs:41:13: 41:27
        StorageDead(_14);                // scope 3 at src/main.rs:41:26: 41:27
        StorageDead(_13);                // scope 3 at src/main.rs:41:26: 41:27
        StorageLive(_17);                // scope 4 at src/main.rs:42:5: 42:29
        StorageLive(_18);                // scope 4 at src/main.rs:42:26: 42:28
        _18 = &_2;                       // scope 4 at src/main.rs:42:26: 42:28
        _17 = std::hint::black_box::<&u32>(move _18) -> [return: bb8, unwind continue]; // scope 4 at src/main.rs:42:5: 42:29
    }

    bb8: {
        StorageDead(_18);                // scope 4 at src/main.rs:42:28: 42:29
        StorageDead(_17);                // scope 4 at src/main.rs:42:29: 42:30
        _0 = copy _12;                   // scope 4 at src/main.rs:43:5: 43:6
        StorageDead(_12);                // scope 3 at src/main.rs:44:1: 44:2
        StorageDead(_11);                // scope 2 at src/main.rs:44:1: 44:2
        StorageDead(_4);                 // scope 1 at src/main.rs:44:1: 44:2
        StorageDead(_2);                 // scope 0 at src/main.rs:44:1: 44:2
        return;                          // scope 0 at src/main.rs:44:2: 44:2
    }

    bb9 (cleanup): {
        drop(_7) -> [return: bb10, unwind terminate(cleanup)]; // scope 1 at src/main.rs:39:34: 39:35
    }

    bb10 (cleanup): {
        resume;                          // scope 0 at src/main.rs:37:1: 44:2
    }

    bb11: {
        _25 = copy _19 as *const () (PtrToPtr); // scope 1 at {ALLOC}:59:86: 59:95
        _26 = copy _25 as usize (Transmute); // scope 1 at {ALLOC}:59:86: 59:95
        _27 = Ne(const <std::mem::MaybeUninit<[u32; 2]> as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 1 at {ALLOC}:59:86: 59:95
        _28 = Eq(copy _26, const 0_usize); // scope 1 at {ALLOC}:59:86: 59:95
        _29 = BitAnd(copy _28, copy _27); // scope 1 at {ALLOC}:59:86: 59:95
        _30 = Not(copy _29);             // scope 1 at {ALLOC}:59:86: 59:95
        assert(copy _30, "null pointer dereference occurred") -> [success: bb12, unwind unreachable]; // scope 1 at {ALLOC}:59:86: 59:95
    }

    bb12: {
        ((((*_19).1: std::mem::ManuallyDrop<[u32; 2]>).0: std::mem::MaybeDangling<[u32; 2]>).0: [u32; 2]) = [move _10, const 2_u32]; // scope 1 at {ALLOC}:59:86: 59:95
        StorageDead(_10);                // scope 1 at {ALLOC}:59:94: 59:95
        _8 = move _9;                    // scope 1 at {ALLOC}:59:13: 59:96
        StorageDead(_9);                 // scope 1 at {ALLOC}:59:95: 59:96
        _7 = std::boxed::box_assume_init_into_vec_unsafe::<u32, 2>(move _8) -> [return: bb3, unwind: bb10]; // scope 1 at {ALLOC}:58:9: 60:10
    }
}
""".replace("{ALLOC}", ALLOC)

# #320 review round 2: a crate module named `alloc`, whose `str::Chars` holds a value beside
# a reference, and the standard library's iterator adapters; rustc 1.96.0's MIR of its
# `shadowed`, `reversed`, `mapped` and `lent`.
ADAPTERS = """use std::env;

// A module of the crate's own named like the standard library's `alloc`, whose `Chars`
// holds a value beside a reference.
mod alloc {
    pub mod str {
        pub struct Chars<'a> {
            pub v: u32,
            pub r: &'a (),
        }
    }
}

#[inline(never)]
fn other() -> u32 {
    1
}

#[inline(never)]
fn mark() {}

#[inline(never)]
fn shadow(x: &u32) -> alloc::str::Chars<'static> {
    alloc::str::Chars { v: *x, r: &() }
}

#[inline(never)]
fn rev_of(s: &str) -> std::iter::Rev<std::str::Chars<'_>> {
    s.chars().rev()
}

fn shadowed(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let c = shadow(&x); // SHADOW
    let k = other(); // SHADOWED
    let t = c.v + k;
    std::hint::black_box((&x, c.r));
    t
}

fn reversed() -> usize {
    let mut s = String::with_capacity(16);
    s.push_str("abc");
    let it = s.chars().rev(); // REV
    mark(); // REVERSED
    let n = it.count();
    let made = rev_of(&s); // MADE
    mark(); // REMADE
    let m = made.count();
    s.push('!');
    n + m
}

fn mapped(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let a = [1u32, 2];
    let it = a.iter().map(move |e| e + x); // MAP
    let k = other(); // MAPPED
    let t = it.sum::<u32>() + k;
    std::hint::black_box(&x);
    t
}

fn lent(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let a = [1u32, 2];
    let it = a.iter().map(|e| e + x); // LEND
    let k = other(); // LENT
    let t = it.sum::<u32>() + k;
    std::hint::black_box(&x);
    t
}

fn main() {
    let x: u32 = env::args().nth(1).unwrap().parse().unwrap();
    println!("SHADOWED {}", shadowed(x));
    println!("REVERSED {}", reversed());
    println!("MAPPED {}", mapped(x));
    println!("LENT {}", lent(x));
}
"""

SHADOWED = r"""// MIR for `shadowed` after PreCodegen

fn shadowed(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:32:13: 32:16
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:32:26: 32:29
    let _2: u32;                         // in scope 0 at src/main.rs:33:9: 33:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:33:36: 33:39
    let mut _5: &u32;                    // in scope 0 at src/main.rs:34:20: 34:22
    let _6: &u32;                        // in scope 0 at src/main.rs:34:20: 34:22
    let mut _9: u32;                     // in scope 0 at src/main.rs:36:13: 36:16
    let mut _10: u32;                    // in scope 0 at src/main.rs:36:19: 36:20
    let mut _11: (u32, bool);            // in scope 0 at src/main.rs:36:13: 36:20
    let _12: (&u32, &());                // in scope 0 at src/main.rs:37:5: 37:36
    let mut _13: (&u32, &());            // in scope 0 at src/main.rs:37:26: 37:35
    let mut _14: &u32;                   // in scope 0 at src/main.rs:37:27: 37:29
    let mut _15: &();                    // in scope 0 at src/main.rs:37:31: 37:34
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:33:9: 33:10
        let _4: alloc::str::Chars<'_>;   // in scope 1 at src/main.rs:34:9: 34:10
        scope 2 {
            debug c => _4;               // in scope 2 at src/main.rs:34:9: 34:10
            let _7: u32;                 // in scope 2 at src/main.rs:35:9: 35:10
            scope 3 {
                debug k => _7;           // in scope 3 at src/main.rs:35:9: 35:10
                let _8: u32;             // in scope 3 at src/main.rs:36:9: 36:10
                scope 4 {
                    debug t => _8;       // in scope 4 at src/main.rs:36:9: 36:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:33:9: 33:10
        StorageLive(_3);                 // scope 0 at src/main.rs:33:36: 33:39
        _3 = copy _1;                    // scope 0 at src/main.rs:33:36: 33:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:33:13: 33:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:33:39: 33:40
        StorageLive(_4);                 // scope 1 at src/main.rs:34:9: 34:10
        StorageLive(_5);                 // scope 1 at src/main.rs:34:20: 34:22
        StorageLive(_6);                 // scope 1 at src/main.rs:34:20: 34:22
        _6 = &_2;                        // scope 1 at src/main.rs:34:20: 34:22
        _5 = &(*_6);                     // scope 1 at src/main.rs:34:20: 34:22
        _4 = shadow(move _5) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:34:13: 34:23
    }

    bb2: {
        StorageDead(_5);                 // scope 1 at src/main.rs:34:22: 34:23
        StorageDead(_6);                 // scope 1 at src/main.rs:34:23: 34:24
        StorageLive(_7);                 // scope 2 at src/main.rs:35:9: 35:10
        _7 = other() -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:35:13: 35:20
    }

    bb3: {
        StorageLive(_8);                 // scope 3 at src/main.rs:36:9: 36:10
        StorageLive(_9);                 // scope 3 at src/main.rs:36:13: 36:16
        _9 = copy (_4.0: u32);           // scope 3 at src/main.rs:36:13: 36:16
        StorageLive(_10);                // scope 3 at src/main.rs:36:19: 36:20
        _10 = copy _7;                   // scope 3 at src/main.rs:36:19: 36:20
        _11 = AddWithOverflow(copy _9, copy _10); // scope 3 at src/main.rs:36:13: 36:20
        assert(!move (_11.1: bool), "attempt to compute `{} + {}`, which would overflow", move _9, move _10) -> [success: bb4, unwind continue]; // scope 3 at src/main.rs:36:13: 36:20
    }

    bb4: {
        _8 = move (_11.0: u32);          // scope 3 at src/main.rs:36:13: 36:20
        StorageDead(_10);                // scope 3 at src/main.rs:36:19: 36:20
        StorageDead(_9);                 // scope 3 at src/main.rs:36:19: 36:20
        StorageLive(_12);                // scope 4 at src/main.rs:37:5: 37:36
        StorageLive(_13);                // scope 4 at src/main.rs:37:26: 37:35
        StorageLive(_14);                // scope 4 at src/main.rs:37:27: 37:29
        _14 = &_2;                       // scope 4 at src/main.rs:37:27: 37:29
        StorageLive(_15);                // scope 4 at src/main.rs:37:31: 37:34
        _15 = copy (_4.1: &());          // scope 4 at src/main.rs:37:31: 37:34
        _13 = (move _14, move _15);      // scope 4 at src/main.rs:37:26: 37:35
        StorageDead(_15);                // scope 4 at src/main.rs:37:34: 37:35
        StorageDead(_14);                // scope 4 at src/main.rs:37:34: 37:35
        _12 = std::hint::black_box::<(&u32, &())>(move _13) -> [return: bb5, unwind continue]; // scope 4 at src/main.rs:37:5: 37:36
    }

    bb5: {
        StorageDead(_13);                // scope 4 at src/main.rs:37:35: 37:36
        StorageDead(_12);                // scope 4 at src/main.rs:37:36: 37:37
        _0 = copy _8;                    // scope 4 at src/main.rs:38:5: 38:6
        StorageDead(_8);                 // scope 3 at src/main.rs:39:1: 39:2
        StorageDead(_7);                 // scope 2 at src/main.rs:39:1: 39:2
        StorageDead(_4);                 // scope 1 at src/main.rs:39:1: 39:2
        StorageDead(_2);                 // scope 0 at src/main.rs:39:1: 39:2
        return;                          // scope 0 at src/main.rs:39:2: 39:2
    }
}
"""

REVERSED = r"""// MIR for `reversed` after PreCodegen

fn reversed() -> usize {
    let mut _0: usize;                   // return place in scope 0 at src/main.rs:41:18: 41:23
    let mut _1: std::string::String;     // in scope 0 at src/main.rs:42:9: 42:14
    let _2: ();                          // in scope 0 at src/main.rs:43:5: 43:22
    let mut _3: &mut std::string::String; // in scope 0 at src/main.rs:43:5: 43:6
    let mut _4: &str;                    // in scope 0 at src/main.rs:43:16: 43:21
    let _5: &str;                        // in scope 0 at src/main.rs:43:16: 43:21
    let mut _7: std::str::Chars<'_>;     // in scope 0 at src/main.rs:44:14: 44:23
    let mut _8: &str;                    // in scope 0 at src/main.rs:44:14: 44:15
    let _9: &str;                        // in scope 0 at src/main.rs:44:14: 44:23
    let mut _10: &std::string::String;   // in scope 0 at src/main.rs:44:14: 44:15
    let _11: ();                         // in scope 0 at src/main.rs:45:5: 45:11
    let mut _13: std::iter::Rev<std::str::Chars<'_>>; // in scope 0 at src/main.rs:46:13: 46:15
    let mut _15: &str;                   // in scope 0 at src/main.rs:47:23: 47:25
    let _16: &str;                       // in scope 0 at src/main.rs:47:23: 47:25
    let mut _17: &std::string::String;   // in scope 0 at src/main.rs:47:23: 47:25
    let _18: &std::string::String;       // in scope 0 at src/main.rs:47:23: 47:25
    let _19: ();                         // in scope 0 at src/main.rs:48:5: 48:11
    let mut _21: std::iter::Rev<std::str::Chars<'_>>; // in scope 0 at src/main.rs:49:13: 49:17
    let _22: ();                         // in scope 0 at src/main.rs:50:5: 50:16
    let mut _23: &mut std::string::String; // in scope 0 at src/main.rs:50:5: 50:6
    let mut _24: usize;                  // in scope 0 at src/main.rs:51:5: 51:6
    let mut _25: usize;                  // in scope 0 at src/main.rs:51:9: 51:10
    let mut _26: (usize, bool);          // in scope 0 at src/main.rs:51:5: 51:10
    scope 1 {
        debug s => _1;                   // in scope 1 at src/main.rs:42:9: 42:14
        let _6: std::iter::Rev<std::str::Chars<'_>>; // in scope 1 at src/main.rs:44:9: 44:11
        scope 2 {
            debug it => _6;              // in scope 2 at src/main.rs:44:9: 44:11
            let _12: usize;              // in scope 2 at src/main.rs:46:9: 46:10
            scope 3 {
                debug n => _12;          // in scope 3 at src/main.rs:46:9: 46:10
                let _14: std::iter::Rev<std::str::Chars<'_>>; // in scope 3 at src/main.rs:47:9: 47:13
                scope 4 {
                    debug made => _14;   // in scope 4 at src/main.rs:47:9: 47:13
                    let _20: usize;      // in scope 4 at src/main.rs:49:9: 49:10
                    scope 5 {
                        debug m => _20;  // in scope 5 at src/main.rs:49:9: 49:10
                    }
                }
            }
        }
    }

    bb0: {
        StorageLive(_1);                 // scope 0 at src/main.rs:42:9: 42:14
        _1 = String::with_capacity(const 16_usize) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:42:17: 42:42
    }

    bb1: {
        StorageLive(_2);                 // scope 1 at src/main.rs:43:5: 43:22
        StorageLive(_3);                 // scope 1 at src/main.rs:43:5: 43:6
        _3 = &mut _1;                    // scope 1 at src/main.rs:43:5: 43:6
        StorageLive(_4);                 // scope 1 at src/main.rs:43:16: 43:21
        StorageLive(_5);                 // scope 1 at src/main.rs:43:16: 43:21
        _5 = const "abc";                // scope 1 at src/main.rs:43:16: 43:21
        _4 = &(*_5);                     // scope 1 at src/main.rs:43:16: 43:21
        _2 = String::push_str(move _3, move _4) -> [return: bb2, unwind: bb15]; // scope 1 at src/main.rs:43:5: 43:22
    }

    bb2: {
        StorageDead(_4);                 // scope 1 at src/main.rs:43:21: 43:22
        StorageDead(_3);                 // scope 1 at src/main.rs:43:21: 43:22
        StorageDead(_5);                 // scope 1 at src/main.rs:43:22: 43:23
        StorageDead(_2);                 // scope 1 at src/main.rs:43:22: 43:23
        StorageLive(_6);                 // scope 1 at src/main.rs:44:9: 44:11
        StorageLive(_7);                 // scope 1 at src/main.rs:44:14: 44:23
        StorageLive(_8);                 // scope 1 at src/main.rs:44:14: 44:15
        StorageLive(_9);                 // scope 1 at src/main.rs:44:14: 44:23
        StorageLive(_10);                // scope 1 at src/main.rs:44:14: 44:15
        _10 = &_1;                       // scope 1 at src/main.rs:44:14: 44:15
        _9 = <String as Deref>::deref(move _10) -> [return: bb3, unwind: bb15]; // scope 1 at src/main.rs:44:14: 44:23
    }

    bb3: {
        _8 = &(*_9);                     // scope 1 at src/main.rs:44:14: 44:15
        StorageDead(_10);                // scope 1 at src/main.rs:44:14: 44:15
        _7 = core::str::<impl str>::chars(move _8) -> [return: bb4, unwind: bb15]; // scope 1 at src/main.rs:44:14: 44:23
    }

    bb4: {
        StorageDead(_8);                 // scope 1 at src/main.rs:44:22: 44:23
        _6 = <std::str::Chars<'_> as Iterator>::rev(move _7) -> [return: bb5, unwind: bb15]; // scope 1 at src/main.rs:44:14: 44:29
    }

    bb5: {
        StorageDead(_7);                 // scope 1 at src/main.rs:44:28: 44:29
        StorageDead(_9);                 // scope 1 at src/main.rs:44:29: 44:30
        StorageLive(_11);                // scope 2 at src/main.rs:45:5: 45:11
        _11 = mark() -> [return: bb6, unwind: bb15]; // scope 2 at src/main.rs:45:5: 45:11
    }

    bb6: {
        StorageDead(_11);                // scope 2 at src/main.rs:45:11: 45:12
        StorageLive(_12);                // scope 2 at src/main.rs:46:9: 46:10
        StorageLive(_13);                // scope 2 at src/main.rs:46:13: 46:15
        _13 = move _6;                   // scope 2 at src/main.rs:46:13: 46:15
        _12 = <Rev<std::str::Chars<'_>> as Iterator>::count(move _13) -> [return: bb7, unwind: bb15]; // scope 2 at src/main.rs:46:13: 46:23
    }

    bb7: {
        StorageDead(_13);                // scope 2 at src/main.rs:46:22: 46:23
        StorageLive(_14);                // scope 3 at src/main.rs:47:9: 47:13
        StorageLive(_15);                // scope 3 at src/main.rs:47:23: 47:25
        StorageLive(_16);                // scope 3 at src/main.rs:47:23: 47:25
        StorageLive(_17);                // scope 3 at src/main.rs:47:23: 47:25
        StorageLive(_18);                // scope 3 at src/main.rs:47:23: 47:25
        _18 = &_1;                       // scope 3 at src/main.rs:47:23: 47:25
        _17 = &(*_18);                   // scope 3 at src/main.rs:47:23: 47:25
        _16 = <String as Deref>::deref(move _17) -> [return: bb8, unwind: bb15]; // scope 3 at src/main.rs:47:23: 47:25
    }

    bb8: {
        _15 = &(*_16);                   // scope 3 at src/main.rs:47:23: 47:25
        StorageDead(_17);                // scope 3 at src/main.rs:47:24: 47:25
        _14 = rev_of(move _15) -> [return: bb9, unwind: bb15]; // scope 3 at src/main.rs:47:16: 47:26
    }

    bb9: {
        StorageDead(_15);                // scope 3 at src/main.rs:47:25: 47:26
        StorageDead(_18);                // scope 3 at src/main.rs:47:26: 47:27
        StorageDead(_16);                // scope 3 at src/main.rs:47:26: 47:27
        StorageLive(_19);                // scope 4 at src/main.rs:48:5: 48:11
        _19 = mark() -> [return: bb10, unwind: bb15]; // scope 4 at src/main.rs:48:5: 48:11
    }

    bb10: {
        StorageDead(_19);                // scope 4 at src/main.rs:48:11: 48:12
        StorageLive(_20);                // scope 4 at src/main.rs:49:9: 49:10
        StorageLive(_21);                // scope 4 at src/main.rs:49:13: 49:17
        _21 = move _14;                  // scope 4 at src/main.rs:49:13: 49:17
        _20 = <Rev<std::str::Chars<'_>> as Iterator>::count(move _21) -> [return: bb11, unwind: bb15]; // scope 4 at src/main.rs:49:13: 49:25
    }

    bb11: {
        StorageDead(_21);                // scope 4 at src/main.rs:49:24: 49:25
        StorageLive(_22);                // scope 5 at src/main.rs:50:5: 50:16
        StorageLive(_23);                // scope 5 at src/main.rs:50:5: 50:6
        _23 = &mut _1;                   // scope 5 at src/main.rs:50:5: 50:6
        _22 = String::push(move _23, const '!') -> [return: bb12, unwind: bb15]; // scope 5 at src/main.rs:50:5: 50:16
    }

    bb12: {
        StorageDead(_23);                // scope 5 at src/main.rs:50:15: 50:16
        StorageDead(_22);                // scope 5 at src/main.rs:50:16: 50:17
        StorageLive(_24);                // scope 5 at src/main.rs:51:5: 51:6
        _24 = copy _12;                  // scope 5 at src/main.rs:51:5: 51:6
        StorageLive(_25);                // scope 5 at src/main.rs:51:9: 51:10
        _25 = copy _20;                  // scope 5 at src/main.rs:51:9: 51:10
        _26 = AddWithOverflow(copy _24, copy _25); // scope 5 at src/main.rs:51:5: 51:10
        assert(!move (_26.1: bool), "attempt to compute `{} + {}`, which would overflow", move _24, move _25) -> [success: bb13, unwind: bb15]; // scope 5 at src/main.rs:51:5: 51:10
    }

    bb13: {
        _0 = move (_26.0: usize);        // scope 5 at src/main.rs:51:5: 51:10
        StorageDead(_25);                // scope 5 at src/main.rs:51:9: 51:10
        StorageDead(_24);                // scope 5 at src/main.rs:51:9: 51:10
        StorageDead(_20);                // scope 4 at src/main.rs:52:1: 52:2
        StorageDead(_14);                // scope 3 at src/main.rs:52:1: 52:2
        StorageDead(_12);                // scope 2 at src/main.rs:52:1: 52:2
        StorageDead(_6);                 // scope 1 at src/main.rs:52:1: 52:2
        drop(_1) -> [return: bb14, unwind: bb16]; // scope 0 at src/main.rs:52:1: 52:2
    }

    bb14: {
        StorageDead(_1);                 // scope 0 at src/main.rs:52:1: 52:2
        return;                          // scope 0 at src/main.rs:52:2: 52:2
    }

    bb15 (cleanup): {
        drop(_1) -> [return: bb16, unwind terminate(cleanup)]; // scope 0 at src/main.rs:52:1: 52:2
    }

    bb16 (cleanup): {
        resume;                          // scope 0 at src/main.rs:41:1: 52:2
    }
}
"""

MAPPED = r"""// MIR for `mapped` after PreCodegen

fn mapped(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:54:11: 54:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:54:24: 54:27
    let _2: u32;                         // in scope 0 at src/main.rs:55:9: 55:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:55:36: 55:39
    let mut _6: std::slice::Iter<'_, u32>; // in scope 0 at src/main.rs:57:14: 57:22
    let mut _7: &[u32];                  // in scope 0 at src/main.rs:57:14: 57:15
    let mut _8: &[u32; 2];               // in scope 0 at src/main.rs:57:14: 57:15
    let mut _9: {closure@src/main.rs:57:27: 57:35}; // in scope 0 at src/main.rs:57:27: 57:41
    let mut _12: u32;                    // in scope 0 at src/main.rs:59:13: 59:28
    let mut _13: std::iter::Map<std::slice::Iter<'_, u32>, {closure@src/main.rs:57:27: 57:35}>; // in scope 0 at src/main.rs:59:13: 59:15
    let mut _14: u32;                    // in scope 0 at src/main.rs:59:31: 59:32
    let mut _15: (u32, bool);            // in scope 0 at src/main.rs:59:13: 59:32
    let _16: &u32;                       // in scope 0 at src/main.rs:60:5: 60:29
    let mut _17: &u32;                   // in scope 0 at src/main.rs:60:26: 60:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:55:9: 55:10
        let _4: [u32; 2];                // in scope 1 at src/main.rs:56:9: 56:10
        scope 2 {
            debug a => _4;               // in scope 2 at src/main.rs:56:9: 56:10
            let _5: std::iter::Map<std::slice::Iter<'_, u32>, {closure@src/main.rs:57:27: 57:35}>; // in scope 2 at src/main.rs:57:9: 57:11
            scope 3 {
                debug it => _5;          // in scope 3 at src/main.rs:57:9: 57:11
                let _10: u32;            // in scope 3 at src/main.rs:58:9: 58:10
                scope 4 {
                    debug k => _10;      // in scope 4 at src/main.rs:58:9: 58:10
                    let _11: u32;        // in scope 4 at src/main.rs:59:9: 59:10
                    scope 5 {
                        debug t => _11;  // in scope 5 at src/main.rs:59:9: 59:10
                    }
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:55:9: 55:10
        StorageLive(_3);                 // scope 0 at src/main.rs:55:36: 55:39
        _3 = copy _1;                    // scope 0 at src/main.rs:55:36: 55:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:55:13: 55:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:55:39: 55:40
        StorageLive(_4);                 // scope 1 at src/main.rs:56:9: 56:10
        _4 = [const 1_u32, const 2_u32]; // scope 1 at src/main.rs:56:13: 56:22
        StorageLive(_5);                 // scope 2 at src/main.rs:57:9: 57:11
        StorageLive(_6);                 // scope 2 at src/main.rs:57:14: 57:22
        StorageLive(_7);                 // scope 2 at src/main.rs:57:14: 57:15
        StorageLive(_8);                 // scope 2 at src/main.rs:57:14: 57:15
        _8 = &_4;                        // scope 2 at src/main.rs:57:14: 57:15
        _7 = move _8 as &[u32] (PointerCoercion(Unsize, Implicit)); // scope 2 at src/main.rs:57:14: 57:15
        StorageDead(_8);                 // scope 2 at src/main.rs:57:14: 57:15
        _6 = core::slice::<impl [u32]>::iter(move _7) -> [return: bb2, unwind continue]; // scope 2 at src/main.rs:57:14: 57:22
    }

    bb2: {
        StorageDead(_7);                 // scope 2 at src/main.rs:57:21: 57:22
        StorageLive(_9);                 // scope 2 at src/main.rs:57:27: 57:41
        _9 = {closure@src/main.rs:57:27: 57:35} { x: copy _2 }; // scope 2 at src/main.rs:57:27: 57:41
                                         // closure
                                         //     i16,
                                         //     Binder { value: extern "RustCall" fn((&'{erased} u32,)) -> u32, bound_vars: [] },
                                         //     (u32,),
                                         // ]
        _5 = <std::slice::Iter<'_, u32> as Iterator>::map::<u32, {closure@src/main.rs:57:27: 57:35}>(move _6, move _9) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:57:14: 57:42
    }

    bb3: {
        StorageDead(_9);                 // scope 2 at src/main.rs:57:41: 57:42
        StorageDead(_6);                 // scope 2 at src/main.rs:57:41: 57:42
        StorageLive(_10);                // scope 3 at src/main.rs:58:9: 58:10
        _10 = other() -> [return: bb4, unwind continue]; // scope 3 at src/main.rs:58:13: 58:20
    }

    bb4: {
        StorageLive(_11);                // scope 4 at src/main.rs:59:9: 59:10
        StorageLive(_12);                // scope 4 at src/main.rs:59:13: 59:28
        StorageLive(_13);                // scope 4 at src/main.rs:59:13: 59:15
        _13 = move _5;                   // scope 4 at src/main.rs:59:13: 59:15
        _12 = <Map<std::slice::Iter<'_, u32>, {closure@src/main.rs:57:27: 57:35}> as Iterator>::sum::<u32>(move _13) -> [return: bb5, unwind continue]; // scope 4 at src/main.rs:59:13: 59:28
    }

    bb5: {
        StorageDead(_13);                // scope 4 at src/main.rs:59:27: 59:28
        StorageLive(_14);                // scope 4 at src/main.rs:59:31: 59:32
        _14 = copy _10;                  // scope 4 at src/main.rs:59:31: 59:32
        _15 = AddWithOverflow(copy _12, copy _14); // scope 4 at src/main.rs:59:13: 59:32
        assert(!move (_15.1: bool), "attempt to compute `{} + {}`, which would overflow", move _12, move _14) -> [success: bb6, unwind continue]; // scope 4 at src/main.rs:59:13: 59:32
    }

    bb6: {
        _11 = move (_15.0: u32);         // scope 4 at src/main.rs:59:13: 59:32
        StorageDead(_14);                // scope 4 at src/main.rs:59:31: 59:32
        StorageDead(_12);                // scope 4 at src/main.rs:59:31: 59:32
        StorageLive(_16);                // scope 5 at src/main.rs:60:5: 60:29
        StorageLive(_17);                // scope 5 at src/main.rs:60:26: 60:28
        _17 = &_2;                       // scope 5 at src/main.rs:60:26: 60:28
        _16 = std::hint::black_box::<&u32>(move _17) -> [return: bb7, unwind continue]; // scope 5 at src/main.rs:60:5: 60:29
    }

    bb7: {
        StorageDead(_17);                // scope 5 at src/main.rs:60:28: 60:29
        StorageDead(_16);                // scope 5 at src/main.rs:60:29: 60:30
        _0 = copy _11;                   // scope 5 at src/main.rs:61:5: 61:6
        StorageDead(_11);                // scope 4 at src/main.rs:62:1: 62:2
        StorageDead(_10);                // scope 3 at src/main.rs:62:1: 62:2
        StorageDead(_5);                 // scope 2 at src/main.rs:62:1: 62:2
        StorageDead(_4);                 // scope 1 at src/main.rs:62:1: 62:2
        StorageDead(_2);                 // scope 0 at src/main.rs:62:1: 62:2
        return;                          // scope 0 at src/main.rs:62:2: 62:2
    }
}
"""

LENT = r"""// MIR for `lent` after PreCodegen

fn lent(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:64:9: 64:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:64:22: 64:25
    let _2: u32;                         // in scope 0 at src/main.rs:65:9: 65:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:65:36: 65:39
    let mut _6: std::slice::Iter<'_, u32>; // in scope 0 at src/main.rs:67:14: 67:22
    let mut _7: &[u32];                  // in scope 0 at src/main.rs:67:14: 67:15
    let mut _8: &[u32; 2];               // in scope 0 at src/main.rs:67:14: 67:15
    let mut _9: {closure@src/main.rs:67:27: 67:30}; // in scope 0 at src/main.rs:67:27: 67:36
    let mut _10: &u32;                   // in scope 0 at src/main.rs:67:27: 67:36
    let mut _13: u32;                    // in scope 0 at src/main.rs:69:13: 69:28
    let mut _14: std::iter::Map<std::slice::Iter<'_, u32>, {closure@src/main.rs:67:27: 67:30}>; // in scope 0 at src/main.rs:69:13: 69:15
    let mut _15: u32;                    // in scope 0 at src/main.rs:69:31: 69:32
    let mut _16: (u32, bool);            // in scope 0 at src/main.rs:69:13: 69:32
    let _17: &u32;                       // in scope 0 at src/main.rs:70:5: 70:29
    let mut _18: &u32;                   // in scope 0 at src/main.rs:70:26: 70:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:65:9: 65:10
        let _4: [u32; 2];                // in scope 1 at src/main.rs:66:9: 66:10
        scope 2 {
            debug a => _4;               // in scope 2 at src/main.rs:66:9: 66:10
            let _5: std::iter::Map<std::slice::Iter<'_, u32>, {closure@src/main.rs:67:27: 67:30}>; // in scope 2 at src/main.rs:67:9: 67:11
            scope 3 {
                debug it => _5;          // in scope 3 at src/main.rs:67:9: 67:11
                let _11: u32;            // in scope 3 at src/main.rs:68:9: 68:10
                scope 4 {
                    debug k => _11;      // in scope 4 at src/main.rs:68:9: 68:10
                    let _12: u32;        // in scope 4 at src/main.rs:69:9: 69:10
                    scope 5 {
                        debug t => _12;  // in scope 5 at src/main.rs:69:9: 69:10
                    }
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:65:9: 65:10
        StorageLive(_3);                 // scope 0 at src/main.rs:65:36: 65:39
        _3 = copy _1;                    // scope 0 at src/main.rs:65:36: 65:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:65:13: 65:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:65:39: 65:40
        StorageLive(_4);                 // scope 1 at src/main.rs:66:9: 66:10
        _4 = [const 1_u32, const 2_u32]; // scope 1 at src/main.rs:66:13: 66:22
        StorageLive(_5);                 // scope 2 at src/main.rs:67:9: 67:11
        StorageLive(_6);                 // scope 2 at src/main.rs:67:14: 67:22
        StorageLive(_7);                 // scope 2 at src/main.rs:67:14: 67:15
        StorageLive(_8);                 // scope 2 at src/main.rs:67:14: 67:15
        _8 = &_4;                        // scope 2 at src/main.rs:67:14: 67:15
        _7 = move _8 as &[u32] (PointerCoercion(Unsize, Implicit)); // scope 2 at src/main.rs:67:14: 67:15
        StorageDead(_8);                 // scope 2 at src/main.rs:67:14: 67:15
        _6 = core::slice::<impl [u32]>::iter(move _7) -> [return: bb2, unwind continue]; // scope 2 at src/main.rs:67:14: 67:22
    }

    bb2: {
        StorageDead(_7);                 // scope 2 at src/main.rs:67:21: 67:22
        StorageLive(_9);                 // scope 2 at src/main.rs:67:27: 67:36
        StorageLive(_10);                // scope 2 at src/main.rs:67:27: 67:36
        _10 = &_2;                       // scope 2 at src/main.rs:67:27: 67:36
        _9 = {closure@src/main.rs:67:27: 67:30} { x: move _10 }; // scope 2 at src/main.rs:67:27: 67:36
                                         // closure
                                         //     i16,
                                         //     Binder { value: extern "RustCall" fn((&'{erased} u32,)) -> u32, bound_vars: [] },
                                         //     (&'{erased} u32,),
                                         // ]
        StorageDead(_10);                // scope 2 at src/main.rs:67:29: 67:30
        _5 = <std::slice::Iter<'_, u32> as Iterator>::map::<u32, {closure@src/main.rs:67:27: 67:30}>(move _6, move _9) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:67:14: 67:37
    }

    bb3: {
        StorageDead(_9);                 // scope 2 at src/main.rs:67:36: 67:37
        StorageDead(_6);                 // scope 2 at src/main.rs:67:36: 67:37
        StorageLive(_11);                // scope 3 at src/main.rs:68:9: 68:10
        _11 = other() -> [return: bb4, unwind continue]; // scope 3 at src/main.rs:68:13: 68:20
    }

    bb4: {
        StorageLive(_12);                // scope 4 at src/main.rs:69:9: 69:10
        StorageLive(_13);                // scope 4 at src/main.rs:69:13: 69:28
        StorageLive(_14);                // scope 4 at src/main.rs:69:13: 69:15
        _14 = move _5;                   // scope 4 at src/main.rs:69:13: 69:15
        _13 = <Map<std::slice::Iter<'_, u32>, {closure@src/main.rs:67:27: 67:30}> as Iterator>::sum::<u32>(move _14) -> [return: bb5, unwind continue]; // scope 4 at src/main.rs:69:13: 69:28
    }

    bb5: {
        StorageDead(_14);                // scope 4 at src/main.rs:69:27: 69:28
        StorageLive(_15);                // scope 4 at src/main.rs:69:31: 69:32
        _15 = copy _11;                  // scope 4 at src/main.rs:69:31: 69:32
        _16 = AddWithOverflow(copy _13, copy _15); // scope 4 at src/main.rs:69:13: 69:32
        assert(!move (_16.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _15) -> [success: bb6, unwind continue]; // scope 4 at src/main.rs:69:13: 69:32
    }

    bb6: {
        _12 = move (_16.0: u32);         // scope 4 at src/main.rs:69:13: 69:32
        StorageDead(_15);                // scope 4 at src/main.rs:69:31: 69:32
        StorageDead(_13);                // scope 4 at src/main.rs:69:31: 69:32
        StorageLive(_17);                // scope 5 at src/main.rs:70:5: 70:29
        StorageLive(_18);                // scope 5 at src/main.rs:70:26: 70:28
        _18 = &_2;                       // scope 5 at src/main.rs:70:26: 70:28
        _17 = std::hint::black_box::<&u32>(move _18) -> [return: bb7, unwind continue]; // scope 5 at src/main.rs:70:5: 70:29
    }

    bb7: {
        StorageDead(_18);                // scope 5 at src/main.rs:70:28: 70:29
        StorageDead(_17);                // scope 5 at src/main.rs:70:29: 70:30
        _0 = copy _12;                   // scope 5 at src/main.rs:71:5: 71:6
        StorageDead(_12);                // scope 4 at src/main.rs:72:1: 72:2
        StorageDead(_11);                // scope 3 at src/main.rs:72:1: 72:2
        StorageDead(_5);                 // scope 2 at src/main.rs:72:1: 72:2
        StorageDead(_4);                 // scope 1 at src/main.rs:72:1: 72:2
        StorageDead(_2);                 // scope 0 at src/main.rs:72:1: 72:2
        return;                          // scope 0 at src/main.rs:72:2: 72:2
    }
}
"""

HASH = "h0123456789abcdef"


def body_of(text, proven=True):
    """The body of ``text``, as matched to its IR: a function with no type parameters,
    whose standard paths are the standard library's where ``proven``."""
    body = parse_body(text)
    body.generics = ()
    body.std_traits = proven
    body.trusted_names = frozenset(_STD_TYPES) if proven else frozenset()
    body.std_paths = _STD_CRATES if proven else frozenset()
    body.std_iterator = proven
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


def owned_for(text, name, source=SOURCE):
    """``OwnedLocals`` over the body of ``text``, matched to an IR function ``name``."""
    body = parse_body(text)
    blocks = {}
    for number, block in body.blocks.items():
        lines = {at for _, file, at in block.statements if file == "src/main.rs" and at}
        blocks["start" if number == 0 else f"bb{number}"] = lines
    declared = min(at for _, file, at in body.blocks[0].statements if file == "src/main.rs")
    function = IrFunction(name, "crate", "src/main.rs", blocks, {}, declared - 1)
    texts = {"src/main.rs": source}
    return body, OwnedLocals([body], {HASH: function}, ("src/main.rs",), texts=texts)


def why(text, name, line, statement, source=SOURCE, variable="x"):
    """Why a change to ``variable`` is refused at a stop on ``line`` of the function
    ``name`` of ``text``, at its statement starting ``statement`` (``mid_statement``'s
    reason), with Peeky's own proofs over the MIR: (the reason, its row's short form)."""
    body, owned = owned_for(text, name, source)
    position = place(body, statement)
    owned._positions = lambda site, line, facts, block=None: [position]
    judge = owned.copy_hazards
    owned.copy_hazards = lambda *args, **asked: judge(*args, **{**asked, "breakpoint": False})
    session = object.__new__(RustSession)
    session.owned = owned
    session.prepared = SimpleNamespace(source_root="/w")
    session._optimized = lambda: False
    session._stop_block = lambda hashed, frame: None
    session._projection = lambda *args: ()
    session._frame_scope = lambda frame, variable: None
    session._settable_names = lambda source, line: []
    frame = {"name": f"crate::{name}::{HASH}", "line": line, "column": 9}
    frame["source"] = {"path": "/w/src/main.rs"}
    found = session.mid_statement({"name": variable}, frame, "breakpoint")
    return (None, None) if found is None else (str(found), found.row)


def kept(name, lines, pointing=False):
    """The refusal of a binding of the program's own that may hold a copy of `x`; or with
    ``pointing``, only point into it, which Peeky can't tell apart (#320 review)."""
    unless = f", unless `{name}` only points into it" if pointing else ""
    return (
        f"the program's own `{name}` (at {lines}) may hold a copy of it that it goes on with"
        f"{unless}, so a change to `x` may never be seen: Peeky proves no read of the changed "
        "part first (a reference lent to a call, or `{:?}`, is not one)",
        f"the program may hold a copy ({lines})",
    )


def test_what_clone_or_ptr_read_returns_from_a_pointer_into_the_local_may_be_a_copy():
    # `let y = x.clone();` (`<u32 as Clone>::clone(move _6)`, `_6 = &_2`) and `let y =
    # unsafe { std::ptr::read(&x) };`: then `let k = other();`, `y + k`, and `x` is only
    # lent to `black_box`. On #316's head neither `y` was a copy, and `x` was settable.
    for text, name, line, stop, y in (
        (CLONED, "cloned", 12, "_7 = other()", 5),
        (READ, "read", 31, "_7 = other()", 4),
    ):
        body = body_of(text)
        at = place(body, stop)
        assert body.direct_copies_at(2, at) == {y}, name
        assert body.copy_undoes(y, 2, at), name
        assert why(text, name, line, stop) == kept("y", f"line {line - 1}"), name
    # Before the copy, nothing holds one: a change there reaches the program.
    body = body_of(CLONED)
    assert body.direct_copies_at(2, place(body, "_4 = other()")) == set()
    assert why(CLONED, "cloned", 10, "_4 = other()") == (None, None)


def test_a_text_a_call_makes_and_another_stores_is_a_copy():
    # `s.push_str(&x.to_string());`: `to_string` returns a copy (`_11`), `deref` a pointer
    # to it, and `push_str` stores what that points at in `s`.
    body = body_of(TEXTED)
    # `deref`'s `&str` points at the copy; it is no copy itself.
    assert body.direct_copies_at(2, place(body, "_7 = &(*_8);")) == {11}
    assert 4 in body.direct_copies_at(2, place(body, "_13 = other()"))
    assert why(TEXTED, "texted", 22, "_13 = other()") == kept("s", "lines 20 and 21")
    # `format!("<{}>", x)`: what `format` returns from the `Arguments` that point at `x`.
    body = body_of(FORMATTED)
    at = place(body, "_16 = other()")
    assert {4, 5} <= body.direct_copies_at(2, at)
    assert not {10, 6} & body.direct_copies_at(2, at)
    assert why(FORMATTED, "formatted", 50, "_16 = other()") == kept("s", "alloc/src/macros.rs:113")


def test_mem_take_gives_its_result_the_copy_and_leaves_the_default_behind():
    # `let mut a = x; let b = std::mem::take(&mut a);`, then `a + b + k`. The refusal
    # named `a` (#316's head: "the program's own `a` (at line 39)"); `b` holds the copy.
    body = body_of(TAKEN)
    at = place(body, "_8 = other()")
    assert body.direct_copies_at(2, at) == {5}
    assert body._sure_pointee(6) == 4
    assert why(TAKEN, "taken", 41, "_8 = other()") == kept("b", "line 40")
    # Where the standard library's paths aren't proven, `take` may be the crate's own:
    # `a` may still hold the copy too.
    unproven = body_of(TAKEN, proven=False)
    assert unproven.direct_copies_at(2, at) == {4, 5}
    # Only a pointer surely to `a`: one assigned again may point elsewhere.
    line = next(each for each in TAKEN.splitlines() if "_6 = &mut (*_7);" in each)
    again = body_of(TAKEN.replace(line, f"{line}\n{line}"))
    assert again._sure_pointee(6) is None


def test_a_call_given_a_pointer_another_call_made_may_store_a_copy_into_a_value():
    # `v.extend_from_slice(std::slice::from_ref(&x))`: `from_ref` returns a `&[u32]` into
    # `x`, and `extend_from_slice` copies what it points at into `v`, a `Vec<u32>`.
    body = body_of(EXTENDED)
    at = place(body, "_11 = other()")
    assert body.direct_copies_at(2, at) == {4}
    assert why(EXTENDED, "extended", 60, "_11 = other()") == kept("v", "lines 58 and 59")
    # Into a `Vec<&u32>`, it may have stored the pointer itself (#164: `v.push(&s[2..])`):
    # that is no copy.
    pointers = body_of(EXTENDED.replace("_4: std::vec::Vec<u32>;", "_4: std::vec::Vec<&u32>;"))
    assert pointers.direct_copies_at(2, at) == set()


def test_the_standard_librarys_measures_and_comparisons_return_no_copy():
    # `a.len() + a.contains(&5) as usize`, `x.eq(&5)`, `x.cmp(&5)`, `x.partial_cmp(&5)`:
    # each the standard library's, returning a `usize`, `bool` or `Ordering`, which a `u32`
    # can't hold. `a = [x, 2]` holds a copy, but isn't used after the stop.
    body = body_of(MEASURED)
    at = place(body, "_33 = other()")
    assert body.direct_copies_at(2, at) == {4, 5}
    assert why(MEASURED, "measured", 73, "_33 = other()") == (None, None)
    # Not by name alone: where the dump's standard paths aren't proven, each may be a copy.
    unproven = body_of(MEASURED, proven=False)
    assert {6, 18, 23, 28} <= unproven.direct_copies_at(2, at)
    # A crate's own `PartialEq`, `PartialOrd` or `Ord` leaves them unproven.
    for name in ("PartialEq", "PartialOrd", "Ord"):
        source = SOURCE.replace("use std::env;", f"use std::env;\ntrait {name} {{}}")
        _, owned = owned_for(MEASURED, "measured", source)
        assert not owned._std_traits(), name
    _, owned = owned_for(MEASURED, "measured")
    assert owned._std_traits()


def test_a_measure_may_return_a_copy_where_the_locals_type_holds_what_it_returns():
    # `usize` can be a copy of a `usize` local, `bool` of a `bool`: judged by the local's
    # type, and any type of the crate's own may hold either.
    assert not _may_hold_type("u32", (), "usize")
    assert not _may_hold_type("std::string::String", (), "usize")
    assert not _may_hold_type("[u32; 2]", (), "bool")
    for kind, word in (
        ("usize", "usize"),
        ("std::vec::Vec<usize>", "usize"),
        ("(u32, bool)", "bool"),
        ("std::option::Option<std::cmp::Ordering>", "Ordering"),
        ("Window", "usize"),
        ("T", "bool"),
        (None, "bool"),
    ):
        assert _may_hold_type(kind, ("T",), word), kind
    body = body_of(MEASURED)
    length = next(
        text
        for block in body.blocks.values()
        for text, _, _ in block.statements
        if text.startswith("_7 = core::slice::<impl [u32]>::len(")
    )
    assert body._only_measures(length, 2)
    wide = body_of(MEASURED.replace("let _2: u32; ", "let _2: usize;"))
    assert wide.types[2] == "usize" and not wide._only_measures(length, 2)


def test_only_a_standard_method_of_a_standard_type_is_a_measure():
    body = body_of(MEASURED)
    calls = {
        "_7 = core::slice::<impl [u32]>::len(move _8) -> [return: bb4, unwind continue];": True,
        "_18 = <u32 as PartialEq>::eq(move _19, move _20) -> [return: bb6, unwind continue];": True,
        # `max` returns one of its values; `Clone::clone` is a copy.
        "_7 = core::slice::<impl [u32]>::max(move _8) -> [return: bb4, unwind continue];": False,
        "_18 = <u32 as Clone>::clone(move _19) -> [return: bb6, unwind continue];": False,
        # A function of the crate's own named like one.
        "_7 = len(move _8) -> [return: bb4, unwind continue];": False,
    }
    for text, measures in calls.items():
        assert body._only_measures(text, 2) is measures, text
    # A method of a type the crate names like a standard one: its first argument isn't a
    # pointer to that standard type.
    call = "_7 = Vec::<u32>::len(move _9) -> [return: bb4];"
    for kind, measures in (("&Vec<u32>", False), ("&std::vec::Vec<u32>", True)):
        body = body_of(MEASURED)
        body.types[9] = kind
        assert body._only_measures(call, 2) is measures, kind


def test_a_result_holding_a_value_beside_a_reference_may_be_a_copy():
    # #320 review: `hybrid(&x) -> (u32, &'static ())` and `hybrid_struct(&x) -> Hybrid<'_>
    # { v: u32, r: &u32 }` return a copy of `x` beside a reference. A type showing a
    # reference was taken for one holding only pointers, so `x` stayed settable there.
    for text, name, line in ((PAIRED, "paired", 97), (HELD, "held", 106)):
        body = body_of(text)
        at = place(body, "_7 = other()")
        assert body.direct_copies_at(2, at) == {4}, name
        # It may also point into `x`, as `h.r` does.
        assert 4 in body._copy_state(2, at, True)[5], name
        assert why(text, name, line, "_7 = other()") == kept("h", f"line {line - 1}", True), name


def test_only_a_type_holding_nothing_but_pointers_holds_no_copy():
    for kind in (
        "&str",
        "&mut std::vec::Vec<u32>",
        "*const u32",
        "std::ptr::NonNull<u32>",
        "()",
        "(&str, &str)",
        "[&u32; 2]",
        "std::option::Option<&u32>",
        "std::option::Option<(&str, &str)>",
        "std::slice::Iter<'_, u32>",
        "std::str::Chars<'_>",
        "core::fmt::rt::Argument<'_>",
        "std::fmt::Arguments<'_>",
        "std::cell::Ref<'_, std::vec::Vec<&str>>",
    ):
        assert _pointer_only(kind, (), _STD_CRATES), kind
    for kind in (
        "(u32, &())",
        "Hybrid<'_>",
        "std::option::Option<(u32, &str)>",
        "std::result::Result<&u32, u32>",
        "std::boxed::Box<dyn std::ops::Fn() -> usize>",
        "{closure@src/main.rs:25:51: 25:58}",
        # A pattern, a size or a key is a value it was given.
        "std::str::Split<'_, char>",
        "std::slice::Chunks<'_, u32>",
        "std::collections::hash_map::Entry<'_, u32, u32>",
        "T",
        None,
    ):
        assert not _pointer_only(kind, ("T",), _STD_CRATES), kind


def test_box_new_returns_only_what_it_is_given():
    # #164's `Box::new(move || check("boxed", t))` with `t = &s[2..]`: what it returns
    # holds the closure, which holds only the pointer; no copy of `s`.
    body = body_of(CLONED)
    body.types[9] = "std::boxed::Box<{closure@src/main.rs:25:51: 25:58}>"
    call = "_9 = Box::<{closure@src/main.rs:25:51: 25:58}>::new(move _10) -> [return: bb3];"
    assert body._only_wraps(call)
    # Not where `Box` may be the crate's own, nor what returns another type.
    assert not body_of(CLONED, proven=False)._only_wraps(call)
    body.trusted_names = frozenset(_STD_TYPES) - {"Box"}
    assert not body._only_wraps(call)
    other = body_of(CLONED)
    other.types[9] = "Boxed"
    assert not other._only_wraps(call)


def test_a_short_type_name_the_crate_gives_its_own_proves_no_measure():
    # #320 review: `struct String;` with `fn len(s: &std::string::String) -> usize`, and a
    # `struct Vec;` likewise: the dump's `String::len(move _5)`, given a `&std::string::
    # String`, is the crate's, which returns what it parsed out of `x`'s text.
    for text, name, line in ((OWNED_TEXTS, "texts", 31), (OWNED_ITEMS, "items", 40)):
        body = body_of(text)
        stop = next(
            each.split(" -> ")[0]
            for block in body.blocks.values()
            for each, _, at in block.statements
            if at == line and "other()" in each
        )
        assert why(text, name, line, stop, OWNED) == kept("n", f"line {line - 1}"), name
        # Taken for the standard library's, it would measure, and `n` hold no copy.
        assert 4 not in body.direct_copies_at(2, place(body, stop)), name
    _, owned = owned_for(OWNED_TEXTS, "texts", OWNED)
    assert {"String", "Vec"}.isdisjoint(owned._trusted_names())
    assert {"Box", "HashMap"} <= owned._trusted_names()
    # Declared, imported or made by a macro.
    for code in (
        "use other::Vec;",
        "use std::vec::Vec as String;",
        "make!(Vec);",
        "macro_rules! make { () => { struct String; } }",
        "struct HashMap;",
    ):
        source = SOURCE.replace("use std::env;", f"use std::env;\n{code}")
        _, owned = owned_for(MEASURED, "measured", source)
        assert len(owned._trusted_names()) == len(_STD_TYPES) - 1, code
    _, owned = owned_for(MEASURED, "measured")
    assert owned._trusted_names() == frozenset(_STD_TYPES)


def test_a_crate_module_named_like_a_standard_crate_proves_no_view():
    # #320 review round 2: `mod alloc { pub mod str { pub struct Chars<'a> { v: u32, r: &'a
    # () } } }`, returned by `shadow(&x)`: the dump's `alloc::str::Chars<'_>` was taken for
    # the standard library's view, holding only pointers, so `x` stayed settable after it.
    assert why(SHADOWED, "shadowed", 35, "_7 = other()", ADAPTERS) == kept("c", "line 34", True)
    _, owned = owned_for(SHADOWED, "shadowed", ADAPTERS)
    assert owned._std_crates() == {"std", "core"}
    assert not _pointer_only("alloc::str::Chars<'_>", (), owned._std_crates())
    assert _pointer_only("std::str::Chars<'_>", (), owned._std_crates())
    # Renamed, passed bare to a macro, or imported as one.
    for code, crate in (
        ("extern crate other as alloc;", "alloc"),
        ("make!(core);", "core"),
        ("use other::thing as std;", "std"),
    ):
        source = SOURCE.replace("use std::env;", f"use std::env;\n{code}")
        _, owned = owned_for(MEASURED, "measured", source)
        assert owned._std_crates() == _STD_CRATES - {crate}, code
    _, owned = owned_for(MEASURED, "measured")
    assert owned._std_crates() == _STD_CRATES


def test_a_standard_adapter_holds_only_what_it_was_given():
    # #320 review round 2: `let it = s.chars().rev();` was "the program's own `it` … may
    # hold a copy of `s`", though `Rev<Chars<'_>>` holds only the borrowing `Chars`. The
    # crate's `mod alloc` leaves its `std::` paths the standard library's.
    body, owned = owned_for(REVERSED, "reversed", ADAPTERS)
    owned.body(HASH)
    stop = place(body, "_11 = mark()")
    assert 6 not in body.direct_copies_at(1, stop)
    assert 6 in body.pointing_copies_at(1, stop)
    assert why(REVERSED, "reversed", 45, "_11 = mark()", ADAPTERS, "s") == (None, None)
    # `rev_of(&s)`, the crate's own, returns a `Rev<Chars<'_>>` too: no copy either. `n`, the
    # count of its chars, may be one.
    stop = place(body, "_19 = mark()")
    assert body.direct_copies_at(1, stop) & body.named_locals() == {12}
    # `map` with a `move` closure holding `x`'s copy holds it; with one borrowing `x`, no copy.
    assert why(MAPPED, "mapped", 58, "_10 = other()", ADAPTERS) == kept("it", "line 57")
    assert why(LENT, "lent", 68, "_11 = other()", ADAPTERS) == (None, None)
    for kind in (
        "std::iter::Rev<std::str::Chars<'_>>",
        "std::iter::Zip<std::slice::Iter<'_, u32>, std::str::Chars<'_>>",
        "std::iter::Enumerate<std::iter::Rev<std::slice::Iter<'_, u32>>>",
    ):
        assert _pointer_only(kind, (), _STD_CRATES), kind
    for kind in (
        "std::iter::Map<std::slice::Iter<'_, u32>, {closure@src/main.rs:57:27: 57:35}>",
        "std::iter::Skip<std::str::Chars<'_>>",
        "std::iter::Peekable<std::str::Chars<'_>>",
        "std::iter::Rev<Hybrid<'_>>",
        "alloc::iter::Rev<std::str::Chars<'_>>",
    ):
        assert not _pointer_only(kind, (), _STD_CRATES - {"alloc"}), kind


def test_a_refusal_that_may_only_point_advises_no_copy_to_change():
    from peeky.errors import _mutation_hint as mutation_hint

    message = (
        "`x` cannot be changed at src/main.rs:35, at any stop of this build: in a::shadowed, "
        "at every place line 35 can stop at, the program's own `c` (at line 34) may hold a "
        "copy of `x` that it goes on with, unless `c` only points into it, so a change there "
        "may never be seen"
    )
    assert "copies or borrows it" in mutation_hint(message)
    assert "change the copy" in mutation_hint(
        message.replace(", unless `c` only points into it", "")
    )


def test_each_standard_proof_needs_only_the_root_it_uses():
    # #320 review round 3: `mod alloc {}` made every standard proof unproven, so `n` of `let n
    # = a.len() + a.contains(&5) as usize;` (`core::slice::<impl [u32]>::len`) was said to
    # "may hold a copy of `x`". It needs `core` only; a `mod core {}` leaves it unproven.
    alloc = SOURCE.replace("use std::env;", "use std::env;\nmod alloc {}")
    assert why(MEASURED, "measured", 73, "_33 = other()", alloc) == (None, None)
    core = SOURCE.replace("use std::env;", "use std::env;\nmod core {}")
    assert why(MEASURED, "measured", 73, "_33 = other()", core) == kept("n", "line 69")
    # `std::mem::take` needs `std`: under a `mod std {}`, `a` may still hold the copy.
    for module, holders in (("mod core {}", {5}), ("mod std {}", {4, 5})):
        source = SOURCE.replace("use std::env;", f"use std::env;\n{module}")
        body, owned = owned_for(TAKEN, "taken", source)
        owned.body(HASH)
        assert body.direct_copies_at(2, place(body, "_8 = other()")) == holders, module
    # A short name's type, a wrapper's result and a named trait, each by its own root.
    body = body_of(MEASURED)
    call = "_7 = Vec::<u32>::len(move _9) -> [return: bb4];"
    body.types[9] = "&alloc::vec::Vec<u32>"
    body.std_paths = frozenset({"std", "core"})
    assert not body._only_measures(call, 2)
    body = body_of(MEASURED)
    body.types[9] = "&alloc::vec::Vec<u32>"
    body.std_paths = frozenset({"alloc"})
    assert body._only_measures(call, 2)
    named = "_18 = <u32 as std::cmp::PartialEq>::eq(move _19, move _20) -> [return: bb6];"
    for trusted, measures in (({"core"}, False), ({"std"}, True)):
        body = body_of(MEASURED)
        body.std_paths = frozenset(trusted)
        assert body._only_measures(named, 2) is measures, trusted
    boxed = "_9 = Box::<{closure@src/main.rs:25:51: 25:58}>::new(move _10) -> [return: bb3];"
    for trusted, wraps in (({"core", "alloc"}, False), ({"std"}, True)):
        body = body_of(CLONED)
        body.types[9] = "std::boxed::Box<{closure@src/main.rs:25:51: 25:58}>"
        body.std_paths = frozenset(trusted)
        assert body._only_wraps(boxed) is wraps, trusted


def test_a_slice_element_needs_the_std_root_its_iterator_is_named_under():
    # `_SLICE_NEXT` names `std::slice::Iter`: a crate's `mod std` may be what it names,
    # whatever other root stays the standard library's (#320 review round 3).
    from peeky.rust import _slice_element

    class Proofs:
        def __init__(self, roots):
            self.roots = roots

        def _std_compares(self):
            return self.roots

        def _std_iterator(self):
            return True

    class Assigned:
        def _assignments(self):
            return {
                9: ["<std::slice::Iter<'_, Pair> as Iterator>::next(move _10) -> [return: bb3]"]
            }

    item = ["copy ((_9 as Some).0: &Pair)"]
    assert _slice_element(Proofs(frozenset({"std", "core"})), Assigned(), item)
    assert _slice_element(Proofs(frozenset({"std"})), Assigned(), item)
    assert not _slice_element(Proofs(frozenset({"core"})), Assigned(), item)
    assert not _slice_element(Proofs(frozenset()), Assigned(), item)
