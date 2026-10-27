#!/bin/bash
# Every Linux lane, as on a laptop: build the CI image, then run scripts/ci_linux/run_lanes.sh in it.
set -euo pipefail
docker build -q -t peeky-ci-linux scripts/ci_linux
git archive HEAD | docker run --rm -i --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
  peeky-ci-linux bash -c "mkdir /w && tar -x -C /w && cd /w && scripts/ci_linux/run_lanes.sh"
