"""#321: a callee given a shared `&` may still store a copy where its type doesn't show a
cell. `send(&tx, x)` with `tx` an `mpsc::Sender` keeps `x` in the channel, where `rx`
reads it; `bump(&c, x)` with `c = Counter::new()` keeps it in `c.c`, a `Cell`, which a
call built, so no provenance shows it. Both left `x` `[settable]` after the call, and the
change never reached the program. Writability through `&T` is now judged by `T`'s whole
layout: the crate's own types by the build's debug information
(rust_symbols.interior_types), the standard library's by the classes of
rust_mir._STD_NAMED."""

from types import SimpleNamespace

from peeky.errors import _mutation_hint as mutation_hint
from peeky.rust import RustSession
from peeky.rust_mir import (
    CrateType,
    IrFunction,
    OwnedLocals,
    TypeLayouts,
    _may_share,
    _may_take,
    _may_write,
    _type_flags,
    parse_body,
)
from peeky.rust_symbols import _Die, interior_types

_STD_CRATES = frozenset({"std", "core", "alloc"})

# The crate the fixtures below are rustc 1.96.0's MIR of (`-Zmir-opt-level=0
# -Zdump-mir=PreCodegen -Zmir-include-spans=yes`, as Peeky builds it; the comment lines of
# its constants left out).
SOURCE = """use std::cell::Cell;
use std::env;
use std::rc::Rc;
use std::sync::mpsc;

#[inline(never)]
fn other() -> u32 {
    1
}

struct Counter {
    c: Cell<u32>,
}

impl Counter {
    #[inline(never)]
    fn new() -> Counter {
        Counter { c: Cell::new(0) }
    }
}

struct Point {
    a: u32,
    v: Vec<u32>,
}

#[inline(never)]
fn point() -> Point {
    Point { a: 3, v: vec![1, 2] }
}

#[inline(never)]
fn send(tx: &mpsc::Sender<u32>, x: u32) {
    tx.send(x).unwrap();
}

#[inline(never)]
fn bump(c: &Counter, x: u32) {
    c.c.set(x);
}

#[inline(never)]
fn look<T>(_t: &T, x: u32) {
    std::hint::black_box(x);
}

#[inline(never)]
fn put(c: &Rc<Cell<u32>>, x: u32) {
    c.set(x);
}

fn sent(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let (tx, rx) = mpsc::channel();
    send(&tx, x);
    let k = other(); // SENT
    std::hint::black_box(&x);
    rx.recv().unwrap() + k
}

fn counted(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let c = Counter::new();
    bump(&c, x);
    let k = other(); // COUNTED
    std::hint::black_box(&x);
    c.c.get() + k
}

fn pointed(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let p = point();
    look(&p, x);
    let k = other(); // POINTED
    p.a + p.v[0] + *std::hint::black_box(&x) + k
}

fn cloned(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let a = Rc::new(Cell::new(0));
    let b = Rc::clone(&a);
    put(&a, x);
    let k = other(); // CLONED
    std::hint::black_box(&x);
    b.get() + k
}

fn peeked(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let (tx, rx) = mpsc::channel::<u32>();
    tx.send(2).unwrap();
    look(&rx, x);
    let k = other(); // PEEKED
    rx.recv().unwrap() + *std::hint::black_box(&x) + k
}

fn main() {
    let x: u32 = env::args().nth(1).unwrap().parse().unwrap();
    println!("SENT {}", sent(x));
    println!("COUNTED {}", counted(x));
    println!("POINTED {}", pointed(x));
    println!("CLONED {}", cloned(x));
    println!("PEEKED {}", peeked(x));
}
"""

SENT = """// MIR for `sent` after PreCodegen

fn sent(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:52:9: 52:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:52:22: 52:25
    let _2: u32;                         // in scope 0 at src/main.rs:53:9: 53:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:53:36: 53:39
    let mut _6: (std::sync::mpsc::Sender<u32>, std::sync::mpsc::Receiver<u32>); // in scope 0 at src/main.rs:54:20: 54:35
    let _7: ();                          // in scope 0 at src/main.rs:55:5: 55:17
    let mut _8: &std::sync::mpsc::Sender<u32>; // in scope 0 at src/main.rs:55:10: 55:13
    let _9: &std::sync::mpsc::Sender<u32>; // in scope 0 at src/main.rs:55:10: 55:13
    let mut _10: u32;                    // in scope 0 at src/main.rs:55:15: 55:16
    let _12: &u32;                       // in scope 0 at src/main.rs:57:5: 57:29
    let mut _13: &u32;                   // in scope 0 at src/main.rs:57:26: 57:28
    let mut _14: u32;                    // in scope 0 at src/main.rs:58:5: 58:23
    let mut _15: std::result::Result<u32, std::sync::mpmc::RecvError>; // in scope 0 at src/main.rs:58:5: 58:14
    let mut _16: &std::sync::mpsc::Receiver<u32>; // in scope 0 at src/main.rs:58:5: 58:7
    let mut _17: u32;                    // in scope 0 at src/main.rs:58:26: 58:27
    let mut _18: (u32, bool);            // in scope 0 at src/main.rs:58:5: 58:27
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:53:9: 53:10
        let _4: std::sync::mpsc::Sender<u32>; // in scope 1 at src/main.rs:54:10: 54:12
        let _5: std::sync::mpsc::Receiver<u32>; // in scope 1 at src/main.rs:54:14: 54:16
        scope 2 {
            debug tx => _4;              // in scope 2 at src/main.rs:54:10: 54:12
            debug rx => _5;              // in scope 2 at src/main.rs:54:14: 54:16
            let _11: u32;                // in scope 2 at src/main.rs:56:9: 56:10
            scope 3 {
                debug k => _11;          // in scope 3 at src/main.rs:56:9: 56:10
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:53:9: 53:10
        StorageLive(_3);                 // scope 0 at src/main.rs:53:36: 53:39
        _3 = copy _1;                    // scope 0 at src/main.rs:53:36: 53:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:53:13: 53:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:53:39: 53:40
        StorageLive(_6);                 // scope 1 at src/main.rs:54:20: 54:35
        _6 = std::sync::mpsc::channel::<u32>() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:54:20: 54:35
    }

    bb2: {
        StorageLive(_4);                 // scope 1 at src/main.rs:54:10: 54:12
        _4 = move (_6.0: std::sync::mpsc::Sender<u32>); // scope 1 at src/main.rs:54:10: 54:12
        StorageLive(_5);                 // scope 1 at src/main.rs:54:14: 54:16
        _5 = move (_6.1: std::sync::mpsc::Receiver<u32>); // scope 1 at src/main.rs:54:14: 54:16
        StorageDead(_6);                 // scope 1 at src/main.rs:54:35: 54:36
        StorageLive(_7);                 // scope 2 at src/main.rs:55:5: 55:17
        StorageLive(_8);                 // scope 2 at src/main.rs:55:10: 55:13
        StorageLive(_9);                 // scope 2 at src/main.rs:55:10: 55:13
        _9 = &_4;                        // scope 2 at src/main.rs:55:10: 55:13
        _8 = &(*_9);                     // scope 2 at src/main.rs:55:10: 55:13
        StorageLive(_10);                // scope 2 at src/main.rs:55:15: 55:16
        _10 = copy _2;                   // scope 2 at src/main.rs:55:15: 55:16
        _7 = send(move _8, move _10) -> [return: bb3, unwind: bb11]; // scope 2 at src/main.rs:55:5: 55:17
    }

    bb3: {
        StorageDead(_10);                // scope 2 at src/main.rs:55:16: 55:17
        StorageDead(_8);                 // scope 2 at src/main.rs:55:16: 55:17
        StorageDead(_9);                 // scope 2 at src/main.rs:55:17: 55:18
        StorageDead(_7);                 // scope 2 at src/main.rs:55:17: 55:18
        StorageLive(_11);                // scope 2 at src/main.rs:56:9: 56:10
        _11 = other() -> [return: bb4, unwind: bb11]; // scope 2 at src/main.rs:56:13: 56:20
    }

    bb4: {
        StorageLive(_12);                // scope 3 at src/main.rs:57:5: 57:29
        StorageLive(_13);                // scope 3 at src/main.rs:57:26: 57:28
        _13 = &_2;                       // scope 3 at src/main.rs:57:26: 57:28
        _12 = std::hint::black_box::<&u32>(move _13) -> [return: bb5, unwind: bb11]; // scope 3 at src/main.rs:57:5: 57:29
    }

    bb5: {
        StorageDead(_13);                // scope 3 at src/main.rs:57:28: 57:29
        StorageDead(_12);                // scope 3 at src/main.rs:57:29: 57:30
        StorageLive(_14);                // scope 3 at src/main.rs:58:5: 58:23
        StorageLive(_15);                // scope 3 at src/main.rs:58:5: 58:14
        StorageLive(_16);                // scope 3 at src/main.rs:58:5: 58:7
        _16 = &_5;                       // scope 3 at src/main.rs:58:5: 58:7
        _15 = std::sync::mpsc::Receiver::<u32>::recv(move _16) -> [return: bb6, unwind: bb11]; // scope 3 at src/main.rs:58:5: 58:14
    }

    bb6: {
        StorageDead(_16);                // scope 3 at src/main.rs:58:13: 58:14
        _14 = Result::<u32, RecvError>::unwrap(move _15) -> [return: bb7, unwind: bb11]; // scope 3 at src/main.rs:58:5: 58:23
    }

    bb7: {
        StorageDead(_15);                // scope 3 at src/main.rs:58:22: 58:23
        StorageLive(_17);                // scope 3 at src/main.rs:58:26: 58:27
        _17 = copy _11;                  // scope 3 at src/main.rs:58:26: 58:27
        _18 = AddWithOverflow(copy _14, copy _17); // scope 3 at src/main.rs:58:5: 58:27
        assert(!move (_18.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _17) -> [success: bb8, unwind: bb11]; // scope 3 at src/main.rs:58:5: 58:27
    }

    bb8: {
        _0 = move (_18.0: u32);          // scope 3 at src/main.rs:58:5: 58:27
        StorageDead(_17);                // scope 3 at src/main.rs:58:26: 58:27
        StorageDead(_14);                // scope 3 at src/main.rs:58:26: 58:27
        StorageDead(_11);                // scope 2 at src/main.rs:59:1: 59:2
        drop(_5) -> [return: bb9, unwind: bb12]; // scope 1 at src/main.rs:59:1: 59:2
    }

    bb9: {
        StorageDead(_5);                 // scope 1 at src/main.rs:59:1: 59:2
        drop(_4) -> [return: bb10, unwind: bb13]; // scope 1 at src/main.rs:59:1: 59:2
    }

    bb10: {
        StorageDead(_4);                 // scope 1 at src/main.rs:59:1: 59:2
        StorageDead(_2);                 // scope 0 at src/main.rs:59:1: 59:2
        return;                          // scope 0 at src/main.rs:59:2: 59:2
    }

    bb11 (cleanup): {
        drop(_5) -> [return: bb12, unwind terminate(cleanup)]; // scope 1 at src/main.rs:59:1: 59:2
    }

    bb12 (cleanup): {
        drop(_4) -> [return: bb13, unwind terminate(cleanup)]; // scope 1 at src/main.rs:59:1: 59:2
    }

    bb13 (cleanup): {
        resume;                          // scope 0 at src/main.rs:52:1: 59:2
    }
}
"""

COUNTED = """// MIR for `counted` after PreCodegen

fn counted(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:61:12: 61:15
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:61:25: 61:28
    let _2: u32;                         // in scope 0 at src/main.rs:62:9: 62:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:62:36: 62:39
    let _5: ();                          // in scope 0 at src/main.rs:64:5: 64:16
    let mut _6: &Counter;                // in scope 0 at src/main.rs:64:10: 64:12
    let _7: &Counter;                    // in scope 0 at src/main.rs:64:10: 64:12
    let mut _8: u32;                     // in scope 0 at src/main.rs:64:14: 64:15
    let _10: &u32;                       // in scope 0 at src/main.rs:66:5: 66:29
    let mut _11: &u32;                   // in scope 0 at src/main.rs:66:26: 66:28
    let mut _12: u32;                    // in scope 0 at src/main.rs:67:5: 67:14
    let mut _13: &std::cell::Cell<u32>;  // in scope 0 at src/main.rs:67:5: 67:8
    let mut _14: u32;                    // in scope 0 at src/main.rs:67:17: 67:18
    let mut _15: (u32, bool);            // in scope 0 at src/main.rs:67:5: 67:18
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:62:9: 62:10
        let _4: Counter;                 // in scope 1 at src/main.rs:63:9: 63:10
        scope 2 {
            debug c => _4;               // in scope 2 at src/main.rs:63:9: 63:10
            let _9: u32;                 // in scope 2 at src/main.rs:65:9: 65:10
            scope 3 {
                debug k => _9;           // in scope 3 at src/main.rs:65:9: 65:10
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:62:9: 62:10
        StorageLive(_3);                 // scope 0 at src/main.rs:62:36: 62:39
        _3 = copy _1;                    // scope 0 at src/main.rs:62:36: 62:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:62:13: 62:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:62:39: 62:40
        StorageLive(_4);                 // scope 1 at src/main.rs:63:9: 63:10
        _4 = Counter::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:63:13: 63:27
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:64:5: 64:16
        StorageLive(_6);                 // scope 2 at src/main.rs:64:10: 64:12
        StorageLive(_7);                 // scope 2 at src/main.rs:64:10: 64:12
        _7 = &_4;                        // scope 2 at src/main.rs:64:10: 64:12
        _6 = &(*_7);                     // scope 2 at src/main.rs:64:10: 64:12
        StorageLive(_8);                 // scope 2 at src/main.rs:64:14: 64:15
        _8 = copy _2;                    // scope 2 at src/main.rs:64:14: 64:15
        _5 = bump(move _6, move _8) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:64:5: 64:16
    }

    bb3: {
        StorageDead(_8);                 // scope 2 at src/main.rs:64:15: 64:16
        StorageDead(_6);                 // scope 2 at src/main.rs:64:15: 64:16
        StorageDead(_7);                 // scope 2 at src/main.rs:64:16: 64:17
        StorageDead(_5);                 // scope 2 at src/main.rs:64:16: 64:17
        StorageLive(_9);                 // scope 2 at src/main.rs:65:9: 65:10
        _9 = other() -> [return: bb4, unwind continue]; // scope 2 at src/main.rs:65:13: 65:20
    }

    bb4: {
        StorageLive(_10);                // scope 3 at src/main.rs:66:5: 66:29
        StorageLive(_11);                // scope 3 at src/main.rs:66:26: 66:28
        _11 = &_2;                       // scope 3 at src/main.rs:66:26: 66:28
        _10 = std::hint::black_box::<&u32>(move _11) -> [return: bb5, unwind continue]; // scope 3 at src/main.rs:66:5: 66:29
    }

    bb5: {
        StorageDead(_11);                // scope 3 at src/main.rs:66:28: 66:29
        StorageDead(_10);                // scope 3 at src/main.rs:66:29: 66:30
        StorageLive(_12);                // scope 3 at src/main.rs:67:5: 67:14
        StorageLive(_13);                // scope 3 at src/main.rs:67:5: 67:8
        _13 = &(_4.0: std::cell::Cell<u32>); // scope 3 at src/main.rs:67:5: 67:8
        _12 = Cell::<u32>::get(move _13) -> [return: bb6, unwind continue]; // scope 3 at src/main.rs:67:5: 67:14
    }

    bb6: {
        StorageDead(_13);                // scope 3 at src/main.rs:67:13: 67:14
        StorageLive(_14);                // scope 3 at src/main.rs:67:17: 67:18
        _14 = copy _9;                   // scope 3 at src/main.rs:67:17: 67:18
        _15 = AddWithOverflow(copy _12, copy _14); // scope 3 at src/main.rs:67:5: 67:18
        assert(!move (_15.1: bool), "attempt to compute `{} + {}`, which would overflow", move _12, move _14) -> [success: bb7, unwind continue]; // scope 3 at src/main.rs:67:5: 67:18
    }

    bb7: {
        _0 = move (_15.0: u32);          // scope 3 at src/main.rs:67:5: 67:18
        StorageDead(_14);                // scope 3 at src/main.rs:67:17: 67:18
        StorageDead(_12);                // scope 3 at src/main.rs:67:17: 67:18
        StorageDead(_9);                 // scope 2 at src/main.rs:68:1: 68:2
        StorageDead(_4);                 // scope 1 at src/main.rs:68:1: 68:2
        StorageDead(_2);                 // scope 0 at src/main.rs:68:1: 68:2
        return;                          // scope 0 at src/main.rs:68:2: 68:2
    }
}
"""

