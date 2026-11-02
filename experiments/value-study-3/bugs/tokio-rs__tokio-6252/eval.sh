#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout 48345d6e4822b4c0ea00d5c1c075a6b5ac663acf -- tokio/tests/sync_watch.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(cargo test --test sync_watch --no-fail-fast) 2>&1 | cat
echo '>>>>> End Test Output'
