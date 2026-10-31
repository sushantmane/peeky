#!/bin/bash
set -uxo pipefail
cd /testbed
git config --global --add safe.directory /testbed
git checkout 2d7fb9a715d8c9f1f7ba73a62cfd587fa6ffc24a -- tests/basics/try_finally_return.py
git apply -v /grade/test.patch
source ./tools/ci.sh
ci_unix_build_helper VARIANT=standard
gcc -shared -o tests/ports/unix/ffi_lib.so tests/ports/unix/ffi_lib.c
echo '>>>>> Start Test Output'
(cd tests && MICROPY_CPYTHON3=python3 MICROPY_MICROPYTHON=../ports/unix/build-standard/micropython ./run-tests.py -i try) | cat
echo '>>>>> End Test Output'
git checkout 2d7fb9a715d8c9f1f7ba73a62cfd587fa6ffc24a -- tests/basics/try_finally_return.py
