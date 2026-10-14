# Bounded transport review — 2026-10-14

Scope: nonblocking DAP stdin writes, transport poisoning on uncertain writes, bounded disconnect, POSIX process cleanup. Independent reviewer: testing_review agent; implementation: architecture_review agent.

The reviewer checked partial-write framing, stalled adapter behavior, preservation of the first error, reader/event progress while the pipe is full, teardown after poisoning, and supported-host boundaries. No blocking finding remained. The reviewer ran the DAP unit and full integration tests: 19 passed in 4.00 seconds at review time.

An optional improvement was to synchronize the asynchronous event test after an actual partial write rather than a scheduling delay; the implementation agent addressed it, and all 13 DAP unit tests passed afterward.

The parent added a real-debugpy second-stop regression: both the old stop ID and unrefreshed scope must be rejected before mutation, even if an adapter reuses numeric handles. The HTTP benchmark checks the actual mutated output and reports raw samples without claiming a speedup.

Limitations: transport pipe deadlines explicitly support POSIX hosts, matching current Linux CI/macOS development. The write deadline is distinct from the response deadline. A timeout cannot determine whether the debugger applied a request, so the session must be invalidated, never automatically retried. Hostile processes escaping their owned process group remain outside the trusted-code contract.
