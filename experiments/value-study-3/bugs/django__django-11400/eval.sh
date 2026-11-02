#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
export LANG=en_US.UTF-8
export LANGUAGE=en_US:en
export LC_ALL=en_US.UTF-8
git checkout 1f8382d34d54061eddc41df6994e20ee38c60907 -- tests/admin_filters/tests.py tests/model_fields/tests.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 admin_filters.tests model_fields.tests
echo ">>>>> End Test Output"
