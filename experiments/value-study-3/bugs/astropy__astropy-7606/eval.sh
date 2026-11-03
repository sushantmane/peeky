#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout 3cedd79e6c121910220f8e6df77c54a0b344ea94 -- astropy/units/tests/test_units.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest -rA -vv -o console_output_style=classic --tb=no astropy/units/tests/test_units.py
echo ">>>>> End Test Output"
