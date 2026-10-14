# Working on Peeky

Read README.md and docs/DESIGN.md before changing lifecycle or protocol behavior. Keep work inside the MVP boundaries in docs/ROADMAP.md.

- Never claim a runtime intervention alone is a verified source fix.
- Preserve source/check identity, session/stop identity, and fail-closed semantics after uncertain mutations.
- Add meaningful failure regressions with fake adapters, plus real-debugpy coverage where runtime behavior matters.
- Do not put model calls, credentials, or real application reports in required CI.
- Keep required CI lanes independent and track their elapsed time; do not hide slow/failing work behind a timeout.
- Nontrivial transport, session, verification, or security-boundary changes need an independent adversarial review before merge. Record findings and dispositions in the PR.
- Run ruff check, ruff format --check, and the relevant pytest lanes. Build/install smoke for packaging changes.
- Do not publish packages or deploy workers as part of routine changes. Private artifact delivery is configured separately.
