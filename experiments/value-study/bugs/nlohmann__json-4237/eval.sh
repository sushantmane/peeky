#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 3780b41dd070436f3f55327b0a88f27a52e2dfa8 -- tests/src/unit-udt.cpp
git apply -v /grade/test.patch
mkdir -p build
cd build
cmake ..
make test-udt_cpp11
cd ..
echo '>>>>> Start Test Output'
(./build/tests/test-udt_cpp11 -s -r=xml) | cat
echo '>>>>> End Test Output'
git checkout 3780b41dd070436f3f55327b0a88f27a52e2dfa8 -- tests/src/unit-udt.cpp
