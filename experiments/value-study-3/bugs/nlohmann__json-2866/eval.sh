#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 9426074ce85da2121c315cae8574d8eaca520cb7 -- test/src/unit-diagnostics.cpp
git apply -v /grade/test.patch
rm -rf build
cmake -S . -B build
cp -r /opt/json_test_data build/
cmake --build build --parallel 4 --target test-diagnostics
echo '>>>>> Start Test Output'
(ctest --test-dir build -R '^test-diagnostics$' --output-on-failure) | cat
echo '>>>>> End Test Output'
git checkout 9426074ce85da2121c315cae8574d8eaca520cb7 -- test/src/unit-diagnostics.cpp
