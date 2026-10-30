# Security policy

## Supported versions

Security fixes go into the latest release only. Upgrade to it, and check that the problem is still
there, before you report it.

## Report a vulnerability

Report it privately, through GitHub's private vulnerability reporting:

1. Open the repository's page on GitHub.
2. Open the **Security** tab, and choose **Report a vulnerability**.
3. Describe the problem, and send the report.

If that option isn't available, open an issue asking for a private contact, without details.

Don't put the details in a public issue, pull request or discussion.

A good report has:

- the Peeky version (`peeky --version`) and the operating system;
- the steps, or a short proof of concept, that show the problem;
- what an attacker gains, and what access they need first.

The reply and the fix are discussed in the private report. The advisory is published when a fixed
release is out.

## In scope

- **The worker's authentication.** A request that the worker carries out without the right token,
  or a worker that listens anywhere but `127.0.0.1`.
- **The worker's proof.** A client that sends the token, or any request, to a process that hasn't
  proven it holds the token. Or a client that sends it to anything but `http://127.0.0.1:PORT`.
- **The worker's root check.** A request that names a program or `--run` script outside `--root`,
  and the worker launches or builds it. The check covers the file a request names, not what code
  then runs.
- **Other users on the same host.** Another local user who gets the token, or makes Peeky run
  their code: through a shared directory such as `/tmp`, or the port of a worker that stopped.
- **Path handling.** A path in a request, a result or a session record that reaches a file it
  shouldn't, for example through `..` or a link.
- **`peeky sql` confinement.** A query that changes saved runs, writes a file, loads an extension,
  or reads anything but the saved runs.
- **Token handling.** The token reaching a result, a report, an error, a log, an argument list, or
  the environment of any process Peeky starts. Two get a token on purpose: a session's own local
  worker gets its own, and a `--background` job gets the client's, as it is the same command.

## By design

These are not vulnerabilities:

- An authenticated client can run any code. It launches programs, changes their values, and builds
  and runs candidate edits, with the worker's permissions. A request's `edit` changes the code
  that runs, so an edit that reaches outside `--root` is not a vulnerability.
- Peeky runs the program you point it at, with your permissions. It is not a sandbox for hostile
  code.
- `peeky-agent` applies and runs the model's edit without a sandbox. Program output that reaches
  the model is untrusted, and can steer it.
- The program the worker launches can read and import files outside `--root`. The root check
  covers the program a request names.
- A process running as the same user, or as root, can read Peeky's environment, process state and
  session records.
- Another local process can keep the worker busy, up to its limits: 32 connections at once, each
  for at most 30 s before its request arrives. That is a denial of service, not a way in.
- The limits of the worker's proof that [THREAT_MODEL.md](docs/THREAT_MODEL.md) names: a relay to
  a running worker with the same token, and a port taken between two of one client's requests.

[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) describes these trust boundaries in full.
