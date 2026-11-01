#!/bin/bash
# On the host: runs ADR 0008 phase 1's acceptance of one study project (accept.py) with this
# checkout's committed Peeky, on 4 CPUs, with no network. Its image is the study's image
# with libc++ added, pinned (tests/projects/Dockerfile, built here, with a network, and
# cached). Fixtures, logs and Peeky's state go to one scratch directory, removed at the end.
# Prints the report as JSON on stdout.
# Usage: tests/projects/accept.sh <image> <project>
# e.g. tests/projects/accept.sh peeky-study/jqlang__jq-2650:control jq
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1
image=$(git archive HEAD:tests/projects | docker build -q --build-arg BASE="$1" -) || {
  echo "the acceptance image for $1 didn't build" >&2
  exit 1
}
git archive --prefix=peeky/ HEAD src tests/projects |
  docker run --rm -i --network=none --cpus=4 --cap-add=SYS_PTRACE \
    --security-opt seccomp=unconfined "$image" bash -c '
      # The archived directories are group-writable: Peeky keeps its state only where no
      # other user can write.
      tar -x -C / && chmod -R go-w /peeky && scratch=$(mktemp -d /peeky/scratch.XXXXXX) || exit 1
      trap "rm -rf \"$scratch\"" EXIT
      PEEKY_ACCEPT_SCRATCH=$scratch PYTHONPATH=/peeky/src PYTHONDONTWRITEBYTECODE=1 \
        python3 /peeky/tests/projects/accept.py "$0"' "$2"
