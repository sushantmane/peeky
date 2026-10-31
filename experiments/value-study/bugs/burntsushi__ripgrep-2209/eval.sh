#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout 4dc6c73c5a9203c5a8a89ce2161feca542329812 -- tests/regression.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(RUSTFLAGS=-Awarnings cargo test --package ripgrep --test integration -- regression::r2208 --exact) 2>&1 | cat
echo '>>>>> End Test Output'
