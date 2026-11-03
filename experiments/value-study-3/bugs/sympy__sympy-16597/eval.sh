#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout 6fd65310fa3167b9626c38a5487e171ca407d988 -- sympy/core/tests/test_assumptions.py sympy/functions/elementary/tests/test_miscellaneous.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose sympy/core/tests/test_assumptions.py sympy/functions/elementary/tests/test_miscellaneous.py
echo ">>>>> End Test Output"
