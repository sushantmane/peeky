#!/bin/bash
# Runs pytest with one lane's environment, inside the image, from the root of an extracted
# tree: `scripts/ci_linux/run_tests.sh c tests/native/test_native_binary.py -k describe`.
# Lanes: c, c-gcc, cpp, cpp-gcc, rust. With the image built for linux/amd64 on an arm64
# machine (CONTRIBUTING.md), it checks x86-64 code: under emulation the debugger can't stop
# a program, so only cases that don't run it (`describe`) mean anything there.
set -u
lane=${1:?usage: run_tests.sh <c|c-gcc|cpp|cpp-gcc|rust> [pytest arguments]}
shift
source <(head -25 "$(dirname "$0")/run_lanes.sh" | grep -v '^set -u')
case $lane in
  c) env=("${native[@]}" PEEKY_NATIVE_CC=/usr/bin/clang-18) ;;
  c-gcc) env=("${native[@]}" PEEKY_NATIVE_CC=/usr/bin/gcc) ;;
  cpp) env=("${native[@]}" PEEKY_NATIVE_CXX=/usr/bin/clang++-18) ;;
  cpp-gcc) env=("${native[@]}" PEEKY_NATIVE_CXX=/usr/bin/g++) ;;
  rust) env=("${rust[@]}") ;;
  *) echo "unknown lane: $lane" >&2; exit 2 ;;
esac
exec env "${env[@]}" pytest -q -p no:cacheprovider "$@"
