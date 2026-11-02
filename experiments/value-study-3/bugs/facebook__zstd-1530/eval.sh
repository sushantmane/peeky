#!/bin/bash
# Multi-SWE-bench's test command, `make; make test`, with the fuzzers' times shortened (from
# -T200s and -T90s); "all tests" passes when `make test` reaches its end (the pool tests).
# `make test` builds the programs with MOREFLAGS="-g -DDEBUGLEVEL=1 -Werror"; GCC 13 warns of
# strict aliasing in lib/compress/zstd_compress.c where Ubuntu 20.04's GCC 9 didn't, so that
# warning stays a warning (MOREFLAGS given on the command line, which make passes to the tests'
# build as well). HAVE_PTHREAD=1: programs/Makefile tests for pthread with
# `$(shell printf '\#include ...')`, which GNU make 4.3 (here) passes with its backslash, so the
# check always fails and zstd is built without threads (make 4.2, as in Ubuntu 20.04, found
# them); playTests.sh's --adapt test then fails at base and with the fix alike.
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 54e9412ddd02ca4c6663d09dbf44f5a209a9a7ce -- tests/fuzzer.c tests/regression/config.c tests/regression/results.csv
git apply -v /grade/test.patch
make clean
make -j4 HAVE_PTHREAD=1
echo '>>>>> Start Test Output'
make test HAVE_PTHREAD=1 MOREFLAGS="-g -DDEBUGLEVEL=1 -Werror -Wno-error=strict-aliasing" \
  FUZZERTEST=-T1mn ZSTREAM_TESTTIME=-T30s
echo '>>>>> End Test Output'
git checkout 54e9412ddd02ca4c6663d09dbf44f5a209a9a7ce -- tests/fuzzer.c tests/regression/config.c tests/regression/results.csv
