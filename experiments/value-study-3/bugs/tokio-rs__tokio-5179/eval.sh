#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout 9e3fb1673a73046363af64c09a040eeed67f2a4c -- tokio/tests/task_local_set.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(cargo test --test task_local_set --no-fail-fast) 2>&1 | cat
echo '>>>>> End Test Output'
