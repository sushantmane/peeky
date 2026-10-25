"""#329 (found fixing #321): a copy a call stores in a `static` or a `thread_local!`, which a
later call reads, was missed. `local_set(&SLOT, x)` keeps `x` in a thread-local `Cell`, and
`SLOT.with(|c| c.get())` reads it back: `x` was `[settable]` after the call, and `--set x=100`
printed the old value. The copy analysis tracked locals only. Now each place that may store a
value in global state with interior mutability (a call given a pointer into it, a function of
the crate's that names it, however deep, a store through such a pointer) stands for one more
holder, which any later code that may read global state uses. A call that provably touches
none (the standard library's, a function of the crate's that names none) stores nothing
there."""

from types import SimpleNamespace

from peeky import rust_mir
from peeky.rust import RustSession
from peeky.rust_mir import (
    _ANY_GLOBAL,
    _GLOBAL_BASE,
    CrateType,
    GlobalState,
    IrFunction,
    OwnedLocals,
    TypeLayouts,
    _crate_traits,
    _impl_trait,
    _inherent,
    _own_trait,
    global_state,
    parse_body,
)

# The crate the fixtures below are rustc 1.96.0's MIR of (`-Zmir-opt-level=0
# -Zdump-mir=PreCodegen -Zmir-include-spans=yes`, as Peeky builds it; its comment lines left
# out).
SOURCE = """use std::cell::Cell;
use std::sync::{Mutex, atomic::{AtomicU32, Ordering}};
use std::thread::LocalKey;

thread_local! {
    static SLOT: Cell<u32> = Cell::new(0);
}
static A: AtomicU32 = AtomicU32::new(0);
static mut RAW: u32 = 0;
static PLAIN: u32 = 7;

#[derive(Debug)]
struct Wrap(u32);

fn other() -> u32 {
    std::hint::black_box(1)
}

fn local_set(k: &'static LocalKey<Cell<u32>>, v: u32) {
    k.with(|c| c.set(v));
}

fn direct_set(v: u32) {
    A.store(v, Ordering::Relaxed);
}

fn outer_set(v: u32) {
    direct_set(v);
}

fn note(v: u32) {
    std::hint::black_box(v + PLAIN);
}

fn local(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    local_set(&SLOT, x);
    let k = other();
    std::hint::black_box(&x);
    SLOT.with(|c| c.get()) + k
}

fn deeper(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    outer_set(x);
    let k = other();
    std::hint::black_box(&x);
    A.load(Ordering::Relaxed) + k
}

fn raw(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    unsafe {
        RAW = x;
    }
    let k = other();
    std::hint::black_box(&x);
    unsafe { RAW + k }
}

fn untouched(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    note(x);
    println!("{:?}", Wrap(x));
    let k = other();
    std::hint::black_box(&x);
    A.load(Ordering::Relaxed) + k
}

fn read_first(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    direct_set(x);
    let k = other();
    x + k + A.load(Ordering::Relaxed)
}

fn returned(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    direct_set(x);
    let k = other();
    std::hint::black_box(&x);
    k
}

fn slot() -> &'static Mutex<u32> {
    &S
}

fn lock_set(m: &Mutex<u32>, v: u32) {
    *m.lock().unwrap() = v;
}

fn returned_ref(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let r = slot();
    lock_set(r, x);
    let k = other();
    std::hint::black_box(&x);
    *slot().lock().unwrap() + k
}

fn through_ptr(p: &mut u32, arg: u32) {
    let x = std::convert::identity(arg);
    *p = x;
    let k = other();
    std::hint::black_box(&x);
    std::hint::black_box(k);
}

fn main() {
    let n: u32 = std::env::args().nth(1).unwrap().parse().unwrap();
    println!("{}", local(n) + deeper(n) + raw(n) + untouched(n) + read_first(n) + returned(n) + returned_ref(n));
    let mut q = 0;
    through_ptr(&mut q, n);
}

static S: Mutex<u32> = Mutex::new(0);
"""

LOCAL = """// MIR for `local` after PreCodegen

fn local(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:35:10: 35:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:35:23: 35:26
    let _2: u32;                         // in scope 0 at src/main.rs:36:9: 36:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:36:36: 36:39
    let _4: ();                          // in scope 0 at src/main.rs:37:5: 37:24
    let mut _5: &std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:37:15: 37:20
    let _6: &std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:37:15: 37:20
    let _7: std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:37:16: 37:20
    let mut _8: u32;                     // in scope 0 at src/main.rs:37:22: 37:23
    let _10: &u32;                       // in scope 0 at src/main.rs:39:5: 39:29
    let mut _11: &u32;                   // in scope 0 at src/main.rs:39:26: 39:28
    let mut _12: u32;                    // in scope 0 at src/main.rs:40:5: 40:27
    let mut _13: &std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:40:5: 40:9
    let _14: std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:40:5: 40:9
    let mut _15: {closure@src/main.rs:40:15: 40:18}; // in scope 0 at src/main.rs:40:15: 40:26
    let mut _16: u32;                    // in scope 0 at src/main.rs:40:30: 40:31
    let mut _17: (u32, bool);            // in scope 0 at src/main.rs:40:5: 40:31
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:36:9: 36:10
        let _9: u32;                     // in scope 1 at src/main.rs:38:9: 38:10
        let mut _19: &std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 1 at src/main.rs:37:15: 37:20
        scope 2 {
            debug k => _9;               // in scope 2 at src/main.rs:38:9: 38:10
            let mut _18: &std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 2 at src/main.rs:40:5: 40:9
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:36:9: 36:10
        StorageLive(_3);                 // scope 0 at src/main.rs:36:36: 36:39
        _3 = copy _1;                    // scope 0 at src/main.rs:36:36: 36:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:36:13: 36:40
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:36:39: 36:40
        StorageLive(_4);                 // scope 1 at src/main.rs:37:5: 37:24
        StorageLive(_5);                 // scope 1 at src/main.rs:37:15: 37:20
        StorageLive(_6);                 // scope 1 at src/main.rs:37:15: 37:20
        _19 = const local::promoted[1];  // scope 1 at src/main.rs:37:15: 37:20
        _6 = &(*_19);                    // scope 1 at src/main.rs:37:15: 37:20
        _5 = &(*_6);                     // scope 1 at src/main.rs:37:15: 37:20
        StorageLive(_8);                 // scope 1 at src/main.rs:37:22: 37:23
        _8 = copy _2;                    // scope 1 at src/main.rs:37:22: 37:23
        _4 = local_set(move _5, move _8) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:37:5: 37:24
    }
    bb2: {
        StorageDead(_8);                 // scope 1 at src/main.rs:37:23: 37:24
        StorageDead(_5);                 // scope 1 at src/main.rs:37:23: 37:24
        StorageDead(_6);                 // scope 1 at src/main.rs:37:24: 37:25
        StorageDead(_4);                 // scope 1 at src/main.rs:37:24: 37:25
        StorageLive(_9);                 // scope 1 at src/main.rs:38:9: 38:10
        _9 = other() -> [return: bb3, unwind continue]; // scope 1 at src/main.rs:38:13: 38:20
    }
    bb3: {
        StorageLive(_10);                // scope 2 at src/main.rs:39:5: 39:29
        StorageLive(_11);                // scope 2 at src/main.rs:39:26: 39:28
        _11 = &_2;                       // scope 2 at src/main.rs:39:26: 39:28
        _10 = std::hint::black_box::<&u32>(move _11) -> [return: bb4, unwind continue]; // scope 2 at src/main.rs:39:5: 39:29
    }
    bb4: {
        StorageDead(_11);                // scope 2 at src/main.rs:39:28: 39:29
        StorageDead(_10);                // scope 2 at src/main.rs:39:29: 39:30
        StorageLive(_12);                // scope 2 at src/main.rs:40:5: 40:27
        StorageLive(_13);                // scope 2 at src/main.rs:40:5: 40:9
        _18 = const local::promoted[0];  // scope 2 at src/main.rs:40:5: 40:9
        _13 = &(*_18);                   // scope 2 at src/main.rs:40:5: 40:9
        StorageLive(_15);                // scope 2 at src/main.rs:40:15: 40:26
        _15 = {closure@src/main.rs:40:15: 40:18}; // scope 2 at src/main.rs:40:15: 40:26
        _12 = LocalKey::<Cell<u32>>::with::<{closure@src/main.rs:40:15: 40:18}, u32>(move _13, move _15) -> [return: bb5, unwind continue]; // scope 2 at src/main.rs:40:5: 40:27
    }
    bb5: {
        StorageDead(_15);                // scope 2 at src/main.rs:40:26: 40:27
        StorageDead(_13);                // scope 2 at src/main.rs:40:26: 40:27
        StorageLive(_16);                // scope 2 at src/main.rs:40:30: 40:31
        _16 = copy _9;                   // scope 2 at src/main.rs:40:30: 40:31
        _17 = AddWithOverflow(copy _12, copy _16); // scope 2 at src/main.rs:40:5: 40:31
        assert(!move (_17.1: bool), "attempt to compute `{} + {}`, which would overflow", move _12, move _16) -> [success: bb6, unwind continue]; // scope 2 at src/main.rs:40:5: 40:31
    }
    bb6: {
        _0 = move (_17.0: u32);          // scope 2 at src/main.rs:40:5: 40:31
        StorageDead(_16);                // scope 2 at src/main.rs:40:30: 40:31
        StorageDead(_12);                // scope 2 at src/main.rs:40:30: 40:31
        StorageDead(_9);                 // scope 1 at src/main.rs:41:1: 41:2
        StorageDead(_2);                 // scope 0 at src/main.rs:41:1: 41:2
        return;                          // scope 0 at src/main.rs:41:2: 41:2
    }
}
"""

LOCAL_SET = """// MIR for `local_set` after PreCodegen

fn local_set(_1: &LocalKey<Cell<u32>>, _2: u32) -> () {
    debug k => _1;                       // in scope 0 at src/main.rs:19:14: 19:15
    debug v => _2;                       // in scope 0 at src/main.rs:19:47: 19:48
    let mut _0: ();                      // return place in scope 0 at src/main.rs:19:54: 19:54
    let _3: ();                          // in scope 0 at src/main.rs:20:5: 20:25
    let mut _4: &std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:20:5: 20:6
    let mut _5: {closure@src/main.rs:20:12: 20:15}; // in scope 0 at src/main.rs:20:12: 20:24
    let mut _6: &u32;                    // in scope 0 at src/main.rs:20:12: 20:24
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:20:5: 20:25
        StorageLive(_4);                 // scope 0 at src/main.rs:20:5: 20:6
        _4 = &(*_1);                     // scope 0 at src/main.rs:20:5: 20:6
        StorageLive(_5);                 // scope 0 at src/main.rs:20:12: 20:24
        StorageLive(_6);                 // scope 0 at src/main.rs:20:12: 20:24
        _6 = &_2;                        // scope 0 at src/main.rs:20:12: 20:24
        _5 = {closure@src/main.rs:20:12: 20:15} { v: move _6 }; // scope 0 at src/main.rs:20:12: 20:24
        StorageDead(_6);                 // scope 0 at src/main.rs:20:14: 20:15
        _3 = LocalKey::<Cell<u32>>::with::<{closure@src/main.rs:20:12: 20:15}, ()>(move _4, move _5) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:20:5: 20:25
    }
    bb1: {
        StorageDead(_5);                 // scope 0 at src/main.rs:20:24: 20:25
        StorageDead(_4);                 // scope 0 at src/main.rs:20:24: 20:25
        StorageDead(_3);                 // scope 0 at src/main.rs:20:25: 20:26
        _0 = const ();                   // scope 0 at src/main.rs:19:55: 21:2
        return;                          // scope 0 at src/main.rs:21:2: 21:2
    }
}
"""

LOCAL_SET_CLOSURE = """// MIR for `local_set::{closure#0}` after PreCodegen

fn local_set::{closure#0}(_1: {closure@src/main.rs:20:12: 20:15}, _2: &Cell<u32>) -> () {
    debug c => _2;                       // in scope 0 at src/main.rs:20:13: 20:14
    debug v => (*(_1.0: &u32));          // in scope 0 at src/main.rs:19:47: 19:48
    let mut _0: ();                      // return place in scope 0 at src/main.rs:20:15: 20:15
    let mut _3: &std::cell::Cell<u32>;   // in scope 0 at src/main.rs:20:16: 20:17
    let mut _4: u32;                     // in scope 0 at src/main.rs:20:22: 20:23
    let mut _5: &u32;                    // in scope 0 at src/main.rs:20:12: 20:24
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:20:16: 20:17
        _3 = &(*_2);                     // scope 0 at src/main.rs:20:16: 20:17
        StorageLive(_4);                 // scope 0 at src/main.rs:20:22: 20:23
        _5 = copy (_1.0: &u32);          // scope 0 at src/main.rs:20:22: 20:23
        _4 = copy (*_5);                 // scope 0 at src/main.rs:20:22: 20:23
        _0 = Cell::<u32>::set(move _3, move _4) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:20:16: 20:24
    }
    bb1: {
        StorageDead(_4);                 // scope 0 at src/main.rs:20:23: 20:24
        StorageDead(_3);                 // scope 0 at src/main.rs:20:23: 20:24
        return;                          // scope 0 at src/main.rs:20:24: 20:24
    }
}
"""

LOCAL_CLOSURE = """// MIR for `local::{closure#0}` after PreCodegen

fn local::{closure#0}(_1: {closure@src/main.rs:40:15: 40:18}, _2: &Cell<u32>) -> u32 {
    debug c => _2;                       // in scope 0 at src/main.rs:40:16: 40:17
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:40:18: 40:18
    let mut _3: &std::cell::Cell<u32>;   // in scope 0 at src/main.rs:40:19: 40:20
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:40:19: 40:20
        _3 = &(*_2);                     // scope 0 at src/main.rs:40:19: 40:20
        _0 = Cell::<u32>::get(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:40:19: 40:26
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:40:25: 40:26
        return;                          // scope 0 at src/main.rs:40:26: 40:26
    }
}
"""

DIRECT_SET = """// MIR for `direct_set` after PreCodegen

fn direct_set(_1: u32) -> () {
    debug v => _1;                       // in scope 0 at src/main.rs:23:15: 23:16
    let mut _0: ();                      // return place in scope 0 at src/main.rs:23:22: 23:22
    let _2: ();                          // in scope 0 at src/main.rs:24:5: 24:34
    let mut _3: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:24:5: 24:6
    let _4: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:24:5: 24:6
    let mut _5: u32;                     // in scope 0 at src/main.rs:24:13: 24:14
    let mut _6: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:24:16: 24:33
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:24:5: 24:34
        StorageLive(_3);                 // scope 0 at src/main.rs:24:5: 24:6
        StorageLive(_4);                 // scope 0 at src/main.rs:24:5: 24:6
        _4 = const {alloc51: &Atomic<u32>}; // scope 0 at src/main.rs:24:5: 24:6
        _3 = &(*_4);                     // scope 0 at src/main.rs:24:5: 24:6
        StorageLive(_5);                 // scope 0 at src/main.rs:24:13: 24:14
        _5 = copy _1;                    // scope 0 at src/main.rs:24:13: 24:14
        StorageLive(_6);                 // scope 0 at src/main.rs:24:16: 24:33
        _6 = std::sync::atomic::Ordering::Relaxed; // scope 0 at src/main.rs:24:16: 24:33
        _2 = Atomic::<u32>::store(move _3, move _5, move _6) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:24:5: 24:34
    }
    bb1: {
        StorageDead(_6);                 // scope 0 at src/main.rs:24:33: 24:34
        StorageDead(_5);                 // scope 0 at src/main.rs:24:33: 24:34
        StorageDead(_3);                 // scope 0 at src/main.rs:24:33: 24:34
        StorageDead(_4);                 // scope 0 at src/main.rs:24:34: 24:35
        StorageDead(_2);                 // scope 0 at src/main.rs:24:34: 24:35
        _0 = const ();                   // scope 0 at src/main.rs:23:23: 25:2
        return;                          // scope 0 at src/main.rs:25:2: 25:2
    }
}
"""

OUTER_SET = """// MIR for `outer_set` after PreCodegen

fn outer_set(_1: u32) -> () {
    debug v => _1;                       // in scope 0 at src/main.rs:27:14: 27:15
    let mut _0: ();                      // return place in scope 0 at src/main.rs:27:21: 27:21
    let _2: ();                          // in scope 0 at src/main.rs:28:5: 28:18
    let mut _3: u32;                     // in scope 0 at src/main.rs:28:16: 28:17
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:28:5: 28:18
        StorageLive(_3);                 // scope 0 at src/main.rs:28:16: 28:17
        _3 = copy _1;                    // scope 0 at src/main.rs:28:16: 28:17
        _2 = direct_set(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:28:5: 28:18
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:28:17: 28:18
        StorageDead(_2);                 // scope 0 at src/main.rs:28:18: 28:19
        _0 = const ();                   // scope 0 at src/main.rs:27:22: 29:2
        return;                          // scope 0 at src/main.rs:29:2: 29:2
    }
}
"""

DEEPER = """// MIR for `deeper` after PreCodegen

fn deeper(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:43:11: 43:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:43:24: 43:27
    let _2: u32;                         // in scope 0 at src/main.rs:44:9: 44:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:44:36: 44:39
    let _4: ();                          // in scope 0 at src/main.rs:45:5: 45:17
    let mut _5: u32;                     // in scope 0 at src/main.rs:45:15: 45:16
    let _7: &u32;                        // in scope 0 at src/main.rs:47:5: 47:29
    let mut _8: &u32;                    // in scope 0 at src/main.rs:47:26: 47:28
    let mut _9: u32;                     // in scope 0 at src/main.rs:48:5: 48:30
    let mut _10: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:48:5: 48:6
    let _11: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:48:5: 48:6
    let mut _12: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:48:12: 48:29
    let mut _13: u32;                    // in scope 0 at src/main.rs:48:33: 48:34
    let mut _14: (u32, bool);            // in scope 0 at src/main.rs:48:5: 48:34
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:44:9: 44:10
        let _6: u32;                     // in scope 1 at src/main.rs:46:9: 46:10
        scope 2 {
            debug k => _6;               // in scope 2 at src/main.rs:46:9: 46:10
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:44:9: 44:10
        StorageLive(_3);                 // scope 0 at src/main.rs:44:36: 44:39
        _3 = copy _1;                    // scope 0 at src/main.rs:44:36: 44:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:44:13: 44:40
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:44:39: 44:40
        StorageLive(_4);                 // scope 1 at src/main.rs:45:5: 45:17
        StorageLive(_5);                 // scope 1 at src/main.rs:45:15: 45:16
        _5 = copy _2;                    // scope 1 at src/main.rs:45:15: 45:16
        _4 = outer_set(move _5) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:45:5: 45:17
    }
    bb2: {
        StorageDead(_5);                 // scope 1 at src/main.rs:45:16: 45:17
        StorageDead(_4);                 // scope 1 at src/main.rs:45:17: 45:18
        StorageLive(_6);                 // scope 1 at src/main.rs:46:9: 46:10
        _6 = other() -> [return: bb3, unwind continue]; // scope 1 at src/main.rs:46:13: 46:20
    }
    bb3: {
        StorageLive(_7);                 // scope 2 at src/main.rs:47:5: 47:29
        StorageLive(_8);                 // scope 2 at src/main.rs:47:26: 47:28
        _8 = &_2;                        // scope 2 at src/main.rs:47:26: 47:28
        _7 = std::hint::black_box::<&u32>(move _8) -> [return: bb4, unwind continue]; // scope 2 at src/main.rs:47:5: 47:29
    }
    bb4: {
        StorageDead(_8);                 // scope 2 at src/main.rs:47:28: 47:29
        StorageDead(_7);                 // scope 2 at src/main.rs:47:29: 47:30
        StorageLive(_9);                 // scope 2 at src/main.rs:48:5: 48:30
        StorageLive(_10);                // scope 2 at src/main.rs:48:5: 48:6
        StorageLive(_11);                // scope 2 at src/main.rs:48:5: 48:6
        _11 = const {alloc51: &Atomic<u32>}; // scope 2 at src/main.rs:48:5: 48:6
        _10 = &(*_11);                   // scope 2 at src/main.rs:48:5: 48:6
        StorageLive(_12);                // scope 2 at src/main.rs:48:12: 48:29
        _12 = std::sync::atomic::Ordering::Relaxed; // scope 2 at src/main.rs:48:12: 48:29
        _9 = Atomic::<u32>::load(move _10, move _12) -> [return: bb5, unwind continue]; // scope 2 at src/main.rs:48:5: 48:30
    }
    bb5: {
        StorageDead(_12);                // scope 2 at src/main.rs:48:29: 48:30
        StorageDead(_10);                // scope 2 at src/main.rs:48:29: 48:30
        StorageLive(_13);                // scope 2 at src/main.rs:48:33: 48:34
        _13 = copy _6;                   // scope 2 at src/main.rs:48:33: 48:34
        _14 = AddWithOverflow(copy _9, copy _13); // scope 2 at src/main.rs:48:5: 48:34
        assert(!move (_14.1: bool), "attempt to compute `{} + {}`, which would overflow", move _9, move _13) -> [success: bb6, unwind continue]; // scope 2 at src/main.rs:48:5: 48:34
    }
    bb6: {
        _0 = move (_14.0: u32);          // scope 2 at src/main.rs:48:5: 48:34
        StorageDead(_13);                // scope 2 at src/main.rs:48:33: 48:34
        StorageDead(_11);                // scope 2 at src/main.rs:48:33: 48:34
        StorageDead(_9);                 // scope 2 at src/main.rs:48:33: 48:34
        StorageDead(_6);                 // scope 1 at src/main.rs:49:1: 49:2
        StorageDead(_2);                 // scope 0 at src/main.rs:49:1: 49:2
        return;                          // scope 0 at src/main.rs:49:2: 49:2
    }
}
"""

NOTE = """// MIR for `note` after PreCodegen

fn note(_1: u32) -> () {
    debug v => _1;                       // in scope 0 at src/main.rs:31:9: 31:10
    let mut _0: ();                      // return place in scope 0 at src/main.rs:31:16: 31:16
    let _2: u32;                         // in scope 0 at src/main.rs:32:5: 32:36
    let mut _3: u32;                     // in scope 0 at src/main.rs:32:26: 32:35
    let mut _4: u32;                     // in scope 0 at src/main.rs:32:26: 32:27
    let mut _5: u32;                     // in scope 0 at src/main.rs:32:30: 32:35
    let mut _6: &u32;                    // in scope 0 at src/main.rs:32:30: 32:35
    let mut _7: (u32, bool);             // in scope 0 at src/main.rs:32:26: 32:35
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:32:5: 32:36
        StorageLive(_3);                 // scope 0 at src/main.rs:32:26: 32:35
        StorageLive(_4);                 // scope 0 at src/main.rs:32:26: 32:27
        _4 = copy _1;                    // scope 0 at src/main.rs:32:26: 32:27
        StorageLive(_5);                 // scope 0 at src/main.rs:32:30: 32:35
        StorageLive(_6);                 // scope 0 at src/main.rs:32:30: 32:35
        _6 = const {alloc53: &u32};      // scope 0 at src/main.rs:32:30: 32:35
        _5 = copy (*_6);                 // scope 0 at src/main.rs:32:30: 32:35
        _7 = AddWithOverflow(copy _4, copy _5); // scope 0 at src/main.rs:32:26: 32:35
        assert(!move (_7.1: bool), "attempt to compute `{} + {}`, which would overflow", move _4, move _5) -> [success: bb1, unwind continue]; // scope 0 at src/main.rs:32:26: 32:35
    }
    bb1: {
        _3 = move (_7.0: u32);           // scope 0 at src/main.rs:32:26: 32:35
        StorageDead(_5);                 // scope 0 at src/main.rs:32:34: 32:35
        StorageDead(_4);                 // scope 0 at src/main.rs:32:34: 32:35
        _2 = std::hint::black_box::<u32>(move _3) -> [return: bb2, unwind continue]; // scope 0 at src/main.rs:32:5: 32:36
    }
    bb2: {
        StorageDead(_3);                 // scope 0 at src/main.rs:32:35: 32:36
        StorageDead(_6);                 // scope 0 at src/main.rs:32:36: 32:37
        StorageDead(_2);                 // scope 0 at src/main.rs:32:36: 32:37
        _0 = const ();                   // scope 0 at src/main.rs:31:17: 33:2
        return;                          // scope 0 at src/main.rs:33:2: 33:2
    }
}
"""

OTHER = """// MIR for `other` after PreCodegen

fn other() -> u32 {
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:15:15: 15:18
    bb0: {
        _0 = std::hint::black_box::<u32>(const 1_u32) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:16:5: 16:28
    }
    bb1: {
        return;                          // scope 0 at src/main.rs:17:2: 17:2
    }
}
"""

