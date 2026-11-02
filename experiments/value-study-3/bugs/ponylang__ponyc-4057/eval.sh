#!/bin/bash
# The project's own debug build from a clean configure, then the third command of
# `make test-core`: the libponyc-run runner over test/libponyc-run, where the test patch adds its
# test. The gtest binaries, stdlib and examples parts of `make test` aren't run: no test of
# theirs is graded.
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
rm -rf test/libponyc-run/regression-3447
git apply -v /grade/test.patch
make clean config=debug
make configure config=debug
make build config=debug build_flags=-j4
echo '>>>>> Start Test Output'
(cd build/debug && /testbed/build/build_debug/test/libponyc-run/runner/runner --sequential=true \
  --exclude=runner --ponyc=/testbed/build/debug/ponyc --output=/testbed/build/debug \
  --test_lib=/testbed/build/debug/test_lib /testbed/test/libponyc-run)
echo '>>>>> End Test Output'
rm -rf test/libponyc-run/regression-3447
