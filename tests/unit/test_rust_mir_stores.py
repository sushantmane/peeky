"""#311: a statement that stores a copy through a pointer holds it in the memory it stores
into. `vec![x, 1]` fills its Box by `((*_10).1: …) = [move _7, const 1_u32]`, then makes
the Vec from that Box, and `total(…)` sums it. The copy analysis skipped every store
through a pointer: once `_7` died, nothing held `x`'s copy, so `x` was `[settable]` on the
statement's later line, and a change there never reached the program."""

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


def read(body, local, position):
    """The temporaries the must-analysis finds surely read ``local`` at ``position``."""
    return set(body.read_copies_at(local, position)[0])


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


READ = (
    "a statement under way at the stop has already read it into a temporary (at line 24), "
    "which it uses after the stop"
)


def test_what_vec_stores_through_its_box_pointer_is_a_copy_the_statement_uses():
    # The issue: at `+ other();`, `total(vec![x, 1])` has summed `x`'s copy into `_3`,
    # which the statement adds after the stop. `_7` held the copy until the store.
    body = body_of(LITERAL)
    stop = place(body, "_8 = other()")
    assert used(body, 1, stop) == {3}
    assert read(body, 1, stop) == {3}
    assert why(LITERAL, SOURCE, "literal", 25, "_8 = other()") == (
        READ,
        "a statement under way read it (line 24)",
    )
    # Each step holds it: the Box the store fills, the Box moved, the Vec made from it.
    for statement, holder in (
        ("((((*_10).1:", 6),
        ("_5 = move _6;", 5),
        ("_4 = std::boxed::box_assume_init_into_vec_unsafe", 4),
    ):
        assert holder in body.copies_at(1, after(body, statement)), statement
        assert holder in read(body, 1, after(body, statement)), statement
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


def test_a_box_an_older_rustc_fills_through_itself_holds_what_it_stores():
    # `ShallowInitBox(exchange_malloc(…))`, then `(*_6) = [move _7, const 1_u32]`.
    body = body_of(SHALLOW)
    stop = place(body, "_8 = other()")
    assert used(body, 1, stop) == read(body, 1, stop) == {3}
    assert 6 in read(body, 1, after(body, "(*_6) = [move _7"))
    assert why(SHALLOW, SOURCE, "literal", 25, "_8 = other()")[0] == READ


def test_a_call_given_a_pointer_into_the_box_may_change_what_it_holds():
    # The must-analysis: a call given the raw pointer `_10` into the Box may write through
    # it, so the Box surely holds the copy no more; the refusal is hedged.
    text = LITERAL.replace(
        "box_assume_init_into_vec_unsafe::<u32, 2>(move _5)",
        "box_assume_init_into_vec_unsafe::<u32, 2>(move _5, copy _10)",
    )
    body = body_of(text)
    stop = place(body, "_8 = other()")
    assert used(body, 1, stop) == {3}
    assert read(body, 1, stop) == set()
    assert why(text, SOURCE, "literal", 25, "_8 = other()")[0] == (
        "a statement under way at the stop reads it into a temporary (at line 24), so it may "
        "hold a copy of it, which it uses after the stop"
    )


def test_a_store_through_a_boxs_own_pointer_leaves_no_sure_copy_in_it():
    # `let mut b = Box::new([x, 1]); b[0] = 7;`: `b` took `x`'s copy, then the store through
    # its own pointer `_17` overwrote it. "has already copied its value into `b`" was false.
    body = body_of(OVERWRITTEN)
    store = place(body, "(*_17)[_7] = const 7_u32;")
    assert 4 in body.read_copies_at(2, store)[1]
    assert 4 not in body.read_copies_at(2, after(body, "(*_17)[_7]"))[1]
    stop = place(body, "_12 = other()")
    taken, sources = body.read_copies_at(2, stop)[1:]
    # Neither `b`, the Box moved out of it (`_11`), nor `boxed(b)` (`_10`).
    assert not taken & {4, 10, 11} and not {made for made, _ in sources} & {10, 11}
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
