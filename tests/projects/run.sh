#!/bin/bash
# On the host: runs rebuild.sh with this checkout's committed Peeky, on 4 CPUs, with no
# network, in the acceptance image of a study image (tests/projects/Dockerfile, as accept.sh).
# Usage: tests/projects/run.sh <image> <project> [variant]
# e.g. tests/projects/run.sh peeky-study/valkey-io__valkey-1842:control valkey
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1
image=$(git archive HEAD:tests/projects | docker build -q --build-arg BASE="$1" -) || {
  echo "the acceptance image for $1 didn't build" >&2
  exit 1
}
git archive --prefix=peeky/ HEAD src tests/projects |
  docker run --rm -i --network=none --cpus=4 "$image" \
    bash -c 'tar -x -C / && bash /peeky/tests/projects/rebuild.sh "$@"' rebuild "$2" ${3:+"$3"}
