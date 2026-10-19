"""CLI text and argument checks found by agent evaluations."""

import json


def test_inspect_marks_names_no_selector_can_read():
    # Eval 5 (C): `(Return Value)` after `step --out` was listed like any local.
    from peeky.cli import _variables_text

    snapshot = {
        "frames": [{"source": {"path": "a.c"}, "line": 3}],
        "scopes": [
            {
                "name": "Locals",
                "variables": [
                    {"name": "(Return Value)", "type": "int", "value": "7"},
                    {"name": "total", "type": "int", "value": "1"},
                ],
            }
        ],
    }
    text = _variables_text(snapshot)
    assert "(Return Value): int = 7 [not selectable]" in text
    assert "total: int = 1\n" in text + "\n" and "total: int = 1 [not" not in text


def test_field_sets_outside_java_are_refused_before_any_build(tmp_path, capsys):
    # Eval 5 (Rust): `--set s.end=10` was refused only after a build and a run.
    from peeky.cli import main

    (tmp_path / "a.c").write_text("int main(void) { return 0; }\n")
    code = main(["probe", str(tmp_path / "a.c"), "--at", "1", "--set", "s.end=10", "--json"])
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 2 and error["code"] == "MUTATION_REJECTED"
    assert "only Java targets allow" in error["message"] and "COOKBOOK 6.6" in error["message"]
    assert main(["describe", str(tmp_path / "a.c"), "--json", "--quiet"]) in (0, 2)
    capsys.readouterr()
    # Review of #109: names are stripped as `--set` itself strips them (`n = 3`).
    from peeky.cli import _member_sets

    _member_sets(["n = 3", " x=1"], "c")
