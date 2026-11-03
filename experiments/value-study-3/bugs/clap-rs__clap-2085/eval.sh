#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout be27f41f8354bfbdf0e4eb43236759b799e7816a -- tests/help.rs tests/hidden_args.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(RUSTFLAGS=-Awarnings cargo test --test help --test hidden_args --no-fail-fast) 2>&1 | cat
echo '>>>>> End Test Output'
