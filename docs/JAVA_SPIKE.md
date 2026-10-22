# Java headless DAP feasibility

> **Historical:** the 2026-10-14 feasibility record for Java, kept as it was. Its limits describe
> the first scratch experiment, not Peeky today. For current behaviour, see [Java](lang/java.md),
> the [Java cookbook](lang/java-cookbook.md), [JAVA_AGENT.md](JAVA_AGENT.md) and
> [JAVA_ATTACH.md](JAVA_ATTACH.md).

Status: **feasibility passed locally; Java is not yet integrated or supported by Peeky.** Tested on macOS ARM64 with Microsoft OpenJDK 17.0.19. The experiment uses the real Microsoft Java Debug Server core, a deliberately limited provider shim, and a standalone DAP client. No Peeky worker/controller code was changed.

## Observed result

A Java fixture initializes `int timeoutMs = 0` and a `String label`, then branches on the integer. A normal JVM run prints `request:fallback`. The DAP experiment:

1. Launches the fixture's compiled class through the Java adapter.
2. Sets a breakpoint at line 5, immediately before the branch.
3. Receives an initially unverified breakpoint followed by a verified breakpoint event at line 5; the stopped stack also identifies that line and source path.
4. Reads `timeoutMs = 0` and `label = "request"` from locals.
5. Sends DAP `setVariable` with `timeoutMs = 5000`; receives the integer value back.
6. Continues and receives `request:success` in an output event, then termination.
7. Confirms that both Java source and compiled class SHA-256 stayed unchanged during the runtime experiment.

Three consecutive complete probes passed in 0.202, 0.207, and 0.211 seconds. These are local feasibility timings, not hosted CI measurements. This is evidence for runtime intervention only; it does not verify a permanent source fix.

## Provenance and setup

