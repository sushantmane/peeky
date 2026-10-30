# Java attach (Venice end-to-end tests)

Peeky can attach to a JVM that is already waiting for a debugger, such as a Gradle test JVM
started with `--debug-jvm`. The basics are in the Java cookbook: the attach config, and what
attaching changes
([Java 7.4](lang/java-cookbook.md#74-java-tests-already-running-gradle---debug-jvm-eg-venice)).
This page has the details:

- what an attached session can and can't do;
- the Venice walkthrough, and breakpoints in Venice's production code;
- how Peeky handles the events that java-debug, the Java debug adapter, loses.

## What an attached session can do

Peeky didn't build the program, so it reports the provenance as **attached**.

- Peeky hashes the declared sources, the exact class files that map them, and the adapter bundle.
  It checks them again before every worker operation, including `wait`, after each stop or end,
  and `report`.
- Peeky still can't prove that the JVM loaded those bytes.

Attached sessions:

- are loopback-only. Tunnel a remote JVM over SSH.
- read values.
- change values with a current adapter bundle, as the next section says. Changes are evidence
  only.
- never verify edits. `peeky verify` refuses; verify with a fresh-build target instead.
- **detach without killing** the JVM, so the test goes on after the probe.

### What a change can reach

An attached session can change:

- numeric, boolean and char root locals, and Strings to a text;
- eligible fields and array elements, such as `current.end=10` or `arr[0]=1`. The
  [table](lang/java-cookbook.md#what-can-be-set) gives the conditions.

It never changes JDK state:

- a field that a JDK class declares, inherited or not
  ([rule](lang/java-cookbook.md#fields-the-jdk-declares));
- an element of an array that a JDK class's field holds, directly or inside another array
  ([rule](lang/java-cookbook.md#arrays-the-jdk-holds)). The backing arrays of the program's own
  `ArrayList`, `Vector`, `ArrayDeque` and `Arrays.asList` lists are the exception.

To tell who holds an array, Peeky asks the JVM what refers to it. It refuses the change when it
can't tell, as the [Java cookbook](lang/java-cookbook.md#when-peeky-cant-tell-who-holds-an-array)
details. The search must also fit in the session's pause budget, which is 10 s by default for an
attached JVM.

### A small example

This `demo/Loop.java` is compiled with `javac -g -d classes demo/Loop.java`:

```java
package demo;

public class Loop {
    static int twice(int n) {
        int doubled = n * 2;
        return doubled;
    }

    public static void main(String[] args) {
        int total = 0;
        for (int i = 1; i <= 3; i++) {
            total += twice(i);
        }
        System.out.println("TOTAL:" + total);
    }
}
```

Build the adapter bundle once, with `peeky setup-java`
([Java 7.3](lang/java-cookbook.md#build-the-adapter)). Start the JVM waiting for a debugger on
port 5005, and describe it in a trusted config, `attach.json`. Without `bundle`, it uses the bundle
`peeky setup-java` built:

```sh
java -agentlib:jdwp=transport=dt_socket,server=y,suspend=y,address=127.0.0.1:5005 -cp classes demo.Loop
```

```json
{
  "port": 5005,
  "sources": ["demo/Loop.java"],
  "class_roots": ["classes"]
}
```

Then probe it:

```
$ peeky probe . --config attach.json --at demo/Loop.java:6 --capture n,doubled --hits 3
hit  phase     line  function         selector  type  value
1    observed  6     Loop.twice(int)  n         int   1  [not settable: not read again after this stop]
1    observed  6     Loop.twice(int)  doubled   int   2  [settable]
2    observed  6     Loop.twice(int)  n         int   2  [not settable: not read again after this stop]
2    observed  6     Loop.twice(int)  doubled   int   4  [settable]
3    observed  6     Loop.twice(int)  n         int   3  [not settable: not read again after this stop]
3    observed  6     Loop.twice(int)  doubled   int   6  [settable]
status: completed  hits: 3  seconds: 0.543  max pause: 43.6 ms
evidence: saved as run 1; query it with `peeky queries run --param run=1` or `peeky sql` (see `peeky sql --schema`)
```

The probe returned right after its third hit, and detached. The JVM ran on, and printed
`TOTAL:12` itself. A verify on the same config is refused:

```
$ peeky verify . --config attach.json --at demo/Loop.java --check check.py --old 'n * 2' --new 'n * 3'
peeky: [UNSUPPORTED] Attached JVMs cannot verify edits; verify with a fresh build target
  hint: Verify on a fresh-build target, not an attached JVM.
```

### Breakpoints and events

- A breakpoint may start pending until its class loads. The adapter verifies it when the class
  loads.
- Large JVMs emit thousands of DAP `thread` events. Peeky keeps only lifecycle events in its
  bounded history, so thread churn can't push a stop out.
- A real flood of lifecycle events still fails the session, with an explicit error.
- A probe that ends without stopping reports `no-stop`.

## Venice walkthrough (verified 2026-10-14, Apple Silicon, JDK 17)

[Venice](https://github.com/linkedin/venice) is LinkedIn's open-source derived-data platform. To
reproduce the runs on this page, you need:

- a Venice checkout at `~/workspace/venice`, at commit `ba78edda8`. Every run here used it, and
  the line numbers below match it;
- JDK 17, and Venice's own Gradle wrapper, which downloads Venice's dependencies on its first
  build;
- on Apple Silicon, Rosetta 2 (step 2);
- port 5005 free;
- Peeky installed, to build the adapter bundle (step 1).

1. Build the adapter once: `peeky setup-java`. Bundles built before the event-thread patch
   ([The fix](#the-fix)) are refused. Run `peeky setup-java` again to replace one.
2. Build Venice's tests. On Apple Silicon, this needs Rosetta 2
   (`softwareupdate --install-rosetta`), because grpc-java ships its macOS code generator for
   x86_64 only. The runs here used a natively built generator instead, which changes only how
   Venice is built.
   ```sh
   cd ~/workspace/venice && ./gradlew assemble :internal:venice-test-common:integrationTestClasses
   ```
3. Start one end-to-end test, suspended on port 5005. This forces a rerun, and bypasses the build
   cache:
   ```sh
   ./gradlew --no-build-cache \
     :internal:venice-test-common:cleanIntegrationTests_32 :internal:venice-test-common:integrationTests_32 \
     --tests 'com.linkedin.venice.endToEnd.TestEmptyPush.testEmptyPushWithZstdWithDictCompression' --debug-jvm
   ```
   The shard, `_32`, comes from `internal/venice-test-common/test-shard-assignments.json`.
4. Describe the attach target in a trusted config, such as
   `~/.peeky/venice/attach-TestEmptyPush.json`:
   ```json
   {
     "port": 5005,
     "sources": ["internal/venice-test-common/src/integrationTest/java/com/linkedin/venice/endToEnd/TestEmptyPush.java"],
     "class_roots": ["internal/venice-test-common/build/classes/java/integrationTest"]
   }
   ```
   To break inside Venice itself, add production sources and their `build/classes/java/main`
   directories ([below](#breakpoints-in-production-code)).
5. Ask questions:
   ```sh
   T=internal/venice-test-common/src/integrationTest/java/com/linkedin/venice/endToEnd/TestEmptyPush.java
   peeky describe ~/workspace/venice --config ~/.peeky/venice/attach-TestEmptyPush.json --at $T
   peeky probe ~/workspace/venice --config ~/.peeky/venice/attach-TestEmptyPush.json \
     --at $T:85 --capture storeName,sendControlMessageDirectly,controllerClient --hits 2 --timeout 240
   ```

The result: one probe captured both data-provider runs in 28.5 s, then detached. They were
`sendControlMessageDirectly=false` and `true`, each with its own `storeName`. Gradle reported both
tests **PASSED**.

### Timed tests

These tests use a TestNG `timeOut`, 30 s for this method, and a long stop fails them
([ADR 0006](adr/0006-agent-speed.md)):

- **Use probes.** Each hit pauses the JVM for milliseconds (`pauses_ms`). On an attached JVM, a
  probe returns as soon as its last hit is captured: `--until hits` is the default there.
- **Sessions on attached JVMs auto-resume** any stop after 10 s (`--max-pause`; `0` turns this
  off). An operation on a resumed stop fails with `STALE_STOP`, and says why. `inspect` shows
  `paused_s` and `resume_in_s`.
- **Long waits:** if Gradle's startup outlasts your tool's timeout, add `--background`, and poll
  with `peeky job wait <id>`.

Verified on Venice `ba78edda8` (2026-10-15):

- **Background probe:** `--background --hits 2` returned a job id at once. `job wait` delivered
  both hits in 19.2 s, against 28.5 s when waiting for the JVM to exit. Each pause lasted
  100–115 ms, and both tests PASSED.
- **Slow-agent session,** with 15 s of "thinking" after the stop: each stop was auto-resumed after
  10 s. Both tests PASSED, in about 13 s each.
- **Absent agent,** with no `wait` after the start: both stops were auto-resumed at about 10.1 s,
  as the audit records. Both tests PASSED.

### Breakpoints in production code

To break inside Venice's code, declare production sources together with their
`build/classes/java/main` roots. Client code works, and so does server code that runs in the test
JVM. `peeky describe` without `--at` lists the breakpoint lines of every declared source.

```json
{
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

Verified on 2026-10-15. Each run captured both data-provider invocations and returned at the last
hit. Both tests PASSED.

| Breakpoint | Values captured | Pause per hit | Probe time |
| --- | --- | --- | --- |
| `ControllerClient.java:594` (client library, inside `createNewStore`) | `storeName`, `owner` | 145–153 ms | 18.3 s |
| `CreateStore.java:64` (controller HTTP handler, on a Jetty thread) | `clusterName`, `storeName`, `owner`, `isSystemStore=false` | 178–186 ms | 21.4 s |

java-debug can fail to report a stop: the test thread then sits at the breakpoint until the test
times out.

- The cause is java-debug's event thread dying
  ([root cause](#root-cause-java-debugs-event-thread-died)).
- The adapter is patched. If it happens anyway, Peeky fails fast with a diagnosis
  ([Lost stop events](#lost-stop-events)).
- For diagnosis, `session report` includes `debugger_events`: recent lifecycle events, counts of
  ignored events, and a DAP trace.
- `session close` keeps the worker log as `<name>.last.log`.

## Dropped exit events

java-debug can fail to send `exited` or `terminated` after a **launched** JVM finishes. The
program's output has arrived, and the adapter is alive but silent. So Peeky checks liveness for
launched Java targets. What you see is
in [Java: when java-debug itself fails](lang/java.md#when-java-debug-itself-fails).

- **When it checks:** after 2 quiet seconds, Peeky asks the adapter for `threads`.
- **Why one empty reply isn't enough:** java-debug can answer with an empty list for a live JVM
  whose threads churn. A thread may be collected mid-lookup.
- **When Peeky infers an exit:**
  - if java-debug announced the JVM's pid, in its `processid` event, only once that process is
    gone;
  - without a pid, only when a second empty reply follows at least 2 s later. The count of empty
    replies starts again for every session.
- **The result:** the run ends as `terminated`, with `exit_inferred: true`, an audit entry and a
  warning.
- **Slow or failed checks** count as "still running", and never close the session.
- **Real events win:** a stop or an exit that arrives during the check is used instead of the
  inference.
- **Attached JVMs are left out:** their death closes the JDWP connection, and a false inference
  would detach a live JVM.

A real-JDK test (`tests/java/test_java_liveness.py`) runs a JVM whose threads churn hard. A rule of
one empty reply would declare it exited. Peeky never reports it as exited.

An attached JVM's lost `stopped` event is handled separately, below.

## Lost stop events

java-debug can fail to send `stopped` for an **attached** JVM, although the thread is suspended at
the breakpoint. Without a check, a probe or session would wait out its whole timeout: 60 s by
default for a probe. What you see, and what to do, is in
[ERRORS: Two Java debugger failures](ERRORS.md#two-java-debugger-failures).

### Root cause: java-debug's event thread died

The cause:

- java-debug's "Event Hub" thread turns every JDI event into DAP stops, steps, thread and exit
  events.
- For each event, it ran `logger.fine("JDI Event: " + event)` (`EventHub.java:71`). That builds
  the string even with FINE logging off.
- Building it calls `thread.name()`. For a short-lived thread the JVM had already collected, that
  throws `ObjectCollectedException`.
- Only `VMDisconnectedException` was caught, so the thread died. The adapter's stderr shows:
  `Exception in thread "Event Hub" com.sun.jdi.ObjectCollectedException`.
- From then on, no JVM event was processed: no `stopped`, `breakpoint`, `thread`, `exited` or
  `terminated`. This probably explains the lost exits of launched JVMs too.
- Under sustained churn, events backed up past 10,000, and threads blocked in the JDWP agent. A
  pending suspend-all class-load event then froze the whole JVM.
- Two threads that start short-lived threads, in a 32 MB heap with constant garbage, reproduce it
  within half a second (`tests/java/test_java_attach.py`, `CHURN`).

### The fix

`peeky setup-java` patches the pinned `EventHub.java` before compiling it
(`eventhub-log-guard`):

- It logs only when FINE is loggable, and catches any `RuntimeException` around the log call.
- The patch fails loudly if the code it changes isn't found exactly once.
- Peeky checks the compiled bundle itself: the patched `EventHub.class` calls
  `Logger.isLoggable`, and the old one doesn't.
- Peeky refuses a bundle without the patch, with `UNSUPPORTED` and the rebuild command. This holds
  whatever the bundle's `adapter.json` lists under `patches`, which is kept as documentation.
- Peeky also requires the bundle's recorded recipe, which holds each patch's text, to be the one
  it builds ([Java 7.3](lang/java-cookbook.md#build-the-adapter)).
- CI builds a fresh bundle with `scripts/setup_java_adapter.py`, which runs the same setup, so it
  tests the patched adapter.

### Two detectors fail fast if it happens anyway

Both end with `DEBUGGER_ERROR`.

#### A dead event thread

This applies to launched and attached JVMs.

- **Detection:** Peeky keeps the last 16 KiB of the adapter's stderr, in
  `debugger_events.stderr`. When it shows `Exception in thread "Event Hub"`, any wait ends at
  once. That line can come from an unpatched bundle, or from an exception escaping one of
  java-debug's event handlers. The message is:

  ```
  java-debug's event thread died (<the line> at <java-debug's own frame>); it reports no more stops, steps or exits, so the session cannot continue: close it and retry
  ```

- **Scanning:** the line is found anywhere in the stream. It may be deep in an overlong line,
  split across writes, or a last line without a newline. Stack frames are collected until stderr
  is quiet for 0.2 s, and for 1 s at most.
- If the adapter exits right after printing the line, the error is that diagnosis, not "adapter
  closed".
- **Timing:** Peeky notices within about a second, even with no client waiting, and with or
  without a pause budget.
- **What happens next:** Peeky detaches an attached JVM, or ends a launched one. It keeps the
  session as a failed one, as for a lost stop. The history gets `adapter_failed`, and a probe
  carries the `debugger_events`. [ERRORS](ERRORS.md#java-debugs-event-thread-died-) has the rest.
- **Coverage:** it also covers what the poll below can't: several threads stopped at once, or
  threads blocked in the JDWP agent.

#### A stop left unreported

This applies to attached JVMs. Peeky detects a stop that java-debug made but never reported. It
**fails fast with `DEBUGGER_ERROR`**, naming the evidence. It never serves such a stop: a stop the
debugger didn't report can't be trusted.

**What java-debug suspends.** java-debug lists stack frames only for threads the debugger
suspended. A running thread's `stackTrace` is empty. Two things suspend threads:

- the session's line breakpoints and steps, which suspend one thread each;
- java-debug's own class-load requests (`Breakpoint.install`, for the breakpoint's class and its
  nested classes). These suspend **every** thread while java-debug looks for the line. A thread
  blocked entering `synchronized` then shows the line inside the block, although that line never
  ran.

Nothing else does. Peeky's Java adapter refuses `pause`, exception and function breakpoints,
conditions and log points. And a JVM accepts one debugger at a time.

**Exact criteria.** A poll is a *sighting* only when all of these hold:

- The session is running, and the debugger has reported no stop or exit that Peeky hasn't used.
- Exactly one thread without a step in flight is suspended. Threads with a step in flight are
  ignored: a step that ends on a breakpoint line is reported as a step. So a lost step report must
  never look like a breakpoint stop.
- That thread's top frame is at the resolved source path and line of a breakpoint the session
  set. The debugger **verified** that breakpoint, in its `setBreakpoints` reply or a later
  `breakpoint` event.
- Every other thread checked is running. Any other suspended thread means a suspend-all event, and
  the poll is no evidence.

The checks cover:

1. the JVM's own threads with Java frames: Reference Handler, Finalizer and Common-Cleaner.
   Signal Dispatcher has none;
2. every other thread in the poll's window;
3. the JVM's own threads again;
4. the suspect, twice more, each time followed by the JVM's own threads again.

Evidence is the same thread at the same place in **3 consecutive sightings, spanning at least
4 s**, with no report in between. A real report that arrives meanwhile, even during a poll, is
served as usual, and starts the count again. So does a poll that isn't a sighting.

**Polling bounds:**

- Polls start 2 s after a resume, run at most every 2 s, and only while a verified breakpoint
  exists.
- Each poll is one `threads` request, plus one-frame `stackTrace` requests, all within 1 s. They
  cover at most 256 threads, in a rotating window in larger JVMs, plus the JVM's own threads and
  the rechecks.
- A slow or failed poll is no evidence.
- Launched JVMs aren't polled: the loss has only been seen on attach.
- This fails closed. java-debug may keep suspending every thread, as under heavy class loading of
  the breakpoint's nested classes. Then a truly lost stop may never get 3 clean sightings, and the
  run times out, as without the detector.

**Timing.** A real report arrives within milliseconds of the stop. So polls never run during a
normal stop. A stop counts as lost only after at least 4 s without its report. The error comes
4–8 s after the stop.

**The error** is `DEBUGGER_ERROR`. Its message names:

- the thread, by name and id;
- where it is suspended: `Loop.twice(int) at Loop.java:5`;
- the session's breakpoint: `src/demo/Loop.java:5`;
- how long the stop has gone unreported, and how many checks saw it.

Its hint says that java-debug didn't report a stop it made, that the session's state can't be
trusted, and to close and retry.

**What happens to the run.** Peeky records the diagnosis, and detaches, so the JVM resumes and
runs on. The diagnosis is a `lost_stop` history entry, and a `lost stop` note in
`debugger_events.trace`.

- **A probe** fails with the error, and carries `debugger_events`.
- **A session's** `wait` returns the error. `peeky session continue` and `step` wait too, so they
  return it as well.
- Every later request on that session returns the same error, except `report` and `close`
  ([ERRORS](ERRORS.md#lost-stop-)). `report` returns the session as it was at the failure:
  `state: detached`, `lost_stop`, `history` and `debugger_events`. `close` ends it.

**Unattended sessions.** With a pause budget, `--max-pause`, the worker polls even when no client is
waiting. So a lost stop still ends in a detach. Attached JVMs have a pause budget by default.

- A breakpoint pending at launch is verified only by a `breakpoint` event. That happens with a
  `suspend=y` JVM, such as Gradle's `--debug-jvm`, where the class loads later.
- So the worker first takes those events, and only those that come before any stop or exit.
- When a poll is due, its own internal `wait` may take a stop or exit that arrived just after. The
  client's next `wait` still reports it once, with the same `stop_id` and hit.
- The worker lock keeps the guard and a client apart.
- A failed adapter connection is left for the client's next request, which gets
  `DEBUGGER_ERROR`.
- If the guard itself meets a debugger failure, such as resuming a stop past its budget, it closes
  the session. It keeps the session as a failed one, so that request still gets the error, never
  `NO_SESSION`.

**Pause.** A lost stop holds its thread for about 4–8 s, until the detach, whatever `--max-pause`
says. The budget can only resume stops that were reported.

**Seen, then the program ended.** A poll may see a thread alone at the breakpoint, and the program
then ends before the evidence is complete. The line did run then.

- A probe fails with `DEBUGGER_ERROR`, or warns if it captured other hits. It never reports
  `NO_STOP`. The message starts:
  `The program ended after thread 'main' was seen paused at …`.
- A session's final state carries `unreported_stop`, and the history gets an `unreported_stop`
  entry.
- A change the probe never made at that line says so. Its warning says the line's full count of
  stops is unknown, not that its hit never came. Its `unapplied` entry has `unreported_stop: true`.
  An `@all` there missed that stop, so it is listed too, with `made`, and the probe exits 4.
- If Peeky can't match that stop to a line, it may have been any pending change's. Each says so,
  and is listed without `unreported_stop`.

**Diagnostics.** The polls stay out of `debugger_events.trace`. A single merged `lost-stop poll`
note there gives the number of polls (`times`), the threads checked, those suspended, and the
suspect.

### What the tests cover

The lost-stop tests drop the `stopped` event at Peeky's DAP client
(`tests/java/test_java_attach.py`, `tests/unit/test_lost_stop.py`):

- An attached probe fails in under 9 s with the diagnosis, and the JVM runs to completion
  unchanged.
- A session fails the same way after a served hit. It repeats its error, reports and closes, and
  the JVM completes.
- A step whose lost report ends on a verified breakpoint line never produces the error.
- Under a class-load storm, a breakpoint that never ran is seen at its line in every poll. It never
  produces the error, and the probe ends normally with 0 hits.

The event-thread tests (`tests/unit/test_adapter_stderr.py`, `tests/java/test_java_attach.py`):

- A fake java-debug that prints the death ends a waiting session in under 2 s: attached, the
  session is detached; launched, it is terminated.
- The failed session repeats its error and reports the stderr tail, and a probe carries it.
- The death is found after 6000 bytes of one line, and when written byte by byte. It is also
  found without a final newline, and when the adapter exits right after it.
- `close()` stops the stderr reader, even while a descendant holds the pipe. A later file that
  reuses its descriptor keeps its own data. This is also checked on Linux.
- The churn scenario against the real, patched adapter reports the stop normally.
- With `PEEKY_JAVA_ADAPTER_UNPATCHED` set to a bundle built before the patch, the same scenario
  fails with the event-thread error in under 5 s. The JVM runs on after the detach.
