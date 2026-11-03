#!/bin/bash
# The project's own debug build from a clean configure, then `make test-core`'s two gtest
# binaries: libponyrt.tests, then libponyc.tests limited to FlattenTest (the suite of
# test/libponyc/flatten.cc, the file the test patch changes) and the two other suites that
# pass_to_pass names (BuildFlagSetTest, SuggestAltNameTest), run in their own order (test-core
# passes --gtest_shuffle). The runner over test/libponyc-run, and the stdlib and examples parts
# of `make test`, aren't run: no test of theirs is graded.
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 10f8a8bc0810b80e0f7ab3aa09fafba98e9f2227 -- test/libponyc/flatten.cc
git apply -v /grade/test.patch
make clean config=debug
make configure config=debug
make build config=debug build_flags=-j4
echo '>>>>> Start Test Output'
(cd build/debug && ./libponyrt.tests; ./libponyc.tests --gtest_filter='BuildFlagSetTest.*:FlattenTest.*:SuggestAltNameTest.*')
echo '>>>>> End Test Output'
git checkout 10f8a8bc0810b80e0f7ab3aa09fafba98e9f2227 -- test/libponyc/flatten.cc
