# Native C worker adversarial review

Date: 2026-10-14. Scope: fixed C targets, fresh compiler builds, LLDB source/module evidence, scalar local mutation, and worker scope ownership. This is an independent AI-assisted source and test review, not an external security audit. Native candidate verification and agent repair acceptance are outside this slice.

## Findings and dispositions

| Finding | Priority | Disposition |
| --- | --- | --- |
| LLVM 18 does not read the newer LLDB initialization key; ignoring the unknown key can permit home initialization commands | P1 | Send both version-specific keys with false and give the adapter an isolated HOME. A real initialization sentinel test uses an enabled positive control before checking the protected launch. LLVM 18 source confirms the older key; hosted Linux execution must independently validate that toolchain. |
| LLDB can reuse the same raw locals reference for different frames within one stop | P1 | Worker issues monotonically increasing public scope handles, translates them internally, and revokes every prior handle on reinspection. Stop, continue and close clear ownership. Mocked regressions cover both same-frame and different-frame reuse and ensure stale handles never reach setVariable. |
| An integer-looking value alone does not establish that the selected variable is safe to mutate | P1 | Worker refreshes the exact variable record and requires one matching name. Native validation permits only writable leaf int/unsigned int locals, exact identifiers and bounded decimal 32-bit values. Pointer, aggregate, const/read-only, out-of-range and expression inputs are rejected. |
| A valid source path alone does not establish that the selected frame belongs to the prepared executable | P1 | Inspect and pre-mutation checks bind source/line to the generated line map and frame module ID to the built executable path, UUID/build ID and loaded-symbol status. Mocked tests reject module changes between inspect and write. |
| Binary ID/status alone does not bind a separately loaded debug-symbol file on Linux | P1 | Fixed: the loaded symbol path must match the hashed symbol artifact on every platform. Native unit regressions reject missing and foreign symbol paths. |
| Mutable source-line metadata could drift after manifest construction | P2 | Fixed: the source-line map is serialized once and exposed through defensive copies. A regression confirms caller changes cannot alter the validation map. |

## Compatibility evidence

The LLVM 18.1.8 [initialization implementation](https://github.com/llvm/llvm-project/blob/llvmorg-18.1.8/lldb/tools/lldb-dap/lldb-dap.cpp) reads `sourceInitFile`. The LLVM 18.1.8 [module serializer](https://github.com/llvm/llvm-project/blob/llvmorg-18.1.8/lldb/tools/lldb-dap/JSONUtils.cpp) supplies UUID-based IDs, loaded-symbol status and symbol-file paths. These source checks support the selected protocol guards; they do not replace a real LLVM 18 run.

Independent targeted run: `tests/unit/test_native_worker.py` and `tests/unit/test_native.py`: **44 passed in 0.03 seconds**. The worker tests cover stale public references, fresh variable types, exact-name ambiguity, uninspected references, module changes and continue invalidation. Ruff check and format check passed for the owned worker regression file. The runtime author separately reported **10 real native tests passed in 18.97 seconds** on macOS with LLVM 21, including the initialization sentinel control. This review inspected those tests but did not independently repeat that real lane. Hosted Linux LLVM 18 execution remains a separate required CI result; neither this unit run nor the macOS run establishes it.

## Remaining trust and scope limits

Targets, compilers, symbol tools, debugger adapters and applications are trusted developer code running with local OS permissions. The controlled environment and disabled initialization reduce accidental ambient configuration, but this is not a hostile-code sandbox. Hash comparisons at operation boundaries do not atomically bind filesystem reads to executable pages and do not prevent concurrent malicious replacement or in-memory patching. SDK/system headers, runtime libraries and external services are not hermetically captured.

The build manifest records the build environment. The adapter overrides HOME with a fresh isolated directory and includes that effective runtime environment in the native session manifest hash; it must not be described as executing with the exact unchanged build environment. DWARF4, declared source/header paths, supported executable formats and tested scalar types define the current boundary. Unsupported source locations or module evidence fail closed. Runtime mutation is evidence about a running execution, not verification of a rebuilt source fix.

A separate bounded no-tool source review attempt timed out after 150 seconds without output. It contributes no findings or approval to this review.

## Review provenance

A separate bounded source-review attempt timed out after 150 seconds without returning output or findings. It supplies no additional review approval or evidence. The independent source review and executed regressions above are the evidence for this record.

## Follow-up: failed build provenance

Independent review found that the existing build runner classified a nonzero command result before rechecking the executable hash. A self-modifying or self-deleting compiler could therefore be reported as ordinary candidate compile feedback. The runner now checks executable integrity before classifying every returned result; missing or changed tools remain fatal integrity errors. Toolchain-probe failures are also fatal setup errors, retaining their diagnostic cause instead of becoming retryable candidate failures. Ordinary unchanged compiler failures retain the existing typed feedback behavior.

Reviewed the minimal runner diff and executed `tests/unit/test_build.py` plus `tests/unit/test_agent_java.py`: **40 passed in 0.87 seconds**. New real subprocess regressions cover self-modification followed by exit 7, self-deletion followed by exit 7, failed probes, and cleanup of each failed workspace. This resolves the classification bypass without changing the documented check-to-use trust boundary.

## Linux LLVM18 compatibility follow-up

The first hosted runs failed closed because LLVM18 stack frames omit moduleId; captured diagnostics confirmed instructionPointerReference and the module's decimal header load address are available. The author's fallback was reviewed independently: only an absent moduleId on Linux permits matching the instruction pointer against file-backed executable ELF segments relocated from the loaded header address. Build ID, binary path, symbol path and source/line guards still apply. Explicit wrong IDs never fall back. Tests cover PIE/non-PIE offsets, absent/invalid addresses, wrong ranges, duplicate modules, overflow and unbacked executable memory. Hosted execution remains the final gate.

The next Linux run passed module validation and exposed two additional differences: LLVM18 returns setVariable's observed value under `result` (confirmed in its request_setVariable implementation), and Linux prohibits overwriting a running executable. Native response normalization now handles both `result` and `value` and treats a missing observed value as uncertain/fatal. The tampering regression atomically replaces the named file and confirms replacement bytes before asserting rejection, rather than failing at the kernel write restriction. These changes preserve the runtime guard and test actual drift.

Native decimal literals also reject ambiguous leading-zero forms such as 010, with regressions, so debugger integer parsing cannot silently choose another radix. Local follow-up native suite:10 passed19.30s; native unit43 passed.
