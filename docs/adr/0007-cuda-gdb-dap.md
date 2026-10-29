# ADR 0007: NVIDIA CUDA debugging: cuda-gdb through DAP, qualified on real GPUs

Status: proposed. Phase 0 of #61; phase 1 starts only after the qualification below passes on real hardware.

## Context

#61 asks for CUDA kernel debugging, Compute Sanitizer, Nsight profiling and ML-workload support through Peeky's existing interfaces (CLI, SDK, HTTP worker, probes, sessions and SQLite evidence). It requires that GPU support is never certified by CPU-only CI and is never silently skipped. Peeky drives every debugger through the Debug Adapter Protocol, so the first question is whether cuda-gdb can be driven the same way.

Verified so far, from NVIDIA's and GDB's documentation (2026-10-17):
- **cuda-gdb's GDB base.** cuda-gdb follows upstream GDB: CUDA 12.9 moved to GDB 14.2, and 13.2 moved to GDB 16.3 ([CUDA-GDB release notes](https://docs.nvidia.com/cuda/cuda-gdb/index.html)).
- **Upstream DAP.** GDB has a built-in DAP interpreter, `gdb -i=dap`, since GDB 14. It is implemented in Python. cuda-gdb ships with Python support, and falls back to a build without Python when no libpython is found.
- **DAP and CUDA.** NVIDIA's documentation does not mention DAP; it documents GDB/MI (`cuda-gdb -i=mi`).
- **GDB's DAP extensions** ([GDB manual, Debugger Adapter Protocol](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Debugger-Adapter-Protocol.html)):
  - `evaluate` with context `repl` runs a CLI command, which can switch CUDA focus (`cuda block (2,0,0) thread (255,0,0)`).
  - `attach` takes `pid`, `target` (for example cuda-gdbserver) or `coreFile` (for example GPU core dumps).
- **Debug builds and containers.** Device-side debugging needs `nvcc -g -G`. A container needs `--cap-add=SYS_PTRACE`, plus GPU access.
- **Compute Sanitizer** writes XML (`--xml yes --save FILE`). `--error-exitcode N` marks runs where it found errors, and `--print-limit` bounds the output. racecheck does no memory-access checking.
- **Nsight Systems** exports reports to SQLite (`nsys export -t sqlite`), as well as JSON lines and Parquet.

Verified by experiment on upstream GDB without a GPU (2026-10-17; GDB 14.2 on Alpine 3.20, 15.1 on Ubuntu 24.04, 16.3 on Debian trixie; arm64 Linux in Docker):
- **When the program starts.** Each version starts it at a different request:
  - 14.2 at `configurationDone`; `launch` only prepares;
  - 15.1 at `launch`; `configurationDone` does nothing;
  - 16.3 at `configurationDone`, which it refuses before `launch` (`launch or attach not specified`).
- **Request order.** So VS Code's order (`launch`, then breakpoints) lets 15.1 run to exit before any breakpoint exists, and configuring before `launch` never starts 14.2 and is refused by 16.3. One order works on all three: `initialize`, the `initialized` event (GDB sends it right after `initialize`), `setBreakpoints`, `launch`, then `configurationDone`. 15.1 may refuse that `configurationDone` when the program is already running, which is harmless. It passed 15 of 15 runs across the three versions.
- **Pending breakpoints.** A breakpoint set before the program loads may report `verified: false` and still stops.
- **`repl` replies.** A failing `repl` command is a failed request carrying GDB's message (`Undefined command: …`). `print` answers `$N = value` in `result`. GDB's `evaluate` selects the given frame before a `repl` command, so focus commands are sent without a `frameId`, which could restore the old focus.
- **Program output.** The program's stdout arrives as DAP `output` events.
- **Kept tested.** `tests/gdb` runs this sequence through `qualify.py` against GDB 15.1 in the CI image (lane `gdb-dap`); 14.2 and 16.3 were run by hand with the same test.

