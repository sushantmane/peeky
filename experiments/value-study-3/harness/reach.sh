#!/bin/bash
# Round 3's reach check: round 2's reach.py on round 3's bugs, each bug's spec in its reach.json,
# against its peeky-study/<id>:peeky-r3 image. Records go to results/reach/<Peeky sha, 12>/.
#   harness/reach.sh [bug_id ...]
#   harness/reach.sh --report results/reach/<sha, 12>
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROUND3=$(dirname "$HERE")
exec python3 "$ROUND3/../value-study-2/harness/reach.py" --specs none --bugs-dir "$ROUND3/bugs" \
  --tag peeky-r3 "$@"
