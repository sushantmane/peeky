#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, rebuild the nu binary the tests run,
# run the tests of the changed test module.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout ecb3b3a364b3332ac9df51dbecd780f1f73e21e8 -- src/tests/test_help.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(cargo build --bin nu && cargo test --bin nu --no-fail-fast -- tests::test_help) 2>&1 | cat
echo '>>>>> End Test Output'
