# Experimental Java DAP launcher

This launcher wraps Microsoft java-debug's core with narrow service providers. `peeky setup-java` builds it, from this directory, which ships in Peeky's package. Setup applies one source patch to that core (`eventhub-log-guard`; see `peeky/java_setup.py`). It is qualified by packaged and nested-class fixtures and connects to the experimental [Java worker](../../../docs/WORKER.md#start-a-java-worker). Use JDK 17 or later (CI uses 17), Python 3.11+, and HTTPS access to GitHub/Maven Central for setup.

With Peeky installed, from a wheel too:

```sh
peeky setup-java
```

That installs Peeky's own bundle in `$PEEKY_STATE_DIR/java-adapter` (`~/.peeky/java-adapter`): each build is an immutable version directory there, and a `current` file names the one in use. Setups are serialized by an owner-only lock (`.lock`), and `current` is switched with one rename once a version is complete. Versions stay, for sessions still using them, until `peeky setup-java --prune` removes all but the current one; an interrupted setup's staging is removed by the next. Java targets whose config names no `bundle` use the version `current` names, resolved once when they start, and only if the state directory, `java-adapter` and the version are directories of the user's, not links, mode 0700, under none another user can write, and `current` is a file of theirs, mode 0600 exactly. Run again, setup keeps a version whose recipe fingerprint (javac's version asked afresh) is the one it would build, and builds a new one otherwise. `--output DIR` builds a plain bundle into a new directory instead. In a source checkout, without Peeky installed, `scripts/setup_java_adapter.py --output DIR` runs the same setup:

```sh
python scripts/setup_java_adapter.py --output "$PWD/work/java-adapter"
PEEKY_JAVA_ADAPTER="$PWD/work/java-adapter" pytest tests/java
```

Setup with `--output` requires a fresh output directory. It validates every archive/jar against committed SHA-256 pins, bounds downloads and compiler subprocesses, compiles core/launcher into clean staging, and moves the finished bundle into place. Failure removes only its own temporary staging. The manifest the script prints (`peeky setup-java --json`: its `adapter`) is also written as `adapter.json`, with command, toolchain version, source/dependency/launcher identities, per-file hashes, and the build `recipe` with its `fingerprint`: each patch's text hash, the launcher, lock and notices hashes, the javac flags (`--release 17`: the classes run on JDK 17 or later, and an older JVM refuses them) and javac's version text. The worker runs a bundle only if that recipe, but for javac, is this Peeky's; it doesn't ask the JDK. It never publishes or downloads executable launch commands from request data. Outputs contain private local paths; regenerate after moving the bundle. Cache the whole bundle only when the source/dependency lock, launcher, JDK, OS, and output path remain identical; consumers must validate its file manifest before execution.

Start the command vector in `adapter.json`, appending one absolute prepared source-map JSON path. Input/output use standard DAP framing over stdio. The source map explicitly lists binary class names and breakpoint lines:

```json
{
  "classes": {
    "demo.BranchBug": {"path": "/workspace/demo/BranchBug.java", "lines": [3]},
    "demo.BranchBug$Inner": {"path": "/workspace/demo/BranchBug.java", "lines": [8]}
  }
}
```

Only one class may own a requested line in one source file. Unknown/ambiguous lines fail. The map must be created by a trusted build preparation layer from compiled debug metadata; the launcher does not infer nested names or prove that declared source/class mappings are authentic. Tests assert fixture maps against `javap -l -p` output, then require actual verified breakpoint events and stopped locations. This fixture-specific check is not a generic source resolver.

Initialize with `adapterID=java`, `pathFormat=path`, and `linesStartAt1/columnsStartAt1=true`. Launch takes a mapped `mainClass` and explicit `classPaths`; the console is internal. The tested sequence is initialize → launch → initialized event → setBreakpoints → configurationDone → breakpoint verified event / stopped → stackTrace/scopes/variables → setVariable → continue → terminated → disconnect.

The launcher exposes only configurationDone and setVariable optional capabilities, plus Peeky's own markers (`peekySteps`, `peekyFieldSet`, `peekyTextSet`, `peekyCharSet`, `peekyJdkOwners`, `peekyArrayHolders`). Its request allowlist includes initialize, launch, attach, setBreakpoints, configurationDone, threads, stackTrace, scopes, variables, setVariable, continue, next, stepIn, stepOut, and disconnect, plus Peeky's own `peekyArrayHolder`, which says whether a JDK class's field holds an array (it never reaches java-debug). Step requests keep only a positive integer `threadId`, and launch and attach always set fixed step filters (skip `java.*`, `javax.*`, `jdk.*`, `sun.*`, `com.sun.*`; synthetic methods such as lambda bodies stay reachable). java-debug applies the filters to step into and step out; a step over can still end in JDK code that called the method. Unsupported commands, external consoles, noDebug, conditional/hit breakpoints and logpoints are rejected. Assignments accept numeric/boolean literals, a quoted char for a variable declared char, and text between plain quotes for one declared String; arbitrary expressions, NaN/Infinity, object rewrites and evaluation are out of scope. Object inspection disables automatic toString and logical-structure providers. The underlying debugger still executes trusted application code; this is not a sandbox.

The worker supplies generated class-metadata maps, fresh-build guards, session/stop identity and failure cleanup. `adapter.json` describes the debugger bundle, not the target application's provenance; JavaSession binds it to a separate prepared-build manifest.
