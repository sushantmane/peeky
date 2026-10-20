# Java attach (Venice end-to-end tests)

`peeky` can attach to a JVM that is already waiting for a debugger, for example a Gradle test JVM started with `--debug-jvm`. Peeky did not build that program, so provenance is reported as **attached**. Declared sources, the exact class files that map them, and the adapter bundle are hashed and rechecked before every worker operation, including `wait` (after each stop or termination) and `report`. Peeky still cannot prove the JVM loaded those bytes. Attached sessions:

- are loopback-only (tunnel remote JVMs over SSH);
- read values and allow numeric/boolean root-local changes (evidence only);
- never verify edits (`peeky verify` refuses; verify with a fresh-build target);
- **detach without killing** the JVM, so the test continues after the probe.

Breakpoints may start pending until the class loads; the adapter verifies them when it does. Large JVMs emit thousands of DAP `thread` events; Peeky keeps only lifecycle events in its bounded history, so thread churn cannot evict a stop. A genuine flood of lifecycle events still fails the session explicitly. A probe that ends without stopping reports `no-stop`.

## Venice walkthrough (verified 2026-10-14, Apple Silicon, JDK 17)

1. Build the adapter once: `python scripts/setup_java_adapter.py --output "$PWD/work/java-adapter"` (use an absolute path).
2. Apple Silicon without Rosetta: grpc-java ships only x86_64 macOS code generators. Build one natively (requires Homebrew `protobuf` and `abseil`):
   ```sh
   scripts/venice/native-grpc.sh            # writes ~/.peeky/venice/native-grpc.init.gradle
   cd ~/workspace/venice && ./gradlew -I ~/.peeky/venice/native-grpc.init.gradle assemble \
     :internal:venice-test-common:integrationTestClasses
   ```
   With Rosetta installed, omit `-I ...`.
3. Start one end-to-end test suspended on port 5005 (forcing a rerun and bypassing the build cache):
   ```sh
   ./gradlew --no-build-cache -I ~/.peeky/venice/native-grpc.init.gradle \
     :internal:venice-test-common:cleanIntegrationTests_32 :internal:venice-test-common:integrationTests_32 \
     --tests 'com.linkedin.venice.endToEnd.TestEmptyPush.testEmptyPushWithZstdWithDictCompression' --debug-jvm
   ```
   The shard (`_32`) comes from `internal/venice-test-common/test-shard-assignments.json`.
4. Describe the attach target in a trusted config, e.g. `~/.peeky/venice/attach-TestEmptyPush.json`:
   ```json
   {
     "bundle": "/absolute/path/to/peeky/work/java-adapter",
     "port": 5005,
     "sources": ["internal/venice-test-common/src/integrationTest/java/com/linkedin/venice/endToEnd/TestEmptyPush.java"],
     "class_roots": ["internal/venice-test-common/build/classes/java/integrationTest"]
   }
   ```
   Add production sources and their `build/classes/java/main` directories to break inside Venice itself.
5. Ask questions:
   ```sh
   T=internal/venice-test-common/src/integrationTest/java/com/linkedin/venice/endToEnd/TestEmptyPush.java
   peeky describe ~/workspace/venice --config ~/.peeky/venice/attach-TestEmptyPush.json --at $T
   peeky probe ~/workspace/venice --config ~/.peeky/venice/attach-TestEmptyPush.json \
     --at $T:85 --capture storeName,sendControlMessageDirectly,controllerClient --hits 2 --timeout 240
   ```

Observed result: one probe captured both data-provider runs (`sendControlMessageDirectly=false` and `true`, each with its own `storeName`) in 28.5 s, then detached. Gradle reported both tests **PASSED**.

