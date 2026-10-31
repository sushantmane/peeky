#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout 53cf021b813c61cdeace26e863c89f65f6e92abd -- tokio/tests/sync_broadcast.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(cargo test --test sync_broadcast --no-fail-fast) 2>&1 | cat
echo '>>>>> End Test Output'
