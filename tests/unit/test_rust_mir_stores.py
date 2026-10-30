"""A statement that stores a copy through a pointer holds it in the memory it stores
into. `vec![x, 1]` fills its Box by `((*_10).1: …) = [move _7, const 1_u32]`, then makes
the Vec from that Box, and `total(…)` sums it. The copy analysis skipped every store
through a pointer: once `_7` died, nothing held `x`'s copy, so `x` was `[settable]` on the
statement's later line, and a change there never reached the program.

On the same path, a refusal before the run listed the very local it refused as "settable
here"."""

import json
from types import SimpleNamespace

from peeky.rust import RustSession
from peeky.rust_mir import IrFunction, OwnedLocals, TypeLayouts, parse_body

ALLOC = "/rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/alloc/src/macros.rs"
# The crate the fixtures below are rustc 1.96.0's MIR of (`-Zmir-opt-level=0
# -Zdump-mir=PreCodegen -Zmir-include-spans=yes`, as Peeky builds it; the comment lines of
# its constants left out, and the standard library's macro file written {ALLOC}).
SOURCE = """use std::env;

#[inline(never)]
fn total(v: Vec<u32>) -> u32 {
    v.iter().sum()
}

#[inline(never)]
fn add(v: Vec<u32>, k: u32) -> u32 {
    v.iter().sum::<u32>() + k
}

#[inline(never)]
fn boxed(b: Box<[u32; 2]>) -> u32 {
    b[0] + b[1]
}

#[inline(never)]
fn other() -> u32 {
    1
}

fn literal(x: u32) -> u32 {
    let t = total(vec![x, 1])
        + other();
    t
}

fn pending(x: u32) -> u32 {
    let t = add(vec![x, 1],
        other());
    t
}

fn overwritten(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut b = Box::new([x, 1]);
    b[0] = 7;
    let t = boxed(b)
        + other();
    std::hint::black_box(&x);
    t
}

fn main() {
    let x: u32 = env::args().nth(1).unwrap().parse().unwrap();
    println!("{} {} {}", literal(x), pending(x), overwritten(x));
}
"""


# `literal` (lines 23 to 27): the statement in question.
LITERAL = """// MIR for `literal` after PreCodegen

fn literal(_1: u32) -> u32 {
    debug x => _1;                       // in scope 0 at src/main.rs:23:12: 23:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:23:23: 23:26
    let _2: u32;                         // in scope 0 at src/main.rs:24:9: 24:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:24:13: 24:30
    let mut _4: std::vec::Vec<u32>;      // in scope 0 at {ALLOC}:58:9: 60:10
    let mut _5: std::boxed::Box<std::mem::MaybeUninit<[u32; 2]>>; // in scope 0 at {ALLOC}:59:13: 59:96
    let mut _6: std::boxed::Box<std::mem::MaybeUninit<[u32; 2]>>; // in scope 0 at {ALLOC}:59:52: 59:84
    let mut _7: u32;                     // in scope 0 at src/main.rs:24:24: 24:25
    let mut _8: u32;                     // in scope 0 at src/main.rs:25:11: 25:18
    let mut _9: (u32, bool);             // in scope 0 at src/main.rs:24:13: 25:18
    let mut _10: *const std::mem::MaybeUninit<[u32; 2]>; // in scope 0 at {ALLOC}:59:52: 59:84
    let mut _11: *const ();              // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _12: usize;                  // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _13: usize;                  // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _14: usize;                  // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _15: bool;                   // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _16: *const ();              // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _17: usize;                  // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _18: bool;                   // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _19: bool;                   // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _20: bool;                   // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _21: bool;                   // in scope 0 at {ALLOC}:59:86: 59:95
    scope 1 {
        debug t => _2;                   // in scope 1 at src/main.rs:24:9: 24:10
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:24:9: 24:10
        StorageLive(_3);                 // scope 0 at src/main.rs:24:13: 24:30
        StorageLive(_4);                 // scope 0 at {ALLOC}:58:9: 60:10
        StorageLive(_5);                 // scope 0 at {ALLOC}:59:13: 59:96
        StorageLive(_6);                 // scope 0 at {ALLOC}:59:52: 59:84
        _6 = Box::<[u32; 2]>::new_uninit() -> [return: bb1, unwind continue]; // scope 0 at {ALLOC}:59:52: 59:84
    }

    bb1: {
        StorageLive(_7);                 // scope 0 at src/main.rs:24:24: 24:25
        _7 = copy _1;                    // scope 0 at src/main.rs:24:24: 24:25
        _10 = copy ((_6.0: std::ptr::Unique<std::mem::MaybeUninit<[u32; 2]>>).0: std::ptr::NonNull<std::mem::MaybeUninit<[u32; 2]>>) as *const std::mem::MaybeUninit<[u32; 2]> (Transmute); // scope 0 at {ALLOC}:59:86: 59:95
        _11 = copy _10 as *const () (PtrToPtr); // scope 0 at {ALLOC}:59:86: 59:95
        _12 = copy _11 as usize (Transmute); // scope 0 at {ALLOC}:59:86: 59:95
        _13 = Sub(const <std::mem::MaybeUninit<[u32; 2]> as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 0 at {ALLOC}:59:86: 59:95
        _14 = BitAnd(copy _12, copy _13); // scope 0 at {ALLOC}:59:86: 59:95
        _15 = Eq(copy _14, const 0_usize); // scope 0 at {ALLOC}:59:86: 59:95
        assert(copy _15, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <std::mem::MaybeUninit<[u32; 2]> as std::mem::SizedTypeProperties>::ALIGN, copy _12) -> [success: bb7, unwind unreachable]; // scope 0 at {ALLOC}:59:86: 59:95
    }

    bb2: {
        StorageDead(_5);                 // scope 0 at {ALLOC}:60:9: 60:10
        _3 = total(move _4) -> [return: bb3, unwind: bb6]; // scope 0 at src/main.rs:24:13: 24:30
    }

    bb3: {
        StorageDead(_4);                 // scope 0 at src/main.rs:24:29: 24:30
        StorageLive(_8);                 // scope 0 at src/main.rs:25:11: 25:18
        _8 = other() -> [return: bb4, unwind continue]; // scope 0 at src/main.rs:25:11: 25:18
    }

    bb4: {
        _9 = AddWithOverflow(copy _3, copy _8); // scope 0 at src/main.rs:24:13: 25:18
        assert(!move (_9.1: bool), "attempt to compute `{} + {}`, which would overflow", move _3, move _8) -> [success: bb5, unwind continue]; // scope 0 at src/main.rs:24:13: 25:18
    }

    bb5: {
        _2 = move (_9.0: u32);           // scope 0 at src/main.rs:24:13: 25:18
        StorageDead(_8);                 // scope 0 at src/main.rs:25:17: 25:18
        StorageDead(_3);                 // scope 0 at src/main.rs:25:17: 25:18
        _0 = copy _2;                    // scope 1 at src/main.rs:26:5: 26:6
        StorageDead(_2);                 // scope 0 at src/main.rs:27:1: 27:2
        return;                          // scope 0 at src/main.rs:27:2: 27:2
    }

    bb6 (cleanup): {
        resume;                          // scope 0 at src/main.rs:23:1: 27:2
    }

    bb7: {
        _16 = copy _10 as *const () (PtrToPtr); // scope 0 at {ALLOC}:59:86: 59:95
        _17 = copy _16 as usize (Transmute); // scope 0 at {ALLOC}:59:86: 59:95
        _18 = Ne(const <std::mem::MaybeUninit<[u32; 2]> as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 0 at {ALLOC}:59:86: 59:95
        _19 = Eq(copy _17, const 0_usize); // scope 0 at {ALLOC}:59:86: 59:95
        _20 = BitAnd(copy _19, copy _18); // scope 0 at {ALLOC}:59:86: 59:95
        _21 = Not(copy _20);             // scope 0 at {ALLOC}:59:86: 59:95
        assert(copy _21, "null pointer dereference occurred") -> [success: bb8, unwind unreachable]; // scope 0 at {ALLOC}:59:86: 59:95
    }

    bb8: {
        ((((*_10).1: std::mem::ManuallyDrop<[u32; 2]>).0: std::mem::MaybeDangling<[u32; 2]>).0: [u32; 2]) = [move _7, const 1_u32]; // scope 0 at {ALLOC}:59:86: 59:95
        StorageDead(_7);                 // scope 0 at {ALLOC}:59:94: 59:95
        _5 = move _6;                    // scope 0 at {ALLOC}:59:13: 59:96
        StorageDead(_6);                 // scope 0 at {ALLOC}:59:95: 59:96
        _4 = std::boxed::box_assume_init_into_vec_unsafe::<u32, 2>(move _5) -> [return: bb2, unwind: bb6]; // scope 0 at {ALLOC}:58:9: 60:10
    }
}
""".replace("{ALLOC}", ALLOC)


# `pending` (lines 29 to 33): the Vec itself is still to be used on the second line.
PENDING = """// MIR for `pending` after PreCodegen

fn pending(_1: u32) -> u32 {
    debug x => _1;                       // in scope 0 at src/main.rs:29:12: 29:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:29:23: 29:26
    let _2: u32;                         // in scope 0 at src/main.rs:30:9: 30:10
    let mut _3: std::vec::Vec<u32>;      // in scope 0 at {ALLOC}:58:9: 60:10
    let mut _4: std::boxed::Box<std::mem::MaybeUninit<[u32; 2]>>; // in scope 0 at {ALLOC}:59:13: 59:96
    let mut _5: std::boxed::Box<std::mem::MaybeUninit<[u32; 2]>>; // in scope 0 at {ALLOC}:59:52: 59:84
    let mut _6: u32;                     // in scope 0 at src/main.rs:30:22: 30:23
    let mut _7: u32;                     // in scope 0 at src/main.rs:31:9: 31:16
    let mut _8: bool;                    // in scope 0 at src/main.rs:31:16: 31:17
    let mut _9: *const std::mem::MaybeUninit<[u32; 2]>; // in scope 0 at {ALLOC}:59:52: 59:84
    let mut _10: *const ();              // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _11: usize;                  // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _12: usize;                  // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _13: usize;                  // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _14: bool;                   // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _15: *const ();              // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _16: usize;                  // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _17: bool;                   // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _18: bool;                   // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _19: bool;                   // in scope 0 at {ALLOC}:59:86: 59:95
    let mut _20: bool;                   // in scope 0 at {ALLOC}:59:86: 59:95
    scope 1 {
        debug t => _2;                   // in scope 1 at src/main.rs:30:9: 30:10
    }

    bb0: {
        _8 = const false;                // scope 0 at src/main.rs:30:9: 30:10
        StorageLive(_2);                 // scope 0 at src/main.rs:30:9: 30:10
        StorageLive(_3);                 // scope 0 at {ALLOC}:58:9: 60:10
        StorageLive(_4);                 // scope 0 at {ALLOC}:59:13: 59:96
        StorageLive(_5);                 // scope 0 at {ALLOC}:59:52: 59:84
        _5 = Box::<[u32; 2]>::new_uninit() -> [return: bb1, unwind continue]; // scope 0 at {ALLOC}:59:52: 59:84
    }

    bb1: {
        StorageLive(_6);                 // scope 0 at src/main.rs:30:22: 30:23
        _6 = copy _1;                    // scope 0 at src/main.rs:30:22: 30:23
        _9 = copy ((_5.0: std::ptr::Unique<std::mem::MaybeUninit<[u32; 2]>>).0: std::ptr::NonNull<std::mem::MaybeUninit<[u32; 2]>>) as *const std::mem::MaybeUninit<[u32; 2]> (Transmute); // scope 0 at {ALLOC}:59:86: 59:95
        _10 = copy _9 as *const () (PtrToPtr); // scope 0 at {ALLOC}:59:86: 59:95
        _11 = copy _10 as usize (Transmute); // scope 0 at {ALLOC}:59:86: 59:95
        _12 = Sub(const <std::mem::MaybeUninit<[u32; 2]> as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 0 at {ALLOC}:59:86: 59:95
        _13 = BitAnd(copy _11, copy _12); // scope 0 at {ALLOC}:59:86: 59:95
        _14 = Eq(copy _13, const 0_usize); // scope 0 at {ALLOC}:59:86: 59:95
        assert(copy _14, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <std::mem::MaybeUninit<[u32; 2]> as std::mem::SizedTypeProperties>::ALIGN, copy _11) -> [success: bb8, unwind unreachable]; // scope 0 at {ALLOC}:59:86: 59:95
    }

    bb2: {
        _8 = const true;                 // scope 0 at {ALLOC}:60:9: 60:10
        StorageDead(_4);                 // scope 0 at {ALLOC}:60:9: 60:10
        StorageLive(_7);                 // scope 0 at src/main.rs:31:9: 31:16
        _7 = other() -> [return: bb3, unwind: bb7]; // scope 0 at src/main.rs:31:9: 31:16
    }

    bb3: {
        _8 = const false;                // scope 0 at src/main.rs:30:13: 31:17
        _2 = add(move _3, move _7) -> [return: bb4, unwind: bb7]; // scope 0 at src/main.rs:30:13: 31:17
    }

    bb4: {
        StorageDead(_7);                 // scope 0 at src/main.rs:31:16: 31:17
        _8 = const false;                // scope 0 at src/main.rs:31:16: 31:17
        StorageDead(_3);                 // scope 0 at src/main.rs:31:16: 31:17
        _0 = copy _2;                    // scope 1 at src/main.rs:32:5: 32:6
        StorageDead(_2);                 // scope 0 at src/main.rs:33:1: 33:2
        return;                          // scope 0 at src/main.rs:33:2: 33:2
    }

    bb5 (cleanup): {
        resume;                          // scope 0 at src/main.rs:29:1: 33:2
    }

    bb6 (cleanup): {
        drop(_3) -> [return: bb5, unwind terminate(cleanup)]; // scope 0 at src/main.rs:31:16: 31:17
    }

    bb7 (cleanup): {
        switchInt(copy _8) -> [0: bb5, otherwise: bb6]; // scope 0 at src/main.rs:31:16: 31:17
    }

    bb8: {
        _15 = copy _9 as *const () (PtrToPtr); // scope 0 at {ALLOC}:59:86: 59:95
        _16 = copy _15 as usize (Transmute); // scope 0 at {ALLOC}:59:86: 59:95
        _17 = Ne(const <std::mem::MaybeUninit<[u32; 2]> as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 0 at {ALLOC}:59:86: 59:95
        _18 = Eq(copy _16, const 0_usize); // scope 0 at {ALLOC}:59:86: 59:95
        _19 = BitAnd(copy _18, copy _17); // scope 0 at {ALLOC}:59:86: 59:95
        _20 = Not(copy _19);             // scope 0 at {ALLOC}:59:86: 59:95
        assert(copy _20, "null pointer dereference occurred") -> [success: bb9, unwind unreachable]; // scope 0 at {ALLOC}:59:86: 59:95
    }

    bb9: {
        ((((*_9).1: std::mem::ManuallyDrop<[u32; 2]>).0: std::mem::MaybeDangling<[u32; 2]>).0: [u32; 2]) = [move _6, const 1_u32]; // scope 0 at {ALLOC}:59:86: 59:95
        StorageDead(_6);                 // scope 0 at {ALLOC}:59:94: 59:95
        _4 = move _5;                    // scope 0 at {ALLOC}:59:13: 59:96
        StorageDead(_5);                 // scope 0 at {ALLOC}:59:95: 59:96
        _3 = std::boxed::box_assume_init_into_vec_unsafe::<u32, 2>(move _4) -> [return: bb2, unwind: bb5]; // scope 0 at {ALLOC}:58:9: 60:10
    }
}
""".replace("{ALLOC}", ALLOC)


# `overwritten` (lines 35 to 44): `b[0] = 7;` writes through the Box's own pointer.
OVERWRITTEN = """// MIR for `overwritten` after PreCodegen

fn overwritten(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:35:16: 35:19
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:35:29: 35:32
    let _2: u32;                         // in scope 0 at src/main.rs:36:9: 36:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:36:36: 36:39
    let mut _5: [u32; 2];                // in scope 0 at src/main.rs:37:26: 37:32
    let mut _6: u32;                     // in scope 0 at src/main.rs:37:27: 37:28
    let _7: usize;                       // in scope 0 at src/main.rs:38:7: 38:8
    let mut _8: bool;                    // in scope 0 at src/main.rs:38:5: 38:9
    let mut _10: u32;                    // in scope 0 at src/main.rs:39:13: 39:21
    let mut _11: std::boxed::Box<[u32; 2]>; // in scope 0 at src/main.rs:39:19: 39:20
    let mut _12: u32;                    // in scope 0 at src/main.rs:40:11: 40:18
    let mut _13: (u32, bool);            // in scope 0 at src/main.rs:39:13: 40:18
    let _14: &u32;                       // in scope 0 at src/main.rs:41:5: 41:29
    let mut _15: &u32;                   // in scope 0 at src/main.rs:41:26: 41:28
    let mut _16: bool;                   // in scope 0 at src/main.rs:43:1: 43:2
    let mut _17: *const [u32; 2];        // in scope 0 at src/main.rs:37:9: 37:14
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:36:9: 36:10
        let mut _4: std::boxed::Box<[u32; 2]>; // in scope 1 at src/main.rs:37:9: 37:14
        scope 2 {
            debug b => _4;               // in scope 2 at src/main.rs:37:9: 37:14
            let _9: u32;                 // in scope 2 at src/main.rs:39:9: 39:10
            let mut _18: *const ();      // in scope 2 at src/main.rs:38:5: 38:13
            let mut _19: usize;          // in scope 2 at src/main.rs:38:5: 38:13
            let mut _20: usize;          // in scope 2 at src/main.rs:38:5: 38:13
            let mut _21: usize;          // in scope 2 at src/main.rs:38:5: 38:13
            let mut _22: bool;           // in scope 2 at src/main.rs:38:5: 38:13
            let mut _23: *const ();      // in scope 2 at src/main.rs:38:5: 38:13
            let mut _24: usize;          // in scope 2 at src/main.rs:38:5: 38:13
            let mut _25: bool;           // in scope 2 at src/main.rs:38:5: 38:13
            let mut _26: bool;           // in scope 2 at src/main.rs:38:5: 38:13
            let mut _27: bool;           // in scope 2 at src/main.rs:38:5: 38:13
            let mut _28: bool;           // in scope 2 at src/main.rs:38:5: 38:13
            scope 3 {
                debug t => _9;           // in scope 3 at src/main.rs:39:9: 39:10
            }
        }
    }

    bb0: {
        _16 = const false;               // scope 0 at src/main.rs:36:9: 36:10
        StorageLive(_2);                 // scope 0 at src/main.rs:36:9: 36:10
        StorageLive(_3);                 // scope 0 at src/main.rs:36:36: 36:39
        _3 = copy _1;                    // scope 0 at src/main.rs:36:36: 36:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:36:13: 36:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:36:39: 36:40
        StorageLive(_4);                 // scope 1 at src/main.rs:37:9: 37:14
        StorageLive(_5);                 // scope 1 at src/main.rs:37:26: 37:32
        StorageLive(_6);                 // scope 1 at src/main.rs:37:27: 37:28
        _6 = copy _2;                    // scope 1 at src/main.rs:37:27: 37:28
        _5 = [move _6, const 1_u32];     // scope 1 at src/main.rs:37:26: 37:32
        StorageDead(_6);                 // scope 1 at src/main.rs:37:31: 37:32
        _16 = const true;                // scope 1 at src/main.rs:37:17: 37:33
        _4 = Box::<[u32; 2]>::new(move _5) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:37:17: 37:33
    }

    bb2: {
        StorageDead(_5);                 // scope 1 at src/main.rs:37:32: 37:33
        StorageLive(_7);                 // scope 2 at src/main.rs:38:7: 38:8
        _7 = const 0_usize;              // scope 2 at src/main.rs:38:7: 38:8
        _8 = Lt(copy _7, const 2_usize); // scope 2 at src/main.rs:38:5: 38:9
        assert(move _8, "index out of bounds: the length is {} but the index is {}", const 2_usize, copy _7) -> [success: bb3, unwind: bb10]; // scope 2 at src/main.rs:38:5: 38:9
    }

    bb3: {
        _17 = copy ((_4.0: std::ptr::Unique<[u32; 2]>).0: std::ptr::NonNull<[u32; 2]>) as *const [u32; 2] (Transmute); // scope 2 at src/main.rs:38:5: 38:13
        _18 = copy _17 as *const () (PtrToPtr); // scope 2 at src/main.rs:38:5: 38:13
        _19 = copy _18 as usize (Transmute); // scope 2 at src/main.rs:38:5: 38:13
        _20 = Sub(const <[u32; 2] as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 2 at src/main.rs:38:5: 38:13
        _21 = BitAnd(copy _19, copy _20); // scope 2 at src/main.rs:38:5: 38:13
        _22 = Eq(copy _21, const 0_usize); // scope 2 at src/main.rs:38:5: 38:13
        assert(copy _22, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <[u32; 2] as std::mem::SizedTypeProperties>::ALIGN, copy _19) -> [success: bb11, unwind unreachable]; // scope 2 at src/main.rs:38:5: 38:13
    }

    bb4: {
        StorageDead(_11);                // scope 2 at src/main.rs:39:20: 39:21
        StorageLive(_12);                // scope 2 at src/main.rs:40:11: 40:18
        _12 = other() -> [return: bb5, unwind: bb10]; // scope 2 at src/main.rs:40:11: 40:18
    }

    bb5: {
        _13 = AddWithOverflow(copy _10, copy _12); // scope 2 at src/main.rs:39:13: 40:18
        assert(!move (_13.1: bool), "attempt to compute `{} + {}`, which would overflow", move _10, move _12) -> [success: bb6, unwind: bb10]; // scope 2 at src/main.rs:39:13: 40:18
    }

    bb6: {
        _9 = move (_13.0: u32);          // scope 2 at src/main.rs:39:13: 40:18
        StorageDead(_12);                // scope 2 at src/main.rs:40:17: 40:18
        StorageDead(_10);                // scope 2 at src/main.rs:40:17: 40:18
        StorageLive(_14);                // scope 3 at src/main.rs:41:5: 41:29
        StorageLive(_15);                // scope 3 at src/main.rs:41:26: 41:28
        _15 = &_2;                       // scope 3 at src/main.rs:41:26: 41:28
        _14 = std::hint::black_box::<&u32>(move _15) -> [return: bb7, unwind: bb10]; // scope 3 at src/main.rs:41:5: 41:29
    }

    bb7: {
        StorageDead(_15);                // scope 3 at src/main.rs:41:28: 41:29
        StorageDead(_14);                // scope 3 at src/main.rs:41:29: 41:30
        _0 = copy _9;                    // scope 3 at src/main.rs:42:5: 42:6
        StorageDead(_9);                 // scope 2 at src/main.rs:43:1: 43:2
        _16 = const false;               // scope 1 at src/main.rs:43:1: 43:2
        StorageDead(_4);                 // scope 1 at src/main.rs:43:1: 43:2
        StorageDead(_2);                 // scope 0 at src/main.rs:43:1: 43:2
        return;                          // scope 0 at src/main.rs:43:2: 43:2
    }

    bb8 (cleanup): {
        resume;                          // scope 0 at src/main.rs:35:1: 43:2
    }

    bb9 (cleanup): {
        drop(_4) -> [return: bb8, unwind terminate(cleanup)]; // scope 1 at src/main.rs:43:1: 43:2
    }

    bb10 (cleanup): {
        switchInt(copy _16) -> [0: bb8, otherwise: bb9]; // scope 1 at src/main.rs:43:1: 43:2
    }

    bb11: {
        _23 = copy _17 as *const () (PtrToPtr); // scope 2 at src/main.rs:38:5: 38:13
        _24 = copy _23 as usize (Transmute); // scope 2 at src/main.rs:38:5: 38:13
        _25 = Ne(const <[u32; 2] as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 2 at src/main.rs:38:5: 38:13
        _26 = Eq(copy _24, const 0_usize); // scope 2 at src/main.rs:38:5: 38:13
        _27 = BitAnd(copy _26, copy _25); // scope 2 at src/main.rs:38:5: 38:13
        _28 = Not(copy _27);             // scope 2 at src/main.rs:38:5: 38:13
        assert(copy _28, "null pointer dereference occurred") -> [success: bb12, unwind unreachable]; // scope 2 at src/main.rs:38:5: 38:13
    }

    bb12: {
        (*_17)[_7] = const 7_u32;        // scope 2 at src/main.rs:38:5: 38:13
        StorageDead(_7);                 // scope 2 at src/main.rs:38:13: 38:14
        StorageLive(_9);                 // scope 2 at src/main.rs:39:9: 39:10
        StorageLive(_10);                // scope 2 at src/main.rs:39:13: 39:21
        StorageLive(_11);                // scope 2 at src/main.rs:39:19: 39:20
        _16 = const false;               // scope 2 at src/main.rs:39:19: 39:20
        _11 = move _4;                   // scope 2 at src/main.rs:39:19: 39:20
        _10 = boxed(move _11) -> [return: bb4, unwind: bb10]; // scope 2 at src/main.rs:39:13: 39:21
    }
}
""".replace("{ALLOC}", ALLOC)


