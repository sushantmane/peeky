#!/bin/bash
# Every Linux lane, as on a laptop: build the CI image, then run run_lanes.sh in it.
# Run from the repository root. Needs Docker that allows ptrace in a container.
set -euo pipefail
echo "--- build the CI image"
docker build --progress=plain -t peeky-ci-linux scripts/ci_linux
echo "--- run the lanes"
git archive HEAD | docker run --rm -i --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
  peeky-ci-linux bash -c "mkdir /w && tar -x -C /w && cd /w && scripts/ci_linux/run_lanes.sh"
