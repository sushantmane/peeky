#!/bin/bash
# Builds the Peeky arm's images.
#
#   harness/build_peeky_images.sh [bug_id ...]
#
# 1. peeky-study/peeky-dist:<sha>: the base image plus Peeky, from a wheel built from
#    $PEEKY_CHECKOUT at its HEAD (a `git archive` of HEAD, so untracked or uncommitted files
#    never reach it), then checked by harness/verify_peeky_image.sh (Python, C, Rust and Java
#    probes with no network). Skipped when that tag exists, unless FORCE=1.
# 2. peeky-study/<id>:peeky for each bug given (default: every bugs/<id> whose :control image
#    exists): the control image plus /opt/peeky and /root/.peeky copied from the dist image,
#    labelled with the Peeky sha and the control image's id (a later rebuild of the control
#    image makes it stale: rerun this). The control image is checked first to hold no Peeky,
#    and the result by verify_peeky_image.sh.
#
# Environment:
#   PEEKY_CHECKOUT  the Peeky repository to build (default: the repository holding the study)
#   STUDY_SCRATCH   where the build context is staged (default: ~/.cache/peeky-study)
#   STUDY_BUGS_DIR  the bug directories (default: bugs/ beside harness/)
#   FORCE=1         rebuild images that exist
set -euo pipefail

HARNESS=$(cd "$(dirname "$0")" && pwd)
STUDY=$(dirname "$HARNESS")
DOCKERFILE=$STUDY/docker/peeky.Dockerfile
PEEKY_CHECKOUT=${PEEKY_CHECKOUT:-$(git -C "$STUDY" rev-parse --show-toplevel)}
STUDY_SCRATCH=${STUDY_SCRATCH:-$HOME/.cache/peeky-study}
BUGS=${STUDY_BUGS_DIR:-$STUDY/bugs}
FORCE=${FORCE:-0}

log() { echo "[build_peeky_images] $*" >&2; }

SHA=$(git -C "$PEEKY_CHECKOUT" rev-parse HEAD)
if [ -n "$(git -C "$PEEKY_CHECKOUT" status --porcelain --untracked-files=no)" ]; then
  log "note: $PEEKY_CHECKOUT has uncommitted changes; the image is built from HEAD ($SHA) only"
fi
DIST=peeky-study/peeky-dist:$SHA
log "Peeky $SHA from $PEEKY_CHECKOUT"

image_exists() { docker image inspect "$1" >/dev/null 2>&1; }

build_dist() {
  local ctx=$STUDY_SCRATCH/peeky-dist-$SHA
  rm -rf "$ctx" && mkdir -p "$ctx/src" "$ctx/wheel" "$ctx/docs/lang" "$ctx/pip-tmp"
  git -C "$PEEKY_CHECKOUT" archive --format=tar HEAD | tar -x -C "$ctx/src"
  local python=python3
  if [ -x "$PEEKY_CHECKOUT/.venv/bin/python" ]; then python=$PEEKY_CHECKOUT/.venv/bin/python; fi
  log "building the wheel with $python"
  (cd "$ctx/src" && TMPDIR=$ctx/pip-tmp "$python" -m pip wheel . --no-deps -q -w "$ctx/wheel")
  ls "$ctx"/wheel/*.whl >/dev/null
  cp "$ctx/src/docs/AGENT_GUIDE.md" "$ctx/src/docs/COOKBOOK.md" "$ctx/src/docs/ERRORS.md" "$ctx/docs/"
  cp "$ctx"/src/docs/lang/* "$ctx/docs/lang/"
  rm -rf "$ctx/src" "$ctx/pip-tmp"
  log "building $DIST"
  docker build -q --target dist --build-arg PEEKY_SHA="$SHA" -t "$DIST" -f "$DOCKERFILE" "$ctx" >/dev/null
  "$HARNESS/verify_peeky_image.sh" "$DIST"
}

if image_exists "$DIST" && [ "$FORCE" != 1 ]; then
  log "$DIST exists"
else
  build_dist
fi

control_image() {
  # bug.yaml's `image:` (peeky-study/<id lowercased>:control), else the default name.
  local id=$1 yaml=$BUGS/$1/bug.yaml image=""
  if [ -f "$yaml" ]; then
    image=$(sed -n 's/^image:[[:space:]]*["'\'']\{0,1\}\([^"'\'' ]*\).*/\1/p' "$yaml" | head -1)
  fi
  echo "${image:-peeky-study/$(echo "$id" | tr '[:upper:]' '[:lower:]'):control}"
}

if [ $# -gt 0 ]; then
  ids=("$@")
else
  ids=()
  for dir in "$BUGS"/*/; do
    [ -d "$dir" ] || continue
    id=$(basename "$dir")
    image_exists "$(control_image "$id")" && ids+=("$id")
  done
fi

empty_ctx=$STUDY_SCRATCH/empty-context
mkdir -p "$empty_ctx"
failed=0
for id in ${ids[@]+"${ids[@]}"}; do
  control=$(control_image "$id")
  peeky=${control%:control}:peeky
  if ! image_exists "$control"; then
    log "$id: no image $control; skipped"; failed=1; continue
  fi
  if docker run --rm --network none --entrypoint bash "$control" -c \
      'command -v peeky || test -e /opt/peeky || test -e /root/.peeky || python3 -c "import peeky" ||
       { test -x /opt/venv/bin/python && /opt/venv/bin/python -c "import peeky"; }' \
      >/dev/null 2>&1; then
    log "$id: $control holds Peeky; refusing to build $peeky"; failed=1; continue
  fi
  if image_exists "$peeky" && [ "$FORCE" != 1 ] &&
     [ "$(docker image inspect -f '{{ index .Config.Labels "peeky-study.peeky-sha" }}' "$peeky")" = "$SHA" ] &&
     [ "$(docker image inspect -f '{{ index .Config.Labels "peeky-study.control-id" }}' "$peeky")" = \
       "$(docker image inspect -f '{{ .Id }}' "$control")" ]; then
    log "$id: $peeky is current"; continue
  fi
  log "$id: building $peeky"
  docker build -q --target bug --build-arg BUG_IMAGE="$control" --build-arg DIST_IMAGE="$DIST" \
    --build-arg PEEKY_SHA="$SHA" \
    --label "peeky-study.control-id=$(docker image inspect -f '{{ .Id }}' "$control")" \
    -t "$peeky" -f "$DOCKERFILE" "$empty_ctx" >/dev/null
  "$HARNESS/verify_peeky_image.sh" "$peeky" >&2 ||
    { log "$id: $peeky failed its check"; failed=1; continue; }
  log "$id: $peeky ok"
done
exit $failed
