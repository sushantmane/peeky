#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
# tox.ini's [testenv] settings (SWE-bench runs these tests through tox --current-env).
export PYTHONWARNINGS=error
git checkout ac2b7599d212af7d04649959ce6926c63c3133fa -- tests/roots/test-ext-inheritance_diagram/conf.py tests/roots/test-ext-inheritance_diagram/index.rst tests/roots/test-ext-inheritance_diagram/test.py tests/test_ext_inheritance_diagram.py
rm -rf tests/roots/test-ext-inheritance_diagram/subdir/index.rst tests/roots/test-ext-inheritance_diagram/subdir/other.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
python -X dev -X warn_default_encoding -m pytest -rA --durations 25 tests/roots/test-ext-inheritance_diagram/conf.py tests/roots/test-ext-inheritance_diagram/index.rst tests/roots/test-ext-inheritance_diagram/test.py tests/test_ext_inheritance_diagram.py tests/roots/test-ext-inheritance_diagram/subdir/index.rst tests/roots/test-ext-inheritance_diagram/subdir/other.py
echo ">>>>> End Test Output"
