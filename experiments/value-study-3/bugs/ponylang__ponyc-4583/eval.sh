#!/bin/bash
# The project's own debug build from a clean configure, then `make test-core`'s full-program
# tests, debug then release (test/full-program-tests, where the test patch adds its test). The
# gtest binaries, stdlib and examples parts of `make test` aren't run: no test of theirs is graded.
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
rm -rf test/full-program-tests/regression-4582
git apply -v /grade/test.patch
make clean config=debug
make configure config=debug
make build config=debug build_flags=-j4
echo '>>>>> Start Test Output'
make test-full-programs-debug config=debug
make test-full-programs-release config=debug
echo '>>>>> End Test Output'
rm -rf test/full-program-tests/regression-4582
