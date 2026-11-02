#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout 84e41d4affe2f94d892c5ab2320db6d695fca536 -- tokio/tests/macros_select.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(cargo test --test macros_select --no-fail-fast) 2>&1 | cat
echo '>>>>> End Test Output'
