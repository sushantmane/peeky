#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout 6ed769d58d89380ebaa1ef52b300691eefda8928 -- astropy/table/tests/test_mixin.py astropy/table/tests/test_table.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest -rA astropy/table/tests/test_mixin.py astropy/table/tests/test_table.py
echo ">>>>> End Test Output"