POINTED = """// MIR for `pointed` after PreCodegen

fn pointed(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:70:12: 70:15
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:70:25: 70:28
    let _2: u32;                         // in scope 0 at src/main.rs:71:9: 71:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:71:36: 71:39
    let _5: ();                          // in scope 0 at src/main.rs:73:5: 73:16
    let mut _6: &Point;                  // in scope 0 at src/main.rs:73:10: 73:12
    let _7: &Point;                      // in scope 0 at src/main.rs:73:10: 73:12
    let mut _8: u32;                     // in scope 0 at src/main.rs:73:14: 73:15
    let mut _10: u32;                    // in scope 0 at src/main.rs:75:5: 75:45
    let mut _11: u32;                    // in scope 0 at src/main.rs:75:5: 75:17
    let mut _12: u32;                    // in scope 0 at src/main.rs:75:5: 75:8
    let mut _13: u32;                    // in scope 0 at src/main.rs:75:11: 75:17
    let mut _14: &u32;                   // in scope 0 at src/main.rs:75:14: 75:17
    let mut _15: &std::vec::Vec<u32>;    // in scope 0 at src/main.rs:75:11: 75:14
    let mut _16: (u32, bool);            // in scope 0 at src/main.rs:75:5: 75:17
    let mut _17: u32;                    // in scope 0 at src/main.rs:75:20: 75:45
    let mut _18: &u32;                   // in scope 0 at src/main.rs:75:21: 75:45
    let mut _19: &u32;                   // in scope 0 at src/main.rs:75:42: 75:44
    let mut _20: (u32, bool);            // in scope 0 at src/main.rs:75:5: 75:45
    let mut _21: u32;                    // in scope 0 at src/main.rs:75:48: 75:49
    let mut _22: (u32, bool);            // in scope 0 at src/main.rs:75:5: 75:49
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:71:9: 71:10
        let _4: Point;                   // in scope 1 at src/main.rs:72:9: 72:10
        scope 2 {
            debug p => _4;               // in scope 2 at src/main.rs:72:9: 72:10
            let _9: u32;                 // in scope 2 at src/main.rs:74:9: 74:10
            scope 3 {
                debug k => _9;           // in scope 3 at src/main.rs:74:9: 74:10
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:71:9: 71:10
        StorageLive(_3);                 // scope 0 at src/main.rs:71:36: 71:39
        _3 = copy _1;                    // scope 0 at src/main.rs:71:36: 71:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:71:13: 71:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:71:39: 71:40
        StorageLive(_4);                 // scope 1 at src/main.rs:72:9: 72:10
        _4 = point() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:72:13: 72:20
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:73:5: 73:16
        StorageLive(_6);                 // scope 2 at src/main.rs:73:10: 73:12
        StorageLive(_7);                 // scope 2 at src/main.rs:73:10: 73:12
        _7 = &_4;                        // scope 2 at src/main.rs:73:10: 73:12
        _6 = &(*_7);                     // scope 2 at src/main.rs:73:10: 73:12
        StorageLive(_8);                 // scope 2 at src/main.rs:73:14: 73:15
        _8 = copy _2;                    // scope 2 at src/main.rs:73:14: 73:15
        _5 = look::<Point>(move _6, move _8) -> [return: bb3, unwind: bb11]; // scope 2 at src/main.rs:73:5: 73:16
    }

    bb3: {
        StorageDead(_8);                 // scope 2 at src/main.rs:73:15: 73:16
        StorageDead(_6);                 // scope 2 at src/main.rs:73:15: 73:16
        StorageDead(_7);                 // scope 2 at src/main.rs:73:16: 73:17
        StorageDead(_5);                 // scope 2 at src/main.rs:73:16: 73:17
        StorageLive(_9);                 // scope 2 at src/main.rs:74:9: 74:10
        _9 = other() -> [return: bb4, unwind: bb11]; // scope 2 at src/main.rs:74:13: 74:20
    }

    bb4: {
        StorageLive(_10);                // scope 3 at src/main.rs:75:5: 75:45
        StorageLive(_11);                // scope 3 at src/main.rs:75:5: 75:17
        StorageLive(_12);                // scope 3 at src/main.rs:75:5: 75:8
        _12 = copy (_4.0: u32);          // scope 3 at src/main.rs:75:5: 75:8
        StorageLive(_13);                // scope 3 at src/main.rs:75:11: 75:17
        StorageLive(_14);                // scope 3 at src/main.rs:75:14: 75:17
        StorageLive(_15);                // scope 3 at src/main.rs:75:11: 75:14
        _15 = &(_4.1: std::vec::Vec<u32>); // scope 3 at src/main.rs:75:11: 75:14
        _14 = <Vec<u32> as Index<usize>>::index(move _15, const 0_usize) -> [return: bb5, unwind: bb11]; // scope 3 at src/main.rs:75:14: 75:17
    }

    bb5: {
        _13 = copy (*_14);               // scope 3 at src/main.rs:75:11: 75:17
        StorageDead(_15);                // scope 3 at src/main.rs:75:16: 75:17
        _16 = AddWithOverflow(copy _12, copy _13); // scope 3 at src/main.rs:75:5: 75:17
        assert(!move (_16.1: bool), "attempt to compute `{} + {}`, which would overflow", move _12, move _13) -> [success: bb6, unwind: bb11]; // scope 3 at src/main.rs:75:5: 75:17
    }

    bb6: {
        _11 = move (_16.0: u32);         // scope 3 at src/main.rs:75:5: 75:17
        StorageDead(_13);                // scope 3 at src/main.rs:75:16: 75:17
        StorageDead(_12);                // scope 3 at src/main.rs:75:16: 75:17
        StorageLive(_17);                // scope 3 at src/main.rs:75:20: 75:45
        StorageLive(_18);                // scope 3 at src/main.rs:75:21: 75:45
        StorageLive(_19);                // scope 3 at src/main.rs:75:42: 75:44
        _19 = &_2;                       // scope 3 at src/main.rs:75:42: 75:44
        _18 = std::hint::black_box::<&u32>(move _19) -> [return: bb7, unwind: bb11]; // scope 3 at src/main.rs:75:21: 75:45
    }

    bb7: {
        StorageDead(_19);                // scope 3 at src/main.rs:75:44: 75:45
        _17 = copy (*_18);               // scope 3 at src/main.rs:75:20: 75:45
        _20 = AddWithOverflow(copy _11, copy _17); // scope 3 at src/main.rs:75:5: 75:45
        assert(!move (_20.1: bool), "attempt to compute `{} + {}`, which would overflow", move _11, move _17) -> [success: bb8, unwind: bb11]; // scope 3 at src/main.rs:75:5: 75:45
    }

    bb8: {
        _10 = move (_20.0: u32);         // scope 3 at src/main.rs:75:5: 75:45
        StorageDead(_17);                // scope 3 at src/main.rs:75:44: 75:45
        StorageDead(_11);                // scope 3 at src/main.rs:75:44: 75:45
        StorageLive(_21);                // scope 3 at src/main.rs:75:48: 75:49
        _21 = copy _9;                   // scope 3 at src/main.rs:75:48: 75:49
        _22 = AddWithOverflow(copy _10, copy _21); // scope 3 at src/main.rs:75:5: 75:49
        assert(!move (_22.1: bool), "attempt to compute `{} + {}`, which would overflow", move _10, move _21) -> [success: bb9, unwind: bb11]; // scope 3 at src/main.rs:75:5: 75:49
    }

    bb9: {
        _0 = move (_22.0: u32);          // scope 3 at src/main.rs:75:5: 75:49
        StorageDead(_21);                // scope 3 at src/main.rs:75:48: 75:49
        StorageDead(_18);                // scope 3 at src/main.rs:75:48: 75:49
        StorageDead(_14);                // scope 3 at src/main.rs:75:48: 75:49
        StorageDead(_10);                // scope 3 at src/main.rs:75:48: 75:49
        StorageDead(_9);                 // scope 2 at src/main.rs:76:1: 76:2
        drop(_4) -> [return: bb10, unwind: bb12]; // scope 1 at src/main.rs:76:1: 76:2
    }

    bb10: {
        StorageDead(_4);                 // scope 1 at src/main.rs:76:1: 76:2
        StorageDead(_2);                 // scope 0 at src/main.rs:76:1: 76:2
        return;                          // scope 0 at src/main.rs:76:2: 76:2
    }

    bb11 (cleanup): {
        drop(_4) -> [return: bb12, unwind terminate(cleanup)]; // scope 1 at src/main.rs:76:1: 76:2
    }

    bb12 (cleanup): {
        resume;                          // scope 0 at src/main.rs:70:1: 76:2
    }
}
"""

CLONED = """// MIR for `cloned` after PreCodegen

fn cloned(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:78:11: 78:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:78:24: 78:27
    let _2: u32;                         // in scope 0 at src/main.rs:79:9: 79:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:79:36: 79:39
    let mut _5: std::cell::Cell<u32>;    // in scope 0 at src/main.rs:80:21: 80:33
    let mut _7: &std::rc::Rc<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:81:23: 81:25
    let _8: &std::rc::Rc<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:81:23: 81:25
    let _9: ();                          // in scope 0 at src/main.rs:82:5: 82:15
    let mut _10: &std::rc::Rc<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:82:9: 82:11
    let _11: &std::rc::Rc<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:82:9: 82:11
    let mut _12: u32;                    // in scope 0 at src/main.rs:82:13: 82:14
    let _14: &u32;                       // in scope 0 at src/main.rs:84:5: 84:29
    let mut _15: &u32;                   // in scope 0 at src/main.rs:84:26: 84:28
    let mut _16: u32;                    // in scope 0 at src/main.rs:85:5: 85:12
    let mut _17: &std::cell::Cell<u32>;  // in scope 0 at src/main.rs:85:5: 85:6
    let _18: &std::cell::Cell<u32>;      // in scope 0 at src/main.rs:85:5: 85:12
    let mut _19: &std::rc::Rc<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:85:5: 85:6
    let mut _20: u32;                    // in scope 0 at src/main.rs:85:15: 85:16
    let mut _21: (u32, bool);            // in scope 0 at src/main.rs:85:5: 85:16
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:79:9: 79:10
        let _4: std::rc::Rc<std::cell::Cell<u32>>; // in scope 1 at src/main.rs:80:9: 80:10
        scope 2 {
            debug a => _4;               // in scope 2 at src/main.rs:80:9: 80:10
            let _6: std::rc::Rc<std::cell::Cell<u32>>; // in scope 2 at src/main.rs:81:9: 81:10
            scope 3 {
                debug b => _6;           // in scope 3 at src/main.rs:81:9: 81:10
                let _13: u32;            // in scope 3 at src/main.rs:83:9: 83:10
                scope 4 {
                    debug k => _13;      // in scope 4 at src/main.rs:83:9: 83:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:79:9: 79:10
        StorageLive(_3);                 // scope 0 at src/main.rs:79:36: 79:39
        _3 = copy _1;                    // scope 0 at src/main.rs:79:36: 79:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:79:13: 79:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:79:39: 79:40
        StorageLive(_4);                 // scope 1 at src/main.rs:80:9: 80:10
        StorageLive(_5);                 // scope 1 at src/main.rs:80:21: 80:33
        _5 = Cell::<u32>::new(const 0_u32) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:80:21: 80:33
    }

    bb2: {
        _4 = Rc::<Cell<u32>>::new(move _5) -> [return: bb3, unwind continue]; // scope 1 at src/main.rs:80:13: 80:34
    }

    bb3: {
        StorageDead(_5);                 // scope 1 at src/main.rs:80:33: 80:34
        StorageLive(_6);                 // scope 2 at src/main.rs:81:9: 81:10
        StorageLive(_7);                 // scope 2 at src/main.rs:81:23: 81:25
        StorageLive(_8);                 // scope 2 at src/main.rs:81:23: 81:25
        _8 = &_4;                        // scope 2 at src/main.rs:81:23: 81:25
        _7 = &(*_8);                     // scope 2 at src/main.rs:81:23: 81:25
        _6 = <Rc<Cell<u32>> as Clone>::clone(move _7) -> [return: bb4, unwind: bb14]; // scope 2 at src/main.rs:81:13: 81:26
    }

    bb4: {
        StorageDead(_7);                 // scope 2 at src/main.rs:81:25: 81:26
        StorageDead(_8);                 // scope 2 at src/main.rs:81:26: 81:27
        StorageLive(_9);                 // scope 3 at src/main.rs:82:5: 82:15
        StorageLive(_10);                // scope 3 at src/main.rs:82:9: 82:11
        StorageLive(_11);                // scope 3 at src/main.rs:82:9: 82:11
        _11 = &_4;                       // scope 3 at src/main.rs:82:9: 82:11
        _10 = &(*_11);                   // scope 3 at src/main.rs:82:9: 82:11
        StorageLive(_12);                // scope 3 at src/main.rs:82:13: 82:14
        _12 = copy _2;                   // scope 3 at src/main.rs:82:13: 82:14
        _9 = put(move _10, move _12) -> [return: bb5, unwind: bb13]; // scope 3 at src/main.rs:82:5: 82:15
    }

    bb5: {
        StorageDead(_12);                // scope 3 at src/main.rs:82:14: 82:15
        StorageDead(_10);                // scope 3 at src/main.rs:82:14: 82:15
        StorageDead(_11);                // scope 3 at src/main.rs:82:15: 82:16
        StorageDead(_9);                 // scope 3 at src/main.rs:82:15: 82:16
        StorageLive(_13);                // scope 3 at src/main.rs:83:9: 83:10
        _13 = other() -> [return: bb6, unwind: bb13]; // scope 3 at src/main.rs:83:13: 83:20
    }

    bb6: {
        StorageLive(_14);                // scope 4 at src/main.rs:84:5: 84:29
        StorageLive(_15);                // scope 4 at src/main.rs:84:26: 84:28
        _15 = &_2;                       // scope 4 at src/main.rs:84:26: 84:28
        _14 = std::hint::black_box::<&u32>(move _15) -> [return: bb7, unwind: bb13]; // scope 4 at src/main.rs:84:5: 84:29
    }

    bb7: {
        StorageDead(_15);                // scope 4 at src/main.rs:84:28: 84:29
        StorageDead(_14);                // scope 4 at src/main.rs:84:29: 84:30
        StorageLive(_16);                // scope 4 at src/main.rs:85:5: 85:12
        StorageLive(_17);                // scope 4 at src/main.rs:85:5: 85:6
        StorageLive(_18);                // scope 4 at src/main.rs:85:5: 85:12
        StorageLive(_19);                // scope 4 at src/main.rs:85:5: 85:6
        _19 = &_6;                       // scope 4 at src/main.rs:85:5: 85:6
        _18 = <Rc<Cell<u32>> as Deref>::deref(move _19) -> [return: bb8, unwind: bb13]; // scope 4 at src/main.rs:85:5: 85:12
    }

    bb8: {
        _17 = &(*_18);                   // scope 4 at src/main.rs:85:5: 85:6
        StorageDead(_19);                // scope 4 at src/main.rs:85:5: 85:6
        _16 = Cell::<u32>::get(move _17) -> [return: bb9, unwind: bb13]; // scope 4 at src/main.rs:85:5: 85:12
    }

    bb9: {
        StorageDead(_17);                // scope 4 at src/main.rs:85:11: 85:12
        StorageLive(_20);                // scope 4 at src/main.rs:85:15: 85:16
        _20 = copy _13;                  // scope 4 at src/main.rs:85:15: 85:16
        _21 = AddWithOverflow(copy _16, copy _20); // scope 4 at src/main.rs:85:5: 85:16
        assert(!move (_21.1: bool), "attempt to compute `{} + {}`, which would overflow", move _16, move _20) -> [success: bb10, unwind: bb13]; // scope 4 at src/main.rs:85:5: 85:16
    }

    bb10: {
        _0 = move (_21.0: u32);          // scope 4 at src/main.rs:85:5: 85:16
        StorageDead(_20);                // scope 4 at src/main.rs:85:15: 85:16
        StorageDead(_18);                // scope 4 at src/main.rs:85:15: 85:16
        StorageDead(_16);                // scope 4 at src/main.rs:85:15: 85:16
        StorageDead(_13);                // scope 3 at src/main.rs:86:1: 86:2
        drop(_6) -> [return: bb11, unwind: bb14]; // scope 2 at src/main.rs:86:1: 86:2
    }

    bb11: {
        StorageDead(_6);                 // scope 2 at src/main.rs:86:1: 86:2
        drop(_4) -> [return: bb12, unwind: bb15]; // scope 1 at src/main.rs:86:1: 86:2
    }

    bb12: {
        StorageDead(_4);                 // scope 1 at src/main.rs:86:1: 86:2
        StorageDead(_2);                 // scope 0 at src/main.rs:86:1: 86:2
        return;                          // scope 0 at src/main.rs:86:2: 86:2
    }

    bb13 (cleanup): {
        drop(_6) -> [return: bb14, unwind terminate(cleanup)]; // scope 2 at src/main.rs:86:1: 86:2
    }

    bb14 (cleanup): {
        drop(_4) -> [return: bb15, unwind terminate(cleanup)]; // scope 1 at src/main.rs:86:1: 86:2
    }

    bb15 (cleanup): {
        resume;                          // scope 0 at src/main.rs:78:1: 86:2
    }
}
"""

