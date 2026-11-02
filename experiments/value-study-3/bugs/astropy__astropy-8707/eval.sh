#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout a85a0747c54bac75e9c3b2fe436b105ea029d6cf -- astropy/io/fits/tests/test_header.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest -rA -vv -o console_output_style=classic --tb=no astropy/io/fits/tests/test_header.py
echo ">>>>> End Test Output"
