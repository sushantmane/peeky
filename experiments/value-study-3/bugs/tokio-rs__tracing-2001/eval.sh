#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): remove the test file the hidden tests
# add, apply /grade/test.patch, run the tracing crate's tests (not its doc tests).
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
rm -f tracing/tests/no_collector.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(cargo test -p tracing --tests --no-fail-fast) 2>&1 | cat
echo '>>>>> End Test Output'