PEEKED = """// MIR for `peeked` after PreCodegen

fn peeked(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:88:11: 88:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:88:24: 88:27
    let _2: u32;                         // in scope 0 at src/main.rs:89:9: 89:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:89:36: 89:39
    let mut _6: (std::sync::mpsc::Sender<u32>, std::sync::mpsc::Receiver<u32>); // in scope 0 at src/main.rs:90:20: 90:42
    let _7: ();                          // in scope 0 at src/main.rs:91:5: 91:24
    let mut _8: std::result::Result<(), std::sync::mpmc::SendError<u32>>; // in scope 0 at src/main.rs:91:5: 91:15
    let mut _9: &std::sync::mpsc::Sender<u32>; // in scope 0 at src/main.rs:91:5: 91:7
    let _10: ();                         // in scope 0 at src/main.rs:92:5: 92:17
    let mut _11: &std::sync::mpsc::Receiver<u32>; // in scope 0 at src/main.rs:92:10: 92:13
    let _12: &std::sync::mpsc::Receiver<u32>; // in scope 0 at src/main.rs:92:10: 92:13
    let mut _13: u32;                    // in scope 0 at src/main.rs:92:15: 92:16
    let mut _15: u32;                    // in scope 0 at src/main.rs:94:5: 94:51
    let mut _16: u32;                    // in scope 0 at src/main.rs:94:5: 94:23
    let mut _17: std::result::Result<u32, std::sync::mpmc::RecvError>; // in scope 0 at src/main.rs:94:5: 94:14
    let mut _18: &std::sync::mpsc::Receiver<u32>; // in scope 0 at src/main.rs:94:5: 94:7
    let mut _19: u32;                    // in scope 0 at src/main.rs:94:26: 94:51
    let mut _20: &u32;                   // in scope 0 at src/main.rs:94:27: 94:51
    let mut _21: &u32;                   // in scope 0 at src/main.rs:94:48: 94:50
    let mut _22: (u32, bool);            // in scope 0 at src/main.rs:94:5: 94:51
    let mut _23: u32;                    // in scope 0 at src/main.rs:94:54: 94:55
    let mut _24: (u32, bool);            // in scope 0 at src/main.rs:94:5: 94:55
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:89:9: 89:10
        let _4: std::sync::mpsc::Sender<u32>; // in scope 1 at src/main.rs:90:10: 90:12
        let _5: std::sync::mpsc::Receiver<u32>; // in scope 1 at src/main.rs:90:14: 90:16
        scope 2 {
            debug tx => _4;              // in scope 2 at src/main.rs:90:10: 90:12
            debug rx => _5;              // in scope 2 at src/main.rs:90:14: 90:16
            let _14: u32;                // in scope 2 at src/main.rs:93:9: 93:10
            scope 3 {
                debug k => _14;          // in scope 3 at src/main.rs:93:9: 93:10
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:89:9: 89:10
        StorageLive(_3);                 // scope 0 at src/main.rs:89:36: 89:39
        _3 = copy _1;                    // scope 0 at src/main.rs:89:36: 89:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:89:13: 89:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:89:39: 89:40
        StorageLive(_6);                 // scope 1 at src/main.rs:90:20: 90:42
        _6 = std::sync::mpsc::channel::<u32>() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:90:20: 90:42
    }

    bb2: {
        StorageLive(_4);                 // scope 1 at src/main.rs:90:10: 90:12
        _4 = move (_6.0: std::sync::mpsc::Sender<u32>); // scope 1 at src/main.rs:90:10: 90:12
        StorageLive(_5);                 // scope 1 at src/main.rs:90:14: 90:16
        _5 = move (_6.1: std::sync::mpsc::Receiver<u32>); // scope 1 at src/main.rs:90:14: 90:16
        StorageDead(_6);                 // scope 1 at src/main.rs:90:42: 90:43
        StorageLive(_7);                 // scope 2 at src/main.rs:91:5: 91:24
        StorageLive(_8);                 // scope 2 at src/main.rs:91:5: 91:15
        StorageLive(_9);                 // scope 2 at src/main.rs:91:5: 91:7
        _9 = &_4;                        // scope 2 at src/main.rs:91:5: 91:7
        _8 = std::sync::mpsc::Sender::<u32>::send(move _9, const 2_u32) -> [return: bb3, unwind: bb14]; // scope 2 at src/main.rs:91:5: 91:15
    }

    bb3: {
        StorageDead(_9);                 // scope 2 at src/main.rs:91:14: 91:15
        _7 = Result::<(), SendError<u32>>::unwrap(move _8) -> [return: bb4, unwind: bb14]; // scope 2 at src/main.rs:91:5: 91:24
    }

    bb4: {
        StorageDead(_8);                 // scope 2 at src/main.rs:91:23: 91:24
        StorageDead(_7);                 // scope 2 at src/main.rs:91:24: 91:25
        StorageLive(_10);                // scope 2 at src/main.rs:92:5: 92:17
        StorageLive(_11);                // scope 2 at src/main.rs:92:10: 92:13
        StorageLive(_12);                // scope 2 at src/main.rs:92:10: 92:13
        _12 = &_5;                       // scope 2 at src/main.rs:92:10: 92:13
        _11 = &(*_12);                   // scope 2 at src/main.rs:92:10: 92:13
        StorageLive(_13);                // scope 2 at src/main.rs:92:15: 92:16
        _13 = copy _2;                   // scope 2 at src/main.rs:92:15: 92:16
        _10 = look::<std::sync::mpsc::Receiver<u32>>(move _11, move _13) -> [return: bb5, unwind: bb14]; // scope 2 at src/main.rs:92:5: 92:17
    }

    bb5: {
        StorageDead(_13);                // scope 2 at src/main.rs:92:16: 92:17
        StorageDead(_11);                // scope 2 at src/main.rs:92:16: 92:17
        StorageDead(_12);                // scope 2 at src/main.rs:92:17: 92:18
        StorageDead(_10);                // scope 2 at src/main.rs:92:17: 92:18
        StorageLive(_14);                // scope 2 at src/main.rs:93:9: 93:10
        _14 = other() -> [return: bb6, unwind: bb14]; // scope 2 at src/main.rs:93:13: 93:20
    }

    bb6: {
        StorageLive(_15);                // scope 3 at src/main.rs:94:5: 94:51
        StorageLive(_16);                // scope 3 at src/main.rs:94:5: 94:23
        StorageLive(_17);                // scope 3 at src/main.rs:94:5: 94:14
        StorageLive(_18);                // scope 3 at src/main.rs:94:5: 94:7
        _18 = &_5;                       // scope 3 at src/main.rs:94:5: 94:7
        _17 = std::sync::mpsc::Receiver::<u32>::recv(move _18) -> [return: bb7, unwind: bb14]; // scope 3 at src/main.rs:94:5: 94:14
    }

    bb7: {
        StorageDead(_18);                // scope 3 at src/main.rs:94:13: 94:14
        _16 = Result::<u32, RecvError>::unwrap(move _17) -> [return: bb8, unwind: bb14]; // scope 3 at src/main.rs:94:5: 94:23
    }

    bb8: {
        StorageDead(_17);                // scope 3 at src/main.rs:94:22: 94:23
        StorageLive(_19);                // scope 3 at src/main.rs:94:26: 94:51
        StorageLive(_20);                // scope 3 at src/main.rs:94:27: 94:51
        StorageLive(_21);                // scope 3 at src/main.rs:94:48: 94:50
        _21 = &_2;                       // scope 3 at src/main.rs:94:48: 94:50
        _20 = std::hint::black_box::<&u32>(move _21) -> [return: bb9, unwind: bb14]; // scope 3 at src/main.rs:94:27: 94:51
    }

    bb9: {
        StorageDead(_21);                // scope 3 at src/main.rs:94:50: 94:51
        _19 = copy (*_20);               // scope 3 at src/main.rs:94:26: 94:51
        _22 = AddWithOverflow(copy _16, copy _19); // scope 3 at src/main.rs:94:5: 94:51
        assert(!move (_22.1: bool), "attempt to compute `{} + {}`, which would overflow", move _16, move _19) -> [success: bb10, unwind: bb14]; // scope 3 at src/main.rs:94:5: 94:51
    }

    bb10: {
        _15 = move (_22.0: u32);         // scope 3 at src/main.rs:94:5: 94:51
        StorageDead(_19);                // scope 3 at src/main.rs:94:50: 94:51
        StorageDead(_16);                // scope 3 at src/main.rs:94:50: 94:51
        StorageLive(_23);                // scope 3 at src/main.rs:94:54: 94:55
        _23 = copy _14;                  // scope 3 at src/main.rs:94:54: 94:55
        _24 = AddWithOverflow(copy _15, copy _23); // scope 3 at src/main.rs:94:5: 94:55
        assert(!move (_24.1: bool), "attempt to compute `{} + {}`, which would overflow", move _15, move _23) -> [success: bb11, unwind: bb14]; // scope 3 at src/main.rs:94:5: 94:55
    }

    bb11: {
        _0 = move (_24.0: u32);          // scope 3 at src/main.rs:94:5: 94:55
        StorageDead(_23);                // scope 3 at src/main.rs:94:54: 94:55
        StorageDead(_20);                // scope 3 at src/main.rs:94:54: 94:55
        StorageDead(_15);                // scope 3 at src/main.rs:94:54: 94:55
        StorageDead(_14);                // scope 2 at src/main.rs:95:1: 95:2
        drop(_5) -> [return: bb12, unwind: bb15]; // scope 1 at src/main.rs:95:1: 95:2
    }

    bb12: {
        StorageDead(_5);                 // scope 1 at src/main.rs:95:1: 95:2
        drop(_4) -> [return: bb13, unwind: bb16]; // scope 1 at src/main.rs:95:1: 95:2
    }

    bb13: {
        StorageDead(_4);                 // scope 1 at src/main.rs:95:1: 95:2
        StorageDead(_2);                 // scope 0 at src/main.rs:95:1: 95:2
        return;                          // scope 0 at src/main.rs:95:2: 95:2
    }

    bb14 (cleanup): {
        drop(_5) -> [return: bb15, unwind terminate(cleanup)]; // scope 1 at src/main.rs:95:1: 95:2
    }

    bb15 (cleanup): {
        drop(_4) -> [return: bb16, unwind terminate(cleanup)]; // scope 1 at src/main.rs:95:1: 95:2
    }

    bb16 (cleanup): {
        resume;                          // scope 0 at src/main.rs:88:1: 95:2
    }
}
"""

HASH = "h0123456789abcdef"
# What the build's debug information shows the crate's own types hold
# (rust_symbols.interior_types of its application units): `Counter` a `Cell`, `Point` a
# `u32` and a `Vec<u32>`.
INTERIOR = {
    "Counter": (frozenset({"interior", "owned"}), ()),
    "Point": (frozenset(), ()),
}


def body_of(text, interior=INTERIOR):
    """The body of ``text``, as matched to its IR: no type parameters, no `std`, `core`
    or `alloc` of the crate's own, its types holding what ``interior`` says."""
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


def why(text, name, line, statement, interior=INTERIOR, source=None, variable="x"):
    """Why a change to `x` is refused at a stop on ``line`` of ``name``, at its statement
    starting ``statement`` (``mid_statement``'s reason and row), with Peeky's own proofs
    over the MIR and the crate's types holding what ``interior`` says."""
    body = parse_body(text)
    blocks = {}
    for number, block in body.blocks.items():
        lines = {at for _, file, at in block.statements if file == "src/main.rs" and at}
        blocks["start" if number == 0 else f"bb{number}"] = lines
    declared = min(at for _, file, at in body.blocks[0].statements if file == "src/main.rs")
    function = IrFunction(name, "crate", "src/main.rs", blocks, {}, declared - 1)
    texts = {"src/main.rs": SOURCE if source is None else source}
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
    return (None, None) if found is None else (str(found), found.row, found.advice)


def kept(name, lines):
    """The refusal of a binding of the program's own that may keep a copy out of Peeky's
    reach, its row, and its advice."""
    return (
        f"the program's own `{name}` (at {lines}) may hold a copy of it that it goes on with, "
        "which may be out of Peeky's reach (behind a cell, a lock, a channel or a pointer its "
        "type holds), so a change to `x` may never be seen: Peeky proves no read of the changed "
        "part first (a reference lent to a call, or `{:?}`, is not one)",
        f"the program may hold a copy ({lines})",
        "Change it before the program copies it (a stop on an earlier line)",
    )


def test_a_struct_with_a_cell_field_may_store_a_copy_through_a_shared_reference():
    # The issue: `bump(&c, x)` with `c = Counter::new()`. A call built `c`, so nothing it
    # was made from shows the `Cell` in `Counter`; the debug information does.
    body = body_of(COUNTED)
    stop = place(body, "_9 = other()")
    assert body._may_write_through(6)
    assert 4 in body.direct_copies_at(2, stop)
    assert why(COUNTED, "counted", 65, "_9 = other()") == kept("c", "lines 63 and 64")
    # Where the debug information wasn't read, `Counter` may hold a cell: refused too.
    assert why(COUNTED, "counted", 65, "_9 = other()", None)[0] is not None
    # Where it shows no cell, nothing writes `x` into `c`.
    plain = {"Counter": (frozenset(), ())}
    assert not body_of(COUNTED, plain)._may_write_through(6)
    assert why(COUNTED, "counted", 65, "_9 = other()", plain) == (None, None)


def test_a_channels_sending_end_stores_where_its_receiving_end_reads():
    # The issue: `send(&tx, x)` stores `x` in the channel, which `rx.recv()` reads. `rx`
    # was moved out of the tuple `mpsc::channel()` returned beside `tx`.
    body = body_of(SENT)
    stop = place(body, "_11 = other()")
    assert body._may_write_through(8)
    assert body._sharers(8) == {5}
    assert 5 in body.direct_copies_at(2, stop)
    assert why(SENT, "sent", 56, "_11 = other()") == kept("rx", "lines 54 and 55")


def test_an_rc_cloned_before_the_call_shares_the_cell_it_changes():
    # `let b = Rc::clone(&a); put(&a, x);`: `b` reads the cell `put` stores `x` in.
    body = body_of(CLONED)
    stop = place(body, "_13 = other()")
    assert 6 in body.direct_copies_at(2, stop)
    assert why(CLONED, "cloned", 83, "_13 = other()") == kept("b", "lines 81 and 82")


def test_a_struct_without_cells_or_a_receiving_end_lent_by_a_shared_reference_stores_nothing():
    # `look(&p, x)` with `p = point()`, a struct of a `u32` and a `Vec<u32>`, and
    # `look(&rx, x)`: through a `&`, neither can take `x`, so `x` stays settable.
    body = body_of(POINTED)
    assert not body._may_write_through(6)
    assert why(POINTED, "pointed", 74, "_9 = other()") == (None, None)
    body = body_of(PEEKED)
    assert not any(body._may_write_through(n) for n in (10, 11))
    assert why(PEEKED, "peeked", 93, "_14 = other()") == (None, None)
    # Without the debug information, `Point` may hold a cell: refused, as a possibility.
    reason = why(POINTED, "pointed", 74, "_9 = other()", None)[0]
    assert "the program's own `p` (at " in reason and "may hold a copy of it" in reason


def test_each_type_counts_by_what_it_holds():
    layouts = TypeLayouts(INTERIOR)
    for kind, writes in (
        ("&std::sync::mpsc::Sender<u32>", True),
        ("&std::sync::mpsc::SyncSender<u32>", True),
        ("&std::sync::mpsc::Receiver<u32>", False),
        ("&std::sync::mpsc::Iter<'_, u32>", False),
        ("&std::sync::mpsc::Receiver<&mut std::vec::Vec<u32>>", True),
        ("&std::rc::Rc<std::cell::RefCell<u32>>", True),
        ("&std::sync::Arc<std::sync::Mutex<u32>>", True),
        ("&std::rc::Rc<u32>", False),
        ("&std::sync::Arc<std::string::String>", False),
        ("&std::sync::atomic::Atomic<u32>", True),
        ("&std::cell::OnceCell<u32>", True),
        ("&std::sync::OnceLock<u32>", True),
        ("&std::cell::LazyCell<u32>", False),
        ("&std::sync::LazyLock<std::sync::Mutex<u32>>", True),
        ("&std::sync::Condvar", False),
        ("&std::sync::Barrier", False),
        ("&std::thread::LocalKey<std::cell::Cell<u32>>", True),
        ("&std::thread::LocalKey<u32>", False),
        ("&std::collections::HashMap<u32, std::vec::Vec<u32>>", False),
        ("&std::option::Option<std::boxed::Box<std::cell::Cell<u32>>>", True),
        # A standard type on no list may: a refusal says so only as a possibility.
        ("&std::fs::File", True),
        ("&std::boxed::Box<dyn std::fmt::Debug>", True),
        # A function item holds nothing.
        ("&std::iter::Map<std::slice::Iter<'_, u32>, fn(&u32) -> u32 {double}>", False),
        # The short names a function's signature gives (`fn f(_1: &Cell<u32>)`).
        ("&Cell<u32>", True),
        ("&Atomic<bool>", True),
        ("&SyncSender<u32>", True),
        ("&Vec<u32>", False),
        ("&Barrier", False),
        # The crate's own, by the debug information.
        ("&Counter", True),
        ("&Point", False),
        ("&std::vec::Vec<Counter>", True),
        ("&Other", True),
    ):
        assert _may_write(kind, (), True, layouts=layouts) is writes, kind
    # A short name the crate gives an item of its own is the crate's.
    assert _may_write("&Vec<u32>", (), True, layouts=TypeLayouts(INTERIOR, bare=frozenset()))
    # A standard path under a root the crate names itself may be its own.
    assert _may_write("&std::vec::Vec<u32>", (), True, frozenset({"core"}), layouts)
    # A generic type by its instances: `Gen<T> { inner: T }` holds what its argument does,
    # and an argument the dump leaves out (a default) counts.
    generic = TypeLayouts({"Gen": (frozenset(), (frozenset({"interior", "owned"}),))})
    assert not _may_write("&Gen<u32>", (), True, layouts=generic)
    assert _may_write("&Gen<std::cell::Cell<u32>>", (), True, layouts=generic)
    assert _may_write("&Gen", (), True, layouts=generic)
    # A closure by what it captures.
    closures = TypeLayouts(
        closures={
            "{closure@src/main.rs:5:5: 5:13}": ["std::rc::Rc<std::cell::Cell<u32>>"],
            "{closure@src/main.rs:9:9: 9:12}": ["&mut std::vec::Vec<u32>"],
            "{closure@src/main.rs:12:1: 12:4}": None,
        }
    )
    for kind, shared, owned in (
        ("&{closure@src/main.rs:5:5: 5:13}", True, True),
        ("{closure@src/main.rs:9:9: 9:12}", False, True),
        ("&{closure@src/main.rs:12:1: 12:4}", True, True),
        ("&{closure@src/main.rs:20:1: 20:4}", True, True),
    ):
        assert _may_write(kind, (), True, layouts=closures) is shared, kind
        assert _may_write(kind, (), False, layouts=closures) is owned, kind
    # What may reach what another value reaches: not a cell held by value.
    for kind, shares in (
        ("std::sync::mpsc::Receiver<u32>", True),
        ("std::rc::Rc<std::cell::Cell<u32>>", True),
        ("&std::cell::Cell<u32>", True),
        ("std::cell::Cell<u32>", False),
        ("std::rc::Rc<u32>", False),
        ("Counter", False),
    ):
        assert _may_share(kind, (), layouts=layouts) is shares, kind


def test_a_short_standard_name_counts_only_where_the_crate_gives_none():
    body = parse_body(COUNTED)
    for source, trusted in (
        (SOURCE, True),
        (SOURCE.replace("struct Point {", "struct Cell;\n\nstruct Point {"), False),
        (SOURCE.replace("use std::env;", "use std::env;\nuse other::Thing as Cell;"), False),
    ):
        owned = OwnedLocals([body], {}, ("src/main.rs",), texts={"src/main.rs": source})
        assert ("Cell" in owned._trusted_bare()) is trusted, source[:80]


class Dies:
    """A unit's DWARF entries, as rust_symbols.parse_dies gives them."""

    def __init__(self):
        self.offset = 0x0B
        self.root = self.add(None, "DW_TAG_compile_unit")

    def add(self, parent, tag, name=None, kind=None):
        die = _Die(self.offset, tag, {}, [])
        self.offset += 0x10
        if name is not None:
            die.attributes["DW_AT_name"] = f'"{name}")'
        if kind is not None:
            die.attributes["DW_AT_type"] = f'0x{kind.offset:08x} "")'
        if parent is not None:
            parent.children.append(die)
        return die

    def struct(self, parent, name, *members, arguments=()):
        die = self.add(parent, "DW_TAG_structure_type", name)
        for argument in arguments:
            self.add(die, "DW_TAG_template_type_parameter", "T", argument)
        for field, kind in members:
            self.add(die, "DW_TAG_member", field, kind)
        return die


