# Project binaries (ADR 0008, phase 1)

These scripts rebuild the value study's C and C++ projects with clang, as phase 1 asks. They
measure each binary: which units Peeky would judge, and why not, and the sizes its symbol bounds
limit. And they run phase 1's acceptance on each: Peeky on the project's own build, as an agent
would run it. The ADR's "Verified for phase 1" and "Acceptance results" come from them.

They run in the study's images (`experiments/value-study/bugs/<bug>/Dockerfile`, built by its
harness), which they don't change. Each adds libc++ 18, pinned, in an acceptance image
(`Dockerfile`, built with a network and cached). Each run copies this checkout's committed `src`
and `tests/projects` into a fresh container of it, on 4 CPUs, with no network. Fixtures, logs and
Peeky's state go to one scratch directory, removed at the end. Run one at a time: the machine may
be shared.

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

- Each project checks how many units Peeky judges, and that it reads each one's text but those
  it names with their reason: fmt-3272's `util.cc` and `os.cc`, behind fmt's `FMT_HAS_INCLUDE`.
- Each value read is checked against an oracle outside Peeky:
  - the program's output: jq's result, its error message and count, and `jv_print.c`'s number;
    valkey's `CLIENT LIST` (its client's id and `(superuser)`); gtest's print of its filter;
  - the source the build generated: jq's rule number, from `src/parser.c`'s `case` for
    `Term: FIELD`, and that rule's length, 1;
  - another tool's output: mpy-cross's of the same script, read by mpy-tool, records the stack
    size MicroPython's compiler computes;
  - the program's arguments: fmt's `argc`, its path and the filter;
  - the program's own copy: valkey's `original`, set from `c->user` on the line before.
- Each refusal must say why. Every project checks the optimized, GCC and DWARF 4 builds are
  refused. Where Peeky judges a unit, it checks a file edited after the build is refused, and the
  optimized build is read with `"optimized": true`. json's binary is refused whole, so it checks
  neither: no file of it is checked, and no unit opts in.

## Prebuilt images

`build.sh` needs nothing else of this checkout, nor a network, so `Dockerfile` can bake a
project's Peeky-ready build into a study image (clang 18, `-O0 -g -gdwarf-5
-grecord-command-line -fdebug-macro`, a build ID; libc++ 18, pinned, for C++):

```
git archive HEAD:tests/projects | docker build --build-arg BASE=peeky-study/jqlang__jq-2650:control \
  --build-arg PROJECT=jq -t peeky-study/jqlang__jq-2650:peeky-build -
```

It prints the binary's path, relative to the project (`/testbed`, or `$TESTBED`). A failed
build exits 1 and prints its log's last lines; one of fmt or json without libc++ says so. Its
log is in a scratch directory it removes. `accept.py` rebuilds anyway, for its variants.

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
