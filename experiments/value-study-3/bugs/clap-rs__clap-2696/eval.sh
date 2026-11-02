#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout 441ff68c2d63536f0e2fd7e171d6fa7d76b2f4c3 -- tests/posix_compatible.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(RUSTFLAGS=-Awarnings cargo test --tests --no-fail-fast) 2>&1 | cat
echo '>>>>> End Test Output'
