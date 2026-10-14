# Experimental Java DAP launcher

This launcher wraps Microsoft java-debug's unmodified core with narrow service providers. It is qualified by tiny packaged and nested-class fixtures; it is not registered as a Peeky product adapter yet. JDK 17+, Python 3.11+, and HTTPS access to GitHub/Maven Central are needed for setup.

From the repository root:

```sh
python scripts/setup_java_adapter.py --output "$PWD/work/java-adapter"
PEEKY_JAVA_ADAPTER="$PWD/work/java-adapter" pytest tests/java
```

Setup requires a fresh output directory, validates every archive/jar against committed SHA-256 pins, bounds downloads and compiler subprocesses, compiles core/launcher into clean staging, and moves the finished bundle into place. Failure removes only its own temporary staging. The JSON printed to stdout is also written as `adapter.json`, with command, toolchain version, source/dependency/launcher identities, and per-file hashes. It never publishes or downloads executable launch commands from request data. Outputs contain private local paths; regenerate after moving the bundle. Cache the whole bundle only when the source/dependency lock, launcher, JDK, OS, and output path remain identical; consumers must validate its file manifest before execution.

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

The launcher exposes only configurationDone and setVariable optional capabilities. Its request allowlist includes initialize, launch, setBreakpoints, configurationDone, threads, stackTrace, scopes, variables, setVariable, continue, and disconnect. Unsupported commands, external consoles, noDebug, conditional/hit breakpoints and logpoints are rejected. Assignments accept numeric/boolean literal syntax only; arbitrary expressions, strings, character literals, NaN/Infinity, object rewrites and evaluation are out of scope. Object inspection disables automatic toString and logical-structure providers. The underlying debugger still executes trusted application code; this is not a sandbox.

Remaining product requirements include authenticated worker integration, session/stop guards, build/artifact identity validation, robust source resolution, candidate rebuild plus independent verification, remote path binding, uncertainty handling, and crash cleanup conformance. `adapter.json` describes the debugger bundle, not the target application's provenance. See `docs/JAVA_SPIKE.md` and `docs/LANGUAGES.md` for remaining language acceptance requirements.
