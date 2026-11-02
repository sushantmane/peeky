#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout 38d1bcd9df6af2de436c5f898829d071f8e46988 -- tokio/tests/io_async_fd.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(cargo test --test io_async_fd --no-fail-fast) 2>&1 | cat
echo '>>>>> End Test Output'
