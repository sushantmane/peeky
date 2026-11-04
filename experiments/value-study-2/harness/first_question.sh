#!/bin/bash
# The first question with `--test` (ADR 0009), with no agent: one Peeky command per bug, in
# round 2's Peeky images, run from a Peeky checkout's sources. Each runs a test command the
# bug's test hint gives, and stops at a line the fix changes (README, "The reach check").
#
#   harness/first_question.sh <peeky checkout> [bug ...]
#
# Prints each command and its output. Round 2's transcripts that stopped (68 of 87) took a
# median of 14 tool calls to their first stop (SETUP_COST.md).
set -u
checkout=$(cd "$1" && pwd)
shift

command_for() {
  case $1 in
    psf__requests-6028)
      # The image's `pytest` is /opt/venv/bin/python3's; --test takes `python -m pytest`.
      echo "peeky probe . --at requests/utils.py:977 --capture scheme,netloc,path --test -- python -m pytest -q tests/test_utils.py -k prepend_scheme" ;;
    jqlang__jq-2650)
      # The image's parser.c is bison's output: the reach check's gold point, found by its text.
      echo "peeky probe . --at src/parser.c:\$(grep -nF 'gen_slice_index((yyvsp[-6].blk), (yyvsp[-4].blk), (yyvsp[-2].blk), INDEX_OPT)' src/parser.c | head -1 | cut -d: -f1) --capture yyn,yylen --test -- ./jq -L tests/modules --run-tests tests/jq.test" ;;
    sharkdp__bat-3108)
      # Its first build is refused: git-version's `git describe` refreshes .git/index while it
      # runs (#486), and --test doesn't build again on its own. The same command again stops.
      echo "peeky probe . --at src/bin/bat/app.rs:101 --capture style_components --test -- cargo run --bin bat -- --decorations=always --plain --paging=never README.md" ;;
  esac
}

bugs=("$@")
[ ${#bugs[@]} -eq 0 ] && bugs=(psf__requests-6028 jqlang__jq-2650 sharkdp__bat-3108)
for bug in "${bugs[@]}"; do
  command=$(command_for "$bug")
  [ -z "$command" ] && { echo "no command for $bug" >&2; continue; }
  echo "=== $bug"
  docker run --rm --network none --cpus=4 --cap-add=SYS_PTRACE --security-opt seccomp=unconfined -v "$checkout/src:/peeky-src:ro" \
    "peeky-study/$bug:peeky-r2" bash -c "
      printf '#!/bin/sh\nPYTHONPATH=/peeky-src exec /opt/peeky/venv/bin/python -c \"import sys; from peeky.cli import main; sys.exit(main())\" \"\$@\"\n' > /usr/local/bin/peeky
      cd /testbed && echo \"\$ $command\" && eval \"$command\""
done
