#!/bin/bash
# Runs inside the image, from the root of an extracted tree (see CONTRIBUTING.md):
# each lane of .github/workflows/ci.yml except the remote SSH e2e (run that on the
# host with PEEKY_E2E_DOCKER=1), plus Rust and C++ with LLVM 20's lldb-dap, whose
# readMemory qualifies Rust memory views and libstdc++ strings with a NUL byte (#128),
# C and C++ built by GCC with it (#279, #167), and GDB's DAP, which CUDA qualification
# drives (scripts/cuda/qualify.py). One PASS/FAIL line per lane; non-zero on failure.
set -u
python3 -m venv /v >/dev/null && /v/bin/pip install -q -e '.[dev]' || exit 90
export PATH=/v/bin:$PATH
status=0
lane() {
  name=$1; shift; start=$SECONDS
  if env "$@" > "/tmp/$name.log" 2>&1; then
    echo "PASS $name ($((SECONDS - start)) s): $(tail -1 "/tmp/$name.log")"
  else
    echo "FAIL $name ($((SECONDS - start)) s): $(tail -1 "/tmp/$name.log")"; failures "$name"
    status=1
  fi
}
native=(PEEKY_NATIVE_DAP=/usr/bin/lldb-dap-18 PEEKY_NATIVE_DWARFDUMP=/usr/bin/llvm-dwarfdump-18)
rust=(PEEKY_RUST_LINKER=/usr/bin/clang-18 PEEKY_RUST_DAP=/usr/bin/lldb-dap-18
      PEEKY_RUST_DWARFDUMP=/usr/bin/llvm-dwarfdump-18
      "PEEKY_RUST_CARGO=$(rustup which --toolchain 1.96.0 cargo)"
      "PEEKY_RUST_RUSTC=$(rustup which --toolchain 1.96.0 rustc)")
# (Helper scripts source the 25 lines above for `native` and `rust`: keep them there.)
# What a failed lane's log says, bounded, so that a failure seen once can be diagnosed:
# pytest's summary lines, then each failing test's section (its header, its frames'
# `file:line` lines, its `E` lines and the start of what it printed), at most 8 tests and
# 300 lines; a lane that is not pytest (quality, wheel) shows its last 40 lines. With
# PEEKY_LANE_LOGS set to a mounted directory, the whole log is kept there too.
failures() {
  log=/tmp/$1.log
  grep -E '^(FAILED|ERROR) ' "$log" | head -20
  awk '
    /^=+ (FAILURES|ERRORS) =+$/ { on = 1; next }
    /^=+ / { on = 0 }
    !on { next }
    { $0 = substr($0, 1, 300) }
    /^_+ .+ _+$/ && !/^(_ )+_?$/ { if (++tests > 8) exit; print; e = at = shown = captured = 0; next }
    /^--+ Captured .+ --+$/ { print; captured = 1; shown = 0; next }
    captured { if (shown++ < 15) print; next }
    /^E / { if (e++ < 25) print; next }
    /^[^ ].*:[0-9]+: / { if (at++ < 10) print }
  ' "$log" | head -300 > /tmp/failures.txt
  if [ -s /tmp/failures.txt ]; then sed 's/^/  | /' /tmp/failures.txt; else tail -40 "$log" | sed 's/^/  | /'; fi
  if [ -n "${PEEKY_LANE_LOGS:-}" ]; then cp "$log" "$PEEKY_LANE_LOGS/"; fi
}
lane quality bash -c 'ruff check . && ruff format --check .'
lane unit pytest -q -p no:cacheprovider tests/unit
lane integration pytest -q -p no:cacheprovider tests/integration
lane c "${native[@]}" PEEKY_NATIVE_CC=/usr/bin/clang-18 pytest -q -p no:cacheprovider tests/native
lane cpp "${native[@]}" PEEKY_NATIVE_CXX=/usr/bin/clang++-18 pytest -q -p no:cacheprovider tests/cpp
# C built by GCC with LLVM 20's lldb-dap: the files whose cases depend on how GCC lays out code
# (prologues, loop headers, previews). The `c` lane runs their GCC cases with lldb-dap 18; the
# rest of tests/native expects clang's lines and text.
lane c-gcc-llvm20 PEEKY_NATIVE_DAP=/usr/bin/lldb-dap-20 PEEKY_NATIVE_DWARFDUMP=/usr/bin/llvm-dwarfdump-20 \
  PEEKY_NATIVE_CC=/usr/bin/gcc pytest -q -p no:cacheprovider tests/native/test_native_entry_stops.py \
  tests/native/test_native_loop_headers.py tests/native/test_native_incbin.py \
  tests/native/test_native_previews.py
lane cpp-llvm20 PEEKY_NATIVE_DAP=/usr/bin/lldb-dap-20 PEEKY_NATIVE_DWARFDUMP=/usr/bin/llvm-dwarfdump-20 \
  PEEKY_NATIVE_CXX=/usr/bin/clang++-18 PEEKY_EXPECT_READ_MEMORY=1 pytest -q -p no:cacheprovider tests/cpp
# C++ built by GCC (#167): every C++ case (`-m cpp`: tests/cpp and those of tests/native),
# with g++ 13. The clang lanes also run the GCC side of cases parametrized over both.
lane cpp-gcc "${native[@]}" PEEKY_NATIVE_CXX=/usr/bin/g++ \
  pytest -q -p no:cacheprovider -m cpp tests/cpp tests/native
lane cpp-gcc-llvm20 PEEKY_NATIVE_DAP=/usr/bin/lldb-dap-20 PEEKY_NATIVE_DWARFDUMP=/usr/bin/llvm-dwarfdump-20 \
  PEEKY_NATIVE_CXX=/usr/bin/g++ PEEKY_EXPECT_READ_MEMORY=1 \
  pytest -q -p no:cacheprovider -m cpp tests/cpp tests/native
lane rust "${rust[@]}" pytest -q -p no:cacheprovider tests/rust
lane rust-llvm20 "${rust[@]}" PEEKY_RUST_DAP=/usr/bin/lldb-dap-20 \
  PEEKY_RUST_DWARFDUMP=/usr/bin/llvm-dwarfdump-20 PEEKY_EXPECT_READ_MEMORY=1 \
  pytest -q -p no:cacheprovider tests/rust
lane gdb-dap PEEKY_GDB=/usr/bin/gdb pytest -q -p no:cacheprovider tests/gdb
if python scripts/setup_java_adapter.py --output /tmp/peeky-java > /tmp/java-setup.log 2>&1; then
  lane java PEEKY_JAVA_ADAPTER=/tmp/peeky-java pytest -q -p no:cacheprovider tests/java
else
  echo "FAIL java setup: $(tail -3 /tmp/java-setup.log)"; status=1
fi
lane wheel bash -c 'pip install -q build==1.3.0 && python -m build >/dev/null 2>&1 &&
  python3 -m venv /wheel && /wheel/bin/pip install -q dist/*.whl &&
  /wheel/bin/python -c "import peeky.client; import peeky.dap" &&
  for tool in peeky-worker peeky-agent peeky-probe peeky; do /wheel/bin/$tool --help >/dev/null || exit 1; done &&
  /wheel/bin/python scripts/smoke_wheel.py && echo "wheel ok"'
exit $status
