# Remote workers over SSH

Peeky's worker runs next to the program, on the target host. The client stays on your machine: an
agent, a harness or you. SSH carries the traffic between them, with encryption and host
authentication. The worker binds to loopback only, and it also requires its bearer token.

```
your machine                                target host
peeky probe/session --worker URL  ──ssh -L──▶  python -m peeky.worker (127.0.0.1 only)
local checkout (same sources)                  target checkout, target interpreter/toolchain
```

The [COOKBOOK, section 9](COOKBOOK.md#9-a-program-on-another-host), has a short version of this
page. The error codes are in [ERRORS.md](ERRORS.md).

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

Results look as they do without a worker. For example, a probe through a worker:

```
$ peeky probe pagination_bug.py --at 8 --capture start,page --set start=0 --worker http://127.0.0.1:18765
hit  phase      line  function  selector  type  value
1    observed   8     get_page  start     int   3
1    observed   8     get_page  page      int   1
1    after-set  8     get_page  start     int   0
after-set: 1 row equal to the hit's observed value left out (`--json` has every row)
set start: 3 -> 0
status: completed  hits: 1  seconds: 1.072  max pause: 14.1 ms
output:
  PAGE: [10, 20, 30]
evidence: saved as run 1; query it with `peeky queries run --param run=1` or `peeky sql` (see `peeky sql --schema`)
```

A session started with `--worker` says `remote` in its state: `session default (python, remote)`.

### What runs where

- The worker's interpreter, and its trusted target config, choose the runtime.
- The client never sends executables or configuration.
- **Verification runs locally.** `peeky verify` builds and checks candidates on your machine. So
  the local interpreter, toolchain and dependencies must match the target's for the verdict to
  carry over. When they differ, run `verify` on the target host.

### Source identity

The client sends the sha256 of the local program, and of the `--run` entry script. If the worker's
copies differ, it refuses with `PROVENANCE_CHANGED` **before launching**. So a mismatched program
is never run or changed:

```
$ peeky probe pagination_bug.py --at 8 --capture start --set start=0 --worker http://127.0.0.1:18765
error [PROVENANCE_CHANGED]: Worker source provenance differs for the program pagination_bug.py: refused before launching. Sync sources before debugging remotely
hint: Sources, build outputs or tools changed; start a new session on a fresh build.
```

As a second check, the client compares the hashes the worker reports. A result it can't confirm
is discarded: `results_discarded: true`, with no rows and no output.

### Why `ExitOnForwardFailure=yes`

Without it, ssh only warns when the local port is already taken. The token would then go to
whatever process holds that port.

### Keep the token secret

- The client reads the token only from `PEEKY_WORKER_TOKEN`, and the worker only from
  `DEBUG_TOKEN`. Neither accepts it as an argument, so it can't appear in `ps` output.
- Session records that hold it are private: `0600`, inside a `0700` directory.
- Probe results, reports and errors never contain it. A test checks this.

### Closing

`session close` closes the session, but never stops a remote worker, which is shared
infrastructure. Stop the worker on the target host.

## Disconnects, reconnects and cleanup

| Situation | Client result | What to do |
| --- | --- | --- |
| The local tunnel port is closed: no ssh, or a local worker is down | `UNREACHABLE` (`retryable: true`, `session_closed: false`) | Restore the tunnel, and retry. The worker still holds the session. |
| The tunnel is up, but the remote worker is down: ssh accepts, then closes | `TRANSPORT_LOST` | As for a lost connection, below. |
| The session's own local worker has exited | `DEBUGGER_ERROR` (`session_closed: true`) | Start a new session. |
| The connection is lost during a request | `TRANSPORT_LOST` (`retryable: false`, `session_closed: false`) | The outcome is unknown. Reconnect, then `inspect` or `wait`. Never repeat a change blindly. |
| The client is gone for longer than `--idle-timeout` | The worker closes the session, and reaps the debuggee. | Start a new session. |

For example, with the worker stopped:

```
$ peeky session get start
peeky: [UNREACHABLE] Worker at http://127.0.0.1:18765 is unreachable ([Errno 61] Connection refused); the request was not sent. Check the worker process or SSH tunnel, then retry
  hint: No request reached the worker. Restore the worker or SSH tunnel and retry; the worker keeps a session until its idle timeout. If the worker is gone for good, use `peeky session close --forget`.
```

- A `session close` that fails with `UNREACHABLE` keeps the session's record, so you can retry it.
- If the worker is gone for good, `session close --forget` drops the record:
  `forgot default (the worker was not contacted)`.

## What is automated

`tests/integration/test_remote_worker.py` runs the worker as a separate process, and an SSH-like
TCP relay as a third. Its tests cover:

- probes through the forwarded port;
- a source-identity mismatch, where results are discarded;
- a dropped tunnel giving `UNREACHABLE`, without losing the session;
- reconnecting on the same port, and continuing the session;
- closing without stopping the worker;
- idle reaping, including the debuggee process;
- refusing a non-loopback URL, a missing token, a wrong token (`UNAUTHORIZED`) and a closed port;
- the token never appearing in output.

The [recorded separate-host run](#recorded-separate-host-run-2026-10-22) is repeated by CI on every
change.

## Recorded separate-host run (2026-10-22)

`tests/remote/test_ssh_e2e.py` runs the procedure above against a second host. The host is a Linux
container started from `scripts/remote_e2e/Dockerfile`. It has its own kernel network namespace,
filesystem, OS and Python.

- The container runs a key-only `sshd`, which permits a single forward, to `127.0.0.1:8765`.
- The client uses a throwaway ed25519 key, and ignores the user's ssh configuration.
- The worker binds to the container's loopback, and reads its token from stdin. So the token never
  appears on a command line.
- Every step goes through a real `ssh -L` tunnel.
- The same test runs in CI, as the job "remote SSH e2e".

To run it locally, you need Docker, for example OrbStack:

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
| Probe with a change | `peeky probe pagination_bug.py --at 8 --capture start,page --set start=0 --worker URL` | 0 | 1.82 | `start` 3 → 0; output `PAGE: [10, 20, 30]` |
| Session start | `peeky session start pagination_bug.py --at 8 --worker URL` | 0 | 0.44 | stopped at line 8 |
| Read a value | `peeky session get start` | 0 | 0.14 | `3` |
| Tunnel killed | `peeky session get start` | 2 | 0.001 | `UNREACHABLE`, `retryable: true`, `session_closed: false` |
| Tunnel reopened on the same port | `peeky session get start` | 0 | 0.09 | `3`: the worker kept the stopped session |
| Change a local | `peeky session set start=0` | 0 | 0.24 | 3 → 0 |
| Run to the end | `peeky session continue --to-exit` | 0 | 0.71 | terminated |
| Program output | `peeky session report` | 0 | 0.005 | `PAGE: [10, 20, 30]` |
| Close | `peeky session close` | 0 | 0.013 | closed; the shared worker kept running |
| The host's source differs | `peeky probe … --set start=0 --worker URL` | 2 | 0.05 | `PROVENANCE_CHANGED`, refused before launching; the host's changed program never ran |
| Worker stopped behind a live tunnel | `peeky probe … --worker URL` | 2 | 0.002 | `TRANSPORT_LOST`, `session_closed: false` |

- Every step gave the same result as in the first recorded run, on 2026-10-16. The one difference
  is the exit status of the refused probe: 2 today, 1 then. A probe refused before launch now
  exits 2.
- The token never appears in any command's output. Every step asserts this.
- This run's module took 9.6 s, including starting the container and sshd, with the image already
  built.
- On 2026-10-16, five consecutive local runs took 4.6–5.0 s. On GitHub's `ubuntu-latest` runner,
  with an x86_64 client and container, the module took 21.7 s including the image build.
