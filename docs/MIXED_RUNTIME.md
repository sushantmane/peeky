# Mixed-runtime inference debugging direction

Status: design requirement, not implemented support. Preserve the Python/Java/C/C++/Rust MVP ordering. Inference engines combining Python orchestration, C++ extensions and CUDA kernels are a future acceptance target; support for each host language alone does not establish mixed-stack or GPU debugging.

## Coordinated investigations

Keep one investigation ID across multiple backend sessions. Each session retains its own process, backend, stop generation, scope handles, capabilities and artifact identity. An explicit coordinator should route observations and interventions to the owning backend, track which participants are running or stopped, and clean up every owned session on failure. Do not assume two debuggers can safely control the same process simultaneously. First qualify a supported ownership/coexistence scheme with a tiny Python-to-C++ extension fixture before supporting an inference engine.

Cross-language call boundaries should be explicit evidence links, not a fabricated unified stack. Bind Python source revisions, extension/shared-library build IDs and debug symbols, and eventually device kernel artifacts to their actual executions. Current C worker validation intentionally permits only its prepared main executable; extension modules need an explicit trusted artifact inventory and separate tests before that restriction changes.

## Code, logs and metrics

Correlate request/batch IDs with process/thread IDs, source/artifact identities, debugger operations and structured logs. Device work additionally needs context, stream and launch identifiers and explicit synchronization evidence. Keep host/device clock domains distinct; timestamp proximity alone is not causality. Record pause intervals because stopping execution can distort latency, queue depth and utilization.

Default observations should be bounded summaries: tensor shape, dtype, device, layout and small selected values/statistics. Do not implicitly copy whole device tensors, synchronize all streams, evaluate arbitrary expressions or mutate GPU memory. Report unavailable data and the cost/side effects of any observation that requires synchronization or copying.

## Incremental qualification

1. Complete independently verified host-language repairs and shared evidence IDs.
2. Add a reproducible Python/C++ extension bug, explicit module ownership, cross-boundary evidence and fresh independent checks.
3. Evaluate a separate CUDA-capable backend and discovery contract on dedicated hardware. Device breakpoint/stepping, kernel symbols, asynchronous completion, launch failures and cleanup require real hardware tests. Host LLDB support must never be advertised as CUDA kernel support.
4. Add a small inference fixture and deterministic independent numerical checks with explicit tolerances, seeds, inputs and device/toolchain identities. Keep correctness and performance validation separate, with matched unpaused baselines for performance claims.

Keep CPU CI independent and fast. Any future GPU lane needs an explicit runner/resource budget and must not silently skip while reporting GPU support. No GPU infrastructure or deployment is provisioned by this design note.
