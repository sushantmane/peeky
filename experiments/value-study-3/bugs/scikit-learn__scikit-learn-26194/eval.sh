#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
git checkout e886ce4e1444c61b865e7839c9cff5464ee20ace -- sklearn/metrics/tests/test_ranking.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest -rA sklearn/metrics/tests/test_ranking.py
echo ">>>>> End Test Output"
