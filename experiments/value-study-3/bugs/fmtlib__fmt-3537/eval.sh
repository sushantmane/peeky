#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 661b23edeb52d400cf5812e7330f14f05c072fab -- test/color-test.cc
git apply -v /grade/test.patch
rm -rf build
cmake -S . -B build
cmake --build build --parallel 4 --target color-test
echo '>>>>> Start Test Output'
(ctest --test-dir build -R '^color-test$' --output-on-failure) | cat
echo '>>>>> End Test Output'
git checkout 661b23edeb52d400cf5812e7330f14f05c072fab -- test/color-test.cc
