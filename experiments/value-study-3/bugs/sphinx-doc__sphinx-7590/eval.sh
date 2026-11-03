#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
# tox.ini's [testenv] settings (SWE-bench runs these tests through tox --current-env).
export PYTHONWARNINGS='all,ignore::ImportWarning:importlib._bootstrap_external,ignore::DeprecationWarning:site,ignore::DeprecationWarning:distutils'
git checkout 2e506c5ab457cba743bb47eb5b8c8eb9dd51d23d -- tests/test_domain_cpp.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest -rA --durations 25 tests/test_domain_cpp.py
echo ">>>>> End Test Output"
