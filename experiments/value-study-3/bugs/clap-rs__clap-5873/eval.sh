#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout 1d5c6798dc16db0b8130a0c73c8a5d818ec22131 -- tests/builder/ignore_errors.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(RUSTFLAGS=-Awarnings cargo test --test builder -- ignore_errors::) 2>&1 | cat
echo '>>>>> End Test Output'
