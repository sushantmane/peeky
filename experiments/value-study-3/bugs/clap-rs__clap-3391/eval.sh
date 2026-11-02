#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout 7c79e76f2f55b1aab083fa201a86bbc8c8c35711 -- tests/builder/possible_values.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(RUSTFLAGS=-Awarnings cargo test --test builder -- possible_values::) 2>&1 | cat
echo '>>>>> End Test Output'
