#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout 298ccb478e6bf092953bca67a3d29dc6c35f6752 -- astropy/timeseries/tests/test_sampled.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest -rA astropy/timeseries/tests/test_sampled.py
echo ">>>>> End Test Output"
