#!/bin/bash
# The test that the test patch changes: tests/fuzzer.c, run by `make test`'s test-fuzzer target
# (time shortened from -T200s). Prints PASS:/FAIL: test-fuzzer. Tracing is off while the
# markers and the result print, so that bash's trace of the end marker (on stderr) can't land
# before the result line (on stdout).
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 3c3f59e68f1771dabeb020c0aa0f30b8c9c59936 -- tests/fuzzer.c
git apply -v /grade/test.patch
make clean
make -j4
{ set +x; } 2>/dev/null
echo '>>>>> Start Test Output'
if make -C tests test-fuzzer FUZZERTEST=-T1mn; then
  echo "PASS: test-fuzzer"
else
  echo "FAIL: test-fuzzer"
fi
echo '>>>>> End Test Output'
set -x
git checkout 3c3f59e68f1771dabeb020c0aa0f30b8c9c59936 -- tests/fuzzer.c
