"""The value study's count of program values Peeky printed (experiments/value-study/harness/
metrics.py, `peeky_values`): Peeky's own output only, wherever the shell call ran Peeky."""

import importlib.util
import shlex
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[2] / "experiments" / "value-study" / "harness"
if not (HARNESS / "metrics.py").is_file():
    pytest.skip("the value study isn't in this tree", allow_module_level=True)
_spec = importlib.util.spec_from_file_location("value_study_metrics", HARNESS / "metrics.py")
metrics = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(metrics)

ROWS = (
    "peeky: hit 1/1 at src/bin/bat/app.rs:107; capturing\n"
    "hit  phase     line  function               selector     type  value\n"
    "1    observed  107   bat::app::App::config  extra_plain  bool  false\n"
    "1    observed  107   bat::app::App::config  self.interactive_output  bool  true\n"
)
SESSION = (
    "original: user * = 0x0000000000000000\n"
    "  original.flags: uint32_t = <no readable value: error: parent is NULL>\n"
    "c.id: uint64_t = 82  [unproven]\n"
)


def test_a_quoted_value_holding_a_semicolon_doesnt_hide_the_peeky_command_after_it():
    # The pager's `;` is inside quotes: the shell runs `peeky probe` with BAT_PAGER set.
    command = (
        "cd /testbed && BAT_CONFIG_PATH=/dev/null "
        "BAT_PAGER=\"sh -c 'echo PAGER_INVOKED; cat >/dev/null'\" "
        "peeky probe . --config /opt/peeky/study/rust.json --at src/bin/bat/app.rs:107 "
        "--capture extra_plain,self.interactive_output -- --paging=never --plain t.txt"
    )
    assert [sub for sub, _ in metrics.peeky_runs(command)] == ["probe"]
    assert metrics.peeky_values(ROWS, command) == 2


def test_peeky_run_from_a_python_here_document_counts():
    command = (
        "cd /testbed && python3 - <<'PY'\n"
        "import subprocess\n"
        "r = subprocess.run(['peeky', 'job', 'wait', '773f75a6188defeb', '--timeout', '30'],"
        " capture_output=True, text=True, timeout=40)\n"
        "print(r.stdout)\n"
        "PY"
    )
    assert [sub for sub, _ in metrics.peeky_runs(command)] == ["job"]
    assert metrics.peeky_values(ROWS, command) == 2


def test_a_python_scripts_session_lines_count_where_it_runs_peeky_session():
    script = (
        "import subprocess\n"
        "def peek(*args):\n"
        "    cmd = ['peeky', 'session', *args, '--name', 'acl']\n"
        "    print(subprocess.run(cmd, capture_output=True, text=True).stdout)\n"
        "peek('get', 'original', 'c.id')\n"
    )
    assert metrics.peeky_values(SESSION, "python3 -u - <<'PY'\n" + script + "PY") == 3
    assert metrics.peeky_values(SESSION, "python3 -c " + shlex.quote(script)) == 3
    # A command line given to a shell, from Python or from `sh -c`.
    assert (
        metrics.peeky_values(SESSION, "python3 -c \"import os; os.system('peeky session get x')\"")
        == 3
    )
    assert metrics.peeky_values(SESSION, "bash -lc 'cd /testbed && peeky session get x'") == 3


def test_a_script_that_only_names_peeky_or_is_only_written_runs_nothing():
    assert metrics.peeky_runs("echo ';' peeky probe x; grep -n 'peeky probe' notes.md") == []
    assert (
        metrics.peeky_runs("cat > run.py <<'PY'\nimport os\nos.system('peeky probe x')\nPY") == []
    )
    assert metrics.peeky_values(ROWS, "python3 - <<'PY'\nprint('peeky probe')\nPY") == 0


def test_help_doc_lines_and_other_programs_lines_dont_count():
    # A call that only asked for --help printed no values: the numbered line is grep's.
    code = "498:            c->user = DefaultUser;\n"
    assert metrics.peeky_values(code, "peeky session start --help && grep -n user src/acl.c") == 0
    # A session's form counts only where `peeky session` ran, and never a numbered file line.
    assert (
        metrics.peeky_values("Note: Google Test filter = a.b\n", "peeky probe . --at a.cc:1") == 0
    )
    assert metrics.peeky_values(code + SESSION, "peeky session get c; grep -n user acl.c") == 3
    # A doc's example output, as printed and as grep -n numbers it.
    example = "1    observed   26    util::std_only  x         u32   5  [settable]"
    metrics.DOCS["sha", "lang/rust-cookbook.md"] = {example, '"value": 5,'}
    command = "sed -n 1,9p /opt/peeky/docs/lang/rust-cookbook.md; peeky probe . --at a.rs:1"
    assert metrics.peeky_values(example + "\n" + ROWS, command, "sha") == 2
    numbered = "grep -n value /opt/peeky/docs/lang/rust-cookbook.md; peeky probe . --json"
    assert metrics.peeky_values('40:    "value": 5,\n', numbered, "sha") == 0
    # JSON's "value" fields count where Peeky printed JSON.
    json = '      "value": "Zsh",\n'
    assert metrics.peeky_values(json, "peeky probe . --at a.rs:1") == 0
    assert metrics.peeky_values(json, "peeky queries run --param run=1 --json | head -c 9") == 1
