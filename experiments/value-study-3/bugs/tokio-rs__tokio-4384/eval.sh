#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit (the test file is new: remove it), apply /grade/test.patch,
# run the tests (SWE-bench's eval_script command).
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
rm -f tokio/tests/net_types_unwind.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(RUSTFLAGS=-Awarnings cargo test --package tokio --test net_types_unwind --features full --no-fail-fast) 2>&1 | cat
echo '>>>>> End Test Output'
