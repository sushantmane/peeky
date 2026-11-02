#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, rebuild the nu binary the tests
# run, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout 399319476aa4decd7cced93488180d96e56bad64 -- src/tests/test_type_check.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(RUSTFLAGS=-Awarnings cargo build --package nu --bin nu && RUSTFLAGS=-Awarnings cargo test --package nu --bin nu -- tests::test_type_check::) 2>&1 | cat
echo '>>>>> End Test Output'
