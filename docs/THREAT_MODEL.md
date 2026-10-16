# MVP trust and failure model

Peeky is a developer tool for trusted code. This document describes the current Python implementation; additional language adapters need equivalent conformance before claiming support. It is not a container boundary, hostile-code sandbox, credential vault, or production authorization system.

## Assets and boundaries

- Worker token: authorizes all operations on one worker; remove it from debugger/verification child environments and never print it in evidence.
- Target memory and source: can contain secrets; debugger outputs and model context are intentional data flows.
- User code and assignment expressions: execute with the worker's OS permissions. Root-path checks are entrypoint checks only.
- Model: receives task data and emits proposed structured decisions; controller validation governs execution.
- Verifier: supplied and trusted, runs candidate code; completion envelope detects accidental/premature exit, not malicious native tampering.
- Provenance hashes (sources, build outputs, toolchain binaries, adapter bundles): detect accidental change during a session (rebuilds, edits, toolchain upgrades), not a same-user adversary. Within one process a file's digest is reused while its device, inode, size, mtime and ctime are unchanged and its timestamps are more than 2 s old (`src/peeky/digest.py`). Any write, replacement or rename changes those. Set `PEEKY_NO_DIGEST_CACHE=1` to rehash every time, for example on filesystems whose timestamps are unreliable.

## Failure handling

| Failure | MVP behavior |
| --- | --- |
| Stale session/stop/scope | Reject request |
| Mutation reply lost | Invalidate/close session; no automatic retry |
| Adapter dies or malformed frame | Fail transport and close owned execution |
| No breakpoint/event in budget | Return timeout/evidence; controller closes when experiment budget ends |
| Output flood | Bounded output separate from control events |
| Model fails or budget expires | Save incomplete/failed evidence; close session |
| Candidate exits zero before checks | Reject absent verifier completion |
| Original/check changes mid-run | Do not accept candidate |
| Remote source differs | Reject experiment and close |

Loopback is the only listener configuration in the MVP. SSH is the remote transport. There is no public HTTP service, TLS termination service, Kubernetes RBAC integration, outbound relay, tenant isolation, or production write policy yet.

Use synthetic fixtures in CI and avoid uploading real application reports. A same-user process may still inspect environment/process state; stripping a token from child env is defense in depth, not same-user isolation. Reports are not tamper-proof attestations.