# Another crate, with unsafe code (lines 8 to 19 of its `src/main.rs`):
STORED_SOURCE = """use std::env;

#[inline(never)]
fn other() -> u32 {
    1
}

fn stored(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut slot = [0u32; 2];
    let p: *mut u32 = slot.as_mut_ptr();
    unsafe {
        *p = x;
    }
    let k = other();
    let t = unsafe { *p } + k;
    std::hint::black_box(&x);
    t
}

fn main() {
    let x: u32 = env::args().nth(1).unwrap().parse().unwrap();
    println!("STORED {}", stored(x));
}
"""
STORED = """// MIR for `stored` after PreCodegen

fn stored(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:8:11: 8:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:8:24: 8:27
    let _2: u32;                         // in scope 0 at src/main.rs:9:9: 9:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:9:36: 9:39
    let mut _6: &mut [u32];              // in scope 0 at src/main.rs:11:23: 11:27
    let mut _7: &mut [u32; 2];           // in scope 0 at src/main.rs:11:23: 11:27
    let _8: ();                          // in scope 0 at src/main.rs:12:5: 14:6
    let mut _9: u32;                     // in scope 0 at src/main.rs:13:14: 13:15
    let mut _12: u32;                    // in scope 0 at src/main.rs:16:13: 16:26
    let mut _13: u32;                    // in scope 0 at src/main.rs:16:29: 16:30
    let mut _14: (u32, bool);            // in scope 0 at src/main.rs:16:13: 16:30
    let _15: &u32;                       // in scope 0 at src/main.rs:17:5: 17:29
    let mut _16: &u32;                   // in scope 0 at src/main.rs:17:26: 17:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:9:9: 9:10
        let mut _4: [u32; 2];            // in scope 1 at src/main.rs:10:9: 10:17
        scope 2 {
            debug slot => _4;            // in scope 2 at src/main.rs:10:9: 10:17
            let _5: *mut u32;            // in scope 2 at src/main.rs:11:9: 11:10
            scope 3 {
                debug p => _5;           // in scope 3 at src/main.rs:11:9: 11:10
                let _10: u32;            // in scope 3 at src/main.rs:15:9: 15:10
                let mut _22: *const ();  // in scope 3 at src/main.rs:13:9: 13:15
                let mut _23: usize;      // in scope 3 at src/main.rs:13:9: 13:15
                let mut _24: usize;      // in scope 3 at src/main.rs:13:9: 13:15
                let mut _25: usize;      // in scope 3 at src/main.rs:13:9: 13:15
                let mut _26: bool;       // in scope 3 at src/main.rs:13:9: 13:15
                let mut _27: *const ();  // in scope 3 at src/main.rs:13:9: 13:15
                let mut _28: usize;      // in scope 3 at src/main.rs:13:9: 13:15
                let mut _29: bool;       // in scope 3 at src/main.rs:13:9: 13:15
                let mut _30: bool;       // in scope 3 at src/main.rs:13:9: 13:15
                let mut _31: bool;       // in scope 3 at src/main.rs:13:9: 13:15
                let mut _32: bool;       // in scope 3 at src/main.rs:13:9: 13:15
                scope 4 {
                    debug k => _10;      // in scope 4 at src/main.rs:15:9: 15:10
                    let _11: u32;        // in scope 4 at src/main.rs:16:9: 16:10
                    let mut _17: *const (); // in scope 4 at src/main.rs:16:22: 16:24
                    let mut _18: usize;  // in scope 4 at src/main.rs:16:22: 16:24
                    let mut _19: usize;  // in scope 4 at src/main.rs:16:22: 16:24
                    let mut _20: usize;  // in scope 4 at src/main.rs:16:22: 16:24
                    let mut _21: bool;   // in scope 4 at src/main.rs:16:22: 16:24
                    let mut _33: *const (); // in scope 4 at src/main.rs:16:22: 16:24
                    let mut _34: usize;  // in scope 4 at src/main.rs:16:22: 16:24
                    let mut _35: bool;   // in scope 4 at src/main.rs:16:22: 16:24
                    let mut _36: bool;   // in scope 4 at src/main.rs:16:22: 16:24
                    let mut _37: bool;   // in scope 4 at src/main.rs:16:22: 16:24
                    let mut _38: bool;   // in scope 4 at src/main.rs:16:22: 16:24
                    scope 5 {
                        debug t => _11;  // in scope 5 at src/main.rs:16:9: 16:10
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
        StorageLive(_4);                 // scope 1 at src/main.rs:10:9: 10:17
        _4 = [const 0_u32; 2];           // scope 1 at src/main.rs:10:20: 10:29
        StorageLive(_5);                 // scope 2 at src/main.rs:11:9: 11:10
        StorageLive(_6);                 // scope 2 at src/main.rs:11:23: 11:27
        StorageLive(_7);                 // scope 2 at src/main.rs:11:23: 11:27
        _7 = &mut _4;                    // scope 2 at src/main.rs:11:23: 11:27
        _6 = move _7 as &mut [u32] (PointerCoercion(Unsize, Implicit)); // scope 2 at src/main.rs:11:23: 11:27
        StorageDead(_7);                 // scope 2 at src/main.rs:11:26: 11:27
        _5 = core::slice::<impl [u32]>::as_mut_ptr(move _6) -> [return: bb2, unwind continue]; // scope 2 at src/main.rs:11:23: 11:40
    }

    bb2: {
        StorageDead(_6);                 // scope 2 at src/main.rs:11:39: 11:40
        StorageLive(_8);                 // scope 3 at src/main.rs:12:5: 14:6
        StorageLive(_9);                 // scope 3 at src/main.rs:13:14: 13:15
        _9 = copy _2;                    // scope 3 at src/main.rs:13:14: 13:15
        _22 = copy _5 as *const () (PtrToPtr); // scope 3 at src/main.rs:13:9: 13:15
        _23 = copy _22 as usize (Transmute); // scope 3 at src/main.rs:13:9: 13:15
        _24 = Sub(const <u32 as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 3 at src/main.rs:13:9: 13:15
        _25 = BitAnd(copy _23, copy _24); // scope 3 at src/main.rs:13:9: 13:15
        _26 = Eq(copy _25, const 0_usize); // scope 3 at src/main.rs:13:9: 13:15
        assert(copy _26, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <u32 as std::mem::SizedTypeProperties>::ALIGN, copy _23) -> [success: bb7, unwind unreachable]; // scope 3 at src/main.rs:13:9: 13:15
    }

    bb3: {
        StorageLive(_11);                // scope 4 at src/main.rs:16:9: 16:10
        StorageLive(_12);                // scope 4 at src/main.rs:16:13: 16:26
        _17 = copy _5 as *const () (PtrToPtr); // scope 4 at src/main.rs:16:22: 16:24
        _18 = copy _17 as usize (Transmute); // scope 4 at src/main.rs:16:22: 16:24
        _19 = Sub(const <u32 as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 4 at src/main.rs:16:22: 16:24
        _20 = BitAnd(copy _18, copy _19); // scope 4 at src/main.rs:16:22: 16:24
        _21 = Eq(copy _20, const 0_usize); // scope 4 at src/main.rs:16:22: 16:24
        assert(copy _21, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <u32 as std::mem::SizedTypeProperties>::ALIGN, copy _18) -> [success: bb6, unwind unreachable]; // scope 4 at src/main.rs:16:22: 16:24
    }

    bb4: {
        _11 = move (_14.0: u32);         // scope 4 at src/main.rs:16:13: 16:30
        StorageDead(_13);                // scope 4 at src/main.rs:16:29: 16:30
        StorageDead(_12);                // scope 4 at src/main.rs:16:29: 16:30
        StorageLive(_15);                // scope 5 at src/main.rs:17:5: 17:29
        StorageLive(_16);                // scope 5 at src/main.rs:17:26: 17:28
        _16 = &_2;                       // scope 5 at src/main.rs:17:26: 17:28
        _15 = std::hint::black_box::<&u32>(move _16) -> [return: bb5, unwind continue]; // scope 5 at src/main.rs:17:5: 17:29
    }

    bb5: {
        StorageDead(_16);                // scope 5 at src/main.rs:17:28: 17:29
        StorageDead(_15);                // scope 5 at src/main.rs:17:29: 17:30
        _0 = copy _11;                   // scope 5 at src/main.rs:18:5: 18:6
        StorageDead(_11);                // scope 4 at src/main.rs:19:1: 19:2
        StorageDead(_10);                // scope 3 at src/main.rs:19:1: 19:2
        StorageDead(_5);                 // scope 2 at src/main.rs:19:1: 19:2
        StorageDead(_4);                 // scope 1 at src/main.rs:19:1: 19:2
        StorageDead(_2);                 // scope 0 at src/main.rs:19:1: 19:2
        return;                          // scope 0 at src/main.rs:19:2: 19:2
    }

    bb6: {
        _33 = copy _5 as *const () (PtrToPtr); // scope 4 at src/main.rs:16:22: 16:24
        _34 = copy _33 as usize (Transmute); // scope 4 at src/main.rs:16:22: 16:24
        _35 = Ne(const <u32 as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 4 at src/main.rs:16:22: 16:24
        _36 = Eq(copy _34, const 0_usize); // scope 4 at src/main.rs:16:22: 16:24
        _37 = BitAnd(copy _36, copy _35); // scope 4 at src/main.rs:16:22: 16:24
        _38 = Not(copy _37);             // scope 4 at src/main.rs:16:22: 16:24
        assert(copy _38, "null pointer dereference occurred") -> [success: bb9, unwind unreachable]; // scope 4 at src/main.rs:16:22: 16:24
    }

    bb7: {
        _27 = copy _5 as *const () (PtrToPtr); // scope 3 at src/main.rs:13:9: 13:15
        _28 = copy _27 as usize (Transmute); // scope 3 at src/main.rs:13:9: 13:15
        _29 = Ne(const <u32 as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 3 at src/main.rs:13:9: 13:15
        _30 = Eq(copy _28, const 0_usize); // scope 3 at src/main.rs:13:9: 13:15
        _31 = BitAnd(copy _30, copy _29); // scope 3 at src/main.rs:13:9: 13:15
        _32 = Not(copy _31);             // scope 3 at src/main.rs:13:9: 13:15
        assert(copy _32, "null pointer dereference occurred") -> [success: bb8, unwind unreachable]; // scope 3 at src/main.rs:13:9: 13:15
    }

    bb8: {
        (*_5) = move _9;                 // scope 3 at src/main.rs:13:9: 13:15
        StorageDead(_9);                 // scope 3 at src/main.rs:13:14: 13:15
        _8 = const ();                   // scope 3 at src/main.rs:12:5: 14:6
        StorageDead(_8);                 // scope 3 at src/main.rs:14:5: 14:6
        StorageLive(_10);                // scope 3 at src/main.rs:15:9: 15:10
        _10 = other() -> [return: bb3, unwind continue]; // scope 3 at src/main.rs:15:13: 15:20
    }

    bb9: {
        _12 = copy (*_5);                // scope 4 at src/main.rs:16:22: 16:24
        StorageLive(_13);                // scope 4 at src/main.rs:16:29: 16:30
        _13 = copy _10;                  // scope 4 at src/main.rs:16:29: 16:30
        _14 = AddWithOverflow(copy _12, copy _13); // scope 4 at src/main.rs:16:13: 16:30
        assert(!move (_14.1: bool), "attempt to compute `{} + {}`, which would overflow", move _12, move _13) -> [success: bb4, unwind continue]; // scope 4 at src/main.rs:16:13: 16:30
    }
}
"""


# `vec!` as an older rustc builds it: `exchange_malloc`, then `ShallowInitBox`, written
# through the Box itself (`(*_6) = [...]`), unsized and made a Vec by `into_vec`. Adapted
# from LITERAL: rustc 1.96.0 fills a `Box::new_uninit()` through its pointer instead.
SHALLOW = (
    LITERAL.replace(
        "    let mut _6: std::boxed::Box<std::mem::MaybeUninit<[u32; 2]>>;",
        "    let mut _22: *mut u8;                // in scope 0 at {ALLOC}:59:52: 59:84\n"
        "    let mut _6: std::boxed::Box<[u32; 2]>;".replace("{ALLOC}", ALLOC),
    )
    .replace(
        "    let mut _5: std::boxed::Box<std::mem::MaybeUninit<[u32; 2]>>;",
        "    let mut _5: std::boxed::Box<[u32]>;",
    )
    .replace(
        "_6 = Box::<[u32; 2]>::new_uninit() -> [return: bb1, unwind continue];",
        "_22 = alloc::alloc::exchange_malloc(const 8_usize, const 4_usize) -> "
        "[return: bb1, unwind continue];",
    )
    .replace(
        LITERAL[LITERAL.index("    bb1: {") : LITERAL.index("    bb2: {")],
        f"""    bb1: {{
        _6 = ShallowInitBox(move _22, [u32; 2]); // scope 0 at {ALLOC}:59:52: 59:84
        StorageLive(_7);                 // scope 0 at src/main.rs:24:24: 24:25
        _7 = copy _1;                    // scope 0 at src/main.rs:24:24: 24:25
        (*_6) = [move _7, const 1_u32];  // scope 0 at {ALLOC}:59:86: 59:95
        StorageDead(_7);                 // scope 0 at {ALLOC}:59:94: 59:95
        _5 = move _6 as std::boxed::Box<[u32]> (PointerCoercion(Unsize, Implicit)); // scope 0 at {ALLOC}:59:13: 59:96
        StorageDead(_6);                 // scope 0 at {ALLOC}:59:95: 59:96
        _4 = slice::<impl [u32]>::into_vec::<std::alloc::Global>(move _5) -> [return: bb2, unwind: bb6]; // scope 0 at {ALLOC}:58:9: 60:10
    }}

""",
    )
)
SHALLOW = SHALLOW[: SHALLOW.index("    bb7: {")].rstrip() + "\n}\n"
HASH = "h0123456789abcdef"


def body_of(text, interior=None):
    """The body of ``text``, as matched to its IR: a function with no type parameters, in a
    crate that names no `std`, `core` or `alloc` of its own, whose own types hold what
    ``interior`` says (rust_symbols.interior_types; None: not read)."""
    body = parse_body(text)
    body.generics = ()
    body.std_paths = frozenset({"std", "core", "alloc"})
    body.layouts = TypeLayouts(interior, body.closure_captures())
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


def after(body, statement):
    """The place right after the statement starting ``statement`` (its call's return)."""
    number, index = place(body, statement)
    block = body.blocks[number]
    if index == len(block.statements) - 1:
        return block.returns, 0
    return number, index + 1


def used(body, local, position):
    """The temporaries the may-analysis finds may hold a copy of ``local`` at
    ``position`` and are used again from there: the statement's copies."""
    live = body.live_ahead(position)
    return {n for n in body.copies_at(local, position) if live >> n & 1}


def read(body, local, position, transfers=True):
    """The temporaries the must-analysis finds surely read ``local`` at ``position``, with
    the standard library's Box-to-Vec conversion proven (``transfers``)."""
    return set(body.read_copies_at(local, position, (), transfers)[0])


def why(text, source, name, line, statement, variable="x", interior=None):
    """Why a change to ``variable`` is refused at a stop on ``line`` of the function
    ``name`` of ``text``, at its statement starting ``statement`` (``mid_statement``'s
    reason), with Peeky's own proofs over the MIR, and the crate's own types holding what
    ``interior`` says (as the build's debug information does; None: not read): (the
    reason, its row's short form)."""
    body = parse_body(text)
    blocks = {}
    for number, block in body.blocks.items():
        lines = {at for _, file, at in block.statements if file == "src/main.rs" and at}
        blocks["start" if number == 0 else f"bb{number}"] = lines
    declared = min(at for _, file, at in body.blocks[0].statements if file == "src/main.rs")
    function = IrFunction(name, "crate", "src/main.rs", blocks, {}, declared - 1)
    texts = {"src/main.rs": source}
    owned = OwnedLocals([body], {HASH: function}, ("src/main.rs",), texts=texts)
    owned.interior = interior
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


MAY_READ = (
    "a statement under way at the stop reads it into a temporary (at line 24), so it may hold "
    "a copy of it, which it uses after the stop",
    "a statement under way may hold a copy (line 24)",
)


def test_what_vec_stores_through_its_box_pointer_is_a_copy_the_statement_uses():
    # At `+ other();`, `total(vec![x, 1])` has summed `x`'s copy into `_3`,
    # which the statement adds after the stop. `_7` held the copy until the store.
    body = body_of(LITERAL)
    stop = place(body, "_8 = other()")
    assert used(body, 1, stop) == {3}
    # `total` is the crate's own: what it returns may be anything, so the
    # refusal is hedged.
    assert read(body, 1, stop) == set()
    assert why(LITERAL, SOURCE, "literal", 25, "_8 = other()") == MAY_READ
    # Each step surely holds it: the Box the store fills, the Box moved, the Vec the
    # standard library's conversion makes of it; then `total` is given it.
    for statement, holder in (
        ("((((*_10).1:", 6),
        ("_5 = move _6;", 5),
        ("_4 = std::boxed::box_assume_init_into_vec_unsafe", 4),
    ):
        assert holder in body.copies_at(1, after(body, statement)), statement
        assert holder in read(body, 1, after(body, statement)), statement
    assert read(body, 1, after(body, "_3 = total(move _4)")) == set()
    # Unless the conversion is proven the standard library's, the Vec holds no sure copy.
    assert 4 not in read(body, 1, after(body, "_4 = std::boxed::"), transfers=False)
    # Before the statement starts, nothing holds it: a change there reaches the program.
    assert used(body, 1, (0, 0)) == read(body, 1, (0, 0)) == set()
    # Nor does a Box that stores no copy of it (`vec![2, 1]`).
    constant = body_of(LITERAL.replace("[move _7, const 1_u32]", "[const 2_u32, const 1_u32]"))
    assert used(constant, 1, stop) == read(constant, 1, stop) == set()


def test_the_box_to_vec_conversion_is_proven_under_the_root_the_dump_names():
    # `std::boxed::box_assume_init_into_vec_unsafe` needs `std` only;
    # a crate's `mod alloc {}` leaves it the standard library's, a `mod std {}` doesn't.
    body = body_of(LITERAL)
    converted = after(body, "_4 = std::boxed::box_assume_init_into_vec_unsafe")
    assert 4 in read(body, 1, converted, frozenset({"std"}))
    assert 4 not in read(body, 1, converted, frozenset({"core", "alloc"}))
    for module, trusted in (("mod alloc {}", {"std"}), ("mod std {}", {"alloc"})):
        source = SOURCE.replace("use std::env;", f"use std::env;\n{module}")
        owned = OwnedLocals([body], {}, ("src/main.rs",), texts={"src/main.rs": source})
        assert owned._std_transfers() == trusted, module


def test_the_vec_a_statement_still_uses_holds_what_was_stored_in_its_box():
    # `add(vec![x, 1],` / `other());`: at the second line the Vec `_3` is yet to be given.
    body = body_of(PENDING)
    stop = place(body, "_7 = other()")
    assert used(body, 1, stop) == read(body, 1, stop) == {3}
    reason, row = why(PENDING, SOURCE, "pending", 31, "_7 = other()")
    assert reason == (
        "a statement under way at the stop has already read it into a temporary (at "
        "alloc/src/macros.rs:58), which it uses after the stop"
    )
    assert row == "a statement under way read it (alloc/src/macros.rs:58)"
    # A call given a pointer into the Box the Vec owns (`_9`) may write through it.
    given = body_of(PENDING.replace("_7 = other() ->", "_7 = other(copy _9) ->"))
    assert read(given, 1, stop) == {3} and read(given, 1, after(given, "_7 = other(")) == set()
    assert read(body, 1, after(body, "_7 = other(")) == {3}


def test_a_box_an_older_rustc_fills_through_itself_holds_what_it_stores():
    # `ShallowInitBox(exchange_malloc(…))`, then `(*_6) = [move _7, const 1_u32]`. Its
    # `into_vec` is not proven the standard library's, nor is `total`: hedged.
    body = body_of(SHALLOW)
    stop = place(body, "_8 = other()")
    assert used(body, 1, stop) == {3} and read(body, 1, stop) == set()
    assert 6 in read(body, 1, after(body, "(*_6) = [move _7"))
    assert 5 in read(body, 1, after(body, "_5 = move _6 as"))
    assert why(SHALLOW, SOURCE, "literal", 25, "_8 = other()") == MAY_READ


def test_a_store_through_a_pointer_into_what_holds_a_read_ends_it():
    # The must-analysis: `vec![x, 1]`'s Box surely holds the copy until another store
    # through its pointer `_10` overwrites it (`[7, 1]`).
    store = (
        "        ((((*_10).1: std::mem::ManuallyDrop<[u32; 2]>).0: std::mem::MaybeDangling<[u32; "
        "2]>).0: [u32; 2]) = [move _7, const 1_u32];"
    )
    assert LITERAL.count(store) == 1
    line = next(each for each in LITERAL.splitlines() if each.startswith(store))
    again = line.replace("[move _7, const 1_u32]", "[const 7_u32, const 1_u32]")
    body = body_of(LITERAL.replace(line, f"{line}\n{again}"))
    first = place(body, store.strip())
    assert 6 in read(body, 1, (first[0], first[1] + 1))
    assert 6 not in read(body, 1, (first[0], first[1] + 2))
    assert read(body, 1, after(body, "_5 = move _6;")) == set()
    # The may-analysis keeps it: `[7, 1]` may be where only part was written.
    assert used(body, 1, place(body, "_8 = other()")) == {3}


def test_a_box_from_a_call_and_written_since_holds_no_sure_copy():
    # `let mut b = Box::new([x, 1]); b[0] = 7;`: "has already copied its value into `b`"
    # would be false past `b[0] = 7`. What a call returns is never a sure copy; the store
    # ends any.
    body = body_of(OVERWRITTEN)
    stop = place(body, "_12 = other()")
    taken, sources = body.read_copies_at(2, stop, (), True)[1:]
    # Neither `b`, the Box moved out of it (`_11`), nor `boxed(b)` (`_10`).
    assert not taken & {4, 10, 11} and not {made for made, _ in sources} & {4, 10, 11}
    # Still refused, since `b` may hold a copy: said as a hedge.
    assert why(OVERWRITTEN, SOURCE, "overwritten", 40, "_12 = other()") == (
        "a statement under way at the stop uses `b` (at line 37), which may hold the program's "
        "own copy of it, so it may hold a copy of it, which it uses after the stop",
        "a statement under way may hold a copy (line 37)",
    )


def test_a_copy_stored_through_a_pointer_into_a_binding_is_the_programs_own():
    # `*p = x;` through `p = slot.as_mut_ptr()`: `slot` holds `x`'s copy, which the next
    # line reads through `p`; `x` is never read again (`black_box(&x)` lends it).
    body = body_of(STORED)
    stop = place(body, "_10 = other()")
    direct = body.direct_copies_at(2, stop)
    assert 4 in direct and 5 not in direct
    assert {5, 6, 7} <= body._pointers_to(4)
    assert body.copy_undoes(4, 2, stop)
    assert why(STORED, STORED_SOURCE, "stored", 15, "_10 = other()") == (
        "the program's own `slot` (at lines 10 and 13) may hold a copy of it that it goes on "
        "with, so a change to `x` may never be seen: Peeky proves no read of the changed part "
        "first (a reference lent to a call, or `{:?}`, is not one)",
        "the program may hold a copy (lines 10 and 13)",
    )


# rustc 1.96.0's MIR of (lines 13 to 17 of a crate of its own):
#
#     fn passed(x: u32, y: u32) -> u32 {
#         let t = add(x,
#             other());
#         t + y
#     }
PASSED = """// MIR for `passed` after PreCodegen

fn passed(_1: u32, _2: u32) -> u32 {
    debug x => _1;                       // in scope 0 at src/main.rs:13:11: 13:12
    debug y => _2;                       // in scope 0 at src/main.rs:13:19: 13:20
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:13:30: 13:33
    let _3: u32;                         // in scope 0 at src/main.rs:14:9: 14:10
    let mut _4: u32;                     // in scope 0 at src/main.rs:14:17: 14:18
    let mut _5: u32;                     // in scope 0 at src/main.rs:15:9: 15:16
    let mut _6: u32;                     // in scope 0 at src/main.rs:16:5: 16:6
    let mut _7: u32;                     // in scope 0 at src/main.rs:16:9: 16:10
    let mut _8: (u32, bool);             // in scope 0 at src/main.rs:16:5: 16:10
    scope 1 {
        debug t => _3;                   // in scope 1 at src/main.rs:14:9: 14:10
    }

    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:14:9: 14:10
        StorageLive(_4);                 // scope 0 at src/main.rs:14:17: 14:18
        _4 = copy _1;                    // scope 0 at src/main.rs:14:17: 14:18
        StorageLive(_5);                 // scope 0 at src/main.rs:15:9: 15:16
        _5 = other() -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:15:9: 15:16
    }

    bb1: {
        _3 = add(move _4, move _5) -> [return: bb2, unwind continue]; // scope 0 at src/main.rs:14:13: 15:17
    }

    bb2: {
        StorageDead(_5);                 // scope 0 at src/main.rs:15:16: 15:17
        StorageDead(_4);                 // scope 0 at src/main.rs:15:16: 15:17
        StorageLive(_6);                 // scope 1 at src/main.rs:16:5: 16:6
        _6 = copy _3;                    // scope 1 at src/main.rs:16:5: 16:6
        StorageLive(_7);                 // scope 1 at src/main.rs:16:9: 16:10
        _7 = copy _2;                    // scope 1 at src/main.rs:16:9: 16:10
        _8 = AddWithOverflow(copy _6, copy _7); // scope 1 at src/main.rs:16:5: 16:10
        assert(!move (_8.1: bool), "attempt to compute `{} + {}`, which would overflow", move _6, move _7) -> [success: bb3, unwind continue]; // scope 1 at src/main.rs:16:5: 16:10
    }

    bb3: {
        _0 = move (_8.0: u32);           // scope 1 at src/main.rs:16:5: 16:10
        StorageDead(_7);                 // scope 1 at src/main.rs:16:9: 16:10
        StorageDead(_6);                 // scope 1 at src/main.rs:16:9: 16:10
        StorageDead(_3);                 // scope 0 at src/main.rs:17:1: 17:2
        return;                          // scope 0 at src/main.rs:17:2: 17:2
    }
}
"""


def test_a_refusal_before_the_run_lists_no_local_refused_at_its_line(tmp_path):
    # At `other());`, `x` is already in `_4`: refused before the run. The refusal ended
    # "settable here: x, y", naming the local it had just refused.
    body = parse_body(PASSED)
    blocks = {"start": {14, 15}, "bb1": {14, 15}, "bb2": {15, 16}, "bb3": {16, 17}}
    function = IrFunction("passed", "crate", "src/main.rs", blocks, {}, 13)
    owned = OwnedLocals([body], {HASH: function}, ("src/main.rs",), texts={"src/main.rs": ""})
    # Before `other()` and back from it: both past `_4 = copy _1`.
    owned._positions = lambda site, line, facts, block=None: [(0, 3), (0, 4)]
    session = object.__new__(RustSession)
    session.owned = owned
    session.memory = {(HASH, "x"): True, (HASH, "y"): True}
    session.prepared = SimpleNamespace(source_root=tmp_path)
    session._lines_data = json.dumps({"src/main.rs": [14, 15, 16]})
    session._optimized = lambda: False

    def refusal(name):
        return session.never_settable(tmp_path / "src/main.rs", [15], [name])

    message = refusal("x")
    assert "has already read `x` into a temporary (at line 14)" in message, message
    assert message.endswith(
        "; settable here: y (root locals; fields too where rows mark them [settable])"
    ), message
    # `y` is settable there; asking for a name not in scope lists it too.
    assert refusal("y") is None
    code, message = refusal("nope")
    assert code == "NOT_FOUND" and message.endswith(
        "settable here: y (root locals; fields too where rows mark them [settable])"
    )
    # Where every local in scope is refused there, it says that none is.
    session.memory = {(HASH, "x"): True}
    assert refusal("x").endswith(
        "; settable here: no root locals (fields where rows mark them [settable])"
    )


