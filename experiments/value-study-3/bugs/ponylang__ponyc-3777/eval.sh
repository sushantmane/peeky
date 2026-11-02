#!/bin/bash
# The project's own debug build from a clean configure, then `make test-stdlib-release` (the
# standard library's PonyTest program, which holds the test patch's packages/collections test).
# The gtest binaries and examples parts of `make test` aren't run: no test of theirs is graded.
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 89b03703acfebf6ee6d1b92298f6b671ce14d556 -- packages/collections/_test.pony
git apply -v /grade/test.patch
make clean config=debug
make configure config=debug
make build config=debug build_flags=-j4
echo '>>>>> Start Test Output'
make test-stdlib-release config=debug build_flags=-j4
echo '>>>>> End Test Output'
git checkout 89b03703acfebf6ee6d1b92298f6b671ce14d556 -- packages/collections/_test.pony
