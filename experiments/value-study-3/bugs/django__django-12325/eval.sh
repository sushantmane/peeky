#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
export LANG=en_US.UTF-8
export LANGUAGE=en_US:en
export LC_ALL=en_US.UTF-8
git checkout 29c126bb349526b5f1cd78facbe9f25906f18563 -- tests/invalid_models_tests/test_models.py tests/invalid_models_tests/test_relative_fields.py tests/migrations/test_state.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 invalid_models_tests.test_models invalid_models_tests.test_relative_fields migrations.test_state
echo ">>>>> End Test Output"
