#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 2622cd23e69b67316cf678a97c268a874774c0e1 -- test/chrono-test.cc
git apply -v /grade/test.patch
rm -rf build
cmake -S . -B build -DCMAKE_CXX_FLAGS=-fsigned-char
cmake --build build --parallel 4 --target chrono-test
echo '>>>>> Start Test Output'
(ctest --test-dir build -R '^chrono-test$' --output-on-failure) | cat
echo '>>>>> End Test Output'
git checkout 2622cd23e69b67316cf678a97c268a874774c0e1 -- test/chrono-test.cc
