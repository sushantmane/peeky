#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout 54cab15bdacfaa05a88fbc5502a5b322d99f148e -- tests/_core/test_plot.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest --no-header -rA tests/_core/test_plot.py
echo ">>>>> End Test Output"
