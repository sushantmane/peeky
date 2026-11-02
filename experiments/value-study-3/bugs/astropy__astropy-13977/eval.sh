#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout 5250b2442501e6c671c6b380536f1edb352602d1 -- astropy/units/tests/test_quantity.py astropy/units/tests/test_quantity_ufuncs.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest -rA astropy/units/tests/test_quantity.py astropy/units/tests/test_quantity_ufuncs.py
echo ">>>>> End Test Output"
