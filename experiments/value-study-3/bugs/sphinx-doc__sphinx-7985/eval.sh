#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
export PYTHONWARNINGS='all,ignore::ImportWarning:importlib._bootstrap_external,ignore::DeprecationWarning:site,ignore::DeprecationWarning:distutils'
git checkout f30284ef926ebaf04b176f21b421e2dffc679792 -- tests/roots/test-linkcheck/links.txt tests/test_build_linkcheck.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
python -m pytest -rA --durations 25 tests/test_build_linkcheck.py
echo ">>>>> End Test Output"
