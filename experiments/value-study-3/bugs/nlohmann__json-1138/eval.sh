#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout e830bc502fe38852654a1f03f963001fecb54a86 -- test/src/unit-conversions.cpp
git apply -v /grade/test.patch
rm -rf build
cmake -S . -B build -DCMAKE_CXX_FLAGS=-DCATCH_CONFIG_NO_POSIX_SIGNALS
cmake --build build --parallel 4 --target test-conversions
echo '>>>>> Start Test Output'
(ctest --test-dir build -R '^test-conversions_(default|all)$' --output-on-failure) | cat
echo '>>>>> End Test Output'
git checkout e830bc502fe38852654a1f03f963001fecb54a86 -- test/src/unit-conversions.cpp
