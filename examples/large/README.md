# Larger programs

Two programs a little bigger than the evaluations' cases, from a build-cost measurement. Each
reads a shop's catalog, orders and stock, and prints a report.

- `cpp-shop`: 8 C++ files using the STL (`map`, `set`, `unordered_map`, `optional`, streams).
  Its debug information is 19-33 MB as llvm-dwarfdump writes it.
- `rust-shop`: a 607-line crate using `HashMap`, `BTreeMap`, `BTreeSet`, `VecDeque` and
  `BinaryHeap`. Its own unit's debug information is 33 MB as text.

`tests/cpp/test_cpp_scale.py` and `tests/rust/test_rust_scale.py` probe them. See
[Reading a build's symbols](../../docs/NATIVE_INTERNALS.md#reading-a-builds-symbols).