# A refusal the must-analysis made through a crate function's result was
# false, and the may-analysis missed writes a call makes, or one through a pointer to
# memory no local of the frame stands for. rustc 1.96.0's MIR of this crate's functions:
CALL_WRITES = """use std::env;

#[inline(never)]
fn total(v: Vec<u32>) -> u32 {
    v.iter().sum()
}

#[inline(never)]
fn discard(_: Vec<u32>) -> Vec<u32> {
    vec![7, 1]
}

#[inline(never)]
fn other() -> u32 {
    1
}

fn discarded(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let t = total(discard(vec![x, 1]))
        + other();
    std::hint::black_box(&x);
    t
}

fn pushed(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut v = Vec::new();
    v.push(x);
    let k = other();
    let t = v[0] + k;
    std::hint::black_box(&x);
    t
}

fn written(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut slot = 0u32;
    let p: *mut u32 = &raw mut slot;
    unsafe { std::ptr::write(p, x) };
    let k = other();
    let t = slot + k;
    std::hint::black_box(&x);
    t
}

fn through(p: *mut u32, arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    unsafe { *p = x };
    let k = other();
    let t = unsafe { *p } + k;
    std::hint::black_box(&x);
    t
}

fn raw(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let p = Box::into_raw(Box::new(0u32));
    unsafe { *p = x };
    let k = other();
    let t = unsafe { *p } + k;
    drop(unsafe { Box::from_raw(p) });
    std::hint::black_box(&x);
    t
}

fn main() {
    let x: u32 = env::args().nth(1).unwrap().parse().unwrap();
    let mut s = 0u32;
    println!("DISCARDED {}", discarded(x));
    println!("PUSHED {}", pushed(x));
    println!("WRITTEN {}", written(x));
    println!("THROUGH {}", through(&raw mut s, x));
    println!("RAW {}", raw(x));
}
"""
DISCARDED = """// MIR for `discarded` after PreCodegen

fn discarded(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:18:14: 18:17
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:18:27: 18:30
    let _2: u32;                         // in scope 0 at src/main.rs:19:9: 19:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:19:36: 19:39
    let mut _5: u32;                     // in scope 0 at src/main.rs:20:13: 20:39
    let mut _6: std::vec::Vec<u32>;      // in scope 0 at src/main.rs:20:19: 20:38
    let mut _7: std::vec::Vec<u32>;      // in scope 0 at {ALLOC}:58:9: 60:10
    let mut _8: std::boxed::Box<std::mem::MaybeUninit<[u32; 2]>>; // in scope 0 at {ALLOC}:59:13: 59:96
    let mut _9: std::boxed::Box<std::mem::MaybeUninit<[u32; 2]>>; // in scope 0 at {ALLOC}:59:52: 59:84
    let mut _10: u32;                    // in scope 0 at src/main.rs:20:32: 20:33
    let mut _11: u32;                    // in scope 0 at src/main.rs:21:11: 21:18
    let mut _12: (u32, bool);            // in scope 0 at src/main.rs:20:13: 21:18
    let _13: &u32;                       // in scope 0 at src/main.rs:22:5: 22:29
    let mut _14: &u32;                   // in scope 0 at src/main.rs:22:26: 22:28
    let mut _15: *const std::mem::MaybeUninit<[u32; 2]>; // in scope 0 at {ALLOC}:59:52: 59:84
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:19:9: 19:10
        let _4: u32;                     // in scope 1 at src/main.rs:20:9: 20:10
        let mut _16: *const ();          // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _17: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _18: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _19: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _20: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _21: *const ();          // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _22: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _23: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _24: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _25: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _26: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        scope 2 {
            debug t => _4;               // in scope 2 at src/main.rs:20:9: 20:10
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
        StorageLive(_4);                 // scope 1 at src/main.rs:20:9: 20:10
        StorageLive(_5);                 // scope 1 at src/main.rs:20:13: 20:39
        StorageLive(_6);                 // scope 1 at src/main.rs:20:19: 20:38
        StorageLive(_7);                 // scope 1 at {ALLOC}:58:9: 60:10
        StorageLive(_8);                 // scope 1 at {ALLOC}:59:13: 59:96
        StorageLive(_9);                 // scope 1 at {ALLOC}:59:52: 59:84
        _9 = Box::<[u32; 2]>::new_uninit() -> [return: bb2, unwind continue]; // scope 1 at {ALLOC}:59:52: 59:84
    }

    bb2: {
        StorageLive(_10);                // scope 1 at src/main.rs:20:32: 20:33
        _10 = copy _2;                   // scope 1 at src/main.rs:20:32: 20:33
        _15 = copy ((_9.0: std::ptr::Unique<std::mem::MaybeUninit<[u32; 2]>>).0: std::ptr::NonNull<std::mem::MaybeUninit<[u32; 2]>>) as *const std::mem::MaybeUninit<[u32; 2]> (Transmute); // scope 1 at {ALLOC}:59:86: 59:95
        _16 = copy _15 as *const () (PtrToPtr); // scope 1 at {ALLOC}:59:86: 59:95
        _17 = copy _16 as usize (Transmute); // scope 1 at {ALLOC}:59:86: 59:95
        _18 = Sub(const <std::mem::MaybeUninit<[u32; 2]> as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 1 at {ALLOC}:59:86: 59:95
        _19 = BitAnd(copy _17, copy _18); // scope 1 at {ALLOC}:59:86: 59:95
        _20 = Eq(copy _19, const 0_usize); // scope 1 at {ALLOC}:59:86: 59:95
        assert(copy _20, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <std::mem::MaybeUninit<[u32; 2]> as std::mem::SizedTypeProperties>::ALIGN, copy _17) -> [success: bb10, unwind unreachable]; // scope 1 at {ALLOC}:59:86: 59:95
    }

    bb3: {
        StorageDead(_8);                 // scope 1 at {ALLOC}:60:9: 60:10
        _6 = discard(move _7) -> [return: bb4, unwind: bb9]; // scope 1 at src/main.rs:20:19: 20:38
    }

    bb4: {
        StorageDead(_7);                 // scope 1 at src/main.rs:20:37: 20:38
        _5 = total(move _6) -> [return: bb5, unwind: bb9]; // scope 1 at src/main.rs:20:13: 20:39
    }

    bb5: {
        StorageDead(_6);                 // scope 1 at src/main.rs:20:38: 20:39
        StorageLive(_11);                // scope 1 at src/main.rs:21:11: 21:18
        _11 = other() -> [return: bb6, unwind continue]; // scope 1 at src/main.rs:21:11: 21:18
    }

    bb6: {
        _12 = AddWithOverflow(copy _5, copy _11); // scope 1 at src/main.rs:20:13: 21:18
        assert(!move (_12.1: bool), "attempt to compute `{} + {}`, which would overflow", move _5, move _11) -> [success: bb7, unwind continue]; // scope 1 at src/main.rs:20:13: 21:18
    }

    bb7: {
        _4 = move (_12.0: u32);          // scope 1 at src/main.rs:20:13: 21:18
        StorageDead(_11);                // scope 1 at src/main.rs:21:17: 21:18
        StorageDead(_5);                 // scope 1 at src/main.rs:21:17: 21:18
        StorageLive(_13);                // scope 2 at src/main.rs:22:5: 22:29
        StorageLive(_14);                // scope 2 at src/main.rs:22:26: 22:28
        _14 = &_2;                       // scope 2 at src/main.rs:22:26: 22:28
        _13 = std::hint::black_box::<&u32>(move _14) -> [return: bb8, unwind continue]; // scope 2 at src/main.rs:22:5: 22:29
    }

    bb8: {
        StorageDead(_14);                // scope 2 at src/main.rs:22:28: 22:29
        StorageDead(_13);                // scope 2 at src/main.rs:22:29: 22:30
        _0 = copy _4;                    // scope 2 at src/main.rs:23:5: 23:6
        StorageDead(_4);                 // scope 1 at src/main.rs:24:1: 24:2
        StorageDead(_2);                 // scope 0 at src/main.rs:24:1: 24:2
        return;                          // scope 0 at src/main.rs:24:2: 24:2
    }

    bb9 (cleanup): {
        resume;                          // scope 0 at src/main.rs:18:1: 24:2
    }

    bb10: {
        _21 = copy _15 as *const () (PtrToPtr); // scope 1 at {ALLOC}:59:86: 59:95
        _22 = copy _21 as usize (Transmute); // scope 1 at {ALLOC}:59:86: 59:95
        _23 = Ne(const <std::mem::MaybeUninit<[u32; 2]> as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 1 at {ALLOC}:59:86: 59:95
        _24 = Eq(copy _22, const 0_usize); // scope 1 at {ALLOC}:59:86: 59:95
        _25 = BitAnd(copy _24, copy _23); // scope 1 at {ALLOC}:59:86: 59:95
        _26 = Not(copy _25);             // scope 1 at {ALLOC}:59:86: 59:95
        assert(copy _26, "null pointer dereference occurred") -> [success: bb11, unwind unreachable]; // scope 1 at {ALLOC}:59:86: 59:95
    }

    bb11: {
        ((((*_15).1: std::mem::ManuallyDrop<[u32; 2]>).0: std::mem::MaybeDangling<[u32; 2]>).0: [u32; 2]) = [move _10, const 1_u32]; // scope 1 at {ALLOC}:59:86: 59:95
        StorageDead(_10);                // scope 1 at {ALLOC}:59:94: 59:95
        _8 = move _9;                    // scope 1 at {ALLOC}:59:13: 59:96
        StorageDead(_9);                 // scope 1 at {ALLOC}:59:95: 59:96
        _7 = std::boxed::box_assume_init_into_vec_unsafe::<u32, 2>(move _8) -> [return: bb3, unwind: bb9]; // scope 1 at {ALLOC}:58:9: 60:10
    }
}
""".replace("{ALLOC}", ALLOC)
PUSHED = """// MIR for `pushed` after PreCodegen

fn pushed(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:26:11: 26:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:26:24: 26:27
    let _2: u32;                         // in scope 0 at src/main.rs:27:9: 27:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:27:36: 27:39
    let _5: ();                          // in scope 0 at src/main.rs:29:5: 29:14
    let mut _6: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:29:5: 29:6
    let mut _7: u32;                     // in scope 0 at src/main.rs:29:12: 29:13
    let mut _10: u32;                    // in scope 0 at src/main.rs:31:13: 31:17
    let mut _11: &u32;                   // in scope 0 at src/main.rs:31:14: 31:17
    let mut _12: &std::vec::Vec<u32>;    // in scope 0 at src/main.rs:31:13: 31:14
    let mut _13: u32;                    // in scope 0 at src/main.rs:31:20: 31:21
    let mut _14: (u32, bool);            // in scope 0 at src/main.rs:31:13: 31:21
    let _15: &u32;                       // in scope 0 at src/main.rs:32:5: 32:29
    let mut _16: &u32;                   // in scope 0 at src/main.rs:32:26: 32:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:27:9: 27:10
        let mut _4: std::vec::Vec<u32>;  // in scope 1 at src/main.rs:28:9: 28:14
        scope 2 {
            debug v => _4;               // in scope 2 at src/main.rs:28:9: 28:14
            let _8: u32;                 // in scope 2 at src/main.rs:30:9: 30:10
            scope 3 {
                debug k => _8;           // in scope 3 at src/main.rs:30:9: 30:10
                let _9: u32;             // in scope 3 at src/main.rs:31:9: 31:10
                scope 4 {
                    debug t => _9;       // in scope 4 at src/main.rs:31:9: 31:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:27:9: 27:10
        StorageLive(_3);                 // scope 0 at src/main.rs:27:36: 27:39
        _3 = copy _1;                    // scope 0 at src/main.rs:27:36: 27:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:27:13: 27:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:27:39: 27:40
        StorageLive(_4);                 // scope 1 at src/main.rs:28:9: 28:14
        _4 = Vec::<u32>::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:28:17: 28:27
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:29:5: 29:14
        StorageLive(_6);                 // scope 2 at src/main.rs:29:5: 29:6
        _6 = &mut _4;                    // scope 2 at src/main.rs:29:5: 29:6
        StorageLive(_7);                 // scope 2 at src/main.rs:29:12: 29:13
        _7 = copy _2;                    // scope 2 at src/main.rs:29:12: 29:13
        _5 = Vec::<u32>::push(move _6, move _7) -> [return: bb3, unwind: bb9]; // scope 2 at src/main.rs:29:5: 29:14
    }

    bb3: {
        StorageDead(_7);                 // scope 2 at src/main.rs:29:13: 29:14
        StorageDead(_6);                 // scope 2 at src/main.rs:29:13: 29:14
        StorageDead(_5);                 // scope 2 at src/main.rs:29:14: 29:15
        StorageLive(_8);                 // scope 2 at src/main.rs:30:9: 30:10
        _8 = other() -> [return: bb4, unwind: bb9]; // scope 2 at src/main.rs:30:13: 30:20
    }

    bb4: {
        StorageLive(_9);                 // scope 3 at src/main.rs:31:9: 31:10
        StorageLive(_10);                // scope 3 at src/main.rs:31:13: 31:17
        StorageLive(_11);                // scope 3 at src/main.rs:31:14: 31:17
        StorageLive(_12);                // scope 3 at src/main.rs:31:13: 31:14
        _12 = &_4;                       // scope 3 at src/main.rs:31:13: 31:14
        _11 = <Vec<u32> as Index<usize>>::index(move _12, const 0_usize) -> [return: bb5, unwind: bb9]; // scope 3 at src/main.rs:31:14: 31:17
    }

    bb5: {
        _10 = copy (*_11);               // scope 3 at src/main.rs:31:13: 31:17
        StorageDead(_12);                // scope 3 at src/main.rs:31:16: 31:17
        StorageLive(_13);                // scope 3 at src/main.rs:31:20: 31:21
        _13 = copy _8;                   // scope 3 at src/main.rs:31:20: 31:21
        _14 = AddWithOverflow(copy _10, copy _13); // scope 3 at src/main.rs:31:13: 31:21
        assert(!move (_14.1: bool), "attempt to compute `{} + {}`, which would overflow", move _10, move _13) -> [success: bb6, unwind: bb9]; // scope 3 at src/main.rs:31:13: 31:21
    }

    bb6: {
        _9 = move (_14.0: u32);          // scope 3 at src/main.rs:31:13: 31:21
        StorageDead(_13);                // scope 3 at src/main.rs:31:20: 31:21
        StorageDead(_10);                // scope 3 at src/main.rs:31:20: 31:21
        StorageDead(_11);                // scope 3 at src/main.rs:31:21: 31:22
        StorageLive(_15);                // scope 4 at src/main.rs:32:5: 32:29
        StorageLive(_16);                // scope 4 at src/main.rs:32:26: 32:28
        _16 = &_2;                       // scope 4 at src/main.rs:32:26: 32:28
        _15 = std::hint::black_box::<&u32>(move _16) -> [return: bb7, unwind: bb9]; // scope 4 at src/main.rs:32:5: 32:29
    }

    bb7: {
        StorageDead(_16);                // scope 4 at src/main.rs:32:28: 32:29
        StorageDead(_15);                // scope 4 at src/main.rs:32:29: 32:30
        _0 = copy _9;                    // scope 4 at src/main.rs:33:5: 33:6
        StorageDead(_9);                 // scope 3 at src/main.rs:34:1: 34:2
        StorageDead(_8);                 // scope 2 at src/main.rs:34:1: 34:2
        drop(_4) -> [return: bb8, unwind: bb10]; // scope 1 at src/main.rs:34:1: 34:2
    }

    bb8: {
        StorageDead(_4);                 // scope 1 at src/main.rs:34:1: 34:2
        StorageDead(_2);                 // scope 0 at src/main.rs:34:1: 34:2
        return;                          // scope 0 at src/main.rs:34:2: 34:2
    }

    bb9 (cleanup): {
        drop(_4) -> [return: bb10, unwind terminate(cleanup)]; // scope 1 at src/main.rs:34:1: 34:2
    }

    bb10 (cleanup): {
        resume;                          // scope 0 at src/main.rs:26:1: 34:2
    }
}
""".replace("{ALLOC}", ALLOC)
WRITTEN = """// MIR for `written` after PreCodegen

fn written(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:36:12: 36:15
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:36:25: 36:28
    let _2: u32;                         // in scope 0 at src/main.rs:37:9: 37:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:37:36: 37:39
    let _6: ();                          // in scope 0 at src/main.rs:40:14: 40:35
    let mut _7: *mut u32;                // in scope 0 at src/main.rs:40:30: 40:31
    let mut _8: u32;                     // in scope 0 at src/main.rs:40:33: 40:34
    let mut _11: u32;                    // in scope 0 at src/main.rs:42:13: 42:17
    let mut _12: u32;                    // in scope 0 at src/main.rs:42:20: 42:21
    let mut _13: (u32, bool);            // in scope 0 at src/main.rs:42:13: 42:21
    let _14: &u32;                       // in scope 0 at src/main.rs:43:5: 43:29
    let mut _15: &u32;                   // in scope 0 at src/main.rs:43:26: 43:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:37:9: 37:10
        let mut _4: u32;                 // in scope 1 at src/main.rs:38:9: 38:17
        scope 2 {
            debug slot => _4;            // in scope 2 at src/main.rs:38:9: 38:17
            let _5: *mut u32;            // in scope 2 at src/main.rs:39:9: 39:10
            scope 3 {
                debug p => _5;           // in scope 3 at src/main.rs:39:9: 39:10
                let _9: u32;             // in scope 3 at src/main.rs:41:9: 41:10
                scope 4 {
                    debug k => _9;       // in scope 4 at src/main.rs:41:9: 41:10
                    let _10: u32;        // in scope 4 at src/main.rs:42:9: 42:10
                    scope 5 {
                        debug t => _10;  // in scope 5 at src/main.rs:42:9: 42:10
                    }
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:37:9: 37:10
        StorageLive(_3);                 // scope 0 at src/main.rs:37:36: 37:39
        _3 = copy _1;                    // scope 0 at src/main.rs:37:36: 37:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:37:13: 37:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:37:39: 37:40
        StorageLive(_4);                 // scope 1 at src/main.rs:38:9: 38:17
        _4 = const 0_u32;                // scope 1 at src/main.rs:38:20: 38:24
        StorageLive(_5);                 // scope 2 at src/main.rs:39:9: 39:10
        _5 = &raw mut _4;                // scope 2 at src/main.rs:39:23: 39:36
        StorageLive(_6);                 // scope 3 at src/main.rs:40:5: 40:37
        StorageLive(_7);                 // scope 3 at src/main.rs:40:30: 40:31
        _7 = copy _5;                    // scope 3 at src/main.rs:40:30: 40:31
        StorageLive(_8);                 // scope 3 at src/main.rs:40:33: 40:34
        _8 = copy _2;                    // scope 3 at src/main.rs:40:33: 40:34
        _6 = std::ptr::write::<u32>(move _7, move _8) -> [return: bb2, unwind continue]; // scope 3 at src/main.rs:40:14: 40:35
    }

    bb2: {
        StorageDead(_8);                 // scope 3 at src/main.rs:40:34: 40:35
        StorageDead(_7);                 // scope 3 at src/main.rs:40:34: 40:35
        StorageDead(_6);                 // scope 3 at src/main.rs:40:37: 40:38
        StorageLive(_9);                 // scope 3 at src/main.rs:41:9: 41:10
        _9 = other() -> [return: bb3, unwind continue]; // scope 3 at src/main.rs:41:13: 41:20
    }

    bb3: {
        StorageLive(_10);                // scope 4 at src/main.rs:42:9: 42:10
        StorageLive(_11);                // scope 4 at src/main.rs:42:13: 42:17
        _11 = copy _4;                   // scope 4 at src/main.rs:42:13: 42:17
        StorageLive(_12);                // scope 4 at src/main.rs:42:20: 42:21
        _12 = copy _9;                   // scope 4 at src/main.rs:42:20: 42:21
        _13 = AddWithOverflow(copy _11, copy _12); // scope 4 at src/main.rs:42:13: 42:21
        assert(!move (_13.1: bool), "attempt to compute `{} + {}`, which would overflow", move _11, move _12) -> [success: bb4, unwind continue]; // scope 4 at src/main.rs:42:13: 42:21
    }

    bb4: {
        _10 = move (_13.0: u32);         // scope 4 at src/main.rs:42:13: 42:21
        StorageDead(_12);                // scope 4 at src/main.rs:42:20: 42:21
        StorageDead(_11);                // scope 4 at src/main.rs:42:20: 42:21
        StorageLive(_14);                // scope 5 at src/main.rs:43:5: 43:29
        StorageLive(_15);                // scope 5 at src/main.rs:43:26: 43:28
        _15 = &_2;                       // scope 5 at src/main.rs:43:26: 43:28
        _14 = std::hint::black_box::<&u32>(move _15) -> [return: bb5, unwind continue]; // scope 5 at src/main.rs:43:5: 43:29
    }

    bb5: {
        StorageDead(_15);                // scope 5 at src/main.rs:43:28: 43:29
        StorageDead(_14);                // scope 5 at src/main.rs:43:29: 43:30
        _0 = copy _10;                   // scope 5 at src/main.rs:44:5: 44:6
        StorageDead(_10);                // scope 4 at src/main.rs:45:1: 45:2
        StorageDead(_9);                 // scope 3 at src/main.rs:45:1: 45:2
        StorageDead(_5);                 // scope 2 at src/main.rs:45:1: 45:2
        StorageDead(_4);                 // scope 1 at src/main.rs:45:1: 45:2
        StorageDead(_2);                 // scope 0 at src/main.rs:45:1: 45:2
        return;                          // scope 0 at src/main.rs:45:2: 45:2
    }
}
""".replace("{ALLOC}", ALLOC)
THROUGH = """// MIR for `through` after PreCodegen

fn through(_1: *mut u32, _2: u32) -> u32 {
    debug p => _1;                       // in scope 0 at src/main.rs:47:12: 47:13
    debug arg => _2;                     // in scope 0 at src/main.rs:47:25: 47:28
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:47:38: 47:41
    let _3: u32;                         // in scope 0 at src/main.rs:48:9: 48:10
    let mut _4: u32;                     // in scope 0 at src/main.rs:48:36: 48:39
    let _5: ();                          // in scope 0 at src/main.rs:49:14: 49:20
    let mut _6: u32;                     // in scope 0 at src/main.rs:49:19: 49:20
    let mut _9: u32;                     // in scope 0 at src/main.rs:51:13: 51:26
    let mut _10: u32;                    // in scope 0 at src/main.rs:51:29: 51:30
    let mut _11: (u32, bool);            // in scope 0 at src/main.rs:51:13: 51:30
    let _12: &u32;                       // in scope 0 at src/main.rs:52:5: 52:29
    let mut _13: &u32;                   // in scope 0 at src/main.rs:52:26: 52:28
    scope 1 {
        debug x => _3;                   // in scope 1 at src/main.rs:48:9: 48:10
        let _7: u32;                     // in scope 1 at src/main.rs:50:9: 50:10
        let mut _19: *const ();          // in scope 1 at src/main.rs:49:14: 49:20
        let mut _20: usize;              // in scope 1 at src/main.rs:49:14: 49:20
        let mut _21: usize;              // in scope 1 at src/main.rs:49:14: 49:20
        let mut _22: usize;              // in scope 1 at src/main.rs:49:14: 49:20
        let mut _23: bool;               // in scope 1 at src/main.rs:49:14: 49:20
        let mut _24: *const ();          // in scope 1 at src/main.rs:49:14: 49:20
        let mut _25: usize;              // in scope 1 at src/main.rs:49:14: 49:20
        let mut _26: bool;               // in scope 1 at src/main.rs:49:14: 49:20
        let mut _27: bool;               // in scope 1 at src/main.rs:49:14: 49:20
        let mut _28: bool;               // in scope 1 at src/main.rs:49:14: 49:20
        let mut _29: bool;               // in scope 1 at src/main.rs:49:14: 49:20
        scope 2 {
            debug k => _7;               // in scope 2 at src/main.rs:50:9: 50:10
            let _8: u32;                 // in scope 2 at src/main.rs:51:9: 51:10
            let mut _14: *const ();      // in scope 2 at src/main.rs:51:22: 51:24
            let mut _15: usize;          // in scope 2 at src/main.rs:51:22: 51:24
            let mut _16: usize;          // in scope 2 at src/main.rs:51:22: 51:24
            let mut _17: usize;          // in scope 2 at src/main.rs:51:22: 51:24
            let mut _18: bool;           // in scope 2 at src/main.rs:51:22: 51:24
            let mut _30: *const ();      // in scope 2 at src/main.rs:51:22: 51:24
            let mut _31: usize;          // in scope 2 at src/main.rs:51:22: 51:24
            let mut _32: bool;           // in scope 2 at src/main.rs:51:22: 51:24
            let mut _33: bool;           // in scope 2 at src/main.rs:51:22: 51:24
            let mut _34: bool;           // in scope 2 at src/main.rs:51:22: 51:24
            let mut _35: bool;           // in scope 2 at src/main.rs:51:22: 51:24
            scope 3 {
                debug t => _8;           // in scope 3 at src/main.rs:51:9: 51:10
            }
        }
    }

    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:48:9: 48:10
        StorageLive(_4);                 // scope 0 at src/main.rs:48:36: 48:39
        _4 = copy _2;                    // scope 0 at src/main.rs:48:36: 48:39
        _3 = identity::<u32>(move _4) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:48:13: 48:40
    }

    bb1: {
        StorageDead(_4);                 // scope 0 at src/main.rs:48:39: 48:40
        StorageLive(_5);                 // scope 1 at src/main.rs:49:5: 49:22
        StorageLive(_6);                 // scope 1 at src/main.rs:49:19: 49:20
        _6 = copy _3;                    // scope 1 at src/main.rs:49:19: 49:20
        _19 = copy _1 as *const () (PtrToPtr); // scope 1 at src/main.rs:49:14: 49:20
        _20 = copy _19 as usize (Transmute); // scope 1 at src/main.rs:49:14: 49:20
        _21 = Sub(const <u32 as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 1 at src/main.rs:49:14: 49:20
        _22 = BitAnd(copy _20, copy _21); // scope 1 at src/main.rs:49:14: 49:20
        _23 = Eq(copy _22, const 0_usize); // scope 1 at src/main.rs:49:14: 49:20
        assert(copy _23, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <u32 as std::mem::SizedTypeProperties>::ALIGN, copy _20) -> [success: bb6, unwind unreachable]; // scope 1 at src/main.rs:49:14: 49:20
    }

    bb2: {
        StorageLive(_8);                 // scope 2 at src/main.rs:51:9: 51:10
        StorageLive(_9);                 // scope 2 at src/main.rs:51:13: 51:26
        _14 = copy _1 as *const () (PtrToPtr); // scope 2 at src/main.rs:51:22: 51:24
        _15 = copy _14 as usize (Transmute); // scope 2 at src/main.rs:51:22: 51:24
        _16 = Sub(const <u32 as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 2 at src/main.rs:51:22: 51:24
        _17 = BitAnd(copy _15, copy _16); // scope 2 at src/main.rs:51:22: 51:24
        _18 = Eq(copy _17, const 0_usize); // scope 2 at src/main.rs:51:22: 51:24
        assert(copy _18, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <u32 as std::mem::SizedTypeProperties>::ALIGN, copy _15) -> [success: bb5, unwind unreachable]; // scope 2 at src/main.rs:51:22: 51:24
    }

    bb3: {
        _8 = move (_11.0: u32);          // scope 2 at src/main.rs:51:13: 51:30
        StorageDead(_10);                // scope 2 at src/main.rs:51:29: 51:30
        StorageDead(_9);                 // scope 2 at src/main.rs:51:29: 51:30
        StorageLive(_12);                // scope 3 at src/main.rs:52:5: 52:29
        StorageLive(_13);                // scope 3 at src/main.rs:52:26: 52:28
        _13 = &_3;                       // scope 3 at src/main.rs:52:26: 52:28
        _12 = std::hint::black_box::<&u32>(move _13) -> [return: bb4, unwind continue]; // scope 3 at src/main.rs:52:5: 52:29
    }

    bb4: {
        StorageDead(_13);                // scope 3 at src/main.rs:52:28: 52:29
        StorageDead(_12);                // scope 3 at src/main.rs:52:29: 52:30
        _0 = copy _8;                    // scope 3 at src/main.rs:53:5: 53:6
        StorageDead(_8);                 // scope 2 at src/main.rs:54:1: 54:2
        StorageDead(_7);                 // scope 1 at src/main.rs:54:1: 54:2
        StorageDead(_3);                 // scope 0 at src/main.rs:54:1: 54:2
        return;                          // scope 0 at src/main.rs:54:2: 54:2
    }

    bb5: {
        _30 = copy _1 as *const () (PtrToPtr); // scope 2 at src/main.rs:51:22: 51:24
        _31 = copy _30 as usize (Transmute); // scope 2 at src/main.rs:51:22: 51:24
        _32 = Ne(const <u32 as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 2 at src/main.rs:51:22: 51:24
        _33 = Eq(copy _31, const 0_usize); // scope 2 at src/main.rs:51:22: 51:24
        _34 = BitAnd(copy _33, copy _32); // scope 2 at src/main.rs:51:22: 51:24
        _35 = Not(copy _34);             // scope 2 at src/main.rs:51:22: 51:24
        assert(copy _35, "null pointer dereference occurred") -> [success: bb8, unwind unreachable]; // scope 2 at src/main.rs:51:22: 51:24
    }

    bb6: {
        _24 = copy _1 as *const () (PtrToPtr); // scope 1 at src/main.rs:49:14: 49:20
        _25 = copy _24 as usize (Transmute); // scope 1 at src/main.rs:49:14: 49:20
        _26 = Ne(const <u32 as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 1 at src/main.rs:49:14: 49:20
        _27 = Eq(copy _25, const 0_usize); // scope 1 at src/main.rs:49:14: 49:20
        _28 = BitAnd(copy _27, copy _26); // scope 1 at src/main.rs:49:14: 49:20
        _29 = Not(copy _28);             // scope 1 at src/main.rs:49:14: 49:20
        assert(copy _29, "null pointer dereference occurred") -> [success: bb7, unwind unreachable]; // scope 1 at src/main.rs:49:14: 49:20
    }

    bb7: {
        (*_1) = move _6;                 // scope 1 at src/main.rs:49:14: 49:20
        _5 = const ();                   // scope 1 at src/main.rs:49:14: 49:20
        StorageDead(_6);                 // scope 1 at src/main.rs:49:19: 49:20
        StorageDead(_5);                 // scope 1 at src/main.rs:49:22: 49:23
        StorageLive(_7);                 // scope 1 at src/main.rs:50:9: 50:10
        _7 = other() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:50:13: 50:20
    }

    bb8: {
        _9 = copy (*_1);                 // scope 2 at src/main.rs:51:22: 51:24
        StorageLive(_10);                // scope 2 at src/main.rs:51:29: 51:30
        _10 = copy _7;                   // scope 2 at src/main.rs:51:29: 51:30
        _11 = AddWithOverflow(copy _9, copy _10); // scope 2 at src/main.rs:51:13: 51:30
        assert(!move (_11.1: bool), "attempt to compute `{} + {}`, which would overflow", move _9, move _10) -> [success: bb3, unwind continue]; // scope 2 at src/main.rs:51:13: 51:30
    }
}
""".replace("{ALLOC}", ALLOC)
RAW = """// MIR for `raw` after PreCodegen

fn raw(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:56:8: 56:11
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:56:21: 56:24
    let _2: u32;                         // in scope 0 at src/main.rs:57:9: 57:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:57:36: 57:39
    let mut _5: std::boxed::Box<u32>;    // in scope 0 at src/main.rs:58:27: 58:41
    let _6: ();                          // in scope 0 at src/main.rs:59:14: 59:20
    let mut _7: u32;                     // in scope 0 at src/main.rs:59:19: 59:20
    let mut _10: u32;                    // in scope 0 at src/main.rs:61:13: 61:26
    let mut _11: u32;                    // in scope 0 at src/main.rs:61:29: 61:30
    let mut _12: (u32, bool);            // in scope 0 at src/main.rs:61:13: 61:30
    let _13: ();                         // in scope 0 at src/main.rs:62:5: 62:38
    let mut _14: std::boxed::Box<u32>;   // in scope 0 at src/main.rs:62:10: 62:37
    let mut _15: *mut u32;               // in scope 0 at src/main.rs:62:33: 62:34
    let _16: &u32;                       // in scope 0 at src/main.rs:63:5: 63:29
    let mut _17: &u32;                   // in scope 0 at src/main.rs:63:26: 63:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:57:9: 57:10
        let _4: *mut u32;                // in scope 1 at src/main.rs:58:9: 58:10
        scope 2 {
            debug p => _4;               // in scope 2 at src/main.rs:58:9: 58:10
            let _8: u32;                 // in scope 2 at src/main.rs:60:9: 60:10
            let mut _23: *const ();      // in scope 2 at src/main.rs:59:14: 59:20
            let mut _24: usize;          // in scope 2 at src/main.rs:59:14: 59:20
            let mut _25: usize;          // in scope 2 at src/main.rs:59:14: 59:20
            let mut _26: usize;          // in scope 2 at src/main.rs:59:14: 59:20
            let mut _27: bool;           // in scope 2 at src/main.rs:59:14: 59:20
            let mut _28: *const ();      // in scope 2 at src/main.rs:59:14: 59:20
            let mut _29: usize;          // in scope 2 at src/main.rs:59:14: 59:20
            let mut _30: bool;           // in scope 2 at src/main.rs:59:14: 59:20
            let mut _31: bool;           // in scope 2 at src/main.rs:59:14: 59:20
            let mut _32: bool;           // in scope 2 at src/main.rs:59:14: 59:20
            let mut _33: bool;           // in scope 2 at src/main.rs:59:14: 59:20
            scope 3 {
                debug k => _8;           // in scope 3 at src/main.rs:60:9: 60:10
                let _9: u32;             // in scope 3 at src/main.rs:61:9: 61:10
                let mut _18: *const ();  // in scope 3 at src/main.rs:61:22: 61:24
                let mut _19: usize;      // in scope 3 at src/main.rs:61:22: 61:24
                let mut _20: usize;      // in scope 3 at src/main.rs:61:22: 61:24
                let mut _21: usize;      // in scope 3 at src/main.rs:61:22: 61:24
                let mut _22: bool;       // in scope 3 at src/main.rs:61:22: 61:24
                let mut _34: *const ();  // in scope 3 at src/main.rs:61:22: 61:24
                let mut _35: usize;      // in scope 3 at src/main.rs:61:22: 61:24
                let mut _36: bool;       // in scope 3 at src/main.rs:61:22: 61:24
                let mut _37: bool;       // in scope 3 at src/main.rs:61:22: 61:24
                let mut _38: bool;       // in scope 3 at src/main.rs:61:22: 61:24
                let mut _39: bool;       // in scope 3 at src/main.rs:61:22: 61:24
                scope 4 {
                    debug t => _9;       // in scope 4 at src/main.rs:61:9: 61:10
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
        StorageLive(_4);                 // scope 1 at src/main.rs:58:9: 58:10
        StorageLive(_5);                 // scope 1 at src/main.rs:58:27: 58:41
        _5 = Box::<u32>::new(const 0_u32) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:58:27: 58:41
    }

    bb2: {
        _4 = Box::<u32>::into_raw(move _5) -> [return: bb3, unwind: bb9]; // scope 1 at src/main.rs:58:13: 58:42
    }

    bb3: {
        StorageDead(_5);                 // scope 1 at src/main.rs:58:41: 58:42
        StorageLive(_6);                 // scope 2 at src/main.rs:59:5: 59:22
        StorageLive(_7);                 // scope 2 at src/main.rs:59:19: 59:20
        _7 = copy _2;                    // scope 2 at src/main.rs:59:19: 59:20
        _23 = copy _4 as *const () (PtrToPtr); // scope 2 at src/main.rs:59:14: 59:20
        _24 = copy _23 as usize (Transmute); // scope 2 at src/main.rs:59:14: 59:20
        _25 = Sub(const <u32 as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 2 at src/main.rs:59:14: 59:20
        _26 = BitAnd(copy _24, copy _25); // scope 2 at src/main.rs:59:14: 59:20
        _27 = Eq(copy _26, const 0_usize); // scope 2 at src/main.rs:59:14: 59:20
        assert(copy _27, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <u32 as std::mem::SizedTypeProperties>::ALIGN, copy _24) -> [success: bb11, unwind unreachable]; // scope 2 at src/main.rs:59:14: 59:20
    }

    bb4: {
        StorageLive(_9);                 // scope 3 at src/main.rs:61:9: 61:10
        StorageLive(_10);                // scope 3 at src/main.rs:61:13: 61:26
        _18 = copy _4 as *const () (PtrToPtr); // scope 3 at src/main.rs:61:22: 61:24
        _19 = copy _18 as usize (Transmute); // scope 3 at src/main.rs:61:22: 61:24
        _20 = Sub(const <u32 as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 3 at src/main.rs:61:22: 61:24
        _21 = BitAnd(copy _19, copy _20); // scope 3 at src/main.rs:61:22: 61:24
        _22 = Eq(copy _21, const 0_usize); // scope 3 at src/main.rs:61:22: 61:24
        assert(copy _22, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <u32 as std::mem::SizedTypeProperties>::ALIGN, copy _19) -> [success: bb10, unwind unreachable]; // scope 3 at src/main.rs:61:22: 61:24
    }

    bb5: {
        _9 = move (_12.0: u32);          // scope 3 at src/main.rs:61:13: 61:30
        StorageDead(_11);                // scope 3 at src/main.rs:61:29: 61:30
        StorageDead(_10);                // scope 3 at src/main.rs:61:29: 61:30
        StorageLive(_13);                // scope 4 at src/main.rs:62:5: 62:38
        StorageLive(_14);                // scope 4 at src/main.rs:62:10: 62:37
        StorageLive(_15);                // scope 4 at src/main.rs:62:33: 62:34
        _15 = copy _4;                   // scope 4 at src/main.rs:62:33: 62:34
        _14 = Box::<u32>::from_raw(move _15) -> [return: bb6, unwind continue]; // scope 4 at src/main.rs:62:19: 62:35
    }

    bb6: {
        StorageDead(_15);                // scope 4 at src/main.rs:62:34: 62:35
        _13 = std::mem::drop::<Box<u32>>(move _14) -> [return: bb7, unwind: bb9]; // scope 4 at src/main.rs:62:5: 62:38
    }

    bb7: {
        StorageDead(_14);                // scope 4 at src/main.rs:62:37: 62:38
        StorageDead(_13);                // scope 4 at src/main.rs:62:38: 62:39
        StorageLive(_16);                // scope 4 at src/main.rs:63:5: 63:29
        StorageLive(_17);                // scope 4 at src/main.rs:63:26: 63:28
        _17 = &_2;                       // scope 4 at src/main.rs:63:26: 63:28
        _16 = std::hint::black_box::<&u32>(move _17) -> [return: bb8, unwind continue]; // scope 4 at src/main.rs:63:5: 63:29
    }

    bb8: {
        StorageDead(_17);                // scope 4 at src/main.rs:63:28: 63:29
        StorageDead(_16);                // scope 4 at src/main.rs:63:29: 63:30
        _0 = copy _9;                    // scope 4 at src/main.rs:64:5: 64:6
        StorageDead(_9);                 // scope 3 at src/main.rs:65:1: 65:2
        StorageDead(_8);                 // scope 2 at src/main.rs:65:1: 65:2
        StorageDead(_4);                 // scope 1 at src/main.rs:65:1: 65:2
        StorageDead(_2);                 // scope 0 at src/main.rs:65:1: 65:2
        return;                          // scope 0 at src/main.rs:65:2: 65:2
    }

    bb9 (cleanup): {
        resume;                          // scope 0 at src/main.rs:56:1: 65:2
    }

    bb10: {
        _34 = copy _4 as *const () (PtrToPtr); // scope 3 at src/main.rs:61:22: 61:24
        _35 = copy _34 as usize (Transmute); // scope 3 at src/main.rs:61:22: 61:24
        _36 = Ne(const <u32 as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 3 at src/main.rs:61:22: 61:24
        _37 = Eq(copy _35, const 0_usize); // scope 3 at src/main.rs:61:22: 61:24
        _38 = BitAnd(copy _37, copy _36); // scope 3 at src/main.rs:61:22: 61:24
        _39 = Not(copy _38);             // scope 3 at src/main.rs:61:22: 61:24
        assert(copy _39, "null pointer dereference occurred") -> [success: bb13, unwind unreachable]; // scope 3 at src/main.rs:61:22: 61:24
    }

    bb11: {
        _28 = copy _4 as *const () (PtrToPtr); // scope 2 at src/main.rs:59:14: 59:20
        _29 = copy _28 as usize (Transmute); // scope 2 at src/main.rs:59:14: 59:20
        _30 = Ne(const <u32 as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 2 at src/main.rs:59:14: 59:20
        _31 = Eq(copy _29, const 0_usize); // scope 2 at src/main.rs:59:14: 59:20
        _32 = BitAnd(copy _31, copy _30); // scope 2 at src/main.rs:59:14: 59:20
        _33 = Not(copy _32);             // scope 2 at src/main.rs:59:14: 59:20
        assert(copy _33, "null pointer dereference occurred") -> [success: bb12, unwind unreachable]; // scope 2 at src/main.rs:59:14: 59:20
    }

    bb12: {
        (*_4) = move _7;                 // scope 2 at src/main.rs:59:14: 59:20
        _6 = const ();                   // scope 2 at src/main.rs:59:14: 59:20
        StorageDead(_7);                 // scope 2 at src/main.rs:59:19: 59:20
        StorageDead(_6);                 // scope 2 at src/main.rs:59:22: 59:23
        StorageLive(_8);                 // scope 2 at src/main.rs:60:9: 60:10
        _8 = other() -> [return: bb4, unwind continue]; // scope 2 at src/main.rs:60:13: 60:20
    }

    bb13: {
        _10 = copy (*_4);                // scope 3 at src/main.rs:61:22: 61:24
        StorageLive(_11);                // scope 3 at src/main.rs:61:29: 61:30
        _11 = copy _8;                   // scope 3 at src/main.rs:61:29: 61:30
        _12 = AddWithOverflow(copy _10, copy _11); // scope 3 at src/main.rs:61:13: 61:30
        assert(!move (_12.1: bool), "attempt to compute `{} + {}`, which would overflow", move _10, move _11) -> [success: bb5, unwind continue]; // scope 3 at src/main.rs:61:13: 61:30
    }
}
""".replace("{ALLOC}", ALLOC)


