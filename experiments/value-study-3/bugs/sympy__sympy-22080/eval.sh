#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout 3f8c8c2377cb8e0daaf8073e8d03ac7d87580813 -- sympy/codegen/tests/test_rewriting.py sympy/printing/tests/test_pycode.py sympy/utilities/tests/test_lambdify.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose sympy/codegen/tests/test_rewriting.py sympy/printing/tests/test_pycode.py sympy/utilities/tests/test_lambdify.py
echo ">>>>> End Test Output"
