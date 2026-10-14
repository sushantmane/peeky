# Experimental Java investigation loop

The Java investigation controller can now use the same `observe`, `intervene`, and `finish` actions as the Python controller. A worker compiles and debugs an isolated copy of the declared Java inputs. A separate verification strategy compiles each proposed source change into another fresh workspace and runs the supplied independent checker against fresh JVM processes.

The bundled example is a one-based pagination bug in `examples/java/demo/PaginationBug.java`. It computes `start = page * pageSize`; page 1 incorrectly starts at the second page. A useful experiment pauses after that assignment, observes `start = 3`, changes it to `0`, and continues to the expected first-page output. That intervention is evidence for the hypothesis. Acceptance still requires a source patch to pass the unchanged checker.

## Deterministic proof in required CI

```sh
python scripts/setup_java_adapter.py --output "$PWD/work/java-adapter"
PEEKY_JAVA_ADAPTER="$PWD/work/java-adapter" pytest tests/java/test_java_agent.py
```

The decisions in these tests are injected fixtures, not model calls. The real debugging/build/checking path remains in use:

1. Observe the actual first-page state through HTTP and the Java debugger.
2. Mutate the live integer and observe `PAGE:[10, 20, 30]` without rebuilding for the experiment.
3. Propose invalid Java syntax; compilation fails and diagnostics return to the controller for another decision.
4. Propose `start = 0`; this fixes the first page but fails the independent later-page checks.
5. Propose `start = (page - 1) * pageSize`; compile and pass 12 independent page/size/length cases in fresh JVMs.

The controller preserves the original source and checker and records candidate bytes, hashes, build evidence and checker completion. Other regressions reject finishing before runtime evidence and reject changes to the staged candidate, saved candidate, checker, launch descriptor or class artifact during checking. Tamper tests record that their mutation actually executed, so an unrelated setup failure cannot satisfy them.

## Optional live-model run

After installing/signing in to Codex CLI, create `work/java-agent-target.json`:

```json
{
  "bundle": "java-adapter",
  "sources": ["demo/PaginationBug.java"],
  "main_class": "demo.PaginationBug",
  "arguments": []
}
```

Run with a model available to your account and a new output directory:

```sh
peeky-agent \
  --root "$PWD/examples/java" \
  --java-config "$PWD/work/java-agent-target.json" \
  --program demo/PaginationBug.java \
  --check verify_pagination.py \
  --model YOUR_MODEL \
  --output "$PWD/work/java-agent-run"
```

The controller launches a local worker when `--url` is omitted. A configured existing worker can be selected with `--url` and its `DEBUG_TOKEN`, but the declared source identities must match. Candidate build and verification occur on the controller host and require the same declared sources and a suitable local Java toolchain. Remote cross-root deployment is not proven by this local regression suite. Live model use sends selected source/runtime evidence to the model and consumes model usage; it is separate from required CI.

## Independent checker contract

A Java checker is trusted Python code. Its `sys.argv[1]` points to a temporary JSON descriptor containing:

- `schema: 1`, `language: "java"`;
- `command`: an argument vector such as `[java, "-cp", classes_directory, main_class]`;
- `arguments`: configured baseline application arguments;
- `environment`: the controlled environment to use for child executions;
- prepared source identity, source manifest, build manifest and artifact manifest ID.

The supplied checker reads that descriptor and runs `[*command, *case_arguments]` with `env=environment`. It asserts its own expected outputs for independent cases and returns normally. An exit code alone is insufficient: the trusted wrapper requires explicit checker completion. The original buggy source must fail the baseline checker before an investigation can claim a repair.

The descriptor and compiled workspace are temporary. Evidence records have `artifacts_retained: false`; recorded commands are not promised to remain runnable after cleanup. The saved candidate preserves its package-relative `.java` filename under the investigation output, and every candidate attempt writes a separate build-evidence JSON file. Build/check evidence is broader than a successful live-variable experiment, but it proves only the supplied verifier's assertions.

This remains trusted-code verification, not protection against a malicious compiler/JVM or an adversary with the worker's OS permissions. Java arbitrary project builds, third-party classpaths, class hot replacement and automatic verifier generation are outside this slice.
