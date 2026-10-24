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
    IrFunction,
    OwnedLocals,
    TypeLayouts,
    _may_share,
    _may_write,
    parse_body,
)
from peeky.rust_symbols import _Die, interior_types

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


def why(text, name, line, statement, interior=INTERIOR):
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
    owned = OwnedLocals([body], {HASH: function}, ("src/main.rs",), texts={"src/main.rs": SOURCE})
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
    found = session.mid_statement({"name": "x"}, frame, "breakpoint")
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
    assert found == {
        **named,
        **{"::" + key.rsplit("::", 1)[-1]: value for key, value in named.items()},
    }
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
