#!/bin/bash
# Builds round 3's Peeky images with round 1's script and round 2's Dockerfile and prepare.sh:
#
#   PEEKY_CHECKOUT=<peeky checkout> harness/build_images.sh [bug_id ...]
#
# peeky-study/peeky-dist-r3:<sha>, then peeky-study/<id>:peeky-r3 for each bug given (default:
# every bugs/<id> whose :control image exists). Each bug's build context is round 2's docker/
# directory plus bug/: the bug's peeky.sh (a C or C++ project's Peeky build) or rust.json (its
# Rust config fields), when it has one. Each build is limited to 4 CPUs and 6 GB. FORCE=1
# rebuilds images that exist, as after a change to a bug's peeky.sh.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROUND3=$(dirname "$HERE")
ROUND2=$(cd "$ROUND3/../value-study-2" && pwd)
ROUND1=$(cd "$ROUND3/../value-study" && pwd)
export STUDY_DOCKERFILE=$ROUND2/docker/peeky.Dockerfile
export STUDY_DIST_REPO=peeky-study/peeky-dist-r3
export STUDY_PEEKY_TAG=peeky-r3
export STUDY_BUGS_DIR=${STUDY_BUGS_DIR:-$ROUND3/bugs}
export STUDY_SCRATCH=${STUDY_SCRATCH:-$HOME/.cache/peeky-study-3}
export STUDY_BUILD_OPTS=${STUDY_BUILD_OPTS:---resource cpu-quota=400000 --resource memory=6g --build-arg STUDY_ROUND=3}
if [ $# -eq 0 ]; then
  set -- $(for d in "$STUDY_BUGS_DIR"/*/; do
    id=$(basename "$d")
    docker image inspect "peeky-study/$(echo "$id" | tr '[:upper:]' '[:lower:]'):control" \
      >/dev/null 2>&1 && echo "$id"
  done)
fi
status=0
for id in "$@"; do
  context=$STUDY_SCRATCH/context-$id
  rm -rf "$context" && mkdir -p "$context/bug"
  cp "$ROUND2/docker/prepare.sh" "$context/"
  for file in peeky.sh rust.json; do
    if [ -f "$STUDY_BUGS_DIR/$id/$file" ]; then cp "$STUDY_BUGS_DIR/$id/$file" "$context/bug/"; fi
  done
  STUDY_BUG_CONTEXT=$context "$ROUND1/harness/build_peeky_images.sh" "$id" || status=1
done
exit $status