def test_the_debug_information_tells_what_each_crate_type_holds():
    dies = Dies()
    root = dies.root
    crate = dies.add(root, "DW_TAG_namespace", "hidden")
    module = dies.add(crate, "DW_TAG_namespace", "m")
    core, alloc, std = (
        dies.add(root, "DW_TAG_namespace", name) for name in ("core", "alloc", "std")
    )
    cell, option = (dies.add(core, "DW_TAG_namespace", name) for name in ("cell", "option"))
    vec, rc, boxed = (dies.add(alloc, "DW_TAG_namespace", name) for name in ("vec", "rc", "boxed"))
    mpsc = dies.add(dies.add(std, "DW_TAG_namespace", "sync"), "DW_TAG_namespace", "mpsc")
    u32 = dies.add(root, "DW_TAG_base_type", "u32")
    cell_u32 = dies.struct(cell, "Cell<u32>", arguments=(u32,))
    # Another crate's type, by its fields too.
    other = dies.struct(dies.add(root, "DW_TAG_namespace", "other"), "Thing", ("c", cell_u32))
    refcell = dies.struct(cell, "RefCell<u32>", arguments=(u32,))
    vec_u32 = dies.struct(vec, "Vec<u32, alloc::alloc::Global>", arguments=(u32,))
    rc_refcell = dies.struct(
        rc, "Rc<core::cell::RefCell<u32>, alloc::alloc::Global>", arguments=(refcell,)
    )
    sender = dies.struct(mpsc, "Sender<u32>", arguments=(u32,))
    receiver = dies.struct(mpsc, "Receiver<u32>", arguments=(u32,))
    boxed_dyn = dies.struct(
        boxed, "Box<dyn core::ops::function::Fn<(u32), Output=u32>, alloc::alloc::Global>"
    )
    dies.struct(crate, "Counter", ("c", cell_u32))
    dies.struct(crate, "Point", ("a", u32), ("v", vec_u32))
    dies.struct(
        crate,
        "Hold",
        (
            "p",
            dies.add(
                root,
                "DW_TAG_pointer_type",
                "&mut alloc::vec::Vec<u32, alloc::alloc::Global>",
                vec_u32,
            ),
        ),
    )
    dies.struct(crate, "Gen<u32>", ("inner", u32), arguments=(u32,))
    dies.struct(crate, "Gen<core::cell::Cell<u32>>", ("inner", cell_u32), arguments=(cell_u32,))
    shape = dies.struct(crate, "Shape")
    part = dies.add(shape, "DW_TAG_variant_part")
    dies.add(part, "DW_TAG_member", None, u32)
    variant = dies.add(part, "DW_TAG_variant")
    dies.add(variant, "DW_TAG_member", "B", dies.struct(shape, "B", ("c", rc_refcell)))
    node = dies.struct(crate, "Node", ("v", u32))
    boxed_node = dies.add(
        root, "DW_TAG_pointer_type", "alloc::boxed::Box<hidden::Node, alloc::alloc::Global>", node
    )
    some = dies.struct(option, "Some", arguments=(boxed_node,))
    option_node = dies.struct(
        option, "Option<alloc::boxed::Box<hidden::Node, alloc::alloc::Global>>"
    )
    option_node.children.append(some)
    dies.add(node, "DW_TAG_member", "next", option_node)
    dies.struct(crate, "Ends", ("tx", sender), ("rx", receiver))
    dies.struct(crate, "Listens", ("rx", receiver))
    dies.struct(crate, "Uses", ("t", other))
    dies.struct(crate, "Boxed", ("f", boxed_dyn))
    dies.struct(module, "Calm", ("n", u32))
    dies.add(crate, "DW_TAG_enumeration_type", "Mode")
    all_ = frozenset({"interior", "owned", "shares"})
    named = {
        "Counter": (frozenset({"interior", "owned"}), ()),
        "Point": (frozenset(), ()),
        "Hold": (frozenset({"owned"}), ()),
        "Gen": (frozenset(), (frozenset({"interior", "owned"}),)),
        "Shape": (all_, ()),
        "Node": (frozenset(), ()),
        "Ends": (all_, ()),
        "Listens": (frozenset({"shares"}), ()),
        "Uses": (frozenset({"interior", "owned"}), ()),
        "Boxed": (all_, ()),
        "m::Calm": (frozenset(), ()),
        "other::Thing": (frozenset({"interior", "owned"}), ()),
        "Mode": (frozenset(), ()),
    }
    # Each by its path, and its last segment for a signature's short name.
    found = interior_types(root, "hidden")
    expected = {**named, **{"::" + key.rsplit("::", 1)[-1]: value for key, value in named.items()}}
    assert {key: (entry.own, entry.arguments) for key, entry in found.items()} == expected
    # How each holds its arguments, what may be stored through a `&`, and each instance.
    assert found["Gen"].uses == (frozenset({(False, False)}),)
    assert found["Gen"].instances == {
        "Gen<u32>": frozenset(),
        "Gen<Cell<u32>>": frozenset({"interior", "owned"}),
    }
    assert found["Gen"].payloads == {"%0"}
    assert found["Counter"].payloads == {"u32"}
    assert found["Ends"].payloads == {"u32"}
    assert found["Boxed"].payloads == {"*"}
    assert found["Mode"].payloads == frozenset()
    # The dump's `m::Calm` and a signature's `Calm` are the same type.
    layouts = TypeLayouts(found)
    assert not _may_write("&m::Calm", (), True, layouts=layouts)
    assert not _may_write("&Calm", (), True, layouts=layouts)
    assert _may_write("&Thing", (), True, layouts=layouts)


def test_a_copy_that_may_be_out_of_reach_advises_a_change_before_the_call():
    message = (
        "`x` cannot be changed at src/main.rs:56, at any stop of this build: in a::sent, at "
        "every place line 56 can stop at, the program's own `rx` (at lines 54 and 55) may hold "
        "a copy of `x` that it goes on with, which may be out of Peeky's reach (behind a cell, "
        "a lock, a channel or a pointer its type holds), so a change there may never be seen"
    )
    assert mutation_hint(message) == (
        "Change it before the program copies it (a stop on an earlier line, or `--set-at` there)."
    )
    assert "change the copy" in mutation_hint(message.split(", which may be out")[0])


