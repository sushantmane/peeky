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

In one earlier run, the worker received no `stopped` event for about 200 s while the test thread sat at the breakpoint, so both tests timed out. This did not reproduce in two identical attempts. Peeky now detects such a stop within seconds and fails with a diagnosis ([Lost stop events](#lost-stop-events-27)). `session report` includes `debugger_events` (recent lifecycle events, counts of ignored events and a DAP trace), and `session close` keeps the worker log as `<name>.last.log` for diagnosis.

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

java-debug occasionally never sends `stopped` for an **attached** JVM, although the thread sits suspended at the breakpoint. This was seen once on Venice and twice in this repository's attach tests under heavy machine load; it never reproduced on demand. A probe or session then waited out its whole timeout (60 s or more). Peeky now detects this and **fails fast with `DEBUGGER_ERROR`**, naming the evidence. It never serves such a stop: a stop the debugger did not report cannot be trusted, and serving it led to wrong hits in review.

- **What java-debug suspends.** java-debug lists stack frames only for threads the debugger suspended (a running thread's `stackTrace` is empty). Two things suspend threads:
  - the session's line breakpoints and steps suspend one thread each;
  - java-debug's own class-load requests (`Breakpoint.install`, for the breakpoint's class and its nested classes) suspend **every** thread while java-debug looks for the line. A thread blocked entering `synchronized` then shows the line inside the block, even though that line never ran.

  Nothing else does: Peeky's Java adapter refuses `pause`, exception and function breakpoints, conditions and log points, and a JVM accepts one debugger at a time.
- **Exact criteria.** A poll is a *sighting* only when all of these hold:
  - the session is running, and the debugger has reported no stop or exit that Peeky has not consumed;
  - exactly one thread without a step in flight is suspended. Threads with a step in flight are ignored: a step that ends on a breakpoint line is reported as a step, so a lost step report must never look like a breakpoint stop;
  - that thread's top frame is at the resolved source path and line of a breakpoint the session set, and the debugger **verified** it (in its `setBreakpoints` reply or a later `breakpoint` event);
  - every other thread checked is running. The checks cover the JVM's own threads (Reference Handler, Finalizer, Common-Cleaner, Signal Dispatcher) before and after the rest, every other thread in the poll's window, and the suspect twice more, each time followed by the JVM's own threads again. Any other suspended thread means a suspend-all event, and the poll is no evidence.

  Evidence is the same thread at the same place in **3 consecutive sightings spanning at least 4 s**, with no report in between. A real report that arrives meanwhile, even during a poll, is served as usual and starts the count again; so does a poll that is not a sighting.
- **Polling bounds:**
  - Polls start 2 s after a resume, run at most every 2 s, and only while a verified breakpoint exists.
  - Each poll is one `threads` request plus one-frame `stackTrace` requests: at most 256 threads (a rotating window in larger JVMs), the JVM's own threads and the rechecks, all within 1 s. With about 300 threads, a poll took 40–90 ms.
  - A slow or failed poll is no evidence. Launched JVMs are not polled: the loss has only been seen on attach.
  - This fails closed: while java-debug keeps suspending every thread (heavy class loading of the breakpoint's nested classes), a truly lost stop may never get 3 clean sightings, and the run times out as it did before.
- **Timing.** A real report arrives within milliseconds of the stop (an attached probe takes about 0.2 s end to end), so polls never run during a normal stop. A stop counts as lost only after at least 4 s without its report; the error comes 4–8 s after the stop (6.6 s for an attached probe whose first stop is lost).
- **The error** is `DEBUGGER_ERROR`. Its message names:
  - the thread (name and id);
  - where it is suspended (`Loop.twice(int) at Loop.java:5`);
  - the session's breakpoint (`src/demo/Loop.java:5`);
  - how long the stop has gone unreported, and how many checks saw it.

  Its hint says that java-debug did not report a stop it made, that the session's state cannot be trusted, and to close and retry.
- **What happens to the run:**
  - Peeky records the diagnosis (history entry `lost_stop`, and a `lost stop` note in `debugger_events.trace`) and detaches, so the JVM resumes and runs on.
  - **Probe:** fails with the error and carries `debugger_events`.
  - **Session:** the `wait` returns the error (`peeky session continue` and `step` wait too). Every later request on that session returns the same error, except `report`, which returns the session as it was at the failure (`state: detached`, `lost_stop`, `history`, `debugger_events`), and `close`, which ends it.
- **Unattended sessions:** with a pause budget (`--max-pause`, the default for attached JVMs), the worker polls even when no client is waiting, so a lost stop still ends in a detach.
- **Diagnostics:** the polls stay out of `debugger_events.trace`. A single merged `lost-stop poll` note there gives the number of polls (`times`), the threads checked, those suspended, and the suspect.

Tests drop the `stopped` event at Peeky's DAP client (`tests/java/test_java_attach.py`, `tests/unit/test_lost_stop.py`):
- an attached probe fails in about 6.6 s with the diagnosis, and the JVM runs to completion unchanged;
- a session fails the same way after a served hit, repeats its error, reports and closes, and the JVM completes;
- a step whose lost report ends on a verified breakpoint line never produces the error;
- under a class-load storm, a breakpoint that never ran is seen at its line in every poll but never produces the error, and the probe ends normally with 0 hits.