RAW = """// MIR for `raw` after PreCodegen

fn raw(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:51:8: 51:11
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:51:21: 51:24
    let _2: u32;                         // in scope 0 at src/main.rs:52:9: 52:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:52:36: 52:39
    let _4: ();                          // in scope 0 at src/main.rs:53:5: 55:6
    let mut _5: u32;                     // in scope 0 at src/main.rs:54:15: 54:16
    let mut _6: *mut u32;                // in scope 0 at src/main.rs:54:9: 54:12
    let _8: &u32;                        // in scope 0 at src/main.rs:57:5: 57:29
    let mut _9: &u32;                    // in scope 0 at src/main.rs:57:26: 57:28
    let mut _10: u32;                    // in scope 0 at src/main.rs:58:14: 58:17
    let mut _11: *mut u32;               // in scope 0 at src/main.rs:58:14: 58:17
    let mut _12: u32;                    // in scope 0 at src/main.rs:58:20: 58:21
    let mut _13: (u32, bool);            // in scope 0 at src/main.rs:58:14: 58:21
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:52:9: 52:10
        let _7: u32;                     // in scope 1 at src/main.rs:56:9: 56:10
        let mut _19: *const ();          // in scope 1 at src/main.rs:54:9: 54:16
        let mut _20: usize;              // in scope 1 at src/main.rs:54:9: 54:16
        let mut _21: usize;              // in scope 1 at src/main.rs:54:9: 54:16
        let mut _22: usize;              // in scope 1 at src/main.rs:54:9: 54:16
        let mut _23: bool;               // in scope 1 at src/main.rs:54:9: 54:16
        let mut _24: *const ();          // in scope 1 at src/main.rs:54:9: 54:16
        let mut _25: usize;              // in scope 1 at src/main.rs:54:9: 54:16
        let mut _26: bool;               // in scope 1 at src/main.rs:54:9: 54:16
        let mut _27: bool;               // in scope 1 at src/main.rs:54:9: 54:16
        let mut _28: bool;               // in scope 1 at src/main.rs:54:9: 54:16
        let mut _29: bool;               // in scope 1 at src/main.rs:54:9: 54:16
        scope 2 {
            debug k => _7;               // in scope 2 at src/main.rs:56:9: 56:10
            let mut _14: *const ();      // in scope 2 at src/main.rs:58:14: 58:17
            let mut _15: usize;          // in scope 2 at src/main.rs:58:14: 58:17
            let mut _16: usize;          // in scope 2 at src/main.rs:58:14: 58:17
            let mut _17: usize;          // in scope 2 at src/main.rs:58:14: 58:17
            let mut _18: bool;           // in scope 2 at src/main.rs:58:14: 58:17
            let mut _30: *const ();      // in scope 2 at src/main.rs:58:14: 58:17
            let mut _31: usize;          // in scope 2 at src/main.rs:58:14: 58:17
            let mut _32: bool;           // in scope 2 at src/main.rs:58:14: 58:17
            let mut _33: bool;           // in scope 2 at src/main.rs:58:14: 58:17
            let mut _34: bool;           // in scope 2 at src/main.rs:58:14: 58:17
            let mut _35: bool;           // in scope 2 at src/main.rs:58:14: 58:17
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:52:9: 52:10
        StorageLive(_3);                 // scope 0 at src/main.rs:52:36: 52:39
        _3 = copy _1;                    // scope 0 at src/main.rs:52:36: 52:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:52:13: 52:40
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:52:39: 52:40
        StorageLive(_4);                 // scope 1 at src/main.rs:53:5: 55:6
        StorageLive(_5);                 // scope 1 at src/main.rs:54:15: 54:16
        _5 = copy _2;                    // scope 1 at src/main.rs:54:15: 54:16
        StorageLive(_6);                 // scope 1 at src/main.rs:54:9: 54:12
        _6 = const {alloc52: *mut u32};  // scope 1 at src/main.rs:54:9: 54:12
        _19 = copy _6 as *const () (PtrToPtr); // scope 1 at src/main.rs:54:9: 54:16
        _20 = copy _19 as usize (Transmute); // scope 1 at src/main.rs:54:9: 54:16
        _21 = Sub(const <u32 as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 1 at src/main.rs:54:9: 54:16
        _22 = BitAnd(copy _20, copy _21); // scope 1 at src/main.rs:54:9: 54:16
        _23 = Eq(copy _22, const 0_usize); // scope 1 at src/main.rs:54:9: 54:16
        assert(copy _23, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <u32 as std::mem::SizedTypeProperties>::ALIGN, copy _20) -> [success: bb6, unwind unreachable]; // scope 1 at src/main.rs:54:9: 54:16
    }
    bb2: {
        StorageLive(_8);                 // scope 2 at src/main.rs:57:5: 57:29
        StorageLive(_9);                 // scope 2 at src/main.rs:57:26: 57:28
        _9 = &_2;                        // scope 2 at src/main.rs:57:26: 57:28
        _8 = std::hint::black_box::<&u32>(move _9) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:57:5: 57:29
    }
    bb3: {
        StorageDead(_9);                 // scope 2 at src/main.rs:57:28: 57:29
        StorageDead(_8);                 // scope 2 at src/main.rs:57:29: 57:30
        StorageLive(_10);                // scope 2 at src/main.rs:58:14: 58:17
        StorageLive(_11);                // scope 2 at src/main.rs:58:14: 58:17
        _11 = const {alloc52: *mut u32}; // scope 2 at src/main.rs:58:14: 58:17
        _14 = copy _11 as *const () (PtrToPtr); // scope 2 at src/main.rs:58:14: 58:17
        _15 = copy _14 as usize (Transmute); // scope 2 at src/main.rs:58:14: 58:17
        _16 = Sub(const <u32 as std::mem::SizedTypeProperties>::ALIGN, const 1_usize); // scope 2 at src/main.rs:58:14: 58:17
        _17 = BitAnd(copy _15, copy _16); // scope 2 at src/main.rs:58:14: 58:17
        _18 = Eq(copy _17, const 0_usize); // scope 2 at src/main.rs:58:14: 58:17
        assert(copy _18, "misaligned pointer dereference: address must be a multiple of {} but is {}", const <u32 as std::mem::SizedTypeProperties>::ALIGN, copy _15) -> [success: bb5, unwind unreachable]; // scope 2 at src/main.rs:58:14: 58:17
    }
    bb4: {
        _0 = move (_13.0: u32);          // scope 2 at src/main.rs:58:14: 58:21
        StorageDead(_12);                // scope 2 at src/main.rs:58:20: 58:21
        StorageDead(_11);                // scope 2 at src/main.rs:58:20: 58:21
        StorageDead(_10);                // scope 2 at src/main.rs:58:20: 58:21
        StorageDead(_7);                 // scope 1 at src/main.rs:59:1: 59:2
        StorageDead(_2);                 // scope 0 at src/main.rs:59:1: 59:2
        return;                          // scope 0 at src/main.rs:59:2: 59:2
    }
    bb5: {
        _30 = copy _11 as *const () (PtrToPtr); // scope 2 at src/main.rs:58:14: 58:17
        _31 = copy _30 as usize (Transmute); // scope 2 at src/main.rs:58:14: 58:17
        _32 = Ne(const <u32 as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 2 at src/main.rs:58:14: 58:17
        _33 = Eq(copy _31, const 0_usize); // scope 2 at src/main.rs:58:14: 58:17
        _34 = BitAnd(copy _33, copy _32); // scope 2 at src/main.rs:58:14: 58:17
        _35 = Not(copy _34);             // scope 2 at src/main.rs:58:14: 58:17
        assert(copy _35, "null pointer dereference occurred") -> [success: bb8, unwind unreachable]; // scope 2 at src/main.rs:58:14: 58:17
    }
    bb6: {
        _24 = copy _6 as *const () (PtrToPtr); // scope 1 at src/main.rs:54:9: 54:16
        _25 = copy _24 as usize (Transmute); // scope 1 at src/main.rs:54:9: 54:16
        _26 = Ne(const <u32 as std::mem::SizedTypeProperties>::SIZE, const 0_usize); // scope 1 at src/main.rs:54:9: 54:16
        _27 = Eq(copy _25, const 0_usize); // scope 1 at src/main.rs:54:9: 54:16
        _28 = BitAnd(copy _27, copy _26); // scope 1 at src/main.rs:54:9: 54:16
        _29 = Not(copy _28);             // scope 1 at src/main.rs:54:9: 54:16
        assert(copy _29, "null pointer dereference occurred") -> [success: bb7, unwind unreachable]; // scope 1 at src/main.rs:54:9: 54:16
    }
    bb7: {
        (*_6) = move _5;                 // scope 1 at src/main.rs:54:9: 54:16
        StorageDead(_5);                 // scope 1 at src/main.rs:54:15: 54:16
        StorageDead(_6);                 // scope 1 at src/main.rs:54:16: 54:17
        _4 = const ();                   // scope 1 at src/main.rs:53:5: 55:6
        StorageDead(_4);                 // scope 1 at src/main.rs:55:5: 55:6
        StorageLive(_7);                 // scope 1 at src/main.rs:56:9: 56:10
        _7 = other() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:56:13: 56:20
    }
    bb8: {
        _10 = copy (*_11);               // scope 2 at src/main.rs:58:14: 58:17
        StorageLive(_12);                // scope 2 at src/main.rs:58:20: 58:21
        _12 = copy _7;                   // scope 2 at src/main.rs:58:20: 58:21
        _13 = AddWithOverflow(copy _10, copy _12); // scope 2 at src/main.rs:58:14: 58:21
        assert(!move (_13.1: bool), "attempt to compute `{} + {}`, which would overflow", move _10, move _12) -> [success: bb4, unwind continue]; // scope 2 at src/main.rs:58:14: 58:21
    }
}
"""

UNTOUCHED = """// MIR for `untouched` after PreCodegen

fn untouched(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:61:14: 61:17
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:61:27: 61:30
    let _2: u32;                         // in scope 0 at src/main.rs:62:9: 62:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:62:36: 62:39
    let _4: ();                          // in scope 0 at src/main.rs:63:5: 63:12
    let mut _5: u32;                     // in scope 0 at src/main.rs:63:10: 63:11
    let _6: ();                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
    let _7: ();                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
    let mut _8: std::fmt::Arguments<'_>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let mut _10: &Wrap;                  // in scope 0 at src/main.rs:64:22: 64:29
    let _11: Wrap;                       // in scope 0 at src/main.rs:64:22: 64:29
    let mut _12: u32;                    // in scope 0 at src/main.rs:64:27: 64:28
    let mut _14: core::fmt::rt::Argument<'_>; // in scope 0 at src/main.rs:64:15: 64:19
    let mut _15: &Wrap;                  // in scope 0 at src/main.rs:64:22: 64:29
    let mut _16: &[u8; 4];               // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let _17: &[u8; 4];                   // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let mut _18: &[core::fmt::rt::Argument<'_>; 1]; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let _19: &[core::fmt::rt::Argument<'_>; 1]; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let _21: &u32;                       // in scope 0 at src/main.rs:66:5: 66:29
    let mut _22: &u32;                   // in scope 0 at src/main.rs:66:26: 66:28
    let mut _23: u32;                    // in scope 0 at src/main.rs:67:5: 67:30
    let mut _24: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:67:5: 67:6
    let _25: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:67:5: 67:6
    let mut _26: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:67:12: 67:29
    let mut _27: u32;                    // in scope 0 at src/main.rs:67:33: 67:34
    let mut _28: (u32, bool);            // in scope 0 at src/main.rs:67:5: 67:34
    let mut _29: &Wrap;                  // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:62:9: 62:10
        let _9: (&Wrap,);                // in scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        let _20: u32;                    // in scope 1 at src/main.rs:65:9: 65:10
        scope 2 {
            debug args => _9;            // in scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
            let _13: [core::fmt::rt::Argument<'_>; 1]; // in scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
            scope 3 {
                debug args => _13;       // in scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
            }
        }
        scope 4 {
            debug k => _20;              // in scope 4 at src/main.rs:65:9: 65:10
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
        StorageLive(_4);                 // scope 1 at src/main.rs:63:5: 63:12
        StorageLive(_5);                 // scope 1 at src/main.rs:63:10: 63:11
        _5 = copy _2;                    // scope 1 at src/main.rs:63:10: 63:11
        _4 = note(move _5) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:63:5: 63:12
    }
    bb2: {
        StorageDead(_5);                 // scope 1 at src/main.rs:63:11: 63:12
        StorageDead(_4);                 // scope 1 at src/main.rs:63:12: 63:13
        StorageLive(_6);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
        StorageLive(_7);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
        StorageLive(_8);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_9);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_10);                // scope 1 at src/main.rs:64:22: 64:29
        StorageLive(_11);                // scope 1 at src/main.rs:64:22: 64:29
        StorageLive(_12);                // scope 1 at src/main.rs:64:27: 64:28
        _12 = copy _2;                   // scope 1 at src/main.rs:64:27: 64:28
        _11 = Wrap(move _12);            // scope 1 at src/main.rs:64:22: 64:29
        StorageDead(_12);                // scope 1 at src/main.rs:64:28: 64:29
        _10 = &_11;                      // scope 1 at src/main.rs:64:22: 64:29
        _9 = (move _10,);                // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageDead(_10);                // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageLive(_13);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_14);                // scope 2 at src/main.rs:64:15: 64:19
        StorageLive(_15);                // scope 2 at src/main.rs:64:22: 64:29
        _29 = copy (_9.0: &Wrap);        // scope 2 at src/main.rs:64:22: 64:29
        _15 = &(*_29);                   // scope 2 at src/main.rs:64:22: 64:29
        _14 = core::fmt::rt::Argument::<'_>::new_debug::<Wrap>(move _15) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:64:15: 64:19
    }
    bb3: {
        StorageDead(_15);                // scope 2 at src/main.rs:64:18: 64:19
        _13 = [move _14];                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageDead(_14);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageLive(_16);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_17);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _17 = const b"";   // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _16 = &(*_17);                   // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_18);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_19);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _19 = &_13;                      // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _18 = &(*_19);                   // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _8 = Arguments::<'_>::new::<4, 1>(move _16, move _18) -> [return: bb4, unwind continue]; // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    }
    bb4: {
        StorageDead(_19);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_18);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_17);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_16);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        _7 = std::io::_print(move _8) -> [return: bb5, unwind continue]; // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
    }
    bb5: {
        StorageDead(_8);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:61: 146:62
        StorageDead(_13);                // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        StorageDead(_11);                // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        StorageDead(_9);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        StorageDead(_7);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        _6 = const ();                   // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
        StorageDead(_6);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:147:5: 147:6
        StorageLive(_20);                // scope 1 at src/main.rs:65:9: 65:10
        _20 = other() -> [return: bb6, unwind continue]; // scope 1 at src/main.rs:65:13: 65:20
    }
    bb6: {
        StorageLive(_21);                // scope 4 at src/main.rs:66:5: 66:29
        StorageLive(_22);                // scope 4 at src/main.rs:66:26: 66:28
        _22 = &_2;                       // scope 4 at src/main.rs:66:26: 66:28
        _21 = std::hint::black_box::<&u32>(move _22) -> [return: bb7, unwind continue]; // scope 4 at src/main.rs:66:5: 66:29
    }
    bb7: {
        StorageDead(_22);                // scope 4 at src/main.rs:66:28: 66:29
        StorageDead(_21);                // scope 4 at src/main.rs:66:29: 66:30
        StorageLive(_23);                // scope 4 at src/main.rs:67:5: 67:30
        StorageLive(_24);                // scope 4 at src/main.rs:67:5: 67:6
        StorageLive(_25);                // scope 4 at src/main.rs:67:5: 67:6
        _25 = const {alloc51: &Atomic<u32>}; // scope 4 at src/main.rs:67:5: 67:6
        _24 = &(*_25);                   // scope 4 at src/main.rs:67:5: 67:6
        StorageLive(_26);                // scope 4 at src/main.rs:67:12: 67:29
        _26 = std::sync::atomic::Ordering::Relaxed; // scope 4 at src/main.rs:67:12: 67:29
        _23 = Atomic::<u32>::load(move _24, move _26) -> [return: bb8, unwind continue]; // scope 4 at src/main.rs:67:5: 67:30
    }
    bb8: {
        StorageDead(_26);                // scope 4 at src/main.rs:67:29: 67:30
        StorageDead(_24);                // scope 4 at src/main.rs:67:29: 67:30
        StorageLive(_27);                // scope 4 at src/main.rs:67:33: 67:34
        _27 = copy _20;                  // scope 4 at src/main.rs:67:33: 67:34
        _28 = AddWithOverflow(copy _23, copy _27); // scope 4 at src/main.rs:67:5: 67:34
        assert(!move (_28.1: bool), "attempt to compute `{} + {}`, which would overflow", move _23, move _27) -> [success: bb9, unwind continue]; // scope 4 at src/main.rs:67:5: 67:34
    }
    bb9: {
        _0 = move (_28.0: u32);          // scope 4 at src/main.rs:67:5: 67:34
        StorageDead(_27);                // scope 4 at src/main.rs:67:33: 67:34
        StorageDead(_25);                // scope 4 at src/main.rs:67:33: 67:34
        StorageDead(_23);                // scope 4 at src/main.rs:67:33: 67:34
        StorageDead(_20);                // scope 1 at src/main.rs:68:1: 68:2
        StorageDead(_2);                 // scope 0 at src/main.rs:68:1: 68:2
        return;                          // scope 0 at src/main.rs:68:2: 68:2
    }
}
"""

READ_FIRST = """// MIR for `read_first` after PreCodegen

fn read_first(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:70:15: 70:18
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:70:28: 70:31
    let _2: u32;                         // in scope 0 at src/main.rs:71:9: 71:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:71:36: 71:39
    let _4: ();                          // in scope 0 at src/main.rs:72:5: 72:18
    let mut _5: u32;                     // in scope 0 at src/main.rs:72:16: 72:17
    let mut _7: u32;                     // in scope 0 at src/main.rs:74:5: 74:10
    let mut _8: u32;                     // in scope 0 at src/main.rs:74:5: 74:6
    let mut _9: u32;                     // in scope 0 at src/main.rs:74:9: 74:10
    let mut _10: (u32, bool);            // in scope 0 at src/main.rs:74:5: 74:10
    let mut _11: u32;                    // in scope 0 at src/main.rs:74:13: 74:38
    let mut _12: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:74:13: 74:14
    let _13: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:74:13: 74:14
    let mut _14: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:74:20: 74:37
    let mut _15: (u32, bool);            // in scope 0 at src/main.rs:74:5: 74:38
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:71:9: 71:10
        let _6: u32;                     // in scope 1 at src/main.rs:73:9: 73:10
        scope 2 {
            debug k => _6;               // in scope 2 at src/main.rs:73:9: 73:10
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
        StorageLive(_4);                 // scope 1 at src/main.rs:72:5: 72:18
        StorageLive(_5);                 // scope 1 at src/main.rs:72:16: 72:17
        _5 = copy _2;                    // scope 1 at src/main.rs:72:16: 72:17
        _4 = direct_set(move _5) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:72:5: 72:18
    }
    bb2: {
        StorageDead(_5);                 // scope 1 at src/main.rs:72:17: 72:18
        StorageDead(_4);                 // scope 1 at src/main.rs:72:18: 72:19
        StorageLive(_6);                 // scope 1 at src/main.rs:73:9: 73:10
        _6 = other() -> [return: bb3, unwind continue]; // scope 1 at src/main.rs:73:13: 73:20
    }
    bb3: {
        StorageLive(_7);                 // scope 2 at src/main.rs:74:5: 74:10
        StorageLive(_8);                 // scope 2 at src/main.rs:74:5: 74:6
        _8 = copy _2;                    // scope 2 at src/main.rs:74:5: 74:6
        StorageLive(_9);                 // scope 2 at src/main.rs:74:9: 74:10
        _9 = copy _6;                    // scope 2 at src/main.rs:74:9: 74:10
        _10 = AddWithOverflow(copy _8, copy _9); // scope 2 at src/main.rs:74:5: 74:10
        assert(!move (_10.1: bool), "attempt to compute `{} + {}`, which would overflow", move _8, move _9) -> [success: bb4, unwind continue]; // scope 2 at src/main.rs:74:5: 74:10
    }
    bb4: {
        _7 = move (_10.0: u32);          // scope 2 at src/main.rs:74:5: 74:10
        StorageDead(_9);                 // scope 2 at src/main.rs:74:9: 74:10
        StorageDead(_8);                 // scope 2 at src/main.rs:74:9: 74:10
        StorageLive(_11);                // scope 2 at src/main.rs:74:13: 74:38
        StorageLive(_12);                // scope 2 at src/main.rs:74:13: 74:14
        StorageLive(_13);                // scope 2 at src/main.rs:74:13: 74:14
        _13 = const {alloc51: &Atomic<u32>}; // scope 2 at src/main.rs:74:13: 74:14
        _12 = &(*_13);                   // scope 2 at src/main.rs:74:13: 74:14
        StorageLive(_14);                // scope 2 at src/main.rs:74:20: 74:37
        _14 = std::sync::atomic::Ordering::Relaxed; // scope 2 at src/main.rs:74:20: 74:37
        _11 = Atomic::<u32>::load(move _12, move _14) -> [return: bb5, unwind continue]; // scope 2 at src/main.rs:74:13: 74:38
    }
    bb5: {
        StorageDead(_14);                // scope 2 at src/main.rs:74:37: 74:38
        StorageDead(_12);                // scope 2 at src/main.rs:74:37: 74:38
        _15 = AddWithOverflow(copy _7, copy _11); // scope 2 at src/main.rs:74:5: 74:38
        assert(!move (_15.1: bool), "attempt to compute `{} + {}`, which would overflow", move _7, move _11) -> [success: bb6, unwind continue]; // scope 2 at src/main.rs:74:5: 74:38
    }
    bb6: {
        _0 = move (_15.0: u32);          // scope 2 at src/main.rs:74:5: 74:38
        StorageDead(_13);                // scope 2 at src/main.rs:74:37: 74:38
        StorageDead(_11);                // scope 2 at src/main.rs:74:37: 74:38
        StorageDead(_7);                 // scope 2 at src/main.rs:74:37: 74:38
        StorageDead(_6);                 // scope 1 at src/main.rs:75:1: 75:2
        StorageDead(_2);                 // scope 0 at src/main.rs:75:1: 75:2
        return;                          // scope 0 at src/main.rs:75:2: 75:2
    }
}
"""

RETURNED = """// MIR for `returned` after PreCodegen

fn returned(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:77:13: 77:16
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:77:26: 77:29
    let _2: u32;                         // in scope 0 at src/main.rs:78:9: 78:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:78:36: 78:39
    let _4: ();                          // in scope 0 at src/main.rs:79:5: 79:18
    let mut _5: u32;                     // in scope 0 at src/main.rs:79:16: 79:17
    let _7: &u32;                        // in scope 0 at src/main.rs:81:5: 81:29
    let mut _8: &u32;                    // in scope 0 at src/main.rs:81:26: 81:28
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:78:9: 78:10
        let _6: u32;                     // in scope 1 at src/main.rs:80:9: 80:10
        scope 2 {
            debug k => _6;               // in scope 2 at src/main.rs:80:9: 80:10
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:78:9: 78:10
        StorageLive(_3);                 // scope 0 at src/main.rs:78:36: 78:39
        _3 = copy _1;                    // scope 0 at src/main.rs:78:36: 78:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:78:13: 78:40
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:78:39: 78:40
        StorageLive(_4);                 // scope 1 at src/main.rs:79:5: 79:18
        StorageLive(_5);                 // scope 1 at src/main.rs:79:16: 79:17
        _5 = copy _2;                    // scope 1 at src/main.rs:79:16: 79:17
        _4 = direct_set(move _5) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:79:5: 79:18
    }
    bb2: {
        StorageDead(_5);                 // scope 1 at src/main.rs:79:17: 79:18
        StorageDead(_4);                 // scope 1 at src/main.rs:79:18: 79:19
        StorageLive(_6);                 // scope 1 at src/main.rs:80:9: 80:10
        _6 = other() -> [return: bb3, unwind continue]; // scope 1 at src/main.rs:80:13: 80:20
    }
    bb3: {
        StorageLive(_7);                 // scope 2 at src/main.rs:81:5: 81:29
        StorageLive(_8);                 // scope 2 at src/main.rs:81:26: 81:28
        _8 = &_2;                        // scope 2 at src/main.rs:81:26: 81:28
        _7 = std::hint::black_box::<&u32>(move _8) -> [return: bb4, unwind continue]; // scope 2 at src/main.rs:81:5: 81:29
    }
    bb4: {
        StorageDead(_8);                 // scope 2 at src/main.rs:81:28: 81:29
        StorageDead(_7);                 // scope 2 at src/main.rs:81:29: 81:30
        _0 = copy _6;                    // scope 2 at src/main.rs:82:5: 82:6
        StorageDead(_6);                 // scope 1 at src/main.rs:83:1: 83:2
        StorageDead(_2);                 // scope 0 at src/main.rs:83:1: 83:2
        return;                          // scope 0 at src/main.rs:83:2: 83:2
    }
}
"""

