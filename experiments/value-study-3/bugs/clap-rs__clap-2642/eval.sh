#!/bin/bash
# Grades an attempt inside the container (cwd /testbed): restore the files the hidden
# tests touch to the base commit, apply /grade/test.patch, run the tests of the workspace's
# default members (clap, clap_derive, clap_generate), without doc tests.
set -uo pipefail
cd /testbed
export CARGO_NET_OFFLINE=true
git config --global --add safe.directory /testbed
git checkout 8ca62aa18559d8c30056d6c1ebfda2840e0a322c -- clap_generate/tests/completions/elvish.rs clap_generate/tests/completions/fish.rs clap_generate/tests/completions/powershell.rs clap_generate/tests/completions/zsh.rs clap_generate/tests/value_hints.rs tests/app_from_crate.rs tests/app_settings.rs tests/arg_aliases.rs tests/arg_aliases_short.rs tests/cargo.rs tests/derive_order.rs tests/flag_subcommands.rs tests/groups.rs tests/help.rs tests/hidden_args.rs tests/macros.rs tests/subcommands.rs tests/template_help.rs tests/version.rs
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(RUSTFLAGS=-Awarnings cargo test --tests --no-fail-fast) 2>&1 | cat
echo '>>>>> End Test Output'
