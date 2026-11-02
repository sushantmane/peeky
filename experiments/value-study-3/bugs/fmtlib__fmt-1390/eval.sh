#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout d6eede9e085f0b36edcf0a2f6dff5f7875181019 -- test/format-test.cc
git apply -v /grade/test.patch
rm -rf build
cmake -S . -B build
cmake --build build --parallel 4 --target format-test
echo '>>>>> Start Test Output'
(ctest --test-dir build -R '^format-test$' --output-on-failure) | cat
echo '>>>>> End Test Output'
git checkout d6eede9e085f0b36edcf0a2f6dff5f7875181019 -- test/format-test.cc
