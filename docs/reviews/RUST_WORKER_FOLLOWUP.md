# Rust worker follow-up adversarial review

Date: 2026-10-14. Reviewed committed `10a1136` (Rust implementation unchanged from `bd2ade2`) against `4bb493d`. This is an independent AI-assisted source review and targeted test run, not a sandbox audit. Scope is experimental Rust worker sessions, symbol evidence and active Option views; Cargo test execution and candidate repair verification are not delivered by this head.

## Review method and verification

Read AGENTS, README, DESIGN, PROGRESS and the committed implementation diff. Another writer changed the shared working tree during review. The initial mixed-tree test run encountered eight artifact-API failures and is not evidence about this PR. A clean archive of committed `10a1136` was then extracted into an ignored review directory and tested with its own source on PYTHONPATH.

Committed-snapshot targeted tests: **179 passed in 0.73 seconds**, covering Rust symbols, Rust worker hooks/configuration, expansion, Cargo preflight and metadata budgets. This reviewer did not independently repeat real macOS/Linux debugger runs; previously reported hosted/local results remain separately attributed in TESTING and PROGRESS.

## Earlier review fixes verified

- Option payload views check each variant child's own payload type; only the selected Some payload is fetched. None does not interpret inactive storage. Disagreement, malformed shapes and unqualified layouts become opaque values without raw expansion handles.
- Base types are resolved through in-unit DIE references and checked for tag, name, encoding and four-byte width; pretty reference labels alone are insufficient.
- DIE parsing requires child-list closure, rejects unknown rows/duplicates and binds entry offsets to the selected application unit. Attribute indentation uses depth rather than printed offset width.
- `$variant$` names use compiler discriminant values instead of positional indices. The compiled mapping determines the active variant.
- Application units require the exact recorded fresh compilation directory and expected crate-root naming. Offset-specific line dumps must identify exactly the requested statement table. The unchanged native 2 MiB parser remains the final line-map boundary.
- Application symbol commands share a 30-second deadline, individual capture budgets remain bounded, and raw value normalization has a 128-fetch budget per inspection. The existing page/depth/node limits remain in force.
- Session metadata stores line/layout snapshots as serialized data with defensive copies. Worker reads retain source/module checks and ownership of opaque handles; interpretation never grants child mutation capability. Expand revokes old write scopes, and mutation/inspection/continuation/close revoke child handles.

## Findings requiring disposition

### P2 — configured macOS symbol tool can differ from the executed helper

`src/peeky/rust.py:165` constructs PATH from the rustc directory before the declared dsymutil directory. The configured dsymutil filename is unrestricted, and only its directory is supplied to rustc. Therefore a configured `/custom/renamed-symbol-tool`, or a different dsymutil earlier in PATH, can be probed and hashed while another helper is actually selected. This weakens the explicit helper-tool identity claim even though resulting symbols are hashed and UUID-bound.

Requested disposition: bind the helper rustc will select to the exact configured executable, rejecting unsupported aliases/shadowing, or execute the configured tool explicitly with a qualified artifact flow. Add a regression for a renamed helper and a shadowing same-name executable. Do not claim a hash of the configured path identifies a different PATH-selected helper.

### P2 follow-up — failed build checks omit previously probed helper drift

`src/peeky/build.py:350` checks source manifests before rethrowing BuildCommandError, but does not recheck previously recorded tool identities on that path. A concrete committed-snapshot subprocess reproduction probed a helper, changed that helper from a later command and exited 7; the result remained BuildCommandError. The currently executing command's own hash is checked, but Cargo delegates compilation/linking to other probed tools.

This is not a successful worker acceptance bypass: launch fails and the Rust worker slice has no candidate verification loop. It must be corrected before ordinary failed Cargo compilation is treated as retryable candidate feedback. Recheck all recorded tool identities before classifying failed preparation, and keep helper drift fatal. Successful preparation already checks the complete manifest.

## Remaining trust and scope limits

An ancestry check plus fresh Cargo directories prevents the covered ambient configuration cases, but it is not atomic filesystem isolation. Compiler/runtime libraries, SDK contents and executable loading are not hermetically captured. A tool or application that deliberately races file replacement or escapes its process group remains outside the trusted-code contract.

Malformed metadata is bounded and generally rejected or rendered opaque. These checks validate the qualified DWARF/debugger representation, not arbitrary Rust ABI layouts or all standard-library enums. Root mutation is limited to the tested debugger representation of i32; no borrow/ownership invariant guarantee follows from changing a live scalar. Runtime intervention remains evidence about an execution, not a verified source repair.

## Independent preparation review (committed 10a1136)

A second reviewer inspected the same committed snapshot, excluding concurrent verification edits:

- **Blocking lifecycle issue:** Rust permits a120-second Cargo build plus preparation, while Client.call has a45-second timeout. The caller can lose the launch response/session ID before the worker finishes launching. Resolve with a coherent overall launch deadline/cancellation contract and an abandoned slow-launch regression; extending one stage alone does not establish cleanup.
- **Blocking input-contract issue:** validate_cargo_inputs reads originals before build.prepare captures and copies its input snapshot. A manifest change in that gap may be accepted as the build's new baseline without revalidation. Validate the exact staged manifest/lock bytes before Cargo executes and bind selected targets to that snapshot. Add a regression changing the manifest between preflight and copying.
- Before enabling repair feedback, validate pinned tool versions before compilation and distinguish compiler diagnostics from Cargo infrastructure/configuration failures. Prior-probed-tool drift during a failed compile must remain fatal.

The earlier review's dsymutil selection finding also needs disposition. No merge approval is granted by the currently passing CI. Production edits for these fixes were not made during this review because another provider was actively implementing verification in the same checkout.
