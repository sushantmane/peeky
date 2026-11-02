#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout 33a0599711d26dc2b79f851c6daed4947df7c167 -- lib/matplotlib/tests/test_figure.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest -rA lib/matplotlib/tests/test_figure.py
echo ">>>>> End Test Output"