# Review of PR #328: values built of a sharer, default type arguments, what a cell can take,
# `PhantomData` and function types. rustc 1.96.0's MIR of this crate.
REVIEW = """use std::cell::Cell;
use std::env;
use std::marker::PhantomData;
use std::rc::Rc;
use std::sync::mpsc;
use std::sync::{Arc, Mutex};

#[inline(never)]
fn other() -> u32 {
    1
}

struct HoldRx {
    rx: mpsc::Receiver<u32>,
}

struct HoldRc {
    c: Rc<Cell<u32>>,
}

struct HoldArc {
    m: Arc<Mutex<u32>>,
}

struct Dflt<T = u32> {
    inner: T,
}

struct Flag {
    on: Cell<bool>,
    n: u32,
}

struct Tag<T> {
    _p: PhantomData<T>,
    n: u32,
}

#[inline(never)]
fn send(tx: &mpsc::Sender<u32>, x: u32) {
    tx.send(x).unwrap();
}

#[inline(never)]
fn put(c: &Rc<Cell<u32>>, x: u32) {
    c.set(x);
}

#[inline(never)]
fn lock(m: &Arc<Mutex<u32>>, x: u32) {
    *m.lock().unwrap() = x;
}

#[inline(never)]
fn look<T>(_t: &T, x: u32) {
    std::hint::black_box(x);
}

#[inline(never)]
fn dset(g: &Dflt<Cell<u32>>, x: u32) {
    g.inner.set(x);
}

#[inline(never)]
fn flag(c: &Cell<bool>, x: u32) {
    c.set(x > 5);
}

#[inline(never)]
fn raise(f: &Flag, x: u32) {
    f.on.set(x > 5);
}

#[inline(never)]
fn sendb(tx: &mpsc::Sender<bool>, s: String) {
    tx.send(s.is_empty()).unwrap();
}

#[inline(never)]
fn read(c: &Cell<u32>) -> u32 {
    c.get()
}

#[inline(never)]
fn same(c: &Cell<u32>) -> &Cell<u32> {
    c
}

fn held_rx(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let (tx, rx) = mpsc::channel();
    let h = HoldRx { rx };
    send(&tx, x); // SEND_RX
    let k = other(); // HELD_RX
    std::hint::black_box(&x);
    h.rx.recv().unwrap() + k
}

fn held_rc(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let a = Rc::new(Cell::new(0));
    let h = HoldRc { c: Rc::clone(&a) };
    put(&a, x); // PUT_RC
    let k = other(); // HELD_RC
    std::hint::black_box(&x);
    h.c.get() + k
}

fn held_rc_method(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let a = Rc::new(Cell::new(0));
    let h = HoldRc { c: a.clone() };
    put(&a, x); // PUT_RCM
    let k = other(); // HELD_RCM
    std::hint::black_box(&x);
    h.c.get() + k
}

fn held_arc(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let a = Arc::new(Mutex::new(0));
    let h = HoldArc { m: Arc::clone(&a) };
    lock(&a, x); // LOCK_ARC
    let k = other(); // HELD_ARC
    std::hint::black_box(&x);
    *h.m.lock().unwrap() + k
}

fn held_arc_method(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let a = Arc::new(Mutex::new(0));
    let h = HoldArc { m: a.clone() };
    lock(&a, x); // LOCK_ARCM
    let k = other(); // HELD_ARCM
    std::hint::black_box(&x);
    *h.m.lock().unwrap() + k
}

fn captured(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let a = Rc::new(Cell::new(0));
    let b = a.clone();
    let f = move || b.get();
    put(&a, x); // PUT_CAP
    let k = other(); // CAPTURED
    std::hint::black_box(&x);
    f() + k
}

fn defaulted(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let g: Dflt = Dflt { inner: 7 };
    let gc: Dflt<Cell<u32>> = Dflt { inner: Cell::new(1) };
    look(&g, x);
    let k = other(); // DEFAULTED
    let t = g.inner + gc.inner.get();
    t + *std::hint::black_box(&x) + k
}

fn defaulted_cell(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let gc: Dflt<Cell<u32>> = Dflt { inner: Cell::new(1) };
    dset(&gc, x); // DSET
    let k = other(); // DEFAULTED_CELL
    std::hint::black_box(&x);
    gc.inner.get() + k
}

fn flagged(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let c = Cell::new(false);
    flag(&c, x); // FLAG
    let k = other(); // FLAGGED
    std::hint::black_box(&x);
    c.get() as u32 + k
}

fn raised(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let f = Flag { on: Cell::new(false), n: 3 };
    raise(&f, x); // RAISE
    let k = other(); // RAISED
    std::hint::black_box(&x);
    f.on.get() as u32 + f.n + k
}

fn phantom(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let p: PhantomData<Cell<u32>> = PhantomData;
    let t: Tag<Cell<u32>> = Tag { _p: PhantomData, n: 2 };
    look(&p, x);
    look(&t, x);
    let k = other(); // PHANTOM
    t.n + *std::hint::black_box(&x) + k
}

fn pointers(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let f: fn(&Cell<u32>) -> u32 = read;
    let g = read;
    let h: for<'a> fn(&'a Cell<u32>) -> &'a Cell<u32> = same;
    look(&f, x);
    look(&g, x);
    look(&h, x);
    let k = other(); // POINTERS
    let c = Cell::new(4);
    f(&c) + g(h(&c)) + *std::hint::black_box(&x) + k
}

fn sent_bool(arg: u32) -> u32 {
    let s = std::convert::identity(arg).to_string();
    let (tx, rx) = mpsc::channel();
    sendb(&tx, s.clone()); // SENDB
    let k = other(); // SENTB
    std::hint::black_box(&s);
    rx.recv().unwrap() as u32 + k
}

fn main() {
    let x: u32 = env::args().nth(1).unwrap().parse().unwrap();
    println!("HELD_RX {}", held_rx(x));
    println!("HELD_RC {}", held_rc(x));
    println!("HELD_RCM {}", held_rc_method(x));
    println!("HELD_ARC {}", held_arc(x));
    println!("HELD_ARCM {}", held_arc_method(x));
    println!("CAPTURED {}", captured(x));
    println!("DEFAULTED {}", defaulted(x));
    println!("DEFAULTED_CELL {}", defaulted_cell(x));
    println!("FLAGGED {}", flagged(x));
    println!("RAISED {}", raised(x));
    println!("PHANTOM {}", phantom(x));
    println!("POINTERS {}", pointers(x));
    println!("SENTB {}", sent_bool(x));
}
"""
HELD_RX = """// MIR for `held_rx` after PreCodegen

fn held_rx(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:89:12: 89:15
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:89:25: 89:28
    let _2: u32;                         // in scope 0 at src/main.rs:90:9: 90:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:90:36: 90:39
    let mut _6: (std::sync::mpsc::Sender<u32>, std::sync::mpsc::Receiver<u32>); // in scope 0 at src/main.rs:91:20: 91:35
    let mut _8: std::sync::mpsc::Receiver<u32>; // in scope 0 at src/main.rs:92:22: 92:24
    let _9: ();                          // in scope 0 at src/main.rs:93:5: 93:17
    let mut _10: &std::sync::mpsc::Sender<u32>; // in scope 0 at src/main.rs:93:10: 93:13
    let _11: &std::sync::mpsc::Sender<u32>; // in scope 0 at src/main.rs:93:10: 93:13
    let mut _12: u32;                    // in scope 0 at src/main.rs:93:15: 93:16
    let _14: &u32;                       // in scope 0 at src/main.rs:95:5: 95:29
    let mut _15: &u32;                   // in scope 0 at src/main.rs:95:26: 95:28
    let mut _16: u32;                    // in scope 0 at src/main.rs:96:5: 96:25
    let mut _17: std::result::Result<u32, std::sync::mpmc::RecvError>; // in scope 0 at src/main.rs:96:5: 96:16
    let mut _18: &std::sync::mpsc::Receiver<u32>; // in scope 0 at src/main.rs:96:5: 96:9
    let mut _19: u32;                    // in scope 0 at src/main.rs:96:28: 96:29
    let mut _20: (u32, bool);            // in scope 0 at src/main.rs:96:5: 96:29
    let mut _21: bool;                   // in scope 0 at src/main.rs:97:1: 97:2
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:90:9: 90:10
        let _4: std::sync::mpsc::Sender<u32>; // in scope 1 at src/main.rs:91:10: 91:12
        let _5: std::sync::mpsc::Receiver<u32>; // in scope 1 at src/main.rs:91:14: 91:16
        scope 2 {
            debug tx => _4;              // in scope 2 at src/main.rs:91:10: 91:12
            debug rx => _5;              // in scope 2 at src/main.rs:91:14: 91:16
            let _7: HoldRx;              // in scope 2 at src/main.rs:92:9: 92:10
            scope 3 {
                debug h => _7;           // in scope 3 at src/main.rs:92:9: 92:10
                let _13: u32;            // in scope 3 at src/main.rs:94:9: 94:10
                scope 4 {
                    debug k => _13;      // in scope 4 at src/main.rs:94:9: 94:10
                }
            }
        }
    }

    bb0: {
        _21 = const false;               // scope 0 at src/main.rs:90:9: 90:10
        StorageLive(_2);                 // scope 0 at src/main.rs:90:9: 90:10
        StorageLive(_3);                 // scope 0 at src/main.rs:90:36: 90:39
        _3 = copy _1;                    // scope 0 at src/main.rs:90:36: 90:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:90:13: 90:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:90:39: 90:40
        StorageLive(_6);                 // scope 1 at src/main.rs:91:20: 91:35
        _6 = std::sync::mpsc::channel::<u32>() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:91:20: 91:35
    }

    bb2: {
        StorageLive(_4);                 // scope 1 at src/main.rs:91:10: 91:12
        _4 = move (_6.0: std::sync::mpsc::Sender<u32>); // scope 1 at src/main.rs:91:10: 91:12
        StorageLive(_5);                 // scope 1 at src/main.rs:91:14: 91:16
        _21 = const true;                // scope 1 at src/main.rs:91:14: 91:16
        _5 = move (_6.1: std::sync::mpsc::Receiver<u32>); // scope 1 at src/main.rs:91:14: 91:16
        StorageDead(_6);                 // scope 1 at src/main.rs:91:35: 91:36
        StorageLive(_7);                 // scope 2 at src/main.rs:92:9: 92:10
        StorageLive(_8);                 // scope 2 at src/main.rs:92:22: 92:24
        _21 = const false;               // scope 2 at src/main.rs:92:22: 92:24
        _8 = move _5;                    // scope 2 at src/main.rs:92:22: 92:24
        _7 = HoldRx { rx: move _8 };     // scope 2 at src/main.rs:92:13: 92:26
        StorageDead(_8);                 // scope 2 at src/main.rs:92:25: 92:26
        StorageLive(_9);                 // scope 3 at src/main.rs:93:5: 93:17
        StorageLive(_10);                // scope 3 at src/main.rs:93:10: 93:13
        StorageLive(_11);                // scope 3 at src/main.rs:93:10: 93:13
        _11 = &_4;                       // scope 3 at src/main.rs:93:10: 93:13
        _10 = &(*_11);                   // scope 3 at src/main.rs:93:10: 93:13
        StorageLive(_12);                // scope 3 at src/main.rs:93:15: 93:16
        _12 = copy _2;                   // scope 3 at src/main.rs:93:15: 93:16
        _9 = send(move _10, move _12) -> [return: bb3, unwind: bb11]; // scope 3 at src/main.rs:93:5: 93:17
    }

    bb3: {
        StorageDead(_12);                // scope 3 at src/main.rs:93:16: 93:17
        StorageDead(_10);                // scope 3 at src/main.rs:93:16: 93:17
        StorageDead(_11);                // scope 3 at src/main.rs:93:17: 93:18
        StorageDead(_9);                 // scope 3 at src/main.rs:93:17: 93:18
        StorageLive(_13);                // scope 3 at src/main.rs:94:9: 94:10
        _13 = other() -> [return: bb4, unwind: bb11]; // scope 3 at src/main.rs:94:13: 94:20
    }

    bb4: {
        StorageLive(_14);                // scope 4 at src/main.rs:95:5: 95:29
        StorageLive(_15);                // scope 4 at src/main.rs:95:26: 95:28
        _15 = &_2;                       // scope 4 at src/main.rs:95:26: 95:28
        _14 = std::hint::black_box::<&u32>(move _15) -> [return: bb5, unwind: bb11]; // scope 4 at src/main.rs:95:5: 95:29
    }

    bb5: {
        StorageDead(_15);                // scope 4 at src/main.rs:95:28: 95:29
        StorageDead(_14);                // scope 4 at src/main.rs:95:29: 95:30
        StorageLive(_16);                // scope 4 at src/main.rs:96:5: 96:25
        StorageLive(_17);                // scope 4 at src/main.rs:96:5: 96:16
        StorageLive(_18);                // scope 4 at src/main.rs:96:5: 96:9
        _18 = &(_7.0: std::sync::mpsc::Receiver<u32>); // scope 4 at src/main.rs:96:5: 96:9
        _17 = std::sync::mpsc::Receiver::<u32>::recv(move _18) -> [return: bb6, unwind: bb11]; // scope 4 at src/main.rs:96:5: 96:16
    }

    bb6: {
        StorageDead(_18);                // scope 4 at src/main.rs:96:15: 96:16
        _16 = Result::<u32, RecvError>::unwrap(move _17) -> [return: bb7, unwind: bb11]; // scope 4 at src/main.rs:96:5: 96:25
    }

    bb7: {
        StorageDead(_17);                // scope 4 at src/main.rs:96:24: 96:25
        StorageLive(_19);                // scope 4 at src/main.rs:96:28: 96:29
        _19 = copy _13;                  // scope 4 at src/main.rs:96:28: 96:29
        _20 = AddWithOverflow(copy _16, copy _19); // scope 4 at src/main.rs:96:5: 96:29
        assert(!move (_20.1: bool), "attempt to compute `{} + {}`, which would overflow", move _16, move _19) -> [success: bb8, unwind: bb11]; // scope 4 at src/main.rs:96:5: 96:29
    }

    bb8: {
        _0 = move (_20.0: u32);          // scope 4 at src/main.rs:96:5: 96:29
        StorageDead(_19);                // scope 4 at src/main.rs:96:28: 96:29
        StorageDead(_16);                // scope 4 at src/main.rs:96:28: 96:29
        StorageDead(_13);                // scope 3 at src/main.rs:97:1: 97:2
        drop(_7) -> [return: bb9, unwind: bb15]; // scope 2 at src/main.rs:97:1: 97:2
    }

    bb9: {
        StorageDead(_7);                 // scope 2 at src/main.rs:97:1: 97:2
        _21 = const false;               // scope 1 at src/main.rs:97:1: 97:2
        StorageDead(_5);                 // scope 1 at src/main.rs:97:1: 97:2
        drop(_4) -> [return: bb10, unwind: bb13]; // scope 1 at src/main.rs:97:1: 97:2
    }

    bb10: {
        StorageDead(_4);                 // scope 1 at src/main.rs:97:1: 97:2
        StorageDead(_2);                 // scope 0 at src/main.rs:97:1: 97:2
        return;                          // scope 0 at src/main.rs:97:2: 97:2
    }

    bb11 (cleanup): {
        drop(_7) -> [return: bb15, unwind terminate(cleanup)]; // scope 2 at src/main.rs:97:1: 97:2
    }

    bb12 (cleanup): {
        drop(_4) -> [return: bb13, unwind terminate(cleanup)]; // scope 1 at src/main.rs:97:1: 97:2
    }

    bb13 (cleanup): {
        resume;                          // scope 0 at src/main.rs:89:1: 97:2
    }

    bb14 (cleanup): {
        drop(_5) -> [return: bb12, unwind terminate(cleanup)]; // scope 1 at src/main.rs:97:1: 97:2
    }

    bb15 (cleanup): {
        switchInt(copy _21) -> [0: bb12, otherwise: bb14]; // scope 1 at src/main.rs:97:1: 97:2
    }
}
"""
HELD_RC = """// MIR for `held_rc` after PreCodegen

fn held_rc(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:99:12: 99:15
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:99:25: 99:28
    let _2: u32;                         // in scope 0 at src/main.rs:100:9: 100:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:100:36: 100:39
    let mut _5: std::cell::Cell<u32>;    // in scope 0 at src/main.rs:101:21: 101:33
    let mut _7: std::rc::Rc<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:102:25: 102:38
    let mut _8: &std::rc::Rc<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:102:35: 102:37
    let _9: &std::rc::Rc<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:102:35: 102:37
    let _10: ();                         // in scope 0 at src/main.rs:103:5: 103:15
    let mut _11: &std::rc::Rc<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:103:9: 103:11
    let _12: &std::rc::Rc<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:103:9: 103:11
    let mut _13: u32;                    // in scope 0 at src/main.rs:103:13: 103:14
    let _15: &u32;                       // in scope 0 at src/main.rs:105:5: 105:29
    let mut _16: &u32;                   // in scope 0 at src/main.rs:105:26: 105:28
    let mut _17: u32;                    // in scope 0 at src/main.rs:106:5: 106:14
    let mut _18: &std::cell::Cell<u32>;  // in scope 0 at src/main.rs:106:5: 106:8
    let _19: &std::cell::Cell<u32>;      // in scope 0 at src/main.rs:106:5: 106:14
    let mut _20: &std::rc::Rc<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:106:5: 106:8
    let mut _21: u32;                    // in scope 0 at src/main.rs:106:17: 106:18
    let mut _22: (u32, bool);            // in scope 0 at src/main.rs:106:5: 106:18
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:100:9: 100:10
        let _4: std::rc::Rc<std::cell::Cell<u32>>; // in scope 1 at src/main.rs:101:9: 101:10
        scope 2 {
            debug a => _4;               // in scope 2 at src/main.rs:101:9: 101:10
            let _6: HoldRc;              // in scope 2 at src/main.rs:102:9: 102:10
            scope 3 {
                debug h => _6;           // in scope 3 at src/main.rs:102:9: 102:10
                let _14: u32;            // in scope 3 at src/main.rs:104:9: 104:10
                scope 4 {
                    debug k => _14;      // in scope 4 at src/main.rs:104:9: 104:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:100:9: 100:10
        StorageLive(_3);                 // scope 0 at src/main.rs:100:36: 100:39
        _3 = copy _1;                    // scope 0 at src/main.rs:100:36: 100:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:100:13: 100:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:100:39: 100:40
        StorageLive(_4);                 // scope 1 at src/main.rs:101:9: 101:10
        StorageLive(_5);                 // scope 1 at src/main.rs:101:21: 101:33
        _5 = Cell::<u32>::new(const 0_u32) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:101:21: 101:33
    }

    bb2: {
        _4 = Rc::<Cell<u32>>::new(move _5) -> [return: bb3, unwind continue]; // scope 1 at src/main.rs:101:13: 101:34
    }

    bb3: {
        StorageDead(_5);                 // scope 1 at src/main.rs:101:33: 101:34
        StorageLive(_6);                 // scope 2 at src/main.rs:102:9: 102:10
        StorageLive(_7);                 // scope 2 at src/main.rs:102:25: 102:38
        StorageLive(_8);                 // scope 2 at src/main.rs:102:35: 102:37
        StorageLive(_9);                 // scope 2 at src/main.rs:102:35: 102:37
        _9 = &_4;                        // scope 2 at src/main.rs:102:35: 102:37
        _8 = &(*_9);                     // scope 2 at src/main.rs:102:35: 102:37
        _7 = <Rc<Cell<u32>> as Clone>::clone(move _8) -> [return: bb4, unwind: bb14]; // scope 2 at src/main.rs:102:25: 102:38
    }

    bb4: {
        StorageDead(_8);                 // scope 2 at src/main.rs:102:37: 102:38
        _6 = HoldRc { c: move _7 };      // scope 2 at src/main.rs:102:13: 102:40
        StorageDead(_7);                 // scope 2 at src/main.rs:102:39: 102:40
        StorageDead(_9);                 // scope 2 at src/main.rs:102:40: 102:41
        StorageLive(_10);                // scope 3 at src/main.rs:103:5: 103:15
        StorageLive(_11);                // scope 3 at src/main.rs:103:9: 103:11
        StorageLive(_12);                // scope 3 at src/main.rs:103:9: 103:11
        _12 = &_4;                       // scope 3 at src/main.rs:103:9: 103:11
        _11 = &(*_12);                   // scope 3 at src/main.rs:103:9: 103:11
        StorageLive(_13);                // scope 3 at src/main.rs:103:13: 103:14
        _13 = copy _2;                   // scope 3 at src/main.rs:103:13: 103:14
        _10 = put(move _11, move _13) -> [return: bb5, unwind: bb13]; // scope 3 at src/main.rs:103:5: 103:15
    }

    bb5: {
        StorageDead(_13);                // scope 3 at src/main.rs:103:14: 103:15
        StorageDead(_11);                // scope 3 at src/main.rs:103:14: 103:15
        StorageDead(_12);                // scope 3 at src/main.rs:103:15: 103:16
        StorageDead(_10);                // scope 3 at src/main.rs:103:15: 103:16
        StorageLive(_14);                // scope 3 at src/main.rs:104:9: 104:10
        _14 = other() -> [return: bb6, unwind: bb13]; // scope 3 at src/main.rs:104:13: 104:20
    }

    bb6: {
        StorageLive(_15);                // scope 4 at src/main.rs:105:5: 105:29
        StorageLive(_16);                // scope 4 at src/main.rs:105:26: 105:28
        _16 = &_2;                       // scope 4 at src/main.rs:105:26: 105:28
        _15 = std::hint::black_box::<&u32>(move _16) -> [return: bb7, unwind: bb13]; // scope 4 at src/main.rs:105:5: 105:29
    }

    bb7: {
        StorageDead(_16);                // scope 4 at src/main.rs:105:28: 105:29
        StorageDead(_15);                // scope 4 at src/main.rs:105:29: 105:30
        StorageLive(_17);                // scope 4 at src/main.rs:106:5: 106:14
        StorageLive(_18);                // scope 4 at src/main.rs:106:5: 106:8
        StorageLive(_19);                // scope 4 at src/main.rs:106:5: 106:14
        StorageLive(_20);                // scope 4 at src/main.rs:106:5: 106:8
        _20 = &(_6.0: std::rc::Rc<std::cell::Cell<u32>>); // scope 4 at src/main.rs:106:5: 106:8
        _19 = <Rc<Cell<u32>> as Deref>::deref(move _20) -> [return: bb8, unwind: bb13]; // scope 4 at src/main.rs:106:5: 106:14
    }

    bb8: {
        _18 = &(*_19);                   // scope 4 at src/main.rs:106:5: 106:8
        StorageDead(_20);                // scope 4 at src/main.rs:106:7: 106:8
        _17 = Cell::<u32>::get(move _18) -> [return: bb9, unwind: bb13]; // scope 4 at src/main.rs:106:5: 106:14
    }

    bb9: {
        StorageDead(_18);                // scope 4 at src/main.rs:106:13: 106:14
        StorageLive(_21);                // scope 4 at src/main.rs:106:17: 106:18
        _21 = copy _14;                  // scope 4 at src/main.rs:106:17: 106:18
        _22 = AddWithOverflow(copy _17, copy _21); // scope 4 at src/main.rs:106:5: 106:18
        assert(!move (_22.1: bool), "attempt to compute `{} + {}`, which would overflow", move _17, move _21) -> [success: bb10, unwind: bb13]; // scope 4 at src/main.rs:106:5: 106:18
    }

    bb10: {
        _0 = move (_22.0: u32);          // scope 4 at src/main.rs:106:5: 106:18
        StorageDead(_21);                // scope 4 at src/main.rs:106:17: 106:18
        StorageDead(_19);                // scope 4 at src/main.rs:106:17: 106:18
        StorageDead(_17);                // scope 4 at src/main.rs:106:17: 106:18
        StorageDead(_14);                // scope 3 at src/main.rs:107:1: 107:2
        drop(_6) -> [return: bb11, unwind: bb14]; // scope 2 at src/main.rs:107:1: 107:2
    }

    bb11: {
        StorageDead(_6);                 // scope 2 at src/main.rs:107:1: 107:2
        drop(_4) -> [return: bb12, unwind: bb15]; // scope 1 at src/main.rs:107:1: 107:2
    }

    bb12: {
        StorageDead(_4);                 // scope 1 at src/main.rs:107:1: 107:2
        StorageDead(_2);                 // scope 0 at src/main.rs:107:1: 107:2
        return;                          // scope 0 at src/main.rs:107:2: 107:2
    }

    bb13 (cleanup): {
        drop(_6) -> [return: bb14, unwind terminate(cleanup)]; // scope 2 at src/main.rs:107:1: 107:2
    }

    bb14 (cleanup): {
        drop(_4) -> [return: bb15, unwind terminate(cleanup)]; // scope 1 at src/main.rs:107:1: 107:2
    }

    bb15 (cleanup): {
        resume;                          // scope 0 at src/main.rs:99:1: 107:2
    }
}
"""
DEFAULTED = """// MIR for `defaulted` after PreCodegen

fn defaulted(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:150:14: 150:17
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:150:27: 150:30
    let _2: u32;                         // in scope 0 at src/main.rs:151:9: 151:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:151:36: 151:39
    let mut _6: std::cell::Cell<u32>;    // in scope 0 at src/main.rs:153:45: 153:57
    let _7: ();                          // in scope 0 at src/main.rs:154:5: 154:16
    let mut _8: &Dflt;                   // in scope 0 at src/main.rs:154:10: 154:12
    let _9: &Dflt;                       // in scope 0 at src/main.rs:154:10: 154:12
    let mut _10: u32;                    // in scope 0 at src/main.rs:154:14: 154:15
    let mut _13: u32;                    // in scope 0 at src/main.rs:156:13: 156:20
    let mut _14: u32;                    // in scope 0 at src/main.rs:156:23: 156:37
    let mut _15: &std::cell::Cell<u32>;  // in scope 0 at src/main.rs:156:23: 156:31
    let mut _16: (u32, bool);            // in scope 0 at src/main.rs:156:13: 156:37
    let mut _17: u32;                    // in scope 0 at src/main.rs:157:5: 157:34
    let mut _18: u32;                    // in scope 0 at src/main.rs:157:5: 157:6
    let mut _19: u32;                    // in scope 0 at src/main.rs:157:9: 157:34
    let mut _20: &u32;                   // in scope 0 at src/main.rs:157:10: 157:34
    let mut _21: &u32;                   // in scope 0 at src/main.rs:157:31: 157:33
    let mut _22: (u32, bool);            // in scope 0 at src/main.rs:157:5: 157:34
    let mut _23: u32;                    // in scope 0 at src/main.rs:157:37: 157:38
    let mut _24: (u32, bool);            // in scope 0 at src/main.rs:157:5: 157:38
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:151:9: 151:10
        let _4: Dflt;                    // in scope 1 at src/main.rs:152:9: 152:10
        scope 2 {
            debug g => _4;               // in scope 2 at src/main.rs:152:9: 152:10
            let _5: Dflt<std::cell::Cell<u32>>; // in scope 2 at src/main.rs:153:9: 153:11
            scope 3 {
                debug gc => _5;          // in scope 3 at src/main.rs:153:9: 153:11
                let _11: u32;            // in scope 3 at src/main.rs:155:9: 155:10
                scope 4 {
                    debug k => _11;      // in scope 4 at src/main.rs:155:9: 155:10
                    let _12: u32;        // in scope 4 at src/main.rs:156:9: 156:10
                    scope 5 {
                        debug t => _12;  // in scope 5 at src/main.rs:156:9: 156:10
                    }
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:151:9: 151:10
        StorageLive(_3);                 // scope 0 at src/main.rs:151:36: 151:39
        _3 = copy _1;                    // scope 0 at src/main.rs:151:36: 151:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:151:13: 151:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:151:39: 151:40
        StorageLive(_4);                 // scope 1 at src/main.rs:152:9: 152:10
        _4 = Dflt { inner: const 7_u32 }; // scope 1 at src/main.rs:152:19: 152:36
        StorageLive(_5);                 // scope 2 at src/main.rs:153:9: 153:11
        StorageLive(_6);                 // scope 2 at src/main.rs:153:45: 153:57
        _6 = Cell::<u32>::new(const 1_u32) -> [return: bb2, unwind continue]; // scope 2 at src/main.rs:153:45: 153:57
    }

    bb2: {
        _5 = Dflt::<Cell<u32>> { inner: move _6 }; // scope 2 at src/main.rs:153:31: 153:59
        StorageDead(_6);                 // scope 2 at src/main.rs:153:58: 153:59
        StorageLive(_7);                 // scope 3 at src/main.rs:154:5: 154:16
        StorageLive(_8);                 // scope 3 at src/main.rs:154:10: 154:12
        StorageLive(_9);                 // scope 3 at src/main.rs:154:10: 154:12
        _9 = &_4;                        // scope 3 at src/main.rs:154:10: 154:12
        _8 = &(*_9);                     // scope 3 at src/main.rs:154:10: 154:12
        StorageLive(_10);                // scope 3 at src/main.rs:154:14: 154:15
        _10 = copy _2;                   // scope 3 at src/main.rs:154:14: 154:15
        _7 = look::<Dflt>(move _8, move _10) -> [return: bb3, unwind continue]; // scope 3 at src/main.rs:154:5: 154:16
    }

    bb3: {
        StorageDead(_10);                // scope 3 at src/main.rs:154:15: 154:16
        StorageDead(_8);                 // scope 3 at src/main.rs:154:15: 154:16
        StorageDead(_9);                 // scope 3 at src/main.rs:154:16: 154:17
        StorageDead(_7);                 // scope 3 at src/main.rs:154:16: 154:17
        StorageLive(_11);                // scope 3 at src/main.rs:155:9: 155:10
        _11 = other() -> [return: bb4, unwind continue]; // scope 3 at src/main.rs:155:13: 155:20
    }

    bb4: {
        StorageLive(_12);                // scope 4 at src/main.rs:156:9: 156:10
        StorageLive(_13);                // scope 4 at src/main.rs:156:13: 156:20
        _13 = copy (_4.0: u32);          // scope 4 at src/main.rs:156:13: 156:20
        StorageLive(_14);                // scope 4 at src/main.rs:156:23: 156:37
        StorageLive(_15);                // scope 4 at src/main.rs:156:23: 156:31
        _15 = &(_5.0: std::cell::Cell<u32>); // scope 4 at src/main.rs:156:23: 156:31
        _14 = Cell::<u32>::get(move _15) -> [return: bb5, unwind continue]; // scope 4 at src/main.rs:156:23: 156:37
    }

    bb5: {
        StorageDead(_15);                // scope 4 at src/main.rs:156:36: 156:37
        _16 = AddWithOverflow(copy _13, copy _14); // scope 4 at src/main.rs:156:13: 156:37
        assert(!move (_16.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _14) -> [success: bb6, unwind continue]; // scope 4 at src/main.rs:156:13: 156:37
    }

    bb6: {
        _12 = move (_16.0: u32);         // scope 4 at src/main.rs:156:13: 156:37
        StorageDead(_14);                // scope 4 at src/main.rs:156:36: 156:37
        StorageDead(_13);                // scope 4 at src/main.rs:156:36: 156:37
        StorageLive(_17);                // scope 5 at src/main.rs:157:5: 157:34
        StorageLive(_18);                // scope 5 at src/main.rs:157:5: 157:6
        _18 = copy _12;                  // scope 5 at src/main.rs:157:5: 157:6
        StorageLive(_19);                // scope 5 at src/main.rs:157:9: 157:34
        StorageLive(_20);                // scope 5 at src/main.rs:157:10: 157:34
        StorageLive(_21);                // scope 5 at src/main.rs:157:31: 157:33
        _21 = &_2;                       // scope 5 at src/main.rs:157:31: 157:33
        _20 = std::hint::black_box::<&u32>(move _21) -> [return: bb7, unwind continue]; // scope 5 at src/main.rs:157:10: 157:34
    }

    bb7: {
        StorageDead(_21);                // scope 5 at src/main.rs:157:33: 157:34
        _19 = copy (*_20);               // scope 5 at src/main.rs:157:9: 157:34
        _22 = AddWithOverflow(copy _18, copy _19); // scope 5 at src/main.rs:157:5: 157:34
        assert(!move (_22.1: bool), "attempt to compute `{} + {}`, which would overflow", move _18, move _19) -> [success: bb8, unwind continue]; // scope 5 at src/main.rs:157:5: 157:34
    }

    bb8: {
        _17 = move (_22.0: u32);         // scope 5 at src/main.rs:157:5: 157:34
        StorageDead(_19);                // scope 5 at src/main.rs:157:33: 157:34
        StorageDead(_18);                // scope 5 at src/main.rs:157:33: 157:34
        StorageLive(_23);                // scope 5 at src/main.rs:157:37: 157:38
        _23 = copy _11;                  // scope 5 at src/main.rs:157:37: 157:38
        _24 = AddWithOverflow(copy _17, copy _23); // scope 5 at src/main.rs:157:5: 157:38
        assert(!move (_24.1: bool), "attempt to compute `{} + {}`, which would overflow", move _17, move _23) -> [success: bb9, unwind continue]; // scope 5 at src/main.rs:157:5: 157:38
    }

    bb9: {
        _0 = move (_24.0: u32);          // scope 5 at src/main.rs:157:5: 157:38
        StorageDead(_23);                // scope 5 at src/main.rs:157:37: 157:38
        StorageDead(_20);                // scope 5 at src/main.rs:157:37: 157:38
        StorageDead(_17);                // scope 5 at src/main.rs:157:37: 157:38
        StorageDead(_12);                // scope 4 at src/main.rs:158:1: 158:2
        StorageDead(_11);                // scope 3 at src/main.rs:158:1: 158:2
        StorageDead(_5);                 // scope 2 at src/main.rs:158:1: 158:2
        StorageDead(_4);                 // scope 1 at src/main.rs:158:1: 158:2
        StorageDead(_2);                 // scope 0 at src/main.rs:158:1: 158:2
        return;                          // scope 0 at src/main.rs:158:2: 158:2
    }
}
"""
FLAGGED = """// MIR for `flagged` after PreCodegen

fn flagged(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:169:12: 169:15
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:169:25: 169:28
    let _2: u32;                         // in scope 0 at src/main.rs:170:9: 170:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:170:36: 170:39
    let _5: ();                          // in scope 0 at src/main.rs:172:5: 172:16
    let mut _6: &std::cell::Cell<bool>;  // in scope 0 at src/main.rs:172:10: 172:12
    let _7: &std::cell::Cell<bool>;      // in scope 0 at src/main.rs:172:10: 172:12
    let mut _8: u32;                     // in scope 0 at src/main.rs:172:14: 172:15
    let _10: &u32;                       // in scope 0 at src/main.rs:174:5: 174:29
    let mut _11: &u32;                   // in scope 0 at src/main.rs:174:26: 174:28
    let mut _12: u32;                    // in scope 0 at src/main.rs:175:5: 175:19
    let mut _13: bool;                   // in scope 0 at src/main.rs:175:5: 175:12
    let mut _14: &std::cell::Cell<bool>; // in scope 0 at src/main.rs:175:5: 175:6
    let mut _15: u32;                    // in scope 0 at src/main.rs:175:22: 175:23
    let mut _16: (u32, bool);            // in scope 0 at src/main.rs:175:5: 175:23
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:170:9: 170:10
        let _4: std::cell::Cell<bool>;   // in scope 1 at src/main.rs:171:9: 171:10
        scope 2 {
            debug c => _4;               // in scope 2 at src/main.rs:171:9: 171:10
            let _9: u32;                 // in scope 2 at src/main.rs:173:9: 173:10
            scope 3 {
                debug k => _9;           // in scope 3 at src/main.rs:173:9: 173:10
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:170:9: 170:10
        StorageLive(_3);                 // scope 0 at src/main.rs:170:36: 170:39
        _3 = copy _1;                    // scope 0 at src/main.rs:170:36: 170:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:170:13: 170:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:170:39: 170:40
        StorageLive(_4);                 // scope 1 at src/main.rs:171:9: 171:10
        _4 = Cell::<bool>::new(const false) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:171:13: 171:29
    }

    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:172:5: 172:16
        StorageLive(_6);                 // scope 2 at src/main.rs:172:10: 172:12
        StorageLive(_7);                 // scope 2 at src/main.rs:172:10: 172:12
        _7 = &_4;                        // scope 2 at src/main.rs:172:10: 172:12
        _6 = &(*_7);                     // scope 2 at src/main.rs:172:10: 172:12
        StorageLive(_8);                 // scope 2 at src/main.rs:172:14: 172:15
        _8 = copy _2;                    // scope 2 at src/main.rs:172:14: 172:15
        _5 = flag(move _6, move _8) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:172:5: 172:16
    }

    bb3: {
        StorageDead(_8);                 // scope 2 at src/main.rs:172:15: 172:16
        StorageDead(_6);                 // scope 2 at src/main.rs:172:15: 172:16
        StorageDead(_7);                 // scope 2 at src/main.rs:172:16: 172:17
        StorageDead(_5);                 // scope 2 at src/main.rs:172:16: 172:17
        StorageLive(_9);                 // scope 2 at src/main.rs:173:9: 173:10
        _9 = other() -> [return: bb4, unwind continue]; // scope 2 at src/main.rs:173:13: 173:20
    }

    bb4: {
        StorageLive(_10);                // scope 3 at src/main.rs:174:5: 174:29
        StorageLive(_11);                // scope 3 at src/main.rs:174:26: 174:28
        _11 = &_2;                       // scope 3 at src/main.rs:174:26: 174:28
        _10 = std::hint::black_box::<&u32>(move _11) -> [return: bb5, unwind continue]; // scope 3 at src/main.rs:174:5: 174:29
    }

    bb5: {
        StorageDead(_11);                // scope 3 at src/main.rs:174:28: 174:29
        StorageDead(_10);                // scope 3 at src/main.rs:174:29: 174:30
        StorageLive(_12);                // scope 3 at src/main.rs:175:5: 175:19
        StorageLive(_13);                // scope 3 at src/main.rs:175:5: 175:12
        StorageLive(_14);                // scope 3 at src/main.rs:175:5: 175:6
        _14 = &_4;                       // scope 3 at src/main.rs:175:5: 175:6
        _13 = Cell::<bool>::get(move _14) -> [return: bb6, unwind continue]; // scope 3 at src/main.rs:175:5: 175:12
    }

    bb6: {
        StorageDead(_14);                // scope 3 at src/main.rs:175:11: 175:12
        _12 = move _13 as u32 (IntToInt); // scope 3 at src/main.rs:175:5: 175:19
        StorageDead(_13);                // scope 3 at src/main.rs:175:18: 175:19
        StorageLive(_15);                // scope 3 at src/main.rs:175:22: 175:23
        _15 = copy _9;                   // scope 3 at src/main.rs:175:22: 175:23
        _16 = AddWithOverflow(copy _12, copy _15); // scope 3 at src/main.rs:175:5: 175:23
        assert(!move (_16.1: bool), "attempt to compute `{} + {}`, which would overflow", move _12, move _15) -> [success: bb7, unwind continue]; // scope 3 at src/main.rs:175:5: 175:23
    }

    bb7: {
        _0 = move (_16.0: u32);          // scope 3 at src/main.rs:175:5: 175:23
        StorageDead(_15);                // scope 3 at src/main.rs:175:22: 175:23
        StorageDead(_12);                // scope 3 at src/main.rs:175:22: 175:23
        StorageDead(_9);                 // scope 2 at src/main.rs:176:1: 176:2
        StorageDead(_4);                 // scope 1 at src/main.rs:176:1: 176:2
        StorageDead(_2);                 // scope 0 at src/main.rs:176:1: 176:2
        return;                          // scope 0 at src/main.rs:176:2: 176:2
    }
}
"""
RAISED = """// MIR for `raised` after PreCodegen

fn raised(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:178:11: 178:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:178:24: 178:27
    let _2: u32;                         // in scope 0 at src/main.rs:179:9: 179:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:179:36: 179:39
    let mut _5: std::cell::Cell<bool>;   // in scope 0 at src/main.rs:180:24: 180:40
    let _6: ();                          // in scope 0 at src/main.rs:181:5: 181:17
    let mut _7: &Flag;                   // in scope 0 at src/main.rs:181:11: 181:13
    let _8: &Flag;                       // in scope 0 at src/main.rs:181:11: 181:13
    let mut _9: u32;                     // in scope 0 at src/main.rs:181:15: 181:16
    let _11: &u32;                       // in scope 0 at src/main.rs:183:5: 183:29
    let mut _12: &u32;                   // in scope 0 at src/main.rs:183:26: 183:28
    let mut _13: u32;                    // in scope 0 at src/main.rs:184:5: 184:28
    let mut _14: u32;                    // in scope 0 at src/main.rs:184:5: 184:22
    let mut _15: bool;                   // in scope 0 at src/main.rs:184:5: 184:15
    let mut _16: &std::cell::Cell<bool>; // in scope 0 at src/main.rs:184:5: 184:9
    let mut _17: u32;                    // in scope 0 at src/main.rs:184:25: 184:28
    let mut _18: (u32, bool);            // in scope 0 at src/main.rs:184:5: 184:28
    let mut _19: u32;                    // in scope 0 at src/main.rs:184:31: 184:32
    let mut _20: (u32, bool);            // in scope 0 at src/main.rs:184:5: 184:32
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:179:9: 179:10
        let _4: Flag;                    // in scope 1 at src/main.rs:180:9: 180:10
        scope 2 {
            debug f => _4;               // in scope 2 at src/main.rs:180:9: 180:10
            let _10: u32;                // in scope 2 at src/main.rs:182:9: 182:10
            scope 3 {
                debug k => _10;          // in scope 3 at src/main.rs:182:9: 182:10
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:179:9: 179:10
        StorageLive(_3);                 // scope 0 at src/main.rs:179:36: 179:39
        _3 = copy _1;                    // scope 0 at src/main.rs:179:36: 179:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:179:13: 179:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:179:39: 179:40
        StorageLive(_4);                 // scope 1 at src/main.rs:180:9: 180:10
        StorageLive(_5);                 // scope 1 at src/main.rs:180:24: 180:40
        _5 = Cell::<bool>::new(const false) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:180:24: 180:40
    }

    bb2: {
        _4 = Flag { on: move _5, n: const 3_u32 }; // scope 1 at src/main.rs:180:13: 180:48
        StorageDead(_5);                 // scope 1 at src/main.rs:180:47: 180:48
        StorageLive(_6);                 // scope 2 at src/main.rs:181:5: 181:17
        StorageLive(_7);                 // scope 2 at src/main.rs:181:11: 181:13
        StorageLive(_8);                 // scope 2 at src/main.rs:181:11: 181:13
        _8 = &_4;                        // scope 2 at src/main.rs:181:11: 181:13
        _7 = &(*_8);                     // scope 2 at src/main.rs:181:11: 181:13
        StorageLive(_9);                 // scope 2 at src/main.rs:181:15: 181:16
        _9 = copy _2;                    // scope 2 at src/main.rs:181:15: 181:16
        _6 = raise(move _7, move _9) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:181:5: 181:17
    }

    bb3: {
        StorageDead(_9);                 // scope 2 at src/main.rs:181:16: 181:17
        StorageDead(_7);                 // scope 2 at src/main.rs:181:16: 181:17
        StorageDead(_8);                 // scope 2 at src/main.rs:181:17: 181:18
        StorageDead(_6);                 // scope 2 at src/main.rs:181:17: 181:18
        StorageLive(_10);                // scope 2 at src/main.rs:182:9: 182:10
        _10 = other() -> [return: bb4, unwind continue]; // scope 2 at src/main.rs:182:13: 182:20
    }

    bb4: {
        StorageLive(_11);                // scope 3 at src/main.rs:183:5: 183:29
        StorageLive(_12);                // scope 3 at src/main.rs:183:26: 183:28
        _12 = &_2;                       // scope 3 at src/main.rs:183:26: 183:28
        _11 = std::hint::black_box::<&u32>(move _12) -> [return: bb5, unwind continue]; // scope 3 at src/main.rs:183:5: 183:29
    }

    bb5: {
        StorageDead(_12);                // scope 3 at src/main.rs:183:28: 183:29
        StorageDead(_11);                // scope 3 at src/main.rs:183:29: 183:30
        StorageLive(_13);                // scope 3 at src/main.rs:184:5: 184:28
        StorageLive(_14);                // scope 3 at src/main.rs:184:5: 184:22
        StorageLive(_15);                // scope 3 at src/main.rs:184:5: 184:15
        StorageLive(_16);                // scope 3 at src/main.rs:184:5: 184:9
        _16 = &(_4.0: std::cell::Cell<bool>); // scope 3 at src/main.rs:184:5: 184:9
        _15 = Cell::<bool>::get(move _16) -> [return: bb6, unwind continue]; // scope 3 at src/main.rs:184:5: 184:15
    }

    bb6: {
        StorageDead(_16);                // scope 3 at src/main.rs:184:14: 184:15
        _14 = move _15 as u32 (IntToInt); // scope 3 at src/main.rs:184:5: 184:22
        StorageDead(_15);                // scope 3 at src/main.rs:184:21: 184:22
        StorageLive(_17);                // scope 3 at src/main.rs:184:25: 184:28
        _17 = copy (_4.1: u32);          // scope 3 at src/main.rs:184:25: 184:28
        _18 = AddWithOverflow(copy _14, copy _17); // scope 3 at src/main.rs:184:5: 184:28
        assert(!move (_18.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _17) -> [success: bb7, unwind continue]; // scope 3 at src/main.rs:184:5: 184:28
    }

    bb7: {
        _13 = move (_18.0: u32);         // scope 3 at src/main.rs:184:5: 184:28
        StorageDead(_17);                // scope 3 at src/main.rs:184:27: 184:28
        StorageDead(_14);                // scope 3 at src/main.rs:184:27: 184:28
        StorageLive(_19);                // scope 3 at src/main.rs:184:31: 184:32
        _19 = copy _10;                  // scope 3 at src/main.rs:184:31: 184:32
        _20 = AddWithOverflow(copy _13, copy _19); // scope 3 at src/main.rs:184:5: 184:32
        assert(!move (_20.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _19) -> [success: bb8, unwind continue]; // scope 3 at src/main.rs:184:5: 184:32
    }

    bb8: {
        _0 = move (_20.0: u32);          // scope 3 at src/main.rs:184:5: 184:32
        StorageDead(_19);                // scope 3 at src/main.rs:184:31: 184:32
        StorageDead(_13);                // scope 3 at src/main.rs:184:31: 184:32
        StorageDead(_10);                // scope 2 at src/main.rs:185:1: 185:2
        StorageDead(_4);                 // scope 1 at src/main.rs:185:1: 185:2
        StorageDead(_2);                 // scope 0 at src/main.rs:185:1: 185:2
        return;                          // scope 0 at src/main.rs:185:2: 185:2
    }
}
"""
PHANTOM = """// MIR for `phantom` after PreCodegen

fn phantom(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:187:12: 187:15
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:187:25: 187:28
    let _2: u32;                         // in scope 0 at src/main.rs:188:9: 188:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:188:36: 188:39
    let mut _6: std::marker::PhantomData<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:190:39: 190:50
    let _7: ();                          // in scope 0 at src/main.rs:191:5: 191:16
    let mut _8: &std::marker::PhantomData<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:191:10: 191:12
    let _9: &std::marker::PhantomData<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:191:10: 191:12
    let mut _10: u32;                    // in scope 0 at src/main.rs:191:14: 191:15
    let _11: ();                         // in scope 0 at src/main.rs:192:5: 192:16
    let mut _12: &Tag<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:192:10: 192:12
    let _13: &Tag<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:192:10: 192:12
    let mut _14: u32;                    // in scope 0 at src/main.rs:192:14: 192:15
    let mut _16: u32;                    // in scope 0 at src/main.rs:194:5: 194:36
    let mut _17: u32;                    // in scope 0 at src/main.rs:194:5: 194:8
    let mut _18: u32;                    // in scope 0 at src/main.rs:194:11: 194:36
    let mut _19: &u32;                   // in scope 0 at src/main.rs:194:12: 194:36
    let mut _20: &u32;                   // in scope 0 at src/main.rs:194:33: 194:35
    let mut _21: (u32, bool);            // in scope 0 at src/main.rs:194:5: 194:36
    let mut _22: u32;                    // in scope 0 at src/main.rs:194:39: 194:40
    let mut _23: (u32, bool);            // in scope 0 at src/main.rs:194:5: 194:40
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:188:9: 188:10
        let _4: std::marker::PhantomData<std::cell::Cell<u32>>; // in scope 1 at src/main.rs:189:9: 189:10
        scope 2 {
            debug p => _4;               // in scope 2 at src/main.rs:189:9: 189:10
            let _5: Tag<std::cell::Cell<u32>>; // in scope 2 at src/main.rs:190:9: 190:10
            scope 3 {
                debug t => _5;           // in scope 3 at src/main.rs:190:9: 190:10
                let _15: u32;            // in scope 3 at src/main.rs:193:9: 193:10
                scope 4 {
                    debug k => _15;      // in scope 4 at src/main.rs:193:9: 193:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:188:9: 188:10
        StorageLive(_3);                 // scope 0 at src/main.rs:188:36: 188:39
        _3 = copy _1;                    // scope 0 at src/main.rs:188:36: 188:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:188:13: 188:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:188:39: 188:40
        StorageLive(_4);                 // scope 1 at src/main.rs:189:9: 189:10
        _4 = PhantomData::<Cell<u32>>;   // scope 1 at src/main.rs:189:37: 189:48
        StorageLive(_5);                 // scope 2 at src/main.rs:190:9: 190:10
        StorageLive(_6);                 // scope 2 at src/main.rs:190:39: 190:50
        _6 = PhantomData::<Cell<u32>>;   // scope 2 at src/main.rs:190:39: 190:50
        _5 = Tag::<Cell<u32>> { _p: move _6, n: const 2_u32 }; // scope 2 at src/main.rs:190:29: 190:58
        StorageDead(_6);                 // scope 2 at src/main.rs:190:57: 190:58
        StorageLive(_7);                 // scope 3 at src/main.rs:191:5: 191:16
        StorageLive(_8);                 // scope 3 at src/main.rs:191:10: 191:12
        StorageLive(_9);                 // scope 3 at src/main.rs:191:10: 191:12
        _9 = &_4;                        // scope 3 at src/main.rs:191:10: 191:12
        _8 = &(*_9);                     // scope 3 at src/main.rs:191:10: 191:12
        StorageLive(_10);                // scope 3 at src/main.rs:191:14: 191:15
        _10 = copy _2;                   // scope 3 at src/main.rs:191:14: 191:15
        _7 = look::<PhantomData<Cell<u32>>>(move _8, move _10) -> [return: bb2, unwind continue]; // scope 3 at src/main.rs:191:5: 191:16
    }

    bb2: {
        StorageDead(_10);                // scope 3 at src/main.rs:191:15: 191:16
        StorageDead(_8);                 // scope 3 at src/main.rs:191:15: 191:16
        StorageDead(_9);                 // scope 3 at src/main.rs:191:16: 191:17
        StorageDead(_7);                 // scope 3 at src/main.rs:191:16: 191:17
        StorageLive(_11);                // scope 3 at src/main.rs:192:5: 192:16
        StorageLive(_12);                // scope 3 at src/main.rs:192:10: 192:12
        StorageLive(_13);                // scope 3 at src/main.rs:192:10: 192:12
        _13 = &_5;                       // scope 3 at src/main.rs:192:10: 192:12
        _12 = &(*_13);                   // scope 3 at src/main.rs:192:10: 192:12
        StorageLive(_14);                // scope 3 at src/main.rs:192:14: 192:15
        _14 = copy _2;                   // scope 3 at src/main.rs:192:14: 192:15
        _11 = look::<Tag<Cell<u32>>>(move _12, move _14) -> [return: bb3, unwind continue]; // scope 3 at src/main.rs:192:5: 192:16
    }

    bb3: {
        StorageDead(_14);                // scope 3 at src/main.rs:192:15: 192:16
        StorageDead(_12);                // scope 3 at src/main.rs:192:15: 192:16
        StorageDead(_13);                // scope 3 at src/main.rs:192:16: 192:17
        StorageDead(_11);                // scope 3 at src/main.rs:192:16: 192:17
        StorageLive(_15);                // scope 3 at src/main.rs:193:9: 193:10
        _15 = other() -> [return: bb4, unwind continue]; // scope 3 at src/main.rs:193:13: 193:20
    }

    bb4: {
        StorageLive(_16);                // scope 4 at src/main.rs:194:5: 194:36
        StorageLive(_17);                // scope 4 at src/main.rs:194:5: 194:8
        _17 = copy (_5.1: u32);          // scope 4 at src/main.rs:194:5: 194:8
        StorageLive(_18);                // scope 4 at src/main.rs:194:11: 194:36
        StorageLive(_19);                // scope 4 at src/main.rs:194:12: 194:36
        StorageLive(_20);                // scope 4 at src/main.rs:194:33: 194:35
        _20 = &_2;                       // scope 4 at src/main.rs:194:33: 194:35
        _19 = std::hint::black_box::<&u32>(move _20) -> [return: bb5, unwind continue]; // scope 4 at src/main.rs:194:12: 194:36
    }

    bb5: {
        StorageDead(_20);                // scope 4 at src/main.rs:194:35: 194:36
        _18 = copy (*_19);               // scope 4 at src/main.rs:194:11: 194:36
        _21 = AddWithOverflow(copy _17, copy _18); // scope 4 at src/main.rs:194:5: 194:36
        assert(!move (_21.1: bool), "attempt to compute `{} + {}`, which would overflow", move _17, move _18) -> [success: bb6, unwind continue]; // scope 4 at src/main.rs:194:5: 194:36
    }

    bb6: {
        _16 = move (_21.0: u32);         // scope 4 at src/main.rs:194:5: 194:36
        StorageDead(_18);                // scope 4 at src/main.rs:194:35: 194:36
        StorageDead(_17);                // scope 4 at src/main.rs:194:35: 194:36
        StorageLive(_22);                // scope 4 at src/main.rs:194:39: 194:40
        _22 = copy _15;                  // scope 4 at src/main.rs:194:39: 194:40
        _23 = AddWithOverflow(copy _16, copy _22); // scope 4 at src/main.rs:194:5: 194:40
        assert(!move (_23.1: bool), "attempt to compute `{} + {}`, which would overflow", move _16, move _22) -> [success: bb7, unwind continue]; // scope 4 at src/main.rs:194:5: 194:40
    }

    bb7: {
        _0 = move (_23.0: u32);          // scope 4 at src/main.rs:194:5: 194:40
        StorageDead(_22);                // scope 4 at src/main.rs:194:39: 194:40
        StorageDead(_19);                // scope 4 at src/main.rs:194:39: 194:40
        StorageDead(_16);                // scope 4 at src/main.rs:194:39: 194:40
        StorageDead(_15);                // scope 3 at src/main.rs:195:1: 195:2
        StorageDead(_5);                 // scope 2 at src/main.rs:195:1: 195:2
        StorageDead(_4);                 // scope 1 at src/main.rs:195:1: 195:2
        StorageDead(_2);                 // scope 0 at src/main.rs:195:1: 195:2
        return;                          // scope 0 at src/main.rs:195:2: 195:2
    }
}
"""
POINTERS = """// MIR for `pointers` after PreCodegen

fn pointers(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:197:13: 197:16
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:197:26: 197:29
    let _2: u32;                         // in scope 0 at src/main.rs:198:9: 198:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:198:36: 198:39
    let _7: ();                          // in scope 0 at src/main.rs:202:5: 202:16
    let mut _8: &for<'a> fn(&'a std::cell::Cell<u32>) -> u32; // in scope 0 at src/main.rs:202:10: 202:12
    let _9: &for<'a> fn(&'a std::cell::Cell<u32>) -> u32; // in scope 0 at src/main.rs:202:10: 202:12
    let mut _10: u32;                    // in scope 0 at src/main.rs:202:14: 202:15
    let _11: ();                         // in scope 0 at src/main.rs:203:5: 203:16
    let mut _12: &for<'a> fn(&'a std::cell::Cell<u32>) -> u32 {read}; // in scope 0 at src/main.rs:203:10: 203:12
    let _13: &for<'a> fn(&'a std::cell::Cell<u32>) -> u32 {read}; // in scope 0 at src/main.rs:203:10: 203:12
    let mut _14: u32;                    // in scope 0 at src/main.rs:203:14: 203:15
    let _15: ();                         // in scope 0 at src/main.rs:204:5: 204:16
    let mut _16: &for<'a> fn(&'a std::cell::Cell<u32>) -> &'a std::cell::Cell<u32>; // in scope 0 at src/main.rs:204:10: 204:12
    let _17: &for<'a> fn(&'a std::cell::Cell<u32>) -> &'a std::cell::Cell<u32>; // in scope 0 at src/main.rs:204:10: 204:12
    let mut _18: u32;                    // in scope 0 at src/main.rs:204:14: 204:15
    let mut _21: u32;                    // in scope 0 at src/main.rs:207:5: 207:49
    let mut _22: u32;                    // in scope 0 at src/main.rs:207:5: 207:21
    let mut _23: u32;                    // in scope 0 at src/main.rs:207:5: 207:10
    let mut _24: for<'a> fn(&'a std::cell::Cell<u32>) -> u32; // in scope 0 at src/main.rs:207:5: 207:6
    let mut _25: &std::cell::Cell<u32>;  // in scope 0 at src/main.rs:207:7: 207:9
    let _26: &std::cell::Cell<u32>;      // in scope 0 at src/main.rs:207:7: 207:9
    let mut _27: u32;                    // in scope 0 at src/main.rs:207:13: 207:21
    let mut _28: for<'a> fn(&'a std::cell::Cell<u32>) -> u32 {read}; // in scope 0 at src/main.rs:207:13: 207:14
    let mut _29: &std::cell::Cell<u32>;  // in scope 0 at src/main.rs:207:15: 207:20
    let _30: &std::cell::Cell<u32>;      // in scope 0 at src/main.rs:207:15: 207:20
    let mut _31: for<'a> fn(&'a std::cell::Cell<u32>) -> &'a std::cell::Cell<u32>; // in scope 0 at src/main.rs:207:15: 207:16
    let mut _32: &std::cell::Cell<u32>;  // in scope 0 at src/main.rs:207:17: 207:19
    let _33: &std::cell::Cell<u32>;      // in scope 0 at src/main.rs:207:17: 207:19
    let mut _34: (u32, bool);            // in scope 0 at src/main.rs:207:5: 207:21
    let mut _35: u32;                    // in scope 0 at src/main.rs:207:24: 207:49
    let mut _36: &u32;                   // in scope 0 at src/main.rs:207:25: 207:49
    let mut _37: &u32;                   // in scope 0 at src/main.rs:207:46: 207:48
    let mut _38: (u32, bool);            // in scope 0 at src/main.rs:207:5: 207:49
    let mut _39: u32;                    // in scope 0 at src/main.rs:207:52: 207:53
    let mut _40: (u32, bool);            // in scope 0 at src/main.rs:207:5: 207:53
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:198:9: 198:10
        let _4: for<'a> fn(&'a std::cell::Cell<u32>) -> u32; // in scope 1 at src/main.rs:199:9: 199:10
        scope 2 {
            debug f => _4;               // in scope 2 at src/main.rs:199:9: 199:10
            let _5: for<'a> fn(&'a std::cell::Cell<u32>) -> u32 {read}; // in scope 2 at src/main.rs:200:9: 200:10
            scope 3 {
                debug g => _5;           // in scope 3 at src/main.rs:200:9: 200:10
                let _6: for<'a> fn(&'a std::cell::Cell<u32>) -> &'a std::cell::Cell<u32>; // in scope 3 at src/main.rs:201:9: 201:10
                scope 4 {
                    debug h => _6;       // in scope 4 at src/main.rs:201:9: 201:10
                    let _19: u32;        // in scope 4 at src/main.rs:205:9: 205:10
                    scope 5 {
                        debug k => _19;  // in scope 5 at src/main.rs:205:9: 205:10
                        let _20: std::cell::Cell<u32>; // in scope 5 at src/main.rs:206:9: 206:10
                        scope 6 {
                            debug c => _20; // in scope 6 at src/main.rs:206:9: 206:10
                        }
                    }
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:198:9: 198:10
        StorageLive(_3);                 // scope 0 at src/main.rs:198:36: 198:39
        _3 = copy _1;                    // scope 0 at src/main.rs:198:36: 198:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:198:13: 198:40
    }

    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:198:39: 198:40
        StorageLive(_4);                 // scope 1 at src/main.rs:199:9: 199:10
        _4 = read as for<'a> fn(&'a std::cell::Cell<u32>) -> u32 (PointerCoercion(ReifyFnPointer(Safe), Implicit)); // scope 1 at src/main.rs:199:36: 199:40
        StorageLive(_5);                 // scope 2 at src/main.rs:200:9: 200:10
        _5 = read;                       // scope 2 at src/main.rs:200:13: 200:17
        StorageLive(_6);                 // scope 3 at src/main.rs:201:9: 201:10
        _6 = same as for<'a> fn(&'a std::cell::Cell<u32>) -> &'a std::cell::Cell<u32> (PointerCoercion(ReifyFnPointer(Safe), Implicit)); // scope 3 at src/main.rs:201:57: 201:61
        StorageLive(_7);                 // scope 4 at src/main.rs:202:5: 202:16
        StorageLive(_8);                 // scope 4 at src/main.rs:202:10: 202:12
        StorageLive(_9);                 // scope 4 at src/main.rs:202:10: 202:12
        _9 = &_4;                        // scope 4 at src/main.rs:202:10: 202:12
        _8 = &(*_9);                     // scope 4 at src/main.rs:202:10: 202:12
        StorageLive(_10);                // scope 4 at src/main.rs:202:14: 202:15
        _10 = copy _2;                   // scope 4 at src/main.rs:202:14: 202:15
        _7 = look::<for<'a> fn(&'a Cell<u32>) -> u32>(move _8, move _10) -> [return: bb2, unwind continue]; // scope 4 at src/main.rs:202:5: 202:16
    }

    bb2: {
        StorageDead(_10);                // scope 4 at src/main.rs:202:15: 202:16
        StorageDead(_8);                 // scope 4 at src/main.rs:202:15: 202:16
        StorageDead(_9);                 // scope 4 at src/main.rs:202:16: 202:17
        StorageDead(_7);                 // scope 4 at src/main.rs:202:16: 202:17
        StorageLive(_11);                // scope 4 at src/main.rs:203:5: 203:16
        StorageLive(_12);                // scope 4 at src/main.rs:203:10: 203:12
        StorageLive(_13);                // scope 4 at src/main.rs:203:10: 203:12
        _13 = &_5;                       // scope 4 at src/main.rs:203:10: 203:12
        _12 = &(*_13);                   // scope 4 at src/main.rs:203:10: 203:12
        StorageLive(_14);                // scope 4 at src/main.rs:203:14: 203:15
        _14 = copy _2;                   // scope 4 at src/main.rs:203:14: 203:15
        _11 = look::<for<'a> fn(&'a Cell<u32>) -> u32 {read}>(move _12, move _14) -> [return: bb3, unwind continue]; // scope 4 at src/main.rs:203:5: 203:16
    }

    bb3: {
        StorageDead(_14);                // scope 4 at src/main.rs:203:15: 203:16
        StorageDead(_12);                // scope 4 at src/main.rs:203:15: 203:16
        StorageDead(_13);                // scope 4 at src/main.rs:203:16: 203:17
        StorageDead(_11);                // scope 4 at src/main.rs:203:16: 203:17
        StorageLive(_15);                // scope 4 at src/main.rs:204:5: 204:16
        StorageLive(_16);                // scope 4 at src/main.rs:204:10: 204:12
        StorageLive(_17);                // scope 4 at src/main.rs:204:10: 204:12
        _17 = &_6;                       // scope 4 at src/main.rs:204:10: 204:12
        _16 = &(*_17);                   // scope 4 at src/main.rs:204:10: 204:12
        StorageLive(_18);                // scope 4 at src/main.rs:204:14: 204:15
        _18 = copy _2;                   // scope 4 at src/main.rs:204:14: 204:15
        _15 = look::<for<'a> fn(&'a Cell<u32>) -> &'a Cell<u32>>(move _16, move _18) -> [return: bb4, unwind continue]; // scope 4 at src/main.rs:204:5: 204:16
    }

    bb4: {
        StorageDead(_18);                // scope 4 at src/main.rs:204:15: 204:16
        StorageDead(_16);                // scope 4 at src/main.rs:204:15: 204:16
        StorageDead(_17);                // scope 4 at src/main.rs:204:16: 204:17
        StorageDead(_15);                // scope 4 at src/main.rs:204:16: 204:17
        StorageLive(_19);                // scope 4 at src/main.rs:205:9: 205:10
        _19 = other() -> [return: bb5, unwind continue]; // scope 4 at src/main.rs:205:13: 205:20
    }

    bb5: {
        StorageLive(_20);                // scope 5 at src/main.rs:206:9: 206:10
        _20 = Cell::<u32>::new(const 4_u32) -> [return: bb6, unwind continue]; // scope 5 at src/main.rs:206:13: 206:25
    }

    bb6: {
        StorageLive(_21);                // scope 6 at src/main.rs:207:5: 207:49
        StorageLive(_22);                // scope 6 at src/main.rs:207:5: 207:21
        StorageLive(_23);                // scope 6 at src/main.rs:207:5: 207:10
        StorageLive(_24);                // scope 6 at src/main.rs:207:5: 207:6
        _24 = copy _4;                   // scope 6 at src/main.rs:207:5: 207:6
        StorageLive(_25);                // scope 6 at src/main.rs:207:7: 207:9
        StorageLive(_26);                // scope 6 at src/main.rs:207:7: 207:9
        _26 = &_20;                      // scope 6 at src/main.rs:207:7: 207:9
        _25 = &(*_26);                   // scope 6 at src/main.rs:207:7: 207:9
        _23 = move _24(move _25) -> [return: bb7, unwind continue]; // scope 6 at src/main.rs:207:5: 207:10
    }

    bb7: {
        StorageDead(_25);                // scope 6 at src/main.rs:207:9: 207:10
        StorageDead(_24);                // scope 6 at src/main.rs:207:9: 207:10
        StorageLive(_27);                // scope 6 at src/main.rs:207:13: 207:21
        StorageLive(_28);                // scope 6 at src/main.rs:207:13: 207:14
        _28 = copy _5;                   // scope 6 at src/main.rs:207:13: 207:14
        StorageLive(_29);                // scope 6 at src/main.rs:207:15: 207:20
        StorageLive(_30);                // scope 6 at src/main.rs:207:15: 207:20
        StorageLive(_31);                // scope 6 at src/main.rs:207:15: 207:16
        _31 = copy _6;                   // scope 6 at src/main.rs:207:15: 207:16
        StorageLive(_32);                // scope 6 at src/main.rs:207:17: 207:19
        StorageLive(_33);                // scope 6 at src/main.rs:207:17: 207:19
        _33 = &_20;                      // scope 6 at src/main.rs:207:17: 207:19
        _32 = &(*_33);                   // scope 6 at src/main.rs:207:17: 207:19
        _30 = move _31(move _32) -> [return: bb8, unwind continue]; // scope 6 at src/main.rs:207:15: 207:20
    }

    bb8: {
        _29 = &(*_30);                   // scope 6 at src/main.rs:207:15: 207:20
        StorageDead(_32);                // scope 6 at src/main.rs:207:19: 207:20
        StorageDead(_31);                // scope 6 at src/main.rs:207:19: 207:20
        _27 = move _28(move _29) -> [return: bb9, unwind continue]; // scope 6 at src/main.rs:207:13: 207:21
    }

    bb9: {
        StorageDead(_29);                // scope 6 at src/main.rs:207:20: 207:21
        StorageDead(_28);                // scope 6 at src/main.rs:207:20: 207:21
        _34 = AddWithOverflow(copy _23, copy _27); // scope 6 at src/main.rs:207:5: 207:21
        assert(!move (_34.1: bool), "attempt to compute `{} + {}`, which would overflow", move _23, move _27) -> [success: bb10, unwind continue]; // scope 6 at src/main.rs:207:5: 207:21
    }

    bb10: {
        _22 = move (_34.0: u32);         // scope 6 at src/main.rs:207:5: 207:21
        StorageDead(_27);                // scope 6 at src/main.rs:207:20: 207:21
        StorageDead(_23);                // scope 6 at src/main.rs:207:20: 207:21
        StorageLive(_35);                // scope 6 at src/main.rs:207:24: 207:49
        StorageLive(_36);                // scope 6 at src/main.rs:207:25: 207:49
        StorageLive(_37);                // scope 6 at src/main.rs:207:46: 207:48
        _37 = &_2;                       // scope 6 at src/main.rs:207:46: 207:48
        _36 = std::hint::black_box::<&u32>(move _37) -> [return: bb11, unwind continue]; // scope 6 at src/main.rs:207:25: 207:49
    }

    bb11: {
        StorageDead(_37);                // scope 6 at src/main.rs:207:48: 207:49
        _35 = copy (*_36);               // scope 6 at src/main.rs:207:24: 207:49
        _38 = AddWithOverflow(copy _22, copy _35); // scope 6 at src/main.rs:207:5: 207:49
        assert(!move (_38.1: bool), "attempt to compute `{} + {}`, which would overflow", move _22, move _35) -> [success: bb12, unwind continue]; // scope 6 at src/main.rs:207:5: 207:49
    }

    bb12: {
        _21 = move (_38.0: u32);         // scope 6 at src/main.rs:207:5: 207:49
        StorageDead(_35);                // scope 6 at src/main.rs:207:48: 207:49
        StorageDead(_22);                // scope 6 at src/main.rs:207:48: 207:49
        StorageLive(_39);                // scope 6 at src/main.rs:207:52: 207:53
        _39 = copy _19;                  // scope 6 at src/main.rs:207:52: 207:53
        _40 = AddWithOverflow(copy _21, copy _39); // scope 6 at src/main.rs:207:5: 207:53
        assert(!move (_40.1: bool), "attempt to compute `{} + {}`, which would overflow", move _21, move _39) -> [success: bb13, unwind continue]; // scope 6 at src/main.rs:207:5: 207:53
    }

    bb13: {
        _0 = move (_40.0: u32);          // scope 6 at src/main.rs:207:5: 207:53
        StorageDead(_39);                // scope 6 at src/main.rs:207:52: 207:53
        StorageDead(_36);                // scope 6 at src/main.rs:207:52: 207:53
        StorageDead(_33);                // scope 6 at src/main.rs:207:52: 207:53
        StorageDead(_30);                // scope 6 at src/main.rs:207:52: 207:53
        StorageDead(_26);                // scope 6 at src/main.rs:207:52: 207:53
        StorageDead(_21);                // scope 6 at src/main.rs:207:52: 207:53
        StorageDead(_20);                // scope 5 at src/main.rs:208:1: 208:2
        StorageDead(_19);                // scope 4 at src/main.rs:208:1: 208:2
        StorageDead(_6);                 // scope 3 at src/main.rs:208:1: 208:2
        StorageDead(_5);                 // scope 2 at src/main.rs:208:1: 208:2
        StorageDead(_4);                 // scope 1 at src/main.rs:208:1: 208:2
        StorageDead(_2);                 // scope 0 at src/main.rs:208:1: 208:2
        return;                          // scope 0 at src/main.rs:208:2: 208:2
    }
}
"""
SENT_BOOL = """// MIR for `sent_bool` after PreCodegen

fn sent_bool(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:210:14: 210:17
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:210:27: 210:30
    let _2: std::string::String;         // in scope 0 at src/main.rs:211:9: 211:10
    let mut _3: &u32;                    // in scope 0 at src/main.rs:211:13: 211:40
    let _4: u32;                         // in scope 0 at src/main.rs:211:13: 211:40
    let mut _5: u32;                     // in scope 0 at src/main.rs:211:36: 211:39
    let mut _8: (std::sync::mpsc::Sender<bool>, std::sync::mpsc::Receiver<bool>); // in scope 0 at src/main.rs:212:20: 212:35
    let _9: ();                          // in scope 0 at src/main.rs:213:5: 213:26
    let mut _10: &std::sync::mpsc::Sender<bool>; // in scope 0 at src/main.rs:213:11: 213:14
    let _11: &std::sync::mpsc::Sender<bool>; // in scope 0 at src/main.rs:213:11: 213:14
    let mut _12: std::string::String;    // in scope 0 at src/main.rs:213:16: 213:25
    let mut _13: &std::string::String;   // in scope 0 at src/main.rs:213:16: 213:17
    let _15: &std::string::String;       // in scope 0 at src/main.rs:215:5: 215:29
    let mut _16: &std::string::String;   // in scope 0 at src/main.rs:215:26: 215:28
    let mut _17: u32;                    // in scope 0 at src/main.rs:216:5: 216:30
    let mut _18: bool;                   // in scope 0 at src/main.rs:216:5: 216:23
    let mut _19: std::result::Result<bool, std::sync::mpmc::RecvError>; // in scope 0 at src/main.rs:216:5: 216:14
    let mut _20: &std::sync::mpsc::Receiver<bool>; // in scope 0 at src/main.rs:216:5: 216:7
    let mut _21: u32;                    // in scope 0 at src/main.rs:216:33: 216:34
    let mut _22: (u32, bool);            // in scope 0 at src/main.rs:216:5: 216:34
    scope 1 {
        debug s => _2;                   // in scope 1 at src/main.rs:211:9: 211:10
        let _6: std::sync::mpsc::Sender<bool>; // in scope 1 at src/main.rs:212:10: 212:12
        let _7: std::sync::mpsc::Receiver<bool>; // in scope 1 at src/main.rs:212:14: 212:16
        scope 2 {
            debug tx => _6;              // in scope 2 at src/main.rs:212:10: 212:12
            debug rx => _7;              // in scope 2 at src/main.rs:212:14: 212:16
            let _14: u32;                // in scope 2 at src/main.rs:214:9: 214:10
            scope 3 {
                debug k => _14;          // in scope 3 at src/main.rs:214:9: 214:10
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:211:9: 211:10
        StorageLive(_3);                 // scope 0 at src/main.rs:211:13: 211:40
        StorageLive(_4);                 // scope 0 at src/main.rs:211:13: 211:40
        StorageLive(_5);                 // scope 0 at src/main.rs:211:36: 211:39
        _5 = copy _1;                    // scope 0 at src/main.rs:211:36: 211:39
        _4 = identity::<u32>(move _5) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:211:13: 211:40
    }

    bb1: {
        _3 = &_4;                        // scope 0 at src/main.rs:211:13: 211:40
        StorageDead(_5);                 // scope 0 at src/main.rs:211:39: 211:40
        _2 = <u32 as ToString>::to_string(move _3) -> [return: bb2, unwind continue]; // scope 0 at src/main.rs:211:13: 211:52
    }

    bb2: {
        StorageDead(_3);                 // scope 0 at src/main.rs:211:51: 211:52
        StorageDead(_4);                 // scope 0 at src/main.rs:211:52: 211:53
        StorageLive(_8);                 // scope 1 at src/main.rs:212:20: 212:35
        _8 = std::sync::mpsc::channel::<bool>() -> [return: bb3, unwind: bb16]; // scope 1 at src/main.rs:212:20: 212:35
    }

    bb3: {
        StorageLive(_6);                 // scope 1 at src/main.rs:212:10: 212:12
        _6 = move (_8.0: std::sync::mpsc::Sender<bool>); // scope 1 at src/main.rs:212:10: 212:12
        StorageLive(_7);                 // scope 1 at src/main.rs:212:14: 212:16
        _7 = move (_8.1: std::sync::mpsc::Receiver<bool>); // scope 1 at src/main.rs:212:14: 212:16
        StorageDead(_8);                 // scope 1 at src/main.rs:212:35: 212:36
        StorageLive(_9);                 // scope 2 at src/main.rs:213:5: 213:26
        StorageLive(_10);                // scope 2 at src/main.rs:213:11: 213:14
        StorageLive(_11);                // scope 2 at src/main.rs:213:11: 213:14
        _11 = &_6;                       // scope 2 at src/main.rs:213:11: 213:14
        _10 = &(*_11);                   // scope 2 at src/main.rs:213:11: 213:14
        StorageLive(_12);                // scope 2 at src/main.rs:213:16: 213:25
        StorageLive(_13);                // scope 2 at src/main.rs:213:16: 213:17
        _13 = &_2;                       // scope 2 at src/main.rs:213:16: 213:17
        _12 = <String as Clone>::clone(move _13) -> [return: bb4, unwind: bb14]; // scope 2 at src/main.rs:213:16: 213:25
    }

    bb4: {
        StorageDead(_13);                // scope 2 at src/main.rs:213:24: 213:25
        _9 = sendb(move _10, move _12) -> [return: bb5, unwind: bb14]; // scope 2 at src/main.rs:213:5: 213:26
    }

    bb5: {
        StorageDead(_12);                // scope 2 at src/main.rs:213:25: 213:26
        StorageDead(_10);                // scope 2 at src/main.rs:213:25: 213:26
        StorageDead(_11);                // scope 2 at src/main.rs:213:26: 213:27
        StorageDead(_9);                 // scope 2 at src/main.rs:213:26: 213:27
        StorageLive(_14);                // scope 2 at src/main.rs:214:9: 214:10
        _14 = other() -> [return: bb6, unwind: bb14]; // scope 2 at src/main.rs:214:13: 214:20
    }

    bb6: {
        StorageLive(_15);                // scope 3 at src/main.rs:215:5: 215:29
        StorageLive(_16);                // scope 3 at src/main.rs:215:26: 215:28
        _16 = &_2;                       // scope 3 at src/main.rs:215:26: 215:28
        _15 = std::hint::black_box::<&String>(move _16) -> [return: bb7, unwind: bb14]; // scope 3 at src/main.rs:215:5: 215:29
    }

    bb7: {
        StorageDead(_16);                // scope 3 at src/main.rs:215:28: 215:29
        StorageDead(_15);                // scope 3 at src/main.rs:215:29: 215:30
        StorageLive(_17);                // scope 3 at src/main.rs:216:5: 216:30
        StorageLive(_18);                // scope 3 at src/main.rs:216:5: 216:23
        StorageLive(_19);                // scope 3 at src/main.rs:216:5: 216:14
        StorageLive(_20);                // scope 3 at src/main.rs:216:5: 216:7
        _20 = &_7;                       // scope 3 at src/main.rs:216:5: 216:7
        _19 = std::sync::mpsc::Receiver::<bool>::recv(move _20) -> [return: bb8, unwind: bb14]; // scope 3 at src/main.rs:216:5: 216:14
    }

    bb8: {
        StorageDead(_20);                // scope 3 at src/main.rs:216:13: 216:14
        _18 = Result::<bool, RecvError>::unwrap(move _19) -> [return: bb9, unwind: bb14]; // scope 3 at src/main.rs:216:5: 216:23
    }

    bb9: {
        StorageDead(_19);                // scope 3 at src/main.rs:216:22: 216:23
        _17 = move _18 as u32 (IntToInt); // scope 3 at src/main.rs:216:5: 216:30
        StorageDead(_18);                // scope 3 at src/main.rs:216:29: 216:30
        StorageLive(_21);                // scope 3 at src/main.rs:216:33: 216:34
        _21 = copy _14;                  // scope 3 at src/main.rs:216:33: 216:34
        _22 = AddWithOverflow(copy _17, copy _21); // scope 3 at src/main.rs:216:5: 216:34
        assert(!move (_22.1: bool), "attempt to compute `{} + {}`, which would overflow", move _17, move _21) -> [success: bb10, unwind: bb14]; // scope 3 at src/main.rs:216:5: 216:34
    }

    bb10: {
        _0 = move (_22.0: u32);          // scope 3 at src/main.rs:216:5: 216:34
        StorageDead(_21);                // scope 3 at src/main.rs:216:33: 216:34
        StorageDead(_17);                // scope 3 at src/main.rs:216:33: 216:34
        StorageDead(_14);                // scope 2 at src/main.rs:217:1: 217:2
        drop(_7) -> [return: bb11, unwind: bb15]; // scope 1 at src/main.rs:217:1: 217:2
    }

    bb11: {
        StorageDead(_7);                 // scope 1 at src/main.rs:217:1: 217:2
        drop(_6) -> [return: bb12, unwind: bb16]; // scope 1 at src/main.rs:217:1: 217:2
    }

    bb12: {
        StorageDead(_6);                 // scope 1 at src/main.rs:217:1: 217:2
        drop(_2) -> [return: bb13, unwind: bb17]; // scope 0 at src/main.rs:217:1: 217:2
    }

    bb13: {
        StorageDead(_2);                 // scope 0 at src/main.rs:217:1: 217:2
        return;                          // scope 0 at src/main.rs:217:2: 217:2
    }

    bb14 (cleanup): {
        drop(_7) -> [return: bb15, unwind terminate(cleanup)]; // scope 1 at src/main.rs:217:1: 217:2
    }

    bb15 (cleanup): {
        drop(_6) -> [return: bb16, unwind terminate(cleanup)]; // scope 1 at src/main.rs:217:1: 217:2
    }

    bb16 (cleanup): {
        drop(_2) -> [return: bb17, unwind terminate(cleanup)]; // scope 0 at src/main.rs:217:1: 217:2
    }

    bb17 (cleanup): {
        resume;                          // scope 0 at src/main.rs:210:1: 217:2
    }
}
"""