def call_writes(text, name, line, statement):
    return why(text, CALL_WRITES, name, line, statement)


def kept(name, lines, verb="hold"):
    """The refusal of a binding of the program's own that may hold (or point at) a copy."""
    return (
        f"the program's own `{name}` (at {lines}) may {verb} a copy of it that it goes on with, "
        "so a change to `x` may never be seen: Peeky proves no read of the changed part first "
        "(a reference lent to a call, or `{:?}`, is not one)",
        f"the program may hold a copy ({lines})",
    )


def test_a_crate_functions_result_is_no_sure_copy_of_what_it_was_given():
    # `total(discard(vec![x, 1]))` / `+ other();` with `discard` returning `vec![7, 1]`:
    # nothing live holds `x`, but "has already read `x` into a temporary" was said.
    body = body_of(DISCARDED)
    stop = place(body, "_11 = other()")
    assert read(body, 2, stop) == set()
    assert used(body, 2, stop) == {5}
    # `vec!`'s Vec surely held it, until `discard` was given it.
    assert 7 in read(body, 2, after(body, "_7 = std::boxed::box_assume_init_into_vec_unsafe"))
    assert read(body, 2, after(body, "_6 = discard(")) == set()
    assert call_writes(DISCARDED, "discarded", 21, "_11 = other()") == (
        "a statement under way at the stop reads it into a temporary (at line 20), so it may "
        "hold a copy of it, which it uses after the stop",
        "a statement under way may hold a copy (line 20)",
    )


def test_a_call_given_a_mutable_pointer_may_store_a_copy_where_it_points():
    # `v.push(x)`: `Vec::push(move _6, move _7)` with `_6 = &mut v`. Then `let k = other();`
    # and `v[0] + k`: `v` holds `x`'s copy, which the program goes on with.
    body = body_of(PUSHED)
    stop = place(body, "_8 = other()")
    assert 4 in body.direct_copies_at(2, stop) and body.copy_undoes(4, 2, stop)
    assert call_writes(PUSHED, "pushed", 30, "_8 = other()") == kept("v", "lines 28 and 29")
    # Not where the call is given no copy: `v.push(7)`.
    seven = body_of(PUSHED.replace("_7 = copy _2;", "_7 = const 7_u32;"))
    assert 4 not in seven.direct_copies_at(2, stop)


def test_ptr_write_stores_a_copy_in_what_its_pointer_points_at():
    # `p = &raw mut slot; ptr::write(p, x);`, then `slot + k`.
    body = body_of(WRITTEN)
    stop = place(body, "_9 = other()")
    direct = body.direct_copies_at(2, stop)
    assert 4 in direct and 5 not in direct
    assert call_writes(WRITTEN, "written", 41, "_9 = other()") == kept("slot", "line 40")


def test_a_pointer_to_memory_no_local_stands_for_stands_for_it():
    # `*p = x;` through an argument `p`, read back by `*p` on the next line: no local of
    # the frame holds the copy, so `p` stands for what it points at.
    body = body_of(THROUGH)
    stop = place(body, "_7 = other()")
    assert 1 in body.direct_copies_at(3, stop) and body.copy_undoes(1, 3, stop)
    assert why(THROUGH, CALL_WRITES, "through", 50, "_7 = other()", "x") == kept(
        "p", "line 49", "point at"
    )
    # Nor does the Box `Box::into_raw` was given, once it is moved out of.
    body = body_of(RAW)
    stop = place(body, "_8 = other()")
    assert 4 in body.direct_copies_at(2, stop) and body.copy_undoes(4, 2, stop)
    assert call_writes(RAW, "raw", 60, "_8 = other()") == kept("p", "lines 58 and 59", "point at")