SLOT_FN = """// MIR for `slot` after PreCodegen

fn slot() -> &std::sync::Mutex<u32> {
    let mut _0: &std::sync::Mutex<u32>;  // return place in scope 0 at src/main.rs:85:14: 85:33
    let _1: &std::sync::Mutex<u32>;      // in scope 0 at src/main.rs:86:5: 86:7
    let _2: &std::sync::Mutex<u32>;      // in scope 0 at src/main.rs:86:6: 86:7
    bb0: {
        StorageLive(_1);                 // scope 0 at src/main.rs:86:5: 86:7
        StorageLive(_2);                 // scope 0 at src/main.rs:86:6: 86:7
        _2 = const {alloc30: &std::sync::Mutex<u32>}; // scope 0 at src/main.rs:86:6: 86:7
        _1 = &(*_2);                     // scope 0 at src/main.rs:86:5: 86:7
        _0 = &(*_1);                     // scope 0 at src/main.rs:86:5: 86:7
        StorageDead(_2);                 // scope 0 at src/main.rs:86:6: 86:7
        StorageDead(_1);                 // scope 0 at src/main.rs:86:6: 86:7
        return;                          // scope 0 at src/main.rs:87:2: 87:2
    }
}
"""

LOCK_SET = """// MIR for `lock_set` after PreCodegen

fn lock_set(_1: &std::sync::Mutex<u32>, _2: u32) -> () {
    debug m => _1;                       // in scope 0 at src/main.rs:89:13: 89:14
    debug v => _2;                       // in scope 0 at src/main.rs:89:29: 89:30
    let mut _0: ();                      // return place in scope 0 at src/main.rs:89:36: 89:36
    let mut _3: u32;                     // in scope 0 at src/main.rs:90:26: 90:27
    let mut _4: &mut u32;                // in scope 0 at src/main.rs:90:5: 90:23
    let mut _5: &mut std::sync::MutexGuard<'_, u32>; // in scope 0 at src/main.rs:90:6: 90:23
    let mut _6: std::sync::MutexGuard<'_, u32>; // in scope 0 at src/main.rs:90:6: 90:23
    let mut _7: std::result::Result<std::sync::MutexGuard<'_, u32>, std::sync::PoisonError<std::sync::MutexGuard<'_, u32>>>; // in scope 0 at src/main.rs:90:6: 90:14
    let mut _8: &std::sync::Mutex<u32>;  // in scope 0 at src/main.rs:90:6: 90:7
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:90:26: 90:27
        _3 = copy _2;                    // scope 0 at src/main.rs:90:26: 90:27
        StorageLive(_4);                 // scope 0 at src/main.rs:90:5: 90:23
        StorageLive(_5);                 // scope 0 at src/main.rs:90:6: 90:23
        StorageLive(_6);                 // scope 0 at src/main.rs:90:6: 90:23
        StorageLive(_7);                 // scope 0 at src/main.rs:90:6: 90:14
        StorageLive(_8);                 // scope 0 at src/main.rs:90:6: 90:7
        _8 = &(*_1);                     // scope 0 at src/main.rs:90:6: 90:7
        _7 = std::sync::Mutex::<u32>::lock(move _8) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:90:6: 90:14
    }
    bb1: {
        StorageDead(_8);                 // scope 0 at src/main.rs:90:13: 90:14
        _6 = Result::<std::sync::MutexGuard<'_, u32>, PoisonError<std::sync::MutexGuard<'_, u32>>>::unwrap(move _7) -> [return: bb2, unwind: bb6]; // scope 0 at src/main.rs:90:6: 90:23
    }
    bb2: {
        _5 = &mut _6;                    // scope 0 at src/main.rs:90:6: 90:23
        StorageDead(_7);                 // scope 0 at src/main.rs:90:22: 90:23
        _4 = <std::sync::MutexGuard<'_, u32> as DerefMut>::deref_mut(move _5) -> [return: bb3, unwind: bb5]; // scope 0 at src/main.rs:90:5: 90:23
    }
    bb3: {
        StorageDead(_5);                 // scope 0 at src/main.rs:90:22: 90:23
        (*_4) = move _3;                 // scope 0 at src/main.rs:90:5: 90:27
        StorageDead(_3);                 // scope 0 at src/main.rs:90:26: 90:27
        drop(_6) -> [return: bb4, unwind: bb6]; // scope 0 at src/main.rs:90:27: 90:28
    }
    bb4: {
        StorageDead(_6);                 // scope 0 at src/main.rs:90:27: 90:28
        StorageDead(_4);                 // scope 0 at src/main.rs:90:27: 90:28
        _0 = const ();                   // scope 0 at src/main.rs:89:37: 91:2
        return;                          // scope 0 at src/main.rs:91:2: 91:2
    }
    bb5 (cleanup): {
        drop(_6) -> [return: bb6, unwind terminate(cleanup)]; // scope 0 at src/main.rs:90:27: 90:28
    }
    bb6 (cleanup): {
        resume;                          // scope 0 at src/main.rs:89:1: 91:2
    }
}
"""

RETURNED_REF = """// MIR for `returned_ref` after PreCodegen

fn returned_ref(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:93:17: 93:20
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:93:30: 93:33
    let _2: u32;                         // in scope 0 at src/main.rs:94:9: 94:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:94:36: 94:39
    let _5: ();                          // in scope 0 at src/main.rs:96:5: 96:19
    let mut _6: &std::sync::Mutex<u32>;  // in scope 0 at src/main.rs:96:14: 96:15
    let mut _7: u32;                     // in scope 0 at src/main.rs:96:17: 96:18
    let _9: &u32;                        // in scope 0 at src/main.rs:98:5: 98:29
    let mut _10: &u32;                   // in scope 0 at src/main.rs:98:26: 98:28
    let mut _11: u32;                    // in scope 0 at src/main.rs:99:5: 99:28
    let mut _12: &u32;                   // in scope 0 at src/main.rs:99:5: 99:28
    let mut _13: &std::sync::MutexGuard<'_, u32>; // in scope 0 at src/main.rs:99:6: 99:28
    let _14: std::sync::MutexGuard<'_, u32>; // in scope 0 at src/main.rs:99:6: 99:28
    let mut _15: std::result::Result<std::sync::MutexGuard<'_, u32>, std::sync::PoisonError<std::sync::MutexGuard<'_, u32>>>; // in scope 0 at src/main.rs:99:6: 99:19
    let mut _16: &std::sync::Mutex<u32>; // in scope 0 at src/main.rs:99:6: 99:12
    let _17: &std::sync::Mutex<u32>;     // in scope 0 at src/main.rs:99:6: 99:12
    let mut _18: u32;                    // in scope 0 at src/main.rs:99:31: 99:32
    let mut _19: (u32, bool);            // in scope 0 at src/main.rs:99:5: 99:32
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:94:9: 94:10
        let _4: &std::sync::Mutex<u32>;  // in scope 1 at src/main.rs:95:9: 95:10
        scope 2 {
            debug r => _4;               // in scope 2 at src/main.rs:95:9: 95:10
            let _8: u32;                 // in scope 2 at src/main.rs:97:9: 97:10
            scope 3 {
                debug k => _8;           // in scope 3 at src/main.rs:97:9: 97:10
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:94:9: 94:10
        StorageLive(_3);                 // scope 0 at src/main.rs:94:36: 94:39
        _3 = copy _1;                    // scope 0 at src/main.rs:94:36: 94:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:94:13: 94:40
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:94:39: 94:40
        StorageLive(_4);                 // scope 1 at src/main.rs:95:9: 95:10
        _4 = slot() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:95:13: 95:19
    }
    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:96:5: 96:19
        StorageLive(_6);                 // scope 2 at src/main.rs:96:14: 96:15
        _6 = &(*_4);                     // scope 2 at src/main.rs:96:14: 96:15
        StorageLive(_7);                 // scope 2 at src/main.rs:96:17: 96:18
        _7 = copy _2;                    // scope 2 at src/main.rs:96:17: 96:18
        _5 = lock_set(move _6, move _7) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:96:5: 96:19
    }
    bb3: {
        StorageDead(_7);                 // scope 2 at src/main.rs:96:18: 96:19
        StorageDead(_6);                 // scope 2 at src/main.rs:96:18: 96:19
        StorageDead(_5);                 // scope 2 at src/main.rs:96:19: 96:20
        StorageLive(_8);                 // scope 2 at src/main.rs:97:9: 97:10
        _8 = other() -> [return: bb4, unwind continue]; // scope 2 at src/main.rs:97:13: 97:20
    }
    bb4: {
        StorageLive(_9);                 // scope 3 at src/main.rs:98:5: 98:29
        StorageLive(_10);                // scope 3 at src/main.rs:98:26: 98:28
        _10 = &_2;                       // scope 3 at src/main.rs:98:26: 98:28
        _9 = std::hint::black_box::<&u32>(move _10) -> [return: bb5, unwind continue]; // scope 3 at src/main.rs:98:5: 98:29
    }
    bb5: {
        StorageDead(_10);                // scope 3 at src/main.rs:98:28: 98:29
        StorageDead(_9);                 // scope 3 at src/main.rs:98:29: 98:30
        StorageLive(_11);                // scope 3 at src/main.rs:99:5: 99:28
        StorageLive(_12);                // scope 3 at src/main.rs:99:5: 99:28
        StorageLive(_13);                // scope 3 at src/main.rs:99:6: 99:28
        StorageLive(_14);                // scope 3 at src/main.rs:99:6: 99:28
        StorageLive(_15);                // scope 3 at src/main.rs:99:6: 99:19
        StorageLive(_16);                // scope 3 at src/main.rs:99:6: 99:12
        StorageLive(_17);                // scope 3 at src/main.rs:99:6: 99:12
        _17 = slot() -> [return: bb6, unwind continue]; // scope 3 at src/main.rs:99:6: 99:12
    }
    bb6: {
        _16 = &(*_17);                   // scope 3 at src/main.rs:99:6: 99:12
        _15 = std::sync::Mutex::<u32>::lock(move _16) -> [return: bb7, unwind continue]; // scope 3 at src/main.rs:99:6: 99:19
    }
    bb7: {
        StorageDead(_16);                // scope 3 at src/main.rs:99:18: 99:19
        _14 = Result::<std::sync::MutexGuard<'_, u32>, PoisonError<std::sync::MutexGuard<'_, u32>>>::unwrap(move _15) -> [return: bb8, unwind: bb13]; // scope 3 at src/main.rs:99:6: 99:28
    }
    bb8: {
        _13 = &_14;                      // scope 3 at src/main.rs:99:6: 99:28
        StorageDead(_15);                // scope 3 at src/main.rs:99:27: 99:28
        _12 = <std::sync::MutexGuard<'_, u32> as Deref>::deref(move _13) -> [return: bb9, unwind: bb12]; // scope 3 at src/main.rs:99:5: 99:28
    }
    bb9: {
        _11 = copy (*_12);               // scope 3 at src/main.rs:99:5: 99:28
        StorageDead(_13);                // scope 3 at src/main.rs:99:27: 99:28
        StorageLive(_18);                // scope 3 at src/main.rs:99:31: 99:32
        _18 = copy _8;                   // scope 3 at src/main.rs:99:31: 99:32
        _19 = AddWithOverflow(copy _11, copy _18); // scope 3 at src/main.rs:99:5: 99:32
        assert(!move (_19.1: bool), "attempt to compute `{} + {}`, which would overflow", move _11, move _18) -> [success: bb10, unwind: bb12]; // scope 3 at src/main.rs:99:5: 99:32
    }
    bb10: {
        _0 = move (_19.0: u32);          // scope 3 at src/main.rs:99:5: 99:32
        StorageDead(_18);                // scope 3 at src/main.rs:99:31: 99:32
        drop(_14) -> [return: bb11, unwind: bb13]; // scope 3 at src/main.rs:99:31: 99:32
    }
    bb11: {
        StorageDead(_17);                // scope 3 at src/main.rs:99:31: 99:32
        StorageDead(_14);                // scope 3 at src/main.rs:99:31: 99:32
        StorageDead(_12);                // scope 3 at src/main.rs:99:31: 99:32
        StorageDead(_11);                // scope 3 at src/main.rs:99:31: 99:32
        StorageDead(_8);                 // scope 2 at src/main.rs:100:1: 100:2
        StorageDead(_4);                 // scope 1 at src/main.rs:100:1: 100:2
        StorageDead(_2);                 // scope 0 at src/main.rs:100:1: 100:2
        return;                          // scope 0 at src/main.rs:100:2: 100:2
    }
    bb12 (cleanup): {
        drop(_14) -> [return: bb13, unwind terminate(cleanup)]; // scope 3 at src/main.rs:99:31: 99:32
    }
    bb13 (cleanup): {
        resume;                          // scope 0 at src/main.rs:93:1: 100:2
    }
}
"""

THROUGH_PTR = """// MIR for `through_ptr` after PreCodegen

fn through_ptr(_1: &mut u32, _2: u32) -> () {
    debug p => _1;                       // in scope 0 at src/main.rs:102:16: 102:17
    debug arg => _2;                     // in scope 0 at src/main.rs:102:29: 102:32
    let mut _0: ();                      // return place in scope 0 at src/main.rs:102:38: 102:38
    let _3: u32;                         // in scope 0 at src/main.rs:103:9: 103:10
    let mut _4: u32;                     // in scope 0 at src/main.rs:103:36: 103:39
    let mut _5: u32;                     // in scope 0 at src/main.rs:104:10: 104:11
    let _7: &u32;                        // in scope 0 at src/main.rs:106:5: 106:29
    let mut _8: &u32;                    // in scope 0 at src/main.rs:106:26: 106:28
    let _9: u32;                         // in scope 0 at src/main.rs:107:5: 107:28
    let mut _10: u32;                    // in scope 0 at src/main.rs:107:26: 107:27
    scope 1 {
        debug x => _3;                   // in scope 1 at src/main.rs:103:9: 103:10
        let _6: u32;                     // in scope 1 at src/main.rs:105:9: 105:10
        scope 2 {
            debug k => _6;               // in scope 2 at src/main.rs:105:9: 105:10
        }
    }
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:103:9: 103:10
        StorageLive(_4);                 // scope 0 at src/main.rs:103:36: 103:39
        _4 = copy _2;                    // scope 0 at src/main.rs:103:36: 103:39
        _3 = identity::<u32>(move _4) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:103:13: 103:40
    }
    bb1: {
        StorageDead(_4);                 // scope 0 at src/main.rs:103:39: 103:40
        StorageLive(_5);                 // scope 1 at src/main.rs:104:10: 104:11
        _5 = copy _3;                    // scope 1 at src/main.rs:104:10: 104:11
        (*_1) = move _5;                 // scope 1 at src/main.rs:104:5: 104:11
        StorageDead(_5);                 // scope 1 at src/main.rs:104:10: 104:11
        StorageLive(_6);                 // scope 1 at src/main.rs:105:9: 105:10
        _6 = other() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:105:13: 105:20
    }
    bb2: {
        StorageLive(_7);                 // scope 2 at src/main.rs:106:5: 106:29
        StorageLive(_8);                 // scope 2 at src/main.rs:106:26: 106:28
        _8 = &_3;                        // scope 2 at src/main.rs:106:26: 106:28
        _7 = std::hint::black_box::<&u32>(move _8) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:106:5: 106:29
    }
    bb3: {
        StorageDead(_8);                 // scope 2 at src/main.rs:106:28: 106:29
        StorageDead(_7);                 // scope 2 at src/main.rs:106:29: 106:30
        StorageLive(_9);                 // scope 2 at src/main.rs:107:5: 107:28
        StorageLive(_10);                // scope 2 at src/main.rs:107:26: 107:27
        _10 = copy _6;                   // scope 2 at src/main.rs:107:26: 107:27
        _9 = std::hint::black_box::<u32>(move _10) -> [return: bb4, unwind continue]; // scope 2 at src/main.rs:107:5: 107:28
    }
    bb4: {
        StorageDead(_10);                // scope 2 at src/main.rs:107:27: 107:28
        StorageDead(_9);                 // scope 2 at src/main.rs:107:28: 107:29
        _0 = const ();                   // scope 0 at src/main.rs:102:39: 108:2
        StorageDead(_6);                 // scope 1 at src/main.rs:108:1: 108:2
        StorageDead(_3);                 // scope 0 at src/main.rs:108:1: 108:2
        return;                          // scope 0 at src/main.rs:108:2: 108:2
    }
}
"""

FMT = """// MIR for `<impl at src/main.rs:12:10: 12:15>::fmt` after PreCodegen

fn <impl at src/main.rs:12:10: 12:15>::fmt(_1: &Wrap, _2: &mut Formatter<'_>) -> Result<(), std::fmt::Error> {
    debug self => _1;                    // in scope 0 at src/main.rs:12:10: 12:15
    debug f => _2;                       // in scope 0 at src/main.rs:12:10: 12:15
    let mut _0: std::result::Result<(), std::fmt::Error>; // return place in scope 0 at src/main.rs:12:10: 12:15
    let mut _3: &mut std::fmt::Formatter<'_>; // in scope 0 at src/main.rs:12:10: 12:15
    let mut _4: &str;                    // in scope 0 at src/main.rs:12:10: 12:15
    let _5: &str;                        // in scope 0 at src/main.rs:12:10: 12:15
    let mut _6: &dyn std::fmt::Debug;    // in scope 0 at src/main.rs:13:13: 13:16
    let mut _7: &&u32;                   // in scope 0 at src/main.rs:13:13: 13:16
    let _8: &&u32;                       // in scope 0 at src/main.rs:13:13: 13:16
    let _9: &u32;                        // in scope 0 at src/main.rs:13:13: 13:16
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:12:10: 12:15
        _3 = &mut (*_2);                 // scope 0 at src/main.rs:12:10: 12:15
        StorageLive(_4);                 // scope 0 at src/main.rs:12:10: 12:15
        StorageLive(_5);                 // scope 0 at src/main.rs:12:10: 12:15
        _5 = const "Wrap";               // scope 0 at src/main.rs:12:10: 12:15
        _4 = &(*_5);                     // scope 0 at src/main.rs:12:10: 12:15
        StorageLive(_6);                 // scope 0 at src/main.rs:13:13: 13:16
        StorageLive(_7);                 // scope 0 at src/main.rs:13:13: 13:16
        StorageLive(_8);                 // scope 0 at src/main.rs:13:13: 13:16
        StorageLive(_9);                 // scope 0 at src/main.rs:13:13: 13:16
        _9 = &((*_1).0: u32);            // scope 0 at src/main.rs:13:13: 13:16
        _8 = &_9;                        // scope 0 at src/main.rs:13:13: 13:16
        _7 = &(*_8);                     // scope 0 at src/main.rs:13:13: 13:16
        _6 = move _7 as &dyn std::fmt::Debug (PointerCoercion(Unsize, Implicit)); // scope 0 at src/main.rs:13:13: 13:16
        StorageDead(_7);                 // scope 0 at src/main.rs:13:15: 13:16
        _0 = Formatter::<'_>::debug_tuple_field1_finish(move _3, move _4, move _6) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:12:10: 12:15
    }
    bb1: {
        StorageDead(_9);                 // scope 0 at src/main.rs:12:14: 12:15
        StorageDead(_8);                 // scope 0 at src/main.rs:12:14: 12:15
        StorageDead(_6);                 // scope 0 at src/main.rs:12:14: 12:15
        StorageDead(_5);                 // scope 0 at src/main.rs:12:14: 12:15
        StorageDead(_4);                 // scope 0 at src/main.rs:12:14: 12:15
        StorageDead(_3);                 // scope 0 at src/main.rs:12:14: 12:15
        return;                          // scope 0 at src/main.rs:12:15: 12:15
    }
}
"""


# Review: the copy reached global state another way. rustc 1.96.0's MIR of this crate, as above.
REVIEW_SOURCE = """use std::cell::Cell;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Mutex, OnceLock};

thread_local! {
    static SLOT: Cell<u32> = const { Cell::new(0) };
}
static A: AtomicU32 = AtomicU32::new(0);
static S: Mutex<u32> = Mutex::new(0);
static REG: OnceLock<Mutex<Vec<u32>>> = OnceLock::new();

struct Job {
    a: &'static AtomicU32,
    v: u32,
}

struct Wrap(u32);

impl From<u32> for Wrap {
    fn from(v: u32) -> Wrap {
        SLOT.set(v);
        Wrap(v)
    }
}

mod inner {
    pub struct Hidden(pub u32);

    impl std::fmt::Display for Hidden {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            super::SLOT.set(self.0);
            write!(f, "h")
        }
    }
}

fn other() -> u32 {
    std::hint::black_box(1)
}

fn slot() -> &'static Mutex<u32> {
    &S
}

fn registry() -> &'static Mutex<Vec<u32>> {
    REG.get_or_init(|| Mutex::new(Vec::new()))
}

fn lock_set(m: &Mutex<u32>, v: u32) {
    *m.lock().unwrap() = v;
}

fn direct_set(v: u32) {
    A.store(v, Ordering::Relaxed);
}

fn run(job: Job) {
    job.a.store(job.v, Ordering::Relaxed);
}

fn slotted(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    lock_set(slot(), x);
    let k = other();
    std::hint::black_box(&x);
    *slot().lock().unwrap() + k
}

fn registered(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    registry().lock().unwrap().push(x);
    let k = other();
    std::hint::black_box(&x);
    registry().lock().unwrap()[0] + k
}

fn jobbed(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    run(Job { a: &A, v: x });
    let k = other();
    std::hint::black_box(&x);
    A.load(Ordering::Relaxed) + k
}

fn captured(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let a = &A;
    let put = move || a.store(x, Ordering::Relaxed);
    put();
    let k = other();
    std::hint::black_box(&x);
    A.load(Ordering::Relaxed) + k
}

fn itemed(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    std::iter::once(x).for_each(direct_set);
    let k = other();
    std::hint::black_box(&x);
    A.load(Ordering::Relaxed) + k
}

fn converted(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let _w: Wrap = x.into();
    let k = other();
    std::hint::black_box(&x);
    SLOT.get() + k
}

fn hidden(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    println!("{}", inner::Hidden(x));
    let k = other();
    std::hint::black_box(&x);
    SLOT.get() + k
}

fn owned(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let m = Mutex::new(0);
    *m.lock().unwrap() = x;
    let k = other();
    std::hint::black_box(&x);
    A.load(Ordering::Relaxed) + k
}

fn main() {
    let n: u32 = std::env::args().nth(1).unwrap().parse().unwrap();
    let total = slotted(n) + registered(n) + jobbed(n) + captured(n) + itemed(n);
    println!("{}", total + converted(n) + hidden(n) + owned(n));
}
"""

R_OTHER = """// MIR for `other` after PreCodegen

fn other() -> u32 {
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:37:15: 37:18
    bb0: {
        _0 = std::hint::black_box::<u32>(const 1_u32) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:38:5: 38:28
    }
    bb1: {
        return;                          // scope 0 at src/main.rs:39:2: 39:2
    }
}
"""

R_SLOT = """// MIR for `slot` after PreCodegen

fn slot() -> &std::sync::Mutex<u32> {
    let mut _0: &std::sync::Mutex<u32>;  // return place in scope 0 at src/main.rs:41:14: 41:33
    let _1: &std::sync::Mutex<u32>;      // in scope 0 at src/main.rs:42:5: 42:7
    let _2: &std::sync::Mutex<u32>;      // in scope 0 at src/main.rs:42:6: 42:7
    bb0: {
        StorageLive(_1);                 // scope 0 at src/main.rs:42:5: 42:7
        StorageLive(_2);                 // scope 0 at src/main.rs:42:6: 42:7
        _2 = const {alloc23: &std::sync::Mutex<u32>}; // scope 0 at src/main.rs:42:6: 42:7
        _1 = &(*_2);                     // scope 0 at src/main.rs:42:5: 42:7
        _0 = &(*_1);                     // scope 0 at src/main.rs:42:5: 42:7
        StorageDead(_2);                 // scope 0 at src/main.rs:42:6: 42:7
        StorageDead(_1);                 // scope 0 at src/main.rs:42:6: 42:7
        return;                          // scope 0 at src/main.rs:43:2: 43:2
    }
}
"""

