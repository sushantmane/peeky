# Five-language plan adversarial review

Date: 2026-10-14. An independent architecture reviewer inspected the documentation-only scope change before merge.

The reviewer agreed with Python → Java → C → C++ → Rust, and identified three acceptance gaps. All are incorporated into LANGUAGES.md:

1. A source hash and binary hash do not prove build provenance. Require an explicit clean build and recorded source/toolchain/flags/artifact manifest; test stale-binary/source mismatch.
2. A working debugger adapter does not establish autonomous language support. Each language needs a provider-injected end-to-end controller experiment, candidate build and independent verification, plus a manual live-model example.
3. Remote path mapping and loaded-artifact identity need explicit negative tests across different absolute roots; reject mismatches before mutation.

The design and ADR now distinguish the implemented Python alpha from the unimplemented five-language minimum product scope. No new adapter or capability is claimed as implemented by this change.
