#!/bin/bash
# Runs inside the image, from the root of an extracted tree (see CONTRIBUTING.md):
# each lane of .github/workflows/ci.yml except the remote SSH e2e (run that on the
# host with PEEKY_E2E_DOCKER=1), plus Rust with LLVM 20's lldb-dap, whose readMemory
# qualifies Rust memory views, and GDB's DAP, which CUDA qualification drives
# (scripts/cuda/qualify.py). One PASS/FAIL line per lane; non-zero on failure.
set -u
python3 -m venv /v >/dev/null && /v/bin/pip install -q -e '.[dev]' || exit 90
export PATH=/v/bin:$PATH
status=0
lane() {
  name=$1; shift; start=$SECONDS
  if env "$@" > "/tmp/$name.log" 2>&1; then
    echo "PASS $name ($((SECONDS - start)) s): $(tail -1 "/tmp/$name.log")"
  else
    echo "FAIL $name: $(tail -1 "/tmp/$name.log")"; grep -E '^(FAILED|ERROR)' "/tmp/$name.log" | head -20
    status=1
  fi
}
native=(PEEKY_NATIVE_DAP=/usr/bin/lldb-dap-18 PEEKY_NATIVE_DWARFDUMP=/usr/bin/llvm-dwarfdump-18)
rust=(PEEKY_RUST_LINKER=/usr/bin/clang-18 PEEKY_RUST_DAP=/usr/bin/lldb-dap-18
      PEEKY_RUST_DWARFDUMP=/usr/bin/llvm-dwarfdump-18
      "PEEKY_RUST_CARGO=$(rustup which --toolchain 1.96.0 cargo)"
      "PEEKY_RUST_RUSTC=$(rustup which --toolchain 1.96.0 rustc)")
lane quality bash -c 'ruff check . && ruff format --check .'
lane unit pytest -q -p no:cacheprovider tests/unit
lane integration pytest -q -p no:cacheprovider tests/integration
lane c "${native[@]}" PEEKY_NATIVE_CC=/usr/bin/clang-18 pytest -q -p no:cacheprovider tests/native
lane cpp "${native[@]}" PEEKY_NATIVE_CXX=/usr/bin/clang++-18 pytest -q -p no:cacheprovider tests/cpp
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