R_REGISTRY = """// MIR for `registry` after PreCodegen

fn registry() -> &std::sync::Mutex<Vec<u32>> {
    let mut _0: &std::sync::Mutex<std::vec::Vec<u32>>; // return place in scope 0 at src/main.rs:45:18: 45:42
    let _1: &std::sync::Mutex<std::vec::Vec<u32>>; // in scope 0 at src/main.rs:46:5: 46:47
    let mut _2: &std::sync::OnceLock<std::sync::Mutex<std::vec::Vec<u32>>>; // in scope 0 at src/main.rs:46:5: 46:8
    let _3: &std::sync::OnceLock<std::sync::Mutex<std::vec::Vec<u32>>>; // in scope 0 at src/main.rs:46:5: 46:8
    let mut _4: {closure@src/main.rs:46:21: 46:23}; // in scope 0 at src/main.rs:46:21: 46:46
    bb0: {
        StorageLive(_1);                 // scope 0 at src/main.rs:46:5: 46:47
        StorageLive(_2);                 // scope 0 at src/main.rs:46:5: 46:8
        StorageLive(_3);                 // scope 0 at src/main.rs:46:5: 46:8
        _3 = const {alloc39: &OnceLock<std::sync::Mutex<Vec<u32>>>}; // scope 0 at src/main.rs:46:5: 46:8
        _2 = &(*_3);                     // scope 0 at src/main.rs:46:5: 46:8
        StorageLive(_4);                 // scope 0 at src/main.rs:46:21: 46:46
        _4 = {closure@src/main.rs:46:21: 46:23}; // scope 0 at src/main.rs:46:21: 46:46
        _1 = OnceLock::<std::sync::Mutex<Vec<u32>>>::get_or_init::<{closure@src/main.rs:46:21: 46:23}>(move _2, move _4) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:46:5: 46:47
    }
    bb1: {
        _0 = &(*_1);                     // scope 0 at src/main.rs:46:5: 46:47
        StorageDead(_4);                 // scope 0 at src/main.rs:46:46: 46:47
        StorageDead(_3);                 // scope 0 at src/main.rs:46:46: 46:47
        StorageDead(_2);                 // scope 0 at src/main.rs:46:46: 46:47
        StorageDead(_1);                 // scope 0 at src/main.rs:46:46: 46:47
        return;                          // scope 0 at src/main.rs:47:2: 47:2
    }
}
"""

R_REGISTRY_CLOSURE = """// MIR for `registry::{closure#0}` after PreCodegen

fn registry::{closure#0}(_1: {closure@src/main.rs:46:21: 46:23}) -> std::sync::Mutex<Vec<u32>> {
    let mut _0: std::sync::Mutex<std::vec::Vec<u32>>; // return place in scope 0 at src/main.rs:46:23: 46:23
    let mut _2: std::vec::Vec<u32>;      // in scope 0 at src/main.rs:46:35: 46:45
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:46:35: 46:45
        _2 = Vec::<u32>::new() -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:46:35: 46:45
    }
    bb1: {
        _0 = std::sync::Mutex::<Vec<u32>>::new(move _2) -> [return: bb2, unwind: bb3]; // scope 0 at src/main.rs:46:24: 46:46
    }
    bb2: {
        StorageDead(_2);                 // scope 0 at src/main.rs:46:45: 46:46
        return;                          // scope 0 at src/main.rs:46:46: 46:46
    }
    bb3 (cleanup): {
        resume;                          // scope 0 at src/main.rs:46:21: 46:46
    }
}
"""

R_LOCK_SET = """// MIR for `lock_set` after PreCodegen

fn lock_set(_1: &std::sync::Mutex<u32>, _2: u32) -> () {
    debug m => _1;                       // in scope 0 at src/main.rs:49:13: 49:14
    debug v => _2;                       // in scope 0 at src/main.rs:49:29: 49:30
    let mut _0: ();                      // return place in scope 0 at src/main.rs:49:36: 49:36
    let mut _3: u32;                     // in scope 0 at src/main.rs:50:26: 50:27
    let mut _4: &mut u32;                // in scope 0 at src/main.rs:50:5: 50:23
    let mut _5: &mut std::sync::MutexGuard<'_, u32>; // in scope 0 at src/main.rs:50:6: 50:23
    let mut _6: std::sync::MutexGuard<'_, u32>; // in scope 0 at src/main.rs:50:6: 50:23
    let mut _7: std::result::Result<std::sync::MutexGuard<'_, u32>, std::sync::PoisonError<std::sync::MutexGuard<'_, u32>>>; // in scope 0 at src/main.rs:50:6: 50:14
    let mut _8: &std::sync::Mutex<u32>;  // in scope 0 at src/main.rs:50:6: 50:7
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:50:26: 50:27
        _3 = copy _2;                    // scope 0 at src/main.rs:50:26: 50:27
        StorageLive(_4);                 // scope 0 at src/main.rs:50:5: 50:23
        StorageLive(_5);                 // scope 0 at src/main.rs:50:6: 50:23
        StorageLive(_6);                 // scope 0 at src/main.rs:50:6: 50:23
        StorageLive(_7);                 // scope 0 at src/main.rs:50:6: 50:14
        StorageLive(_8);                 // scope 0 at src/main.rs:50:6: 50:7
        _8 = &(*_1);                     // scope 0 at src/main.rs:50:6: 50:7
        _7 = std::sync::Mutex::<u32>::lock(move _8) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:50:6: 50:14
    }
    bb1: {
        StorageDead(_8);                 // scope 0 at src/main.rs:50:13: 50:14
        _6 = Result::<std::sync::MutexGuard<'_, u32>, PoisonError<std::sync::MutexGuard<'_, u32>>>::unwrap(move _7) -> [return: bb2, unwind: bb6]; // scope 0 at src/main.rs:50:6: 50:23
    }
    bb2: {
        _5 = &mut _6;                    // scope 0 at src/main.rs:50:6: 50:23
        StorageDead(_7);                 // scope 0 at src/main.rs:50:22: 50:23
        _4 = <std::sync::MutexGuard<'_, u32> as DerefMut>::deref_mut(move _5) -> [return: bb3, unwind: bb5]; // scope 0 at src/main.rs:50:5: 50:23
    }
    bb3: {
        StorageDead(_5);                 // scope 0 at src/main.rs:50:22: 50:23
        (*_4) = move _3;                 // scope 0 at src/main.rs:50:5: 50:27
        StorageDead(_3);                 // scope 0 at src/main.rs:50:26: 50:27
        drop(_6) -> [return: bb4, unwind: bb6]; // scope 0 at src/main.rs:50:27: 50:28
    }
    bb4: {
        StorageDead(_6);                 // scope 0 at src/main.rs:50:27: 50:28
        StorageDead(_4);                 // scope 0 at src/main.rs:50:27: 50:28
        _0 = const ();                   // scope 0 at src/main.rs:49:37: 51:2
        return;                          // scope 0 at src/main.rs:51:2: 51:2
    }
    bb5 (cleanup): {
        drop(_6) -> [return: bb6, unwind terminate(cleanup)]; // scope 0 at src/main.rs:50:27: 50:28
    }
    bb6 (cleanup): {
        resume;                          // scope 0 at src/main.rs:49:1: 51:2
    }
}
"""

R_DIRECT_SET = """// MIR for `direct_set` after PreCodegen

fn direct_set(_1: u32) -> () {
    debug v => _1;                       // in scope 0 at src/main.rs:53:15: 53:16
    let mut _0: ();                      // return place in scope 0 at src/main.rs:53:22: 53:22
    let _2: ();                          // in scope 0 at src/main.rs:54:5: 54:34
    let mut _3: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:54:5: 54:6
    let _4: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:54:5: 54:6
    let mut _5: u32;                     // in scope 0 at src/main.rs:54:13: 54:14
    let mut _6: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:54:16: 54:33
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:54:5: 54:34
        StorageLive(_3);                 // scope 0 at src/main.rs:54:5: 54:6
        StorageLive(_4);                 // scope 0 at src/main.rs:54:5: 54:6
        _4 = const {alloc20: &Atomic<u32>}; // scope 0 at src/main.rs:54:5: 54:6
        _3 = &(*_4);                     // scope 0 at src/main.rs:54:5: 54:6
        StorageLive(_5);                 // scope 0 at src/main.rs:54:13: 54:14
        _5 = copy _1;                    // scope 0 at src/main.rs:54:13: 54:14
        StorageLive(_6);                 // scope 0 at src/main.rs:54:16: 54:33
        _6 = std::sync::atomic::Ordering::Relaxed; // scope 0 at src/main.rs:54:16: 54:33
        _2 = Atomic::<u32>::store(move _3, move _5, move _6) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:54:5: 54:34
    }
    bb1: {
        StorageDead(_6);                 // scope 0 at src/main.rs:54:33: 54:34
        StorageDead(_5);                 // scope 0 at src/main.rs:54:33: 54:34
        StorageDead(_3);                 // scope 0 at src/main.rs:54:33: 54:34
        StorageDead(_4);                 // scope 0 at src/main.rs:54:34: 54:35
        StorageDead(_2);                 // scope 0 at src/main.rs:54:34: 54:35
        _0 = const ();                   // scope 0 at src/main.rs:53:23: 55:2
        return;                          // scope 0 at src/main.rs:55:2: 55:2
    }
}
"""

R_RUN = """// MIR for `run` after PreCodegen

fn run(_1: Job) -> () {
    debug job => _1;                     // in scope 0 at src/main.rs:57:8: 57:11
    let mut _0: ();                      // return place in scope 0 at src/main.rs:57:17: 57:17
    let _2: ();                          // in scope 0 at src/main.rs:58:5: 58:42
    let mut _3: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:58:5: 58:10
    let mut _4: u32;                     // in scope 0 at src/main.rs:58:17: 58:22
    let mut _5: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:58:24: 58:41
    let mut _6: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:57:8: 57:11
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:58:5: 58:42
        StorageLive(_3);                 // scope 0 at src/main.rs:58:5: 58:10
        _6 = copy (_1.0: &std::sync::atomic::Atomic<u32>); // scope 0 at src/main.rs:58:5: 58:10
        _3 = &(*_6);                     // scope 0 at src/main.rs:58:5: 58:10
        StorageLive(_4);                 // scope 0 at src/main.rs:58:17: 58:22
        _4 = copy (_1.1: u32);           // scope 0 at src/main.rs:58:17: 58:22
        StorageLive(_5);                 // scope 0 at src/main.rs:58:24: 58:41
        _5 = std::sync::atomic::Ordering::Relaxed; // scope 0 at src/main.rs:58:24: 58:41
        _2 = Atomic::<u32>::store(move _3, move _4, move _5) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:58:5: 58:42
    }
    bb1: {
        StorageDead(_5);                 // scope 0 at src/main.rs:58:41: 58:42
        StorageDead(_4);                 // scope 0 at src/main.rs:58:41: 58:42
        StorageDead(_3);                 // scope 0 at src/main.rs:58:41: 58:42
        StorageDead(_2);                 // scope 0 at src/main.rs:58:42: 58:43
        _0 = const ();                   // scope 0 at src/main.rs:57:18: 59:2
        return;                          // scope 0 at src/main.rs:59:2: 59:2
    }
}
"""

R_SLOTTED = """// MIR for `slotted` after PreCodegen

fn slotted(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:61:12: 61:15
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:61:25: 61:28
    let _2: u32;                         // in scope 0 at src/main.rs:62:9: 62:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:62:36: 62:39
    let _4: ();                          // in scope 0 at src/main.rs:63:5: 63:24
    let mut _5: &std::sync::Mutex<u32>;  // in scope 0 at src/main.rs:63:14: 63:20
    let _6: &std::sync::Mutex<u32>;      // in scope 0 at src/main.rs:63:14: 63:20
    let mut _7: u32;                     // in scope 0 at src/main.rs:63:22: 63:23
    let _9: &u32;                        // in scope 0 at src/main.rs:65:5: 65:29
    let mut _10: &u32;                   // in scope 0 at src/main.rs:65:26: 65:28
    let mut _11: u32;                    // in scope 0 at src/main.rs:66:5: 66:28
    let mut _12: &u32;                   // in scope 0 at src/main.rs:66:5: 66:28
    let mut _13: &std::sync::MutexGuard<'_, u32>; // in scope 0 at src/main.rs:66:6: 66:28
    let _14: std::sync::MutexGuard<'_, u32>; // in scope 0 at src/main.rs:66:6: 66:28
    let mut _15: std::result::Result<std::sync::MutexGuard<'_, u32>, std::sync::PoisonError<std::sync::MutexGuard<'_, u32>>>; // in scope 0 at src/main.rs:66:6: 66:19
    let mut _16: &std::sync::Mutex<u32>; // in scope 0 at src/main.rs:66:6: 66:12
    let _17: &std::sync::Mutex<u32>;     // in scope 0 at src/main.rs:66:6: 66:12
    let mut _18: u32;                    // in scope 0 at src/main.rs:66:31: 66:32
    let mut _19: (u32, bool);            // in scope 0 at src/main.rs:66:5: 66:32
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:62:9: 62:10
        let _8: u32;                     // in scope 1 at src/main.rs:64:9: 64:10
        scope 2 {
            debug k => _8;               // in scope 2 at src/main.rs:64:9: 64:10
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
        StorageLive(_4);                 // scope 1 at src/main.rs:63:5: 63:24
        StorageLive(_5);                 // scope 1 at src/main.rs:63:14: 63:20
        StorageLive(_6);                 // scope 1 at src/main.rs:63:14: 63:20
        _6 = slot() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:63:14: 63:20
    }
    bb2: {
        _5 = &(*_6);                     // scope 1 at src/main.rs:63:14: 63:20
        StorageLive(_7);                 // scope 1 at src/main.rs:63:22: 63:23
        _7 = copy _2;                    // scope 1 at src/main.rs:63:22: 63:23
        _4 = lock_set(move _5, move _7) -> [return: bb3, unwind continue]; // scope 1 at src/main.rs:63:5: 63:24
    }
    bb3: {
        StorageDead(_7);                 // scope 1 at src/main.rs:63:23: 63:24
        StorageDead(_5);                 // scope 1 at src/main.rs:63:23: 63:24
        StorageDead(_6);                 // scope 1 at src/main.rs:63:24: 63:25
        StorageDead(_4);                 // scope 1 at src/main.rs:63:24: 63:25
        StorageLive(_8);                 // scope 1 at src/main.rs:64:9: 64:10
        _8 = other() -> [return: bb4, unwind continue]; // scope 1 at src/main.rs:64:13: 64:20
    }
    bb4: {
        StorageLive(_9);                 // scope 2 at src/main.rs:65:5: 65:29
        StorageLive(_10);                // scope 2 at src/main.rs:65:26: 65:28
        _10 = &_2;                       // scope 2 at src/main.rs:65:26: 65:28
        _9 = std::hint::black_box::<&u32>(move _10) -> [return: bb5, unwind continue]; // scope 2 at src/main.rs:65:5: 65:29
    }
    bb5: {
        StorageDead(_10);                // scope 2 at src/main.rs:65:28: 65:29
        StorageDead(_9);                 // scope 2 at src/main.rs:65:29: 65:30
        StorageLive(_11);                // scope 2 at src/main.rs:66:5: 66:28
        StorageLive(_12);                // scope 2 at src/main.rs:66:5: 66:28
        StorageLive(_13);                // scope 2 at src/main.rs:66:6: 66:28
        StorageLive(_14);                // scope 2 at src/main.rs:66:6: 66:28
        StorageLive(_15);                // scope 2 at src/main.rs:66:6: 66:19
        StorageLive(_16);                // scope 2 at src/main.rs:66:6: 66:12
        StorageLive(_17);                // scope 2 at src/main.rs:66:6: 66:12
        _17 = slot() -> [return: bb6, unwind continue]; // scope 2 at src/main.rs:66:6: 66:12
    }
    bb6: {
        _16 = &(*_17);                   // scope 2 at src/main.rs:66:6: 66:12
        _15 = std::sync::Mutex::<u32>::lock(move _16) -> [return: bb7, unwind continue]; // scope 2 at src/main.rs:66:6: 66:19
    }
    bb7: {
        StorageDead(_16);                // scope 2 at src/main.rs:66:18: 66:19
        _14 = Result::<std::sync::MutexGuard<'_, u32>, PoisonError<std::sync::MutexGuard<'_, u32>>>::unwrap(move _15) -> [return: bb8, unwind: bb13]; // scope 2 at src/main.rs:66:6: 66:28
    }
    bb8: {
        _13 = &_14;                      // scope 2 at src/main.rs:66:6: 66:28
        StorageDead(_15);                // scope 2 at src/main.rs:66:27: 66:28
        _12 = <std::sync::MutexGuard<'_, u32> as Deref>::deref(move _13) -> [return: bb9, unwind: bb12]; // scope 2 at src/main.rs:66:5: 66:28
    }
    bb9: {
        _11 = copy (*_12);               // scope 2 at src/main.rs:66:5: 66:28
        StorageDead(_13);                // scope 2 at src/main.rs:66:27: 66:28
        StorageLive(_18);                // scope 2 at src/main.rs:66:31: 66:32
        _18 = copy _8;                   // scope 2 at src/main.rs:66:31: 66:32
        _19 = AddWithOverflow(copy _11, copy _18); // scope 2 at src/main.rs:66:5: 66:32
        assert(!move (_19.1: bool), "attempt to compute `{} + {}`, which would overflow", move _11, move _18) -> [success: bb10, unwind: bb12]; // scope 2 at src/main.rs:66:5: 66:32
    }
    bb10: {
        _0 = move (_19.0: u32);          // scope 2 at src/main.rs:66:5: 66:32
        StorageDead(_18);                // scope 2 at src/main.rs:66:31: 66:32
        drop(_14) -> [return: bb11, unwind: bb13]; // scope 2 at src/main.rs:66:31: 66:32
    }
    bb11: {
        StorageDead(_17);                // scope 2 at src/main.rs:66:31: 66:32
        StorageDead(_14);                // scope 2 at src/main.rs:66:31: 66:32
        StorageDead(_12);                // scope 2 at src/main.rs:66:31: 66:32
        StorageDead(_11);                // scope 2 at src/main.rs:66:31: 66:32
        StorageDead(_8);                 // scope 1 at src/main.rs:67:1: 67:2
        StorageDead(_2);                 // scope 0 at src/main.rs:67:1: 67:2
        return;                          // scope 0 at src/main.rs:67:2: 67:2
    }
    bb12 (cleanup): {
        drop(_14) -> [return: bb13, unwind terminate(cleanup)]; // scope 2 at src/main.rs:66:31: 66:32
    }
    bb13 (cleanup): {
        resume;                          // scope 0 at src/main.rs:61:1: 67:2
    }
}
"""

R_REGISTERED = """// MIR for `registered` after PreCodegen

fn registered(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:69:15: 69:18
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:69:28: 69:31
    let _2: u32;                         // in scope 0 at src/main.rs:70:9: 70:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:70:36: 70:39
    let _4: ();                          // in scope 0 at src/main.rs:71:5: 71:39
    let mut _5: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:71:5: 71:31
    let mut _6: &mut std::vec::Vec<u32>; // in scope 0 at src/main.rs:71:5: 71:39
    let mut _7: &mut std::sync::MutexGuard<'_, std::vec::Vec<u32>>; // in scope 0 at src/main.rs:71:5: 71:31
    let mut _8: std::sync::MutexGuard<'_, std::vec::Vec<u32>>; // in scope 0 at src/main.rs:71:5: 71:31
    let mut _9: std::result::Result<std::sync::MutexGuard<'_, std::vec::Vec<u32>>, std::sync::PoisonError<std::sync::MutexGuard<'_, std::vec::Vec<u32>>>>; // in scope 0 at src/main.rs:71:5: 71:22
    let mut _10: &std::sync::Mutex<std::vec::Vec<u32>>; // in scope 0 at src/main.rs:71:5: 71:15
    let _11: &std::sync::Mutex<std::vec::Vec<u32>>; // in scope 0 at src/main.rs:71:5: 71:15
    let mut _12: u32;                    // in scope 0 at src/main.rs:71:37: 71:38
    let _14: &u32;                       // in scope 0 at src/main.rs:73:5: 73:29
    let mut _15: &u32;                   // in scope 0 at src/main.rs:73:26: 73:28
    let mut _16: u32;                    // in scope 0 at src/main.rs:74:5: 74:34
    let mut _17: &u32;                   // in scope 0 at src/main.rs:74:31: 74:34
    let mut _18: &std::vec::Vec<u32>;    // in scope 0 at src/main.rs:74:5: 74:31
    let _19: &std::vec::Vec<u32>;        // in scope 0 at src/main.rs:74:5: 74:31
    let mut _20: &std::sync::MutexGuard<'_, std::vec::Vec<u32>>; // in scope 0 at src/main.rs:74:5: 74:31
    let _21: std::sync::MutexGuard<'_, std::vec::Vec<u32>>; // in scope 0 at src/main.rs:74:5: 74:31
    let mut _22: std::result::Result<std::sync::MutexGuard<'_, std::vec::Vec<u32>>, std::sync::PoisonError<std::sync::MutexGuard<'_, std::vec::Vec<u32>>>>; // in scope 0 at src/main.rs:74:5: 74:22
    let mut _23: &std::sync::Mutex<std::vec::Vec<u32>>; // in scope 0 at src/main.rs:74:5: 74:15
    let _24: &std::sync::Mutex<std::vec::Vec<u32>>; // in scope 0 at src/main.rs:74:5: 74:15
    let mut _25: u32;                    // in scope 0 at src/main.rs:74:37: 74:38
    let mut _26: (u32, bool);            // in scope 0 at src/main.rs:74:5: 74:38
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:70:9: 70:10
        let _13: u32;                    // in scope 1 at src/main.rs:72:9: 72:10
        scope 2 {
            debug k => _13;              // in scope 2 at src/main.rs:72:9: 72:10
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:70:9: 70:10
        StorageLive(_3);                 // scope 0 at src/main.rs:70:36: 70:39
        _3 = copy _1;                    // scope 0 at src/main.rs:70:36: 70:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:70:13: 70:40
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:70:39: 70:40
        StorageLive(_4);                 // scope 1 at src/main.rs:71:5: 71:39
        StorageLive(_5);                 // scope 1 at src/main.rs:71:5: 71:31
        StorageLive(_6);                 // scope 1 at src/main.rs:71:5: 71:39
        StorageLive(_7);                 // scope 1 at src/main.rs:71:5: 71:31
        StorageLive(_8);                 // scope 1 at src/main.rs:71:5: 71:31
        StorageLive(_9);                 // scope 1 at src/main.rs:71:5: 71:22
        StorageLive(_10);                // scope 1 at src/main.rs:71:5: 71:15
        StorageLive(_11);                // scope 1 at src/main.rs:71:5: 71:15
        _11 = registry() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:71:5: 71:15
    }
    bb2: {
        _10 = &(*_11);                   // scope 1 at src/main.rs:71:5: 71:15
        _9 = std::sync::Mutex::<Vec<u32>>::lock(move _10) -> [return: bb3, unwind continue]; // scope 1 at src/main.rs:71:5: 71:22
    }
    bb3: {
        StorageDead(_10);                // scope 1 at src/main.rs:71:21: 71:22
        _8 = Result::<std::sync::MutexGuard<'_, Vec<u32>>, PoisonError<std::sync::MutexGuard<'_, Vec<u32>>>>::unwrap(move _9) -> [return: bb4, unwind: bb19]; // scope 1 at src/main.rs:71:5: 71:31
    }
    bb4: {
        _7 = &mut _8;                    // scope 1 at src/main.rs:71:5: 71:31
        _6 = <std::sync::MutexGuard<'_, Vec<u32>> as DerefMut>::deref_mut(move _7) -> [return: bb5, unwind: bb18]; // scope 1 at src/main.rs:71:5: 71:39
    }
    bb5: {
        _5 = &mut (*_6);                 // scope 1 at src/main.rs:71:5: 71:31
        StorageDead(_9);                 // scope 1 at src/main.rs:71:30: 71:31
        StorageDead(_7);                 // scope 1 at src/main.rs:71:30: 71:31
        StorageLive(_12);                // scope 1 at src/main.rs:71:37: 71:38
        _12 = copy _2;                   // scope 1 at src/main.rs:71:37: 71:38
        _4 = Vec::<u32>::push(move _5, move _12) -> [return: bb6, unwind: bb18]; // scope 1 at src/main.rs:71:5: 71:39
    }
    bb6: {
        StorageDead(_12);                // scope 1 at src/main.rs:71:38: 71:39
        StorageDead(_5);                 // scope 1 at src/main.rs:71:38: 71:39
        drop(_8) -> [return: bb7, unwind: bb19]; // scope 1 at src/main.rs:71:39: 71:40
    }
    bb7: {
        StorageDead(_11);                // scope 1 at src/main.rs:71:39: 71:40
        StorageDead(_8);                 // scope 1 at src/main.rs:71:39: 71:40
        StorageDead(_6);                 // scope 1 at src/main.rs:71:39: 71:40
        StorageDead(_4);                 // scope 1 at src/main.rs:71:39: 71:40
        StorageLive(_13);                // scope 1 at src/main.rs:72:9: 72:10
        _13 = other() -> [return: bb8, unwind continue]; // scope 1 at src/main.rs:72:13: 72:20
    }
    bb8: {
        StorageLive(_14);                // scope 2 at src/main.rs:73:5: 73:29
        StorageLive(_15);                // scope 2 at src/main.rs:73:26: 73:28
        _15 = &_2;                       // scope 2 at src/main.rs:73:26: 73:28
        _14 = std::hint::black_box::<&u32>(move _15) -> [return: bb9, unwind continue]; // scope 2 at src/main.rs:73:5: 73:29
    }
    bb9: {
        StorageDead(_15);                // scope 2 at src/main.rs:73:28: 73:29
        StorageDead(_14);                // scope 2 at src/main.rs:73:29: 73:30
        StorageLive(_16);                // scope 2 at src/main.rs:74:5: 74:34
        StorageLive(_17);                // scope 2 at src/main.rs:74:31: 74:34
        StorageLive(_18);                // scope 2 at src/main.rs:74:5: 74:31
        StorageLive(_19);                // scope 2 at src/main.rs:74:5: 74:31
        StorageLive(_20);                // scope 2 at src/main.rs:74:5: 74:31
        StorageLive(_21);                // scope 2 at src/main.rs:74:5: 74:31
        StorageLive(_22);                // scope 2 at src/main.rs:74:5: 74:22
        StorageLive(_23);                // scope 2 at src/main.rs:74:5: 74:15
        StorageLive(_24);                // scope 2 at src/main.rs:74:5: 74:15
        _24 = registry() -> [return: bb10, unwind continue]; // scope 2 at src/main.rs:74:5: 74:15
    }
    bb10: {
        _23 = &(*_24);                   // scope 2 at src/main.rs:74:5: 74:15
        _22 = std::sync::Mutex::<Vec<u32>>::lock(move _23) -> [return: bb11, unwind continue]; // scope 2 at src/main.rs:74:5: 74:22
    }
    bb11: {
        StorageDead(_23);                // scope 2 at src/main.rs:74:21: 74:22
        _21 = Result::<std::sync::MutexGuard<'_, Vec<u32>>, PoisonError<std::sync::MutexGuard<'_, Vec<u32>>>>::unwrap(move _22) -> [return: bb12, unwind: bb19]; // scope 2 at src/main.rs:74:5: 74:31
    }
    bb12: {
        _20 = &_21;                      // scope 2 at src/main.rs:74:5: 74:31
        _19 = <std::sync::MutexGuard<'_, Vec<u32>> as Deref>::deref(move _20) -> [return: bb13, unwind: bb17]; // scope 2 at src/main.rs:74:5: 74:31
    }
    bb13: {
        _18 = &(*_19);                   // scope 2 at src/main.rs:74:5: 74:31
        StorageDead(_22);                // scope 2 at src/main.rs:74:30: 74:31
        StorageDead(_20);                // scope 2 at src/main.rs:74:30: 74:31
        _17 = <Vec<u32> as Index<usize>>::index(move _18, const 0_usize) -> [return: bb14, unwind: bb17]; // scope 2 at src/main.rs:74:31: 74:34
    }
    bb14: {
        _16 = copy (*_17);               // scope 2 at src/main.rs:74:5: 74:34
        StorageDead(_18);                // scope 2 at src/main.rs:74:33: 74:34
        StorageLive(_25);                // scope 2 at src/main.rs:74:37: 74:38
        _25 = copy _13;                  // scope 2 at src/main.rs:74:37: 74:38
        _26 = AddWithOverflow(copy _16, copy _25); // scope 2 at src/main.rs:74:5: 74:38
        assert(!move (_26.1: bool), "attempt to compute `{} + {}`, which would overflow", move _16, move _25) -> [success: bb15, unwind: bb17]; // scope 2 at src/main.rs:74:5: 74:38
    }
    bb15: {
        _0 = move (_26.0: u32);          // scope 2 at src/main.rs:74:5: 74:38
        StorageDead(_25);                // scope 2 at src/main.rs:74:37: 74:38
        drop(_21) -> [return: bb16, unwind: bb19]; // scope 2 at src/main.rs:74:37: 74:38
    }
    bb16: {
        StorageDead(_24);                // scope 2 at src/main.rs:74:37: 74:38
        StorageDead(_21);                // scope 2 at src/main.rs:74:37: 74:38
        StorageDead(_19);                // scope 2 at src/main.rs:74:37: 74:38
        StorageDead(_17);                // scope 2 at src/main.rs:74:37: 74:38
        StorageDead(_16);                // scope 2 at src/main.rs:74:37: 74:38
        StorageDead(_13);                // scope 1 at src/main.rs:75:1: 75:2
        StorageDead(_2);                 // scope 0 at src/main.rs:75:1: 75:2
        return;                          // scope 0 at src/main.rs:75:2: 75:2
    }
    bb17 (cleanup): {
        drop(_21) -> [return: bb19, unwind terminate(cleanup)]; // scope 2 at src/main.rs:74:37: 74:38
    }
    bb18 (cleanup): {
        drop(_8) -> [return: bb19, unwind terminate(cleanup)]; // scope 1 at src/main.rs:71:39: 71:40
    }
    bb19 (cleanup): {
        resume;                          // scope 0 at src/main.rs:69:1: 75:2
    }
}
"""

