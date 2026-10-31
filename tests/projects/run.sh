#!/bin/bash
# On the host: runs rebuild.sh in a study image with this checkout's committed Peeky.
# Usage: tests/projects/run.sh <image> <project> [variant]
# e.g. tests/projects/run.sh peeky-study/valkey-io__valkey-1842:control valkey
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1
git archive --prefix=peeky/ HEAD src tests/projects |
  docker run --rm -i --cpus=4 "$1" bash -c "tar -x -C / && bash /peeky/tests/projects/rebuild.sh $2 ${3:-}"
