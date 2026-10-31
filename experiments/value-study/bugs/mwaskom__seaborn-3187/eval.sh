#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout 22cdfb0c93f8ec78492d87edb810f10cb7f57a31 -- tests/_core/test_plot.py tests/test_relational.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest --no-header -rA tests/_core/test_plot.py tests/test_relational.py
echo ">>>>> End Test Output"
