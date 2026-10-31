# Project binaries (ADR 0008, phase 1)

These scripts rebuild the value study's C and C++ projects with clang, as phase 1 asks. They
measure each binary: which units Peeky would judge, and why not, and the sizes its symbol bounds
limit. And they run phase 1's acceptance on each: Peeky on the project's own build, as an agent
would run it. The ADR's "Verified for phase 1" and "Acceptance results" come from them.

They run in the study's images (`experiments/value-study/bugs/<bug>/Dockerfile`, built by its
harness), which they don't change. Each run copies this checkout's committed `src` and
`tests/projects` into a fresh container, on 4 CPUs. Run one at a time: the machine may be shared.

## Acceptance

```
tests/projects/accept.sh peeky-study/jqlang__jq-2650:control jq
tests/projects/accept.sh peeky-study/valkey-io__valkey-1842:control valkey
tests/projects/accept.sh peeky-study/micropython__micropython-13569:control micropython
tests/projects/accept.sh peeky-study/fmtlib__fmt-3158:control fmt3158
tests/projects/accept.sh peeky-study/fmtlib__fmt-3272:control fmt3272
tests/projects/accept.sh peeky-study/nlohmann__json-4237:control json
```

`accept.py` builds the project (`build.sh`), measures it (`measure.py`), and runs its checks. A
line per check goes to stderr, and the report, as JSON, to stdout: each check, each probe's time,
the build times, the units judged, and the symbol sizes against Peeky's bounds. The exit status
is 1 if a check failed.

- Each value read must match the program's own print of it. jq prints its result and its
  error; valkey's `CLIENT LIST` names each client's user; for MicroPython, mpy-cross's output of
  the same script records the stack size its compiler computes.
- Each refusal must say why. Every project also checks the refusals all share: a file edited
  after the build (not for json, whose binary is refused whole), the project's own optimized
  build (refused, then read with `"optimized": true`), and GCC's and DWARF 4's builds.

## Builds and measurements

```
tests/projects/run.sh peeky-study/jqlang__jq-2650:control jq
tests/projects/run.sh peeky-study/valkey-io__valkey-1842:control valkey
```

`run.sh` builds the project (`build.sh`) and measures the binary (`measure.py`). A third argument
selects a variant of the build:

- `nomacro`: without `-fdebug-macro`;
- `libstdcxx`: C++ with the image's libstdc++ instead of libc++;
- `lines`: jq's grammar and lexer with bison's and flex's `#line`;
- `gcc`: GCC 13, which records its switches but no MD5s;
- `dwarf4`: `-gdwarf-4`;
- `optimized`: the project's own `-O` level (jq `-O2`, valkey and the C++ tests `-O3`, MicroPython
  `-Os`).

By default, jq's grammar and lexer are generated with `bison -l` and `flex -L`, as the C and C++
cookbook's recipe says.
