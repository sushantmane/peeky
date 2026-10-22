# Performance baseline

Correctness gates come before optimization. An investigation made faster but invalid is not useful.
So measure debugger operations apart from model decisions, compilation and verification.

## The worker benchmark

Run `python scripts/benchmark_worker.py --iterations 10` from an installed development checkout.

- It uses synthetic data, a temporary loopback worker, real debugpy, and a fresh adapter each
  iteration.
- It verifies that the changed value actually reaches the target's output.
- A failure stops the benchmark. It never enters the latency samples as a success.
- No model is called.

## Initial local measurement — 2026-10-14

macOS 27.0.1 arm64, Python 3.11.14, debugpy 1.8.21. Each variant ran ten sequential
fresh-adapter iterations, with no warmup discarded. The "after" variant adds nonblocking DAP pipe
writes, bound by deadlines. Raw samples and the environment are in
[before](benchmarks/2026-10-14-before.json) and [after](benchmarks/2026-10-14-after.json).

| HTTP operation / phase | Before median ms | After median ms | After sample p95 ms |
| --- | ---: | ---: | ---: |
| Launch/configure | 147.316 | 149.290 | 176.613 |
| Wait for breakpoint | 114.801 | 115.506 | 116.280 |
| Inspect | 2.490 | 2.429 | 2.580 |
| Mutate variable | 1.168 | 1.127 | 1.335 |
| Continue | 1.091 | 1.074 | 1.360 |
| Wait for termination | 719.564 | 717.409 | 735.325 |
| Close | 3.414 | 3.328 | 4.640 |
| Total, including report | 991.637 | 993.966 | 1009.487 |

How to read it:

- These small, unpaired local samples show the scale. They don't show a statistically significant
  change, or a production SLO.
- With ten samples, the nearest-rank p95 is the maximum.
- Hosted Linux measurements, and larger paired runs, are needed before setting regression
  thresholds.
- The results suggest looking at startup and termination before optimizing inspection, which
  takes under 3 ms.
- Never leave out confirmed termination or cleanup to improve a benchmark.

CI execution has its own budget, under five minutes. Timing cutoffs are failure bounds, not
measured speed. The proposed measurements of evidence-collection overhead are in
[ADR 0002](adr/0002-correlated-evidence.md).

## Java worker and refreshed Python baseline

After Java's HTTP integration, run the benchmark against a bundle that the setup script built:

```sh
python scripts/benchmark_worker.py --iterations 10 --java-bundle work/java-final
```

Replace the bundle path as needed, and leave out `--java-bundle` for Python. Copilot wrote the
first version of this benchmark extension, in an isolated folder. Review added the missing
interpreter, JDK and adapter metadata, and verified both modes with real runs.

Ten fresh runs each, on the same local host as above, with full integrity checks and real output
assertions:

| Phase | Python median ms | Java median ms | Java sample p95 ms |
| --- | ---: | ---: | ---: |
| Launch (Java includes fresh compilation) | 146.936 | 528.074 | 546.072 |
| Wait for stop after launch | 116.063 | 0.509 | 0.549 |
| Inspect | 2.582 | 21.743 | 22.357 |
| Mutate | 1.253 | 31.109 | 31.786 |
| Continue | 1.273 | 15.387 | 15.970 |
| Wait for termination | 718.776 | 2.832 | 3.076 |
| Close | 4.648 | 5.002 | 5.235 |
| Total, including report | 993.433 | 606.110 | 624.746 |

The raw [Python samples](benchmarks/2026-10-14-python-worker.json) and
[Java samples](benchmarks/2026-10-14-java-worker.json) record the environment and the adapter's
version.

- These are different debugger and runtime paths. They are not a benchmark of Python against Java
  as implementation languages.
- Small samples, warm OS caches and local transport limit how far the numbers generalize.
- The Java path also hashes its prepared build and the adapter's inventory before guarded
  operations. Profile that cost before optimizing it.
- [ADR 0003](adr/0003-measure-before-rewriting.md) records why a full rewrite in Rust is deferred
  until there is evidence.

## C++ inspection microbaseline, 2026-10-14

Ten sequential repetitions on one stopped C++ process, with Apple LLVM 21. Each starts with a fresh
inspection.

- Included: authenticated loopback HTTP, DAP, and the source, artifact and module guards.
- Excluded: launch and build, cleanup, and model calls.
- No warmup was discarded. The nearest-rank p95 is the maximum of ten samples.
- This small local sample is a baseline, not a service-level promise.