R_JOBBED = """// MIR for `jobbed` after PreCodegen

fn jobbed(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:77:11: 77:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:77:24: 77:27
    let _2: u32;                         // in scope 0 at src/main.rs:78:9: 78:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:78:36: 78:39
    let _4: ();                          // in scope 0 at src/main.rs:79:5: 79:29
    let mut _5: Job;                     // in scope 0 at src/main.rs:79:9: 79:28
    let mut _6: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:79:18: 79:20
    let _7: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:79:18: 79:20
    let _8: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:79:19: 79:20
    let mut _9: u32;                     // in scope 0 at src/main.rs:79:25: 79:26
    let _11: &u32;                       // in scope 0 at src/main.rs:81:5: 81:29
    let mut _12: &u32;                   // in scope 0 at src/main.rs:81:26: 81:28
    let mut _13: u32;                    // in scope 0 at src/main.rs:82:5: 82:30
    let mut _14: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:82:5: 82:6
    let _15: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:82:5: 82:6
    let mut _16: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:82:12: 82:29
    let mut _17: u32;                    // in scope 0 at src/main.rs:82:33: 82:34
    let mut _18: (u32, bool);            // in scope 0 at src/main.rs:82:5: 82:34
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:78:9: 78:10
        let _10: u32;                    // in scope 1 at src/main.rs:80:9: 80:10
        scope 2 {
            debug k => _10;              // in scope 2 at src/main.rs:80:9: 80:10
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:78:9: 78:10
        StorageLive(_3);                 // scope 0 at src/main.rs:78:36: 78:39
        _3 = copy _1;                    // scope 0 at src/main.rs:78:36: 78:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:78:13: 78:40
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:78:39: 78:40
        StorageLive(_4);                 // scope 1 at src/main.rs:79:5: 79:29
        StorageLive(_5);                 // scope 1 at src/main.rs:79:9: 79:28
        StorageLive(_6);                 // scope 1 at src/main.rs:79:18: 79:20
        StorageLive(_7);                 // scope 1 at src/main.rs:79:18: 79:20
        StorageLive(_8);                 // scope 1 at src/main.rs:79:19: 79:20
        _8 = const {alloc20: &Atomic<u32>}; // scope 1 at src/main.rs:79:19: 79:20
        _7 = &(*_8);                     // scope 1 at src/main.rs:79:18: 79:20
        _6 = &(*_7);                     // scope 1 at src/main.rs:79:18: 79:20
        StorageLive(_9);                 // scope 1 at src/main.rs:79:25: 79:26
        _9 = copy _2;                    // scope 1 at src/main.rs:79:25: 79:26
        _5 = Job { a: move _6, v: move _9 }; // scope 1 at src/main.rs:79:9: 79:28
        StorageDead(_9);                 // scope 1 at src/main.rs:79:27: 79:28
        StorageDead(_6);                 // scope 1 at src/main.rs:79:27: 79:28
        _4 = run(move _5) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:79:5: 79:29
    }
    bb2: {
        StorageDead(_5);                 // scope 1 at src/main.rs:79:28: 79:29
        StorageDead(_8);                 // scope 1 at src/main.rs:79:29: 79:30
        StorageDead(_7);                 // scope 1 at src/main.rs:79:29: 79:30
        StorageDead(_4);                 // scope 1 at src/main.rs:79:29: 79:30
        StorageLive(_10);                // scope 1 at src/main.rs:80:9: 80:10
        _10 = other() -> [return: bb3, unwind continue]; // scope 1 at src/main.rs:80:13: 80:20
    }
    bb3: {
        StorageLive(_11);                // scope 2 at src/main.rs:81:5: 81:29
        StorageLive(_12);                // scope 2 at src/main.rs:81:26: 81:28
        _12 = &_2;                       // scope 2 at src/main.rs:81:26: 81:28
        _11 = std::hint::black_box::<&u32>(move _12) -> [return: bb4, unwind continue]; // scope 2 at src/main.rs:81:5: 81:29
    }
    bb4: {
        StorageDead(_12);                // scope 2 at src/main.rs:81:28: 81:29
        StorageDead(_11);                // scope 2 at src/main.rs:81:29: 81:30
        StorageLive(_13);                // scope 2 at src/main.rs:82:5: 82:30
        StorageLive(_14);                // scope 2 at src/main.rs:82:5: 82:6
        StorageLive(_15);                // scope 2 at src/main.rs:82:5: 82:6
        _15 = const {alloc20: &Atomic<u32>}; // scope 2 at src/main.rs:82:5: 82:6
        _14 = &(*_15);                   // scope 2 at src/main.rs:82:5: 82:6
        StorageLive(_16);                // scope 2 at src/main.rs:82:12: 82:29
        _16 = std::sync::atomic::Ordering::Relaxed; // scope 2 at src/main.rs:82:12: 82:29
        _13 = Atomic::<u32>::load(move _14, move _16) -> [return: bb5, unwind continue]; // scope 2 at src/main.rs:82:5: 82:30
    }
    bb5: {
        StorageDead(_16);                // scope 2 at src/main.rs:82:29: 82:30
        StorageDead(_14);                // scope 2 at src/main.rs:82:29: 82:30
        StorageLive(_17);                // scope 2 at src/main.rs:82:33: 82:34
        _17 = copy _10;                  // scope 2 at src/main.rs:82:33: 82:34
        _18 = AddWithOverflow(copy _13, copy _17); // scope 2 at src/main.rs:82:5: 82:34
        assert(!move (_18.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _17) -> [success: bb6, unwind continue]; // scope 2 at src/main.rs:82:5: 82:34
    }
    bb6: {
        _0 = move (_18.0: u32);          // scope 2 at src/main.rs:82:5: 82:34
        StorageDead(_17);                // scope 2 at src/main.rs:82:33: 82:34
        StorageDead(_15);                // scope 2 at src/main.rs:82:33: 82:34
        StorageDead(_13);                // scope 2 at src/main.rs:82:33: 82:34
        StorageDead(_10);                // scope 1 at src/main.rs:83:1: 83:2
        StorageDead(_2);                 // scope 0 at src/main.rs:83:1: 83:2
        return;                          // scope 0 at src/main.rs:83:2: 83:2
    }
}
"""

R_CAPTURED = """// MIR for `captured` after PreCodegen

fn captured(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:85:13: 85:16
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:85:26: 85:29
    let _2: u32;                         // in scope 0 at src/main.rs:86:9: 86:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:86:36: 86:39
    let _5: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:87:14: 87:15
    let _7: ();                          // in scope 0 at src/main.rs:89:5: 89:10
    let mut _8: &{closure@src/main.rs:88:15: 88:22}; // in scope 0 at src/main.rs:89:5: 89:8
    let mut _9: ();                      // in scope 0 at src/main.rs:89:5: 89:10
    let _11: &u32;                       // in scope 0 at src/main.rs:91:5: 91:29
    let mut _12: &u32;                   // in scope 0 at src/main.rs:91:26: 91:28
    let mut _13: u32;                    // in scope 0 at src/main.rs:92:5: 92:30
    let mut _14: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:92:5: 92:6
    let _15: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:92:5: 92:6
    let mut _16: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:92:12: 92:29
    let mut _17: u32;                    // in scope 0 at src/main.rs:92:33: 92:34
    let mut _18: (u32, bool);            // in scope 0 at src/main.rs:92:5: 92:34
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:86:9: 86:10
        let _4: &std::sync::atomic::Atomic<u32>; // in scope 1 at src/main.rs:87:9: 87:10
        scope 2 {
            debug a => _4;               // in scope 2 at src/main.rs:87:9: 87:10
            let _6: {closure@src/main.rs:88:15: 88:22}; // in scope 2 at src/main.rs:88:9: 88:12
            scope 3 {
                debug put => _6;         // in scope 3 at src/main.rs:88:9: 88:12
                let _10: u32;            // in scope 3 at src/main.rs:90:9: 90:10
                scope 4 {
                    debug k => _10;      // in scope 4 at src/main.rs:90:9: 90:10
                }
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:86:9: 86:10
        StorageLive(_3);                 // scope 0 at src/main.rs:86:36: 86:39
        _3 = copy _1;                    // scope 0 at src/main.rs:86:36: 86:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:86:13: 86:40
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:86:39: 86:40
        StorageLive(_4);                 // scope 1 at src/main.rs:87:9: 87:10
        StorageLive(_5);                 // scope 1 at src/main.rs:87:14: 87:15
        _5 = const {alloc20: &Atomic<u32>}; // scope 1 at src/main.rs:87:14: 87:15
        _4 = &(*_5);                     // scope 1 at src/main.rs:87:13: 87:15
        StorageLive(_6);                 // scope 2 at src/main.rs:88:9: 88:12
        _6 = {closure@src/main.rs:88:15: 88:22} { a: copy _4, x: copy _2 }; // scope 2 at src/main.rs:88:15: 88:52
        StorageLive(_7);                 // scope 3 at src/main.rs:89:5: 89:10
        StorageLive(_8);                 // scope 3 at src/main.rs:89:5: 89:8
        _8 = &_6;                        // scope 3 at src/main.rs:89:5: 89:8
        StorageLive(_9);                 // scope 3 at src/main.rs:89:5: 89:10
        _9 = ();                         // scope 3 at src/main.rs:89:5: 89:10
        _7 = <{closure@src/main.rs:88:15: 88:22} as Fn<()>>::call(move _8, move _9) -> [return: bb2, unwind continue]; // scope 3 at src/main.rs:89:5: 89:10
    }
    bb2: {
        StorageDead(_9);                 // scope 3 at src/main.rs:89:9: 89:10
        StorageDead(_8);                 // scope 3 at src/main.rs:89:9: 89:10
        StorageDead(_7);                 // scope 3 at src/main.rs:89:10: 89:11
        StorageLive(_10);                // scope 3 at src/main.rs:90:9: 90:10
        _10 = other() -> [return: bb3, unwind continue]; // scope 3 at src/main.rs:90:13: 90:20
    }
    bb3: {
        StorageLive(_11);                // scope 4 at src/main.rs:91:5: 91:29
        StorageLive(_12);                // scope 4 at src/main.rs:91:26: 91:28
        _12 = &_2;                       // scope 4 at src/main.rs:91:26: 91:28
        _11 = std::hint::black_box::<&u32>(move _12) -> [return: bb4, unwind continue]; // scope 4 at src/main.rs:91:5: 91:29
    }
    bb4: {
        StorageDead(_12);                // scope 4 at src/main.rs:91:28: 91:29
        StorageDead(_11);                // scope 4 at src/main.rs:91:29: 91:30
        StorageLive(_13);                // scope 4 at src/main.rs:92:5: 92:30
        StorageLive(_14);                // scope 4 at src/main.rs:92:5: 92:6
        StorageLive(_15);                // scope 4 at src/main.rs:92:5: 92:6
        _15 = const {alloc20: &Atomic<u32>}; // scope 4 at src/main.rs:92:5: 92:6
        _14 = &(*_15);                   // scope 4 at src/main.rs:92:5: 92:6
        StorageLive(_16);                // scope 4 at src/main.rs:92:12: 92:29
        _16 = std::sync::atomic::Ordering::Relaxed; // scope 4 at src/main.rs:92:12: 92:29
        _13 = Atomic::<u32>::load(move _14, move _16) -> [return: bb5, unwind continue]; // scope 4 at src/main.rs:92:5: 92:30
    }
    bb5: {
        StorageDead(_16);                // scope 4 at src/main.rs:92:29: 92:30
        StorageDead(_14);                // scope 4 at src/main.rs:92:29: 92:30
        StorageLive(_17);                // scope 4 at src/main.rs:92:33: 92:34
        _17 = copy _10;                  // scope 4 at src/main.rs:92:33: 92:34
        _18 = AddWithOverflow(copy _13, copy _17); // scope 4 at src/main.rs:92:5: 92:34
        assert(!move (_18.1: bool), "attempt to compute `{} + {}`, which would overflow", move _13, move _17) -> [success: bb6, unwind continue]; // scope 4 at src/main.rs:92:5: 92:34
    }
    bb6: {
        _0 = move (_18.0: u32);          // scope 4 at src/main.rs:92:5: 92:34
        StorageDead(_17);                // scope 4 at src/main.rs:92:33: 92:34
        StorageDead(_15);                // scope 4 at src/main.rs:92:33: 92:34
        StorageDead(_13);                // scope 4 at src/main.rs:92:33: 92:34
        StorageDead(_10);                // scope 3 at src/main.rs:93:1: 93:2
        StorageDead(_6);                 // scope 2 at src/main.rs:93:1: 93:2
        StorageDead(_5);                 // scope 1 at src/main.rs:93:1: 93:2
        StorageDead(_4);                 // scope 1 at src/main.rs:93:1: 93:2
        StorageDead(_2);                 // scope 0 at src/main.rs:93:1: 93:2
        return;                          // scope 0 at src/main.rs:93:2: 93:2
    }
}
"""

R_CAPTURED_CLOSURE = """// MIR for `captured::{closure#0}` after PreCodegen

fn captured::{closure#0}(_1: &{closure@src/main.rs:88:15: 88:22}) -> () {
    debug a => ((*_1).0: &std::sync::atomic::Atomic<u32>); // in scope 0 at src/main.rs:87:9: 87:10
    debug x => ((*_1).1: u32);           // in scope 0 at src/main.rs:86:9: 86:10
    let mut _0: ();                      // return place in scope 0 at src/main.rs:88:22: 88:22
    let mut _2: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:88:23: 88:24
    let mut _3: u32;                     // in scope 0 at src/main.rs:88:31: 88:32
    let mut _4: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:88:34: 88:51
    let mut _5: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:88:15: 88:52
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:88:23: 88:24
        _5 = copy ((*_1).0: &std::sync::atomic::Atomic<u32>); // scope 0 at src/main.rs:88:23: 88:24
        _2 = &(*_5);                     // scope 0 at src/main.rs:88:23: 88:24
        StorageLive(_3);                 // scope 0 at src/main.rs:88:31: 88:32
        _3 = copy ((*_1).1: u32);        // scope 0 at src/main.rs:88:31: 88:32
        StorageLive(_4);                 // scope 0 at src/main.rs:88:34: 88:51
        _4 = std::sync::atomic::Ordering::Relaxed; // scope 0 at src/main.rs:88:34: 88:51
        _0 = Atomic::<u32>::store(move _2, move _3, move _4) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:88:23: 88:52
    }
    bb1: {
        StorageDead(_4);                 // scope 0 at src/main.rs:88:51: 88:52
        StorageDead(_3);                 // scope 0 at src/main.rs:88:51: 88:52
        StorageDead(_2);                 // scope 0 at src/main.rs:88:51: 88:52
        return;                          // scope 0 at src/main.rs:88:52: 88:52
    }
}
"""

R_ITEMED = """// MIR for `itemed` after PreCodegen

fn itemed(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:95:11: 95:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:95:24: 95:27
    let _2: u32;                         // in scope 0 at src/main.rs:96:9: 96:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:96:36: 96:39
    let _4: ();                          // in scope 0 at src/main.rs:97:5: 97:44
    let mut _5: std::iter::Once<u32>;    // in scope 0 at src/main.rs:97:5: 97:23
    let mut _6: u32;                     // in scope 0 at src/main.rs:97:21: 97:22
    let _8: &u32;                        // in scope 0 at src/main.rs:99:5: 99:29
    let mut _9: &u32;                    // in scope 0 at src/main.rs:99:26: 99:28
    let mut _10: u32;                    // in scope 0 at src/main.rs:100:5: 100:30
    let mut _11: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:100:5: 100:6
    let _12: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:100:5: 100:6
    let mut _13: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:100:12: 100:29
    let mut _14: u32;                    // in scope 0 at src/main.rs:100:33: 100:34
    let mut _15: (u32, bool);            // in scope 0 at src/main.rs:100:5: 100:34
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:96:9: 96:10
        let _7: u32;                     // in scope 1 at src/main.rs:98:9: 98:10
        scope 2 {
            debug k => _7;               // in scope 2 at src/main.rs:98:9: 98:10
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:96:9: 96:10
        StorageLive(_3);                 // scope 0 at src/main.rs:96:36: 96:39
        _3 = copy _1;                    // scope 0 at src/main.rs:96:36: 96:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:96:13: 96:40
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:96:39: 96:40
        StorageLive(_4);                 // scope 1 at src/main.rs:97:5: 97:44
        StorageLive(_5);                 // scope 1 at src/main.rs:97:5: 97:23
        StorageLive(_6);                 // scope 1 at src/main.rs:97:21: 97:22
        _6 = copy _2;                    // scope 1 at src/main.rs:97:21: 97:22
        _5 = once::<u32>(move _6) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:97:5: 97:23
    }
    bb2: {
        StorageDead(_6);                 // scope 1 at src/main.rs:97:22: 97:23
        _4 = <std::iter::Once<u32> as Iterator>::for_each::<fn(u32) {direct_set}>(move _5, direct_set) -> [return: bb3, unwind continue]; // scope 1 at src/main.rs:97:5: 97:44
    }
    bb3: {
        StorageDead(_5);                 // scope 1 at src/main.rs:97:43: 97:44
        StorageDead(_4);                 // scope 1 at src/main.rs:97:44: 97:45
        StorageLive(_7);                 // scope 1 at src/main.rs:98:9: 98:10
        _7 = other() -> [return: bb4, unwind continue]; // scope 1 at src/main.rs:98:13: 98:20
    }
    bb4: {
        StorageLive(_8);                 // scope 2 at src/main.rs:99:5: 99:29
        StorageLive(_9);                 // scope 2 at src/main.rs:99:26: 99:28
        _9 = &_2;                        // scope 2 at src/main.rs:99:26: 99:28
        _8 = std::hint::black_box::<&u32>(move _9) -> [return: bb5, unwind continue]; // scope 2 at src/main.rs:99:5: 99:29
    }
    bb5: {
        StorageDead(_9);                 // scope 2 at src/main.rs:99:28: 99:29
        StorageDead(_8);                 // scope 2 at src/main.rs:99:29: 99:30
        StorageLive(_10);                // scope 2 at src/main.rs:100:5: 100:30
        StorageLive(_11);                // scope 2 at src/main.rs:100:5: 100:6
        StorageLive(_12);                // scope 2 at src/main.rs:100:5: 100:6
        _12 = const {alloc20: &Atomic<u32>}; // scope 2 at src/main.rs:100:5: 100:6
        _11 = &(*_12);                   // scope 2 at src/main.rs:100:5: 100:6
        StorageLive(_13);                // scope 2 at src/main.rs:100:12: 100:29
        _13 = std::sync::atomic::Ordering::Relaxed; // scope 2 at src/main.rs:100:12: 100:29
        _10 = Atomic::<u32>::load(move _11, move _13) -> [return: bb6, unwind continue]; // scope 2 at src/main.rs:100:5: 100:30
    }
    bb6: {
        StorageDead(_13);                // scope 2 at src/main.rs:100:29: 100:30
        StorageDead(_11);                // scope 2 at src/main.rs:100:29: 100:30
        StorageLive(_14);                // scope 2 at src/main.rs:100:33: 100:34
        _14 = copy _7;                   // scope 2 at src/main.rs:100:33: 100:34
        _15 = AddWithOverflow(copy _10, copy _14); // scope 2 at src/main.rs:100:5: 100:34
        assert(!move (_15.1: bool), "attempt to compute `{} + {}`, which would overflow", move _10, move _14) -> [success: bb7, unwind continue]; // scope 2 at src/main.rs:100:5: 100:34
    }
    bb7: {
        _0 = move (_15.0: u32);          // scope 2 at src/main.rs:100:5: 100:34
        StorageDead(_14);                // scope 2 at src/main.rs:100:33: 100:34
        StorageDead(_12);                // scope 2 at src/main.rs:100:33: 100:34
        StorageDead(_10);                // scope 2 at src/main.rs:100:33: 100:34
        StorageDead(_7);                 // scope 1 at src/main.rs:101:1: 101:2
        StorageDead(_2);                 // scope 0 at src/main.rs:101:1: 101:2
        return;                          // scope 0 at src/main.rs:101:2: 101:2
    }
}
"""

