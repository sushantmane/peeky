#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the test binaries whose files the
# test patch changes and that hold tests in this configuration (the others are compiled out
# without --cfg tokio_unstable).
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout fb7dec0e952d0d796b98650998c44a21d6775564 -- stress-test/Cargo.toml tests-build/Cargo.toml tests-integration/Cargo.toml tokio-test/Cargo.toml tokio-test/src/lib.rs tokio-util/tests/task_join_map.rs tokio/tests/_require_full.rs tokio/tests/dump.rs tokio/tests/macros_select.rs tokio/tests/macros_test.rs tokio/tests/rt_basic.rs tokio/tests/rt_common.rs tokio/tests/rt_handle.rs tokio/tests/rt_local.rs tokio/tests/rt_metrics.rs tokio/tests/rt_threaded.rs tokio/tests/rt_threaded_alt.rs tokio/tests/rt_unstable_metrics.rs tokio/tests/task_builder.rs tokio/tests/task_hooks.rs tokio/tests/task_id.rs tokio/tests/task_join_set.rs tokio/tests/task_local_set.rs tokio/tests/task_yield_now.rs tokio/tests/tracing_sync.rs tokio/tests/tracing_task.rs tokio/tests/tracing_time.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(cargo test --test macros_select --test macros_test --test rt_basic --test rt_common --test rt_handle --test rt_metrics --test rt_threaded --test task_id --test task_join_set --test task_local_set --no-fail-fast) 2>&1 | cat
echo '>>>>> End Test Output'
