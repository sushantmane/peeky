#!/bin/bash
# The test that the test patch changes: tests/fuzzer.c, run by `make test`'s test-fuzzer target
# (time shortened from -T200s). Prints PASS:/FAIL: test-fuzzer. Tracing is off while the
# markers and the result print, so that bash's trace of the end marker (on stderr) can't land
# before the result line (on stdout).
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 99e8063d40c38355f26f97249c8ef3f30e15d5b4 -- tests/fuzzer.c
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
git checkout 99e8063d40c38355f26f97249c8ef3f30e15d5b4 -- tests/fuzzer.c
