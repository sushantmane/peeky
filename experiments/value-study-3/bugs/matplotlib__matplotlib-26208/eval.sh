#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout f0f133943d3e4f1e2e665291fe1c8f658a84cc09 -- lib/matplotlib/tests/test_axes.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest -rA lib/matplotlib/tests/test_axes.py
echo ">>>>> End Test Output"
