#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
# tox.ini's [testenv] settings (SWE-bench runs these tests through tox --current-env).
export PYTHONWARNINGS='all,ignore::ImportWarning:importlib._bootstrap_external,ignore::DeprecationWarning:site,ignore::DeprecationWarning:distutils,ignore::DeprecationWarning:pip._vendor.packaging.version'
export PYTEST_ADDOPTS='--color yes'
git checkout 876fa81e0a038cda466925b85ccf6c5452e0f685 -- tests/roots/test-ext-autodoc/target/classes.py tests/test_ext_autodoc_autoclass.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
python -X dev -m pytest -rA --durations 25 tests/roots/test-ext-autodoc/target/classes.py tests/test_ext_autodoc_autoclass.py
echo ">>>>> End Test Output"
