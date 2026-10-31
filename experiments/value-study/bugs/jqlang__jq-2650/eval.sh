#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout d8072564c28d38d29aa0a7c416621f02c19f7f44 -- tests/jq.test
git apply -v /grade/test.patch
git submodule update --init
autoreconf -fi
./configure --with-oniguruma=builtin
make clean
touch src/parser.y src/lexer.l
make -j$(nproc)
echo '>>>>> Start Test Output'
(make check) | cat
echo '>>>>> End Test Output'
git checkout d8072564c28d38d29aa0a7c416621f02c19f7f44 -- tests/jq.test
