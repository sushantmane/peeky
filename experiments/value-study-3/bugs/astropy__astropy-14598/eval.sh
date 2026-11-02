#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout 80c3854a5f4f4a6ab86c03d9db7854767fcd83c1 -- astropy/io/fits/tests/test_header.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest -rA astropy/io/fits/tests/test_header.py
echo ">>>>> End Test Output"