| Operation | Median ms | p95 ms |
| --- | ---: | ---: |
| Inspect | 62.63 | 74.91 |
| Class expansion | 123.11 | 124.07 |
| Array storage expansion | 122.95 | 125.62 |
| Array elements expansion | 128.70 | 131.99 |
| Complete sequence | 437.95 | 450.80 |

[Raw samples and tool versions](performance/cpp-2026-10-14.json).

The sequence inspects `pagination_bug.cpp` at `OBSERVE`, and expands `pagination`. Then it expands
`items`, and its raw storage member, which holds three values. Each expansion revalidates
provenance and resolves its ancestry. Profile those costs before caching or batching.
Optimizations must keep the guarantees of identity and invalidation.

## Where a probe's time goes, 2026-10-16

Measured with `python scripts/profile_probe.py <target> --at <line> --capture <names> --runs N`, on
macOS arm64 (Apple Silicon), with Python 3.13, debugpy 1.8.22 and Apple LLVM. The script splits wall
time into three parts:

- **Peeky CPU:** CPU used by Peeky itself, all of its threads;
- **Child CPU:** CPU of child processes, such as compilers, adapters and the debuggee. Descendants
  are counted once they are reaped;
- **The rest:** waiting.

Only Peeky's own CPU could shrink by rewriting Peeky in another language.

| Probe (examples) | Wall | Peeky CPU | Child CPU | Mostly |
| --- | ---: | ---: | ---: | --- |
| Python `pagination_bug.py:8` | ~1.0 s | 5 ms | ~50 ms | waiting: 0.15 s debugpy launch, 0.11 s to the first stop, 0.73 s for the program to exit |
| C `pagination_bug.c:24`, before | failed | | | the auto-detected build also linked `branch_bug.c` (duplicate `main`) |
| C, after | 0.65 s | 0.10 s | 0.40 s | clang, dsymutil, lldb-dap |
| C++ `pagination_bug.cpp`, before | 2.28 s | 1.72 s | 0.48 s | rehashing clang (125 MB) 32 times: 4 GB of SHA-256 |
| C++, after | 0.73 s | 0.10 s | 0.47 s | clang, dsymutil, lldb-dap |
| Rust `examples/rust:36`, before | 1.35 s | 0.50 s | 0.82 s | cargo build; rehashing cargo (32 MB) 30 times |
| Rust, after | 1.04 s | 0.16 s | 0.79 s | cargo build, dsymutil, dwarfdump, lldb-dap |
| Java, 8 hits on a loop line, before | 1.61 s | | | 120 ms pause per hit: each check walked and rehashed the 273-file adapter bundle, about 6 checks per hit |
| Java, after | 0.58 s | | | 9.9 ms pause per hit |

**Provenance checks no longer rehash unchanged toolchain binaries** (`src/peeky/digest.py`):

- Each debugger operation, such as inspect, continue or report, went from 58 ms to 0.8 ms for C++.
- For Rust, it went from 17 ms to 5 ms.
- This also explains the C++ inspect and expansion times of 62–129 ms in the 2026-10-14
  microbaseline above.
- Java reuses its verified adapter bundle while a stat fingerprint of the whole bundle tree is
  unchanged. That cut the time a paused JVM waits per hit by 12×.
- This matters for tests with timeouts, and for the 10 s pause budget of attached JVMs.

**Python probes:** about 0.5 s of the 0.73 s exit wait is inside debugpy.

- At exit, pydevd's CheckAliveThread and CommandThread each wait for the other, with a 0.5 s
  timeout. So the process exits half a second late.
- A `PYDEVD_DEBUG` log shows it: "The following pydb threads may not have finished correctly:
  pydevd.CommandThread".
- Nothing Peeky sends triggers it. No Peeky implementation can avoid it while it waits for the
  program's real exit, which it does to report the complete output.
- `--until hits` ends the probe right after the last hit, when the output isn't needed.

**Rewriting Peeky in Rust:**

- After these fixes, Peeky's own CPU is about 0.5% of a Python probe, and 10–15% of a compiled one.
- Starting the interpreter and importing add about 40 ms to each `peeky` command.
- A Rust implementation could save at most those amounts.
- Most of the remaining time belongs to compilers, debug adapters and the program. They take the
  same time whichever language calls them.
- This supports [ADR 0003](adr/0003-measure-before-rewriting.md).
