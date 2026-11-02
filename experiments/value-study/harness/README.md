# The study's harness

Everything here runs on the host (macOS or Linux) with Python 3.9 or later and the standard
library only, Docker, and a signed-in Copilot CLI. The protocol is in [../README.md](../README.md).

| File | What it does |
| --- | --- |
| `build_peeky_images.sh [ids]` | Builds `peeky-study/peeky-dist:<sha>` from a Peeky checkout ([../docker/peeky.Dockerfile](../docker/peeky.Dockerfile)), then each bug's `:peeky` image from its `:control` image. |
| `verify_peeky_image.sh <image>` | Probes a tiny Python script, C program, Rust crate and Java class with Peeky, with no network. |
| `run_one.py <bug> <arm> <rep>` | One attempt: a fresh container, the agent, grading in the same container, `result.json`. |
| `schedule.py` | All attempts, shuffled with seed 441, three at a time, resumable. |
| `analyze.py` | `results/summary.json` and the report's tables. |
| `grade.py <bug> [--gold]` | Grades a bug's image as `run_one.py` does, for validating a bug. |
| `judge_input.py <run>` | The text a judge reads; `judge_prompt.md` is the judge's instructions. |
| `prompt.md`, `prompt-peeky.md` | The prompt; the Peeky arm appends the second. |
| `studylib.py`, `metrics.py` | Shared code: bug files, Docker, grading; the CLI's events and usage. |

## Later rounds

A later round reuses this code with a study config, a JSON file that `$PEEKY_STUDY_CONFIG` names
([round 2's](../../value-study-2/study.json)). It sets the bugs and results directories, the
seed, the arms with their images and prompts, and the comparisons to analyse. Without one,
everything runs as this round ran: run on this round's results, `analyze.py` gives the same
report and `summary.json`. With a config, the summary keeps its values unrounded, so each table
rounds a value once. `build_peeky_images.sh` takes its Dockerfile, image names and build
context from `STUDY_*` variables (its header lists them).

## Running

```sh
PEEKY_CHECKOUT=<peeky checkout> harness/build_peeky_images.sh   # after the bugs' images
harness/schedule.py --dry-run          # what would run, and which bugs are left out and why
harness/schedule.py --preflight        # the attempts (one tiny model call first)
harness/analyze.py > report-tables.md
```

`--fake-agent gold|noop` (both `run_one.py` and `schedule.py`) replaces the agent: `gold`
applies the upstream fix, `noop` changes nothing. Together they check every bug's grading in both
arms without a model call: gold must be solved, noop must not.

## How an attempt runs

1. `docker run -d --network none --cap-add SYS_PTRACE --security-opt seccomp=unconfined
   --memory 6g --cpus 4 --label peeky-study=1` from the arm's image, named
   `study-<bug>-<arm>-<rep>-<random>`, running `sleep infinity`.
2. The agent runs on the host: `copilot -p <prompt> --available-tools bash read_bash write_bash
   stop_bash list_bash --allow-all-tools --allow-all-paths --bash-env on --no-custom-instructions
   --disable-builtin-mcps --no-ask-user --no-auto-update --output-format json
   --usage-output-file ... --max-ai-credits <budget> --share ...`, killed (its process group)
   at the wall-time budget.
   - `BASH_ENV` points at a script that hands every shell command to `docker exec -i -e
     STUDY_INSIDE=1 -w /testbed <container> bash -c "$BASH_EXECUTION_STRING"`. No host variable
     reaches the container.
   - The agent's working directory, `COPILOT_HOME` and `TMPDIR` are fresh, empty directories
     under `--agent-root` (default `~/.cache/value-study-agent/<random>/`). The CLI shows the
     first two paths to the model, so they hold neither the arm nor the word Peeky. Afterwards
     the session's events and the CLI's logs are copied into the run directory and the
     directory is removed.
   - The container mounts that `TMPDIR` read-only at the same path. The CLI saves a command's
     output over 20 KB there and tells the model to read the file with `head`, `grep` and the
     like; those commands run in the container, which otherwise couldn't see the file.
   - The environment is built from scratch: `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_*`, Docker
     and proxy variables if set, the first of `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`,
     `GITHUB_TOKEN`, a `PATH` of the CLI's and Docker's directories plus the system's, and
     `BASH_ENV`, `COPILOT_HOME`, `TMPDIR`, `COPILOT_AUTO_UPDATE=false`, `NO_COLOR=1`. Nothing of
     a parent Copilot session (its `COPILOT_*` and app variables) is passed on.
3. Every process left in the container is killed, `git diff <base_commit>` of `/testbed` is saved
   as `agent.patch` (new text files included, the hidden test files left out), and the attempt is
   graded in the same container: `/grade` gets `test.patch`, `eval.sh` and `parse.py`;
   `eval.sh` runs with `grade.json`'s timeout; its marked output is parsed in the container. A
   test passes when its status is `PASSED` or `XFAIL`; solved means every FAIL_TO_PASS test passes
   and no PASS_TO_PASS test doesn't.
4. The container is removed, also on an exception or a signal.

## The run directory

`results/runs/<bug>/<arm>-<rep>/`: `result.json`, `prompt.txt`, `command.json`, `bash_env.sh`,
`events.jsonl.gz` (the CLI's output), `session-events.jsonl.gz` (the session's own events, with
the system prompt and per-call usage), `usage.json`, `transcript.md`, `stderr.txt`,
`copilot-logs/`, `agent.patch`, `eval.log`. A rerun moves an earlier directory to
`<arm>-<rep>.prev<N>`; `analyze.py` reads only `<arm>-<rep>`.

`result.json` holds, among others: `solved`, `f2p`, `p2p_failed`, `wall_seconds`, `timed_out`,
`exit_code`, `ai_credits` (`totalNanoAiu / 1e9`), `tokens` (`input` counts cached tokens too),
`model_calls`, `tool_calls`, `shell_commands` (with exit codes), `build_or_test_runs`,
`peeky_commands`, `peeky_subcommands`, `peeky_withheld_markers`, `final_message`,
`assistant_messages`, `assistant_reasoning`, `image_digest`, `peeky_sha`, `copilot_version`,
`models`, `isolation` (the tools offered, MCP servers, system prompt segments: `ok` is false if
anything beyond the shell tools loaded), `contaminated` (a control attempt that ran Peeky) and
`infra_failure` with `errors`.

An infrastructure failure is a Docker or harness error, no model call at all, or the CLI ending
with an error other than at the credit budget; `schedule.py` retries those at most twice. A
signal leaves `result.interrupted.json`, which `schedule.py` reruns.

Counting: `build_or_test_runs` counts commands that run the language's build or test programs
(`pytest`, `python -m unittest`, `tox`, `runtests`, `bin/test`; `mvn`, `gradle`, `ant`; `cargo
test|build|nextest`; `make`, `cmake`, `ctest`, `ninja`, `meson`) or a program the bug's
`test_hint` names; here-document bodies don't count. `peeky_commands` are commands that run
`peeky` (or `peeky-probe`, `peeky-worker`) as a program; reading `/opt/peeky/docs` is counted
apart as `peeky_doc_reads`.