# A write through a destination propagated only direct copies, not
# what may hold or point at one; and a later read through `r` of `v.last_mut().unwrap()`
# was no use of `v`. rustc 1.96.0's MIR of these crates' functions:
DESTINATIONS = """use std::collections::HashMap;
use std::env;

#[inline(never)]
fn other() -> u32 {
    1
}

fn sliced(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let a = [x];
    let mut v: Vec<u32> = Vec::new();
    v.extend_from_slice(&a);
    let k = other(); // SLICED
    let t = v[0] + k;
    std::hint::black_box(&x);
    t
}

fn literal(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut v: Vec<u32> = Vec::new();
    v.extend_from_slice(&[x]);
    let k = other(); // LITERAL
    let t = v[0] + k;
    std::hint::black_box(&x);
    t
}

fn last(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut v = vec![0u32];
    let r = v.last_mut().unwrap();
    *r = x;
    let k = other(); // LAST
    let t = *r + k;
    std::hint::black_box(&x);
    t
}

fn mapped(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut m: HashMap<u32, u32> = HashMap::new();
    m.insert(1, x);
    let k = other(); // MAPPED
    let t = m[&1] + k;
    std::hint::black_box(&x);
    t
}

fn texted(arg: u32) -> usize {
    let x = std::convert::identity(arg);
    let mut s = String::new();
    s.push_str(&x.to_string());
    let k = other() as usize; // TEXTED
    let t = s.len() + k;
    std::hint::black_box(&x);
    t
}

fn first(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut v = vec![0u32];
    let r = v.iter_mut().next().unwrap();
    *r = x;
    let k = other(); // FIRST
    let t = *r + k;
    std::hint::black_box(&x);
    t
}

fn inserted(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut o: Option<u32> = None;
    let r = o.insert(x);
    let k = other(); // INSERTED
    let t = *r + k;
    std::hint::black_box(&x);
    t
}

fn got(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut o: Option<u32> = None;
    let r = o.get_or_insert(x);
    let k = other(); // GOT
    let t = *r + k;
    std::hint::black_box(&x);
    t
}

fn main() {
    let x: u32 = env::args().nth(1).unwrap().parse().unwrap();
    println!("SLICED {}", sliced(x));
    println!("LITERAL {}", literal(x));
    println!("LAST {}", last(x));
    println!("MAPPED {}", mapped(x));
    println!("TEXTED {}", texted(x));
    println!("FIRST {}", first(x));
    println!("INSERTED {}", inserted(x));
    println!("GOT {}", got(x));
}
"""
CELLS = """use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::env;

#[inline(never)]
fn other() -> u32 {
    1
}

fn arrayed(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut v: Vec<u32> = Vec::new();
    v.extend([x]);
    let k = other(); // ARRAYED
    let t = v[0] + k;
    std::hint::black_box(&x);
    t
}

fn iterated(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let a = [x];
    let mut v: Vec<u32> = Vec::new();
    v.extend(a.iter());
    let k = other(); // ITERATED
    let t = v[0] + k;
    std::hint::black_box(&x);
    t
}

fn entered(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut m: HashMap<u32, u32> = HashMap::new();
    m.entry(1).or_insert(x);
    let k = other(); // ENTERED
    let t = m[&1] + k;
    std::hint::black_box(&x);
    t
}

fn each(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut v = vec![0u32];
    v.iter_mut().for_each(|r| *r = x);
    let k = other(); // EACH
    let t = v[0] + k;
    std::hint::black_box(&x);
    t
}

fn swapped(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut a = x;
    let mut b = 0u32;
    std::mem::swap(&mut a, &mut b);
    a = 0;
    let k = other(); // SWAPPED
    let t = a + b + k;
    std::hint::black_box(&x);
    t
}

fn celled(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let c = Cell::new(0u32);
    c.set(x);
    let k = other(); // CELLED
    let t = c.get() + k;
    std::hint::black_box(&x);
    t
}

fn refcelled(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let c = RefCell::new(0u32);
    *c.borrow_mut() = x;
    let k = other(); // REFCELLED
    let t = *c.borrow() + k;
    std::hint::black_box(&x);
    t
}

fn taken(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut a = x;
    let b = std::mem::take(&mut a);
    let k = other(); // TAKEN
    let t = a + b + k;
    std::hint::black_box(&x);
    t
}

fn indexed(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut v = vec![0u32];
    *v.get_mut(0).unwrap() = x;
    let k = other(); // INDEXED
    let t = v[0] + k;
    std::hint::black_box(&x);
    t
}

fn main() {
    let x: u32 = env::args().nth(1).unwrap().parse().unwrap();
    println!("ARRAYED {}", arrayed(x));
    println!("ITERATED {}", iterated(x));
    println!("ENTERED {}", entered(x));
    println!("EACH {}", each(x));
    println!("SWAPPED {}", swapped(x));
    println!("CELLED {}", celled(x));
    println!("REFCELLED {}", refcelled(x));
    println!("TAKEN {}", taken(x));
    println!("INDEXED {}", indexed(x));
}
"""
SLICED = """// MIR for `sliced` after PreCodegen

fn sliced(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:9:11: 9:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:9:24: 9:27
    let _2: u32;                         // in scope 0 at src/main.rs:10:9: 10:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:10:36: 10:39
    let mut _5: u32;                     // in scope 0 at src/main.rs:11:14: 11:15
    let _7: ();                          // in scope 0 at src/main.rs:13:5: 13:28
    let mut _8: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:13:5: 13:6
    let mut _9: &[u32];                  // in scope 0 at src/main.rs:13:25: 13:27
    let mut _10: &[u32; 1];              // in scope 0 at src/main.rs:13:25: 13:27
    let _11: &[u32; 1];                  // in scope 0 at src/main.rs:13:25: 13:27
    let mut _14: u32;                    // in scope 0 at src/main.rs:15:13: 15:17
    let mut _15: &u32;                   // in scope 0 at src/main.rs:15:14: 15:17
    let mut _16: &std::vec::Vec<u32>;    // in scope 0 at src/main.rs:15:13: 15:14
    let mut _17: u32;                    // in scope 0 at src/main.rs:15:20: 15:21
    let mut _18: (u32, bool);            // in scope 0 at src/main.rs:15:13: 15:21
    let _19: &u32;                       // in scope 0 at src/main.rs:16:5: 16:29
    let mut _20: &u32;                   // in scope 0 at src/main.rs:16:26: 16:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:10:9: 10:10
        let _4: [u32; 1];                // in scope 1 at src/main.rs:11:9: 11:10
        scope 2 {
            debug a => _4;               // in scope 2 at src/main.rs:11:9: 11:10
            let mut _6: std::vec::Vec<u32>; // in scope 2 at src/main.rs:12:9: 12:14
            scope 3 {
                debug v => _6;           // in scope 3 at src/main.rs:12:9: 12:14
                let _12: u32;            // in scope 3 at src/main.rs:14:9: 14:10
                scope 4 {
                    debug k => _12;      // in scope 4 at src/main.rs:14:9: 14:10
                    let _13: u32;        // in scope 4 at src/main.rs:15:9: 15:10
                    scope 5 {
                        debug t => _13;  // in scope 5 at src/main.rs:15:9: 15:10
                    }
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:10:9: 10:10
        StorageLive(_3);                 // scope 0 at src/main.rs:10:36: 10:39
        _3 = copy _1;                    // scope 0 at src/main.rs:10:36: 10:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:10:13: 10:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:10:39: 10:40
        StorageLive(_4);                 // scope 1 at src/main.rs:11:9: 11:10
        StorageLive(_5);                 // scope 1 at src/main.rs:11:14: 11:15
        _5 = copy _2;                    // scope 1 at src/main.rs:11:14: 11:15
        _4 = [move _5];                  // scope 1 at src/main.rs:11:13: 11:16
        StorageDead(_5);                 // scope 1 at src/main.rs:11:15: 11:16
        StorageLive(_6);                 // scope 2 at src/main.rs:12:9: 12:14
        _6 = Vec::<u32>::new() -> [return: bb2, unwind continue]; // scope 2 at src/main.rs:12:27: 12:37
    }

    bb2: {
        StorageLive(_7);                 // scope 3 at src/main.rs:13:5: 13:28
        StorageLive(_8);                 // scope 3 at src/main.rs:13:5: 13:6
        _8 = &mut _6;                    // scope 3 at src/main.rs:13:5: 13:6
        StorageLive(_9);                 // scope 3 at src/main.rs:13:25: 13:27
        StorageLive(_10);                // scope 3 at src/main.rs:13:25: 13:27
        StorageLive(_11);                // scope 3 at src/main.rs:13:25: 13:27
        _11 = &_4;                       // scope 3 at src/main.rs:13:25: 13:27
        _10 = &(*_11);                   // scope 3 at src/main.rs:13:25: 13:27
        _9 = move _10 as &[u32] (PointerCoercion(Unsize, Implicit)); // scope 3 at src/main.rs:13:25: 13:27
        StorageDead(_10);                // scope 3 at src/main.rs:13:26: 13:27
        _7 = Vec::<u32>::extend_from_slice(move _8, move _9) -> [return: bb3, unwind: bb9]; // scope 3 at src/main.rs:13:5: 13:28
    }

    bb3: {
        StorageDead(_9);                 // scope 3 at src/main.rs:13:27: 13:28
        StorageDead(_8);                 // scope 3 at src/main.rs:13:27: 13:28
        StorageDead(_11);                // scope 3 at src/main.rs:13:28: 13:29
        StorageDead(_7);                 // scope 3 at src/main.rs:13:28: 13:29
        StorageLive(_12);                // scope 3 at src/main.rs:14:9: 14:10
        _12 = other() -> [return: bb4, unwind: bb9]; // scope 3 at src/main.rs:14:13: 14:20
    }

    bb4: {
        StorageLive(_13);                // scope 4 at src/main.rs:15:9: 15:10
        StorageLive(_14);                // scope 4 at src/main.rs:15:13: 15:17
        StorageLive(_15);                // scope 4 at src/main.rs:15:14: 15:17
        StorageLive(_16);                // scope 4 at src/main.rs:15:13: 15:14
        _16 = &_6;                       // scope 4 at src/main.rs:15:13: 15:14
        _15 = <Vec<u32> as Index<usize>>::index(move _16, const 0_usize) -> [return: bb5, unwind: bb9]; // scope 4 at src/main.rs:15:14: 15:17
    }

    bb5: {
        _14 = copy (*_15);               // scope 4 at src/main.rs:15:13: 15:17
        StorageDead(_16);                // scope 4 at src/main.rs:15:16: 15:17
        StorageLive(_17);                // scope 4 at src/main.rs:15:20: 15:21
        _17 = copy _12;                  // scope 4 at src/main.rs:15:20: 15:21
        _18 = AddWithOverflow(copy _14, copy _17); // scope 4 at src/main.rs:15:13: 15:21
        assert(!move (_18.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _17) -> [success: bb6, unwind: bb9]; // scope 4 at src/main.rs:15:13: 15:21
    }

    bb6: {
        _13 = move (_18.0: u32);         // scope 4 at src/main.rs:15:13: 15:21
        StorageDead(_17);                // scope 4 at src/main.rs:15:20: 15:21
        StorageDead(_14);                // scope 4 at src/main.rs:15:20: 15:21
        StorageDead(_15);                // scope 4 at src/main.rs:15:21: 15:22
        StorageLive(_19);                // scope 5 at src/main.rs:16:5: 16:29
        StorageLive(_20);                // scope 5 at src/main.rs:16:26: 16:28
        _20 = &_2;                       // scope 5 at src/main.rs:16:26: 16:28
        _19 = std::hint::black_box::<&u32>(move _20) -> [return: bb7, unwind: bb9]; // scope 5 at src/main.rs:16:5: 16:29
    }

    bb7: {
        StorageDead(_20);                // scope 5 at src/main.rs:16:28: 16:29
        StorageDead(_19);                // scope 5 at src/main.rs:16:29: 16:30
        _0 = copy _13;                   // scope 5 at src/main.rs:17:5: 17:6
        StorageDead(_13);                // scope 4 at src/main.rs:18:1: 18:2
        StorageDead(_12);                // scope 3 at src/main.rs:18:1: 18:2
        drop(_6) -> [return: bb8, unwind: bb10]; // scope 2 at src/main.rs:18:1: 18:2
    }

    bb8: {
        StorageDead(_6);                 // scope 2 at src/main.rs:18:1: 18:2
        StorageDead(_4);                 // scope 1 at src/main.rs:18:1: 18:2
        StorageDead(_2);                 // scope 0 at src/main.rs:18:1: 18:2
        return;                          // scope 0 at src/main.rs:18:2: 18:2
    }

    bb9 (cleanup): {
        drop(_6) -> [return: bb10, unwind terminate(cleanup)]; // scope 2 at src/main.rs:18:1: 18:2
    }

    bb10 (cleanup): {
        resume;                          // scope 0 at src/main.rs:9:1: 18:2
    }
}
""".replace("{ALLOC}", ALLOC)
SLICE_LITERAL = """// MIR for `literal` after PreCodegen

fn literal(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:20:12: 20:15
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:20:25: 20:28
    let _2: u32;                         // in scope 0 at src/main.rs:21:9: 21:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:21:36: 21:39
    let _5: ();                          // in scope 0 at src/main.rs:23:5: 23:30
    let mut _6: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:23:5: 23:6
    let mut _7: &[u32];                  // in scope 0 at src/main.rs:23:25: 23:29
    let mut _8: &[u32; 1];               // in scope 0 at src/main.rs:23:25: 23:29
    let _9: &[u32; 1];                   // in scope 0 at src/main.rs:23:25: 23:29
    let _10: [u32; 1];                   // in scope 0 at src/main.rs:23:26: 23:29
    let mut _11: u32;                    // in scope 0 at src/main.rs:23:27: 23:28
    let mut _14: u32;                    // in scope 0 at src/main.rs:25:13: 25:17
    let mut _15: &u32;                   // in scope 0 at src/main.rs:25:14: 25:17
    let mut _16: &std::vec::Vec<u32>;    // in scope 0 at src/main.rs:25:13: 25:14
    let mut _17: u32;                    // in scope 0 at src/main.rs:25:20: 25:21
    let mut _18: (u32, bool);            // in scope 0 at src/main.rs:25:13: 25:21
    let _19: &u32;                       // in scope 0 at src/main.rs:26:5: 26:29
    let mut _20: &u32;                   // in scope 0 at src/main.rs:26:26: 26:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:21:9: 21:10
        let mut _4: std::vec::Vec<u32>;  // in scope 1 at src/main.rs:22:9: 22:14
        scope 2 {
            debug v => _4;               // in scope 2 at src/main.rs:22:9: 22:14
            let _12: u32;                // in scope 2 at src/main.rs:24:9: 24:10
            scope 3 {
                debug k => _12;          // in scope 3 at src/main.rs:24:9: 24:10
                let _13: u32;            // in scope 3 at src/main.rs:25:9: 25:10
                scope 4 {
                    debug t => _13;      // in scope 4 at src/main.rs:25:9: 25:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:21:9: 21:10
        StorageLive(_3);                 // scope 0 at src/main.rs:21:36: 21:39
        _3 = copy _1;                    // scope 0 at src/main.rs:21:36: 21:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:21:13: 21:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:21:39: 21:40
        StorageLive(_4);                 // scope 1 at src/main.rs:22:9: 22:14
        _4 = Vec::<u32>::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:22:27: 22:37
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:23:5: 23:30
        StorageLive(_6);                 // scope 2 at src/main.rs:23:5: 23:6
        _6 = &mut _4;                    // scope 2 at src/main.rs:23:5: 23:6
        StorageLive(_7);                 // scope 2 at src/main.rs:23:25: 23:29
        StorageLive(_8);                 // scope 2 at src/main.rs:23:25: 23:29
        StorageLive(_9);                 // scope 2 at src/main.rs:23:25: 23:29
        StorageLive(_10);                // scope 2 at src/main.rs:23:26: 23:29
        StorageLive(_11);                // scope 2 at src/main.rs:23:27: 23:28
        _11 = copy _2;                   // scope 2 at src/main.rs:23:27: 23:28
        _10 = [move _11];                // scope 2 at src/main.rs:23:26: 23:29
        StorageDead(_11);                // scope 2 at src/main.rs:23:28: 23:29
        _9 = &_10;                       // scope 2 at src/main.rs:23:25: 23:29
        _8 = &(*_9);                     // scope 2 at src/main.rs:23:25: 23:29
        _7 = move _8 as &[u32] (PointerCoercion(Unsize, Implicit)); // scope 2 at src/main.rs:23:25: 23:29
        StorageDead(_8);                 // scope 2 at src/main.rs:23:28: 23:29
        _5 = Vec::<u32>::extend_from_slice(move _6, move _7) -> [return: bb3, unwind: bb9]; // scope 2 at src/main.rs:23:5: 23:30
    }

    bb3: {
        StorageDead(_7);                 // scope 2 at src/main.rs:23:29: 23:30
        StorageDead(_6);                 // scope 2 at src/main.rs:23:29: 23:30
        StorageDead(_10);                // scope 2 at src/main.rs:23:30: 23:31
        StorageDead(_9);                 // scope 2 at src/main.rs:23:30: 23:31
        StorageDead(_5);                 // scope 2 at src/main.rs:23:30: 23:31
        StorageLive(_12);                // scope 2 at src/main.rs:24:9: 24:10
        _12 = other() -> [return: bb4, unwind: bb9]; // scope 2 at src/main.rs:24:13: 24:20
    }

    bb4: {
        StorageLive(_13);                // scope 3 at src/main.rs:25:9: 25:10
        StorageLive(_14);                // scope 3 at src/main.rs:25:13: 25:17
        StorageLive(_15);                // scope 3 at src/main.rs:25:14: 25:17
        StorageLive(_16);                // scope 3 at src/main.rs:25:13: 25:14
        _16 = &_4;                       // scope 3 at src/main.rs:25:13: 25:14
        _15 = <Vec<u32> as Index<usize>>::index(move _16, const 0_usize) -> [return: bb5, unwind: bb9]; // scope 3 at src/main.rs:25:14: 25:17
    }

    bb5: {
        _14 = copy (*_15);               // scope 3 at src/main.rs:25:13: 25:17
        StorageDead(_16);                // scope 3 at src/main.rs:25:16: 25:17
        StorageLive(_17);                // scope 3 at src/main.rs:25:20: 25:21
        _17 = copy _12;                  // scope 3 at src/main.rs:25:20: 25:21
        _18 = AddWithOverflow(copy _14, copy _17); // scope 3 at src/main.rs:25:13: 25:21
        assert(!move (_18.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _17) -> [success: bb6, unwind: bb9]; // scope 3 at src/main.rs:25:13: 25:21
    }

    bb6: {
        _13 = move (_18.0: u32);         // scope 3 at src/main.rs:25:13: 25:21
        StorageDead(_17);                // scope 3 at src/main.rs:25:20: 25:21
        StorageDead(_14);                // scope 3 at src/main.rs:25:20: 25:21
        StorageDead(_15);                // scope 3 at src/main.rs:25:21: 25:22
        StorageLive(_19);                // scope 4 at src/main.rs:26:5: 26:29
        StorageLive(_20);                // scope 4 at src/main.rs:26:26: 26:28
        _20 = &_2;                       // scope 4 at src/main.rs:26:26: 26:28
        _19 = std::hint::black_box::<&u32>(move _20) -> [return: bb7, unwind: bb9]; // scope 4 at src/main.rs:26:5: 26:29
    }

    bb7: {
        StorageDead(_20);                // scope 4 at src/main.rs:26:28: 26:29
        StorageDead(_19);                // scope 4 at src/main.rs:26:29: 26:30
        _0 = copy _13;                   // scope 4 at src/main.rs:27:5: 27:6
        StorageDead(_13);                // scope 3 at src/main.rs:28:1: 28:2
        StorageDead(_12);                // scope 2 at src/main.rs:28:1: 28:2
        drop(_4) -> [return: bb8, unwind: bb10]; // scope 1 at src/main.rs:28:1: 28:2
    }

    bb8: {
        StorageDead(_4);                 // scope 1 at src/main.rs:28:1: 28:2
        StorageDead(_2);                 // scope 0 at src/main.rs:28:1: 28:2
        return;                          // scope 0 at src/main.rs:28:2: 28:2
    }

    bb9 (cleanup): {
        drop(_4) -> [return: bb10, unwind terminate(cleanup)]; // scope 1 at src/main.rs:28:1: 28:2
    }

    bb10 (cleanup): {
        resume;                          // scope 0 at src/main.rs:20:1: 28:2
    }
}
""".replace("{ALLOC}", ALLOC)
LAST = """// MIR for `last` after PreCodegen

fn last(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:30:9: 30:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:30:22: 30:25
    let _2: u32;                         // in scope 0 at src/main.rs:31:9: 31:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:31:36: 31:39
    let mut _5: std::boxed::Box<std::mem::MaybeUninit<[u32; 1]>>; // in scope 0 at {ALLOC}:59:13: 59:96
    let mut _6: std::boxed::Box<std::mem::MaybeUninit<[u32; 1]>>; // in scope 0 at {ALLOC}:59:52: 59:84
    let mut _8: std::option::Option<&mut u32>; // in scope 0 at src/main.rs:33:13: 33:25
    let mut _9: &mut [u32];              // in scope 0 at src/main.rs:33:13: 33:14
    let mut _10: &mut [u32];             // in scope 0 at src/main.rs:33:13: 33:25
    let mut _11: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:33:13: 33:14
    let mut _12: u32;                    // in scope 0 at src/main.rs:34:10: 34:11
    let mut _15: u32;                    // in scope 0 at src/main.rs:36:13: 36:15
    let mut _16: u32;                    // in scope 0 at src/main.rs:36:18: 36:19
    let mut _17: (u32, bool);            // in scope 0 at src/main.rs:36:13: 36:19
    let _18: &u32;                       // in scope 0 at src/main.rs:37:5: 37:29
    let mut _19: &u32;                   // in scope 0 at src/main.rs:37:26: 37:28
    let mut _20: *const std::mem::MaybeUninit<[u32; 1]>; // in scope 0 at {ALLOC}:59:52: 59:84
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:31:9: 31:10
        let mut _4: std::vec::Vec<u32>;  // in scope 1 at src/main.rs:32:9: 32:14
        let mut _21: *const ();          // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _22: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _23: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _24: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _25: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _26: *const ();          // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _27: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _28: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _29: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _30: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _31: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        scope 2 {
            debug v => _4;               // in scope 2 at src/main.rs:32:9: 32:14
            let _7: &mut u32;            // in scope 2 at src/main.rs:33:9: 33:10
            scope 3 {
                debug r => _7;           // in scope 3 at src/main.rs:33:9: 33:10
                let _13: u32;            // in scope 3 at src/main.rs:35:9: 35:10
                scope 4 {
                    debug k => _13;      // in scope 4 at src/main.rs:35:9: 35:10
                    let _14: u32;        // in scope 4 at src/main.rs:36:9: 36:10
                    scope 5 {
                        debug t => _14;  // in scope 5 at src/main.rs:36:9: 36:10
                    }
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:31:9: 31:10
        StorageLive(_3);                 // scope 0 at src/main.rs:31:36: 31:39
        _3 = copy _1;                    // scope 0 at src/main.rs:31:36: 31:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:31:13: 31:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:31:39: 31:40
        StorageLive(_4);                 // scope 1 at src/main.rs:32:9: 32:14
        StorageLive(_5);                 // scope 1 at {ALLOC}:59:13: 59:96
        StorageLive(_6);                 // scope 1 at {ALLOC}:59:52: 59:84
        _6 = Box::<[u32; 1]>::new_uninit() -> [return: bb2, unwind continue]; // scope 1 at {ALLOC}:59:52: 59:84
    }

    bb2: {
        _20 = copy ((_6.0: std::ptr::Unique<std::mem::MaybeUninit<[u32; 1]>>).0: std::ptr::NonNull<std::mem::MaybeUninit<[u32; 1]>>) as *const std::mem::MaybeUninit<[u32; 1]> (Transmute); // scope 1 at {ALLOC}:59:86: 59:95
        _21 = copy _20 as *const () (PtrToPtr); // scope 1 at {ALLOC}:59:86: 59:95
        _22 = copy _21 as usize (Transmute); // scope 1 at {ALLOC}:59:86: 59:95
        _23 = Sub(const <std::mem::MaybeUninit<[u32; 1]> as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 1 at {ALLOC}:59:86: 59:95
        _24 = BitAnd(copy _22, copy _23); // scope 1 at {ALLOC}:59:86: 59:95
        _25 = Eq(copy _24, const 0_usize); // scope 1 at {ALLOC}:59:86: 59:95
        assert(copy _25, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <std::mem::MaybeUninit<[u32; 1]> as std::mem::SizedTypeProperties>::ALIGN, copy _22) -> [success: bb13, unwind unreachable]; // scope 1 at {ALLOC}:59:86: 59:95
    }

    bb3: {
        StorageDead(_5);                 // scope 1 at {ALLOC}:60:9: 60:10
        StorageLive(_7);                 // scope 2 at src/main.rs:33:9: 33:10
        StorageLive(_8);                 // scope 2 at src/main.rs:33:13: 33:25
        StorageLive(_9);                 // scope 2 at src/main.rs:33:13: 33:14
        StorageLive(_10);                // scope 2 at src/main.rs:33:13: 33:25
        StorageLive(_11);                // scope 2 at src/main.rs:33:13: 33:14
        _11 = &mut _4;                   // scope 2 at src/main.rs:33:13: 33:14
        _10 = <Vec<u32> as DerefMut>::deref_mut(move _11) -> [return: bb4, unwind: bb11]; // scope 2 at src/main.rs:33:13: 33:25
    }

    bb4: {
        _9 = &mut (*_10);                // scope 2 at src/main.rs:33:13: 33:14
        StorageDead(_11);                // scope 2 at src/main.rs:33:13: 33:14
        _8 = core::slice::<impl [u32]>::last_mut(move _9) -> [return: bb5, unwind: bb11]; // scope 2 at src/main.rs:33:13: 33:25
    }

    bb5: {
        StorageDead(_9);                 // scope 2 at src/main.rs:33:24: 33:25
        _7 = Option::<&mut u32>::unwrap(move _8) -> [return: bb6, unwind: bb11]; // scope 2 at src/main.rs:33:13: 33:34
    }

    bb6: {
        StorageDead(_8);                 // scope 2 at src/main.rs:33:33: 33:34
        StorageDead(_10);                // scope 2 at src/main.rs:33:34: 33:35
        StorageLive(_12);                // scope 3 at src/main.rs:34:10: 34:11
        _12 = copy _2;                   // scope 3 at src/main.rs:34:10: 34:11
        (*_7) = move _12;                // scope 3 at src/main.rs:34:5: 34:11
        StorageDead(_12);                // scope 3 at src/main.rs:34:10: 34:11
        StorageLive(_13);                // scope 3 at src/main.rs:35:9: 35:10
        _13 = other() -> [return: bb7, unwind: bb11]; // scope 3 at src/main.rs:35:13: 35:20
    }

    bb7: {
        StorageLive(_14);                // scope 4 at src/main.rs:36:9: 36:10
        StorageLive(_15);                // scope 4 at src/main.rs:36:13: 36:15
        _15 = copy (*_7);                // scope 4 at src/main.rs:36:13: 36:15
        StorageLive(_16);                // scope 4 at src/main.rs:36:18: 36:19
        _16 = copy _13;                  // scope 4 at src/main.rs:36:18: 36:19
        _17 = AddWithOverflow(copy _15, copy _16); // scope 4 at src/main.rs:36:13: 36:19
        assert(!move (_17.1: bool), "attempt to compute `{} + {}`, which would overflow", move _15, move _16) -> [success: bb8, unwind: bb11]; // scope 4 at src/main.rs:36:13: 36:19
    }

    bb8: {
        _14 = move (_17.0: u32);         // scope 4 at src/main.rs:36:13: 36:19
        StorageDead(_16);                // scope 4 at src/main.rs:36:18: 36:19
        StorageDead(_15);                // scope 4 at src/main.rs:36:18: 36:19
        StorageLive(_18);                // scope 5 at src/main.rs:37:5: 37:29
        StorageLive(_19);                // scope 5 at src/main.rs:37:26: 37:28
        _19 = &_2;                       // scope 5 at src/main.rs:37:26: 37:28
        _18 = std::hint::black_box::<&u32>(move _19) -> [return: bb9, unwind: bb11]; // scope 5 at src/main.rs:37:5: 37:29
    }

    bb9: {
        StorageDead(_19);                // scope 5 at src/main.rs:37:28: 37:29
        StorageDead(_18);                // scope 5 at src/main.rs:37:29: 37:30
        _0 = copy _14;                   // scope 5 at src/main.rs:38:5: 38:6
        StorageDead(_14);                // scope 4 at src/main.rs:39:1: 39:2
        StorageDead(_13);                // scope 3 at src/main.rs:39:1: 39:2
        StorageDead(_7);                 // scope 2 at src/main.rs:39:1: 39:2
        drop(_4) -> [return: bb10, unwind: bb12]; // scope 1 at src/main.rs:39:1: 39:2
    }

    bb10: {
        StorageDead(_4);                 // scope 1 at src/main.rs:39:1: 39:2
        StorageDead(_2);                 // scope 0 at src/main.rs:39:1: 39:2
        return;                          // scope 0 at src/main.rs:39:2: 39:2
    }

    bb11 (cleanup): {
        drop(_4) -> [return: bb12, unwind terminate(cleanup)]; // scope 1 at src/main.rs:39:1: 39:2
    }

    bb12 (cleanup): {
        resume;                          // scope 0 at src/main.rs:30:1: 39:2
    }

    bb13: {
        _26 = copy _20 as *const () (PtrToPtr); // scope 1 at {ALLOC}:59:86: 59:95
        _27 = copy _26 as usize (Transmute); // scope 1 at {ALLOC}:59:86: 59:95
        _28 = Ne(const <std::mem::MaybeUninit<[u32; 1]> as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 1 at {ALLOC}:59:86: 59:95
        _29 = Eq(copy _27, const 0_usize); // scope 1 at {ALLOC}:59:86: 59:95
        _30 = BitAnd(copy _29, copy _28); // scope 1 at {ALLOC}:59:86: 59:95
        _31 = Not(copy _30);             // scope 1 at {ALLOC}:59:86: 59:95
        assert(copy _31, "null pointer dereference occurred") -> [success: bb14, unwind unreachable]; // scope 1 at {ALLOC}:59:86: 59:95
    }

    bb14: {
        ((((*_20).1: std::mem::ManuallyDrop<[u32; 1]>).0: std::mem::MaybeDangling<[u32; 1]>).0: [u32; 1]) = [const 0_u32]; // scope 1 at {ALLOC}:59:86: 59:95
        _5 = move _6;                    // scope 1 at {ALLOC}:59:13: 59:96
        StorageDead(_6);                 // scope 1 at {ALLOC}:59:95: 59:96
        _4 = std::boxed::box_assume_init_into_vec_unsafe::<u32, 1>(move _5) -> [return: bb3, unwind: bb12]; // scope 1 at {ALLOC}:58:9: 60:10
    }
}
""".replace("{ALLOC}", ALLOC)
ENTERED = """// MIR for `entered` after PreCodegen

fn entered(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:31:12: 31:15
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:31:25: 31:28
    let _2: u32;                         // in scope 0 at src/main.rs:32:9: 32:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:32:36: 32:39
    let _5: &mut u32;                    // in scope 0 at src/main.rs:34:5: 34:28
    let mut _6: std::collections::hash_map::Entry<'_, u32, u32>; // in scope 0 at src/main.rs:34:5: 34:15
    let mut _7: &mut std::collections::HashMap<u32, u32>; // in scope 0 at src/main.rs:34:5: 34:6
    let mut _8: u32;                     // in scope 0 at src/main.rs:34:26: 34:27
    let mut _11: u32;                    // in scope 0 at src/main.rs:36:13: 36:18
    let mut _12: &u32;                   // in scope 0 at src/main.rs:36:14: 36:18
    let mut _13: &std::collections::HashMap<u32, u32>; // in scope 0 at src/main.rs:36:13: 36:14
    let mut _14: &u32;                   // in scope 0 at src/main.rs:36:15: 36:17
    let _15: &u32;                       // in scope 0 at src/main.rs:36:15: 36:17
    let _16: u32;                        // in scope 0 at src/main.rs:36:16: 36:17
    let mut _17: u32;                    // in scope 0 at src/main.rs:36:21: 36:22
    let mut _18: (u32, bool);            // in scope 0 at src/main.rs:36:13: 36:22
    let _19: &u32;                       // in scope 0 at src/main.rs:37:5: 37:29
    let mut _20: &u32;                   // in scope 0 at src/main.rs:37:26: 37:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:32:9: 32:10
        let mut _4: std::collections::HashMap<u32, u32>; // in scope 1 at src/main.rs:33:9: 33:14
        scope 2 {
            debug m => _4;               // in scope 2 at src/main.rs:33:9: 33:14
            let _9: u32;                 // in scope 2 at src/main.rs:35:9: 35:10
            scope 3 {
                debug k => _9;           // in scope 3 at src/main.rs:35:9: 35:10
                let _10: u32;            // in scope 3 at src/main.rs:36:9: 36:10
                let mut _21: &u32;       // in scope 3 at src/main.rs:36:15: 36:17
                scope 4 {
                    debug t => _10;      // in scope 4 at src/main.rs:36:9: 36:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:32:9: 32:10
        StorageLive(_3);                 // scope 0 at src/main.rs:32:36: 32:39
        _3 = copy _1;                    // scope 0 at src/main.rs:32:36: 32:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:32:13: 32:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:32:39: 32:40
        StorageLive(_4);                 // scope 1 at src/main.rs:33:9: 33:14
        _4 = HashMap::<u32, u32>::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:33:36: 33:50
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:34:5: 34:28
        StorageLive(_6);                 // scope 2 at src/main.rs:34:5: 34:15
        StorageLive(_7);                 // scope 2 at src/main.rs:34:5: 34:6
        _7 = &mut _4;                    // scope 2 at src/main.rs:34:5: 34:6
        _6 = HashMap::<u32, u32>::entry(move _7, const 1_u32) -> [return: bb3, unwind: bb10]; // scope 2 at src/main.rs:34:5: 34:15
    }

    bb3: {
        StorageDead(_7);                 // scope 2 at src/main.rs:34:14: 34:15
        StorageLive(_8);                 // scope 2 at src/main.rs:34:26: 34:27
        _8 = copy _2;                    // scope 2 at src/main.rs:34:26: 34:27
        _5 = std::collections::hash_map::Entry::<'_, u32, u32>::or_insert(move _6, move _8) -> [return: bb4, unwind: bb10]; // scope 2 at src/main.rs:34:5: 34:28
    }

    bb4: {
        StorageDead(_8);                 // scope 2 at src/main.rs:34:27: 34:28
        StorageDead(_6);                 // scope 2 at src/main.rs:34:27: 34:28
        StorageDead(_5);                 // scope 2 at src/main.rs:34:28: 34:29
        StorageLive(_9);                 // scope 2 at src/main.rs:35:9: 35:10
        _9 = other() -> [return: bb5, unwind: bb10]; // scope 2 at src/main.rs:35:13: 35:20
    }

    bb5: {
        StorageLive(_10);                // scope 3 at src/main.rs:36:9: 36:10
        StorageLive(_11);                // scope 3 at src/main.rs:36:13: 36:18
        StorageLive(_12);                // scope 3 at src/main.rs:36:14: 36:18
        StorageLive(_13);                // scope 3 at src/main.rs:36:13: 36:14
        _13 = &_4;                       // scope 3 at src/main.rs:36:13: 36:14
        StorageLive(_14);                // scope 3 at src/main.rs:36:15: 36:17
        StorageLive(_15);                // scope 3 at src/main.rs:36:15: 36:17
        _21 = const entered::promoted[0]; // scope 3 at src/main.rs:36:15: 36:17
        _15 = &(*_21);                   // scope 3 at src/main.rs:36:15: 36:17
        _14 = &(*_15);                   // scope 3 at src/main.rs:36:15: 36:17
        _12 = <HashMap<u32, u32> as Index<&u32>>::index(move _13, move _14) -> [return: bb6, unwind: bb10]; // scope 3 at src/main.rs:36:14: 36:18
    }

    bb6: {
        _11 = copy (*_12);               // scope 3 at src/main.rs:36:13: 36:18
        StorageDead(_14);                // scope 3 at src/main.rs:36:17: 36:18
        StorageDead(_13);                // scope 3 at src/main.rs:36:17: 36:18
        StorageLive(_17);                // scope 3 at src/main.rs:36:21: 36:22
        _17 = copy _9;                   // scope 3 at src/main.rs:36:21: 36:22
        _18 = AddWithOverflow(copy _11, copy _17); // scope 3 at src/main.rs:36:13: 36:22
        assert(!move (_18.1: bool), "attempt to compute `{} + {}`, which would overflow", move _11, move _17) -> [success: bb7, unwind: bb10]; // scope 3 at src/main.rs:36:13: 36:22
    }

    bb7: {
        _10 = move (_18.0: u32);         // scope 3 at src/main.rs:36:13: 36:22
        StorageDead(_17);                // scope 3 at src/main.rs:36:21: 36:22
        StorageDead(_11);                // scope 3 at src/main.rs:36:21: 36:22
        StorageDead(_15);                // scope 3 at src/main.rs:36:22: 36:23
        StorageDead(_12);                // scope 3 at src/main.rs:36:22: 36:23
        StorageLive(_19);                // scope 4 at src/main.rs:37:5: 37:29
        StorageLive(_20);                // scope 4 at src/main.rs:37:26: 37:28
        _20 = &_2;                       // scope 4 at src/main.rs:37:26: 37:28
        _19 = std::hint::black_box::<&u32>(move _20) -> [return: bb8, unwind: bb10]; // scope 4 at src/main.rs:37:5: 37:29
    }

    bb8: {
        StorageDead(_20);                // scope 4 at src/main.rs:37:28: 37:29
        StorageDead(_19);                // scope 4 at src/main.rs:37:29: 37:30
        _0 = copy _10;                   // scope 4 at src/main.rs:38:5: 38:6
        StorageDead(_10);                // scope 3 at src/main.rs:39:1: 39:2
        StorageDead(_9);                 // scope 2 at src/main.rs:39:1: 39:2
        drop(_4) -> [return: bb9, unwind: bb11]; // scope 1 at src/main.rs:39:1: 39:2
    }

    bb9: {
        StorageDead(_4);                 // scope 1 at src/main.rs:39:1: 39:2
        StorageDead(_2);                 // scope 0 at src/main.rs:39:1: 39:2
        return;                          // scope 0 at src/main.rs:39:2: 39:2
    }

    bb10 (cleanup): {
        drop(_4) -> [return: bb11, unwind terminate(cleanup)]; // scope 1 at src/main.rs:39:1: 39:2
    }

    bb11 (cleanup): {
        resume;                          // scope 0 at src/main.rs:31:1: 39:2
    }
}
""".replace("{ALLOC}", ALLOC)
EACH = """// MIR for `each` after PreCodegen

fn each(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:41:9: 41:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:41:22: 41:25
    let _2: u32;                         // in scope 0 at src/main.rs:42:9: 42:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:42:36: 42:39
    let mut _5: std::boxed::Box<std::mem::MaybeUninit<[u32; 1]>>; // in scope 0 at {ALLOC}:59:13: 59:96
    let mut _6: std::boxed::Box<std::mem::MaybeUninit<[u32; 1]>>; // in scope 0 at {ALLOC}:59:52: 59:84
    let _7: ();                          // in scope 0 at src/main.rs:44:5: 44:38
    let mut _8: std::slice::IterMut<'_, u32>; // in scope 0 at src/main.rs:44:5: 44:17
    let mut _9: &mut [u32];              // in scope 0 at src/main.rs:44:5: 44:6
    let mut _10: &mut [u32];             // in scope 0 at src/main.rs:44:5: 44:17
    let mut _11: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:44:5: 44:6
    let mut _12: {closure@src/main.rs:44:27: 44:30}; // in scope 0 at src/main.rs:44:27: 44:37
    let mut _13: &u32;                   // in scope 0 at src/main.rs:44:27: 44:37
    let mut _16: u32;                    // in scope 0 at src/main.rs:46:13: 46:17
    let mut _17: &u32;                   // in scope 0 at src/main.rs:46:14: 46:17
    let mut _18: &std::vec::Vec<u32>;    // in scope 0 at src/main.rs:46:13: 46:14
    let mut _19: u32;                    // in scope 0 at src/main.rs:46:20: 46:21
    let mut _20: (u32, bool);            // in scope 0 at src/main.rs:46:13: 46:21
    let _21: &u32;                       // in scope 0 at src/main.rs:47:5: 47:29
    let mut _22: &u32;                   // in scope 0 at src/main.rs:47:26: 47:28
    let mut _23: *const std::mem::MaybeUninit<[u32; 1]>; // in scope 0 at {ALLOC}:59:52: 59:84
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:42:9: 42:10
        let mut _4: std::vec::Vec<u32>;  // in scope 1 at src/main.rs:43:9: 43:14
        let mut _24: *const ();          // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _25: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _26: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _27: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _28: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _29: *const ();          // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _30: usize;              // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _31: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _32: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _33: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        let mut _34: bool;               // in scope 1 at {ALLOC}:59:86: 59:95
        scope 2 {
            debug v => _4;               // in scope 2 at src/main.rs:43:9: 43:14
            let _14: u32;                // in scope 2 at src/main.rs:45:9: 45:10
            scope 3 {
                debug k => _14;          // in scope 3 at src/main.rs:45:9: 45:10
                let _15: u32;            // in scope 3 at src/main.rs:46:9: 46:10
                scope 4 {
                    debug t => _15;      // in scope 4 at src/main.rs:46:9: 46:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:42:9: 42:10
        StorageLive(_3);                 // scope 0 at src/main.rs:42:36: 42:39
        _3 = copy _1;                    // scope 0 at src/main.rs:42:36: 42:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:42:13: 42:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:42:39: 42:40
        StorageLive(_4);                 // scope 1 at src/main.rs:43:9: 43:14
        StorageLive(_5);                 // scope 1 at {ALLOC}:59:13: 59:96
        StorageLive(_6);                 // scope 1 at {ALLOC}:59:52: 59:84
        _6 = Box::<[u32; 1]>::new_uninit() -> [return: bb2, unwind continue]; // scope 1 at {ALLOC}:59:52: 59:84
    }

    bb2: {
        _23 = copy ((_6.0: std::ptr::Unique<std::mem::MaybeUninit<[u32; 1]>>).0: std::ptr::NonNull<std::mem::MaybeUninit<[u32; 1]>>) as *const std::mem::MaybeUninit<[u32; 1]> (Transmute); // scope 1 at {ALLOC}:59:86: 59:95
        _24 = copy _23 as *const () (PtrToPtr); // scope 1 at {ALLOC}:59:86: 59:95
        _25 = copy _24 as usize (Transmute); // scope 1 at {ALLOC}:59:86: 59:95
        _26 = Sub(const <std::mem::MaybeUninit<[u32; 1]> as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 1 at {ALLOC}:59:86: 59:95
        _27 = BitAnd(copy _25, copy _26); // scope 1 at {ALLOC}:59:86: 59:95
        _28 = Eq(copy _27, const 0_usize); // scope 1 at {ALLOC}:59:86: 59:95
        assert(copy _28, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <std::mem::MaybeUninit<[u32; 1]> as std::mem::SizedTypeProperties>::ALIGN, copy _25) -> [success: bb14, unwind unreachable]; // scope 1 at {ALLOC}:59:86: 59:95
    }

    bb3: {
        StorageDead(_5);                 // scope 1 at {ALLOC}:60:9: 60:10
        StorageLive(_7);                 // scope 2 at src/main.rs:44:5: 44:38
        StorageLive(_8);                 // scope 2 at src/main.rs:44:5: 44:17
        StorageLive(_9);                 // scope 2 at src/main.rs:44:5: 44:6
        StorageLive(_10);                // scope 2 at src/main.rs:44:5: 44:17
        StorageLive(_11);                // scope 2 at src/main.rs:44:5: 44:6
        _11 = &mut _4;                   // scope 2 at src/main.rs:44:5: 44:6
        _10 = <Vec<u32> as DerefMut>::deref_mut(move _11) -> [return: bb4, unwind: bb12]; // scope 2 at src/main.rs:44:5: 44:17
    }

    bb4: {
        _9 = &mut (*_10);                // scope 2 at src/main.rs:44:5: 44:6
        StorageDead(_11);                // scope 2 at src/main.rs:44:5: 44:6
        _8 = core::slice::<impl [u32]>::iter_mut(move _9) -> [return: bb5, unwind: bb12]; // scope 2 at src/main.rs:44:5: 44:17
    }

    bb5: {
        StorageDead(_9);                 // scope 2 at src/main.rs:44:16: 44:17
        StorageLive(_12);                // scope 2 at src/main.rs:44:27: 44:37
        StorageLive(_13);                // scope 2 at src/main.rs:44:27: 44:37
        _13 = &_2;                       // scope 2 at src/main.rs:44:27: 44:37
        _12 = {closure@src/main.rs:44:27: 44:30} { x: move _13 }; // scope 2 at src/main.rs:44:27: 44:37
                                         // closure
                                         //     i16,
                                         //     Binder { value: extern "RustCall" fn((&'{erased} mut u32,)), bound_vars: [] },
                                         //     (&'{erased} u32,),
                                         // ]
        StorageDead(_13);                // scope 2 at src/main.rs:44:29: 44:30
        _7 = <std::slice::IterMut<'_, u32> as Iterator>::for_each::<{closure@src/main.rs:44:27: 44:30}>(move _8, move _12) -> [return: bb6, unwind: bb12]; // scope 2 at src/main.rs:44:5: 44:38
    }

    bb6: {
        StorageDead(_12);                // scope 2 at src/main.rs:44:37: 44:38
        StorageDead(_8);                 // scope 2 at src/main.rs:44:37: 44:38
        StorageDead(_10);                // scope 2 at src/main.rs:44:38: 44:39
        StorageDead(_7);                 // scope 2 at src/main.rs:44:38: 44:39
        StorageLive(_14);                // scope 2 at src/main.rs:45:9: 45:10
        _14 = other() -> [return: bb7, unwind: bb12]; // scope 2 at src/main.rs:45:13: 45:20
    }

    bb7: {
        StorageLive(_15);                // scope 3 at src/main.rs:46:9: 46:10
        StorageLive(_16);                // scope 3 at src/main.rs:46:13: 46:17
        StorageLive(_17);                // scope 3 at src/main.rs:46:14: 46:17
        StorageLive(_18);                // scope 3 at src/main.rs:46:13: 46:14
        _18 = &_4;                       // scope 3 at src/main.rs:46:13: 46:14
        _17 = <Vec<u32> as Index<usize>>::index(move _18, const 0_usize) -> [return: bb8, unwind: bb12]; // scope 3 at src/main.rs:46:14: 46:17
    }

    bb8: {
        _16 = copy (*_17);               // scope 3 at src/main.rs:46:13: 46:17
        StorageDead(_18);                // scope 3 at src/main.rs:46:16: 46:17
        StorageLive(_19);                // scope 3 at src/main.rs:46:20: 46:21
        _19 = copy _14;                  // scope 3 at src/main.rs:46:20: 46:21
        _20 = AddWithOverflow(copy _16, copy _19); // scope 3 at src/main.rs:46:13: 46:21
        assert(!move (_20.1: bool), "attempt to compute `{} + {}`, which would overflow", move _16, move _19) -> [success: bb9, unwind: bb12]; // scope 3 at src/main.rs:46:13: 46:21
    }

    bb9: {
        _15 = move (_20.0: u32);         // scope 3 at src/main.rs:46:13: 46:21
        StorageDead(_19);                // scope 3 at src/main.rs:46:20: 46:21
        StorageDead(_16);                // scope 3 at src/main.rs:46:20: 46:21
        StorageDead(_17);                // scope 3 at src/main.rs:46:21: 46:22
        StorageLive(_21);                // scope 4 at src/main.rs:47:5: 47:29
        StorageLive(_22);                // scope 4 at src/main.rs:47:26: 47:28
        _22 = &_2;                       // scope 4 at src/main.rs:47:26: 47:28
        _21 = std::hint::black_box::<&u32>(move _22) -> [return: bb10, unwind: bb12]; // scope 4 at src/main.rs:47:5: 47:29
    }

    bb10: {
        StorageDead(_22);                // scope 4 at src/main.rs:47:28: 47:29
        StorageDead(_21);                // scope 4 at src/main.rs:47:29: 47:30
        _0 = copy _15;                   // scope 4 at src/main.rs:48:5: 48:6
        StorageDead(_15);                // scope 3 at src/main.rs:49:1: 49:2
        StorageDead(_14);                // scope 2 at src/main.rs:49:1: 49:2
        drop(_4) -> [return: bb11, unwind: bb13]; // scope 1 at src/main.rs:49:1: 49:2
    }

    bb11: {
        StorageDead(_4);                 // scope 1 at src/main.rs:49:1: 49:2
        StorageDead(_2);                 // scope 0 at src/main.rs:49:1: 49:2
        return;                          // scope 0 at src/main.rs:49:2: 49:2
    }

    bb12 (cleanup): {
        drop(_4) -> [return: bb13, unwind terminate(cleanup)]; // scope 1 at src/main.rs:49:1: 49:2
    }

    bb13 (cleanup): {
        resume;                          // scope 0 at src/main.rs:41:1: 49:2
    }

    bb14: {
        _29 = copy _23 as *const () (PtrToPtr); // scope 1 at {ALLOC}:59:86: 59:95
        _30 = copy _29 as usize (Transmute); // scope 1 at {ALLOC}:59:86: 59:95
        _31 = Ne(const <std::mem::MaybeUninit<[u32; 1]> as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 1 at {ALLOC}:59:86: 59:95
        _32 = Eq(copy _30, const 0_usize); // scope 1 at {ALLOC}:59:86: 59:95
        _33 = BitAnd(copy _32, copy _31); // scope 1 at {ALLOC}:59:86: 59:95
        _34 = Not(copy _33);             // scope 1 at {ALLOC}:59:86: 59:95
        assert(copy _34, "null pointer dereference occurred") -> [success: bb15, unwind unreachable]; // scope 1 at {ALLOC}:59:86: 59:95
    }

    bb15: {
        ((((*_23).1: std::mem::ManuallyDrop<[u32; 1]>).0: std::mem::MaybeDangling<[u32; 1]>).0: [u32; 1]) = [const 0_u32]; // scope 1 at {ALLOC}:59:86: 59:95
        _5 = move _6;                    // scope 1 at {ALLOC}:59:13: 59:96
        StorageDead(_6);                 // scope 1 at {ALLOC}:59:95: 59:96
        _4 = std::boxed::box_assume_init_into_vec_unsafe::<u32, 1>(move _5) -> [return: bb3, unwind: bb13]; // scope 1 at {ALLOC}:58:9: 60:10
    }
}
""".replace("{ALLOC}", ALLOC)


