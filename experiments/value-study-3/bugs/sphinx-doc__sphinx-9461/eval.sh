#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
# tox.ini's [testenv] settings (SWE-bench runs these tests through tox --current-env).
export PYTHONWARNINGS='all,ignore::ImportWarning:importlib._bootstrap_external,ignore::DeprecationWarning:site,ignore::DeprecationWarning:distutils,ignore::DeprecationWarning:pip._vendor.packaging.version'
export PYTEST_ADDOPTS='--color yes'
git checkout 939c7bb7ff7c53a4d27df067cea637540f0e1dad -- tests/roots/test-ext-autodoc/target/properties.py tests/test_domain_py.py tests/test_ext_autodoc_autoclass.py tests/test_ext_autodoc_autoproperty.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
python -X dev -m pytest -rA --durations 25 tests/roots/test-ext-autodoc/target/properties.py tests/test_domain_py.py tests/test_ext_autodoc_autoclass.py tests/test_ext_autodoc_autoproperty.py
echo ">>>>> End Test Output"
