#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
# tox.ini's [testenv] settings (SWE-bench runs these tests through tox --current-env).
export PYTHONWARNINGS=all
export PYTEST_ADDOPTS='--color yes'
git checkout f1061c012e214f16fd8790dec3c283d787e3daa8 -- tests/test_build_latex.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
python -X dev -X warn_default_encoding -m pytest -rA --durations 25 tests/test_build_latex.py
echo ">>>>> End Test Output"
