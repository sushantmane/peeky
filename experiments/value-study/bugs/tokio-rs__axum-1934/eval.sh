#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout e97462d45251c21ffd1ba4fb5db30f3cf6f6cb6c -- axum/src/routing/tests/fallback.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(RUSTFLAGS=-Awarnings cargo test --package axum --lib -- routing::tests::fallback) 2>&1 | cat
echo '>>>>> End Test Output'
