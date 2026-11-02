#!/bin/bash
# The project's own debug build from a clean configure, then `make test-core`'s two gtest
# binaries (libponyrt.tests, then libponyc.tests, which holds the test patch's tests), run as
# test-core runs them but in their own order (no --gtest_shuffle) and without
# CodegenTest.CycleDetector, which crashes or times out in this environment at base and with the
# fix alike, taking the rest of a shuffled run with it. The stdlib and examples parts of
# `make test` aren't run: no test of theirs is graded.
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 847c83430e8dfa645ba753a84754004134a9ca60 -- test/libponyc/iftype.cc
git apply -v /grade/test.patch
make clean config=debug
make configure config=debug
make build config=debug build_flags=-j4
echo '>>>>> Start Test Output'
(cd build/debug && ./libponyrt.tests && ./libponyc.tests --gtest_filter=-CodegenTest.CycleDetector)
echo '>>>>> End Test Output'
git checkout 847c83430e8dfa645ba753a84754004134a9ca60 -- test/libponyc/iftype.cc