def test_a_call_writing_through_a_destination_takes_what_points_at_a_copy():
    # `let a = [x]; v.extend_from_slice(&a);` and `v.extend_from_slice(&[x]);`: the
    # slice the call is given points at a copy of `x` (`a`, or the array `_10`), not at
    # `x`; `v` was left out, and `--set x=100` there left the output as it was.
    for text, name, line, v, statement in (
        (SLICED, "sliced", 14, 6, "_12 = other()"),
        (SLICE_LITERAL, "literal", 24, 4, "_12 = other()"),
    ):
        body = body_of(text)
        stop = place(body, statement)
        assert v in body.direct_copies_at(2, stop) and body.copy_undoes(v, 2, stop), name
        reason, _ = why(text, DESTINATIONS, name, line, statement)
        assert reason.startswith("the program's own `v` (at lines "), reason
        assert "may hold a copy of it that it goes on with" in reason, reason
    # Not where what it points at holds no copy: `let a = [7];`.
    body = body_of(SLICED.replace("_4 = [move _5];", "_4 = [const 7_u32];"))
    assert 6 not in body.direct_copies_at(2, place(body, "_12 = other()"))


def test_a_read_through_a_pointer_a_call_made_from_a_binding_uses_it():
    # `let r = v.last_mut().unwrap(); *r = x;` then `*r + k`: `v` holds the copy, and
    # `*r` reads it; `r` was made through `Option<&mut u32>`, which `_pointers_to` left.
    body = body_of(LAST)
    stop = place(body, "_13 = other()")
    assert {8, 7} <= body._pointers_to(4)
    assert 4 in body.direct_copies_at(2, stop) and body.copy_undoes(4, 2, stop)
    reason, _ = why(LAST, DESTINATIONS, "last", 35, "_13 = other()")
    assert reason.startswith(
        f"the program's own `v` (at line 34 and {ALLOC.split('/library/')[1]}:58) may hold a "
        "copy of it"
    ), reason


def test_a_standard_type_with_a_lifetime_may_be_written_through():
    # `m.entry(1).or_insert(x)` and `v.iter_mut().for_each(|r| *r = x)`: an `Entry` or an
    # `IterMut` holds a `&mut` its type does not show.
    from peeky.rust_mir import _may_write

    for kind, writes in (
        ("std::collections::hash_map::Entry<'_, u32, u32>", True),
        ("std::slice::IterMut<'_, u32>", True),
        ("std::cell::RefMut<'_, u32>", True),
        ("std::slice::Iter<'_, u32>", False),
        ("std::iter::Enumerate<std::slice::Iter<'_, u32>>", False),
        ("std::fmt::Arguments<'_>", False),
        ("&[core::fmt::rt::Argument<'_>; 2]", False),
        ("std::vec::Vec<u32>", False),
        ("&std::vec::Vec<u32>", False),
    ):
        assert _may_write(kind, ()) is writes, kind
    for text, name, line, owner, statement in (
        (ENTERED, "entered", 35, 4, "_9 = other()"),
        (EACH, "each", 45, 4, "_14 = other()"),
    ):
        body = body_of(text)
        stop = place(body, statement)
        assert owner in body.direct_copies_at(2, stop), name
        reason, _ = why(text, CELLS, name, line, statement)
        assert "may hold a copy of it that it goes on with" in reason, reason


# A standard view with a lifetime was taken to write through a
# `&mut` it may hold, whatever it was; `a.chunks_exact(1)` only reads. rustc 1.96.0's MIR
# of this crate's functions:
VIEWS = """use std::collections::BTreeMap;
use std::env;
use std::path::Path;

#[inline(never)]
fn other() -> u32 {
    1
}

#[inline(never)]
fn observe<I: Iterator>(it: I, x: u32) {
    std::hint::black_box((it.count(), x));
}

fn chunked(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let a = [7u32, 8];
    observe(a.chunks_exact(1), x);
    let k = other(); // CHUNKED
    let t = a[0] + k;
    std::hint::black_box(&x);
    t
}

fn rchunked(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let a = [7u32, 8];
    observe(a.rchunks(1), x);
    let k = other(); // RCHUNKED
    let t = a[0] + k;
    std::hint::black_box(&x);
    t
}

fn split(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let s = String::from("a b");
    observe(s.split_ascii_whitespace(), x);
    let k = other(); // SPLIT
    let t = s.len() as u32 + k;
    std::hint::black_box(&x);
    t
}

fn mapped(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut m: BTreeMap<u32, u32> = BTreeMap::new();
    m.insert(1, 7);
    observe(m.iter(), x);
    let k = other(); // BTREE
    let t = m[&1] + k;
    std::hint::black_box(&x);
    t
}

fn pathed(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let p = Path::new("/a/b").to_path_buf();
    observe(p.components(), x);
    let k = other(); // PATHED
    let t = p.as_os_str().len() as u32 + k;
    std::hint::black_box(&x);
    t
}

fn main() {
    let x: u32 = env::args().nth(1).unwrap().parse().unwrap();
    println!("CHUNKED {}", chunked(x));
    println!("RCHUNKED {}", rchunked(x));
    println!("SPLIT {}", split(x));
    println!("BTREE {}", mapped(x));
    println!("PATHED {}", pathed(x));
}
"""
CHUNKED = """// MIR for `chunked` after PreCodegen

fn chunked(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:15:12: 15:15
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:15:25: 15:28
    let _2: u32;                         // in scope 0 at src/main.rs:16:9: 16:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:16:36: 16:39
    let _5: ();                          // in scope 0 at src/main.rs:18:5: 18:34
    let mut _6: std::slice::ChunksExact<'_, u32>; // in scope 0 at src/main.rs:18:13: 18:30
    let mut _7: &[u32];                  // in scope 0 at src/main.rs:18:13: 18:14
    let mut _8: &[u32; 2];               // in scope 0 at src/main.rs:18:13: 18:14
    let mut _9: u32;                     // in scope 0 at src/main.rs:18:32: 18:33
    let mut _12: u32;                    // in scope 0 at src/main.rs:20:13: 20:17
    let _13: usize;                      // in scope 0 at src/main.rs:20:15: 20:16
    let mut _14: bool;                   // in scope 0 at src/main.rs:20:13: 20:17
    let mut _15: u32;                    // in scope 0 at src/main.rs:20:20: 20:21
    let mut _16: (u32, bool);            // in scope 0 at src/main.rs:20:13: 20:21
    let _17: &u32;                       // in scope 0 at src/main.rs:21:5: 21:29
    let mut _18: &u32;                   // in scope 0 at src/main.rs:21:26: 21:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:16:9: 16:10
        let _4: [u32; 2];                // in scope 1 at src/main.rs:17:9: 17:10
        scope 2 {
            debug a => _4;               // in scope 2 at src/main.rs:17:9: 17:10
            let _10: u32;                // in scope 2 at src/main.rs:19:9: 19:10
            scope 3 {
                debug k => _10;          // in scope 3 at src/main.rs:19:9: 19:10
                let _11: u32;            // in scope 3 at src/main.rs:20:9: 20:10
                scope 4 {
                    debug t => _11;      // in scope 4 at src/main.rs:20:9: 20:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:16:9: 16:10
        StorageLive(_3);                 // scope 0 at src/main.rs:16:36: 16:39
        _3 = copy _1;                    // scope 0 at src/main.rs:16:36: 16:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:16:13: 16:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:16:39: 16:40
        StorageLive(_4);                 // scope 1 at src/main.rs:17:9: 17:10
        _4 = [const 7_u32, const 8_u32]; // scope 1 at src/main.rs:17:13: 17:22
        StorageLive(_5);                 // scope 2 at src/main.rs:18:5: 18:34
        StorageLive(_6);                 // scope 2 at src/main.rs:18:13: 18:30
        StorageLive(_7);                 // scope 2 at src/main.rs:18:13: 18:14
        StorageLive(_8);                 // scope 2 at src/main.rs:18:13: 18:14
        _8 = &_4;                        // scope 2 at src/main.rs:18:13: 18:14
        _7 = move _8 as &[u32] (PointerCoercion(Unsize, Implicit)); // scope 2 at src/main.rs:18:13: 18:14
        StorageDead(_8);                 // scope 2 at src/main.rs:18:13: 18:14
        _6 = core::slice::<impl [u32]>::chunks_exact(move _7, const 1_usize) -> [return: bb2, unwind continue]; // scope 2 at src/main.rs:18:13: 18:30
    }

    bb2: {
        StorageDead(_7);                 // scope 2 at src/main.rs:18:29: 18:30
        StorageLive(_9);                 // scope 2 at src/main.rs:18:32: 18:33
        _9 = copy _2;                    // scope 2 at src/main.rs:18:32: 18:33
        _5 = observe::<ChunksExact<'_, u32>>(move _6, move _9) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:18:5: 18:34
    }

    bb3: {
        StorageDead(_9);                 // scope 2 at src/main.rs:18:33: 18:34
        StorageDead(_6);                 // scope 2 at src/main.rs:18:33: 18:34
        StorageDead(_5);                 // scope 2 at src/main.rs:18:34: 18:35
        StorageLive(_10);                // scope 2 at src/main.rs:19:9: 19:10
        _10 = other() -> [return: bb4, unwind continue]; // scope 2 at src/main.rs:19:13: 19:20
    }

    bb4: {
        StorageLive(_11);                // scope 3 at src/main.rs:20:9: 20:10
        StorageLive(_12);                // scope 3 at src/main.rs:20:13: 20:17
        StorageLive(_13);                // scope 3 at src/main.rs:20:15: 20:16
        _13 = const 0_usize;             // scope 3 at src/main.rs:20:15: 20:16
        _14 = Lt(copy _13, const 2_usize); // scope 3 at src/main.rs:20:13: 20:17
        assert(move _14, "index out of bounds: the length is {} but the index is {}", const 2_usize, copy _13) -> [success: bb5, unwind continue]; // scope 3 at src/main.rs:20:13: 20:17
    }

    bb5: {
        _12 = copy _4[_13];              // scope 3 at src/main.rs:20:13: 20:17
        StorageLive(_15);                // scope 3 at src/main.rs:20:20: 20:21
        _15 = copy _10;                  // scope 3 at src/main.rs:20:20: 20:21
        _16 = AddWithOverflow(copy _12, copy _15); // scope 3 at src/main.rs:20:13: 20:21
        assert(!move (_16.1: bool), "attempt to compute `{} + {}`, which would overflow", move _12, move _15) -> [success: bb6, unwind continue]; // scope 3 at src/main.rs:20:13: 20:21
    }

    bb6: {
        _11 = move (_16.0: u32);         // scope 3 at src/main.rs:20:13: 20:21
        StorageDead(_15);                // scope 3 at src/main.rs:20:20: 20:21
        StorageDead(_12);                // scope 3 at src/main.rs:20:20: 20:21
        StorageDead(_13);                // scope 3 at src/main.rs:20:21: 20:22
        StorageLive(_17);                // scope 4 at src/main.rs:21:5: 21:29
        StorageLive(_18);                // scope 4 at src/main.rs:21:26: 21:28
        _18 = &_2;                       // scope 4 at src/main.rs:21:26: 21:28
        _17 = std::hint::black_box::<&u32>(move _18) -> [return: bb7, unwind continue]; // scope 4 at src/main.rs:21:5: 21:29
    }

    bb7: {
        StorageDead(_18);                // scope 4 at src/main.rs:21:28: 21:29
        StorageDead(_17);                // scope 4 at src/main.rs:21:29: 21:30
        _0 = copy _11;                   // scope 4 at src/main.rs:22:5: 22:6
        StorageDead(_11);                // scope 3 at src/main.rs:23:1: 23:2
        StorageDead(_10);                // scope 2 at src/main.rs:23:1: 23:2
        StorageDead(_4);                 // scope 1 at src/main.rs:23:1: 23:2
        StorageDead(_2);                 // scope 0 at src/main.rs:23:1: 23:2
        return;                          // scope 0 at src/main.rs:23:2: 23:2
    }
}
"""
BTREE = """// MIR for `mapped` after PreCodegen

fn mapped(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:45:11: 45:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:45:24: 45:27
    let _2: u32;                         // in scope 0 at src/main.rs:46:9: 46:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:46:36: 46:39
    let _5: std::option::Option<u32>;    // in scope 0 at src/main.rs:48:5: 48:19
    let mut _6: &mut std::collections::BTreeMap<u32, u32>; // in scope 0 at src/main.rs:48:5: 48:6
    let _7: ();                          // in scope 0 at src/main.rs:49:5: 49:25
    let mut _8: std::collections::btree_map::Iter<'_, u32, u32>; // in scope 0 at src/main.rs:49:13: 49:21
    let mut _9: &std::collections::BTreeMap<u32, u32>; // in scope 0 at src/main.rs:49:13: 49:14
    let mut _10: u32;                    // in scope 0 at src/main.rs:49:23: 49:24
    let mut _13: u32;                    // in scope 0 at src/main.rs:51:13: 51:18
    let mut _14: &u32;                   // in scope 0 at src/main.rs:51:14: 51:18
    let mut _15: &std::collections::BTreeMap<u32, u32>; // in scope 0 at src/main.rs:51:13: 51:14
    let mut _16: &u32;                   // in scope 0 at src/main.rs:51:15: 51:17
    let _17: &u32;                       // in scope 0 at src/main.rs:51:15: 51:17
    let _18: u32;                        // in scope 0 at src/main.rs:51:16: 51:17
    let mut _19: u32;                    // in scope 0 at src/main.rs:51:21: 51:22
    let mut _20: (u32, bool);            // in scope 0 at src/main.rs:51:13: 51:22
    let _21: &u32;                       // in scope 0 at src/main.rs:52:5: 52:29
    let mut _22: &u32;                   // in scope 0 at src/main.rs:52:26: 52:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:46:9: 46:10
        let mut _4: std::collections::BTreeMap<u32, u32>; // in scope 1 at src/main.rs:47:9: 47:14
        scope 2 {
            debug m => _4;               // in scope 2 at src/main.rs:47:9: 47:14
            let _11: u32;                // in scope 2 at src/main.rs:50:9: 50:10
            scope 3 {
                debug k => _11;          // in scope 3 at src/main.rs:50:9: 50:10
                let _12: u32;            // in scope 3 at src/main.rs:51:9: 51:10
                let mut _23: &u32;       // in scope 3 at src/main.rs:51:15: 51:17
                scope 4 {
                    debug t => _12;      // in scope 4 at src/main.rs:51:9: 51:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:46:9: 46:10
        StorageLive(_3);                 // scope 0 at src/main.rs:46:36: 46:39
        _3 = copy _1;                    // scope 0 at src/main.rs:46:36: 46:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:46:13: 46:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:46:39: 46:40
        StorageLive(_4);                 // scope 1 at src/main.rs:47:9: 47:14
        _4 = BTreeMap::<u32, u32>::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:47:37: 47:52
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:48:5: 48:19
        StorageLive(_6);                 // scope 2 at src/main.rs:48:5: 48:6
        _6 = &mut _4;                    // scope 2 at src/main.rs:48:5: 48:6
        _5 = BTreeMap::<u32, u32>::insert(move _6, const 1_u32, const 7_u32) -> [return: bb3, unwind: bb11]; // scope 2 at src/main.rs:48:5: 48:19
    }

    bb3: {
        StorageDead(_6);                 // scope 2 at src/main.rs:48:18: 48:19
        StorageDead(_5);                 // scope 2 at src/main.rs:48:19: 48:20
        StorageLive(_7);                 // scope 2 at src/main.rs:49:5: 49:25
        StorageLive(_8);                 // scope 2 at src/main.rs:49:13: 49:21
        StorageLive(_9);                 // scope 2 at src/main.rs:49:13: 49:14
        _9 = &_4;                        // scope 2 at src/main.rs:49:13: 49:14
        _8 = BTreeMap::<u32, u32>::iter(move _9) -> [return: bb4, unwind: bb11]; // scope 2 at src/main.rs:49:13: 49:21
    }

    bb4: {
        StorageDead(_9);                 // scope 2 at src/main.rs:49:20: 49:21
        StorageLive(_10);                // scope 2 at src/main.rs:49:23: 49:24
        _10 = copy _2;                   // scope 2 at src/main.rs:49:23: 49:24
        _7 = observe::<std::collections::btree_map::Iter<'_, u32, u32>>(move _8, move _10) -> [return: bb5, unwind: bb11]; // scope 2 at src/main.rs:49:5: 49:25
    }

    bb5: {
        StorageDead(_10);                // scope 2 at src/main.rs:49:24: 49:25
        StorageDead(_8);                 // scope 2 at src/main.rs:49:24: 49:25
        StorageDead(_7);                 // scope 2 at src/main.rs:49:25: 49:26
        StorageLive(_11);                // scope 2 at src/main.rs:50:9: 50:10
        _11 = other() -> [return: bb6, unwind: bb11]; // scope 2 at src/main.rs:50:13: 50:20
    }

    bb6: {
        StorageLive(_12);                // scope 3 at src/main.rs:51:9: 51:10
        StorageLive(_13);                // scope 3 at src/main.rs:51:13: 51:18
        StorageLive(_14);                // scope 3 at src/main.rs:51:14: 51:18
        StorageLive(_15);                // scope 3 at src/main.rs:51:13: 51:14
        _15 = &_4;                       // scope 3 at src/main.rs:51:13: 51:14
        StorageLive(_16);                // scope 3 at src/main.rs:51:15: 51:17
        StorageLive(_17);                // scope 3 at src/main.rs:51:15: 51:17
        _23 = const mapped::promoted[0]; // scope 3 at src/main.rs:51:15: 51:17
        _17 = &(*_23);                   // scope 3 at src/main.rs:51:15: 51:17
        _16 = &(*_17);                   // scope 3 at src/main.rs:51:15: 51:17
        _14 = <BTreeMap<u32, u32> as Index<&u32>>::index(move _15, move _16) -> [return: bb7, unwind: bb11]; // scope 3 at src/main.rs:51:14: 51:18
    }

    bb7: {
        _13 = copy (*_14);               // scope 3 at src/main.rs:51:13: 51:18
        StorageDead(_16);                // scope 3 at src/main.rs:51:17: 51:18
        StorageDead(_15);                // scope 3 at src/main.rs:51:17: 51:18
        StorageLive(_19);                // scope 3 at src/main.rs:51:21: 51:22
        _19 = copy _11;                  // scope 3 at src/main.rs:51:21: 51:22
        _20 = AddWithOverflow(copy _13, copy _19); // scope 3 at src/main.rs:51:13: 51:22
        assert(!move (_20.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _19) -> [success: bb8, unwind: bb11]; // scope 3 at src/main.rs:51:13: 51:22
    }

    bb8: {
        _12 = move (_20.0: u32);         // scope 3 at src/main.rs:51:13: 51:22
        StorageDead(_19);                // scope 3 at src/main.rs:51:21: 51:22
        StorageDead(_13);                // scope 3 at src/main.rs:51:21: 51:22
        StorageDead(_17);                // scope 3 at src/main.rs:51:22: 51:23
        StorageDead(_14);                // scope 3 at src/main.rs:51:22: 51:23
        StorageLive(_21);                // scope 4 at src/main.rs:52:5: 52:29
        StorageLive(_22);                // scope 4 at src/main.rs:52:26: 52:28
        _22 = &_2;                       // scope 4 at src/main.rs:52:26: 52:28
        _21 = std::hint::black_box::<&u32>(move _22) -> [return: bb9, unwind: bb11]; // scope 4 at src/main.rs:52:5: 52:29
    }

    bb9: {
        StorageDead(_22);                // scope 4 at src/main.rs:52:28: 52:29
        StorageDead(_21);                // scope 4 at src/main.rs:52:29: 52:30
        _0 = copy _12;                   // scope 4 at src/main.rs:53:5: 53:6
        StorageDead(_12);                // scope 3 at src/main.rs:54:1: 54:2
        StorageDead(_11);                // scope 2 at src/main.rs:54:1: 54:2
        drop(_4) -> [return: bb10, unwind: bb12]; // scope 1 at src/main.rs:54:1: 54:2
    }

    bb10: {
        StorageDead(_4);                 // scope 1 at src/main.rs:54:1: 54:2
        StorageDead(_2);                 // scope 0 at src/main.rs:54:1: 54:2
        return;                          // scope 0 at src/main.rs:54:2: 54:2
    }

    bb11 (cleanup): {
        drop(_4) -> [return: bb12, unwind terminate(cleanup)]; // scope 1 at src/main.rs:54:1: 54:2
    }

    bb12 (cleanup): {
        resume;                          // scope 0 at src/main.rs:45:1: 54:2
    }
}
"""


