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
- The worker reads it from `DEBUG_TOKEN`, and every client from `PEEKY_WORKER_TOKEN`, including
  `peeky-agent --url`. A client given only `DEBUG_TOKEN` refuses it, and names
  `PEEKY_WORKER_TOKEN`.
- `peeky-worker`, `peeky` and `peeky-agent` take both names out of their own environment first.
  Each process Peeky starts also gets an environment without them. So no debugger, debuggee,
  build, tool Peeky asks, checker, candidate, probe process or model process gets the token. Nor
  does a Python program's interpreter, or Peeky's helper that reads the program's code in it.
- Two processes get one on purpose. A session's own local worker gets its own token, as
  `DEBUG_TOKEN`. A `--background` job gets the client's, as `PEEKY_WORKER_TOKEN`, since it is the
  same `peeky` command.
- Never print it in evidence. No process that could print it is given it, so a reply can hold the
  token only where the client put it in a request.
- The worker never adds the token to a reply itself. No error it sends, and nothing it logs,
  quotes a request's headers.
- As a second line of defence, the worker removes its token from the error text it builds, before
  its own cuts: a build's output, a probe child's errors and stderr, and the message. The child
  leaves its errors' text long (64 KiB), and the worker cuts it.
- Limit: text quoted from the debugger or a request is passed as sent, and may be cut first. A
  part of a token there can remain. A client that sends the token in a field gets its own input
  back.
- Request fields are forwarded and echoed as sent, and program output and values are never
  changed.

**A Python program's interpreter**

