#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests of the changed test
# module (cargo rebuilds the nu binary they run).
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout 6e1e824473e15eba246e1c43704c5d88fa237a17 -- tests/shell/pipeline/commands/external.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(cargo test --package nu --test main -- shell::pipeline::commands::external::) 2>&1 | cat
echo '>>>>> End Test Output'
