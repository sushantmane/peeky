# ADR 0006: Agent-speed debugging — bounded pauses and calls

Status: accepted (maintainer direction, 2026-10-15: agents come first; they must be able to deal with timeouts, and any pause must stay short).

## Context

Agents are slow and impatient in specific ways:

- **Agents think slowly.** Each model turn takes seconds to tens of seconds. A debugger stop that waits for the next agent command can last minutes. Programs with real clocks break under such pauses. Examples include TestNG `timeOut` (30 s on the Venice test we exercised), JUnit `@Timeout`, ZooKeeper sessions, Kafka consumer heartbeats and RPC deadlines. The JAVA_ATTACH guide could only say "keep pauses short", which an agent cannot reliably do.
- **Agent tools time out.** Harnesses commonly kill shell commands after 30 to 120 s. A probe on a Gradle-launched JVM, or a compiled verify, can take longer. When the tool is killed, the work is lost.

## Decision

1. **Probes are the default for timed programs.** A probe captures values and resumes the program within milliseconds, with no model turn while the program is paused. Results report `pauses_ms` per hit (about 4 to 8 ms locally).
2. **Probes on attached JVMs end at the last hit.** `--until hits` returns, and detaches, as soon as the requested hits are captured, instead of waiting for the JVM to exit. `auto` is the default: `hits` for attached JVMs, `exit` for launched programs, which run to completion so their output can be reported.
3. **Sessions have an optional pause budget.** `session start --max-pause S` (HTTP `launch` field `max_pause`, 0 < S ≤ 300) makes the worker auto-resume any stop older than S seconds:
   - Only stops at Peeky's own breakpoints (`reason: breakpoint`) are resumed. Crash, exception and signal stops stay paused for inspection; resuming a fault would usually just fault again. `wait` and `inspect` report each stop's `reason`.
   - This applies even to stops no client has observed yet.
   - Auto-resume is recorded in the audit log.
   - Later operations on that stop fail with `STALE_STOP` and a message saying the program was auto-resumed. `wait` reports a count, `auto_resumed`, plus `last_auto_resumed` (its reason, location and pause).
   - Attached JVMs default to 10 s; `--max-pause 0` disables it. Launched programs default to no budget.
   - `inspect` and `wait` report `paused_s` and `resume_in_s`, so the agent can see how much time it has left.
   - If auto-resume itself fails, the worker closes the session, which detaches an attached JVM. It never leaves the program paused.
4. **Long calls go to the background.** `probe` and `verify` accept `--background` and return a job id immediately. The job runs detached in its own process session. `peeky job wait <id> [--timeout S]` blocks for at most S seconds (default 20). It then replays the job's exact foreground stdout and stderr and its exit code, or exits **3** while the job is still running, with the last progress lines.
   - A job whose process died is reported as `JOB_LOST`.
   - Job directories are private (`0700`/`0600`) under `$PEEKY_STATE_DIR/jobs` and are pruned after 24 hours.
   - Sessions already have a resumable form, because `session start` and `wait` return `running` after `--timeout`.
5. **`describe` states the timing contract** for the target in a `timing` field: probe pause, the session pause budget, and advice.

## Evidence

On Venice's timed `TestEmptyPush` test (TestNG `timeOut` 30 s), see [JAVA_ATTACH.md](../JAVA_ATTACH.md):
- A background probe returned both hits in 19.2 s, with pauses of 100–115 ms.
- A slow agent's session and an absent agent's session both had their stops auto-resumed after 10 s.
- All three runs passed both tests.

## Consequences

- Agents get a safe default: a probe never stalls a timed program on a model turn.
- Sessions on attached JVMs are bounded by default, so the test fails less often. The agent may lose a stop and must wait for the next one or switch to a probe. That is the intended trade-off: the program's correctness matters more than the agent's convenience.
- Background jobs mean any call can fit in a short tool budget.
- A stop with no budget, on a launched program, still waits indefinitely, up to the worker's 300 s idle reaper.
- Pause budgets limit the *debugger's* pause. They cannot make up for a test whose own timeout is shorter than its setup time.