The inspected Microsoft source is pinned to commit [`6a597f2bcccdd0aac7e91631588361826fde11f3`](https://github.com/microsoft/java-debug/tree/6a597f2bcccdd0aac7e91631588361826fde11f3), whose parent POM reports version 0.53.2. The unmodified core sources compile directly with JDK 17. The core's [POM](https://github.com/microsoft/java-debug/blob/6a597f2bcccdd0aac7e91631588361826fde11f3/com.microsoft.java.debug.core/pom.xml) declares the following runtime dependencies, downloaded only from Maven Central over HTTPS. Published SHA-1 values were checked at acquisition; the stronger SHA-256 identities below pin the exact bytes for subsequent setup.

| Maven dependency | SHA-256 |
| --- | --- |
| org.apache.commons:commons-lang3:3.18.0 | `4eeeae8d20c078abb64b015ec158add383ac581571cddc45c68f0c9ae0230720` |
| com.google.code.gson:gson:2.8.9 | `d3999291855de495c94c743761b8ab5176cfeabe281a5ab0d8e8d45326fd703e` |
| io.reactivex.rxjava2:rxjava:2.2.21 | `59df6541a840018f0f4c899aae4f4c1f4383f4c16feb5268615fbe384d28501c` |
| org.reactivestreams:reactive-streams:1.0.4 | `f75ca597789b3dac58f61857b9ac2e1034a68fa672db35055a8fb4509e325f28` |
| commons-io:commons-io:2.14.0 | `e35d1df7232568ed4b81d9a9ed94a0159b3ff6cf1915cc21eef5cb55d7b157a2` |

Acquiring those five dependencies took 2.024 seconds locally; compiling the core alone took 0.85 seconds. A subsequent full core/shim/fixture build with dependencies present took 1.304 seconds. Network and runner variability have not been characterized. The exploratory upstream Maven reactor attempt is excluded from these successful-path numbers.

The experiment is preserved in ignored local `work/java-spike/`: `Headless.java`, `BranchBug.java`, `probe.py`, `build.py`, `dependencies.json`, the pinned source checkout, and `result*.json`/`transcript.json`/`adapter.stderr`. Those scratch files are not shipped in the repository; promote a reviewed launcher and setup script as part of the actual Java implementation.

Reproduce in the existing spike workspace from the repository root:

```sh
java -version
javac -version
git -C work/java-spike/java-debug rev-parse HEAD
python3 work/java-spike/build.py
java -cp work/java-spike/classes BranchBug
python3 work/java-spike/probe.py
```

`build.py` checks the source commit and dependency SHA-256s, compiles unmodified core sources, compiles the small launcher, and compiles the fixture with `javac -g`. `probe.py` speaks DAP framing over subprocess stdio with bounded response waits and process-group cleanup in `finally`. It asserts the verified breakpoint event, observed locals, mutation response, output, and unchanged source/class hashes. This is a POSIX/macOS feasibility harness, not a portable product test suite.

## Why headless startup is possible

The [official README](https://github.com/microsoft/java-debug/blob/6a597f2bcccdd0aac7e91631588361826fde11f3/README.md) describes the supported editor integration: load the debug plugin into JDT LS, send `vscode.java.startDebugSession`, then connect DAP to the returned port. That is a viable fuller-featured route, but it needs a language-server lifecycle as well as the debugger lifecycle.

The core also exposes `ProtocolServer(InputStream, OutputStream, IProviderContext)`. The spike provides the five services required to launch: source lookup, VM manager, evaluation, hot-code replacement, and completions. VM management delegates to the JDK JDI bootstrap. Source lookup is deliberately specific to one default-package fixture. Evaluation/HCR/completion implementations reject unsupported execution or return empty/no-event results where the core requires registration.

The upstream Maven command below failed on local JDK 17 before building the core: Tycho 5.0.0 contains class-file version 65 (Java 21), while the available runtime accepts up to version 61 (Java 17).

```sh
cd work/java-spike/java-debug
./mvnw -pl com.microsoft.java.debug.core -am package -DskipTests -Dcheckstyle.skip -Dmaven.repo.local=../m2
```

This is a build-tool mismatch, not a failure of core DAP/JDI on JDK 17. Current [JDT LS documentation](https://github.com/eclipse-jdtls/eclipse.jdt.ls#requirements) also requires Java 21 for the server runtime. A JDT LS integration can use a separate newer host JDK while debugging an older target JVM; that configuration has not been tested here.

Microsoft java-debug uses EPL 1.0; JDT LS uses EPL 2.0. Dependency notices and redistribution obligations need to accompany any packaged adapter. The spike leaves upstream sources and downloaded dependencies in scratch and publishes nothing.

## Product decision and remaining risks

Prefer a narrowly scoped launcher around the existing Microsoft core for the next Java slice, subject to a source-resolution implementation and independent review. Do not implement a new JDI debugger protocol just to reproduce already working DAP operations. Keep the JDT LS route available if robust project/source discovery or evaluation makes the standalone provider maintenance too expensive.

The following are required before claiming Java support:

- Replace the hardcoded `BranchBug` class mapping with explicit source/class/build metadata. Test packages, nested classes, multiple source files, missing debug information, breakpoint relocation, and stale source/class pairs. Returning a guessed class name is not an acceptable production resolver.
- Mask unsupported adapter capabilities. The upstream core advertises expression evaluation, completions, conditional breakpoints, logpoints, and other operations regardless of this shim's incomplete providers. Peeky must expose only tested capabilities, reject unsupported requests before dispatch, and avoid implicit user-code invocation during inspection.
- Extract an adapter boundary in Peeky; retain session/stop identity and fail-closed semantics in the worker. Do not copy debugpy-specific scope-name assumptions into Java.
- Add artifact provenance and explicit `javac -g` build/independent verification recipes. A hash of a class file alone does not prove it corresponds to a particular source revision.
- Test integer mutation plus representative reference/array inspection, adapter death, child cleanup, timeouts, stale IDs, and separate-process HTTP operation. The spike's normal shutdown is not evidence for all cleanup/error paths.
- Add an injected-provider agent test that proposes a Java source change, rebuilds in isolation, and passes the unchanged independent verifier in a fresh JVM. No live-model calls in required CI.
- Audit/version the old upstream dependency set before production distribution. The spike's matching of upstream pins is provenance, not a security audit.

## CI plan

Add an independent `java integration` lane alongside existing lanes, using a pinned JDK and commit/checksum-pinned adapter inputs. Cache adapter compilation and Maven dependency jars keyed by source commit, dependency manifest, JDK, and OS. Keep tiny `javac -g` fixtures and compile candidates in fresh temporary directories. This lane must run on shared worker/protocol changes and must not depend on Python or native lane completion.

The observed work is compatible with a four-minute job cutoff and under-five-minute critical-path target, but no hosted cold-cache guarantee is established. Measure a cold Linux acquisition/build/probe run before making it required. If acquisition dominates, retain a versioned private adapter bundle with manifest and notices or use an explicitly maintained runner image; continue to run essential Java conformance on each change. Do not rebuild the full JDT LS/Tycho ecosystem on every PR merely to launch a tiny JVM fixture.
