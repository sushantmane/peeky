# Java attach (Venice end-to-end tests)

`peeky` can attach to a JVM that is already waiting for a debugger, for example a Gradle test JVM started with `--debug-jvm`. Peeky did not build that program, so provenance is reported as **attached**. Declared sources, the exact class files that map them, and the adapter bundle are hashed and rechecked before every worker operation, including `wait` (after each stop or termination) and `report`. Peeky still cannot prove the JVM loaded those bytes. Attached sessions:

- are loopback-only (tunnel remote JVMs over SSH);
- read values and allow numeric, boolean and char root-local changes, and Strings to a text, with a current adapter bundle (evidence only);
- never verify edits (`peeky verify` refuses; verify with a fresh-build target);
- **detach without killing** the JVM, so the test continues after the probe.

Breakpoints may start pending until the class loads; the adapter verifies them when it does. Large JVMs emit thousands of DAP `thread` events; Peeky keeps only lifecycle events in its bounded history, so thread churn cannot evict a stop. A genuine flood of lifecycle events still fails the session explicitly. A probe that ends without stopping reports `no-stop`.

## Venice walkthrough (verified 2026-10-14, Apple Silicon, JDK 17)

1. Build the adapter once: `python scripts/setup_java_adapter.py --output "$PWD/work/java-adapter"` (use an absolute path). Bundles built before the #27 fix are refused; rebuild them into a new directory.
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

In one earlier run, the worker received no `stopped` event for about 200 s while the test thread sat at the breakpoint, so both tests timed out. This did not reproduce in two identical attempts. The cause was java-debug's event thread dying ([root cause](#root-cause-java-debugs-event-thread-died)); the adapter is now patched, and Peeky fails fast with a diagnosis if it happens anyway ([Lost stop events](#lost-stop-events-27)). `session report` includes `debugger_events` (recent lifecycle events, counts of ignored events and a DAP trace), and `session close` keeps the worker log as `<name>.last.log` for diagnosis.

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

java-debug sometimes never sent `stopped` for an **attached** JVM, although the thread sat suspended at the breakpoint. This was seen once on Venice and twice in this repository's attach tests under heavy machine load. A probe or session then waited out its whole timeout (60 s or more).

### Root cause: java-debug's event thread died