- A Python program runs on its project's interpreter: `--python`, else the root's `.venv` or
  `venv`, else `$VIRTUAL_ENV` ([Python 0](lang/python-cookbook.md#which-interpreter-runs-the-program)).
- It is the project's code, as the program is, and runs as the program does. Its startup, such as
  an environment's `.pth` files, also runs in the helper that reads the program's code.
- Peeky looks for an environment in the root only, never in a directory above it. It follows links
  only to report where they lead.
- A request to a worker can't name an interpreter. Only the worker's `--python` and its root do.

**The worker proves it holds the token (protocol 11)**

- A client sends a worker nothing, and not the token, before the worker answers its `hello`.
- The client sends a fresh random 32-byte nonce, without the token. The worker answers with
  HMAC-SHA256 of a fixed label and the nonce, keyed by the token. The client compares it in
  constant time.
- So a process that took the worker's port after it stopped never gets the token. Nor does a
  worker started with another token. The client refuses both with `UNAUTHORIZED`.
- `hello` needs no token. It says only the protocol and the proof, and runs nothing.
- So anyone who can connect can test guesses of the token offline, against a proof. The token
  must be random, and 32 to 512 characters: the worker and each client command refuse others.
  `secrets.token_urlsafe(32)` makes 43. The `Client` class needs no such check: it never sends
  the token before the proof.
- The most, 512, is far below the 64 KiB a probe child keeps of an error's text. So the worker
  can remove a token whole from it.
- **Browsers:** a web page's requests are refused before anything runs. Its scripts send an
  `Origin` header, and a page whose name was pointed at 127.0.0.1 (DNS rebinding) sends that name
  as its `Host`. The worker refuses a request with an `Origin` header, or a `Host` other than
  127.0.0.1, with 403, before `hello` and before the token check.
- At capacity, the `BUSY` answer comes before that check: such a request gets 503, not 403. It
  still runs nothing.
- It assumes the token is secret, random and long. A process that can reach a running worker with
  the same token can pass a client's `hello` on to it (a relay). So start each worker with a fresh
  token, and never two with the same one.
- A client checks the proof once, and again after any lost connection. A process that takes the
  port between two of one client's requests, with no failure between them, isn't caught.
- SSH still carries the traffic between hosts. Its encryption keeps the token from the network.
- Start SSH tunnels with `-o ExitOnForwardFailure=yes`, so that ssh exits when another process
  holds the local port.

**Request limits**

- The worker waits at most 10 s for each read of a request. The whole request must arrive within
  30 s. Both hold before and after the token check. Then the worker closes the connection, and
  runs nothing.
- It serves at most 32 connections at once. It reads up to 16 more, within 2 s each, and answers
  each `BUSY` (HTTP 503): not run, retry. It closes any beyond those unread, which a client
  reads as a lost connection (`TRANSPORT_LOST`).
- These bound what one process can hold, not what it can deny. Another local process can still
  keep the worker busy, up to those limits. That is a known denial of service.

**Other users on the host**

They are in scope. Another user can connect to the worker's port, and write to a shared
directory such as `/tmp`. Peeky defends against them:

- **The token:** a client sends it only to a worker that proved it holds it.
- **Builds and candidates:** a build of any language, a Python candidate's copy of its root, and
  `peeky verify`'s candidate go only where no other user can write, along the whole path. So a
  program that reaches above them (`../`, `Path(__file__).parents[2]`) finds nothing of another
  user's.
- Peeky takes the first such place of: the system's temporary directory (macOS gives each user
  one), your runtime directory on Linux (`$XDG_RUNTIME_DIR`), and `scratch` in Peeky's state
  directory. It refuses to build where there is none. A Python candidate's copy is never made
  inside its own target.
- A Rust build also needs a place with no Cargo configuration above it, such as your own
  `~/.cargo/config.toml` above the state directory.
- Peeky makes its state directory writable by you alone, whatever the umask.
- Cargo reads `.cargo/config.toml` in every directory above its build. A C `#include "../x.h"`
  reads above it too. So another user can add neither.
- **A session's own worker** runs isolated (`python -I`), from Peeky's state directory. A `peeky`
  package in the current directory never runs.

A process running as the same user, or as root, isn't covered.

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
  reuses it by the same rules. Peeky reads an entry only if it's a regular file of yours that
  others can't read or write. Each build still asks each tool its version.
- To rehash every time, set `PEEKY_NO_DIGEST_CACHE=1`. This also turns off what the state
  directory keeps. Do this, for example, on filesystems whose timestamps are unreliable.
- Compiled builds also record every file the compiler read:
  - Rust fails closed on any file the compiler read that isn't a declared source.
  - C and C++ hash the headers they read outside the workspace, and recheck them before every
    operation.
- Cargo's own configuration isn't a file the compiler reads. Before each Cargo command, Peeky
  refuses a `.cargo/config.toml`, `.cargo/config` or `Cargo.toml` in any directory above the
  build. It also refuses any of those directories that another user can write.
- Cargo runs with `RUSTC_WRAPPER` set empty, so no configured `build.rustc-wrapper` runs. A file
  that the same user adds between that check and Cargo's read isn't caught.
- A Java adapter bundle's `adapter.json` lists its files' hashes, so it vouches only for itself.
  Peeky runs its own bundle, the one `peeky setup-java` installs, only from directories you own,
  mode 0700, under none that another user can write. A bundle a config names is trusted as the
  config is.
- Peeky runs a Java adapter bundle only if its recorded recipe is this Peeky's: the patches'
  text, the launcher, the lock, the notices and the compiler flags. It doesn't ask the JDK that
  runs it. The classes are compiled with `--release 17`, and a JVM that can't run them refuses
  them.

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
- Clients send the token only to `http://127.0.0.1:PORT`. `peeky --worker`, `peeky-agent --url`
  and the `Client` SDK refuse any other URL.
- SSH is the remote transport.
- There is no public HTTP service, TLS termination service, Kubernetes RBAC integration, outbound
  relay, tenant isolation, or production write policy yet.

## Data and limits

- Use synthetic fixtures in CI. Avoid uploading real application reports.
- A process of the same user may still inspect the environment and process state. Stripping a
  token from a child's environment is defense in depth, not isolation from the same user.
- Reports are not tamper-proof attestations.
