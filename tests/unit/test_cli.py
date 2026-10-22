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


def test_field_sets_where_no_rule_allows_them_are_refused_before_any_build(tmp_path, capsys):
    # Eval 5 (Rust): `--set s.end=10` was refused only after a build and a run. Since
    # #105 Rust, C and C++ change fields too; Python still refuses before it runs.
    from peeky.cli import main

    (tmp_path / "a.py").write_text("x = 1\n")
    code = main(["probe", str(tmp_path / "a.py"), "--at", "1", "--set", "s.end=10", "--json"])
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 2 and error["code"] == "MUTATION_REJECTED"
    assert "only Java, Rust, C and C++ targets allow" in error["message"]
    assert "COOKBOOK 6.6" in error["message"]
    assert "only the stopped function's own locals change" in error["message"]
    # Review of #109: names are stripped as `--set` itself strips them (`n = 3`).
    from peeky.cli import _member_sets

    for language in ("c", "cpp", "rust", "java"):
        _member_sets(["s.end=10", "xs[2] = 1", "t.__0=3"], language)
    _member_sets(["n = 3", " x=1"], "python")


def test_session_set_help_names_every_language_that_changes_fields(capsys):
    # Review of #105: `session set --help` still said "(Java also current.end=10, ...)".
    import pytest

    from peeky.cli import main

    with pytest.raises(SystemExit):
        main(["session", "set", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "Java also" not in text
    assert "(Java, Rust, C, C++: current.end=10, xs[0]=1, t.__0=3)" in text


def test_describe_lists_a_java_block_locals_lines_and_each_type_its_own():
    # #264: `ranking (int[], lines 51-53, 78-79)`; one name two blocks declare with
    # different types is listed once per type, each with its lines.
    from peeky.cli import _describe_text

    text = _describe_text(
        {
            "language": "java",
            "root": "/app",
            "breakpoint_lines": {},
            "scopes": [
                {
                    "scope": "Runoff.count",
                    "source": "app/Runoff.java",
                    "lines": [43, 79],
                    "names": ["n", "b", "ranking", "x", "i", "y", "z"],
                    "types": {"n": "int", "b": "int", "ranking": "int[]", "x": "String or int"},
                    "spans": {
                        "b": [[49, 53], [76, 79]],
                        "ranking": [[51, 53], [78, 79]],
                        "x": [[60, 60], [70, 71]],
                        "i": [[60, 60]],
                        "y": [],
                        "z": [[80, 81]],
                    },
                    "typed_spans": {
                        "x": [
                            {"type": "int", "spans": [[60, 60]]},
                            {"type": "String", "spans": [[70, 71]]},
                        ],
                        # A one-line block's `long z`, and an `int z` to the method's end.
                        "z": [
                            {"type": "long", "spans": []},
                            {"type": "int", "spans": [[80, 81]]},
                        ],
                    },
                }
            ],
            "mutable_types": "int",
            "commands": {},
            "timing": {"advice": "fast"},
        }
    )
    [line] = [line for line in text.splitlines() if line.startswith("names in Runoff.count")]
    assert line == (
        "names in Runoff.count (app/Runoff.java:43-79): n (int), b (int, lines 49-53, 76-79), "
        "ranking (int[], lines 51-53, 78-79), x (int, line 60), x (String, lines 70-71), "
        "i (line 60), y (no breakpoint line), z (long, no breakpoint line), z (int, lines 80-81)"
    )
