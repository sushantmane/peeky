#!/bin/bash
# Runs inside a value-study image (experiments/value-study/bugs/<bug>/Dockerfile): builds its
# project (build.sh), then measures the binary (measure.py).
# Usage: rebuild.sh <jq|valkey|micropython|fmt3158|fmt3272|json> [variant]
set -u
binary=$(bash /peeky/tests/projects/build.sh "$@") || exit 1
compiler=clang-18
[[ $1 == fmt* || $1 == json ]] && compiler=clang++-18
[ "${2:-}" = gcc ] && compiler=gcc-13
PYTHONPATH=/peeky/src PYTHONDONTWRITEBYTECODE=1 python3 /peeky/tests/projects/measure.py \
  llvm-dwarfdump-18 "/testbed/$binary" /testbed "$compiler"
