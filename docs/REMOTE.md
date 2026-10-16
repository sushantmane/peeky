# Remote workers over SSH (#8)

Peeky's worker runs next to the program, on the target host. The client (an agent, a harness or you) stays on your machine. SSH carries the traffic and provides encryption and host authentication. The worker binds to loopback only and also requires its bearer token.

```
your machine                                target host
peeky probe/session --worker URL  ──ssh -L──▶  python -m peeky.worker (127.0.0.1 only)
local checkout (same sources)                  target checkout, target interpreter/toolchain
```

## Separate-host procedure

On the **target host**, use the interpreter and toolchain that run the program:

```sh
export DEBUG_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
echo "$DEBUG_TOKEN"            # copy it over a trusted channel; never commit it
python3 -m peeky.worker --root /srv/app --port 8765 [--idle-timeout 300] \
    [--rust-config|--native-config|--java-config trusted.json]
```

On **your machine**, in a checkout with the same sources:

```sh
ssh -N -o ExitOnForwardFailure=yes -L 8765:127.0.0.1:8765 user@target &
export PEEKY_WORKER_TOKEN='<the token>'
peeky probe ./app --at lib/parse.py:42 --capture rows,total --worker http://127.0.0.1:8765
peeky session start ./app --at lib/parse.py:42 --worker http://127.0.0.1:8765
peeky session inspect; peeky session continue; peeky session close
```

Notes:
- **Interpreter and dependencies:** the worker's interpreter and its trusted target config choose the runtime. The client never sends executables or configuration.
- **Source identity:** the client sends the sha256 of the local program (and of the `--run` entry script). The worker refuses with `PROVENANCE_CHANGED` **before launching** if its copies differ, so a mismatched program is never executed or changed. As a second check, the client also compares the hashes the worker reports; any result that can't be confirmed is discarded (`results_discarded: true`, no rows and no output).
- **`ExitOnForwardFailure=yes`:** without it, ssh only warns when the local port is already taken, and the token would go to whatever process holds that port.
- **Verification runs locally.** `peeky verify` builds and checks candidates on your machine, so the local interpreter, toolchain and dependencies must match the target for its verdict to transfer. Run `verify` on the target host when they differ.
- **Secrets:**
  - The token is read only from `PEEKY_WORKER_TOKEN` (or `DEBUG_TOKEN` on the worker) and is never accepted as an argument, so it can't appear in `ps` output.
  - Session records holding it are private (`0600` inside a `0700` directory).
  - Probe results, reports and errors never contain it; a test checks this.
- **Closing:** `session close` closes the session but never stops a remote worker, which is shared infrastructure. Stop it on the target host.

## Disconnects, reconnects and cleanup

| Situation | Client result | What to do |
| --- | --- | --- |
| Local tunnel port closed (no ssh, or the worker is local and down) | `UNREACHABLE` (`retryable: true`, `session_closed: false`) | Restore the tunnel and retry. The worker still holds the session. A `session close` that fails this way keeps the record so it can be retried; `session close --forget` drops it if the worker is gone for good. |
| Tunnel up but the remote worker down (ssh accepts, then closes) | `TRANSPORT_LOST` | As below. |
| The session's own local worker has exited | `DEBUGGER_ERROR` (`session_closed: true`) | Start a new session. |
| Connection lost during a request | `TRANSPORT_LOST` (`retryable: false`, `session_closed: false`) | The outcome is unknown. Reconnect, then `inspect`/`wait`; never repeat a mutation blindly. |
| Client gone for longer than `--idle-timeout` | The worker closes the session and reaps the debuggee | Start a new session. |

## What is automated

See also the [recorded separate-host run](#recorded-separate-host-run-2026-10-16), which CI repeats on every change.


`tests/integration/test_remote_worker.py` runs the worker as a separate process and an SSH-like TCP relay as a third process. Its tests cover:
- probes through the forwarded port;
- the source-identity mismatch, where results are discarded;
- a dropped tunnel giving `UNREACHABLE` without losing the session;
- reconnecting on the same port and continuing the session;
- closing without stopping the worker;
- idle reaping, including the debuggee process;
- rejection of a non-loopback URL, a missing token, a wrong token (`UNAUTHORIZED`) and a closed port;
- the token never appearing in output.

## Recorded separate-host run (2026-10-16)

`tests/remote/test_ssh_e2e.py` runs the procedure above against a second host: a Linux container started from `scripts/remote_e2e/Dockerfile`, with its own kernel network namespace, filesystem, OS and Python. The container runs a key-only `sshd` that permits a single forward, to `127.0.0.1:8765`. The client uses a throwaway ed25519 key and ignores the user's ssh configuration. The worker binds to the container's loopback and reads its token from stdin, so the token never appears on a command line. Every step goes through a real `ssh -L` tunnel. The same test runs in CI (job "remote SSH e2e"). Run it locally with Docker (for example OrbStack):

```sh
PEEKY_E2E_DOCKER=1 PEEKY_E2E_RECORD=remote-run.json pytest tests/remote -v
```

| | Client | Worker host |
| --- | --- | --- |
| OS | macOS 27.0.1 arm64 | Linux 7.2.9 (OrbStack) aarch64, Debian 13 |
| Python / debugpy | 3.13.8 / 1.8.22 | 3.11.17 / 1.8.22 |
| SSH | OpenSSH 10.3p1 client | OpenSSH 10.0p2 sshd |

| Step | Command (client) | Exit | Seconds | Result |
| --- | --- | ---: | ---: | --- |
| Probe with an intervention | `peeky probe pagination_bug.py --at 8 --capture start,page --set start=0 --worker URL` | 0 | 1.55 | `start` 3 → 0; output `PAGE: [10, 20, 30]` |
| Session start | `peeky session start pagination_bug.py --at 8 --worker URL` | 0 | 0.45 | stopped at line 8 |
| Read a value | `peeky session get start` | 0 | 0.14 | `3` |
| Tunnel killed | `peeky session get start` | 2 | 0.005 | `UNREACHABLE`, `retryable: true`, `session_closed: false` |
| Tunnel reopened on the same port | `peeky session get start` | 0 | 0.10 | `3`: the worker kept the stopped session |
| Change a local | `peeky session set start=0` | 0 | 0.23 | 3 → 0 |
| Run to the end | `peeky session continue --to-exit` | 0 | 0.69 | terminated |
| Program output | `peeky session report` | 0 | 0.007 | `PAGE: [10, 20, 30]` |
| Close | `peeky session close` | 0 | 0.011 | closed; the shared worker kept running |
| Host's source differs | `peeky probe … --set start=0 --worker URL` | 1 | 0.06 | `PROVENANCE_CHANGED`, refused before launching; the host's changed program never ran |
| Worker stopped behind a live tunnel | `peeky probe … --worker URL` | 2 | 0.008 | `TRANSPORT_LOST`, `session_closed: false` |

Across five consecutive local runs, the module took 4.6–5.0 s, including starting the container and sshd. The token never appears in any command's output (asserted on every step).
