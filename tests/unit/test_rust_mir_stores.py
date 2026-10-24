"""#311: a statement that stores a copy through a pointer holds it in the memory it stores
into. `vec![x, 1]` fills its Box by `((*_10).1: …) = [move _7, const 1_u32]`, then makes
the Vec from that Box, and `total(…)` sums it. The copy analysis skipped every store
through a pointer: once `_7` died, nothing held `x`'s copy, so `x` was `[settable]` on the
statement's later line, and a change there never reached the program.

On the same path, a refusal before the run listed the very local it refused as "settable
here"."""

import json
from types import SimpleNamespace

from peeky.rust import RustSession
from peeky.rust_mir import IrFunction, OwnedLocals, parse_body

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


# `literal` (lines 23 to 27): the issue's statement.
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


def body_of(text):
    """The body of ``text``, as matched to its IR: a function with no type parameters."""
    body = parse_body(text)
    body.generics = ()
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


def why(text, source, name, line, statement, variable="x"):
    """Why a change to ``variable`` is refused at a stop on ``line`` of the function
    ``name`` of ``text``, at its statement starting ``statement`` (``mid_statement``'s
    reason), with Peeky's own proofs over the MIR: (the reason, its row's short form)."""
    body = parse_body(text)
    blocks = {}
    for number, block in body.blocks.items():
        lines = {at for _, file, at in block.statements if file == "src/main.rs" and at}
        blocks["start" if number == 0 else f"bb{number}"] = lines
    declared = min(at for _, file, at in body.blocks[0].statements if file == "src/main.rs")
    function = IrFunction(name, "crate", "src/main.rs", blocks, {}, declared - 1)
    texts = {"src/main.rs": source}
    owned = OwnedLocals([body], {HASH: function}, ("src/main.rs",), texts=texts)
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
    # The issue: at `+ other();`, `total(vec![x, 1])` has summed `x`'s copy into `_3`,
    # which the statement adds after the stop. `_7` held the copy until the store.
    body = body_of(LITERAL)
    stop = place(body, "_8 = other()")
    assert used(body, 1, stop) == {3}
    # `total` is the crate's own: what it returns may be anything (#311 review), so the
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
    # `let mut b = Box::new([x, 1]); b[0] = 7;`. On #310's merge `b` took `x`'s copy from
    # `Box::new`, and kept it past `b[0] = 7`: "has already copied its value into `b`"
    # was false. What a call returns is never a sure copy now; the store ends any.
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


# #311 review. A refusal the must-analysis made through a crate function's result was
# false, and the may-analysis missed writes a call makes, or one through a pointer to
# memory no local of the frame stands for. rustc 1.96.0's MIR of this crate's functions:
ROUND2 = """use std::env;

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


def round2(text, name, line, statement):
    return why(text, ROUND2, name, line, statement)


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
    # nothing live holds `x`, but "has already read `x` into a temporary" was said. Before
    # this review the must-analysis held `{2, 3, 4, 5}` there.
    body = body_of(DISCARDED)
    stop = place(body, "_11 = other()")
    assert read(body, 2, stop) == set()
    assert used(body, 2, stop) == {5}
    # `vec!`'s Vec surely held it, until `discard` was given it.
    assert 7 in read(body, 2, after(body, "_7 = std::boxed::box_assume_init_into_vec_unsafe"))
    assert read(body, 2, after(body, "_6 = discard(")) == set()
    assert round2(DISCARDED, "discarded", 21, "_11 = other()") == (
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
    assert round2(PUSHED, "pushed", 30, "_8 = other()") == kept("v", "lines 28 and 29")
    # Not where the call is given no copy: `v.push(7)`.
    seven = body_of(PUSHED.replace("_7 = copy _2;", "_7 = const 7_u32;"))
    assert 4 not in seven.direct_copies_at(2, stop)


def test_ptr_write_stores_a_copy_in_what_its_pointer_points_at():
    # `p = &raw mut slot; ptr::write(p, x);`, then `slot + k`.
    body = body_of(WRITTEN)
    stop = place(body, "_9 = other()")
    direct = body.direct_copies_at(2, stop)
    assert 4 in direct and 5 not in direct
    assert round2(WRITTEN, "written", 41, "_9 = other()") == kept("slot", "line 40")


def test_a_pointer_to_memory_no_local_stands_for_stands_for_it():
    # `*p = x;` through an argument `p`, read back by `*p` on the next line: no local of
    # the frame holds the copy, so `p` stands for what it points at.
    body = body_of(THROUGH)
    stop = place(body, "_7 = other()")
    assert 1 in body.direct_copies_at(3, stop) and body.copy_undoes(1, 3, stop)
    assert why(THROUGH, ROUND2, "through", 50, "_7 = other()", "x") == kept(
        "p", "line 49", "point at"
    )
    # Nor does the Box `Box::into_raw` was given, once it is moved out of.
    body = body_of(RAW)
    stop = place(body, "_8 = other()")
    assert 4 in body.direct_copies_at(2, stop) and body.copy_undoes(4, 2, stop)
    assert round2(RAW, "raw", 60, "_8 = other()") == kept("p", "lines 58 and 59", "point at")
