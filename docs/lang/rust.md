# Peeky for Rust

What is particular to Rust targets (a Cargo crate), one rule per line, each with the [COOKBOOK](../COOKBOOK.md) section that has the details. Read [AGENT_GUIDE.md](../AGENT_GUIDE.md) first: the loop and the rules every language shares are there.

## Targets

- The target is the crate directory with its `Cargo.lock`: a plain `cargo new` binary crate works (edition 2021 or 2024, no dependencies, no `src/lib.rs`, `src/bin` or `build.rs`, at most one integration test); run `cargo generate-lockfile` once if `Cargo.lock` is missing ([7.2](../COOKBOOK.md#72-rust-a-cargo-crate)).
- A probe whose build fails (`BUILD_FAILED`, a candidate edit that does not compile) is refused before launch: exit 2, no table, no run saved; `verify` calls it a failed verify, exit 1 ([ERRORS](../ERRORS.md)).
- `--at src/main.rs:LINE`; program arguments go after `--` or in repeated `--arg` ([2.5](../COOKBOOK.md#25-a-module-imported-by-a-script-with-arguments)). `verify` also runs the crate's integration test, when it has one ([6.5](../COOKBOOK.md#65-writing-a-checker)).

## Reading values

- Locals show their Rust types. An owned `String`, or a `Vec` of plain elements (integers, `bool`, floats, `char`, or tuples and crate structs of them), and a reference parameter of such elements (`&[T]`, `&str`), are read from memory (`[memory]`) in an unoptimized build with lldb-dap 20. A `Vec<String>` stays raw ([7.2](../COOKBOOK.md#72-rust-a-cargo-crate)).
- `Vec` and `String` fields read too: `self.values` behind `&mut self` (innermost frame only; behind `&self`, any frame), and `w.values` in `main`, which owns `w`; a moved struct's stay raw, with a note ([7.2](../COOKBOOK.md#72-rust-a-cargo-crate)).
- A crate with `unsafe` code or a nightly feature reads no `Vec` or `String` contents, no reference parameter's contents and no such field: each stays raw, with the reason; scalars are still read ([7.2](../COOKBOOK.md#72-rust-a-cargo-crate)).
- Never guessed: a value Peeky cannot prove (moved or dropped, in a caller frame, in an optimized build) keeps its raw fields with a note (`[stale]`, `[unproven]`, `<unverified ...>`); a local whose storage may hold leftovers is withheld (`no value yet:` and why): unassigned on a path (`let x;`) or not kept across an `.await`; an enum Peeky cannot decode with certainty stays opaque ([7.2](../COOKBOOK.md#72-rust-a-cargo-crate)).
- A tuple's fields are `.__0`, `.__1` (`runs[0].__0`) ([7.2](../COOKBOOK.md#72-rust-a-cargo-crate)); a shadowed `let` is listed as `x @ main.rs:6`, which selects it (`s @ main.rs:81.a`) ([guide](../AGENT_GUIDE.md#targets-and-selectors)).
- A one-line `for` stops at three locations (1, 2, 3, 3, 3 over three passes): rows carry `stop`; for one row per pass, put `--at` on a plain statement line in the body ([2.2](../COOKBOOK.md#22-every-iteration-of-a-loop)).

## Changing values

- `--set` changes a 16- to 64-bit integer local kept in memory, or such a field or element of a struct, tuple or array local the frame owns (`--set w.start=10`), where the MIR proves it initialized and nothing under way has read it ([7.2](../COOKBOOK.md#72-rust-a-cargo-crate)). `describe`'s `mutable:` line lists, per function, the fields and elements `set` may change (`board.entries`, `grid.cells[*].n`).
- A binding of the program's own that copied the value and goes on with it (`n` of `x = match x { n => … n }`, `let y = x;` … `x = y + 1;`) blocks the change unless the program reads the changed part itself again (a reference given to a call, or `{:?}`, does not count): change it before the copy is made, or change the copy ([7.2](../COOKBOOK.md#72-rust-a-cargo-crate)).
- A local kept in a register is `[not settable: register]` (`describe`'s `mutable:` line lists them): a candidate edit adding `std::hint::black_box(&mut x);` after its `let` keeps it in memory, and the same probe can set it ([6.6](../COOKBOOK.md#66-see-what-a-fix-does-before-making-it)).
- A `String` takes a text in double quotes (`--set 'name="pear"'`) within its capacity, where no borrow of it (`&s[..2]`, `s.chars()`) is used after the stop, with lldb-dap 21 (macOS); never a `&str`, nor in a crate with `unsafe` code or a nightly feature ([7.2](../COOKBOOK.md#72-rust-a-cargo-crate)).
- Never through a reference or a `Box`: for `w.start` where `w: &mut Window`, set it in the caller that owns `w`, at the call's line ([7.2](../COOKBOOK.md#72-rust-a-cargo-crate)).

## Returns

- `--capture return` shows integers, `bool` and floats exactly; an enum or `Option`, a struct, a string or any return in an optimized build is withheld, saying why. A line that stops again in the same call before it returns (a loop's next pass, each of `format!`'s blocks) gives each of those hits the call's return where the step out's watch proves that stop is in the same call, else withholds the earlier one ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
- `session step --out` shows with the stop what the function returned (`(return): u32 = 42`), or a withheld row saying why, as `--capture return` does (a panic that ended the call included); nothing where another stop interrupted the step first, or the program ended. `session get return` reads it again ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
