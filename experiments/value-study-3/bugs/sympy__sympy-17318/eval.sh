#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout d4e0231b08147337745dcf601e62de7eefe2fb2d -- sympy/simplify/tests/test_sqrtdenest.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose sympy/simplify/tests/test_sqrtdenest.py
echo ">>>>> End Test Output"