# What the build's debug information gives for its types (rust_symbols.interior_types).
REVIEW_TYPES = {
    "HoldRx": CrateType(frozenset({"shares"}), (), (), frozenset({"u32"})),
    "HoldRc": CrateType(frozenset({"interior", "owned", "shares"}), (), (), frozenset({"u32"})),
    "HoldArc": CrateType(frozenset({"interior", "owned", "shares"}), (), (), frozenset({"u32"})),
    "Dflt": CrateType(
        frozenset(),
        (frozenset({"interior", "owned"}),),
        (frozenset({(False, False)}),),
        frozenset({"%0"}),
        frozenset({"Dflt", "#0"}),
        {"Dflt<u32>": frozenset(), "Dflt<Cell<u32>>": frozenset({"interior", "owned"})},
    ),
    "Flag": CrateType(
        frozenset({"interior", "owned"}),
        (),
        (),
        frozenset({"bool"}),
        frozenset({"Flag", "Cell", "bool", "u32"}),
    ),
    "Tag": CrateType(
        frozenset(),
        (frozenset({"interior", "owned"}),),
        (frozenset(),),
        frozenset(),
        frozenset({"Tag", "PhantomData", "u32"}),
        {"Tag<Cell<u32>>": frozenset()},
    ),
}


def review(text, name, marker, statement, interior=REVIEW_TYPES, source=REVIEW, variable="x"):
    """``why`` at the line of ``REVIEW`` that ``marker`` ends."""
    line = next(i for i, each in enumerate(REVIEW.splitlines(), 1) if each.endswith(f"// {marker}"))
    return why(text, name, line, statement, interior, source, variable)


