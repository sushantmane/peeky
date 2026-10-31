#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout ab53bf0c4727ae63c6a3a3d772b7bd837d2f49c3 -- tokio-util/tests/time_delay_queue.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(cargo test --test time_delay_queue --no-fail-fast) 2>&1 | cat
echo '>>>>> End Test Output'
