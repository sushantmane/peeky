#!/bin/bash
# The tests that the test patch changes: tests/fuzzer.c (test-fuzzer) and tests/zstreamtest.c
# (test-zstream), run as `make test` runs them, in its order, stopping at the first failure as
# `make test` does; times shortened from -T200s and -T90s. Each prints PASS:/FAIL: <target>.
# Tracing is off while the markers and the results print, so that bash's trace (on stderr) can't
# land between them.
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 823a28a1f4cb89be7ec22ee5d34754b54e9f2b6e -- tests/fuzzer.c tests/zstreamtest.c
git apply -v /grade/test.patch
make clean
make -j4
{ set +x; } 2>/dev/null
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
set -x
git checkout 823a28a1f4cb89be7ec22ee5d34754b54e9f2b6e -- tests/fuzzer.c tests/zstreamtest.c
