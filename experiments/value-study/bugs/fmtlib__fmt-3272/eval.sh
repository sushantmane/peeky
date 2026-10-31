#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 0f42c17d85f26bbbd6a30d71ce8306d371426bfc -- test/xchar-test.cc
git apply -v /grade/test.patch
mkdir -p build
cmake -B build -S .
cmake --build build --parallel $(nproc) --target xchar-test
echo '>>>>> Start Test Output'
(ctest --test-dir build -V -R xchar-test) | cat
echo '>>>>> End Test Output'
git checkout 0f42c17d85f26bbbd6a30d71ce8306d371426bfc -- test/xchar-test.cc
