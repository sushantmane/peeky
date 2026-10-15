# ADR 0005: Peeky is a tool that agents call, not an agent

Status: accepted (owner, 2026-10-14). Supersedes the embedded model loop as the primary interface.

## Context

`peeky-agent` embedded a Codex CLI decision loop: Peeky called the model. That inverts the useful relationship. As with heappy, the model or harness (Copilot, Claude, Codex, a script) should orchestrate a standalone tool, and a person should be able to run the same commands without any model. Users should also not have to choose a mode before asking a question.

## Decision

- `peeky` is a standalone CLI (and the same operations over HTTP for remote targets). It never calls a model.
- There is no mode switch. Each subcommand stands alone:
  - `peeky describe <target>`: a manifest of language, breakpoint lines, qualified value views, mutation rules and commands, the equivalent of heappy's `_manifest.json`.
  - `peeky probe <target> --at <path:line> --capture ... [--set ...]`: one stateless question.
  - `peeky session start|wait|inspect|get|set|continue|report|close|list`: step-by-step debugging across separate shell commands, with state held by a local worker process.
  - `peeky verify <target> --check <checker> --old ... --new ...`: a fresh build plus the independent checker (and the compiled Cargo test for Rust) for one candidate edit.
- Targets are detected from the path (`Cargo.toml`, `.py`, `.c`/`.cpp`), with toolchains discovered from `PEEKY_*` variables, rustup, xcrun or `PATH`. A trusted `--config` overrides detection and is required for Java.
- Every command prints a readable table by default and JSON with `--json`.
- Agent operating instructions live in [AGENT_GUIDE.md](../AGENT_GUIDE.md) and [skills/peeky-debug/SKILL.md](../../skills/peeky-debug/SKILL.md).
- `peeky-agent` remains only as a reference harness and deterministic test driver. It is no longer the product interface.

All existing guarantees are unchanged: fresh builds and provenance checks, stop/frame/scope identity, bounded output and time, opaque unqualified values, restricted mutation, and verification only by fresh build plus independent checker.

## Consequences

Agents can learn Peeky from `describe` and the guide, then call it the way they call `git` or `heappy`. Humans get the same commands. Session state lives in a local worker process and a `0600` record under `~/.peeky/sessions`. That record holds a loopback URL and a random token and must be treated as a credential.

Next: Java attach to a JVM started by Gradle (`--debug-jvm`) so Venice end-to-end tests can be investigated, with provenance explicitly labeled as attached rather than freshly built.
