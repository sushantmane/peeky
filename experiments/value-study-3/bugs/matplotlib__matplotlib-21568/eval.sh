#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout f0632c0fc7339f68e992ed63ae4cfac76cd41aad -- lib/matplotlib/tests/test_dates.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest -rA lib/matplotlib/tests/test_dates.py
echo ">>>>> End Test Output"
