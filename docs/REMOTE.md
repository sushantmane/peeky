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
    [--rust-config|--native-config|--java-config|--java-attach-config trusted.json]
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
after-set: 1 number, flag, null or text row equal to the observed value left out; `--json` has all
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
- A location in a compared source names your copy of it. Any other path is the worker host's, and
  is marked `worker:` ([Paths in results](#paths-in-results)).
- The client saves remote probe evidence, and its `runs.worker` column names the worker's URL. The
  worker runs stateless probes with `--no-save`, so the worker host keeps none.
- **Verification runs locally.** `peeky verify` builds and checks candidates on your machine. So
  the local interpreter, toolchain and dependencies must match the target's for the verdict to
  carry over. When they differ, run `verify` on the target host.

### Paths in results

A location, frame or breakpoint names your copy of a file only when that file is one of the
sources compared ([Source identity](#source-identity)). Both hosts check this:

- **The worker** names the file only if the path is the compared file itself, under its root, with
  no `..` and through no link.
- **Your client** shows it only if it hashed a file of that name, and that file is still a regular
  file under your root, reached through no link.

Any other path is the worker host's, and is marked `worker:`. In the recorded Rust session, frame 0
was `src/main.rs` in the client's checkout. Its callers, in Rust's standard library, came back as:

```
worker:/rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/core/src/ops/function.rs
worker:/rustc/ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96/library/std/src/sys/backtrace.rs
```

These paths stay the worker host's:

- a file never compared: in a virtual environment under the root, or a generated one;
- a path with `..` in it, or reached through a link;
- installed files, such as the standard library's;
- the build's own files, and a candidate's edited file, since its lines are the candidate's;
- a compared file that your checkout no longer has.

In JSON, your files' paths are absolute. A `worker:` path may not exist on your machine.

### Java on the worker host

The worker's own trusted config decides what runs. Its paths are relative to `--root`, and its
`bundle` is relative to the config file.

| Target | Worker flag | The config names |
| --- | --- | --- |
| A JVM that Peeky launches | `--java-config` | `bundle`, `sources`, `main_class` |
| A JVM already waiting for a debugger | `--java-attach-config` | `bundle`, `port`, `sources`, `class_roots` |

- The host needs a JDK, and an adapter bundle built there with `scripts/setup_java_adapter.py`.
- On your machine, pass your own `--config` of the same kind. With `--worker`, the client uses it
  only to tell a launched JVM from an attached one.
- Every file in `sources` must match yours: see [Source identity](#source-identity).

A probe through the worker:

```
$ peeky probe . --config launch.json --at demo/PaginationBug.java:13 --capture start,pageSize --set start=0 --worker http://127.0.0.1:56757
hit  phase      line  function                      selector  type  value
1    observed   13    PaginationBug.main(String[])  start     int   3  [settable]
1    observed   13    PaginationBug.main(String[])  pageSize  int   3  [settable]
1    after-set  13    PaginationBug.main(String[])  start     int   0  [settable]
after-set: 1 number, flag, null or text row equal to the observed value left out; `--json` has all
set start: 3 -> 0
status: completed  hits: 1  seconds: 0.706  max pause: 117.5 ms
output:
  PAGE:[10, 20, 30]
```

A session works the same way:

```
$ peeky session start . --config launch.json --at demo/PaginationBug.java:13 --worker http://127.0.0.1:56757
session default (java, remote): state: stopped at demo/PaginationBug.java:13 in PaginationBug.main(String[]), hit 1 (paused 0.03 s)
```

With an attached JVM, a probe stops at its hits, then detaches. The JVM runs on, and Peeky never
kills it ([JAVA_ATTACH.md](JAVA_ATTACH.md)).

### C, C++ and Rust on the worker host

The worker builds and runs the program on its host, from its own trusted config. That config takes
the same keys as `--config`. Its paths are relative to `--root`.

| Target | Worker flag | The config names |
| --- | --- | --- |
| C or C++ | `--native-config` | `sources`, `headers`, `compiler`, `adapter`, `dwarfdump`; `"language": "cpp"` for C++ |
| A Cargo crate | `--rust-config` | `sources`, `tests`, and every tool: `cargo`, `rustc`, `linker`, `adapter`, `dwarfdump` |

- The host needs the toolchain: clang, lldb-dap and llvm-dwarfdump, and for Rust, cargo and rustc
  ([WORKER.md](WORKER.md#start-a-c-or-c-worker)).
- On your machine, no `--config` is needed. The worker says which files to hash.
- The files compared are listed in [Source identity](#source-identity): a C header or a Rust module
  that differs is refused by name.

A probe of a C program through the worker, run in the checkout:

```
$ peeky probe . --at main.c:7 --capture start --set start=0 --worker http://127.0.0.1:56227 --no-save
hit  phase      line  function  selector  type  value
1    observed   7     main      start     int   3  [settable]
1    after-set  7     main      start     int   0  [settable]
set start: 3 -> 0
status: completed  hits: 1  seconds: 0.736  max pause: 33.5 ms
output so far (the program ended, but its output may still have been arriving):
  START:0
the debugger sent more text once Peeky began closing the session, its own or the program's still on its way (output_after_disconnect in --json)
```

The last lines are lldb-dap 18's, on Linux. It sends text as it exits, so the output isn't proven
whole ([COOKBOOK](COOKBOOK.md#when-the-program-ended-but-its-output-isnt-proven-whole)).

A Rust session:

```
$ peeky session start . --at src/main.rs:5 --worker http://127.0.0.1:56288
session default (rust, remote): state: stopped at src/main.rs:5 in remote_pages::main, hit 1 (paused 0.01 s)
$ peeky session get start
start: i32 = 3  [settable]
```

**Locations name your checkout,** for the compared sources. The worker builds from a private copy
of the sources on its host. Still, the session above stopped at `src/main.rs:5` in your checkout,
not in that copy. Other paths are marked `worker:` ([Paths in results](#paths-in-results)).

**In a container,** LLDB turns off address randomization for the program it launches. Docker's
default seccomp profile refuses that call, so run the container with `--cap-add=SYS_PTRACE
--security-opt seccomp=unconfined`.

### Source identity

Before anything launches, the worker compares its copy of each source in the table below with
yours. If one differs, or is on one side only, it refuses with `PROVENANCE_CHANGED`, and names the
first such file. The program then never runs.

```
$ peeky probe main.py --at 3 --capture x --worker http://127.0.0.1:18766
error [PROVENANCE_CHANGED]: Worker source provenance differs for helper.py (the copy on the worker host differs from your checkout): refused before launching. Sync sources before debugging remotely
hint: Sources, build outputs or tools changed; start a new session on a fresh build.
```

The worker's trusted target decides which files count. The client hashes its own copies of them,
and sends the hashes with the request.

| Target | The sources compared |
| --- | --- |
| Python | every `.py` file under the root, except in virtual environments (a directory holding `pyvenv.cfg`) |
| Java, launched or attached | the config's `sources` |
| C and C++ | the config's `sources` and `headers` |
| Rust | `Cargo.toml`, `Cargo.lock`, and the config's `sources` and `tests` |

- **Python counts every `.py` file,** in hidden directories and `.git` too, because the program
  may import any of them. A stray file on one side is refused too:
  `scratch.py (only in your checkout)`. Remove it, or copy it over.
- **The Python program and its `--run` script** must be among the files compared. One in a
  virtual environment is refused.
- **Python refuses what it can't hash in full:** more than 4096 `.py` files or 16 MiB in all, a
  file that isn't a readable regular file, or a symlinked directory under the root. The side that
  can't hash refuses. Probe another directory, or start the worker with another `--root`.
- **The program and the `--run` script** are also compared on their own:
  `differs for the program pagination_bug.py`.

**Checked again while it runs.**

- **C, C++, Rust and launched Java:** the build's own copies are compared again, after the build
  and before the debugger starts. So a source changed in between is refused, and doesn't run.
- **Python:** the worker hashes its `.py` files again before and after every operation that
  resumes the program or returns a stop or values: `wait` (and so `session start`), `continue`,
  `step`, `inspect` (and so `get`), `expand`, `preview`, `set` and `report`. A change closes the
  session, and no values come back:

```
$ peeky session continue --to-exit
peeky: [PROVENANCE_CHANGED] Worker source provenance changed during the session: helper.py changed on the worker host after launch. The session was closed, and no values were returned. Sync sources, then start again
```

- **Python stack frames:** at each stop, the worker reads the whole stack. A frame whose code
  names a file under the root that isn't a compared `.py` file is refused: for example, code
  loaded from a `.pyc` beside the program. A frame whose code names a file outside the root isn't
  caught. A frame in a virtual environment inside the root is installed code: it isn't compared,
  and the note below says so.
- **Python bytecode:** a remote Python run reads no `.pyc` from the root, and writes none there.
  Its bytecode cache is a fresh, empty temporary directory, never inside the root or under
  `~/Documents`. It is removed once the program and the debugger have ended.
- A `.pyc` file with no `.py` beside it is still loaded. The frame check above refuses it when it
  is on the stack.
- A Python probe runs these checks at each stop and at its end. A probe that fails one is
  discarded: no rows and no output.
- **The limit:** a file changed, then changed back, between two checks isn't seen.

**Not compared:**

- installed packages, outside the root or in any directory holding a `pyvenv.cfg`, and the
  standard library;
- compiled modules (`.so`), `.pyc` files, and data files the program reads;
- the interpreter and the toolchain;
- an attached JVM's class files. The worker checks them against its own sources instead
  ([JAVA_ATTACH.md](JAVA_ATTACH.md)).

Peeky can't list every module a Python program loaded: debugpy's `modules` request names only the
modules it has seen in stack frames. So code from these that ran without being on the stack at a
stop isn't checked. Every remote Python result says so: probes, `session start`, and every later
`session` command, in its text and in its JSON `warnings`:
`Only the .py files under the target's directory are compared with yours. Not compared: installed
packages, any directory holding a pyvenv.cfg, the standard library, and compiled modules (.so,
.pyc).`

**The client checks every answer:**

- The worker must confirm that it compared the sources (`inputs_checked`).
- It must report the sources the run read (`inputs_sha256`), and they must match yours.
- The program's and the `--run` script's hashes must match yours.
- Your own checkout must still be what was compared. The client hashes it again after a probe's
  answer and after `session start`, for every language. For a session, it also hashes it before
  each later command except `close`, and again after each one that succeeds. A command that fails
  returns no values.

A probe that fails one is discarded: `PROVENANCE_CHANGED`, `results_discarded: true`, no rows and
no output. A session that fails one is closed, and its record is dropped.

**Protocol 8.** These checks, and the [paths in results](#paths-in-results), came with protocol 3.
Protocol 4 adds restores (`@before`). Protocol 5 withholds what a C or C++ pointer one past an
array's end points at, which a protocol 4 worker reads as a value. Protocol 6 marks what is read
through a C or C++ pointer `unproven` where Peeky can't show it points at a live object. A
protocol 5 worker shows it unmarked. Protocol 7 says which C or C++ pointers point at a pointer,
for the note on a value read through one. Protocol 8 reads a GCC lambda's captures, and says why
it doesn't read one. Both hosts need the same protocol. A client and a worker of
different protocols refuse each other:

- A client refuses a worker of another protocol before sending anything: `Worker speaks protocol 7,
  this client 8: install the same Peeky version on both hosts`. That holds for a probe and a
  `session start`.
- A worker refuses a `launch` or `probe` that sends `expect_inputs` without protocol 8: `Client
  speaks protocol 7, this worker 8: install the same Peeky version on both hosts`.
- A worker refuses a request that sends the program's hash without the others, or an empty list.
- A remote session keeps its protocol and its sources' digest. Every later `session` command checks
  the worker's protocol again. A session started with another protocol is refused: start it again.


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
  hint: Restore the worker or SSH tunnel, and retry: it keeps the session until its idle timeout. If it is gone for good, run `peeky session close --forget`.
```

- A `session close` that fails with `UNREACHABLE` keeps the session's record, so you can retry it.
- If the worker is gone for good, `session close --forget` drops the record:
  `forgot default (the worker was not contacted)`.

## What is automated

`tests/integration/test_remote_worker.py` runs the worker as a separate process, and an SSH-like
TCP relay as a third. Its tests cover:

- probes through the forwarded port;
- remote probe evidence saved by the client only, none on the worker host;
- a source-identity mismatch, where results are discarded;
- an imported module that differs on the host, and a module on one side only: refused before
  launching, by name;
- a dropped tunnel giving `UNREACHABLE`, without losing the session;
- reconnecting on the same port, and continuing the session;
- closing without stopping the worker;
- idle reaping, including the debuggee process;
- refusing a non-loopback URL, a missing token, a wrong token (`UNAUTHORIZED`) and a closed port;
- the token never appearing in output.

The [recorded separate-host run](#recorded-separate-host-run-2026-10-22), for Python, Java, C, C++
and Rust, is repeated by CI on every change. A differing C header and a differing Rust module file
are refused by name there, and in the C and Rust lanes too
(`tests/native/test_native_remote_sources.py`, `tests/rust/test_rust_remote_sources.py`).
`tests/unit/test_remote_sources.py` covers the protocol.

## Recorded separate-host run (2026-10-22)

`tests/remote/test_ssh_e2e.py` runs the procedure above against a second host. The host is a Linux
container started from `scripts/remote_e2e/Dockerfile`. It has its own kernel network namespace,
filesystem, OS and Python. The Java tests use the Dockerfile's `java` target, which adds a JDK and
an adapter bundle built in the image. The C, C++ and Rust tests use its `native` target, which adds
LLVM 18 and Rust 1.96.0, the versions the C and Rust lanes use.

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

The Java steps are saved beside it, in `remote-run-java.json`, and the C, C++ and Rust steps in
`remote-run-native.json`.

| | Client | Worker host |
| --- | --- | --- |
| OS | macOS 27.0.1 arm64 | Linux 7.2.9 (OrbStack) aarch64, Debian 13 |
| Python / debugpy | 3.13.8 / 1.8.22 | 3.11.17 / 1.8.22 |
| SSH | OpenSSH 10.3p1 client | OpenSSH 10.0p2 sshd |
| Java | none needed | Temurin 17.0.20.1 (the `java` target only) |
| C, C++ | none needed | LLVM 18 from Debian 13's signed packages: clang, lldb and llvm-dwarfdump 18.1.8 in this run (the `native` target only) |
| Rust | none needed | rustc and cargo 1.96.0, linked with clang 18; installed by rustup-init 1.29.2, checked against its published SHA-256 (the `native` target only) |

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

- All step results matched the first recorded run, of 2026-10-16, except the changed-source
  probe's exit status, which changed from 1 to 2. A probe refused before launch exits 2.
- The token never appears in any command's output. Every step asserts this.
- This run's module took 9.6 s, including starting the container and sshd, with the image already
  built.

**Java** (2026-10-22), with the `java` target's container:

| Step | Command (client) | Exit | Seconds | Result |
| --- | --- | ---: | ---: | --- |
| Probe with a change | `peeky probe . --config launch.json --at demo/PaginationBug.java:13 --capture start,pageSize --set start=0 --worker URL` | 0 | 0.60 | `start` 3 → 0; output `PAGE:[10, 20, 30]` |
| Session start | `peeky session start . --config launch.json --at demo/PaginationBug.java:13 --worker URL` | 0 | 0.49 | stopped at line 13 |
| Read, change, run to the end | `peeky session get start`, `session set start=0`, `session continue --to-exit` | 0 | 0.02 each | `3`; 3 → 0; terminated |
| Program output, close | `peeky session report`, `session close` | 0 | 0.01 | `PAGE:[10, 20, 30]`; closed |
| The host's source differs | `peeky probe … --config launch.json … --worker URL` | 2 | 0.05 | `PROVENANCE_CHANGED`, refused before launching; the host's changed program never ran |
| A second source differs (`demo/Helper.java`) | `peeky probe … --config two.json … --worker URL` | 2 | 0.06 | `PROVENANCE_CHANGED`, naming `demo/Helper.java`; refused before launching |
| Both sources the same | the same probe | 0 | 0.60 | completed; output `PAGE:[10, 20, 30]` |
| The host's source differs, attached JVM | `peeky probe … --config attach.json … --worker URL` | 2 | 0.07 | `PROVENANCE_CHANGED`; the JVM still waited, not attached |
| Attached JVM, probe with a change | `peeky probe … --config attach.json … --worker URL` | 0 | 0.25 | `start` 3 → 0; `ended: hits`. The JVM stayed alive, printed `PAGE:[10, 20, 30]` and blocked on its sentinel file |
| The sentinel file created | `touch release`, on the host | — | — | the JVM printed `DONE` and exited with status 0 |

- The attached JVM was started on the host with
  `-agentlib:jdwp=transport=dt_socket,server=y,suspend=y,address=127.0.0.1:5005`. Then the worker
  was restarted with `--java-attach-config`.
- After its output, the attached program waits for a `release` file. So the probe could pass only
  by ending at its hit, and leaving the JVM running: not by waiting for the JVM's exit, or ending
  it.
- With the Java tests, the module took 8.0 s locally, with both images already built. A cold build
  of the `java` image took 17 s, with its base images already pulled.
- On 2026-10-16, five consecutive local runs took 4.6–5.0 s. On GitHub's `ubuntu-latest` runner,
  with an x86_64 client and container, the module took 21.7 s including the image build.

**C, C++ and Rust** (2026-10-22), with the `native` target's container. Each language has a
`main` file and a header or module it reads: `page.h`, `page.hpp` or `src/page.rs`.

| Step | Command (client) | Exit | Seconds (C / C++ / Rust) | Result |
| --- | --- | ---: | ---: | --- |
| Probe with a change | `peeky probe . --at main.c:7 --capture start --set start=0 --worker URL` | 0 | 0.79 / 0.93 / 0.96 | `start` 3 → 0; output `START:0` |
| Session start | `peeky session start . --at main.c:7 --worker URL` | 0 | 0.60 / 0.77 / 0.71 | stopped at `main.c:7`, `main.cpp:7`, `src/main.rs:5`: in your checkout |
| Read, inspect, run to the end | `peeky session get start`, `session inspect`, `session continue --to-exit` | 0 | 0.06 or less each | `3`; frame 0 in your checkout; terminated |
| Program output, close | `peeky session report`, `session close` | 0 | 0.11 or less | `START:3`; closed |
| The host's `main` file differs | `peeky probe … --worker URL` | 2 | 0.08–0.09 | `PROVENANCE_CHANGED`, `differs for the program main.c`; the changed program was never built or run |
| The host's header or module differs | `peeky probe … --worker URL` | 2 | 0.08–0.09 | `PROVENANCE_CHANGED`, naming `page.h`, `page.hpp` or `src/page.rs`; refused before launching |

- The C++ and Rust commands are the same, with `main.cpp:7` or `src/main.rs:5`.
- The record's `environment` keeps the tool versions each run used. apt installs LLVM 18's current
  Debian package, so its patch version can change.
- The changed `main` file creates a marker file if it runs. The marker never appeared.
- No probe or session result names a path that exists only on the host, unless it is marked
  `worker:`. Those tests check every string in their JSON results for the worker's root and its
  build directories.
- The container needs `--cap-add=SYS_PTRACE --security-opt seccomp=unconfined` for LLDB.
- With all three images built, the whole module took 19.8 s locally: 21 tests, 12 of them C, C++
  and Rust.
- Cold builds, with the base images already pulled, took 94 s in all: 12 s for `host`, 9 s for
  `java` and 73 s for `native`. The `native` stage took 35–37 s in two earlier builds; most of it
  is downloading. The image is 1.8 GB.
- The `native` stage installs its toolchain before Peeky. So a change to Peeky's source rebuilds it
  in about 5 s. A cold rebuild of the stage, with the checked rustup-init, took 45 s.
