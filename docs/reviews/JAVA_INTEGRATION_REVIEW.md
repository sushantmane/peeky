# External review of the Java integration — 2026-10-14

An external AI-assisted review read the Java, worker and build sources at main commit `59b6508`. It completed in 37.91 seconds. It had no tools and ran no code.

## Assessment

The response reaffirmed already documented integration requirements: generated source maps, prepared artifact checks, session/stop identity and cleanup. Its proposed implementation was not accepted without verification:

- It claimed the worker sets breakpoints before initialization. The worker explicitly waits for the initialized event first; this finding is incorrect.
- It described concurrent sessions sharing maps as a current critical bug. The worker currently permits only one session and Java is not registered. This is a future integration consideration, not a demonstrated current bug.
- It proposed proving loaded-artifact identity from class-name membership. Matching names cannot establish matching bytecode; source/build provenance and loaded-image limitations still need explicit handling.
- It proposed deferring breakpoint installation until the first stop. That can prevent the first breakpoint stop from occurring; use the debugger core's pending-breakpoint resolution and validate actual stop evidence instead.
- It suggested putting session guards into the Java adapter. The worker already owns those guards; preserve that boundary.

No source changes were made from these proposals. External reviews are useful independent input, but code and claims are accepted only after reproduction, review and required CI.
