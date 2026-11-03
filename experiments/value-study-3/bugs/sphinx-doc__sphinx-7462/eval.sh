#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
# tox.ini's [testenv] setting (SWE-bench runs these tests through tox --current-env).
export PYTHONWARNINGS='all,ignore::ImportWarning:importlib._bootstrap_external,ignore::DeprecationWarning:site,ignore::DeprecationWarning:distutils'
git checkout b3e26a6c851133b82b50f4b68b53692076574d13 -- tests/test_domain_py.py tests/test_pycode_ast.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
python -m pytest -rA --durations 25 tests/test_domain_py.py tests/test_pycode_ast.py
echo ">>>>> End Test Output"
