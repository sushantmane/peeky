# Mixed-runtime inference debugging direction

**Status: a design direction and requirement, not implemented support.**

Some inference engines combine Python orchestration, C++ extensions and CUDA kernels. Debugging
these engines is a future acceptance target. Support for each host language alone does not
establish mixed-stack or GPU debugging.

- **Order:** this work comes after the five-language MVP: Python, Java, C, C++ and Rust. The
  [roadmap](ROADMAP.md) has the MVP's status.
- **CUDA:** [#61](https://github.com/sushantmane/peeky/issues/61) tracks CUDA debugging.
  [ADR 0007](adr/0007-cuda-gdb-dap.md) proposes its backend: cuda-gdb through DAP, qualified on
  real GPUs.
- **Evidence:** [ADR 0002](adr/0002-correlated-evidence.md) designs how debugger evidence is joined
  with code, logs and metrics.
- **Infrastructure:** this note provisions no GPU infrastructure or deployment.

## Coordinated investigations

One investigation can span several debugger sessions, one per backend.

- Keep one investigation ID across all its backend sessions.
- Each session keeps its own process, backend, stop generation, scope handles, capabilities and
  artifact identity.
- An explicit coordinator should:
  - send each observation and change to the backend that owns it
  - track which participants are running and which are stopped
  - clean up every owned session on failure
- Don't assume two debuggers can safely control the same process at once.
- Before supporting an inference engine, qualify one scheme for ownership and coexistence. Use a
  tiny fixture: Python calling a C++ extension.

**Across languages:**

- Record a call across a language boundary as an explicit evidence link. Don't fabricate one
  unified stack.
- Bind these artifacts to the executions that actually used them:
  - Python source revisions
  - the build IDs and debug symbols of extensions and shared libraries
  - eventually, device kernel artifacts
- The C worker's validation deliberately accepts only its prepared main executable. Before that
  changes, extension modules need an explicit, trusted artifact inventory and their own tests.

## Code, logs and metrics

- Correlate request and batch IDs with process and thread IDs, source and artifact identities,
  debugger operations and structured logs.
- Device work also needs context, stream and launch IDs, and explicit evidence of synchronization.
- Keep host and device clock domains separate. Timestamps close together don't prove causality.
- Record pause intervals. Stopping execution can distort latency, queue depth and utilization.

**Observations:**

- By default, an observation is a bounded summary: a tensor's shape, dtype, device and layout,
  plus a few selected values or statistics.
- Never do any of these implicitly:
  - copy a whole device tensor
  - synchronize all streams
  - evaluate arbitrary expressions
  - change GPU memory
- Report data that is unavailable.
- Report the cost and side effects of any observation that needs a synchronization or a copy.

## Incremental qualification

1. Verified repairs in each host language, checked independently, and evidence IDs shared across
   backends.
2. A reproducible bug in a Python/C++ extension, with:
   - explicit module ownership
   - evidence that crosses the boundary
   - fresh, independent checks
3. A separate CUDA-capable backend and a discovery contract, evaluated on dedicated hardware.
   [ADR 0007](adr/0007-cuda-gdb-dap.md#phase-0-qualification) has the proposed qualification.
   - Device breakpoints and stepping, kernel symbols, asynchronous completion, launch failures
     and cleanup need tests on real hardware.
   - Never advertise host LLDB support as CUDA kernel support.
4. A small inference fixture, with deterministic, independent numerical checks. The checks state
   their tolerances, seeds and inputs, and the device and toolchain identities.
   - Keep correctness and performance validation separate.
   - A performance claim needs a matched baseline that runs without pauses.

**CI:**

- Keep CPU CI independent and fast.
- A future GPU lane needs an explicit runner and resource budget.
- A GPU lane must never skip silently while Peeky reports GPU support. ADR 0007's
  [certification rule](adr/0007-cuda-gdb-dap.md#decision) says the same for CUDA.