Timed tests ([ADR 0006](adr/0006-agent-speed.md)). These tests use a TestNG `timeOut` (30 s for this method), and a long stop fails them.
- **Use probes.** Each hit pauses the JVM for milliseconds (`pauses_ms`), and on attached JVMs a probe returns as soon as its last hit is captured (`--until hits`, the default here).
- **Sessions on attached JVMs auto-resume** any stop after 10 s (`--max-pause`; `0` disables this). An operation on a resumed stop fails with `STALE_STOP` and says why. `inspect` shows `paused_s` and `resume_in_s`.
- **Long waits:** if the Gradle startup outlasts your tool timeout, add `--background` and poll with `peeky job wait <id>`.

Verified on Venice `ba78edda8` (2026-10-15):
- **Background probe:** `--background --hits 2` returned a job id at once. `job wait` delivered both hits in 19.2 s, compared with 28.5 s when waiting for JVM exit. Each pause lasted 100–115 ms, and both tests PASSED.
- **Slow-agent session** (stop observed, 15 s of "thinking"): each stop was auto-resumed after 10 s, and both tests PASSED in about 13 s each.
- **Absent agent** (no `wait` after start): both stops were auto-resumed at about 10.1 s, as recorded in the audit, and both tests PASSED.

### Breakpoints in production code

To break inside Venice code, declare production sources together with their `build/classes/java/main` roots. Client code and server code that runs in the test JVM both work. `peeky describe` without `--at` lists breakpoint lines for every declared source.

```json
{
  "bundle": "/absolute/path/to/peeky/work/java-adapter-attach",
  "port": 5005,
  "sources": [
    "internal/venice-common/src/main/java/com/linkedin/venice/controllerapi/ControllerClient.java",
    "services/venice-controller/src/main/java/com/linkedin/venice/controller/server/CreateStore.java"
  ],
  "class_roots": [
    "internal/venice-common/build/classes/java/main",
    "services/venice-controller/build/classes/java/main"
  ]
}
```

Verified on 2026-10-15. Each run captured both data-provider invocations, returned at the last hit, and both tests PASSED.

| Breakpoint | Values captured | Pause per hit | Probe time |
| --- | --- | --- | --- |
| `ControllerClient.java:594` (client library, inside `createNewStore`) | `storeName`, `owner` | 145–153 ms | 18.3 s |
| `CreateStore.java:64` (controller HTTP handler, on a Jetty thread) | `clusterName`, `storeName`, `owner`, `isSystemStore=false` | 178–186 ms | 21.4 s |

