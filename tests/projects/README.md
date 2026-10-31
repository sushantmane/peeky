# Project binaries (ADR 0008, phase 1)

These scripts rebuild the value study's C and C++ projects with clang, as phase 1 asks, and measure
each binary: which units Peeky would judge, and why not, and the sizes its symbol bounds limit.
The ADR's "Verified for phase 1" table comes from them.

They run in the study's images (`experiments/value-study/bugs/<bug>/Dockerfile`, built by its
harness), which they don't change:

```
tests/projects/run.sh peeky-study/jqlang__jq-2650:control jq
tests/projects/run.sh peeky-study/valkey-io__valkey-1842:control valkey
tests/projects/run.sh peeky-study/micropython__micropython-13569:control micropython
tests/projects/run.sh peeky-study/fmtlib__fmt-3158:control fmt3158
tests/projects/run.sh peeky-study/fmtlib__fmt-3272:control fmt3272
tests/projects/run.sh peeky-study/nlohmann__json-4237:control json
```

`run.sh` copies this checkout's committed `src` and `tests/projects` into the image. A second
argument after the project selects a variant: `nomacro` builds without `-fdebug-macro`, and
`libstdcxx` builds C++ with the image's libstdc++ instead of libc++.
