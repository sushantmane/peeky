#!/bin/bash
# Quality, unit and integration tests in Ubuntu 24.04 (Python 3.12), with the LLVM 18 tools the
# unit tests look for. The hosted image's own Python is 3.10, too old for Peeky.
set -euo pipefail
git archive HEAD | docker run --rm -i ubuntu:24.04 bash -c '
  set -euo pipefail
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends python3 python3-venv git ca-certificates \
    clang-18 lldb-18 llvm-18 >/dev/null
  mkdir /w && tar -x -C /w && cd /w
  python3 -m venv /v && . /v/bin/activate
  pip install -q -e ".[dev]"
  echo "--- quality" && ruff check . && ruff format --check .
  echo "--- unit" && pytest -q -p no:cacheprovider tests/unit
  echo "--- integration" && pytest -q -p no:cacheprovider tests/integration'
