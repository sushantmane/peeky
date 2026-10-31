#!/bin/bash
# Grades an attempt: restores the files the hidden tests touch to the base commit, applies
# /grade/test.patch and runs the tests between the SWE-bench markers. cwd: /testbed.
set -uo pipefail
exec 2>&1
cd /testbed
# extract_zipped_paths caches extracted files in /tmp; a copy left by an earlier run of
# tests/test_utils.py would make test_zipped_paths_extracted compare against a stale file.
rm -f /tmp/test_utils.py
git checkout 0192aac24123735b3eaf9b08df46429bb770c283 -- tests/test_utils.py
git apply -v /grade/test.patch || { echo ">>>>> Patch Apply Failed"; exit 1; }
echo ">>>>> Start Test Output"
pytest -rA tests/test_utils.py
echo ">>>>> End Test Output"
