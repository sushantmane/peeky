#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 80f8d34427d40ec5e7ce3b10ededc46bd4bd5759 -- test/ranges-test.cc
git apply -v /grade/test.patch
mkdir -p build
cmake -B build -S .
cmake --build build --parallel $(nproc) --target ranges-test
echo '>>>>> Start Test Output'
(ctest --test-dir build -V -R ranges-test) | cat
echo '>>>>> End Test Output'
git checkout 80f8d34427d40ec5e7ce3b10ededc46bd4bd5759 -- test/ranges-test.cc
