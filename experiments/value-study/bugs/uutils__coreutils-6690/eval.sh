#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout af86aee6b86a99736ca304ff656d0e4c6d547333 -- tests/by-util/test_cp.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(cargo test --no-fail-fast -- test_cp_cp test_cp_same_file test_cp_multiple_files test_cp_single_file test_cp_no_file) 2>&1 | cat
echo '>>>>> End Test Output'
