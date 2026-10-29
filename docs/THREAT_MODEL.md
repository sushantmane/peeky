# MVP trust and failure model

Peeky is a developer tool for trusted code. This document started with the Python implementation.
Each additional language adapter needs equivalent conformance before Peeky claims its support.

Peeky is not:

- a container boundary;
- a sandbox for hostile code;
- a credential vault;
- a production authorization system.

## Assets and boundaries

**Worker token**

- It authorizes all operations on one worker.
- The worker reads it from `DEBUG_TOKEN`, and a client from `PEEKY_WORKER_TOKEN`.
- Both names are removed from the environments of debuggers, debuggees, build commands, checkers,
  candidates, probe processes and the model's process.
- A session's own local worker gets only its own token, as `DEBUG_TOKEN`.
- Never print it in evidence.
- The worker waits at most 10 s for each read of a request, before and after the token. A
  connection that sends nothing doesn't hold a worker thread for longer.
- A client sends the token to whatever process holds the local port. Start SSH tunnels with
  `-o ExitOnForwardFailure=yes`, so that ssh exits when another process holds that port.

**Target memory and source**

- They can contain secrets.
- Debugger output and the model's context are intentional data flows.

**User code and assignment expressions**

- They run with the worker's OS permissions.
- Root-path checks only check the entry point.

**Model**

- It receives task data, and emits proposed structured decisions.
- The controller's validation governs what runs.
- `peeky-agent` applies the model's `finish` edit to a copy, and runs it with the checker. There is
  no sandbox: the model's code runs with your permissions.
- The source, the bug report, and the program's output and values reach the model. Treat them as
  untrusted. Text in them can steer the model (prompt injection) into writing code that then runs.
- Run `peeky-agent` only on code and inputs you trust, or inside a container or VM.

**Verifier**

- It is supplied and trusted, and runs candidate code.
- Its completion envelope detects an accidental or premature exit, not malicious native tampering.

**Provenance hashes** of sources, build outputs, toolchain binaries and adapter bundles

- They detect accidental change during a session, such as rebuilds, edits and toolchain upgrades.
  They don't detect an adversary running as the same user.
- Within one process, a file's digest is reused while its device, inode, size, mtime and ctime are
  unchanged. Its timestamps must also be more than 2 s old (`src/peeky/digest.py`).
- Any write, replacement or rename changes those.
- A C or C++ toolchain binary's digest is also kept in the state directory, so a new process
  reuses it by the same rules. So is each tool's answer to `--version` or `--help`, keyed by the
  tool's SHA-256. A tool that is a script is asked every time: the program it runs isn't hashed.
- To rehash every time, set `PEEKY_NO_DIGEST_CACHE=1`. This also turns off what the state
  directory keeps. Do this, for example, on filesystems whose timestamps are unreliable.
- Compiled builds also record every file the compiler read:
  - Rust fails closed on any file that isn't a declared source.
  - C and C++ hash the headers they read outside the workspace, and recheck them before every
    operation.

## Failure handling

| Failure | MVP behavior |
| --- | --- |
| Stale session, stop or scope | Reject the request. |
| A mutation's reply is lost | Invalidate or close the session. No automatic retry. |
| The adapter dies, or sends a malformed frame | Fail the transport, and close the owned execution. |
| No breakpoint or event within the budget | Return a timeout and the evidence. The controller closes when the experiment's budget ends. |
| Output flood | Output is bounded, and kept apart from control events. |
| The model fails, or the budget expires | Save the incomplete or failed evidence. Close the session. |
| A candidate exits zero before the checks | Reject it: the verifier's completion is absent. |
| The original or the check changes mid-run | Don't accept the candidate. |
| The remote source differs | Reject the experiment, and close. |

## Transport

- Loopback is the only listener configuration in the MVP.
- SSH is the remote transport.
- There is no public HTTP service, TLS termination service, Kubernetes RBAC integration, outbound
  relay, tenant isolation, or production write policy yet.

## Data and limits

- Use synthetic fixtures in CI. Avoid uploading real application reports.
- A process of the same user may still inspect the environment and process state. Stripping a
  token from a child's environment is defense in depth, not isolation from the same user.
- Reports are not tamper-proof attestations.
