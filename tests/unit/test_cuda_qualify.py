"""The CUDA qualification script's own logic; the GPU parts need a real GPU host."""

import importlib.util
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("qualify", ROOT / "scripts/cuda/qualify.py")
qualify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualify)


def test_reports_stay_bounded():
    value = qualify.bounded({"text": "x" * 5000, "items": list(range(500))})
    assert value["text"].endswith("... (5000 chars)") and len(value["items"]) == 200


def test_sanitizer_xml_is_summarized_without_assuming_its_schema():
    summary = qualify.sanitizer_summary(
        "<ComputeSanitizerOutput><Record><Kind>InvalidWrite</Kind></Record>"
        "<Record><Kind>InvalidWrite</Kind></Record></ComputeSanitizerOutput>"
    )
    assert summary["root"] == "ComputeSanitizerOutput"
    assert summary["element_counts"]["Record"] == 2
    assert "InvalidWrite" in summary["first_child"]
    assert "parse_error" in qualify.sanitizer_summary("not xml")


def test_fixtures_mark_their_breakpoint_line():
    assert qualify.marker_line(ROOT / "examples/cuda/scale_bug.cu", "// BREAK") == 9


def test_missing_tools_are_reported_never_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(qualify.shutil, "which", lambda name: None)
    out = tmp_path / "report.json"
    assert qualify.main(["--out", str(out)]) == 2
    report = json.loads(out.read_text())
    assert report["missing_tools"] == ["compute-sanitizer", "cuda-gdb", "nvcc"]
    assert "checks" not in report


def test_printed_reads_gdb_value_replies():
    assert qualify.printed("$12 = 767\n") == "767"
    assert qualify.printed('No symbol "i" in current context.') is None
    assert qualify.printed(None) is None


def fake_tool(directory, name, script):
    path = directory / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)
    return str(path)


def fake_toolchain(directory):
    """nvcc and compute-sanitizer that behave like the fixtures; a cuda-gdb whose DAP dies."""
    nvcc = fake_tool(
        directory,
        "nvcc",
        'out=""\nwhile [ $# -gt 0 ]; do [ "$1" = "-o" ] && { out=$2; shift; }; shift; done\n'
        '[ -z "$out" ] && { echo fake nvcc; exit 0; }\n'
        'case "$out" in scale) msg=SUM:1536;; *) msg=DONE:ok;; esac\n'
        'printf \'#!/bin/sh\\necho %s\\n\' "$msg" > "$out"; chmod +x "$out"\n',
    )
    sanitizer = fake_tool(
        directory,
        "compute-sanitizer",
        'save=""\nwhile [ $# -gt 0 ]; do [ "$1" = "--save" ] && { save=$2; shift; }; shift; done\n'
        '[ -z "$save" ] && { echo fake sanitizer; exit 0; }\n'
        "echo '<ComputeSanitizerOutput><Record/></ComputeSanitizerOutput>' > \"$save\"\n"
        "exit 86\n",
    )
    debugger = fake_tool(
        directory, "cuda-gdb", 'case " $* " in *" -batch "*) echo 14.2; exit 0;; esac\nexit 3\n'
    )
    return ["--nvcc", nvcc, "--compute-sanitizer", sanitizer, "--cuda-gdb", debugger]


def test_a_broken_debugger_fails_the_report_even_when_everything_else_passes(tmp_path):
    out = tmp_path / "report.json"
    assert qualify.main(["--out", str(out), "--timeout", "5", *fake_toolchain(tmp_path)]) == 1
    report = json.loads(out.read_text())
    checks = report["checks"]
    assert set(qualify.DAP_CHECKS) <= set(checks) and report["passed"] is False
    others = {name for name in checks if name not in qualify.DAP_CHECKS}
    assert others == {
        "cuda_gdb_python",
        "build_scale",
        "build_oob",
        "baseline_shows_the_bug",
        "memcheck_flagged_errors",
        "memcheck_xml_written",
    }
    assert all(checks[name] for name in others)
    assert checks["debugger_started"] and not checks["dap_initialize"]
    assert "error" in report["dap_transcript"][-1]


def test_a_debugger_that_cannot_start_is_recorded_not_raised(tmp_path):
    shutil.copyfile(ROOT / "examples/cuda/scale_bug.cu", tmp_path / "scale_bug.cu")
    missing = {"cuda-gdb": str(tmp_path / "no-such-debugger")}
    checks, transcript, output = qualify.debug_scale(missing, tmp_path, 2)
    assert checks == dict.fromkeys(qualify.DAP_CHECKS, False) and output == ""
    assert "no-such-debugger" in transcript[-1]["error"]


def test_explicit_tool_paths_must_be_executable(tmp_path):
    out = tmp_path / "report.json"
    args = fake_toolchain(tmp_path)
    args[-1] = str(tmp_path / "not-there" / "cuda-gdb")
    assert qualify.main(["--out", str(out), *args]) == 2
    report = json.loads(out.read_text())
    assert report["missing_tools"] == ["cuda-gdb"] and report["tools"]["cuda-gdb"] is None