def test_a_value_built_of_what_shares_the_cell_shares_it():
    # `let h = HoldRx { rx };` and `let h = HoldRc { c: Rc::clone(&a) };`, built before the
    # call: `h` reaches what `send(&tx, x)` and `put(&a, x)` store. On the PR's first head an
    # aggregate was no link, so `x` was `[settable]` and the program printed the old `6`.
    for text, name, marker, statement in (
        (HELD_RX, "held_rx", "HELD_RX", "_13 = other()"),
        (HELD_RC, "held_rc", "HELD_RC", "_14 = other()"),
    ):
        body = body_of(text, REVIEW_TYPES)
        holder = body.bindings["h"][0][0]
        assert any(holder in body._sharers(n) for n in body.types), name
        assert holder in body.direct_copies_at(2, place(body, statement)), name
        reason = review(text, name, marker, statement)[0]
        assert "the program's own `h` (at " in reason, reason
        assert "may hold a copy of it that it goes on with" in reason, reason


def test_an_argument_the_dump_leaves_out_is_its_declared_default():
    # `let g: Dflt = Dflt { inner: 7 };` (`struct Dflt<T = u32>`) beside a `Dflt<Cell<u32>>`:
    # the dump names `g`'s type `&Dflt`. Its argument is the default `u32`, so `look(&g, x)`
    # stores nothing. On the PR's first head the instances' flags were joined: "may hold".
    body = body_of(DEFAULTED, REVIEW_TYPES)
    body.layouts.defaults = {"Dflt": ("u32",)}
    lent = [n for n, kind in body.types.items() if kind == "&Dflt"]
    assert lent and not any(body._may_write_through(n) for n in lent)
    assert review(DEFAULTED, "defaulted", "DEFAULTED", "_11 = other()") == (None, None)
    layouts = TypeLayouts(REVIEW_TYPES, defaults={"Dflt": ("u32",)})
    assert not _may_write("&Dflt", (), True, layouts=layouts)
    assert _may_write("&Dflt<std::cell::Cell<u32>>", (), True, layouts=layouts)
    # The default as the sources declare it, the crate's own types' too.
    owned = OwnedLocals([body], {}, ("src/main.rs",), texts={"src/main.rs": REVIEW})
    assert owned._declared_defaults()["Dflt"] == ("u32",)
    declared = REVIEW.replace("struct Dflt<T = u32>", "struct Dflt<'a, T: Into<u8> = Cell<u32>>")
    owned = OwnedLocals([body], {}, ("src/main.rs",), texts={"src/main.rs": declared})
    assert owned._declared_defaults()["Dflt"] == ("Cell<u32>",)
    # Where the declared default can't be found, which instance it is can't be told.
    assert "unsure" in _type_flags("&Dflt", _STD_CRATES, TypeLayouts(REVIEW_TYPES))
    source = REVIEW.replace("struct Dflt<T = u32>", "struct Dflt<T>")
    reason = review(DEFAULTED, "defaulted", "DEFAULTED", "_11 = other()", source=source)[0]
    assert "which instance of its type it is can't be told)" in reason, reason


