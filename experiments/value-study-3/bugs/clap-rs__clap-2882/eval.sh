#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): remove the test file the hidden tests
# add, apply /grade/test.patch, run that test target of clap_derive.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
rm -f clap_derive/tests/help.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(RUSTFLAGS=-Awarnings cargo test -p clap_derive --test help) 2>&1 | cat
echo '>>>>> End Test Output'