The second adversarial review of PR #123 reproduced it naturally and found the cause (repros in the workspace's `work/vr27/churn/`).
- java-debug's "Event Hub" thread turns every JDI event into DAP stops, steps, thread and exit events. For each event it ran `logger.fine("JDI Event: " + event)` (`EventHub.java:71`), building the string even with FINE logging off.
- Building it calls `thread.name()`. For a short-lived thread the JVM had already collected, that throws `ObjectCollectedException`. Only `VMDisconnectedException` was caught, so the thread died: `Exception in thread "Event Hub" com.sun.jdi.ObjectCollectedException` on the adapter's stderr.
- From then on no JVM event was processed: no `stopped`, `breakpoint`, `thread`, `exited` or `terminated`. This probably explains the lost exits of launched JVMs too (#29).
- Under sustained churn, events backed up past 10,000, threads blocked in the JDWP agent, and a pending suspend-all class-load event froze the whole JVM.
- Two threads starting short-lived threads in a 32 MB heap with constant garbage reproduce it within half a second (`tests/java/test_java_attach.py`, `CHURN`).

**The fix.** `scripts/setup_java_adapter.py` patches the pinned `EventHub.java` before compiling (`eventhub-log-guard`): it logs only when FINE is loggable, and catches any `RuntimeException` around the log call. The patch fails loudly if the code it changes is not found exactly once. Peeky checks the compiled bundle itself: the patched `EventHub.class` calls `Logger.isLoggable`, the old one does not. It refuses a bundle without it (`UNSUPPORTED`, with the rebuild command), whatever the bundle's `adapter.json` lists under `patches` (kept as documentation). With the patch, the review's churn runs completed 18 of 18 times, with stops reported in 16–812 ms. The CI lanes build a fresh bundle with the script, so they use the patched adapter.

### Two detectors fail fast if it happens anyway

**A dead event thread.** This applies to launched and attached JVMs.
- **Detection.** Peeky keeps the adapter's stderr (the last 16 KiB, in `debugger_events.stderr`). When it shows `Exception in thread "Event Hub"`, from an unpatched bundle or an exception escaping one of java-debug's event handlers, any wait ends at once with `DEBUGGER_ERROR`: `java-debug's event thread died (<the line> at <java-debug's own frame>); it reports no more stops, steps or exits, so the session cannot continue: close it and retry`.
- **Scanning.** The line is found anywhere in the stream: deep in an overlong line, split across writes, or as a last line without a newline. Stack frames are collected until stderr is quiet for 0.2 s (1 s at most). If the adapter exits right after printing the line, that diagnosis is the error, not "adapter closed".
- **What happens next.** Peeky notices within about a second, even with no client waiting and with or without a pause budget. It detaches an attached JVM or ends a launched one, and keeps the session as a failed one, as for a lost stop:
  - the history gets `adapter_failed`;
  - later requests repeat the error;
  - `report` returns the final state (`state: closed`, `adapter_failure`, `debugger_events` with the stderr tail), and `close` ends it;
  - a probe carries those `debugger_events`.
- **Coverage.** It also covers what the poll below cannot: several threads stopped at once, or threads blocked in the JDWP agent.

**A stop left unreported** (attached JVMs). Peeky detects a stop java-debug made but never reported and **fails fast with `DEBUGGER_ERROR`**, naming the evidence. It never serves such a stop: a stop the debugger did not report cannot be trusted, and serving it led to wrong hits in the first review.

- **What java-debug suspends.** java-debug lists stack frames only for threads the debugger suspended (a running thread's `stackTrace` is empty). Two things suspend threads:
  - the session's line breakpoints and steps suspend one thread each;
  - java-debug's own class-load requests (`Breakpoint.install`, for the breakpoint's class and its nested classes) suspend **every** thread while java-debug looks for the line. A thread blocked entering `synchronized` then shows the line inside the block, even though that line never ran.

  Nothing else does: Peeky's Java adapter refuses `pause`, exception and function breakpoints, conditions and log points, and a JVM accepts one debugger at a time.
- **Exact criteria.** A poll is a *sighting* only when all of these hold:
  - the session is running, and the debugger has reported no stop or exit that Peeky has not consumed;
  - exactly one thread without a step in flight is suspended. Threads with a step in flight are ignored: a step that ends on a breakpoint line is reported as a step, so a lost step report must never look like a breakpoint stop;
  - that thread's top frame is at the resolved source path and line of a breakpoint the session set, and the debugger **verified** it (in its `setBreakpoints` reply or a later `breakpoint` event);
  - every other thread checked is running. The checks cover the JVM's own threads with Java frames (Reference Handler, Finalizer, Common-Cleaner; Signal Dispatcher has none) before and after the rest, every other thread in the poll's window, and the suspect twice more, each time followed by the JVM's own threads again. Any other suspended thread means a suspend-all event, and the poll is no evidence.

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
- **Unattended sessions:** with a pause budget (`--max-pause`, the default for attached JVMs), the worker polls even when no client is waiting, so a lost stop still ends in a detach. A breakpoint pending at launch (a `suspend=y` JVM such as Gradle's `--debug-jvm`, where the class loads later) is verified only by a `breakpoint` event, so the worker first takes those events. It takes only those that come before any stop or exit. When a poll is due, its own internal `wait` may take a stop or exit that arrived just after; the client's next `wait` still reports it once, with the same `stop_id` and hit. The worker lock keeps the guard and a client apart. A failed adapter connection is left for the client's next request, which gets `DEBUGGER_ERROR`. If the guard itself meets a debugger failure, for example resuming a stop past its budget, it closes the session and keeps it as a failed one, so that request still gets the error, never `NO_SESSION`.
- **Pause.** A lost stop holds its thread for about 4–8 s, until the detach, whatever `--max-pause` says: the budget can only resume stops that were reported.
- **Seen, then the program ended.** If a poll saw a thread alone at the breakpoint and the program then ended before the evidence was complete, the line did run. A probe then fails with `DEBUGGER_ERROR` (`The program ended after thread 'main' was seen paused at …`), or warns if it captured other hits; it never reports `NO_STOP`. A session's final state carries `unreported_stop`, and the history gets an `unreported_stop` entry.
- **Diagnostics:** the polls stay out of `debugger_events.trace`. A single merged `lost-stop poll` note there gives the number of polls (`times`), the threads checked, those suspended, and the suspect.

The lost-stop tests drop the `stopped` event at Peeky's DAP client (`tests/java/test_java_attach.py`, `tests/unit/test_lost_stop.py`):
- an attached probe fails in about 6.6 s with the diagnosis, and the JVM runs to completion unchanged;
- a session fails the same way after a served hit, repeats its error, reports and closes, and the JVM completes;
- a step whose lost report ends on a verified breakpoint line never produces the error;
- under a class-load storm, a breakpoint that never ran is seen at its line in every poll but never produces the error, and the probe ends normally with 0 hits.

The event-thread tests (`tests/unit/test_adapter_stderr.py`, `tests/java/test_java_attach.py`):
- a fake java-debug that prints the death ends a waiting session in under 2 s, attached (detached) or launched (terminated);
- the failed session repeats its error and reports the stderr tail, and a probe carries it;
- the death is found after 6000 bytes of one line, written byte by byte, without a final newline, and when the adapter exits right after it;
- `close()` stops the stderr reader even while a descendant holds the pipe, and a later file reusing its descriptor keeps its own data (also checked on Linux).
- the churn scenario against the real, patched adapter reports the stop normally;
- with `PEEKY_JAVA_ADAPTER_UNPATCHED` set to a bundle built before the patch, the same scenario fails with the event-thread error in under 5 s, and the JVM runs on after the detach.