Not yet verified, and the reason for phase 0:
- whether NVIDIA's cuda-gdb builds include a working `-i=dap` (it needs cuda-gdb's Python support) and behave like upstream GDB above;
- whether DAP stack frames and variables follow CUDA focus after a `repl` focus change;
- whether kernel breakpoints set before the CUDA module loads resolve;
- what cuda-gdb answers for a block that does not exist.

## Decision

1. **Transport.** Drive cuda-gdb (12.9 or later) with `cuda-gdb -i=dap` through Peeky's existing DAP client and worker. A GDB/MI client is built only if qualification shows DAP cannot serve the acceptance fixture.
2. **CUDA focus.** Focus is changed only through Peeky-owned `repl` commands, built from validated integers: `cuda device|sm|warp|lane|kernel|grid|block|thread`, plus read-only `info cuda …`. Users and agents never send `repl` text, so no `print`, `call` or `set var` is possible this way. Every selector reads at a stated focus, and a focus that does not exist is an explicit error, never the nearest thread.
3. **Values.** Values come from DAP `variables`, with the same bounds as today. Device memory reads (shared, global, constant) need explicit, bounded reads with a stated address space; they come in a later phase, after their DAP shape is qualified.
4. **Diagnostics are separate runs, not debugger sessions.**
   - Compute Sanitizer runs with `--xml`, `--error-exitcode` and `--print-limit`.
   - Nsight Systems and Nsight Compute run with their own exports.
   - Each becomes a saved run in the evidence store, with its tool version, command, artifact identity and parsed summary. The original XML or SQLite is kept as an artifact.
   - Profiling is never mixed with breakpoint debugging.
5. **Where it runs.** On a Linux GPU host, through the existing SSH remote worker (REMOTE.md), or in a container with `--gpus` and `SYS_PTRACE`. The local Mac has no CUDA, and cuda-gdb dropped its macOS host client in 12.5.
6. **Certification.**
   - A GPU test lane runs only on a real NVIDIA GPU and states its GPU, driver and toolkit.
   - Without one, GPU tests report "not qualified on this host", never pass and never go missing silently.
   - CPU-only CI does not claim CUDA support.

## Phase 0 qualification

`scripts/cuda/qualify.py` runs on the GPU host and writes a JSON report with every raw debugger reply.
- **Records:** the platform, `nvidia-smi` (GPU, driver, compute capability), nvcc, cuda-gdb, compute-sanitizer, nsys and ncu versions, and cuda-gdb's GDB base (`gdb.VERSION` through its Python).
- **Builds:**
  - `examples/cuda/scale_bug.cu` with `-g -G`: a launch drops the tail, so it prints `SUM:1536` instead of 2000;
  - `examples/cuda/oob_bug.cu` with `-lineinfo`: thread n writes one past its allocation.
- **Checks:** each item below is a pass/fail check.

  Through DAP, in the order above:
  - cuda-gdb has Python, which its DAP needs;
  - a kernel breakpoint is accepted and stops in `scale`;
  - focus moves to block 2, thread 255, and a fresh DAP frame there evaluates `blockIdx.x * blockDim.x + threadIdx.x` to 767. Builtins are right wherever that thread's warp stopped, while the local `i` may not be computed yet;
  - DAP's `i` there matches the CLI's `print i`;
  - after asking for block 3, which does not exist, the focus is still block 2, thread 255;
  - the program continues to exit and its output is seen.

  Through Compute Sanitizer:
  - memcheck flags the error (exit code 86) and writes its XML.

Phase 1 (launch, kernel breakpoints, focus, inspection, continuation through `peeky probe` and sessions) begins only after this report passes on at least one real GPU, and its acceptance test reproduces a known kernel bug end to end, as #61 requires.

## Consequences

- No new protocol stack if DAP qualifies: probes, sessions, the evidence store, remote workers and error codes carry over, and CUDA adds a focus dimension to stops and selectors.
- Peeky's safety rules hold: no expression evaluation, bounded reads, explicit stop identity and focus, refusal over guessing.
- GPU work cannot be verified on the maintainer's Mac or in the current CI. It needs a GPU host (a cloud VM, a lab machine or a hosted GPU runner), which is not chosen yet.
