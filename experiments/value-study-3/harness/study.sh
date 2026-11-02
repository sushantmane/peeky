#!/bin/bash
# Runs one of round 1's harness scripts with round 3's study config (../study.json):
#   harness/study.sh schedule.py --dry-run
#   harness/study.sh grade.py <bug> [--gold]
#   harness/study.sh run_one.py <bug> peeky-first 1 --fake-agent gold --results <dir>
# Nothing of round 1's code is copied.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROUND3=$(dirname "$HERE")
script=${1:?usage: harness/study.sh <script.py> [args...]}
shift
export PEEKY_STUDY_CONFIG=$ROUND3/study.json
cd "$ROUND3"
exec python3 "$ROUND3/../value-study/harness/$script" "$@"