R_CONVERTED = """// MIR for `converted` after PreCodegen

fn converted(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:103:14: 103:17
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:103:27: 103:30
    let _2: u32;                         // in scope 0 at src/main.rs:104:9: 104:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:104:36: 104:39
    let mut _5: u32;                     // in scope 0 at src/main.rs:105:20: 105:21
    let _7: &u32;                        // in scope 0 at src/main.rs:107:5: 107:29
    let mut _8: &u32;                    // in scope 0 at src/main.rs:107:26: 107:28
    let mut _9: u32;                     // in scope 0 at src/main.rs:108:5: 108:15
    let mut _10: &std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:108:5: 108:9
    let _11: std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:108:5: 108:9
    let mut _12: u32;                    // in scope 0 at src/main.rs:108:18: 108:19
    let mut _13: (u32, bool);            // in scope 0 at src/main.rs:108:5: 108:19
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:104:9: 104:10
        let _4: Wrap;                    // in scope 1 at src/main.rs:105:9: 105:11
        scope 2 {
            debug _w => _4;              // in scope 2 at src/main.rs:105:9: 105:11
            let _6: u32;                 // in scope 2 at src/main.rs:106:9: 106:10
            scope 3 {
                debug k => _6;           // in scope 3 at src/main.rs:106:9: 106:10
                let mut _14: &std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 3 at src/main.rs:108:5: 108:9
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
        StorageLive(_4);                 // scope 1 at src/main.rs:105:9: 105:11
        StorageLive(_5);                 // scope 1 at src/main.rs:105:20: 105:21
        _5 = copy _2;                    // scope 1 at src/main.rs:105:20: 105:21
        _4 = <u32 as Into<Wrap>>::into(move _5) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:105:20: 105:28
    }
    bb2: {
        StorageDead(_5);                 // scope 1 at src/main.rs:105:27: 105:28
        StorageLive(_6);                 // scope 2 at src/main.rs:106:9: 106:10
        _6 = other() -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:106:13: 106:20
    }
    bb3: {
        StorageLive(_7);                 // scope 3 at src/main.rs:107:5: 107:29
        StorageLive(_8);                 // scope 3 at src/main.rs:107:26: 107:28
        _8 = &_2;                        // scope 3 at src/main.rs:107:26: 107:28
        _7 = std::hint::black_box::<&u32>(move _8) -> [return: bb4, unwind continue]; // scope 3 at src/main.rs:107:5: 107:29
    }
    bb4: {
        StorageDead(_8);                 // scope 3 at src/main.rs:107:28: 107:29
        StorageDead(_7);                 // scope 3 at src/main.rs:107:29: 107:30
        StorageLive(_9);                 // scope 3 at src/main.rs:108:5: 108:15
        StorageLive(_10);                // scope 3 at src/main.rs:108:5: 108:9
        _14 = const converted::promoted[0]; // scope 3 at src/main.rs:108:5: 108:9
        _10 = &(*_14);                   // scope 3 at src/main.rs:108:5: 108:9
        _9 = LocalKey::<Cell<u32>>::get(move _10) -> [return: bb5, unwind continue]; // scope 3 at src/main.rs:108:5: 108:15
    }
    bb5: {
        StorageDead(_10);                // scope 3 at src/main.rs:108:14: 108:15
        StorageLive(_12);                // scope 3 at src/main.rs:108:18: 108:19
        _12 = copy _6;                   // scope 3 at src/main.rs:108:18: 108:19
        _13 = AddWithOverflow(copy _9, copy _12); // scope 3 at src/main.rs:108:5: 108:19
        assert(!move (_13.1: bool), "attempt to compute `{} + {}`, which would overflow", move _9, move _12) -> [success: bb6, unwind continue]; // scope 3 at src/main.rs:108:5: 108:19
    }
    bb6: {
        _0 = move (_13.0: u32);          // scope 3 at src/main.rs:108:5: 108:19
        StorageDead(_12);                // scope 3 at src/main.rs:108:18: 108:19
        StorageDead(_9);                 // scope 3 at src/main.rs:108:18: 108:19
        StorageDead(_6);                 // scope 2 at src/main.rs:109:1: 109:2
        StorageDead(_4);                 // scope 1 at src/main.rs:109:1: 109:2
        StorageDead(_2);                 // scope 0 at src/main.rs:109:1: 109:2
        return;                          // scope 0 at src/main.rs:109:2: 109:2
    }
}
"""

R_HIDDEN = """// MIR for `hidden` after PreCodegen

fn hidden(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:111:11: 111:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:111:24: 111:27
    let _2: u32;                         // in scope 0 at src/main.rs:112:9: 112:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:112:36: 112:39
    let _4: ();                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
    let _5: ();                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
    let mut _6: std::fmt::Arguments<'_>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let mut _8: &inner::Hidden;          // in scope 0 at src/main.rs:113:20: 113:36
    let _9: inner::Hidden;               // in scope 0 at src/main.rs:113:20: 113:36
    let mut _10: u32;                    // in scope 0 at src/main.rs:113:34: 113:35
    let mut _12: core::fmt::rt::Argument<'_>; // in scope 0 at src/main.rs:113:15: 113:17
    let mut _13: &inner::Hidden;         // in scope 0 at src/main.rs:113:20: 113:36
    let mut _14: &[u8; 4];               // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let _15: &[u8; 4];                   // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let mut _16: &[core::fmt::rt::Argument<'_>; 1]; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let _17: &[core::fmt::rt::Argument<'_>; 1]; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let _19: &u32;                       // in scope 0 at src/main.rs:115:5: 115:29
    let mut _20: &u32;                   // in scope 0 at src/main.rs:115:26: 115:28
    let mut _21: u32;                    // in scope 0 at src/main.rs:116:5: 116:15
    let mut _22: &std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:116:5: 116:9
    let _23: std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:116:5: 116:9
    let mut _24: u32;                    // in scope 0 at src/main.rs:116:18: 116:19
    let mut _25: (u32, bool);            // in scope 0 at src/main.rs:116:5: 116:19
    let mut _27: &inner::Hidden;         // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:112:9: 112:10
        let _7: (&inner::Hidden,);       // in scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        let _18: u32;                    // in scope 1 at src/main.rs:114:9: 114:10
        scope 2 {
            debug args => _7;            // in scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
            let _11: [core::fmt::rt::Argument<'_>; 1]; // in scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
            scope 3 {
                debug args => _11;       // in scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
            }
        }
        scope 4 {
            debug k => _18;              // in scope 4 at src/main.rs:114:9: 114:10
            let mut _26: &std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 4 at src/main.rs:116:5: 116:9
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:112:9: 112:10
        StorageLive(_3);                 // scope 0 at src/main.rs:112:36: 112:39
        _3 = copy _1;                    // scope 0 at src/main.rs:112:36: 112:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:112:13: 112:40
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:112:39: 112:40
        StorageLive(_4);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
        StorageLive(_5);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
        StorageLive(_6);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_7);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_8);                 // scope 1 at src/main.rs:113:20: 113:36
        StorageLive(_9);                 // scope 1 at src/main.rs:113:20: 113:36
        StorageLive(_10);                // scope 1 at src/main.rs:113:34: 113:35
        _10 = copy _2;                   // scope 1 at src/main.rs:113:34: 113:35
        _9 = Hidden(move _10);           // scope 1 at src/main.rs:113:20: 113:36
        StorageDead(_10);                // scope 1 at src/main.rs:113:35: 113:36
        _8 = &_9;                        // scope 1 at src/main.rs:113:20: 113:36
        _7 = (move _8,);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageDead(_8);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageLive(_11);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_12);                // scope 2 at src/main.rs:113:15: 113:17
        StorageLive(_13);                // scope 2 at src/main.rs:113:20: 113:36
        _27 = copy (_7.0: &inner::Hidden); // scope 2 at src/main.rs:113:20: 113:36
        _13 = &(*_27);                   // scope 2 at src/main.rs:113:20: 113:36
        _12 = core::fmt::rt::Argument::<'_>::new_display::<Hidden>(move _13) -> [return: bb2, unwind continue]; // scope 2 at src/main.rs:113:15: 113:17
    }
    bb2: {
        StorageDead(_13);                // scope 2 at src/main.rs:113:16: 113:17
        _11 = [move _12];                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageDead(_12);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageLive(_14);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_15);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _15 = const b"";   // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _14 = &(*_15);                   // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_16);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_17);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _17 = &_11;                      // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _16 = &(*_17);                   // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _6 = Arguments::<'_>::new::<4, 1>(move _14, move _16) -> [return: bb3, unwind continue]; // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    }
    bb3: {
        StorageDead(_17);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_16);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_15);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_14);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        _5 = std::io::_print(move _6) -> [return: bb4, unwind continue]; // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
    }
    bb4: {
        StorageDead(_6);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:61: 146:62
        StorageDead(_11);                // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        StorageDead(_9);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        StorageDead(_7);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        StorageDead(_5);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        _4 = const ();                   // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
        StorageDead(_4);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:147:5: 147:6
        StorageLive(_18);                // scope 1 at src/main.rs:114:9: 114:10
        _18 = other() -> [return: bb5, unwind continue]; // scope 1 at src/main.rs:114:13: 114:20
    }
    bb5: {
        StorageLive(_19);                // scope 4 at src/main.rs:115:5: 115:29
        StorageLive(_20);                // scope 4 at src/main.rs:115:26: 115:28
        _20 = &_2;                       // scope 4 at src/main.rs:115:26: 115:28
        _19 = std::hint::black_box::<&u32>(move _20) -> [return: bb6, unwind continue]; // scope 4 at src/main.rs:115:5: 115:29
    }
    bb6: {
        StorageDead(_20);                // scope 4 at src/main.rs:115:28: 115:29
        StorageDead(_19);                // scope 4 at src/main.rs:115:29: 115:30
        StorageLive(_21);                // scope 4 at src/main.rs:116:5: 116:15
        StorageLive(_22);                // scope 4 at src/main.rs:116:5: 116:9
        _26 = const hidden::promoted[0]; // scope 4 at src/main.rs:116:5: 116:9
        _22 = &(*_26);                   // scope 4 at src/main.rs:116:5: 116:9
        _21 = LocalKey::<Cell<u32>>::get(move _22) -> [return: bb7, unwind continue]; // scope 4 at src/main.rs:116:5: 116:15
    }
    bb7: {
        StorageDead(_22);                // scope 4 at src/main.rs:116:14: 116:15
        StorageLive(_24);                // scope 4 at src/main.rs:116:18: 116:19
        _24 = copy _18;                  // scope 4 at src/main.rs:116:18: 116:19
        _25 = AddWithOverflow(copy _21, copy _24); // scope 4 at src/main.rs:116:5: 116:19
        assert(!move (_25.1: bool), "attempt to compute `{} + {}`, which would overflow", move _21, move _24) -> [success: bb8, unwind continue]; // scope 4 at src/main.rs:116:5: 116:19
    }
    bb8: {
        _0 = move (_25.0: u32);          // scope 4 at src/main.rs:116:5: 116:19
        StorageDead(_24);                // scope 4 at src/main.rs:116:18: 116:19
        StorageDead(_21);                // scope 4 at src/main.rs:116:18: 116:19
        StorageDead(_18);                // scope 1 at src/main.rs:117:1: 117:2
        StorageDead(_2);                 // scope 0 at src/main.rs:117:1: 117:2
        return;                          // scope 0 at src/main.rs:117:2: 117:2
    }
}
"""

R_OWNED = """// MIR for `owned` after PreCodegen

fn owned(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:119:10: 119:13
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:119:23: 119:26
    let _2: u32;                         // in scope 0 at src/main.rs:120:9: 120:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:120:36: 120:39
    let mut _5: u32;                     // in scope 0 at src/main.rs:122:26: 122:27
    let mut _6: &mut u32;                // in scope 0 at src/main.rs:122:5: 122:23
    let mut _7: &mut std::sync::MutexGuard<'_, u32>; // in scope 0 at src/main.rs:122:6: 122:23
    let mut _8: std::sync::MutexGuard<'_, u32>; // in scope 0 at src/main.rs:122:6: 122:23
    let mut _9: std::result::Result<std::sync::MutexGuard<'_, u32>, std::sync::PoisonError<std::sync::MutexGuard<'_, u32>>>; // in scope 0 at src/main.rs:122:6: 122:14
    let mut _10: &std::sync::Mutex<u32>; // in scope 0 at src/main.rs:122:6: 122:7
    let _12: &u32;                       // in scope 0 at src/main.rs:124:5: 124:29
    let mut _13: &u32;                   // in scope 0 at src/main.rs:124:26: 124:28
    let mut _14: u32;                    // in scope 0 at src/main.rs:125:5: 125:30
    let mut _15: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:125:5: 125:6
    let _16: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:125:5: 125:6
    let mut _17: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:125:12: 125:29
    let mut _18: u32;                    // in scope 0 at src/main.rs:125:33: 125:34
    let mut _19: (u32, bool);            // in scope 0 at src/main.rs:125:5: 125:34
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:120:9: 120:10
        let _4: std::sync::Mutex<u32>;   // in scope 1 at src/main.rs:121:9: 121:10
        scope 2 {
            debug m => _4;               // in scope 2 at src/main.rs:121:9: 121:10
            let _11: u32;                // in scope 2 at src/main.rs:123:9: 123:10
            scope 3 {
                debug k => _11;          // in scope 3 at src/main.rs:123:9: 123:10
            }
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:120:9: 120:10
        StorageLive(_3);                 // scope 0 at src/main.rs:120:36: 120:39
        _3 = copy _1;                    // scope 0 at src/main.rs:120:36: 120:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:120:13: 120:40
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:120:39: 120:40
        StorageLive(_4);                 // scope 1 at src/main.rs:121:9: 121:10
        _4 = std::sync::Mutex::<u32>::new(const 0_u32) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:121:13: 121:26
    }
    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:122:26: 122:27
        _5 = copy _2;                    // scope 2 at src/main.rs:122:26: 122:27
        StorageLive(_6);                 // scope 2 at src/main.rs:122:5: 122:23
        StorageLive(_7);                 // scope 2 at src/main.rs:122:6: 122:23
        StorageLive(_8);                 // scope 2 at src/main.rs:122:6: 122:23
        StorageLive(_9);                 // scope 2 at src/main.rs:122:6: 122:14
        StorageLive(_10);                // scope 2 at src/main.rs:122:6: 122:7
        _10 = &_4;                       // scope 2 at src/main.rs:122:6: 122:7
        _9 = std::sync::Mutex::<u32>::lock(move _10) -> [return: bb3, unwind: bb13]; // scope 2 at src/main.rs:122:6: 122:14
    }
    bb3: {
        StorageDead(_10);                // scope 2 at src/main.rs:122:13: 122:14
        _8 = Result::<std::sync::MutexGuard<'_, u32>, PoisonError<std::sync::MutexGuard<'_, u32>>>::unwrap(move _9) -> [return: bb4, unwind: bb13]; // scope 2 at src/main.rs:122:6: 122:23
    }
    bb4: {
        _7 = &mut _8;                    // scope 2 at src/main.rs:122:6: 122:23
        StorageDead(_9);                 // scope 2 at src/main.rs:122:22: 122:23
        _6 = <std::sync::MutexGuard<'_, u32> as DerefMut>::deref_mut(move _7) -> [return: bb5, unwind: bb12]; // scope 2 at src/main.rs:122:5: 122:23
    }
    bb5: {
        StorageDead(_7);                 // scope 2 at src/main.rs:122:22: 122:23
        (*_6) = move _5;                 // scope 2 at src/main.rs:122:5: 122:27
        StorageDead(_5);                 // scope 2 at src/main.rs:122:26: 122:27
        drop(_8) -> [return: bb6, unwind: bb13]; // scope 2 at src/main.rs:122:27: 122:28
    }
    bb6: {
        StorageDead(_8);                 // scope 2 at src/main.rs:122:27: 122:28
        StorageDead(_6);                 // scope 2 at src/main.rs:122:27: 122:28
        StorageLive(_11);                // scope 2 at src/main.rs:123:9: 123:10
        _11 = other() -> [return: bb7, unwind: bb13]; // scope 2 at src/main.rs:123:13: 123:20
    }
    bb7: {
        StorageLive(_12);                // scope 3 at src/main.rs:124:5: 124:29
        StorageLive(_13);                // scope 3 at src/main.rs:124:26: 124:28
        _13 = &_2;                       // scope 3 at src/main.rs:124:26: 124:28
        _12 = std::hint::black_box::<&u32>(move _13) -> [return: bb8, unwind: bb13]; // scope 3 at src/main.rs:124:5: 124:29
    }
    bb8: {
        StorageDead(_13);                // scope 3 at src/main.rs:124:28: 124:29
        StorageDead(_12);                // scope 3 at src/main.rs:124:29: 124:30
        StorageLive(_14);                // scope 3 at src/main.rs:125:5: 125:30
        StorageLive(_15);                // scope 3 at src/main.rs:125:5: 125:6
        StorageLive(_16);                // scope 3 at src/main.rs:125:5: 125:6
        _16 = const {alloc20: &Atomic<u32>}; // scope 3 at src/main.rs:125:5: 125:6
        _15 = &(*_16);                   // scope 3 at src/main.rs:125:5: 125:6
        StorageLive(_17);                // scope 3 at src/main.rs:125:12: 125:29
        _17 = std::sync::atomic::Ordering::Relaxed; // scope 3 at src/main.rs:125:12: 125:29
        _14 = Atomic::<u32>::load(move _15, move _17) -> [return: bb9, unwind: bb13]; // scope 3 at src/main.rs:125:5: 125:30
    }
    bb9: {
        StorageDead(_17);                // scope 3 at src/main.rs:125:29: 125:30
        StorageDead(_15);                // scope 3 at src/main.rs:125:29: 125:30
        StorageLive(_18);                // scope 3 at src/main.rs:125:33: 125:34
        _18 = copy _11;                  // scope 3 at src/main.rs:125:33: 125:34
        _19 = AddWithOverflow(copy _14, copy _18); // scope 3 at src/main.rs:125:5: 125:34
        assert(!move (_19.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _18) -> [success: bb10, unwind: bb13]; // scope 3 at src/main.rs:125:5: 125:34
    }
    bb10: {
        _0 = move (_19.0: u32);          // scope 3 at src/main.rs:125:5: 125:34
        StorageDead(_18);                // scope 3 at src/main.rs:125:33: 125:34
        StorageDead(_16);                // scope 3 at src/main.rs:125:33: 125:34
        StorageDead(_14);                // scope 3 at src/main.rs:125:33: 125:34
        StorageDead(_11);                // scope 2 at src/main.rs:126:1: 126:2
        drop(_4) -> [return: bb11, unwind: bb14]; // scope 1 at src/main.rs:126:1: 126:2
    }
    bb11: {
        StorageDead(_4);                 // scope 1 at src/main.rs:126:1: 126:2
        StorageDead(_2);                 // scope 0 at src/main.rs:126:1: 126:2
        return;                          // scope 0 at src/main.rs:126:2: 126:2
    }
    bb12 (cleanup): {
        drop(_8) -> [return: bb13, unwind terminate(cleanup)]; // scope 2 at src/main.rs:122:27: 122:28
    }
    bb13 (cleanup): {
        drop(_4) -> [return: bb14, unwind terminate(cleanup)]; // scope 1 at src/main.rs:126:1: 126:2
    }
    bb14 (cleanup): {
        resume;                          // scope 0 at src/main.rs:119:1: 126:2
    }
}
"""

R_FROM = """// MIR for `<impl at src/main.rs:19:1: 19:24>::from` after PreCodegen

fn <impl at src/main.rs:19:1: 19:24>::from(_1: u32) -> Wrap {
    debug v => _1;                       // in scope 0 at src/main.rs:20:13: 20:14
    let mut _0: Wrap;                    // return place in scope 0 at src/main.rs:20:24: 20:28
    let _2: ();                          // in scope 0 at src/main.rs:21:9: 21:20
    let mut _3: &std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:21:9: 21:13
    let _4: std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:21:9: 21:13
    let mut _5: u32;                     // in scope 0 at src/main.rs:21:18: 21:19
    let mut _6: u32;                     // in scope 0 at src/main.rs:22:14: 22:15
    let mut _7: &std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:21:9: 21:13
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:21:9: 21:20
        StorageLive(_3);                 // scope 0 at src/main.rs:21:9: 21:13
        _7 = const <Wrap as std::convert::From<u32>>::from::promoted[0]; // scope 0 at src/main.rs:21:9: 21:13
        _3 = &(*_7);                     // scope 0 at src/main.rs:21:9: 21:13
        StorageLive(_5);                 // scope 0 at src/main.rs:21:18: 21:19
        _5 = copy _1;                    // scope 0 at src/main.rs:21:18: 21:19
        _2 = LocalKey::<Cell<u32>>::set(move _3, move _5) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:21:9: 21:20
    }
    bb1: {
        StorageDead(_5);                 // scope 0 at src/main.rs:21:19: 21:20
        StorageDead(_3);                 // scope 0 at src/main.rs:21:19: 21:20
        StorageDead(_2);                 // scope 0 at src/main.rs:21:20: 21:21
        StorageLive(_6);                 // scope 0 at src/main.rs:22:14: 22:15
        _6 = copy _1;                    // scope 0 at src/main.rs:22:14: 22:15
        _0 = Wrap(move _6);              // scope 0 at src/main.rs:22:9: 22:16
        StorageDead(_6);                 // scope 0 at src/main.rs:22:15: 22:16
        return;                          // scope 0 at src/main.rs:23:6: 23:6
    }
}
"""

R_FMT = """// MIR for `inner::<impl at src/main.rs:29:5: 29:38>::fmt` after PreCodegen

fn inner::<impl at src/main.rs:29:5: 29:38>::fmt(_1: &Hidden, _2: &mut Formatter<'_>) -> Result<(), std::fmt::Error> {
    debug self => _1;                    // in scope 0 at src/main.rs:30:16: 30:21
    debug f => _2;                       // in scope 0 at src/main.rs:30:23: 30:24
    let mut _0: std::result::Result<(), std::fmt::Error>; // return place in scope 0 at src/main.rs:30:59: 30:75
    let _3: ();                          // in scope 0 at src/main.rs:31:13: 31:36
    let mut _4: &std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:31:13: 31:24
    let _5: std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:31:13: 31:24
    let mut _6: u32;                     // in scope 0 at src/main.rs:31:29: 31:35
    let mut _7: &mut std::fmt::Formatter<'_>; // in scope 0 at src/main.rs:32:20: 32:21
    let mut _8: std::fmt::Arguments<'_>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/macros/mod.rs:604:24: 604:54
    let mut _9: &std::thread::LocalKey<std::cell::Cell<u32>>; // in scope 0 at src/main.rs:31:13: 31:24
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:31:13: 31:36
        StorageLive(_4);                 // scope 0 at src/main.rs:31:13: 31:24
        _9 = const <inner::Hidden as std::fmt::Display>::fmt::promoted[0]; // scope 0 at src/main.rs:31:13: 31:24
        _4 = &(*_9);                     // scope 0 at src/main.rs:31:13: 31:24
        StorageLive(_6);                 // scope 0 at src/main.rs:31:29: 31:35
        _6 = copy ((*_1).0: u32);        // scope 0 at src/main.rs:31:29: 31:35
        _3 = LocalKey::<Cell<u32>>::set(move _4, move _6) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:31:13: 31:36
    }
    bb1: {
        StorageDead(_6);                 // scope 0 at src/main.rs:31:35: 31:36
        StorageDead(_4);                 // scope 0 at src/main.rs:31:35: 31:36
        StorageDead(_3);                 // scope 0 at src/main.rs:31:36: 31:37
        StorageLive(_7);                 // scope 0 at src/main.rs:32:20: 32:21
        _7 = &mut (*_2);                 // scope 0 at src/main.rs:32:20: 32:21
        StorageLive(_8);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/macros/mod.rs:604:24: 604:54
        _8 = Arguments::<'_>::from_str(const "h") -> [return: bb2, unwind continue]; // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/macros/mod.rs:604:24: 604:54
    }
    bb2: {
        _0 = Formatter::<'_>::write_fmt(move _7, move _8) -> [return: bb3, unwind continue]; // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/macros/mod.rs:604:9: 604:55
    }
    bb3: {
        StorageDead(_8);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/macros/mod.rs:604:54: 604:55
        StorageDead(_7);                 // scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/macros/mod.rs:604:54: 604:55
        return;                          // scope 0 at src/main.rs:33:10: 33:10
    }
}
"""


