## What changed, and why

<!-- The problem you saw, and the behaviour after this change. Link the issue, for example "Closes #123". -->

## Tests that fail on main

<!--
Name each new or changed test that fails on `main` and passes with this change.
If no test can, say why (for example, a docs-only change).
-->

## Commands run

<!--
The commands you ran and what each reported, for example:
  ruff check . && ruff format --check .   -> clean
  pytest tests/unit                        -> 3600 passed
Add the language lanes your change touches (tests/java, tests/native, tests/cpp, tests/rust).
-->

## Evidence

<!-- Any live debugger results. Keep what you observed apart from what you infer. -->

## Review

<!--
For lifecycle, protocol, verification, transport or security changes: the adversarial
review's findings, and how each was resolved. Note any changed trust assumption or CI time.
-->

## Checklist

- [ ] A line under **Unreleased** in `CHANGELOG.md`, if users would notice the change.
- [ ] Docs updated, if behaviour or output changed.
- [ ] Each commit signed off (`git commit -s`), under the DCO: see CONTRIBUTING.md.
