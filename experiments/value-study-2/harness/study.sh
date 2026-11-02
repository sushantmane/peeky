#!/bin/bash
# Runs one of round 1's harness scripts with round 2's study config (../study.json):
#
#   harness/study.sh schedule.py --dry-run
#   harness/study.sh run_one.py <bug> peeky-first 1 --fake-agent noop
#   harness/study.sh analyze.py
#   harness/study.sh prepare_judging.py results/runs <judge dir>
#
# The config names the bugs (round 1's), this round's results directory, the seed, the three
# arms with their images and prompts, and the comparisons. Nothing of round 1's code is copied.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROUND2=$(dirname "$HERE")
script=${1:?usage: harness/study.sh <script.py> [args...]}
shift
export PEEKY_STUDY_CONFIG=$ROUND2/study.json
cd "$ROUND2"
exec python3 "$ROUND2/../value-study/harness/$script" "$@"