HASH = "h0123456789abcdef"
# `Wrap`, as the build's debug information shows it: a `u32`, no cell (#321).
LAYOUTS = {"Wrap": CrateType(payloads=frozenset(), parts=frozenset({("prim", "u32")}))}
STD_MACROS = "/rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs"
# Every body of the crate the analysis reads.
CRATE = (
    LOCAL,
    LOCAL_SET,
    LOCAL_SET_CLOSURE,
    LOCAL_CLOSURE,
    DIRECT_SET,
    OUTER_SET,
    DEEPER,
    NOTE,
    OTHER,
    RAW,
    UNTOUCHED,
    READ_FIRST,
    RETURNED,
    SLOT_FN,
    LOCK_SET,
    RETURNED_REF,
    THROUGH_PTR,
    FMT,
)
KEY = "&std::thread::LocalKey<std::cell::Cell<u32>>"
ATOMIC = "&std::sync::atomic::Atomic<u32>"
SEEN = (
    "so a change to `{}` may never be seen: Peeky proves no read of the changed part first (a "
    "reference lent to a call, or `{{:?}}`, is not one)"
)


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


def owned_of(text, name, statement, crate=CRATE, defined=(), source=None, layouts=None):
    """OwnedLocals over the crate's bodies ``crate`` (of ``source``, ``SOURCE`` by default,
    whose types the debug information gives as ``layouts``, ``LAYOUTS`` by default), stopped
    in the function ``name`` of ``text`` only ever before its statement starting
    ``statement``."""
    body = parse_body(text)
    blocks = {}
    for number, block in body.blocks.items():
        lines = {at for _, file, at in block.statements if file == "src/main.rs" and at}
        blocks["start" if number == 0 else f"bb{number}"] = lines
    declared = min(at for _, file, at in body.blocks[0].statements if file == "src/main.rs")
    function = IrFunction(name, "globals", "src/main.rs", blocks, {}, declared - 1)
    bodies = [body, *(parse_body(each) for each in crate if each is not text)]
    owned = OwnedLocals(
        bodies,
        {HASH: function},
        ("src/main.rs",),
        texts={"src/main.rs": source or SOURCE},
        defined=defined,
    )
    owned.interior = LAYOUTS if layouts is None else layouts
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


def why(text, name, line, statement, variable, crate=CRATE, defined=(), source=None, layouts=None):
    """Why a change to ``variable`` is refused at a stop on ``line`` of the function ``name``
    of ``text``, before its statement starting ``statement`` (``mid_statement``'s reason):
    (the reason, its row's short form), or (None, None) where it is allowed."""
    owned = owned_of(text, name, statement, crate, defined, source, layouts)
    session = object.__new__(RustSession)
    session.owned = owned
    session.prepared = SimpleNamespace(source_root="/w")
    session._optimized = lambda: False
    session._stop_block = lambda hashed, frame: None
    session._projection = lambda *args: ()
    session._frame_scope = lambda frame, variable: None
    session._settable_names = lambda source, line: []
    session._inlined_below = lambda frame: None
    frame = {"name": f"globals::{name}::{HASH}", "line": line, "column": 5}
    frame["source"] = {"path": "/w/src/main.rs"}
    found = session.mid_statement({"name": variable}, frame, "breakpoint")
    return (None, None) if found is None else (str(found), found.row)


def test_what_each_function_may_touch_of_the_crates_global_state():
    state = owned_of(LOCAL, "local", "StorageLive(_9)")._globals()
    # What names a static with interior mutability, a `static mut` or a thread-local key.
    assert state.names["direct_set"] == {ATOMIC}
    assert state.names["local"] == {KEY}
    assert state.names["raw"] == {"*mut u32"}
    # What calls one, however deep.
    assert state.names["outer_set"] == {ATOMIC}
    assert state.names["deeper"] == {ATOMIC}
    # `local_set` writes only through the key it is given; `note` reads `PLAIN`, a `u32`;
    # the derived `Debug` passes `&dyn Debug`, which runs only an `impl` of it.
    for name in ("local_set", "note", "other", "fmt"):
        assert state.names[name] == frozenset(), name
    assert state.impls == frozenset()
    assert state.closures["{closure@src/main.rs:20:12: 20:15}"] == frozenset()
    assert state.unknown == frozenset()
    # A crate that names no such state: no call touches any.
    assert global_state([parse_body(each) for each in (NOTE, OTHER, OUTER_SET)]) is None


def test_a_copy_a_call_stores_in_a_thread_local_withholds_the_local():
    # The issue: `local_set(&SLOT, x)` (line 37) keeps `x` in the thread-local, and line 40's
    # `SLOT.with(|c| c.get())` reads it. At line 38, `x` was settable, and a change was lost.
    owned = owned_of(LOCAL, "local", "StorageLive(_9)")
    body = owned.body(HASH)
    at_38 = place(body, "StorageLive(_9)")
    (holder,) = {n for n in body.direct_copies_at(2, at_38) if n >= _GLOBAL_BASE}
    assert body.global_holders[holder] == (place(body, "_4 = local_set("), {KEY})
    assert body.copy_kind(holder, 2, at_38) == "end"
    assert body.global_level(holder, "u32") == "copy"
    assert why(LOCAL, "local", 38, "StorageLive(_9)", "x") == (
        "a static or thread-local may hold a copy of it (from line 37) that the program "
        "may go on with, " + SEEN.format("x"),
        "a static or thread-local may hold a copy (line 37)",
    )
    # Before the call, the change reaches it.
    assert why(LOCAL, "local", 37, "StorageLive(_4)", "x") == (None, None)


def test_a_function_that_names_a_static_stores_what_it_is_given_however_deep():
    # `outer_set(x)` calls `direct_set(x)`, which stores it in `A`; `A.load` reads it.
    assert why(DEEPER, "deeper", 46, "StorageLive(_6)", "x") == (
        "a static or thread-local may hold a copy of it (from line 45) that the program "
        "may go on with, " + SEEN.format("x"),
        "a static or thread-local may hold a copy (line 45)",
    )
    # A `static mut` written whole holds a copy too.
    assert why(RAW, "raw", 56, "StorageLive(_7)", "x") == (
        "a static or thread-local may hold a copy of it (from line 54) that the program "
        "may go on with, " + SEEN.format("x"),
        "a static or thread-local may hold a copy (line 54)",
    )
    # Where nothing reads `A` before the return, the caller may.
    assert why(RETURNED, "returned", 80, "StorageLive(_6)", "x")[0].startswith(
        "a static or thread-local may hold a copy of it (from line 79)"
    )


def test_what_a_pointer_stands_for_where_nothing_of_the_frame_does_outlives_it():
    # `slot()` returns `&S`, a `&'static Mutex<u32>`, and `lock_set(r, x)` (line 96) stores
    # `x` there; line 99 reads `S` by another call. `r` stands for what it points at, which
    # outlives `r` and the frame: a caller, or code after `r` ends, may read it.
    owned = owned_of(RETURNED_REF, "returned_ref", "StorageLive(_8)")
    body = owned.body(HASH)
    r = body.bindings["r"][0][0]
    assert body._outlives_frame(r)
    assert body.copy_kind(r, 2, place(body, "StorageLive(_8)")) == "end"
    assert why(RETURNED_REF, "returned_ref", 97, "StorageLive(_8)", "x") == (
        "the program's own `r` (at lines 95 and 96) may point at a copy of it that it goes on "
        "with, " + SEEN.format("x"),
        "the program may hold a copy (lines 95 and 96)",
    )
    # So does an argument `p` of `*p = x;` (line 104): the caller may read it.
    assert why(THROUGH_PTR, "through_ptr", 105, "StorageLive(_6)", "x") == (
        "the program's own `p` (at line 104) may point at a copy of it that it goes on with, "
        + SEEN.format("x"),
        "the program may hold a copy (line 104)",
    )
    # A binding of the frame's own, or a pointer made from one, ends with the frame.
    assert not body._outlives_frame(2)
    assert not body._outlives_frame(body.bindings["k"][0][0])


def test_a_call_that_touches_no_global_state_leaves_the_local_settable():
    # `note(x)` reads only `PLAIN`, and `println!("{:?}", Wrap(x))` runs a derived `Debug`:
    # neither keeps `x`, though line 67 reads `A`.
    assert why(UNTOUCHED, "untouched", 65, "StorageLive(_20)", "x") == (None, None)
    # The program reads `x` again first: it sees the change, whatever `A` holds.
    assert why(READ_FIRST, "read_first", 73, "StorageLive(_6)", "x") == (None, None)


def test_past_its_rounds_or_where_a_function_cant_be_read_any_call_may_touch_it(monkeypatch):
    # Failing closed: past the rounds, each function may touch any global, of a type not
    # known, so a call keeps state derived from what it is given.
    monkeypatch.setattr(rust_mir, "_GLOBAL_ROUNDS", 1)
    state = owned_of(UNTOUCHED, "untouched", "StorageLive(_20)")._globals()
    assert state.names["note"] == state.every >= {ATOMIC, KEY, _ANY_GLOBAL}
    assert why(UNTOUCHED, "untouched", 65, "StorageLive(_20)", "x") == (
        "a static or thread-local may hold state derived from it (from lines 63 and 64 and "
        "std/src/macros.rs:146) "
        "that the program may go on with, " + SEEN.format("x"),
        "a static or thread-local may hold state derived from it (lines 63 and 64 and "
        "std/src/macros.rs:146)",
    )
    monkeypatch.undo()
    # A function the dump has but Peeky can't read may touch anything.
    state = owned_of(LOCAL, "local", "StorageLive(_9)", defined={"mystery"})._globals()
    assert state.unknown == {"mystery"}
    assert state.call("mystery", ()) == state.every | {_ANY_GLOBAL}
    assert state.call("note", ("u32",)) == frozenset()


def test_what_a_value_given_to_a_call_may_run():
    state = GlobalState(
        names={"drop": frozenset({"D"})},
        closures={"{closure@src/main.rs:1:1: 1:5}": frozenset({"C"})},
        impls=frozenset({"I"}),
        every=frozenset({"E"}),
        unknown=frozenset(),
        layouts=TypeLayouts(LAYOUTS),
        formats=frozenset({"F"}),
    )
    assert state.carried("&std::vec::Vec<u32>") == frozenset()
    assert state.carried("u32") == frozenset()
    # A header's short standard name (`&Cell<u32>`), where the crate gives it nothing.
    assert state.carried("&Cell<u32>") == frozenset()
    assert state.carried("&Cell<Wrap>") == {"I"}
    assert state._replace(layouts=TypeLayouts(LAYOUTS, bare=frozenset())).carried("&Cell<u32>") == {
        "E"
    }
    assert state.carried("{closure@src/main.rs:1:1: 1:5}") == {"C"}
    assert state.carried("&{closure@src/main.rs:9:1: 9:5}") == {"E"}
    # A type of the crate's: the standard library may run one of its trait `impl`s.
    assert state.carried("&Wrap") == {"I"}
    assert state.carried("&std::vec::Vec<Wrap>") == {"I"}
    assert state.carried("&dyn std::fmt::Debug") == {"I"}
    # What holds a formatted value's `fmt` (`println!("{}", s)` hands `_print` only its
    # `Arguments`) or a `Formatter`'s writer runs only a formatting trait's `impl`; a
    # `Waker`'s wake or an `io::Error`'s custom error, any trait's.
    for kind in (
        "std::fmt::Arguments<'_>",
        "&[core::fmt::rt::Argument<'_>; 1]",
        "&mut std::fmt::Formatter<'_>",
    ):
        assert state.carried(kind) == {"F"}, kind
    for kind in ("std::task::Waker", "std::io::Error"):
        assert state.carried(kind) == {"I"}, kind
    # A function behind a pointer, a trait object of `Fn`, a type not known: anything.
    # A function item counts by its name: none of the crate's is named `direct_set` here.
    assert state.carried("fn(u32) {direct_set}") == frozenset()
    assert state.carried("fn(u32) {std::hint::black_box::<u32>}") == frozenset()
    assert state._replace(names={"direct_set": frozenset({"N"})}).carried(
        "fn(u32) {direct_set}"
    ) == {"N"}
    for kind in (
        "&dyn Fn(u32) -> u32",
        "&dyn for<'a> FnMut(&'a u32)",
        "fn(u32)",
        "&T",
        "&Other",
        None,
    ):
        assert state.carried(kind) == {"E"}, kind
    assert state._replace(layouts=TypeLayouts()).carried("&Wrap") == {"E"}
    assert state._replace(unknown=frozenset({"f"})).carried("&T") == {"E", _ANY_GLOBAL}
    # A call by a bare name, or a type's of the crate's, may be the crate's function so
    # named; a standard type's method or a standard path is the standard library's own.
    state = state._replace(names={**state.names, "set": frozenset({"S"})})
    std_cell = ("&std::cell::Cell<u32>", "u32")
    assert state.call("set", std_cell, "set") == {"S"}
    assert state.call("set", ("&Wrap", "u32"), "Wrap::set") == {"S", "I"}
    assert state.call("set", std_cell, "Cell::<u32>::set") == frozenset()
    assert state.call("set", std_cell, "std::cell::Cell::<u32>::set") == frozenset()
    assert state.call("set", std_cell, "<u32 as Tr>::set") == {"S"}
    # A drop: a standard type's drops nothing of the crate's; another runs a `Drop` of it.
    assert state.drop(("std::vec::Vec<u32>",)) == frozenset()
    assert state.drop(("std::vec::Vec<Wrap>",)) == {"D"}
    # Formatting alone runs only a formatting trait's `impl`.
    arguments = ("std::fmt::Arguments<'_>",)
    assert state.call("_print", arguments, "std::io::_print") == {"F"}


def test_what_stands_for_global_memory():
    for value in (
        "const {alloc46: &Atomic<u32>}",
        "const local::promoted[1]",
        "const <Shown as std::fmt::Display>::fmt::promoted[0]",
        "const apply::<u32, {closure@src/main.rs:5:5: 5:9}>::promoted[2]",
        "&/*tls*/ SLOT::{constant#0}::{closure#1}::__RUST_STD_INTERNAL_VAL",
    ):
        assert rust_mir._GLOBAL_VALUE.match(value), value
    # A cast of a constant is no address of a static; a constant of a type that holds no
    # pointer writes nothing (``global_roots`` judges the type).
    assert not rust_mir._GLOBAL_VALUE.match(
        "const 0_usize as *mut u8 (PointerWithExposedProvenance)"
    )


def test_a_copy_is_said_only_where_each_global_holds_the_very_type():
    body = owned_of(LOCAL, "local", "StorageLive(_9)").body(HASH)
    body.global_holders = {
        _GLOBAL_BASE: (None, frozenset({KEY})),
        _GLOBAL_BASE + 1: (None, frozenset({"&std::sync::atomic::Atomic<u64>"})),
        _GLOBAL_BASE + 2: (None, frozenset({"*mut u32"})),
        _GLOBAL_BASE + 3: (None, frozenset({"*mut *const u32"})),
        _GLOBAL_BASE + 4: (None, frozenset({KEY, _ANY_GLOBAL})),
        _GLOBAL_BASE + 5: (None, frozenset({KEY, "&std::sync::Mutex<u32>"})),
    }
    levels = [body.global_level(_GLOBAL_BASE + n, "u32") for n in range(6)]
    assert levels == ["copy", "derived", "copy", "derived", "derived", "copy"]
    assert body.global_level(_GLOBAL_BASE, "u64") == "derived"
    assert body.global_level(_GLOBAL_BASE, None) == "derived"


def test_an_inherent_impl_is_one_the_standard_library_cant_call():
    texts = {
        "src/main.rs": (
            "#[derive(Debug)]\nstruct Wrap(u32);\nimpl Wrap {\n    fn keep(&self) {}\n}\n"
            "impl std::fmt::Display for Wrap {\n    fn fmt(&self) {}\n}\n"
        )
    }
    assert _inherent("<impl at src/main.rs:3:1: 3:10>", texts)
    assert not _inherent("<impl at src/main.rs:6:1: 6:33>", texts)
    assert not _inherent("<impl at src/main.rs:1:10: 1:15>", texts)
    assert not _inherent("<impl at src/other.rs:3:1: 3:10>", texts)
    assert not _inherent("keep", texts)


def test_a_refusal_by_global_state_advises_a_change_before_the_call():
    from peeky.errors import _mutation_hint

    reason, _ = why(LOCAL, "local", 38, "StorageLive(_9)", "x")
    assert _mutation_hint(f"'x' cannot be changed here: {reason}") == (
        "Change it before the program copies it (a stop on an earlier line, or `--set-at` there)."
    )
    assert _mutation_hint(
        "`x` cannot be changed at src/main.rs:65, at any stop of this build: in "
        "globals::untouched, at every place line 65 can stop at, a static or thread-local may "
        "hold state derived from `x` (from line 63) that the program may go on with"
    ) == ("Change it before the program uses it (a stop on an earlier line, or `--set-at` there).")


# What the debug information gives of the review crate's types.
REVIEW_LAYOUTS = {
    "Job": CrateType(own=frozenset({"interior"})),
    **{
        name: CrateType(payloads=frozenset(), parts=frozenset({("prim", "u32")}))
        for name in ("Wrap", "inner::Hidden", "::Hidden")
    },
}
REVIEW = tuple(each for name, each in sorted(globals().items()) if name.startswith("R_"))


def first_at(text, line):
    """The first statement of ``text`` on ``line``."""
    return next(
        statement
        for block in parse_body(text).blocks.values()
        for statement, file, at in block.statements
        if at == line and file == "src/main.rs"
    )


def test_a_copy_that_reaches_global_state_another_way_withholds_the_local():
    # Review: each left `x` settable. A `&'static` a call returned into a temporary
    # (`lock_set(slot(), x)`, `registry().lock().unwrap().push(x)`); a value carrying both the
    # pointer and the copy (`run(Job { a: &A, v: x })`, a closure capturing `&A` and `x`); a
    # function item a standard call runs (`once(x).for_each(direct_set)`); a trait `impl` the
    # standard library runs for a type its path names (`<u32 as Into<Wrap>>::into`); and one
    # in a module (`println!("{}", inner::Hidden(x))`).
    for name, line, made in (
        ("slotted", 64, "line 63"),
        ("registered", 72, "line 71"),
        ("jobbed", 80, "line 79"),
        ("captured", 90, "line 89"),
        ("itemed", 98, "line 97"),
        ("converted", 106, "line 105"),
        ("hidden", 114, "line 113 and std/src/macros.rs:146"),
    ):
        text = globals()[f"R_{name.upper()}"]
        found = why(
            text, name, line, first_at(text, line), "x", REVIEW, (), REVIEW_SOURCE, REVIEW_LAYOUTS
        )
        assert found == (
            f"a static or thread-local may hold a copy of it (from {made}) that the program "
            "may go on with, " + SEEN.format("x"),
            f"a static or thread-local may hold a copy ({made})",
        ), name
    # A `Mutex` the frame owns, though a guard of it is what `*m.lock().unwrap() = x` writes
    # through, is no global state: nothing reads it after.
    found = why(
        R_OWNED,
        "owned",
        123,
        first_at(R_OWNED, 123),
        "x",
        REVIEW,
        (),
        REVIEW_SOURCE,
        REVIEW_LAYOUTS,
    )
    assert found == (None, None)
    state = owned_of(
        R_OWNED, "owned", first_at(R_OWNED, 123), REVIEW, (), REVIEW_SOURCE, REVIEW_LAYOUTS
    )._globals()
    # `From` and the module's `Display` are trait `impl`s.
    assert state.impls == {"&std::thread::LocalKey<std::cell::Cell<u32>>"}


# The `traits` crate (#329 review): a trait of the crate's own, whose `impl` for `Global`
# stores in `A`. Formatting and the standard library's `Vec::push` can't run it; only the
# crate's code calls it, by its name (`s.put(x)` through a `&dyn Sink`).
TRAITS_SOURCE = """use std::sync::atomic::{AtomicU32, Ordering};

static A: AtomicU32 = AtomicU32::new(0);

trait Sink {
    fn put(&self, v: u32);
}

struct Global;

struct Item(u32);

impl Sink for Global {
    fn put(&self, v: u32) {
        A.store(v, Ordering::Relaxed);
    }
}

fn other() -> u32 {
    std::hint::black_box(1)
}

fn printed(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    println!("{}", x);
    let k = other();
    std::hint::black_box(&x);
    A.load(Ordering::Relaxed) + k
}

fn pushed(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let mut items = Vec::new();
    items.push(Item(x));
    drop(items);
    let k = other();
    std::hint::black_box(&x);
    A.load(Ordering::Relaxed) + k
}

fn dynamic(arg: u32) -> u32 {
    let x = std::convert::identity(arg);
    let s: &dyn Sink = &Global;
    s.put(x);
    let k = other();
    std::hint::black_box(&x);
    A.load(Ordering::Relaxed) + k
}

fn main() {
    let n: u32 = std::env::args().nth(1).unwrap().parse().unwrap();
    println!("{}", printed(n) + pushed(n) + dynamic(n));
}
"""

T_OTHER = """// MIR for `other` after PreCodegen

fn other() -> u32 {
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:19:15: 19:18
    bb0: {
        _0 = std::hint::black_box::<u32>(const 1_u32) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:20:5: 20:28
    }
    bb1: {
        return;                          // scope 0 at src/main.rs:21:2: 21:2
    }
}
"""


T_PUT = """// MIR for `<impl at src/main.rs:13:1: 13:21>::put` after PreCodegen

fn <impl at src/main.rs:13:1: 13:21>::put(_1: &Global, _2: u32) -> () {
    debug self => _1;                    // in scope 0 at src/main.rs:14:12: 14:17
    debug v => _2;                       // in scope 0 at src/main.rs:14:19: 14:20
    let mut _0: ();                      // return place in scope 0 at src/main.rs:14:26: 14:26
    let _3: ();                          // in scope 0 at src/main.rs:15:9: 15:38
    let mut _4: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:15:9: 15:10
    let _5: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:15:9: 15:10
    let mut _6: u32;                     // in scope 0 at src/main.rs:15:17: 15:18
    let mut _7: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:15:20: 15:37
    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:15:9: 15:38
        StorageLive(_4);                 // scope 0 at src/main.rs:15:9: 15:10
        StorageLive(_5);                 // scope 0 at src/main.rs:15:9: 15:10
        _5 = const {alloc1: &Atomic<u32>}; // scope 0 at src/main.rs:15:9: 15:10
        _4 = &(*_5);                     // scope 0 at src/main.rs:15:9: 15:10
        StorageLive(_6);                 // scope 0 at src/main.rs:15:17: 15:18
        _6 = copy _2;                    // scope 0 at src/main.rs:15:17: 15:18
        StorageLive(_7);                 // scope 0 at src/main.rs:15:20: 15:37
        _7 = std::sync::atomic::Ordering::Relaxed; // scope 0 at src/main.rs:15:20: 15:37
        _3 = Atomic::<u32>::store(move _4, move _6, move _7) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:15:9: 15:38
    }
    bb1: {
        StorageDead(_7);                 // scope 0 at src/main.rs:15:37: 15:38
        StorageDead(_6);                 // scope 0 at src/main.rs:15:37: 15:38
        StorageDead(_4);                 // scope 0 at src/main.rs:15:37: 15:38
        StorageDead(_5);                 // scope 0 at src/main.rs:15:38: 15:39
        StorageDead(_3);                 // scope 0 at src/main.rs:15:38: 15:39
        _0 = const ();                   // scope 0 at src/main.rs:14:27: 16:6
        return;                          // scope 0 at src/main.rs:16:6: 16:6
    }
}
"""


