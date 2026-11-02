#!/bin/bash
# The tests that the test patch changes: tests/fuzzer.c (test-fuzzer) and tests/zstreamtest.c
# (test-zstream), run as `make test` runs them, in its order, stopping at the first failure as
# `make test` does; times shortened from -T5mn and -T2mn. Each prints PASS:/FAIL: <target>.
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 3128e03be69b9d3db054a3765ae1310c7b3666f6 -- tests/fuzzer.c tests/zstreamtest.c
git apply -v /grade/test.patch
make clean
make -j4
echo '>>>>> Start Test Output'
for target in test-fuzzer test-zstream; do
  if make -C tests $target FUZZERTEST=-T1mn ZSTREAM_TESTTIME=-T30s; then
    echo "PASS: $target"
  else
    echo "FAIL: $target"
    break
  fi
done
echo '>>>>> End Test Output'
git checkout 3128e03be69b9d3db054a3765ae1310c7b3666f6 -- tests/fuzzer.c tests/zstreamtest.c
