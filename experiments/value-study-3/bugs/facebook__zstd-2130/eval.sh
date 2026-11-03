#!/bin/bash
# Multi-SWE-bench's test command, `make; make test`, with the fuzzers' times shortened (from
# -T200s and -T90s); "all tests" passes when `make test` reaches its end (the educational
# decoder's test, its last step). `make test` builds the programs with
# MOREFLAGS="-g -DDEBUGLEVEL=1 -Werror"; GCC 13 warns of strict aliasing in lib/compress where
# Ubuntu 20.04's GCC 9 didn't, so that warning stays a warning (MOREFLAGS given on the command
# line, which make passes to the tests' build as well). HAVE_PTHREAD=1: programs/Makefile tests
# for pthread with `$(shell printf '\#include ...')`, which GNU make 4.3 (here) passes with its
# backslash, so the check always fails and zstd is built without threads (make 4.2, as in
# Ubuntu 20.04, found them). Tracing is off while the markers print.
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout e7d2391e9a75154657183b049b69a7a2effa9724 -- tests/fuzzer.c
git apply -v /grade/test.patch
make clean
make -j4 HAVE_PTHREAD=1
{ set +x; } 2>/dev/null
echo '>>>>> Start Test Output'
make test HAVE_PTHREAD=1 MOREFLAGS="-g -DDEBUGLEVEL=1 -Werror -Wno-error=strict-aliasing" \
  FUZZERTEST=-T1mn ZSTREAM_TESTTIME=-T30s 2>&1
echo '>>>>> End Test Output'
set -x
git checkout e7d2391e9a75154657183b049b69a7a2effa9724 -- tests/fuzzer.c