T_PRINTED = """// MIR for `printed` after PreCodegen

fn printed(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:23:12: 23:15
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:23:25: 23:28
    let _2: u32;                         // in scope 0 at src/main.rs:24:9: 24:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:24:36: 24:39
    let _4: ();                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
    let _5: ();                          // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
    let mut _6: std::fmt::Arguments<'_>; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let mut _8: &u32;                    // in scope 0 at src/main.rs:25:20: 25:21
    let mut _10: core::fmt::rt::Argument<'_>; // in scope 0 at src/main.rs:25:15: 25:17
    let mut _11: &u32;                   // in scope 0 at src/main.rs:25:20: 25:21
    let mut _12: &[u8; 4];               // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let _13: &[u8; 4];                   // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let mut _14: &[core::fmt::rt::Argument<'_>; 1]; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let _15: &[core::fmt::rt::Argument<'_>; 1]; // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    let _17: &u32;                       // in scope 0 at src/main.rs:27:5: 27:29
    let mut _18: &u32;                   // in scope 0 at src/main.rs:27:26: 27:28
    let mut _19: u32;                    // in scope 0 at src/main.rs:28:5: 28:30
    let mut _20: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:28:5: 28:6
    let _21: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:28:5: 28:6
    let mut _22: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:28:12: 28:29
    let mut _23: u32;                    // in scope 0 at src/main.rs:28:33: 28:34
    let mut _24: (u32, bool);            // in scope 0 at src/main.rs:28:5: 28:34
    let mut _25: &u32;                   // in scope 0 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:24:9: 24:10
        let _7: (&u32,);                 // in scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        let _16: u32;                    // in scope 1 at src/main.rs:26:9: 26:10
        scope 2 {
            debug args => _7;            // in scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
            let _9: [core::fmt::rt::Argument<'_>; 1]; // in scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
            scope 3 {
                debug args => _9;        // in scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
            }
        }
        scope 4 {
            debug k => _16;              // in scope 4 at src/main.rs:26:9: 26:10
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:24:9: 24:10
        StorageLive(_3);                 // scope 0 at src/main.rs:24:36: 24:39
        _3 = copy _1;                    // scope 0 at src/main.rs:24:36: 24:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:24:13: 24:40
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:24:39: 24:40
        StorageLive(_4);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
        StorageLive(_5);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
        StorageLive(_6);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_7);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_8);                 // scope 1 at src/main.rs:25:20: 25:21
        _8 = &_2;                        // scope 1 at src/main.rs:25:20: 25:21
        _7 = (move _8,);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageDead(_8);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageLive(_9);                 // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_10);                // scope 2 at src/main.rs:25:15: 25:17
        StorageLive(_11);                // scope 2 at src/main.rs:25:20: 25:21
        _25 = copy (_7.0: &u32);         // scope 2 at src/main.rs:25:20: 25:21
        _11 = &(*_25);                   // scope 2 at src/main.rs:25:20: 25:21
        _10 = core::fmt::rt::Argument::<'_>::new_display::<u32>(move _11) -> [return: bb2, unwind continue]; // scope 2 at src/main.rs:25:15: 25:17
    }
    bb2: {
        StorageDead(_11);                // scope 2 at src/main.rs:25:16: 25:17
        _9 = [move _10];                 // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageDead(_10);                // scope 2 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageLive(_12);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_13);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _13 = const b"";   // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _12 = &(*_13);                   // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_14);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_15);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _15 = &_9;                       // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _14 = &(*_15);                   // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
        _6 = Arguments::<'_>::new::<4, 1>(move _12, move _14) -> [return: bb3, unwind continue]; // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:28: 146:61
    }
    bb3: {
        StorageDead(_15);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_14);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_13);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_12);                // scope 3 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:60: 146:61
        _5 = std::io::_print(move _6) -> [return: bb4, unwind continue]; // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:9: 146:62
    }
    bb4: {
        StorageDead(_6);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:61: 146:62
        StorageDead(_9);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        StorageDead(_7);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        StorageDead(_5);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:146:62: 146:63
        _4 = const ();                   // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:145:23: 147:6
        StorageDead(_4);                 // scope 1 at /rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/macros.rs:147:5: 147:6
        StorageLive(_16);                // scope 1 at src/main.rs:26:9: 26:10
        _16 = other() -> [return: bb5, unwind continue]; // scope 1 at src/main.rs:26:13: 26:20
    }
    bb5: {
        StorageLive(_17);                // scope 4 at src/main.rs:27:5: 27:29
        StorageLive(_18);                // scope 4 at src/main.rs:27:26: 27:28
        _18 = &_2;                       // scope 4 at src/main.rs:27:26: 27:28
        _17 = std::hint::black_box::<&u32>(move _18) -> [return: bb6, unwind continue]; // scope 4 at src/main.rs:27:5: 27:29
    }
    bb6: {
        StorageDead(_18);                // scope 4 at src/main.rs:27:28: 27:29
        StorageDead(_17);                // scope 4 at src/main.rs:27:29: 27:30
        StorageLive(_19);                // scope 4 at src/main.rs:28:5: 28:30
        StorageLive(_20);                // scope 4 at src/main.rs:28:5: 28:6
        StorageLive(_21);                // scope 4 at src/main.rs:28:5: 28:6
        _21 = const {alloc1: &Atomic<u32>}; // scope 4 at src/main.rs:28:5: 28:6
        _20 = &(*_21);                   // scope 4 at src/main.rs:28:5: 28:6
        StorageLive(_22);                // scope 4 at src/main.rs:28:12: 28:29
        _22 = std::sync::atomic::Ordering::Relaxed; // scope 4 at src/main.rs:28:12: 28:29
        _19 = Atomic::<u32>::load(move _20, move _22) -> [return: bb7, unwind continue]; // scope 4 at src/main.rs:28:5: 28:30
    }
    bb7: {
        StorageDead(_22);                // scope 4 at src/main.rs:28:29: 28:30
        StorageDead(_20);                // scope 4 at src/main.rs:28:29: 28:30
        StorageLive(_23);                // scope 4 at src/main.rs:28:33: 28:34
        _23 = copy _16;                  // scope 4 at src/main.rs:28:33: 28:34
        _24 = AddWithOverflow(copy _19, copy _23); // scope 4 at src/main.rs:28:5: 28:34
        assert(!move (_24.1: bool), "attempt to compute `{} + {}`, which would overflow", move _19, move _23) -> [success: bb8, unwind continue]; // scope 4 at src/main.rs:28:5: 28:34
    }
    bb8: {
        _0 = move (_24.0: u32);          // scope 4 at src/main.rs:28:5: 28:34
        StorageDead(_23);                // scope 4 at src/main.rs:28:33: 28:34
        StorageDead(_21);                // scope 4 at src/main.rs:28:33: 28:34
        StorageDead(_19);                // scope 4 at src/main.rs:28:33: 28:34
        StorageDead(_16);                // scope 1 at src/main.rs:29:1: 29:2
        StorageDead(_2);                 // scope 0 at src/main.rs:29:1: 29:2
        return;                          // scope 0 at src/main.rs:29:2: 29:2
    }
}
"""


T_PUSHED = """// MIR for `pushed` after PreCodegen

fn pushed(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:31:11: 31:14
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:31:24: 31:27
    let _2: u32;                         // in scope 0 at src/main.rs:32:9: 32:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:32:36: 32:39
    let _5: ();                          // in scope 0 at src/main.rs:34:5: 34:24
    let mut _6: &mut std::vec::Vec<Item>; // in scope 0 at src/main.rs:34:5: 34:10
    let mut _7: Item;                    // in scope 0 at src/main.rs:34:16: 34:23
    let mut _8: u32;                     // in scope 0 at src/main.rs:34:21: 34:22
    let _9: ();                          // in scope 0 at src/main.rs:35:5: 35:16
    let mut _10: std::vec::Vec<Item>;    // in scope 0 at src/main.rs:35:10: 35:15
    let _12: &u32;                       // in scope 0 at src/main.rs:37:5: 37:29
    let mut _13: &u32;                   // in scope 0 at src/main.rs:37:26: 37:28
    let mut _14: u32;                    // in scope 0 at src/main.rs:38:5: 38:30
    let mut _15: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:38:5: 38:6
    let _16: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:38:5: 38:6
    let mut _17: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:38:12: 38:29
    let mut _18: u32;                    // in scope 0 at src/main.rs:38:33: 38:34
    let mut _19: (u32, bool);            // in scope 0 at src/main.rs:38:5: 38:34
    let mut _20: bool;                   // in scope 0 at src/main.rs:39:1: 39:2
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:32:9: 32:10
        let mut _4: std::vec::Vec<Item>; // in scope 1 at src/main.rs:33:9: 33:18
        scope 2 {
            debug items => _4;           // in scope 2 at src/main.rs:33:9: 33:18
            let _11: u32;                // in scope 2 at src/main.rs:36:9: 36:10
            scope 3 {
                debug k => _11;          // in scope 3 at src/main.rs:36:9: 36:10
            }
        }
    }
    bb0: {
        _20 = const false;               // scope 0 at src/main.rs:32:9: 32:10
        StorageLive(_2);                 // scope 0 at src/main.rs:32:9: 32:10
        StorageLive(_3);                 // scope 0 at src/main.rs:32:36: 32:39
        _3 = copy _1;                    // scope 0 at src/main.rs:32:36: 32:39
        _2 = identity::<u32>(move _3) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:32:13: 32:40
    }
    bb1: {
        StorageDead(_3);                 // scope 0 at src/main.rs:32:39: 32:40
        StorageLive(_4);                 // scope 1 at src/main.rs:33:9: 33:18
        _20 = const true;                // scope 1 at src/main.rs:33:21: 33:31
        _4 = Vec::<Item>::new() -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:33:21: 33:31
    }
    bb2: {
        StorageLive(_5);                 // scope 2 at src/main.rs:34:5: 34:24
        StorageLive(_6);                 // scope 2 at src/main.rs:34:5: 34:10
        _6 = &mut _4;                    // scope 2 at src/main.rs:34:5: 34:10
        StorageLive(_7);                 // scope 2 at src/main.rs:34:16: 34:23
        StorageLive(_8);                 // scope 2 at src/main.rs:34:21: 34:22
        _8 = copy _2;                    // scope 2 at src/main.rs:34:21: 34:22
        _7 = Item(move _8);              // scope 2 at src/main.rs:34:16: 34:23
        StorageDead(_8);                 // scope 2 at src/main.rs:34:22: 34:23
        _5 = Vec::<Item>::push(move _6, move _7) -> [return: bb3, unwind: bb11]; // scope 2 at src/main.rs:34:5: 34:24
    }
    bb3: {
        StorageDead(_7);                 // scope 2 at src/main.rs:34:23: 34:24
        StorageDead(_6);                 // scope 2 at src/main.rs:34:23: 34:24
        StorageDead(_5);                 // scope 2 at src/main.rs:34:24: 34:25
        StorageLive(_9);                 // scope 2 at src/main.rs:35:5: 35:16
        StorageLive(_10);                // scope 2 at src/main.rs:35:10: 35:15
        _20 = const false;               // scope 2 at src/main.rs:35:10: 35:15
        _10 = move _4;                   // scope 2 at src/main.rs:35:10: 35:15
        _9 = std::mem::drop::<Vec<Item>>(move _10) -> [return: bb4, unwind: bb11]; // scope 2 at src/main.rs:35:5: 35:16
    }
    bb4: {
        StorageDead(_10);                // scope 2 at src/main.rs:35:15: 35:16
        StorageDead(_9);                 // scope 2 at src/main.rs:35:16: 35:17
        StorageLive(_11);                // scope 2 at src/main.rs:36:9: 36:10
        _11 = other() -> [return: bb5, unwind: bb11]; // scope 2 at src/main.rs:36:13: 36:20
    }
    bb5: {
        StorageLive(_12);                // scope 3 at src/main.rs:37:5: 37:29
        StorageLive(_13);                // scope 3 at src/main.rs:37:26: 37:28
        _13 = &_2;                       // scope 3 at src/main.rs:37:26: 37:28
        _12 = std::hint::black_box::<&u32>(move _13) -> [return: bb6, unwind: bb11]; // scope 3 at src/main.rs:37:5: 37:29
    }
    bb6: {
        StorageDead(_13);                // scope 3 at src/main.rs:37:28: 37:29
        StorageDead(_12);                // scope 3 at src/main.rs:37:29: 37:30
        StorageLive(_14);                // scope 3 at src/main.rs:38:5: 38:30
        StorageLive(_15);                // scope 3 at src/main.rs:38:5: 38:6
        StorageLive(_16);                // scope 3 at src/main.rs:38:5: 38:6
        _16 = const {alloc1: &Atomic<u32>}; // scope 3 at src/main.rs:38:5: 38:6
        _15 = &(*_16);                   // scope 3 at src/main.rs:38:5: 38:6
        StorageLive(_17);                // scope 3 at src/main.rs:38:12: 38:29
        _17 = std::sync::atomic::Ordering::Relaxed; // scope 3 at src/main.rs:38:12: 38:29
        _14 = Atomic::<u32>::load(move _15, move _17) -> [return: bb7, unwind: bb11]; // scope 3 at src/main.rs:38:5: 38:30
    }
    bb7: {
        StorageDead(_17);                // scope 3 at src/main.rs:38:29: 38:30
        StorageDead(_15);                // scope 3 at src/main.rs:38:29: 38:30
        StorageLive(_18);                // scope 3 at src/main.rs:38:33: 38:34
        _18 = copy _11;                  // scope 3 at src/main.rs:38:33: 38:34
        _19 = AddWithOverflow(copy _14, copy _18); // scope 3 at src/main.rs:38:5: 38:34
        assert(!move (_19.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _18) -> [success: bb8, unwind: bb11]; // scope 3 at src/main.rs:38:5: 38:34
    }
    bb8: {
        _0 = move (_19.0: u32);          // scope 3 at src/main.rs:38:5: 38:34
        StorageDead(_18);                // scope 3 at src/main.rs:38:33: 38:34
        StorageDead(_16);                // scope 3 at src/main.rs:38:33: 38:34
        StorageDead(_14);                // scope 3 at src/main.rs:38:33: 38:34
        StorageDead(_11);                // scope 2 at src/main.rs:39:1: 39:2
        _20 = const false;               // scope 1 at src/main.rs:39:1: 39:2
        StorageDead(_4);                 // scope 1 at src/main.rs:39:1: 39:2
        StorageDead(_2);                 // scope 0 at src/main.rs:39:1: 39:2
        return;                          // scope 0 at src/main.rs:39:2: 39:2
    }
    bb9 (cleanup): {
        resume;                          // scope 0 at src/main.rs:31:1: 39:2
    }
    bb10 (cleanup): {
        drop(_4) -> [return: bb9, unwind terminate(cleanup)]; // scope 1 at src/main.rs:39:1: 39:2
    }
    bb11 (cleanup): {
        switchInt(copy _20) -> [0: bb9, otherwise: bb10]; // scope 1 at src/main.rs:39:1: 39:2
    }
}
"""


T_DYNAMIC = """// MIR for `dynamic` after PreCodegen

fn dynamic(_1: u32) -> u32 {
    debug arg => _1;                     // in scope 0 at src/main.rs:41:12: 41:15
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:41:25: 41:28
    let _2: u32;                         // in scope 0 at src/main.rs:42:9: 42:10
    let mut _3: u32;                     // in scope 0 at src/main.rs:42:36: 42:39
    let mut _5: &Global;                 // in scope 0 at src/main.rs:43:24: 43:31
    let _6: &Global;                     // in scope 0 at src/main.rs:43:24: 43:31
    let _7: Global;                      // in scope 0 at src/main.rs:43:25: 43:31
    let _8: ();                          // in scope 0 at src/main.rs:44:5: 44:13
    let mut _9: &dyn Sink;               // in scope 0 at src/main.rs:44:5: 44:6
    let mut _10: u32;                    // in scope 0 at src/main.rs:44:11: 44:12
    let _12: &u32;                       // in scope 0 at src/main.rs:46:5: 46:29
    let mut _13: &u32;                   // in scope 0 at src/main.rs:46:26: 46:28
    let mut _14: u32;                    // in scope 0 at src/main.rs:47:5: 47:30
    let mut _15: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:47:5: 47:6
    let _16: &std::sync::atomic::Atomic<u32>; // in scope 0 at src/main.rs:47:5: 47:6
    let mut _17: std::sync::atomic::Ordering; // in scope 0 at src/main.rs:47:12: 47:29
    let mut _18: u32;                    // in scope 0 at src/main.rs:47:33: 47:34
    let mut _19: (u32, bool);            // in scope 0 at src/main.rs:47:5: 47:34
    scope 1 {
        debug x => _2;                   // in scope 1 at src/main.rs:42:9: 42:10
        let _4: &dyn Sink;               // in scope 1 at src/main.rs:43:9: 43:10
        let mut _20: &Global;            // in scope 1 at src/main.rs:43:24: 43:31
        scope 2 {
            debug s => _4;               // in scope 2 at src/main.rs:43:9: 43:10
            let _11: u32;                // in scope 2 at src/main.rs:45:9: 45:10
            scope 3 {
                debug k => _11;          // in scope 3 at src/main.rs:45:9: 45:10
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
        StorageLive(_4);                 // scope 1 at src/main.rs:43:9: 43:10
        StorageLive(_5);                 // scope 1 at src/main.rs:43:24: 43:31
        StorageLive(_6);                 // scope 1 at src/main.rs:43:24: 43:31
        _20 = const dynamic::promoted[0]; // scope 1 at src/main.rs:43:24: 43:31
        _6 = &(*_20);                    // scope 1 at src/main.rs:43:24: 43:31
        _5 = &(*_6);                     // scope 1 at src/main.rs:43:24: 43:31
        _4 = move _5 as &dyn Sink (PointerCoercion(Unsize, Implicit)); // scope 1 at src/main.rs:43:24: 43:31
        StorageDead(_5);                 // scope 1 at src/main.rs:43:30: 43:31
        StorageDead(_6);                 // scope 1 at src/main.rs:43:31: 43:32
        StorageLive(_8);                 // scope 2 at src/main.rs:44:5: 44:13
        StorageLive(_9);                 // scope 2 at src/main.rs:44:5: 44:6
        _9 = &(*_4);                     // scope 2 at src/main.rs:44:5: 44:6
        StorageLive(_10);                // scope 2 at src/main.rs:44:11: 44:12
        _10 = copy _2;                   // scope 2 at src/main.rs:44:11: 44:12
        _8 = <dyn Sink as Sink>::put(move _9, move _10) -> [return: bb2, unwind continue]; // scope 2 at src/main.rs:44:5: 44:13
    }
    bb2: {
        StorageDead(_10);                // scope 2 at src/main.rs:44:12: 44:13
        StorageDead(_9);                 // scope 2 at src/main.rs:44:12: 44:13
        StorageDead(_8);                 // scope 2 at src/main.rs:44:13: 44:14
        StorageLive(_11);                // scope 2 at src/main.rs:45:9: 45:10
        _11 = other() -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:45:13: 45:20
    }
    bb3: {
        StorageLive(_12);                // scope 3 at src/main.rs:46:5: 46:29
        StorageLive(_13);                // scope 3 at src/main.rs:46:26: 46:28
        _13 = &_2;                       // scope 3 at src/main.rs:46:26: 46:28
        _12 = std::hint::black_box::<&u32>(move _13) -> [return: bb4, unwind continue]; // scope 3 at src/main.rs:46:5: 46:29
    }
    bb4: {
        StorageDead(_13);                // scope 3 at src/main.rs:46:28: 46:29
        StorageDead(_12);                // scope 3 at src/main.rs:46:29: 46:30
        StorageLive(_14);                // scope 3 at src/main.rs:47:5: 47:30
        StorageLive(_15);                // scope 3 at src/main.rs:47:5: 47:6
        StorageLive(_16);                // scope 3 at src/main.rs:47:5: 47:6
        _16 = const {alloc1: &Atomic<u32>}; // scope 3 at src/main.rs:47:5: 47:6
        _15 = &(*_16);                   // scope 3 at src/main.rs:47:5: 47:6
        StorageLive(_17);                // scope 3 at src/main.rs:47:12: 47:29
        _17 = std::sync::atomic::Ordering::Relaxed; // scope 3 at src/main.rs:47:12: 47:29
        _14 = Atomic::<u32>::load(move _15, move _17) -> [return: bb5, unwind continue]; // scope 3 at src/main.rs:47:5: 47:30
    }
    bb5: {
        StorageDead(_17);                // scope 3 at src/main.rs:47:29: 47:30
        StorageDead(_15);                // scope 3 at src/main.rs:47:29: 47:30
        StorageLive(_18);                // scope 3 at src/main.rs:47:33: 47:34
        _18 = copy _11;                  // scope 3 at src/main.rs:47:33: 47:34
        _19 = AddWithOverflow(copy _14, copy _18); // scope 3 at src/main.rs:47:5: 47:34
        assert(!move (_19.1: bool), "attempt to compute `{} + {}`, which would overflow", move _14, move _18) -> [success: bb6, unwind continue]; // scope 3 at src/main.rs:47:5: 47:34
    }
    bb6: {
        _0 = move (_19.0: u32);          // scope 3 at src/main.rs:47:5: 47:34
        StorageDead(_18);                // scope 3 at src/main.rs:47:33: 47:34
        StorageDead(_16);                // scope 3 at src/main.rs:47:33: 47:34
        StorageDead(_14);                // scope 3 at src/main.rs:47:33: 47:34
        StorageDead(_11);                // scope 2 at src/main.rs:48:1: 48:2
        StorageDead(_4);                 // scope 1 at src/main.rs:48:1: 48:2
        StorageDead(_2);                 // scope 0 at src/main.rs:48:1: 48:2
        return;                          // scope 0 at src/main.rs:48:2: 48:2
    }
}
"""

TRAITS = (T_OTHER, T_PUT, T_PRINTED, T_PUSHED, T_DYNAMIC)
TRAITS_LAYOUTS = {
    "Global": CrateType(payloads=frozenset(), parts=frozenset(), single=True),
    "Item": CrateType(payloads=frozenset(), parts=frozenset({("prim", "u32")})),
}


def traits_why(text, name, line, source=TRAITS_SOURCE):
    return why(text, name, line, first_at(text, line), "x", TRAITS, (), source, TRAITS_LAYOUTS)


def test_a_trait_of_the_crates_own_runs_only_where_the_crates_code_calls_it():
    # `println!("{}", x)` runs only a formatting trait's `impl`, and `items.push(Item(x))`
    # only the standard library's code and a trait's `impl` it may call for `Item`: neither
    # runs `Sink::put`, though it stores in `A`, which line 28 reads. Each was refused.
    assert traits_why(T_PRINTED, "printed", 26) == (None, None)
    assert traits_why(T_PUSHED, "pushed", 36) == (None, None)
    state = owned_of(
        T_PRINTED, "printed", first_at(T_PRINTED, 26), TRAITS, (), TRAITS_SOURCE, TRAITS_LAYOUTS
    )._globals()
    assert state.names["put"] == {ATOMIC}
    assert state.impls == state.formats == frozenset()
    # `s.put(x)` calls it by its name, through a `&dyn Sink`.
    reason, row = traits_why(T_DYNAMIC, "dynamic", 45)
    assert "a static or thread-local may hold a copy of it (from line 44)" in reason, reason
    # Where a `use` of a path outside the crate names `Sink`, the `impl` may be of that
    # crate's trait, which its code, given an `Item`, may run; formatting still runs none.
    used = TRAITS_SOURCE + "use other::Sink;\n"
    assert traits_why(T_PUSHED, "pushed", 36, used)[0].startswith(
        "a static or thread-local may hold a copy of it (from lines 34 and 35)"
    )
    assert traits_why(T_PRINTED, "printed", 26, used) == (None, None)


def test_the_trait_an_impl_block_implements():
    texts = {
        "src/main.rs": (
            "#[derive(Debug, Clone)]\n"
            "struct Wrap(u32);\n"
            "impl Wrap {\n"
            "impl std::fmt::Display for Wrap {\n"
            "impl<T: Clone + Into<u32>> Sink<T> for Gen<T> {\n"
            "impl<'a, F: for<'b> Fn(&'b u32) -> u32> crate::Sink<u32> for Holder<'a, F> {\n"
            "unsafe impl Send for Wrap {}\n"
            "impl<T> Gen<T> {\n"
            "trait Sink<T> {}\n"
            "use std::fmt;\n"
        )
    }
    lines = texts["src/main.rs"].split("\n")

    def block(n):
        line = lines[n - 1]
        end = len(line.removesuffix(" {}").removesuffix(" {")) + 1
        return f"<impl at src/main.rs:{n}:1: {n}:{end}>"

    assert [_impl_trait(block(n), texts) for n in range(3, 9)] == [
        ("", False),
        ("std::fmt::Display", False),
        ("Sink", False),
        ("crate::Sink", False),
        ("Send", False),
        ("", False),
    ]
    # A derived one's span is its `derive`'s; one Peeky can't read is not known.
    assert _impl_trait("<impl at src/main.rs:1:10: 1:15>", texts) == ("Debug", True)
    assert _impl_trait("<impl at src/other.rs:1:10: 1:15>", texts) is None
    traits = _crate_traits(texts)
    assert traits == ({"Sink"}, {"std", "fmt"}, False)
    # The crate's own trait, by its name or a path from the crate.
    assert _own_trait(("Sink", False), traits)
    assert _own_trait(("crate::Sink", False), traits)
    # Not another's, a derived one, the prelude's, nor one a path from elsewhere names.
    for found in (
        ("std::fmt::Display", False),
        ("Debug", True),
        ("Send", False),
        ("other::Sink", False),
        ("", False),
        None,
    ):
        assert not _own_trait(found, traits), found
    # A `use` of a path outside the crate naming it, or a glob one, may bring another's.
    for line in ("use other::{Sink as Put, Other};", "use other::*;"):
        both = _crate_traits({**texts, "src/m.rs": line})
        assert not _own_trait(("Sink", False), both), line
    assert _own_trait(("crate::Sink", False), _crate_traits({**texts, "src/m.rs": "use other::*;"}))
    assert _own_trait(("Sink", False), _crate_traits({**texts, "src/m.rs": "use crate::Sink;"}))
