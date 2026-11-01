#!/bin/bash
# Builds round 2's Peeky images with round 1's script (../../value-study/harness/build_peeky_images.sh)
# and this round's Dockerfile (../docker/peeky.Dockerfile, with prepare.sh):
#
#   PEEKY_CHECKOUT=<peeky checkout at the commit to study> harness/build_images.sh [bug_id ...]
#
# peeky-study/peeky-dist-r2:<sha>, then peeky-study/<id>:peeky-r2 for each bug (default: every
# bug whose :control image exists), one build at a time, each limited to 4 CPUs and 6 GB.
# FORCE=1 rebuilds images that exist, as after a change to prepare.sh.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROUND2=$(dirname "$HERE")
ROUND1=$(cd "$ROUND2/../value-study" && pwd)
export STUDY_DOCKERFILE=$ROUND2/docker/peeky.Dockerfile
export STUDY_DIST_REPO=peeky-study/peeky-dist-r2
export STUDY_PEEKY_TAG=peeky-r2
export STUDY_BUG_CONTEXT=$ROUND2/docker
export STUDY_BUGS_DIR=${STUDY_BUGS_DIR:-$ROUND1/bugs}
export STUDY_SCRATCH=${STUDY_SCRATCH:-$HOME/.cache/peeky-study-2}
export STUDY_BUILD_OPTS=${STUDY_BUILD_OPTS:---resource cpu-quota=400000 --resource memory=6g}
exec "$ROUND1/harness/build_peeky_images.sh" "$@"
