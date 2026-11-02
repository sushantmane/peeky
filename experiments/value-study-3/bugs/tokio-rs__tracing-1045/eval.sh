#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout 0dc8ef2da97f605689fff17f6c38b69e105a5281 -- tracing-core/tests/dispatch.rs tracing-core/tests/global_dispatch.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(cargo test -p tracing-core --test dispatch --test global_dispatch --no-fail-fast) 2>&1 | cat
echo '>>>>> End Test Output'
