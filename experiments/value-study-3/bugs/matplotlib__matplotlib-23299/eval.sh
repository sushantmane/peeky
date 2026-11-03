#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout 3eadeacc06c9f2ddcdac6ae39819faa9fbee9e39 -- lib/matplotlib/tests/test_rcparams.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest -rA lib/matplotlib/tests/test_rcparams.py
echo ">>>>> End Test Output"
