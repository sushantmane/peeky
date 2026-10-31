#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout 6500928dc0e57be8f06d1162eacc3ba5e2eff692 -- astropy/coordinates/tests/test_intermediate_transformations.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest -rA astropy/coordinates/tests/test_intermediate_transformations.py
echo ">>>>> End Test Output"
