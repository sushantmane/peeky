# Java worker review — 2026-10-14

Scope: freshly compiled Java targets behind the existing authenticated HTTP worker, generated class/source mappings, prepared-file guards, pending breakpoint events and cleanup. No autonomous Java source-fix acceptance is claimed.

Independent implementation/review roles: architecture_review implemented the runtime binding; testing_review implemented metadata parsing and reviewed the worker/runtime; ci_scaffold implemented real HTTP tests and documentation. The parent integrated lifecycle/CLI behavior and resolved cross-component issues.

## Findings resolved

- **Java launch argument shape:** the core expects a string, not an argv array. Corrected to the actual JDI connector quoting contract, with real whitespace/backslash argument roundtrip coverage. Embedded double quotes and line breaks are explicitly rejected instead of guessed escaping.
- **Slow launch errors:** a failed launch response could leave the worker waiting for an initialized event that would never arrive. DAP event waiting now observes the failed request under the same condition lock and raises immediately; launch cleanup releases the prepared workspace. A regression covers failure without an initialized event.
- **Non-executable classes:** rejecting all classes without line tables also rejected legitimate interfaces/annotations. Metadata now permits classes with no concrete methods, while requiring line data for concrete methods; a real javac fixture covers the distinction.
- **Configuration coercion:** converting a string to a tuple silently changed one argument into many characters. CLI sources/arguments now require JSON arrays before conversion; tests verify rejection before server startup.
- **Stopped-frame and map integrity:** stop lines must be positive integers, and callers receive source-map copies backed by saved bytes rather than a mutable authoritative dictionary.

The final independent reviewer ran 32 worker/DAP tests in 1.92 seconds and found no remaining blocker for the documented worker scope. The parent ran all 121 tests, including real Java adapter, compiler metadata, HTTP behavior, argument roundtrip and failure cleanup: all passed in 15.00 seconds. Ruff lint/format passed.

## Remaining scope

Files and controlled launch inputs establish trusted-development provenance, not a cryptographic attestation of every byte loaded by an arbitrary class loader. Java candidate source rebuilding and independent checker acceptance are still required before claiming the five-language agent MVP. Remote source mapping, external classpaths/build systems, attach, expression evaluation and production isolation remain separate capabilities.
