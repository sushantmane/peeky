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

Open issue: in one earlier run, the worker received no `stopped` event for about 200 s while the test thread sat at the breakpoint, so both tests timed out. This did not reproduce in two identical attempts. `session report` now includes `debugger_events` (recent lifecycle events and counts of ignored events), and `session close` keeps the worker log as `<name>.last.log` for diagnosis.
