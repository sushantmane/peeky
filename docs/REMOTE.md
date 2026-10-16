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
ssh -N -L 8765:127.0.0.1:8765 user@target &
export PEEKY_WORKER_TOKEN='<the token>'
peeky probe ./app --at lib/parse.py:42 --capture rows,total --worker http://127.0.0.1:8765
peeky session start ./app --at lib/parse.py:42 --worker http://127.0.0.1:8765
peeky session inspect; peeky session continue; peeky session close
```

Notes:
- **Interpreter and dependencies:** the worker's interpreter and its trusted target config choose the runtime. The client never sends executables or configuration.
- **Source identity:** before any value is shown, the client compares the sha256 of the local program (and of the `--run` entry script) with the worker's copy.
  - On a mismatch, a session is closed at once and `PROVENANCE_CHANGED` is reported.
  - A probe's results are discarded: `results_discarded: true`, no rows.
- **Verification runs locally.** `peeky verify` builds and checks candidates on your machine, so the local interpreter, toolchain and dependencies must match the target for its verdict to transfer. Run `verify` on the target host when they differ.
- **Secrets:**
  - The token is read only from `PEEKY_WORKER_TOKEN` (or `DEBUG_TOKEN` on the worker) and is never accepted as an argument, so it can't appear in `ps` output.
  - Session records holding it are private (`0600` inside a `0700` directory).
  - Probe results, reports and errors never contain it; a test checks this.
- **Closing:** `session close` closes the session but never stops a remote worker, which is shared infrastructure. Stop it on the target host.

## Disconnects, reconnects and cleanup

| Situation | Client result | What to do |
| --- | --- | --- |
| Tunnel or worker down when a request starts | `UNREACHABLE` (`retryable: true`, `session_closed: false`) | Restore the tunnel and retry. The worker still holds the session. |
| Connection lost during a request | `TRANSPORT_LOST` (`retryable: false`, `session_closed: false`) | The outcome is unknown. Reconnect, then `inspect`/`wait`; never repeat a mutation blindly. |
| Client gone for longer than `--idle-timeout` | The worker closes the session and reaps the debuggee | Start a new session. |

## What is automated

`tests/integration/test_remote_worker.py` runs the worker as a separate process and an SSH-like TCP relay as a third process. Its tests cover:
- probes through the forwarded port;
- the source-identity mismatch, where results are discarded;
- a dropped tunnel giving `UNREACHABLE` without losing the session;
- reconnecting on the same port and continuing the session;
- closing without stopping the worker;
- idle reaping, including the debuggee process;
- rejection of a non-loopback URL, a missing token, a wrong token (`UNAUTHORIZED`) and a closed port;
- the token never appearing in output.

## Recorded separate-host run

This still needs a second machine. Record the following and add it here:
- the host OS and interpreter on both sides;
- the commands above and their outputs: one probe, one session, a source mismatch, and a tunnel drop and recovery;
- the timings.
