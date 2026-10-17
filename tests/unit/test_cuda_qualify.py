"""The CUDA qualification script's own logic; the GPU parts need a real GPU host."""

import importlib.util
import json
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
