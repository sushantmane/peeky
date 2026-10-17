"""A directory target names its C/C++ program, or holds exactly one."""

import pytest

from peeky import cli

PROGRAM = b"#include <cstdio>\nint main(int argc, char** argv) {\n    return 0;\n}\n"


@pytest.fixture(autouse=True)
def no_toolchain(monkeypatch):
    # Detection only; without a compiler every text match counts as defining main.
    monkeypatch.setattr(cli, "native_tools", lambda language: {"compiler": None})


def test_a_directory_with_one_cpp_program_and_its_checker_is_that_program(tmp_path):
    (tmp_path / "ring.cpp").write_bytes(PROGRAM)
    (tmp_path / "check.py").write_text("print('checker')\n")
    root, program, line, language, data = cli.resolve(tmp_path, "16")
    assert (root, program, line, language) == (tmp_path.resolve(), "ring.cpp", 16, "cpp")
    assert data["sources"] == ("ring.cpp",)
    assert cli.resolve(tmp_path, "ring.cpp:16")[1:4] == ("ring.cpp", 16, "cpp")


def test_a_c_program_with_helpers_is_found_and_built_with_them(tmp_path):
    (tmp_path / "main.c").write_bytes(b"int helper(void);\nint main(void) { return helper(); }\n")
    (tmp_path / "helper.c").write_bytes(b"int helper(void) { return 0; }\n")
    root, program, line, language, data = cli.resolve(tmp_path, "2")
    assert (program, language, data["sources"]) == ("main.c", "c", ("helper.c", "main.c"))


def test_several_programs_or_a_missing_one_are_named_never_guessed(tmp_path):
    (tmp_path / "a.c").write_bytes(b"int main(void) { return 0; }\n")
    (tmp_path / "b.c").write_bytes(b"int main(void) { return 1; }\n")
    with pytest.raises(
        ValueError, match=r"programs define main here \(a.c, b.c\); name one with --at"
    ):
        cli.resolve(tmp_path, "1")
    assert cli.resolve(tmp_path, "b.c:1")[1] == "b.c"
    with pytest.raises(ValueError, match="No C/C\\+\\+ file missing.c"):
        cli.resolve(tmp_path, "missing.c:1")
    (tmp_path.parent / "outside.c").write_bytes(b"int main(void) { return 0; }\n")
    with pytest.raises(ValueError, match=f"target: peeky probe {tmp_path.parent.resolve()} --at"):
        cli.resolve(tmp_path, "../outside.c:1")


def test_a_python_directory_still_needs_its_module(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    with pytest.raises(ValueError, match="Python directory target"):
        cli.resolve(tmp_path, "1")
    assert cli.resolve(tmp_path, "app.py:1")[3] == "python"


def test_java_targets_without_a_config_say_what_the_config_needs(tmp_path):
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "Restock.java").write_text("class Restock {}\n")
    (tmp_path / "check.py").write_text("print('checker')\n")
    for at in ("shop/Restock.java:20", "20"):
        with pytest.raises(ValueError, match=r"Java targets need --config <file.json>.*main_class"):
            cli.resolve(tmp_path, at)
    with pytest.raises(ValueError, match="Java targets need --config"):
        cli.resolve(tmp_path / "shop" / "Restock.java", "20")


def test_state_lines_name_the_stopped_function():
    state = {
        "state": "stopped",
        "location": "/w/src/main.rs:16",
        "function": "topk::insert_sorted::_$u7b$$u7b$closure$u7d$$u7d$::h0123456789abcdef",
    }
    text = cli._state_text(state)
    assert text.startswith(
        "state: stopped at /w/src/main.rs:16 in topk::insert_sorted::{{closure}}"
    )
    assert " in " not in cli._state_text({"state": "stopped", "function": "f"})


def test_a_program_in_a_subdirectory_or_behind_a_link_is_not_taken_from_elsewhere(tmp_path):
    project = tmp_path / "proj"
    (project / "ring").mkdir(parents=True)
    (project / "ring" / "ring.cpp").write_bytes(PROGRAM)
    with pytest.raises(
        ValueError,
        match=r"Pass the program's own directory as the target: "
        r"peeky probe .*proj/ring --at ring.cpp:<line>",
    ):
        cli.resolve(project, "ring/ring.cpp:3")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "real.cpp").write_bytes(PROGRAM)
    (project / "linked.cpp").symlink_to(tmp_path / "outside" / "real.cpp")
    with pytest.raises(ValueError, match="Python directory target|Cannot detect the target"):
        cli.resolve(project, "3")