def test_a_view_that_only_reads_writes_no_copy_where_it_points():
    # `observe(a.chunks_exact(1), x)` and `observe(m.iter(), x)`: neither view writes, so
    # neither `a` nor `m` holds a copy of `x`. The refusal said "the program's own `a` …
    # may hold a copy of `x` that it goes on with": no safe program can do that.
    from peeky.rust_mir import _may_write

    for kind in (
        "std::slice::ChunksExact<'_, u32>",
        "std::slice::RChunks<'_, u32>",
        "std::collections::btree_map::Iter<'_, u32, u32>",
        "std::str::SplitAsciiWhitespace<'_>",
        "std::path::Components<'_>",
    ):
        assert _may_write(kind, ()) is False, kind
    for text, name, line, view, owner, statement in (
        (CHUNKED, "chunked", 19, 6, 4, "_10 = other()"),
        (BTREE, "mapped", 50, 8, 4, "_11 = other()"),
    ):
        body = body_of(text)
        stop = place(body, statement)
        assert not body._may_write_through(view), name
        assert owner not in body.direct_copies_at(2, stop), name
        assert why(text, VIEWS, name, line, statement) == (None, None), name
    # A view made from a `&mut` (an `IterMut`, an `Entry`, a closure capturing `&mut v`)
    # still may: what it was made from shows it. Not where it was made from a shared
    # `&[u32]` of a `&mut`.
    lent = CHUNKED.replace("let mut _8: &[u32; 2];", "let mut _8: &mut [u32; 2];")
    lent = lent.replace("_8 = &_4;", "_8 = &mut _4;")
    assert not body_of(lent)._may_write_through(6)
    lent = lent.replace("let mut _7: &[u32];", "let mut _7: &mut [u32];")
    body = body_of(lent.replace("move _8 as &[u32] (", "move _8 as &mut [u32] ("))
    assert body._may_write_through(6)


# A struct holding a `&mut`, passed through a shared `&`, by
# value, and through a `&mut`; rustc 1.96.0's MIR (round8u).
HELD_MUT = """use std::env;

#[inline(never)]
fn other() -> u32 {
    1
}

struct Hold<'a> {
    p: &'a mut Vec<u32>,
}

#[inline(never)]
fn look(h: &Hold, x: u32) {
    std::hint::black_box((h.p.len(), x));
}

#[inline(never)]
fn keep(h: Hold, x: u32) {
    h.p.push(x);
}

#[inline(never)]
fn poke(h: &mut Hold, x: u32) {
    h.p.push(x);
}

fn shared(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut v: Vec<u32> = Vec::new();
    let h = Hold { p: &mut v };
    look(&h, x);
    let k = other(); // SHARED
    v.len() as u32 + *std::hint::black_box(&x) + k
}

fn byval(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut v: Vec<u32> = Vec::new();
    let h = Hold { p: &mut v };
    keep(h, x);
    let k = other(); // BYVAL
    std::hint::black_box(&x);
    v[0] + k
}

fn mutref(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut v: Vec<u32> = Vec::new();
    let mut h = Hold { p: &mut v };
    poke(&mut h, x);
    let k = other(); // MUTREF
    std::hint::black_box(&x);
    v[0] + k
}

fn main() {
    let x: u32 = env::args().nth(1).unwrap().parse().unwrap();
    println!("SHARED {}", shared(x));
    println!("BYVAL {}", byval(x));
    println!("MUTREF {}", mutref(x));
}
"""

SHARED_HOLD = """// MIR for `shared` after PreCodegen

fn shared(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:27:11: 27:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:27:24: 27:27
    let _2: u32;                         // in scope 0 at src/main.rs:28:9: 28:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:28:36: 28:39
    let mut _6: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:30:23: 30:29
    let mut _7: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:30:23: 30:29
    let _8: ();                          // in scope 0 at src/main.rs:31:5: 31:16
    let mut _9: &Hold<'_>;               // in scope 0 at src/main.rs:31:10: 31:12
    let _10: &Hold<'_>;                  // in scope 0 at src/main.rs:31:10: 31:12
    let mut _11: u32;                    // in scope 0 at src/main.rs:31:14: 31:15
    let mut _13: u32;                    // in scope 0 at src/main.rs:33:5: 33:47
    let mut _14: u32;                    // in scope 0 at src/main.rs:33:5: 33:19
    let mut _15: usize;                  // in scope 0 at src/main.rs:33:5: 33:12
    let mut _16: &std::vec::Vec<u32>;    // in scope 0 at src/main.rs:33:5: 33:6
    let mut _17: u32;                    // in scope 0 at src/main.rs:33:22: 33:47
    let mut _18: &u32;                   // in scope 0 at src/main.rs:33:23: 33:47
    let mut _19: &u32;                   // in scope 0 at src/main.rs:33:44: 33:46
    let mut _20: (u32, bool);            // in scope 0 at src/main.rs:33:5: 33:47
    let mut _21: u32;                    // in scope 0 at src/main.rs:33:50: 33:51
    let mut _22: (u32, bool);            // in scope 0 at src/main.rs:33:5: 33:51
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:28:9: 28:10
        let mut _4: std::vec::Vec<u32>;  // in scope 1 at src/main.rs:29:9: 29:14
        scope 2 {
            debug v => _4;               // in scope 2 at src/main.rs:29:9: 29:14
            let _5: Hold<'_>;            // in scope 2 at src/main.rs:30:9: 30:10
            scope 3 {
                debug h => _5;           // in scope 3 at src/main.rs:30:9: 30:10
                let _12: u32;            // in scope 3 at src/main.rs:32:9: 32:10
                scope 4 {
                    debug k => _12;      // in scope 4 at src/main.rs:32:9: 32:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:28:9: 28:10
        StorageLive(_3);                 // scope 0 at src/main.rs:28:36: 28:39
        _3 = copy _1;                    // scope 0 at src/main.rs:28:36: 28:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:28:13: 28:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:28:39: 28:40
        StorageLive(_4);                 // scope 1 at src/main.rs:29:9: 29:14
        _4 = Vec::<u32>::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:29:27: 29:37
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:30:9: 30:10
        StorageLive(_6);                 // scope 2 at src/main.rs:30:23: 30:29
        StorageLive(_7);                 // scope 2 at src/main.rs:30:23: 30:29
        _7 = &mut _4;                    // scope 2 at src/main.rs:30:23: 30:29
        _6 = &mut (*_7);                 // scope 2 at src/main.rs:30:23: 30:29
        _5 = Hold::<'_> { p: move _6 };  // scope 2 at src/main.rs:30:13: 30:31
        StorageDead(_6);                 // scope 2 at src/main.rs:30:30: 30:31
        StorageDead(_7);                 // scope 2 at src/main.rs:30:31: 30:32
        StorageLive(_8);                 // scope 3 at src/main.rs:31:5: 31:16
        StorageLive(_9);                 // scope 3 at src/main.rs:31:10: 31:12
        StorageLive(_10);                // scope 3 at src/main.rs:31:10: 31:12
        _10 = &_5;                       // scope 3 at src/main.rs:31:10: 31:12
        _9 = &(*_10);                    // scope 3 at src/main.rs:31:10: 31:12
        StorageLive(_11);                // scope 3 at src/main.rs:31:14: 31:15
        _11 = copy _2;                   // scope 3 at src/main.rs:31:14: 31:15
        _8 = look(move _9, move _11) -> [return: bb3, unwind: bb10]; // scope 3 at src/main.rs:31:5: 31:16
    }

    bb3: {
        StorageDead(_11);                // scope 3 at src/main.rs:31:15: 31:16
        StorageDead(_9);                 // scope 3 at src/main.rs:31:15: 31:16
        StorageDead(_10);                // scope 3 at src/main.rs:31:16: 31:17
        StorageDead(_8);                 // scope 3 at src/main.rs:31:16: 31:17
        StorageLive(_12);                // scope 3 at src/main.rs:32:9: 32:10
        _12 = other() -> [return: bb4, unwind: bb10]; // scope 3 at src/main.rs:32:13: 32:20
    }

    bb4: {
        StorageLive(_13);                // scope 4 at src/main.rs:33:5: 33:47
        StorageLive(_14);                // scope 4 at src/main.rs:33:5: 33:19
        StorageLive(_15);                // scope 4 at src/main.rs:33:5: 33:12
        StorageLive(_16);                // scope 4 at src/main.rs:33:5: 33:6
        _16 = &_4;                       // scope 4 at src/main.rs:33:5: 33:6
        _15 = Vec::<u32>::len(move _16) -> [return: bb5, unwind: bb10]; // scope 4 at src/main.rs:33:5: 33:12
    }

    bb5: {
        StorageDead(_16);                // scope 4 at src/main.rs:33:11: 33:12
        _14 = move _15 as u32 (IntToInt); // scope 4 at src/main.rs:33:5: 33:19
        StorageDead(_15);                // scope 4 at src/main.rs:33:18: 33:19
        StorageLive(_17);                // scope 4 at src/main.rs:33:22: 33:47
        StorageLive(_18);                // scope 4 at src/main.rs:33:23: 33:47
        StorageLive(_19);                // scope 4 at src/main.rs:33:44: 33:46
        _19 = &_2;                       // scope 4 at src/main.rs:33:44: 33:46
        _18 = std::hint::black_box::<&u32>(move _19) -> [return: bb6, unwind: bb10]; // scope 4 at src/main.rs:33:23: 33:47
    }

    bb6: {
        StorageDead(_19);                // scope 4 at src/main.rs:33:46: 33:47
        _17 = copy (*_18);               // scope 4 at src/main.rs:33:22: 33:47
        _20 = AddWithOverflow(copy _14, copy _17); // scope 4 at src/main.rs:33:5: 33:47
        assert(!move (_20.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _17) -> [success: bb7, unwind: bb10]; // scope 4 at src/main.rs:33:5: 33:47
    }

    bb7: {
        _13 = move (_20.0: u32);         // scope 4 at src/main.rs:33:5: 33:47
        StorageDead(_17);                // scope 4 at src/main.rs:33:46: 33:47
        StorageDead(_14);                // scope 4 at src/main.rs:33:46: 33:47
        StorageLive(_21);                // scope 4 at src/main.rs:33:50: 33:51
        _21 = copy _12;                  // scope 4 at src/main.rs:33:50: 33:51
        _22 = AddWithOverflow(copy _13, copy _21); // scope 4 at src/main.rs:33:5: 33:51
        assert(!move (_22.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _21) -> [success: bb8, unwind: bb10]; // scope 4 at src/main.rs:33:5: 33:51
    }

    bb8: {
        _0 = move (_22.0: u32);          // scope 4 at src/main.rs:33:5: 33:51
        StorageDead(_21);                // scope 4 at src/main.rs:33:50: 33:51
        StorageDead(_18);                // scope 4 at src/main.rs:33:50: 33:51
        StorageDead(_13);                // scope 4 at src/main.rs:33:50: 33:51
        StorageDead(_12);                // scope 3 at src/main.rs:34:1: 34:2
        StorageDead(_5);                 // scope 2 at src/main.rs:34:1: 34:2
        drop(_4) -> [return: bb9, unwind: bb11]; // scope 1 at src/main.rs:34:1: 34:2
    }

    bb9: {
        StorageDead(_4);                 // scope 1 at src/main.rs:34:1: 34:2
        StorageDead(_2);                 // scope 0 at src/main.rs:34:1: 34:2
        return;                          // scope 0 at src/main.rs:34:2: 34:2
    }

    bb10 (cleanup): {
        drop(_4) -> [return: bb11, unwind terminate(cleanup)]; // scope 1 at src/main.rs:34:1: 34:2
    }

    bb11 (cleanup): {
        resume;                          // scope 0 at src/main.rs:27:1: 34:2
    }
}
"""

BYVAL_HOLD = """// MIR for `byval` after PreCodegen

fn byval(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:36:10: 36:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:36:23: 36:26
    let _2: u32;                         // in scope 0 at src/main.rs:37:9: 37:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:37:36: 37:39
    let mut _6: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:39:23: 39:29
    let mut _7: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:39:23: 39:29
    let _8: ();                          // in scope 0 at src/main.rs:40:5: 40:15
    let mut _9: Hold<'_>;                // in scope 0 at src/main.rs:40:10: 40:11
    let mut _10: u32;                    // in scope 0 at src/main.rs:40:13: 40:14
    let _12: &u32;                       // in scope 0 at src/main.rs:42:5: 42:29
    let mut _13: &u32;                   // in scope 0 at src/main.rs:42:26: 42:28
    let mut _14: u32;                    // in scope 0 at src/main.rs:43:5: 43:9
    let mut _15: &u32;                   // in scope 0 at src/main.rs:43:6: 43:9
    let mut _16: &std::vec::Vec<u32>;    // in scope 0 at src/main.rs:43:5: 43:6
    let mut _17: u32;                    // in scope 0 at src/main.rs:43:12: 43:13
    let mut _18: (u32, bool);            // in scope 0 at src/main.rs:43:5: 43:13
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:37:9: 37:10
        let mut _4: std::vec::Vec<u32>;  // in scope 1 at src/main.rs:38:9: 38:14
        scope 2 {
            debug v => _4;               // in scope 2 at src/main.rs:38:9: 38:14
            let _5: Hold<'_>;            // in scope 2 at src/main.rs:39:9: 39:10
            scope 3 {
                debug h => _5;           // in scope 3 at src/main.rs:39:9: 39:10
                let _11: u32;            // in scope 3 at src/main.rs:41:9: 41:10
                scope 4 {
                    debug k => _11;      // in scope 4 at src/main.rs:41:9: 41:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:37:9: 37:10
        StorageLive(_3);                 // scope 0 at src/main.rs:37:36: 37:39
        _3 = copy _1;                    // scope 0 at src/main.rs:37:36: 37:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:37:13: 37:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:37:39: 37:40
        StorageLive(_4);                 // scope 1 at src/main.rs:38:9: 38:14
        _4 = Vec::<u32>::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:38:27: 38:37
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:39:9: 39:10
        StorageLive(_6);                 // scope 2 at src/main.rs:39:23: 39:29
        StorageLive(_7);                 // scope 2 at src/main.rs:39:23: 39:29
        _7 = &mut _4;                    // scope 2 at src/main.rs:39:23: 39:29
        _6 = &mut (*_7);                 // scope 2 at src/main.rs:39:23: 39:29
        _5 = Hold::<'_> { p: move _6 };  // scope 2 at src/main.rs:39:13: 39:31
        StorageDead(_6);                 // scope 2 at src/main.rs:39:30: 39:31
        StorageDead(_7);                 // scope 2 at src/main.rs:39:31: 39:32
        StorageLive(_8);                 // scope 3 at src/main.rs:40:5: 40:15
        StorageLive(_9);                 // scope 3 at src/main.rs:40:10: 40:11
        _9 = move _5;                    // scope 3 at src/main.rs:40:10: 40:11
        StorageLive(_10);                // scope 3 at src/main.rs:40:13: 40:14
        _10 = copy _2;                   // scope 3 at src/main.rs:40:13: 40:14
        _8 = keep(move _9, move _10) -> [return: bb3, unwind: bb9]; // scope 3 at src/main.rs:40:5: 40:15
    }

    bb3: {
        StorageDead(_10);                // scope 3 at src/main.rs:40:14: 40:15
        StorageDead(_9);                 // scope 3 at src/main.rs:40:14: 40:15
        StorageDead(_8);                 // scope 3 at src/main.rs:40:15: 40:16
        StorageLive(_11);                // scope 3 at src/main.rs:41:9: 41:10
        _11 = other() -> [return: bb4, unwind: bb9]; // scope 3 at src/main.rs:41:13: 41:20
    }

    bb4: {
        StorageLive(_12);                // scope 4 at src/main.rs:42:5: 42:29
        StorageLive(_13);                // scope 4 at src/main.rs:42:26: 42:28
        _13 = &_2;                       // scope 4 at src/main.rs:42:26: 42:28
        _12 = std::hint::black_box::<&u32>(move _13) -> [return: bb5, unwind: bb9]; // scope 4 at src/main.rs:42:5: 42:29
    }

    bb5: {
        StorageDead(_13);                // scope 4 at src/main.rs:42:28: 42:29
        StorageDead(_12);                // scope 4 at src/main.rs:42:29: 42:30
        StorageLive(_14);                // scope 4 at src/main.rs:43:5: 43:9
        StorageLive(_15);                // scope 4 at src/main.rs:43:6: 43:9
        StorageLive(_16);                // scope 4 at src/main.rs:43:5: 43:6
        _16 = &_4;                       // scope 4 at src/main.rs:43:5: 43:6
        _15 = <Vec<u32> as Index<usize>>::index(move _16, const 0_usize) -> [return: bb6, unwind: bb9]; // scope 4 at src/main.rs:43:6: 43:9
    }

    bb6: {
        _14 = copy (*_15);               // scope 4 at src/main.rs:43:5: 43:9
        StorageDead(_16);                // scope 4 at src/main.rs:43:8: 43:9
        StorageLive(_17);                // scope 4 at src/main.rs:43:12: 43:13
        _17 = copy _11;                  // scope 4 at src/main.rs:43:12: 43:13
        _18 = AddWithOverflow(copy _14, copy _17); // scope 4 at src/main.rs:43:5: 43:13
        assert(!move (_18.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _17) -> [success: bb7, unwind: bb9]; // scope 4 at src/main.rs:43:5: 43:13
    }

    bb7: {
        _0 = move (_18.0: u32);          // scope 4 at src/main.rs:43:5: 43:13
        StorageDead(_17);                // scope 4 at src/main.rs:43:12: 43:13
        StorageDead(_15);                // scope 4 at src/main.rs:43:12: 43:13
        StorageDead(_14);                // scope 4 at src/main.rs:43:12: 43:13
        StorageDead(_11);                // scope 3 at src/main.rs:44:1: 44:2
        StorageDead(_5);                 // scope 2 at src/main.rs:44:1: 44:2
        drop(_4) -> [return: bb8, unwind: bb10]; // scope 1 at src/main.rs:44:1: 44:2
    }

    bb8: {
        StorageDead(_4);                 // scope 1 at src/main.rs:44:1: 44:2
        StorageDead(_2);                 // scope 0 at src/main.rs:44:1: 44:2
        return;                          // scope 0 at src/main.rs:44:2: 44:2
    }

    bb9 (cleanup): {
        drop(_4) -> [return: bb10, unwind terminate(cleanup)]; // scope 1 at src/main.rs:44:1: 44:2
    }

    bb10 (cleanup): {
        resume;                          // scope 0 at src/main.rs:36:1: 44:2
    }
}
"""

MUTREF_HOLD = """// MIR for `mutref` after PreCodegen

fn mutref(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:46:11: 46:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:46:24: 46:27
    let _2: u32;                         // in scope 0 at src/main.rs:47:9: 47:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:47:36: 47:39
    let mut _6: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:49:27: 49:33
    let mut _7: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:49:27: 49:33
    let _8: ();                          // in scope 0 at src/main.rs:50:5: 50:20
    let mut _9: &mut Hold<'_>;           // in scope 0 at src/main.rs:50:10: 50:16
    let mut _10: &mut Hold<'_>;          // in scope 0 at src/main.rs:50:10: 50:16
    let mut _11: u32;                    // in scope 0 at src/main.rs:50:18: 50:19
    let _13: &u32;                       // in scope 0 at src/main.rs:52:5: 52:29
    let mut _14: &u32;                   // in scope 0 at src/main.rs:52:26: 52:28
    let mut _15: u32;                    // in scope 0 at src/main.rs:53:5: 53:9
    let mut _16: &u32;                   // in scope 0 at src/main.rs:53:6: 53:9
    let mut _17: &std::vec::Vec<u32>;    // in scope 0 at src/main.rs:53:5: 53:6
    let mut _18: u32;                    // in scope 0 at src/main.rs:53:12: 53:13
    let mut _19: (u32, bool);            // in scope 0 at src/main.rs:53:5: 53:13
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:47:9: 47:10
        let mut _4: std::vec::Vec<u32>;  // in scope 1 at src/main.rs:48:9: 48:14
        scope 2 {
            debug v => _4;               // in scope 2 at src/main.rs:48:9: 48:14
            let mut _5: Hold<'_>;        // in scope 2 at src/main.rs:49:9: 49:14
            scope 3 {
                debug h => _5;           // in scope 3 at src/main.rs:49:9: 49:14
                let _12: u32;            // in scope 3 at src/main.rs:51:9: 51:10
                scope 4 {
                    debug k => _12;      // in scope 4 at src/main.rs:51:9: 51:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:47:9: 47:10
        StorageLive(_3);                 // scope 0 at src/main.rs:47:36: 47:39
        _3 = copy _1;                    // scope 0 at src/main.rs:47:36: 47:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:47:13: 47:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:47:39: 47:40
        StorageLive(_4);                 // scope 1 at src/main.rs:48:9: 48:14
        _4 = Vec::<u32>::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:48:27: 48:37
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:49:9: 49:14
        StorageLive(_6);                 // scope 2 at src/main.rs:49:27: 49:33
        StorageLive(_7);                 // scope 2 at src/main.rs:49:27: 49:33
        _7 = &mut _4;                    // scope 2 at src/main.rs:49:27: 49:33
        _6 = &mut (*_7);                 // scope 2 at src/main.rs:49:27: 49:33
        _5 = Hold::<'_> { p: move _6 };  // scope 2 at src/main.rs:49:17: 49:35
        StorageDead(_6);                 // scope 2 at src/main.rs:49:34: 49:35
        StorageDead(_7);                 // scope 2 at src/main.rs:49:35: 49:36
        StorageLive(_8);                 // scope 3 at src/main.rs:50:5: 50:20
        StorageLive(_9);                 // scope 3 at src/main.rs:50:10: 50:16
        StorageLive(_10);                // scope 3 at src/main.rs:50:10: 50:16
        _10 = &mut _5;                   // scope 3 at src/main.rs:50:10: 50:16
        _9 = &mut (*_10);                // scope 3 at src/main.rs:50:10: 50:16
        StorageLive(_11);                // scope 3 at src/main.rs:50:18: 50:19
        _11 = copy _2;                   // scope 3 at src/main.rs:50:18: 50:19
        _8 = poke(move _9, move _11) -> [return: bb3, unwind: bb9]; // scope 3 at src/main.rs:50:5: 50:20
    }

    bb3: {
        StorageDead(_11);                // scope 3 at src/main.rs:50:19: 50:20
        StorageDead(_9);                 // scope 3 at src/main.rs:50:19: 50:20
        StorageDead(_10);                // scope 3 at src/main.rs:50:20: 50:21
        StorageDead(_8);                 // scope 3 at src/main.rs:50:20: 50:21
        StorageLive(_12);                // scope 3 at src/main.rs:51:9: 51:10
        _12 = other() -> [return: bb4, unwind: bb9]; // scope 3 at src/main.rs:51:13: 51:20
    }

    bb4: {
        StorageLive(_13);                // scope 4 at src/main.rs:52:5: 52:29
        StorageLive(_14);                // scope 4 at src/main.rs:52:26: 52:28
        _14 = &_2;                       // scope 4 at src/main.rs:52:26: 52:28
        _13 = std::hint::black_box::<&u32>(move _14) -> [return: bb5, unwind: bb9]; // scope 4 at src/main.rs:52:5: 52:29
    }

    bb5: {
        StorageDead(_14);                // scope 4 at src/main.rs:52:28: 52:29
        StorageDead(_13);                // scope 4 at src/main.rs:52:29: 52:30
        StorageLive(_15);                // scope 4 at src/main.rs:53:5: 53:9
        StorageLive(_16);                // scope 4 at src/main.rs:53:6: 53:9
        StorageLive(_17);                // scope 4 at src/main.rs:53:5: 53:6
        _17 = &_4;                       // scope 4 at src/main.rs:53:5: 53:6
        _16 = <Vec<u32> as Index<usize>>::index(move _17, const 0_usize) -> [return: bb6, unwind: bb9]; // scope 4 at src/main.rs:53:6: 53:9
    }

    bb6: {
        _15 = copy (*_16);               // scope 4 at src/main.rs:53:5: 53:9
        StorageDead(_17);                // scope 4 at src/main.rs:53:8: 53:9
        StorageLive(_18);                // scope 4 at src/main.rs:53:12: 53:13
        _18 = copy _12;                  // scope 4 at src/main.rs:53:12: 53:13
        _19 = AddWithOverflow(copy _15, copy _18); // scope 4 at src/main.rs:53:5: 53:13
        assert(!move (_19.1: bool), "attempt to compute `{} + {}`, which would overflow", move _15, move _18) -> [success: bb7, unwind: bb9]; // scope 4 at src/main.rs:53:5: 53:13
    }

    bb7: {
        _0 = move (_19.0: u32);          // scope 4 at src/main.rs:53:5: 53:13
        StorageDead(_18);                // scope 4 at src/main.rs:53:12: 53:13
        StorageDead(_16);                // scope 4 at src/main.rs:53:12: 53:13
        StorageDead(_15);                // scope 4 at src/main.rs:53:12: 53:13
        StorageDead(_12);                // scope 3 at src/main.rs:54:1: 54:2
        StorageDead(_5);                 // scope 2 at src/main.rs:54:1: 54:2
        drop(_4) -> [return: bb8, unwind: bb10]; // scope 1 at src/main.rs:54:1: 54:2
    }

    bb8: {
        StorageDead(_4);                 // scope 1 at src/main.rs:54:1: 54:2
        StorageDead(_2);                 // scope 0 at src/main.rs:54:1: 54:2
        return;                          // scope 0 at src/main.rs:54:2: 54:2
    }

    bb9 (cleanup): {
        drop(_4) -> [return: bb10, unwind terminate(cleanup)]; // scope 1 at src/main.rs:54:1: 54:2
    }

    bb10 (cleanup): {
        resume;                          // scope 0 at src/main.rs:46:1: 54:2
    }
}
"""


# What the debug information shows `Hold` holds: a `&mut`, written through only by
# value or through a `&mut` (rust_symbols.interior_types).
HOLD = {"Hold": (frozenset({"owned"}), ())}


