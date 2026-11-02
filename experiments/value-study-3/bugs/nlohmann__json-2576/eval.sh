#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 6f551930e5c7ef397056de121c0da82f77573cca -- test/src/unit-regression2.cpp
git apply -v /grade/test.patch
rm -rf build
cmake -S . -B build -DCMAKE_CXX_FLAGS=-DDOCTEST_CONFIG_NO_POSIX_SIGNALS
cp -r /opt/json_test_data build/
cmake --build build --parallel 4 --target test-regression2
echo '>>>>> Start Test Output'
(ctest --test-dir build -R '^test-regression2$' --output-on-failure) | cat
echo '>>>>> End Test Output'
git checkout 6f551930e5c7ef397056de121c0da82f77573cca -- test/src/unit-regression2.cpp
