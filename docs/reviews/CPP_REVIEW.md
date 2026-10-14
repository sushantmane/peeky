# C++ qualification and expansion adversarial review

Date: 2026-10-14. Scope: explicit C++ native targets and bounded read-only variable expansion. This is an independent AI-assisted code review with deterministic failure tests, not an external security audit or live-model success evaluation.

## Findings and dispositions

| Finding | Priority | Disposition |
| --- | --- | --- |
| Raw debugger references can be reused across frame/scope refreshes | P1 | Expansion exposes only worker-issued child handles in a separate registry from mutable scope handles. Each read re-resolves the selected frame, local scope and exact name/type ancestry through fresh debugger responses. Tests cover raw-reference changes and reinspection across frames. |
| Refreshing scopes during expansion can invalidate a previously issued raw mutable scope binding | P1 | The parallel architecture review identified this aliasing hazard. Expansion revokes existing mutable scope handles; callers must inspect again before writing. A dedicated regression rejects an old root-scope write after expansion. |
| Child traversal could accidentally enable aggregate or pointer mutation | P1 | Child handles are not accepted as mutable scope references, and mutable scope references cannot be expanded. Native writes retain exact writable leaf int/unsigned int validation. Wrong-role regressions confirm that no setVariable request is sent. |
| A stale path could resolve to a different frame, type or duplicate variable name | P1 | Fresh frame identity includes source, line, module and instruction pointer; scope identity must be unique. Every ancestor has one exact name match with the recorded type. Missing, duplicate or changed evidence is rejected before child reads. |
| Ignored adapter paging or cyclic object graphs could cause unbounded traversal | P1 | Internal reads request 513 entries to detect a collection beyond the 512-entry limit; public pages remain at most 64, depth at most four, and the whole inspection has a 512-node budget. Tests exercise ignored paging, oversized collections, strict integer inputs, local paging, deep objects and repeated expansion of the same handle. |
| Reusing handles after inspection, mutation or continuation could expose stale state | P1 | Lifecycle changes revoke child handles. Invalid-frame reinspection and rejected mutation also invalidate prior child handles. Timeout/provenance failures close the session. Tests cover these transitions and fresh scope/module validation. |

## Native language boundary

C++ is an explicit `cpp` configuration with fixed C++17 options, recognized source/header suffixes and clang++ as its default driver. C retains its existing default. The build recipe, target identity and verification language include the selected language; source/artifact identity and fresh candidate checking remain shared with C. This review found no source-level blocker in that language-selection diff.

Container inspection is qualified against the fixture and tested LLDB versions. It is not a guarantee that every C++ formatter or standard-library implementation exposes the same structure. Read-only here means the worker does not issue child mutation or arbitrary evaluate commands; debugger formatting itself is trusted code and is not promised free of side effects. Initialization controls and bounded debugger transport remain applicable.

## Validation and limits

`tests/unit/test_expansion.py`: **34 passed in 0.03 seconds**. Ruff check and format check passed for that test file. The independent expansion tests use an adapter that deliberately ignores paging and reuses raw references. They check ownership and rejection paths without a real compiler or debugger. Real C++ container shape, candidate repair and Linux/macOS compatibility require the separate C++ integration lane; unit success alone does not establish those claims.

Source, binary and symbol hashes are boundary checks, not an atomic binding to loaded process memory. Targets, debuggers, formatters and compilers retain local OS permissions. This slice does not add child writes, arbitrary expression evaluation, general C++ project support, or mixed-language runtime qualification.
