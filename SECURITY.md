# Security policy

## Supported versions

Security fixes go into the latest release only. Upgrade to it, and check that the problem is still
there, before you report it.

## Report a vulnerability

Report it privately, through GitHub's private vulnerability reporting:

1. Open the repository's page on GitHub.
2. Open the **Security** tab, and choose **Report a vulnerability**.
3. Describe the problem, and send the report.

Don't open a public issue, pull request or discussion about it.

A good report has:

- the Peeky version (`peeky --version`) and the operating system;
- the steps, or a short proof of concept, that show the problem;
- what an attacker gains, and what access they need first.

The reply and the fix are discussed in the private report. The advisory is published when a fixed
release is out.

## In scope

- **The worker's authentication.** A request that the worker carries out without the right token,
  or a worker that listens anywhere but `127.0.0.1`.
- **The worker's root confinement.** A request that makes the worker launch or build a program
  outside its `--root`.
- **Path handling.** A path in a request, a result or a session record that reaches a file it
  shouldn't, for example through `..` or a link.
- **`peeky sql` confinement.** A query that changes saved runs, writes a file, loads an extension,
  or reads anything but the saved runs.
- **Token handling.** The token reaching a result, a report, an error, a log, an argument list, or
  the environment of a debugger, debuggee, build, checker or model process.

## By design

These are not vulnerabilities:

- An authenticated client can run code. It launches programs, changes their values, and builds and
  runs candidate edits, with the worker's permissions.
- Peeky runs the program you point it at, with your permissions. It is not a sandbox for hostile
  code.
- `peeky-agent` applies and runs the model's edit without a sandbox. Program output that reaches
  the model is untrusted, and can steer it.
- The program the worker launches can read and import files outside `--root`. The root check
  covers the program a request names.
- A process running as the same user can read Peeky's environment, process state and session
  records.

[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) describes these trust boundaries in full.