def test_a_shared_reference_lends_no_write_through_the_mut_it_reaches():
    # `let h = Hold { p: &mut v }; look(&h, x);`: through `&Hold`, safe code cannot write
    # through `h.p`, so `v` holds no copy of `x`. The refusal said "the program's own `v`
    # … may hold a copy of `x` that it goes on with".
    from peeky.rust_mir import _may_write

    held = TypeLayouts(HOLD)
    for kind, writes in (
        ("&Hold<'_>", False),
        ("&&mut std::vec::Vec<u32>", False),
        ("(&&mut std::vec::Vec<u32>, u32)", False),
        ("&[&mut u32; 2]", False),
        ("&std::collections::hash_map::Entry<'_, u32, u32>", False),
        ("&std::slice::IterMut<'_, u32>", False),
        ("(&Hold<'_>, &mut u32)", True),
        ("&mut &std::vec::Vec<u32>", True),
        ("&std::cell::Cell<u32>", True),
        ("&std::cell::RefCell<std::vec::Vec<u32>>", True),
        ("&*mut u32", True),
    ):
        assert _may_write(kind, (), layouts=held) is writes, kind
    # Where the debug information wasn't read, `Hold` may hold a cell.
    assert _may_write("&Hold<'_>", ()) is True
    # Reached only through a `&`, a `&mut` or a mutable view writes nothing; a raw
    # pointer or a cell still may.
    for kind, writes in (
        ("&mut std::vec::Vec<u32>", False),
        ("std::slice::IterMut<'_, u32>", False),
        ("*mut std::vec::Vec<u32>", True),
        ("std::cell::Cell<u32>", True),
    ):
        assert _may_write(kind, (), True) is writes, kind
    body = body_of(SHARED_HOLD, HOLD)
    stop = place(body, "_12 = other()")
    assert not body._may_write_through(9)
    assert 4 not in body.direct_copies_at(2, stop)
    assert why(SHARED_HOLD, HELD_MUT, "shared", 32, "_12 = other()", interior=HOLD) == (None, None)
    # By value (`keep(h, x)`) and through `&mut h` (`poke`), the callee may push `x`.
    for text, name, line, given, statement in (
        (BYVAL_HOLD, "byval", 41, 9, "_11 = other()"),
        (MUTREF_HOLD, "mutref", 51, 9, "_12 = other()"),
    ):
        body = body_of(text)
        stop = place(body, statement)
        assert body._may_write_through(given), name
        assert 4 in body.direct_copies_at(2, stop), name
        reason, _ = why(text, HELD_MUT, name, line, statement)
        assert "the program's own `v` (at " in reason, (name, reason)
        assert "may hold a copy of it that it goes on with" in reason, (name, reason)
    # Through `&h`, a raw pointer or a cell `h` holds still may be written through.
    for kind in ("*mut std::vec::Vec<u32>", "&std::cell::Cell<std::vec::Vec<u32>>"):
        text = SHARED_HOLD.replace("let mut _6: &mut std::vec::Vec<u32>;", f"let mut _6: {kind};")
        assert body_of(text, HOLD)._may_write_through(9), kind


# Crate types named like a cell or an atomic, and a standard
# `Cell`, each passed through a shared `&`; rustc 1.96.0's MIR (round10u).
CELL_NAMES = """use std::cell::Cell;
use std::env;

#[inline(never)]
fn other() -> u32 {
    1
}

struct NotCell<'a> {
    p: &'a mut Vec<u32>,
}

struct Atomicity<'a> {
    p: &'a mut Vec<u32>,
}

#[inline(never)]
fn look(h: &NotCell, x: u32) {
    std::hint::black_box((h.p.len(), x));
}

#[inline(never)]
fn glance(h: &Atomicity, x: u32) {
    std::hint::black_box((h.p.len(), x));
}

#[inline(never)]
fn put(c: &Cell<u32>, x: u32) {
    c.set(x);
}

fn notcell(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut v: Vec<u32> = Vec::new();
    let h = NotCell { p: &mut v };
    look(&h, x);
    let k = other(); // NOTCELL
    v.len() as u32 + *std::hint::black_box(&x) + k
}

fn atomicity(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut v: Vec<u32> = Vec::new();
    let h = Atomicity { p: &mut v };
    glance(&h, x);
    let k = other(); // ATOMICITY
    v.len() as u32 + *std::hint::black_box(&x) + k
}

fn celled(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let c = Cell::new(0);
    put(&c, x);
    let k = other(); // CELLED
    std::hint::black_box(&x);
    c.get() + k
}

fn main() {
    let x: u32 = env::args().nth(1).unwrap().parse().unwrap();
    println!("NOTCELL {}", notcell(x));
    println!("ATOMICITY {}", atomicity(x));
    println!("CELLED {}", celled(x));
}
"""

NOT_CELL = """// MIR for `notcell` after PreCodegen

fn notcell(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:32:12: 32:15
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:32:25: 32:28
    let _2: u32;                         // in scope 0 at src/main.rs:33:9: 33:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:33:36: 33:39
    let mut _6: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:35:26: 35:32
    let mut _7: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:35:26: 35:32
    let _8: ();                          // in scope 0 at src/main.rs:36:5: 36:16
    let mut _9: &NotCell<'_>;            // in scope 0 at src/main.rs:36:10: 36:12
    let _10: &NotCell<'_>;               // in scope 0 at src/main.rs:36:10: 36:12
    let mut _11: u32;                    // in scope 0 at src/main.rs:36:14: 36:15
    let mut _13: u32;                    // in scope 0 at src/main.rs:38:5: 38:47
    let mut _14: u32;                    // in scope 0 at src/main.rs:38:5: 38:19
    let mut _15: usize;                  // in scope 0 at src/main.rs:38:5: 38:12
    let mut _16: &std::vec::Vec<u32>;    // in scope 0 at src/main.rs:38:5: 38:6
    let mut _17: u32;                    // in scope 0 at src/main.rs:38:22: 38:47
    let mut _18: &u32;                   // in scope 0 at src/main.rs:38:23: 38:47
    let mut _19: &u32;                   // in scope 0 at src/main.rs:38:44: 38:46
    let mut _20: (u32, bool);            // in scope 0 at src/main.rs:38:5: 38:47
    let mut _21: u32;                    // in scope 0 at src/main.rs:38:50: 38:51
    let mut _22: (u32, bool);            // in scope 0 at src/main.rs:38:5: 38:51
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:33:9: 33:10
        let mut _4: std::vec::Vec<u32>;  // in scope 1 at src/main.rs:34:9: 34:14
        scope 2 {
            debug v => _4;               // in scope 2 at src/main.rs:34:9: 34:14
            let _5: NotCell<'_>;         // in scope 2 at src/main.rs:35:9: 35:10
            scope 3 {
                debug h => _5;           // in scope 3 at src/main.rs:35:9: 35:10
                let _12: u32;            // in scope 3 at src/main.rs:37:9: 37:10
                scope 4 {
                    debug k => _12;      // in scope 4 at src/main.rs:37:9: 37:10
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
        StorageLive(_4);                 // scope 1 at src/main.rs:34:9: 34:14
        _4 = Vec::<u32>::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:34:27: 34:37
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:35:9: 35:10
        StorageLive(_6);                 // scope 2 at src/main.rs:35:26: 35:32
        StorageLive(_7);                 // scope 2 at src/main.rs:35:26: 35:32
        _7 = &mut _4;                    // scope 2 at src/main.rs:35:26: 35:32
        _6 = &mut (*_7);                 // scope 2 at src/main.rs:35:26: 35:32
        _5 = NotCell::<'_> { p: move _6 }; // scope 2 at src/main.rs:35:13: 35:34
        StorageDead(_6);                 // scope 2 at src/main.rs:35:33: 35:34
        StorageDead(_7);                 // scope 2 at src/main.rs:35:34: 35:35
        StorageLive(_8);                 // scope 3 at src/main.rs:36:5: 36:16
        StorageLive(_9);                 // scope 3 at src/main.rs:36:10: 36:12
        StorageLive(_10);                // scope 3 at src/main.rs:36:10: 36:12
        _10 = &_5;                       // scope 3 at src/main.rs:36:10: 36:12
        _9 = &(*_10);                    // scope 3 at src/main.rs:36:10: 36:12
        StorageLive(_11);                // scope 3 at src/main.rs:36:14: 36:15
        _11 = copy _2;                   // scope 3 at src/main.rs:36:14: 36:15
        _8 = look(move _9, move _11) -> [return: bb3, unwind: bb10]; // scope 3 at src/main.rs:36:5: 36:16
    }

    bb3: {
        StorageDead(_11);                // scope 3 at src/main.rs:36:15: 36:16
        StorageDead(_9);                 // scope 3 at src/main.rs:36:15: 36:16
        StorageDead(_10);                // scope 3 at src/main.rs:36:16: 36:17
        StorageDead(_8);                 // scope 3 at src/main.rs:36:16: 36:17
        StorageLive(_12);                // scope 3 at src/main.rs:37:9: 37:10
        _12 = other() -> [return: bb4, unwind: bb10]; // scope 3 at src/main.rs:37:13: 37:20
    }

    bb4: {
        StorageLive(_13);                // scope 4 at src/main.rs:38:5: 38:47
        StorageLive(_14);                // scope 4 at src/main.rs:38:5: 38:19
        StorageLive(_15);                // scope 4 at src/main.rs:38:5: 38:12
        StorageLive(_16);                // scope 4 at src/main.rs:38:5: 38:6
        _16 = &_4;                       // scope 4 at src/main.rs:38:5: 38:6
        _15 = Vec::<u32>::len(move _16) -> [return: bb5, unwind: bb10]; // scope 4 at src/main.rs:38:5: 38:12
    }

    bb5: {
        StorageDead(_16);                // scope 4 at src/main.rs:38:11: 38:12
        _14 = move _15 as u32 (IntToInt); // scope 4 at src/main.rs:38:5: 38:19
        StorageDead(_15);                // scope 4 at src/main.rs:38:18: 38:19
        StorageLive(_17);                // scope 4 at src/main.rs:38:22: 38:47
        StorageLive(_18);                // scope 4 at src/main.rs:38:23: 38:47
        StorageLive(_19);                // scope 4 at src/main.rs:38:44: 38:46
        _19 = &_2;                       // scope 4 at src/main.rs:38:44: 38:46
        _18 = std::hint::black_box::<&u32>(move _19) -> [return: bb6, unwind: bb10]; // scope 4 at src/main.rs:38:23: 38:47
    }

    bb6: {
        StorageDead(_19);                // scope 4 at src/main.rs:38:46: 38:47
        _17 = copy (*_18);               // scope 4 at src/main.rs:38:22: 38:47
        _20 = AddWithOverflow(copy _14, copy _17); // scope 4 at src/main.rs:38:5: 38:47
        assert(!move (_20.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _17) -> [success: bb7, unwind: bb10]; // scope 4 at src/main.rs:38:5: 38:47
    }

    bb7: {
        _13 = move (_20.0: u32);         // scope 4 at src/main.rs:38:5: 38:47
        StorageDead(_17);                // scope 4 at src/main.rs:38:46: 38:47
        StorageDead(_14);                // scope 4 at src/main.rs:38:46: 38:47
        StorageLive(_21);                // scope 4 at src/main.rs:38:50: 38:51
        _21 = copy _12;                  // scope 4 at src/main.rs:38:50: 38:51
        _22 = AddWithOverflow(copy _13, copy _21); // scope 4 at src/main.rs:38:5: 38:51
        assert(!move (_22.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _21) -> [success: bb8, unwind: bb10]; // scope 4 at src/main.rs:38:5: 38:51
    }

    bb8: {
        _0 = move (_22.0: u32);          // scope 4 at src/main.rs:38:5: 38:51
        StorageDead(_21);                // scope 4 at src/main.rs:38:50: 38:51
        StorageDead(_18);                // scope 4 at src/main.rs:38:50: 38:51
        StorageDead(_13);                // scope 4 at src/main.rs:38:50: 38:51
        StorageDead(_12);                // scope 3 at src/main.rs:39:1: 39:2
        StorageDead(_5);                 // scope 2 at src/main.rs:39:1: 39:2
        drop(_4) -> [return: bb9, unwind: bb11]; // scope 1 at src/main.rs:39:1: 39:2
    }

    bb9: {
        StorageDead(_4);                 // scope 1 at src/main.rs:39:1: 39:2
        StorageDead(_2);                 // scope 0 at src/main.rs:39:1: 39:2
        return;                          // scope 0 at src/main.rs:39:2: 39:2
    }

    bb10 (cleanup): {
        drop(_4) -> [return: bb11, unwind terminate(cleanup)]; // scope 1 at src/main.rs:39:1: 39:2
    }

    bb11 (cleanup): {
        resume;                          // scope 0 at src/main.rs:32:1: 39:2
    }
}
"""

ATOMICITY = """// MIR for `atomicity` after PreCodegen

fn atomicity(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:41:14: 41:17
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:41:27: 41:30
    let _2: u32;                         // in scope 0 at src/main.rs:42:9: 42:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:42:36: 42:39
    let mut _6: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:44:28: 44:34
    let mut _7: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:44:28: 44:34
    let _8: ();                          // in scope 0 at src/main.rs:45:5: 45:18
    let mut _9: &Atomicity<'_>;          // in scope 0 at src/main.rs:45:12: 45:14
    let _10: &Atomicity<'_>;             // in scope 0 at src/main.rs:45:12: 45:14
    let mut _11: u32;                    // in scope 0 at src/main.rs:45:16: 45:17
    let mut _13: u32;                    // in scope 0 at src/main.rs:47:5: 47:47
    let mut _14: u32;                    // in scope 0 at src/main.rs:47:5: 47:19
    let mut _15: usize;                  // in scope 0 at src/main.rs:47:5: 47:12
    let mut _16: &std::vec::Vec<u32>;    // in scope 0 at src/main.rs:47:5: 47:6
    let mut _17: u32;                    // in scope 0 at src/main.rs:47:22: 47:47
    let mut _18: &u32;                   // in scope 0 at src/main.rs:47:23: 47:47
    let mut _19: &u32;                   // in scope 0 at src/main.rs:47:44: 47:46
    let mut _20: (u32, bool);            // in scope 0 at src/main.rs:47:5: 47:47
    let mut _21: u32;                    // in scope 0 at src/main.rs:47:50: 47:51
    let mut _22: (u32, bool);            // in scope 0 at src/main.rs:47:5: 47:51
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:42:9: 42:10
        let mut _4: std::vec::Vec<u32>;  // in scope 1 at src/main.rs:43:9: 43:14
        scope 2 {
            debug v => _4;               // in scope 2 at src/main.rs:43:9: 43:14
            let _5: Atomicity<'_>;       // in scope 2 at src/main.rs:44:9: 44:10
            scope 3 {
                debug h => _5;           // in scope 3 at src/main.rs:44:9: 44:10
                let _12: u32;            // in scope 3 at src/main.rs:46:9: 46:10
                scope 4 {
                    debug k => _12;      // in scope 4 at src/main.rs:46:9: 46:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:42:9: 42:10
        StorageLive(_3);                 // scope 0 at src/main.rs:42:36: 42:39
        _3 = copy _1;                    // scope 0 at src/main.rs:42:36: 42:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:42:13: 42:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:42:39: 42:40
        StorageLive(_4);                 // scope 1 at src/main.rs:43:9: 43:14
        _4 = Vec::<u32>::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:43:27: 43:37
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:44:9: 44:10
        StorageLive(_6);                 // scope 2 at src/main.rs:44:28: 44:34
        StorageLive(_7);                 // scope 2 at src/main.rs:44:28: 44:34
        _7 = &mut _4;                    // scope 2 at src/main.rs:44:28: 44:34
        _6 = &mut (*_7);                 // scope 2 at src/main.rs:44:28: 44:34
        _5 = Atomicity::<'_> { p: move _6 }; // scope 2 at src/main.rs:44:13: 44:36
        StorageDead(_6);                 // scope 2 at src/main.rs:44:35: 44:36
        StorageDead(_7);                 // scope 2 at src/main.rs:44:36: 44:37
        StorageLive(_8);                 // scope 3 at src/main.rs:45:5: 45:18
        StorageLive(_9);                 // scope 3 at src/main.rs:45:12: 45:14
        StorageLive(_10);                // scope 3 at src/main.rs:45:12: 45:14
        _10 = &_5;                       // scope 3 at src/main.rs:45:12: 45:14
        _9 = &(*_10);                    // scope 3 at src/main.rs:45:12: 45:14
        StorageLive(_11);                // scope 3 at src/main.rs:45:16: 45:17
        _11 = copy _2;                   // scope 3 at src/main.rs:45:16: 45:17
        _8 = glance(move _9, move _11) -> [return: bb3, unwind: bb10]; // scope 3 at src/main.rs:45:5: 45:18
    }

    bb3: {
        StorageDead(_11);                // scope 3 at src/main.rs:45:17: 45:18
        StorageDead(_9);                 // scope 3 at src/main.rs:45:17: 45:18
        StorageDead(_10);                // scope 3 at src/main.rs:45:18: 45:19
        StorageDead(_8);                 // scope 3 at src/main.rs:45:18: 45:19
        StorageLive(_12);                // scope 3 at src/main.rs:46:9: 46:10
        _12 = other() -> [return: bb4, unwind: bb10]; // scope 3 at src/main.rs:46:13: 46:20
    }

    bb4: {
        StorageLive(_13);                // scope 4 at src/main.rs:47:5: 47:47
        StorageLive(_14);                // scope 4 at src/main.rs:47:5: 47:19
        StorageLive(_15);                // scope 4 at src/main.rs:47:5: 47:12
        StorageLive(_16);                // scope 4 at src/main.rs:47:5: 47:6
        _16 = &_4;                       // scope 4 at src/main.rs:47:5: 47:6
        _15 = Vec::<u32>::len(move _16) -> [return: bb5, unwind: bb10]; // scope 4 at src/main.rs:47:5: 47:12
    }

    bb5: {
        StorageDead(_16);                // scope 4 at src/main.rs:47:11: 47:12
        _14 = move _15 as u32 (IntToInt); // scope 4 at src/main.rs:47:5: 47:19
        StorageDead(_15);                // scope 4 at src/main.rs:47:18: 47:19
        StorageLive(_17);                // scope 4 at src/main.rs:47:22: 47:47
        StorageLive(_18);                // scope 4 at src/main.rs:47:23: 47:47
        StorageLive(_19);                // scope 4 at src/main.rs:47:44: 47:46
        _19 = &_2;                       // scope 4 at src/main.rs:47:44: 47:46
        _18 = std::hint::black_box::<&u32>(move _19) -> [return: bb6, unwind: bb10]; // scope 4 at src/main.rs:47:23: 47:47
    }

    bb6: {
        StorageDead(_19);                // scope 4 at src/main.rs:47:46: 47:47
        _17 = copy (*_18);               // scope 4 at src/main.rs:47:22: 47:47
        _20 = AddWithOverflow(copy _14, copy _17); // scope 4 at src/main.rs:47:5: 47:47
        assert(!move (_20.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _17) -> [success: bb7, unwind: bb10]; // scope 4 at src/main.rs:47:5: 47:47
    }

    bb7: {
        _13 = move (_20.0: u32);         // scope 4 at src/main.rs:47:5: 47:47
        StorageDead(_17);                // scope 4 at src/main.rs:47:46: 47:47
        StorageDead(_14);                // scope 4 at src/main.rs:47:46: 47:47
        StorageLive(_21);                // scope 4 at src/main.rs:47:50: 47:51
        _21 = copy _12;                  // scope 4 at src/main.rs:47:50: 47:51
        _22 = AddWithOverflow(copy _13, copy _21); // scope 4 at src/main.rs:47:5: 47:51
        assert(!move (_22.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _21) -> [success: bb8, unwind: bb10]; // scope 4 at src/main.rs:47:5: 47:51
    }

    bb8: {
        _0 = move (_22.0: u32);          // scope 4 at src/main.rs:47:5: 47:51
        StorageDead(_21);                // scope 4 at src/main.rs:47:50: 47:51
        StorageDead(_18);                // scope 4 at src/main.rs:47:50: 47:51
        StorageDead(_13);                // scope 4 at src/main.rs:47:50: 47:51
        StorageDead(_12);                // scope 3 at src/main.rs:48:1: 48:2
        StorageDead(_5);                 // scope 2 at src/main.rs:48:1: 48:2
        drop(_4) -> [return: bb9, unwind: bb11]; // scope 1 at src/main.rs:48:1: 48:2
    }

    bb9: {
        StorageDead(_4);                 // scope 1 at src/main.rs:48:1: 48:2
        StorageDead(_2);                 // scope 0 at src/main.rs:48:1: 48:2
        return;                          // scope 0 at src/main.rs:48:2: 48:2
    }

    bb10 (cleanup): {
        drop(_4) -> [return: bb11, unwind terminate(cleanup)]; // scope 1 at src/main.rs:48:1: 48:2
    }

    bb11 (cleanup): {
        resume;                          // scope 0 at src/main.rs:41:1: 48:2
    }
}
"""

CELL_LENT = """// MIR for `celled` after PreCodegen

fn celled(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:50:11: 50:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:50:24: 50:27
    let _2: u32;                         // in scope 0 at src/main.rs:51:9: 51:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:51:36: 51:39
    let _5: ();                          // in scope 0 at src/main.rs:53:5: 53:15
    let mut _6: &std::cell::Cell<u32>;   // in scope 0 at src/main.rs:53:9: 53:11
    let _7: &std::cell::Cell<u32>;       // in scope 0 at src/main.rs:53:9: 53:11
    let mut _8: u32;                     // in scope 0 at src/main.rs:53:13: 53:14
    let _10: &u32;                       // in scope 0 at src/main.rs:55:5: 55:29
    let mut _11: &u32;                   // in scope 0 at src/main.rs:55:26: 55:28
    let mut _12: u32;                    // in scope 0 at src/main.rs:56:5: 56:12
    let mut _13: &std::cell::Cell<u32>;  // in scope 0 at src/main.rs:56:5: 56:6
    let mut _14: u32;                    // in scope 0 at src/main.rs:56:15: 56:16
    let mut _15: (u32, bool);            // in scope 0 at src/main.rs:56:5: 56:16
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:51:9: 51:10
        let _4: std::cell::Cell<u32>;    // in scope 1 at src/main.rs:52:9: 52:10
        scope 2 {
            debug c => _4;               // in scope 2 at src/main.rs:52:9: 52:10
            let _9: u32;                 // in scope 2 at src/main.rs:54:9: 54:10
            scope 3 {
                debug k => _9;           // in scope 3 at src/main.rs:54:9: 54:10
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:51:9: 51:10
        StorageLive(_3);                 // scope 0 at src/main.rs:51:36: 51:39
        _3 = copy _1;                    // scope 0 at src/main.rs:51:36: 51:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:51:13: 51:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:51:39: 51:40
        StorageLive(_4);                 // scope 1 at src/main.rs:52:9: 52:10
        _4 = Cell::<u32>::new(const 0_u32) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:52:13: 52:25
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:53:5: 53:15
        StorageLive(_6);                 // scope 2 at src/main.rs:53:9: 53:11
        StorageLive(_7);                 // scope 2 at src/main.rs:53:9: 53:11
        _7 = &_4;                        // scope 2 at src/main.rs:53:9: 53:11
        _6 = &(*_7);                     // scope 2 at src/main.rs:53:9: 53:11
        StorageLive(_8);                 // scope 2 at src/main.rs:53:13: 53:14
        _8 = copy _2;                    // scope 2 at src/main.rs:53:13: 53:14
        _5 = put(move _6, move _8) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:53:5: 53:15
    }

    bb3: {
        StorageDead(_8);                 // scope 2 at src/main.rs:53:14: 53:15
        StorageDead(_6);                 // scope 2 at src/main.rs:53:14: 53:15
        StorageDead(_7);                 // scope 2 at src/main.rs:53:15: 53:16
        StorageDead(_5);                 // scope 2 at src/main.rs:53:15: 53:16
        StorageLive(_9);                 // scope 2 at src/main.rs:54:9: 54:10
        _9 = other() -> [return: bb4, unwind continue]; // scope 2 at src/main.rs:54:13: 54:20
    }

    bb4: {
        StorageLive(_10);                // scope 3 at src/main.rs:55:5: 55:29
        StorageLive(_11);                // scope 3 at src/main.rs:55:26: 55:28
        _11 = &_2;                       // scope 3 at src/main.rs:55:26: 55:28
        _10 = std::hint::black_box::<&u32>(move _11) -> [return: bb5, unwind continue]; // scope 3 at src/main.rs:55:5: 55:29
    }

    bb5: {
        StorageDead(_11);                // scope 3 at src/main.rs:55:28: 55:29
        StorageDead(_10);                // scope 3 at src/main.rs:55:29: 55:30
        StorageLive(_12);                // scope 3 at src/main.rs:56:5: 56:12
        StorageLive(_13);                // scope 3 at src/main.rs:56:5: 56:6
        _13 = &_4;                       // scope 3 at src/main.rs:56:5: 56:6
        _12 = Cell::<u32>::get(move _13) -> [return: bb6, unwind continue]; // scope 3 at src/main.rs:56:5: 56:12
    }

    bb6: {
        StorageDead(_13);                // scope 3 at src/main.rs:56:11: 56:12
        StorageLive(_14);                // scope 3 at src/main.rs:56:15: 56:16
        _14 = copy _9;                   // scope 3 at src/main.rs:56:15: 56:16
        _15 = AddWithOverflow(copy _12, copy _14); // scope 3 at src/main.rs:56:5: 56:16
        assert(!move (_15.1: bool), "attempt to compute `{} + {}`, which would overflow", move _12, move _14) -> [success: bb7, unwind continue]; // scope 3 at src/main.rs:56:5: 56:16
    }

    bb7: {
        _0 = move (_15.0: u32);          // scope 3 at src/main.rs:56:5: 56:16
        StorageDead(_14);                // scope 3 at src/main.rs:56:15: 56:16
        StorageDead(_12);                // scope 3 at src/main.rs:56:15: 56:16
        StorageDead(_9);                 // scope 2 at src/main.rs:57:1: 57:2
        StorageDead(_4);                 // scope 1 at src/main.rs:57:1: 57:2
        StorageDead(_2);                 // scope 0 at src/main.rs:57:1: 57:2
        return;                          // scope 0 at src/main.rs:57:2: 57:2
    }
}
"""


def test_a_crate_type_named_like_a_cell_or_an_atomic_is_neither():
    # `look(&h, x)` with `h = NotCell { p: &mut v }`, and `glance(&h, x)` with an
    # `Atomicity`: neither is a standard cell or atomic, so through `&h` no callee writes
    # into `v`. A match on `Cell<` or `Atomic` in the type's text said "the program's own
    # `v` … may hold a copy of `x`".
    from peeky.rust_mir import _may_write

    # Each crate type by what the debug information shows it holds.
    named = {
        "NotCell": (frozenset({"owned"}), ()),
        "Atomicity": (frozenset({"owned"}), ()),
        "AtomicCounter": (frozenset(), ()),
        "shapes::Cell": (frozenset(), ()),
    }
    for kind, writes in (
        ("&NotCell<'_>", False),
        ("&Atomicity<'_>", False),
        ("&AtomicCounter", False),
        ("&shapes::Cell<u32>", False),
        ("&std::cell::LazyCell<u32>", False),
        ("&std::sync::atomic::Ordering", False),
        ("&std::cell::Cell<u32>", True),
        ("&core::cell::RefCell<u32>", True),
        ("&std::cell::UnsafeCell<u32>", True),
        ("&std::cell::OnceCell<u32>", True),
        ("&std::sync::OnceLock<u32>", True),
        ("&std::sync::Mutex<u32>", True),
        ("&std::sync::RwLock<u32>", True),
        ("&std::sync::atomic::Atomic<u32>", True),
        ("&std::sync::atomic::AtomicU32", True),
        ("&core::sync::atomic::AtomicBool", True),
        ("&std::sync::atomic::AtomicPtr<u32>", True),
        ("&std::ptr::NonNull<u32>", True),
        ("&*const u32", True),
    ):
        assert _may_write(kind, (), True, layouts=TypeLayouts(named)) is writes, kind
    # One that holds an atomic, as the debug information shows, may be written through.
    holds = {"AtomicCounter": (frozenset({"interior", "owned"}), ())}
    assert _may_write("&AtomicCounter", (), True, layouts=TypeLayouts(holds)) is True
    for text, name, line in ((NOT_CELL, "notcell", 37), (ATOMICITY, "atomicity", 46)):
        body = body_of(text, named)
        stop = place(body, "_12 = other()")
        assert not body._may_write_through(9), name
        assert 4 not in body.direct_copies_at(2, stop), name
        assert why(text, CELL_NAMES, name, line, "_12 = other()", interior=named) == (None, None)
    # `put(&c, x)` with `c = Cell::new(0)`: through `&Cell<u32>`, `c.set(x)` stores it.
    body = body_of(CELL_LENT)
    stop = place(body, "_9 = other()")
    assert body._may_write_through(6)
    assert 4 in body.direct_copies_at(2, stop)
    reason, _ = why(CELL_LENT, CELL_NAMES, "celled", 54, "_9 = other()")
    assert "the program's own `c` (at " in reason, reason
    assert "may hold a copy of it that it goes on with" in reason, reason
