# Your first Peeky question

Give Peeky the line to stop at and the command that runs the failing test. It runs that command
under the debugger, stops at the line and prints the values there. You need no config, and no
other page, for this.

```sh
peeky probe . --at FILE:LINE --capture a,b.c --test -- <the test command>
```

Run it from the project's root, where the test command runs:

| Project | After `--test --` |
| --- | --- |
| Python | `pytest -q tests/test_x.py::test_y`, or `python script.py ARGS` |
| Java | `mvn test -Dtest='FooTest#bar'`, `./gradlew test --tests pkg.FooTest`, or `java -cp … Main` |
| Rust | `cargo test -p crate --test it name -- --exact`, or `cargo run --bin app -- ARGS` |
| C, C++ | `./build/prog ARGS`, a binary built with clang `-O0 -g -gdwarf-5 -grecord-command-line` |

Three rules:

1. A breakpoint stops **before** its line runs. To see what a line assigns, stop on the next.
2. Read every `warning:` line.
3. A `note`, `no value yet:` or `unavailable: true` is not a value, nor is an error's text.

If Peeky refuses, its message says what to do next. `peeky describe . --at FILE` lists the lines
it can stop at. [AGENT_GUIDE.md](AGENT_GUIDE.md) has the rest: every hit, `--set`, sessions,
`verify`.
