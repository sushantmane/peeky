# Java attach (Venice end-to-end tests)

`peeky` can attach to a JVM that is already waiting for a debugger, for example a Gradle test JVM started with `--debug-jvm`. Peeky did not build that program, so provenance is reported as **attached**. Declared sources, the exact class files that map them, and the adapter bundle are hashed and rechecked before every operation, but Peeky cannot prove the JVM loaded those bytes. Attached sessions:

- are loopback-only (tunnel remote JVMs over SSH);
- read values and allow numeric/boolean root-local changes (evidence only);
- never verify edits (`peeky verify` refuses; verify with a fresh-build target);
- **detach without killing** the JVM, so the test continues after the probe.

Breakpoints may start pending until the class loads; the adapter verifies them when it does. Large JVMs emit thousands of DAP `thread` events; Peeky keeps only lifecycle events in its bounded history so stops are never lost.

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

Observed result: one probe captured both data-provider runs (`sendControlMessageDirectly=false` and `true`, each with its own `storeName`) in 28.5 s, then detached. Gradle reported both tests **PASSED**. Keep pauses short: these tests use a TestNG `timeOut` (30 s for this method), and a long stop fails the test.
