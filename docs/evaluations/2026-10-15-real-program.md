# Live agent evaluation: a real program, CPython 3.9.6 `ipaddress` (2026-10-15)

This closes the last #7 acceptance item: a demonstration on a non-synthetic Python program. The case is in [examples/real/ipaddress_mapped](../../examples/real/ipaddress_mapped).

## Setup

- **Program:** CPython 3.9.6's `Lib/ipaddress.py`, copied byte for byte and named `ipaddress39.py`, plus a small egress-policy entry script, `classify.py`.
- **Bug:** a real one, fixed upstream for CVE-2024-4032. `IPv6Address.is_private` and `is_global` misclassify IPv4-mapped addresses, so `::ffff:8.8.8.8` is labelled private.
- **Agent:** one independent general-purpose coding agent, given:
  - only `docs/AGENT_GUIDE.md`, `docs/ERRORS.md` and the `peeky` CLI (`feat/real-program`, with the new `--run`);
  - an issue text that does not mention the CVE;
  - instructions not to use web search or recall the upstream patch, and to find the cause with Peeky evidence.
- **Checker:** `check.py`, based on RFC 4291 §2.5.5.2. A mapped address must classify like its IPv4 address, and native classifications must not change.

## Outcome: verified general fix in 11 Peeky calls, about 3 minutes end to end

1. **Observe:** a probe at `is_private`'s `return any(...)` with `--run classify.py` showed `self` as `::ffff:808:808`. A probe in `_BaseNetwork.__contains__` with `--hits 80` showed the membership checks: for the mapped address, the last network tested (where `any()` stopped) was `::ffff:0:0/96`.
2. **Find where that network comes from:** a probe at the private-network table's construction found the hit that builds `IPv6Network('::ffff:0:0/96')`.
3. **Intervene:** in a session the agent stopped there, set `address='::/128'`, continued, and predicted both mapped addresses would become public. Confirmed: `::ffff:10.0.0.1: public`, `::ffff:8.8.8.8: public`. That proves the `/96` entry alone causes the misclassification. The agent then rejected the tempting fix of deleting it, since `::ffff:10.0.0.1` must stay private.
4. **Verify:**
   - The one-line `--old` was refused with `matched 2 times (lines 1335, 2008)`.
   - An `is_private`-only fix was **rejected** by the checker (`::ffff:6440:1` / `100.64.0.1`: `is_global` stays `not is_private`). The agent had predicted this.
   - The fix that defers both `is_private` and `is_global` to `self.ipv4_mapped` **passed**. It matches the upstream fix.

Wall time per call: probes 0.3–1.1 s, verify about 0.07 s.

## Feedback and dispositions

**Delivered in #26:** names in scope per function in `describe`, `--set-hit N`, and `by_hit`.

**Delivered in this slice:**
- `--run` lets the agent break in an imported module.
- The helpful directory-target error and full suggested commands.
- The function name per hit, which tells `is_private` apart from its `<genexpr>`.
- `--args` for the entry script.
- An output-completeness flag for `--until hits`.
- `session continue --to-exit`.
- `verify --old-file/--new-file`, so shell quoting cannot strip newlines.

**Deferred:**
- **Return values (`@return`):** needs a step-out, which would execute code.
- **Literal conditions (`--when`):** DAP conditions are evaluated inside the debuggee, so they are rejected by design. A Peeky-side literal filter is possible later.
- **Several edits per verify:** the compiled-language verification path takes a single edit.
- **A `--symbol` lookup:** `describe` scopes cover most of this.
