#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 40cba172ed66584cf670c98202ed474a316667e3 -- tests/ondemand/CMakeLists.txt
rm -f tests/ondemand/ondemand_json_pointer_tests.cpp
git apply -v /grade/test.patch
rm -rf build
cmake -S . -B build -DSIMDJSON_DEVELOPER_MODE=ON
cmake --build build --parallel 4 --target ondemand_json_pointer_tests
echo '>>>>> Start Test Output'
(ctest --test-dir build -R '^ondemand_json_pointer_tests$' --output-on-failure) | cat
echo '>>>>> End Test Output'
git checkout 40cba172ed66584cf670c98202ed474a316667e3 -- tests/ondemand/CMakeLists.txt
rm -f tests/ondemand/ondemand_json_pointer_tests.cpp
