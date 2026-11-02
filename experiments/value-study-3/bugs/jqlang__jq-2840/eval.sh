#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 4cf1408e0bbac8fc714b051fe420921905128efd -- tests/jq.test
git apply -v /grade/test.patch
git submodule update --init
autoreconf -fi
./configure --with-oniguruma=builtin
make clean
touch src/parser.y src/lexer.l
make -j4
echo '>>>>> Start Test Output'
(make check) | cat
echo '>>>>> End Test Output'
git checkout 4cf1408e0bbac8fc714b051fe420921905128efd -- tests/jq.test
