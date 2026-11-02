#!/bin/bash
# Round 3's screening runs: round 1's schedule.py with the screening config (control only,
# results/screening/), over candidates/survivors.txt. Paid: Copilot CLI attempts.
#   harness/screen.sh wave1    # attempt 1 of every survivor
#   harness/screen.sh wave2    # attempts 2 and 3 of those whose attempt 1 failed
# Then harness/screen.py status --write.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROUND3=$(dirname "$HERE")
export PEEKY_STUDY_CONFIG=$ROUND3/study-screening.json
cd "$ROUND3"
case ${1:?usage: harness/screen.sh wave1|wave2 [schedule.py options]} in
  wave1) reps=1; only=$(paste -sd, candidates/survivors.txt) ;;
  wave2)
    reps=3
    only=$(python3 harness/screen.py status | sed -n 's/.*attempts 2 and 3 still to run for: //p')
    if [ "$only" = none ]; then echo "nothing to run"; exit 0; fi
    ;;
  *) echo "wave1 or wave2" >&2; exit 2 ;;
esac
shift
exec python3 "$ROUND3/../value-study/harness/schedule.py" --reps "$reps" --only "$only" \
  --concurrency 3 --budget-minutes 40 --budget-credits 250 "$@"