def test_a_copy_is_said_only_where_what_a_callee_may_store_can_be_one():
    # `flag(&c, x)` with `c: Cell<bool>`; `raise(&f, x)` with `Flag { on: Cell<bool>, n: u32 }`,
    # whose `n` no callee changes through `&f`; `sendb(&tx, s.clone())` with a
    # `Sender<bool>`: none can take a copy, only what was computed from one.
    layouts = TypeLayouts(REVIEW_TYPES)
    for kind, local, takes in (
        ("std::cell::Cell<bool>", "u32", False),
        ("std::cell::Cell<u32>", "u32", True),
        ("Flag", "u32", False),
        ("std::sync::mpsc::Receiver<bool>", "std::string::String", False),
        ("std::sync::mpsc::Receiver<std::string::String>", "std::string::String", True),
        ("std::cell::RefCell<std::vec::Vec<u32>>", "u32", True),
        ("std::sync::atomic::Atomic<bool>", "u32", False),
        ("std::sync::atomic::AtomicU32", "u32", True),
        ("std::cell::Cell<*const u8>", "u32", True),
        ("Dflt<std::cell::Cell<bool>>", "u32", False),
        ("Other", "u32", True),
    ):
        assert _may_take(kind, local, (), layouts=layouts) is takes, kind
    for text, name, marker, statement, holder, variable in (
        (FLAGGED, "flagged", "FLAGGED", "_9 = other()", "c", "x"),
        (RAISED, "raised", "RAISED", "_10 = other()", "f", "x"),
        (SENT_BOOL, "sent_bool", "SENTB", "_14 = other()", "rx", "s"),
    ):
        reason, row, advice = review(text, name, marker, statement, variable=variable)
        assert f"the program's own `{holder}` (at " in reason, reason
        assert "may hold state derived from it that it goes on with" in reason, reason
        assert row.startswith("the program may hold state derived from it ("), row
        assert advice == "Change it before the program uses it (a stop on an earlier line)"
    message = (
        "`x` cannot be changed at src/main.rs:173, at any stop of this build: in a::flagged, at "
        "every place line 173 can stop at, the program's own `c` (at lines 171 and 172) may "
        "hold state derived from `x` that it goes on with, which may be out of Peeky's reach "
        "(behind a cell, a lock, a channel or a pointer its type holds), so a change there may "
        "never be seen"
    )
    assert mutation_hint(message) == (
        "Change it before the program uses it (a stop on an earlier line, or `--set-at` there)."
    )


def test_phantom_data_and_function_types_hold_nothing():
    # `look(&p, x)` with `p: PhantomData<Cell<u32>>`, `look(&t, x)` with `Tag<Cell<u32>>`
    # holding only a `PhantomData<T>` and a `u32`, and `look(&f, x)` with function pointers
    # and items whose signatures name a `Cell`: none stores anything.
    layouts = TypeLayouts(REVIEW_TYPES)
    for kind in (
        "&std::marker::PhantomData<std::cell::Cell<u32>>",
        "&PhantomData<Cell<u32>>",
        "&Tag<std::cell::Cell<u32>>",
        "&Tag<std::sync::mpsc::Sender<u32>>",
        "&for<'a> fn(&'a std::cell::Cell<u32>) -> u32",
        "&for<'a> fn(&'a std::cell::Cell<u32>) -> u32 {read}",
        "&fn(&Cell<u32>) -> u32 {read}",
        "&for<'a> fn(&'a std::cell::Cell<u32>) -> &'a std::cell::Cell<u32>",
        "&fn() -> fn(&std::cell::Cell<u32>) -> std::cell::Cell<u32>",
        '&unsafe extern "C" fn(*mut u32)',
    ):
        assert not _may_write(kind, (), True, layouts=layouts), kind
        assert not _may_share(kind, (), layouts=layouts), kind
    # Nor through a `&mut` its signature names, or a type parameter it names.
    assert not _may_write("fn(&mut std::vec::Vec<u32>)", ())
    assert not _may_write("fn(T) -> T", ("T",))
    for text, name, marker, statement in (
        (PHANTOM, "phantom", "PHANTOM", "_15 = other()"),
        (POINTERS, "pointers", "POINTERS", "_19 = other()"),
    ):
        assert review(text, name, marker, statement) == (None, None), name
