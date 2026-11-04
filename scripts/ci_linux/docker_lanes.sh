#!/bin/bash
# The Linux lanes of run_lanes.sh in the CI image, as CI's lane jobs run them. Run it from the
# repository root. It tests the committed tree (`git archive HEAD`), and needs Docker that
# allows ptrace in a container.
#   PEEKY_LANES="unit c"       only those lanes; unset or empty runs every lane
#   PEEKY_CI_IMAGE=<image>     an image already built (CI loads it from its layer cache);
#                              unset builds scripts/ci_linux as peeky-ci-linux
#   PEEKY_LANE_LOGS_DIR=<dir>  keeps each failed lane's whole log there
set -euo pipefail
image=${PEEKY_CI_IMAGE:-}
if [ -z "$image" ]; then
  image=peeky-ci-linux
  docker build -t "$image" scripts/ci_linux
fi
logs=()
if [ -n "${PEEKY_LANE_LOGS_DIR:-}" ]; then
  mkdir -p "$PEEKY_LANE_LOGS_DIR"
  logs=(-v "$(cd "$PEEKY_LANE_LOGS_DIR" && pwd):/lane-logs" -e PEEKY_LANE_LOGS=/lane-logs)
fi
git archive HEAD | docker run --rm -i -e PEEKY_LANES="${PEEKY_LANES:-}" ${logs[@]+"${logs[@]}"} \
  --cap-add=SYS_PTRACE --security-opt seccomp=unconfined "$image" \
  bash -c 'mkdir /w && tar -x -C /w && cd /w && scripts/ci_linux/run_lanes.sh'
