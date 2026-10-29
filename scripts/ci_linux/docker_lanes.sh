#!/bin/bash
# Linux lanes, as on a laptop: build the CI image, then run run_lanes.sh in it.
# Run from the repository root. Needs Docker that allows ptrace in a container.
# PEEKY_LANES="unit c" runs only those lanes; unset runs every lane.
set -euo pipefail
echo "--- build the CI image"
docker build --progress=plain -t peeky-ci-linux scripts/ci_linux
echo "--- run the lanes: ${PEEKY_LANES:-all}"
git archive HEAD | docker run --rm -i -e PEEKY_LANES="${PEEKY_LANES:-}" \
  --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
  peeky-ci-linux bash -c "mkdir /w && tar -x -C /w && cd /w && scripts/ci_linux/run_lanes.sh"
