# Performance baseline

Correctness gates precede optimization. Measure debugger operations separately from model decisions, compilation, and verification; an accelerated but invalid investigation is not useful.

Run `python scripts/benchmark_worker.py --iterations 10` from an installed development checkout. It uses synthetic data, a temporary loopback worker, real debugpy, and a fresh adapter each iteration. It verifies the changed value actually reaches target output. Failures stop the benchmark rather than entering latency samples as successes. No model calls occur.

## Initial local measurement — 2026-10-14

macOS 27.0.1 arm64, Python 3.11.14, debugpy 1.8.21. Ten sequential fresh-adapter iterations per variant, no discarded warmup. The after variant adds nonblocking deadline-bound DAP pipe writes. Raw samples and environment are in [before](benchmarks/2026-10-14-before.json) and [after](benchmarks/2026-10-14-after.json).

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

These small, unpaired local samples establish scale; they do not establish a statistically significant change or a production SLO. With ten samples, nearest-rank p95 is the maximum. Hosted Linux measurements and larger paired runs are needed before setting regression thresholds. Current results suggest startup/termination deserve investigation before optimizing sub-3ms inspection. Never omit confirmed termination or cleanup to improve a benchmark.

CI execution remains separately budgeted below five minutes. Timing cutoffs are failure bounds, not measured speed. Proposed evidence-collection overhead measurements are in [ADR 0002](adr/0002-correlated-evidence.md).