In one earlier run, the worker received no `stopped` event for about 200 s while the test thread sat at the breakpoint, so both tests timed out. This did not reproduce in two identical attempts. Peeky now recovers such a stop within a few seconds ([Lost stop events](#lost-stop-events-27)). `session report` includes `debugger_events` (recent lifecycle events, counts of ignored events and a DAP trace), and `session close` keeps the worker log as `<name>.last.log` for diagnosis.

## Dropped exit events (#27)

The Java adapter (java-debug) occasionally never sends `exited`/`terminated` after a **launched** JVM finishes. In stress runs this happened in about 1 of 50–100 runs; the program's output had arrived and the adapter was alive but silent. Peeky now checks liveness for launched Java targets.
- **When it checks:** after 2 quiet seconds, it asks the adapter for `threads`.
- **Why one empty reply isn't enough:** java-debug can answer with an empty list for a live JVM whose threads churn, because a thread is collected mid-lookup.
- **When Peeky infers an exit:** when java-debug announced the JVM's pid (its `processid` event, which is the JVM itself), only once that process is gone. Without a pid, only when a second empty reply follows at least 2 s later. The count of empty replies resets for every session. The run then ends as `terminated` with `exit_inferred: true`, an audit entry and a warning.
- **Slow or failed checks:** these count as "still running" and never close the session.
- **Real events:** a stop or exit that arrives during the check is used instead of the inference.
- **Attached JVMs are excluded:** their death closes the JDWP connection, and a false inference would detach a live JVM.

Evidence:
- 200 of 200 stress runs completed, and one dropped exit resolved in 2.8 s.
- A real-JDK regression test runs an extreme-churn JVM. The single-reply rule declared it exited after about 6 s; it is now never reported as exited.

An attached JVM's lost `stopped` event is handled separately, below.

## Lost stop events (#27)

java-debug occasionally never sends `stopped` for an **attached** JVM, although the thread sits suspended at the breakpoint. This was seen once on Venice and twice in this repository's attach tests under heavy machine load; it never reproduced on demand. A probe or session then waited out its whole timeout (60 s or more). Peeky now recovers the stop.

- **Why the evidence is sound:**
  - java-debug lists stack frames only for a thread the debugger suspended (a running thread's `stackTrace` is empty).
  - Peeky's Java adapter suspends threads only for the session's own line breakpoints and steps. It refuses `pause`, exception and function breakpoints, conditions and log points, and a JVM accepts one debugger at a time.
  - So a thread that is not stepping, whose top frame is exactly at one of the session's breakpoints, was stopped by that breakpoint.
- **Exact criteria:** a thread is taken for a lost breakpoint stop only when all of these hold:
  - the session is running, and the debugger has reported no stop or exit that Peeky has not consumed;
  - the thread has no step in flight. A step that ends on a breakpoint line is reported as a step, so a lost step report must never become a breakpoint hit;
  - its top frame's resolved source path and line equal those of a breakpoint the session set, and the debugger **verified** that breakpoint (in its `setBreakpoints` reply or a later `breakpoint` event);
  - two consecutive polls, at least 2 s apart, find it there, and no report arrived in between.
- **Polling bounds:**
  - Polls start 2 s after a resume and run at most every 2 s, and only while a verified breakpoint exists.
  - Each poll is one `threads` request plus at most 256 one-frame `stackTrace` requests (a rotating window in larger JVMs), all within 1 s. With about 300 threads, a poll took 40–90 ms.
  - A slow or failed poll is no evidence: the session keeps waiting.
- **Why 2 s:** a real report arrives within milliseconds of the stop (an attached probe takes about 0.2 s end to end), so polls never run during a normal stop. A recovered stop's report is always at least 2 s overdue. Recovery takes 2–4 s after the stop.
- **The recovered stop** is an ordinary stop:
  - It gets a new `stop_id` and reason `breakpoint`, and it is numbered like any hit. `inspect`, `set`, `continue` and `step` work as usual.
  - It carries `recovered: true`, and the event body's `peeky` field says why. The history gets a `stop_recovered` entry, and probes add `recovered_stops` and a warning.
  - Its pause (`paused_s`, `pauses_ms`, `--max-pause`) counts from the first poll that saw it.
- **Races:**
  - A real report that arrives during a poll wins over the recovery.
  - A report that arrives after the stop was recovered is consumed and recorded in the history as `late_stop_report`; it is never served as a second stop. java-debug reports each thread's stops in order and once each, so that thread's next breakpoint report is the late one.
  - If that report was really a new stop (the earlier one's report was lost for good), the thread is still suspended at the breakpoint, and the next polls recover it. A report with another reason (a step) means the late one will not come.
- **Unattended sessions:** with a pause budget (`--max-pause`, the default for attached JVMs), the worker polls even when no client is waiting, so a lost stop is still auto-resumed.
- **Launched JVMs are not polled.** The same evidence would hold there, but the loss has only been seen on attach.
- **Diagnostics:** the polls stay out of `debugger_events.trace`. A single merged `lost-stop poll` note there gives the number of polls (`times`), the threads checked and the suspects.

Tests drop the `stopped` event at Peeky's DAP client (`tests/java/test_java_attach.py`, `tests/unit/test_lost_stop.py`):
- an attached probe still reads and changes values within a few seconds;
- a session steps and continues after recovered stops, absorbs late reports (both while the stop is current and after the thread runs again), and counts each hit once;
- a step that ends on a verified breakpoint line, with its report lost, is never taken for a breakpoint stop.
