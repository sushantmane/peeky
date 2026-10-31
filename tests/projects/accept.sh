#!/bin/bash
# On the host: runs ADR 0008 phase 1's acceptance of one study project (accept.py) in its
# image, with this checkout's committed Peeky, on 4 CPUs. Prints its report as JSON on stdout.
# Usage: tests/projects/accept.sh <image> <project>
# e.g. tests/projects/accept.sh peeky-study/jqlang__jq-2650:control jq
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1
git archive --prefix=peeky/ HEAD src tests/projects |
  docker run --rm -i --cpus=4 --cap-add=SYS_PTRACE --security-opt seccomp=unconfined "$1" \
    bash -c "tar -x -C / && PYTHONPATH=/peeky/src PYTHONDONTWRITEBYTECODE=1 python3 /peeky/tests/projects/accept.py $2"
