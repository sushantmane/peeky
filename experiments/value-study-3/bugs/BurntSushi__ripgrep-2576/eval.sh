#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout fed4fea217abbc502f2e823465de903c8f2b623d -- tests/regression.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(RUSTFLAGS=-Awarnings cargo test --package ripgrep --test integration -- regression) 2>&1 | cat
echo '>>>>> End Test Output'
