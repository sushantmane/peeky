#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
export LANG=en_US.UTF-8
export LANGUAGE=en_US:en
export LC_ALL=en_US.UTF-8
git checkout 8180ffba21bf10f4be905cb0d4890dc2bcff2788 -- tests/delete/models.py tests/delete/tests.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 delete.models delete.tests
echo ">>>>> End Test Output"
