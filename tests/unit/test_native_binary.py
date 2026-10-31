"""native_binary: a binary the project built (ADR 0008, phase 1). Each unit is judged by
what its own debug information records; a stale file refuses the binary by name; and a
frame in a unit Peeky doesn't read shows nothing, with why."""

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from peeky import cli
from peeky.errors import Refused, classify
from peeky.native import Withholding
from peeky.native_binary import (
    JUDGED,
    NOT_JUDGED,
    OPTIMIZED,
    BinaryChanged,
    BinaryManifest,
    BinaryRefused,
    BinaryRun,
    BinarySession,
    BinaryTarget,
    Judged,
    _executable,
    _none_read,
    _unread_file,
    check_files,
    judge,
)
from peeky.native_units import LineFile, LineHeader, Unit, code_ranges

CLANG = "Ubuntu clang version 18.1.3 (1ubuntu1)"
MAIN = "int main(void) { return 0; }\n"
HEADER = "#define A 1\n"


def _md5(text):
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


@pytest.fixture
def root(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.c").write_text(MAIN)
    (tmp_path / "src" / "a.h").write_text(HEADER)
    return tmp_path.resolve()


def _unit(root, flags="-O0 -g -gdwarf-5 -grecord-command-line -c src/main.c", **given):
    fields = {
        "offset": 0,
        "die": 0x0C,
        "version": 5,
        "kind": "Compile",
        "tag": "DW_TAG_compile_unit",
        "name": "src/main.c",
        "comp_dir": str(root),
        "producer": f"{CLANG} /usr/lib/llvm-18/bin/clang {flags}",
        "apple_flags": None,
        "language": "DW_LANG_C11",
        "stmt_list": 0,
        "dwo_name": None,
        "ranges": ((0x1000, 0x1100),),
    }
    fields.update(given)
    return Unit(**fields)


def _tables(root, main=MAIN, header=HEADER, checksums=True, file0="src/main.c"):
    files = {
        0: LineFile(0, file0, _md5(main) if checksums else None),
        1: LineFile(1, "a.h", _md5(header) if checksums else None),
    }
    return {0: LineHeader(0, 5, {0: str(root), 1: "src"}, files)}


def test_a_unit_clang_built_at_o0_with_an_md5_of_every_file_is_judged(root):
    judged = judge(_unit(root), _tables(root), CLANG, root)
    assert judged.status == JUDGED and judged.read and judged.why is None
    assert judged.name == "src/main.c" and judged.language == "c" and judged.level == "0"
    assert judged.files == (
        (str(root / "src/main.c"), _md5(MAIN)),
        (str(root / "src/a.h"), _md5(HEADER)),
    )
    assert check_files([judged], root) == {
        str(root / "src/main.c"): hashlib.sha256(MAIN.encode()).hexdigest(),
        str(root / "src/a.h"): hashlib.sha256(HEADER.encode()).hexdigest(),
    }


@pytest.mark.parametrize(
    "change, why",
    [
        (
            {"producer": "GNU C17 13.3.0 -mlittle-endian -g -gdwarf-5"},
            "built by GCC, which writes no checksum of its files: rebuild with clang (CC=clang",
        ),
        ({"producer": "rustc version 1.96.0"}, "built by rustc version 1.96.0: rebuild with clang"),
        ({"version": 4}, "its debug information is DWARF 4, which records no checksum of its"),
        ({"producer": CLANG}, "records no command line: rebuild with -grecord-command-line"),
        (
            {"producer": "Ubuntu clang version 17.0.6 /usr/bin/clang -O0 -c src/main.c"},
            "not by Ubuntu clang version 18.1.3 (1ubuntu1): set the config's compiler to the "
            "clang that built it",
        ),
        ({"language": "DW_LANG_Mips_Assembler"}, "its language is Mips_Assembler: Peeky reads C"),
        ({"comp_dir": "rel"}, "its file has no absolute path (-fdebug-compilation-dir)"),
        ({"comp_dir": "/elsewhere"}, "its file is outside the target directory"),
        (
            {"producer": f"{CLANG} /usr/bin/clang -O2 -c src/main.c"},
            "built with -O2 (its recorded flags): rebuild it with -O0 and without -flto, or set "
            '"optimized": true',
        ),
        (
            {"producer": f"{CLANG} /usr/bin/clang -O0 -flto=thin -c src/main.c"},
            "built with -flto (its recorded flags)",
        ),
        (
            {"producer": f"{CLANG} /usr/bin/clang -O0 -ffast-math -c src/main.c"},
            "built with -ffast-math, under which Peeky's proofs aren't checked",
        ),
        (
            {"producer": f"{CLANG} /usr/bin/clang -O0 -fsanitize=address -c src/main.c"},
            "built with -fsanitize=address, which instruments the code, under which",
        ),
        *(
            (
                {"producer": f"{CLANG} /usr/bin/clang -O0 {option} -c src/main.c"},
                f"built with {option}{said}, under which Peeky's proofs aren't checked",
            )
            for option, said in (
                ("-ffp-model=fast", ", which implies -ffast-math's options"),
                ("-ffp-model=aggressive", ", which implies -ffast-math"),
                ("-fapprox-func", ""),
                ("-fno-signed-zeros", ""),
                ("-fcx-limited-range", ""),
                ("-ffp-contract=fast-honor-pragmas", ""),
                ("-mrecip", ""),
                ("-mrecip=all", ", which estimates reciprocals"),
                ("-mrecip=!div,sqrt:1", ", which estimates reciprocals"),
                ("-fdenormal-fp-math=preserve-sign", ", which flushes denormals"),
                ("-fdenormal-fp-math-f32=positive-zero", ", which flushes denormals"),
            )
        ),
        (
            {"producer": f"{CLANG} /usr/bin/clang -Ofast -c src/main.c"},
            "built with -Ofast, which implies -ffast-math",
        ),
        (
            {"producer": f"{CLANG} /usr/bin/clang -O2 -mllvm -x -O0 -c src/main.c"},
            "records -mllvm -x, which Peeky can't judge",
        ),
        ({"stmt_list": 9}, "it has no line table"),
    ],
)
def test_a_unit_peeky_cant_judge_says_why(root, change, why):
    judged = judge(_unit(root, **change), _tables(root), CLANG, root)
    assert judged.status == NOT_JUDGED and not judged.read and not judged.files
    assert why in judged.why, judged.why


def test_an_mrecip_value_peeky_hasnt_classified_says_only_that(root):
    unit = _unit(root, producer=f"{CLANG} /usr/bin/clang -O0 -mrecip=fast-ish -c src/main.c")
    assert judge(unit, _tables(root), CLANG, root).why == (
        "built with -mrecip=fast-ish, whose value Peeky hasn't classified (its recorded flags): "
        "rebuild without it"
    )


def test_floating_point_options_that_keep_ieee_results_are_judged(root):
    for option in (
        "-ffp-model=precise",
        "-ffp-model=strict",
        "-fno-trapping-math",
        "-ffp-contract=on",
        "-fdenormal-fp-math=ieee",
        "-fno-fast-math",
        "-mrecip=none",
        "-mrecip=!div,!vec-sqrtf:2",
    ):
        unit = _unit(root, producer=f"{CLANG} /usr/bin/clang -O0 {option} -c src/main.c")
        assert judge(unit, _tables(root), CLANG, root).status == JUDGED, option


def test_an_optimized_unit_is_read_as_one_only_when_opted_in(root):
    unit = _unit(root, producer=f"{CLANG} /usr/bin/clang -O2 -c src/main.c")
    assert judge(unit, _tables(root), CLANG, root).status == NOT_JUDGED
    judged = judge(unit, _tables(root), CLANG, root, optimized=True)
    assert judged.status == OPTIMIZED and judged.read and judged.level == "2"
    assert judged.why == "built with -O2 (its recorded flags)" and judged.files
    # What changes what the code computes is never read, opted in or not.
    fast = _unit(root, producer=f"{CLANG} /usr/bin/clang -O2 -ffast-math -c src/main.c")
    assert judge(fast, _tables(root), CLANG, root, optimized=True).status == NOT_JUDGED


def test_a_table_without_md5s_says_what_it_shows_and_the_likely_remedies(root):
    judged = judge(_unit(root), _tables(root, checksums=False), CLANG, root)
    assert judged.why == (
        "its line table records no MD5s: the likely cause is a #line naming another file, as "
        "bison and flex write (bison -l, flex -L)"
    )
    tables = _tables(root, checksums=False)
    tables[0].files[2] = LineFile(0, "<stdin>", None)
    unit = _unit(root, language="DW_LANG_C_plus_plus_14")
    assert judge(unit, tables, CLANG, root).why == (
        "its line table records no MD5s, and names <stdin>, which clang adds for an inheriting "
        "constructor: the likely cause is a #line naming another file, as bison and flex write "
        "(bison -l, flex -L), or libc++'s system-header pragma "
        "(-D_LIBCPP_HAS_NO_PRAGMA_SYSTEM_HEADER)"
    )


def test_a_table_naming_another_file_0_or_no_absolute_path_isnt_judged(root):
    judged = judge(_unit(root), _tables(root, file0="src/other.c"), CLANG, root)
    assert judged.why == (
        f"its line table's file 0 is {root}/src/other.c, but the unit names {root}/src/main.c"
    )
    tables = _tables(root)
    tables[0].directories[1] = "src"
    tables[0].files[1] = LineFile(1, "a.h", _md5(HEADER))
    tables[0].directories[0] = str(root)
    relative = {0: LineHeader(0, 5, {0: "rel", 1: "src"}, tables[0].files)}
    assert "no absolute path" in judge(_unit(root), relative, CLANG, root).why


def test_a_stale_or_missing_file_refuses_the_binary_by_name(root):
    judged = judge(_unit(root), _tables(root), CLANG, root)
    (root / "src" / "a.h").write_text("#define A 2\n")
    with pytest.raises(BinaryChanged, match="the binary is stale: src/a.h changed, since it"):
        check_files([judged], root)
    (root / "src" / "a.h").unlink()
    with pytest.raises(BinaryChanged, match="src/a.h is gone or unreadable") as stale:
        check_files([judged], root)
    assert stale.value.before_launch is True
    assert classify(stale.value)["code"] == "PROVENANCE_CHANGED"
    # Two units that read one file as different texts: it changed between their compiles.
    other = Judged(judged.unit, "src/b.c", JUDGED, files=((judged.files[0][0], "0" * 32),))
    with pytest.raises(BinaryChanged, match="two of the binary's units recorded different MD5s"):
        check_files([judged, other], root)


def test_files_outside_the_root_are_checked_too(root, tmp_path_factory):
    system = tmp_path_factory.mktemp("usr").resolve()
    (system / "stdio.h").write_text("int printf(const char *, ...);\n")
    tables = _tables(root)
    tables[0].directories[2] = str(system)
    tables[0].files[2] = LineFile(2, "stdio.h", _md5("int printf(const char *, ...);\n"))
    judged = judge(_unit(root), tables, CLANG, root)
    assert str(system / "stdio.h") in check_files([judged], root)
    (system / "stdio.h").write_text("changed\n")
    with pytest.raises(BinaryChanged, match=f"{system}/stdio.h changed"):
        check_files([judged], root)


def test_what_isnt_an_executable_peeky_can_run_is_refused(tmp_path):
    cases = [
        (b"#!/bin/sh\nexec .libs/jq\n", "jq is a script, not an executable: libtool"),
        (b"\xca\xfe\xba\xbe" + b"\0" * 60, "jq is a universal binary"),
        (b"MZ" + b"\0" * 62, "jq is not an ELF or Mach-O executable"),
    ]
    if sys.platform == "darwin":
        cases.append((b"\x7fELF\x02\x01" + b"\0" * 58, "is an ELF executable, which runs on Linux"))
    else:
        mach = (0xFEEDFACF).to_bytes(4, "little") + b"\0" * 60
        cases.append((mach, "is a Mach-O executable, which runs on macOS"))
        object_file = b"\x7fELF\x02\x01" + b"\0" * 10 + (1).to_bytes(2, "little") + b"\0" * 46
        cases.append((object_file, "is an ELF object or core file"))
    for head, said in cases:
        path = tmp_path / "jq"
        path.write_bytes(head)
        with pytest.raises(BinaryRefused, match=said) as refused:
            _executable(path, "jq")
        assert classify(refused.value)["code"] == "UNSUPPORTED", said
        assert refused.value.before_launch is True


def test_a_unit_s_code_ranges():
    assert code_ranges(
        {"DW_AT_low_pc": "0x0000000000000718)", "DW_AT_high_pc": "0x0000000000000774)"}, {}
    ) == ((0x718, 0x774),)
    # A size past the low address, as DWARF 4's dump may write it.
    assert code_ranges({"DW_AT_low_pc": "0x1000)", "DW_AT_high_pc": "0x40)"}, {}) == (
        (0x1000, 0x1040),
    )
    listed = {
        "DW_AT_low_pc": "0x0000000000000000)",
        "DW_AT_ranges": "indexed (0x0) rangelist = 0x00000010",
    }
    rows = {
        "DW_AT_ranges": [
            "[0x0000000000000758, 0x000000000000078c)",
            "[0x0000000000000000, 0x0000000000000010)",
            "[0xffffffff00000000, 0xffffffff00000010)",
            "[0x000000000000078c, 0x00000000000007a4))",
        ]
    }
    # A linker's discarded code (at 0, or a tombstone) holds none.
    assert code_ranges(listed, rows) == ((0x758, 0x78C), (0x78C, 0x7A4))
    with pytest.raises(ValueError, match="without its end"):
        code_ranges(listed, {"DW_AT_ranges": rows["DW_AT_ranges"][:1]})


def _session(judged, bias=0):
    session = BinarySession.__new__(BinarySession)
    session.judged, session.load_bias, session.load_bias_unknown = judged, bias, False
    session.innermost_id = 1
    return session


def _frame(address, identity=1):
    return {"id": identity, "instructionPointerReference": f"0x{address:x}", "name": "f"}


def test_a_frame_shows_nothing_unless_one_unit_read_holds_its_code(root):
    read = Judged(_unit(root, ranges=((0x1000, 0x1100),)), "src/main.c", JUDGED)
    optimized = Judged(_unit(root, ranges=((0x1100, 0x1200),)), "src/o.c", OPTIMIZED, "built")
    unread = Judged(
        _unit(root, ranges=((0x2000, 0x2100),)),
        "src/vm.c",
        NOT_JUDGED,
        "built with -O3 (its recorded flags)",
    )
    session = _session((read, optimized, unread))
    assert session.frame_withheld(_frame(0x1010)) is None
    assert session.frame_withheld(_frame(0x1110)) is None
    withheld = session.frame_withheld(_frame(0x2010))
    assert isinstance(withheld, Withholding) and withheld.label == "not shown"
    assert withheld == (
        "this frame runs src/vm.c, a unit Peeky doesn't judge: built with -O3 (its recorded flags)"
    )
    assert withheld.refusal.startswith("Nothing in this frame can be changed: this frame runs")
    assert "no unit of the binary's debug information" in session.frame_withheld(_frame(0x9000))
    both = _session((read, Judged(_unit(root), "src/twin.c", JUDGED)))
    assert "more than one unit" in both.frame_withheld(_frame(0x1010))
    unknown = _session((read,), bias=None)
    unknown.load_bias_unknown = True
    assert "Peeky can't tell which unit this frame runs" in unknown.frame_withheld(_frame(0x1010))


def test_a_caller_frame_is_placed_by_the_address_before_its_return(root):
    read = Judged(_unit(root, ranges=((0x1000, 0x1100),)), "src/main.c", JUDGED)
    unread = Judged(_unit(root, ranges=((0x1100, 0x1200),)), "src/vm.c", NOT_JUDGED, "built")
    session = _session((read, unread))
    # Its call is the last instruction of src/main.c's code: the frame runs that unit.
    assert session.frame_withheld(_frame(0x1100, identity=2)) is None
    assert session.frame_withheld(_frame(0x1100, identity=1)) is not None


def test_the_refusals_name_the_units_and_why(root):
    units = [
        Judged(_unit(root), "src/a.c", NOT_JUDGED, "built by GCC, which writes no checksum"),
        Judged(_unit(root), "src/b.c", NOT_JUDGED, "built by GCC, which writes no checksum"),
        Judged(_unit(root), "src/c.c", NOT_JUDGED, "its line table records no MD5s"),
        Judged(
            _unit(root), "/usr/lib/crt1.c", NOT_JUDGED, "its file is outside the target directory"
        ),
    ]
    said = _none_read("jq", units)
    assert said == (
        "Peeky judges no unit of jq: src/a.c and 1 more: built by GCC, which writes no "
        "checksum; src/c.c: its line table records no MD5s"
    )
    assert classify(BinaryRefused(said))["code"] == "UNSUPPORTED"
    assert _none_read("jq", units[3:]).startswith("No unit of jq has its file in the target")
    assert _unread_file("src/c.c", "jq", units, root) == (
        "src/c.c is a unit Peeky doesn't judge: its line table records no MD5s"
    )
    elsewhere = _unread_file("src/parser.y", "jq", units, root)
    assert elsewhere.startswith("No unit of jq Peeky judges has code in src/parser.y")
    for message in (said, elsewhere, _unread_file("src/c.c", "jq", units, root)):
        assert classify(BinaryRefused(message))["code"] == "UNSUPPORTED", message


def test_a_binary_target_s_config(tmp_path):
    with pytest.raises(ValueError, match='"optimized" must be true or false'):
        BinaryTarget(binary="jq", optimized="yes")
    with pytest.raises(ValueError, match="language must be c or cpp"):
        BinaryTarget(binary="jq", language="rust")
    assert BinaryTarget(binary="jq", language="cpp").compiler == "clang++"
    assert BinaryTarget(binary="jq").supports_edits is False
    (tmp_path / "jq").write_bytes(b"\x7fELF")
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(Refused, match="outside the target directory") as outside:
        BinaryTarget(binary="../jq").located(root)
    assert outside.value.code == "INVALID_REQUEST"
    with pytest.raises(Refused, match="No binary missing"):
        BinaryTarget(binary="missing").located(root)
    with pytest.raises(BinaryRefused, match="can.t take a candidate edit") as edit:
        BinaryTarget(binary="jq").prepare(None, tmp_path, edit=object())
    assert classify(edit.value)["code"] == "UNSUPPORTED"


def test_a_binary_run_removes_only_its_own_directory(tmp_path):
    root, scratch = tmp_path / "project", tmp_path / "scratch"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.c").write_text(MAIN)
    scratch.mkdir()
    (scratch / "prog.dSYM").mkdir()
    manifest = BinaryManifest(None, (), (), (), (), ())
    BinaryRun(root, root, scratch, manifest).cleanup()
    assert not scratch.exists() and (root / "src" / "main.c").read_text() == MAIN


def test_a_binary_config_is_checked_before_anything_runs(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    config = tmp_path / "binary.json"
    config.write_text(json.dumps({"binary": "prog", "sources": ["main.c"]}))
    with pytest.raises(Refused, match="not sources: its sources are the files its debug") as found:
        cli.resolve(str(root), "main.c:3", str(config))
    assert found.value.code == "INVALID_REQUEST"
    config.write_text(json.dumps({"binary": "prog", "compile_commands": "cc.json"}))
    with pytest.raises(Refused, match="not compile_commands"):
        cli.resolve(str(root), "main.c:3", str(config))
    config.write_text(json.dumps({"binary": "prog", "optimized": True}))
    _, program, line, language, data = cli.resolve(str(root), "src/a.cpp:3", str(config))
    assert (program, line, language) == ("src/a.cpp", 3, "cpp")
    # The units say the language where the config doesn't.
    assert data == {"binary": "prog", "optimized": True, "language": None}
    assert isinstance(cli.adapter_for(language, data), BinaryTarget)
    (root / "prog").write_bytes(b"")
    with pytest.raises(Refused, match="--binary or a config naming its binary, not both"):
        cli.resolve(str(root), "main.c:3", str(config), binary=str(root / "prog"))
    (tmp_path / "other").write_bytes(b"")
    with pytest.raises(Refused, match="outside the target directory"):
        cli.resolve(str(root), "main.c:3", binary=str(tmp_path / "other"))
    with pytest.raises(Refused, match="outside the target directory"):
        cli.resolve(str(root), "main.c:3", binary="../other")
    with pytest.raises(Refused, match="A worker takes a binary only from its own configuration"):
        cli.resolve(str(root), "main.c:3", binary=str(root / "prog"), tools=False)
    with pytest.raises(Refused, match="the target is the directory"):
        cli.resolve(str(root / "prog"), "main.c:3", binary=str(root / "prog"))
    monkeypatch.setattr(cli, "native_tools", lambda language: {"compiler": "cc", "sysroot": "/s"})
    found = cli.resolve(str(root), "src/main.c:3", binary=str(root / "prog"))
    assert found[3] == "c" and found[4] == {"binary": "prog", "language": None, "compiler": "cc"}
    assert os.path.samefile(found[0], root) and Path(found[0]).is_dir()
    # `--binary ./prog` is the target's, wherever the command runs: never the cwd's.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "prog").write_bytes(b"other")
    monkeypatch.chdir(elsewhere)
    found = cli.resolve(str(root), "src/main.c:3", binary="./prog")
    assert found[4]["binary"] == "prog" and os.path.samefile(found[0], root)
    (root / "prog").unlink()
    with pytest.raises(Refused, match="No binary ./prog in"):
        cli.resolve(str(root), "src/main.c:3", binary="./prog")


def test_a_frame_whose_code_several_functions_share_shows_nothing(root):
    from peeky.native import CodeDie

    read = Judged(_unit(root, ranges=((0x1000, 0x1100),)), "src/main.c", JUDGED)
    session = _session((read,))
    folded = [
        CodeDie(0x30, ((0x1000, 0x1020),), False, 1, 0x30, None, None, {}),
        CodeDie(0x60, ((0x1000, 0x1020),), False, 1, 0x60, None, None, {}),
        CodeDie(0x90, ((0x1040, 0x1060),), False, 1, 0x90, None, None, {}),
        CodeDie(0xA0, ((0x1040, 0x1050),), True, 2, 0x90, 0x90, None, {}),
    ]
    session.code = {"dies": folded}
    withheld = session.frame_withheld(_frame(0x1010))
    assert withheld.label == "not shown" and withheld.startswith(
        "2 functions share the code this frame runs (identical code folding"
    )
    # A function and a call inlined in it share code: that is no folding.
    assert session.frame_withheld(_frame(0x1048)) is None


def _placed(root, lines):
    session = _session(())
    session.prepared = SimpleNamespace(source_root=root)
    session._lines_data = json.dumps(lines)
    return session


def test_a_breakpoint_off_the_judged_units_lines_is_refused_before_launch(root):
    session = _placed(root, {"src/main.c": [3, 4]})
    assert session.unplaceable_line(root / "src/main.c", 3) is None
    why = session.unplaceable_line(root / "src/main.c", 9)
    assert why.startswith("Line 9 of src/main.c is not among the breakpoint lines of the units")
    assert classify(ValueError(why))["code"] == "INVALID_BREAKPOINT"
    assert session.unplaceable_line(root / "src/a.h", 1).startswith("Line 1 of src/a.h")


def test_a_frame_on_a_line_of_a_unit_not_read_is_accepted_but_shows_nothing(root):
    session = _placed(root, {"src/main.c": [3, 4]})
    session.prepared = SimpleNamespace(source_root=root, workspace=root)
    session.validate_stop({"source": {"path": str(root / "src/main.c")}, "line": 3})
    session.validate_stop({"source": {"path": str(root / "src/vm.c")}, "line": 7})
    with pytest.raises(RuntimeError, match="does not match prepared symbols"):
        session.validate_stop({"source": {"path": "/elsewhere/x.c"}, "line": 3})


def test_any_file_or_tool_that_changes_during_the_run_ends_it(root, tmp_path_factory):
    scratch = tmp_path_factory.mktemp("scratch")
    (scratch / "prog.dSYM").mkdir()
    (scratch / "prog.dSYM" / "dwarf").write_text("symbols")
    binary = root / "prog"
    binary.write_text("binary")
    digest = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()  # noqa: E731
    source = root / "src" / "main.c"
    manifest = BinaryManifest(
        SimpleNamespace(path="prog", sha256=digest(binary)),
        (SimpleNamespace(path="prog.dSYM/dwarf", sha256=digest(scratch / "prog.dSYM/dwarf")),),
        (SimpleNamespace(path=str(binary), sha256=digest(binary)),),
        ((str(source), digest(source)),),
        (),
        (),
    )
    session = _session(())
    session.prepared = BinaryRun(root, root, scratch, manifest)
    session.binary = binary
    session.assert_current()
    source.write_text("int main(void) { return 1; }\n")
    with pytest.raises(RuntimeError, match="Provenance: src/main.c changed during the run") as gone:
        session.assert_current()
    assert classify(gone.value)["code"] == "PROVENANCE_CHANGED"


def test_lldb_reads_peeky_s_own_dsym_on_macos(root, monkeypatch):
    import peeky.native_binary as native_binary

    monkeypatch.setattr(native_binary.sys, "platform", "darwin")
    monkeypatch.setattr(
        native_binary.NativeSession, "launch_args", lambda self, *args, **kwargs: {"cwd": "/w"}
    )
    session = _session(())
    session.symbols = Path('/state/a "b"/prog.dSYM/Contents/Resources/DWARF/prog')
    assert session.launch_args(root / "src/main.c", root)["preRunCommands"] == [
        'target symbols add "/state/a \\"b\\"/prog.dSYM"'
    ]


LINE_TABLES = """debug_line[0x00000000]
Line table prologue:
         version: 5
include_directories[  0] = "{root}"
file_names[  0]:
           name: "src/main.c"
      dir_index: 0
   md5_checksum: d78a4c753a502a3a43f8f721a7f12da4

Address            Line   Column File   ISA Discriminator OpIndex Flags
------------------ ------ ------ ------ --- ------------- ------- -------------
0x0000000000001000      1      0      0   0             0       0  is_stmt
0x0000000000001010      1      0      0   0             0       0  is_stmt end_sequence
debug_line[0x00000040]
Line table prologue:
         version: 5
include_directories[  0] = "{root}"
file_names[  0]:
           name: "src/main.c"
      dir_index: 0
   md5_checksum: d78a4c753a502a3a43f8f721a7f12da4

Address            Line   Column File   ISA Discriminator OpIndex Flags
------------------ ------ ------ ------ --- ------------- ------- -------------
0x0000000000002000      1      0      0   0             0       0  is_stmt
0x0000000000002010      1      0      0   0             0       0  is_stmt end_sequence
"""


def test_only_the_line_tables_of_the_units_read_give_rows(root):
    from peeky.native import _line_rows, _LineParser

    text = LINE_TABLES.format(root=root)
    parser = _LineParser(root, ("src/main.c",), only=frozenset({0x40}))
    parser.feed(text.splitlines())
    assert _line_rows(parser.table(), root, ("src/main.c",)) == [(0x2000, "src/main.c", 1)]
    every = _LineParser(root, ("src/main.c",))
    every.feed(text.splitlines())
    assert len(_line_rows(every.table(), root, ("src/main.c",))) == 2
    with pytest.raises(ValueError, match="without its offset"):
        _LineParser(root, ("src/main.c",), only=frozenset()).feed(["debug_line[x]"])


def _macho(entries):
    """A 64-bit Mach-O executable whose symbol table holds ``entries``: (name, type, value)."""
    import struct

    strings = b"\0"
    table = b""
    for name, kind, value in entries:
        table += struct.pack("<IBBHQ", len(strings), kind, 0, 0, value)
        strings += name.encode() + b"\0"
    symbols = 32 + 24
    command = struct.pack(
        "<IIIIII", 0x2, 24, symbols, len(entries), symbols + len(table), len(strings)
    )
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 24, 0, 0)
    return header + command + table + strings


def _archive(members):
    """A BSD `ar` archive of ``members``: (name, time, bytes), long names after the header."""
    out = b"!<arch>\n"
    for name, moment, data in members:
        encoded = name.encode()
        body = encoded + data
        head = f"#1/{len(encoded)}".ljust(16) + str(moment).ljust(12) + "0".ljust(6) * 2
        head += "644".ljust(8) + str(len(body)).ljust(10) + "`\n"
        out += head.encode() + body + (b"\n" if len(body) % 2 else b"")
    return out


def test_the_debug_map_names_each_object_and_when_the_linker_read_it(tmp_path):
    from peeky.native_binary import N_OSO, check_debug_map, debug_map

    obj = tmp_path / "main.o"
    obj.write_bytes(b"object")
    early = 1_700_000_000 * 10**9 + 100_000_000
    os.utime(obj, ns=(early, early))
    library = tmp_path / "libu.a"
    library.write_bytes(_archive([("util.o", 1_600_000_000, b"util"), ("x.o", 5, b"x")]))
    entries = [
        (str(obj), N_OSO, 1_700_000_000),
        ("_main", 0x0F, 0x100000328),
        (f"{library}(util.o)", N_OSO, 1_600_000_000),
    ]
    binary = tmp_path / "prog"
    binary.write_bytes(_macho(entries))
    linked = 1_700_000_000 * 10**9 + 500_000_000
    os.utime(library, ns=(linked - 1, linked - 1))
    os.utime(binary, ns=(linked, linked))
    found = debug_map(binary)
    assert found == [(str(obj), 1_700_000_000), (f"{library}(util.o)", 1_600_000_000)]
    hashes = check_debug_map(found, tmp_path, binary)
    assert set(hashes) == {str(obj), str(library)}
    # Rebuilt within the second the map records, after the link: newer than the binary.
    os.utime(obj, ns=(linked + 1, linked + 1))
    with pytest.raises(
        BinaryChanged, match=r"main\.o is newer than prog: rebuilt after the link\?"
    ):
        check_debug_map(found, tmp_path, binary)
    os.utime(library, ns=(linked + 1, linked + 1))
    with pytest.raises(BinaryChanged, match=r"libu\.a is newer than prog"):
        check_debug_map(found[1:], tmp_path, binary)
    # A binary touched or copied later still has its objects older than it.
    os.utime(binary, None)
    assert set(check_debug_map(found, tmp_path, binary)) == {str(obj), str(library)}
    # Whole seconds, as a filesystem keeping no more gives: in the binary's second, which
    # was written first can't be told. In an earlier second, the object was.
    second = 1_700_000_000 * 10**9
    os.utime(library, ns=(second - 10**9, second - 10**9))
    # A whole-second binary with an object later in its second: the binary's time may be
    # rounded down, so the object isn't proven newer either.
    cases = ((second, second + 500_000_000), (second, second), (second + 100_000_000, second))
    for written, linked in cases:
        os.utime(obj, ns=(written, written))
        os.utime(binary, ns=(linked, linked))
        with pytest.raises(BinaryRefused, match="main.o and prog were written in the same") as same:
            check_debug_map(found, tmp_path, binary)
        assert "relink prog (or touch it) and retry" in str(same.value)
        assert classify(same.value)["code"] == "UNSUPPORTED"
    os.utime(binary, ns=(second + 10**9, second + 10**9))
    assert set(check_debug_map(found, tmp_path, binary)) == {str(obj), str(library)}
    # An archive too.
    os.utime(library, ns=(second + 10**9, second + 10**9))
    with pytest.raises(BinaryRefused, match="libu.a and prog were written in the same second"):
        check_debug_map(found, tmp_path, binary)
    # Rebuilt since the link, gone, or a member not there: the binary is stale, by name.
    for entries, said in (
        ([(str(obj), 1_699_999_999)], "main.o changed since the binary was linked"),
        ([(f"{library}(util.o)", 7)], "libu.a(util.o) changed since the binary was linked"),
        ([(f"{library}(gone.o)", 7)], "libu.a(gone.o) is gone"),
        ([(str(tmp_path / "missing.o"), 7)], "missing.o is gone or unreadable"),
    ):
        with pytest.raises(BinaryChanged, match=re.escape(said)) as stale:
            check_debug_map(entries, tmp_path, binary)
        assert classify(stale.value)["code"] == "PROVENANCE_CHANGED"
    # A time of 0 (ZERO_AR_DATE) can't tell a rebuilt object from the one linked.
    with pytest.raises(BinaryRefused, match="records no time of main.o") as zero:
        check_debug_map([(str(obj), 0)], tmp_path, binary)
    assert classify(zero.value)["code"] == "UNSUPPORTED"
    binary.write_bytes(_macho([])[:40])
    with pytest.raises(ValueError):
        debug_map(binary)


FAKE_CC = """#!/bin/sh
# Writes what a preprocessor would: the text (-o) and its dependencies (-MF).
echo "$@" > argv
out= deps= input=
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -MF) deps="$2"; shift ;;
    -*) ;;
    *) input="$1" ;;
  esac
  shift
done
if [ -f fail ]; then echo "src/main.c:1:10: fatal error: 'gone.h' file not found" >&2; exit 1; fi
if [ -f search ]; then cat search >&2; fi
if [ -f output ]; then cat output > "$out"; else
printf '# 1 "%s"\\nint main(void) { return 0; }\\n' "$input" > "$out"; fi
printf 'x.o: %s %s\\n' "$input" "$(cat reads 2>/dev/null)" > "$deps"
"""


def _replay(root, tmp_path, judged=None, words=None):
    from peeky.native_binary import replay, replay_environment

    compiler = tmp_path / "fake-cc"
    compiler.write_text(FAKE_CC)
    compiler.chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    judged = judged or judge(_unit(root), _tables(root), CLANG, root)
    if words is not None:
        judged = Judged(**{**judged.__dict__, "words": tuple(words)})
    return replay(judged, str(compiler), replay_environment(scratch), scratch, root, 10)


def test_a_unit_s_text_is_its_replayed_preprocessing(root, tmp_path):
    from peeky.native_binary import UnitText

    (root / "reads").write_text("src/a.h")
    text = _replay(root, tmp_path)
    assert isinstance(text, UnitText), text
    assert text.reads == ("src/a.h", "src/main.c") and not text.asm and not text.optimizes
    assert text.preprocessed.text.startswith("int main(void)")
    # Only the passed options, `--no-default-config` first, its own -E, its own input last.
    argv = (root / "argv").read_text().split()
    assert argv[0] == "--no-default-config" and argv[1] == "-O0"
    assert argv[2:6] == ["-E", "-v", "-MD", "-MF"] and argv[-3:] == ["src/main.c", "-o", argv[-1]]


def test_what_a_replay_reads_must_be_what_the_line_table_names(root, tmp_path):
    (root / "src" / "config.h").write_text("#define C 1\n")
    (root / "reads").write_text("src/config.h")
    assert _replay(root, tmp_path) == (
        "Peeky can't read src/main.c as compiled: its preprocessing reads src/config.h, which "
        "its line table doesn't name (-fdebug-macro names each header, on Linux)"
    )
    # Apple's SDK settings, under the recorded sysroot, are no header.
    sdk = tmp_path / "SDK"
    sdk.mkdir()
    (sdk / "SDKSettings.json").write_text("{}")
    (root / "reads").write_text(str(sdk / "SDKSettings.json"))
    words = ["/usr/bin/clang", "-isysroot", str(sdk), "-O0", "-c", "src/main.c"]
    from peeky.native_binary import UnitText

    assert isinstance(_replay(root, tmp_path, words=words), UnitText)


def test_a_replay_that_cant_run_says_why(root, tmp_path):
    (root / "fail").write_text("")
    assert _replay(root, tmp_path) == (
        "Peeky can't read src/main.c as compiled: its preprocessing failed: src/main.c:1:10: "
        "fatal error: 'gone.h' file not found"
    )
    (root / "fail").unlink()
    words = ["/usr/bin/clang", "-fplugin=x.so", "-c", "src/main.c"]
    assert _replay(root, tmp_path, words=words) == (
        "Peeky can't read src/main.c as compiled: its command uses -fplugin=x.so, which Peeky "
        "doesn't pass to its compiler"
    )
    gone = judge(_unit(root), _tables(root), CLANG, root)
    gone = Judged(**{**gone.__dict__, "unit": _unit(root, comp_dir=str(root / "nowhere"))})
    assert "its compilation directory" in _replay(root, tmp_path, judged=gone)


def test_the_replay_environment_is_peeky_s_own(tmp_path, monkeypatch):
    from peeky.native_binary import replay_environment

    for name in ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "OBJC_INCLUDE_PATH", "SDKROOT"):
        monkeypatch.setenv(name, "/evil")
    monkeypatch.setenv("CCC_OVERRIDE_OPTIONS", "+-O2")
    monkeypatch.setenv("CL", "/O2")
    monkeypatch.setenv("_CL_", "/O2")
    env = replay_environment(tmp_path)
    assert set(env) <= {"PATH", "LANG", "HOME", "TMPDIR", "LC_ALL"}, env
    assert env["HOME"] == env["TMPDIR"] == str(tmp_path)


def test_any_call_may_return_twice_where_a_unit_isnt_read_or_holds_asm(root):
    from peeky.native_binary import UnitText, any_call_returns_twice

    def text(asm=None):
        return UnitText(None, None, False, asm, (), False)

    read = Judged(_unit(root), "src/main.c", JUDGED)
    other = Judged(_unit(root), "src/b.c", JUDGED)
    unread = Judged(_unit(root), "py/nlraarch64.c", NOT_JUDGED, "built with -Os")
    assert any_call_returns_twice([read, other], {"src/main.c": text(), "src/b.c": text()}) is None
    assert any_call_returns_twice([read, unread], {"src/main.c": text()}) == (
        "py/nlraarch64.c is a unit Peeky doesn't read"
    )
    held = "src/main.c's text holds asm (src/main.c:3)"
    assert any_call_returns_twice([read, other], {"src/main.c": text(held), "src/b.c": "why"}) == (
        f"{held}, and 1 more"
    )


def test_a_config_s_returns_twice_is_a_list_of_names():
    assert BinaryTarget(binary="mp", returns_twice=("nlr_push",)).target_identity[
        "returns_twice"
    ] == ["nlr_push"]
    for bad in (("nlr push",), ["nlr_push"], tuple(f"f{n}" for n in range(101))):
        with pytest.raises(ValueError, match="returns_twice"):
            BinaryTarget(binary="mp", returns_twice=bad)


def test_the_setjmp_note_says_any_call_may_return_twice_and_why(root):
    session = _session(())
    session.any_call = "py/nlraarch64.c is a unit Peeky doesn't read"
    assert session._setjmp_above({}, 9) == 0
    note = session._setjmp_note(9, 0, "declared at line {}, below this stop")
    assert note.refuses and note.startswith(
        "declared at line 9, below this stop, and any call above the stop may return twice "
        "(py/nlraarch64.c is a unit Peeky doesn't read; a config's \"returns_twice\" can name "
        "the functions that do)"
    )


def test_what_pads_the_space_between_functions_is_no_code():
    from peeky.native_binary import _padding

    assert _padding(b"\0" * 12)
    assert _padding(b"\x1f\x20\x03\xd5" * 3 + b"\0" * 4)
    assert _padding(b"\x66\x2e\x0f\x1f\x84\x00\x00\x00\x00\x00\x0f\x1f\x40\x00\x90\xcc")
    # AArch64's `ret`, x86's `ret`: code.
    assert not _padding(b"\xc0\x03\x5f\xd6")
    assert not _padding(b"\x90\xc3")


def test_the_note_names_the_configs_claim(root):
    session = _session(())
    session.any_call = None
    session.target = SimpleNamespace(target=SimpleNamespace(returns_twice=("nlr_push",)))
    note = session._setjmp_note(5, 4, "declared on line {}, at this stop")
    assert note.refuses and note.startswith(
        "declared on line 5, at this stop, and this function calls setjmp, another function "
        "that returns twice, or one the config's "
        '"returns_twice" names (line 4), above the stop'
    )


def test_code_no_unit_covers_is_named(root, monkeypatch):
    from peeky import native_binary
    from peeky.native_binary import CodeMap, CodeSection, CodeSymbol, uncovered_code

    ret = b"\xc0\x03\x5f\xd6"
    text = bytearray(0x100)
    text[0x60:0x64] = ret  # nlr_push
    text[0xC4:0xC8] = ret  # no symbol
    binary = root / "prog"
    binary.write_bytes(bytes(text) + ret * 4)
    sections = [CodeSection(".text", 0x1000, 0x1100, 0), CodeSection(".plt", 0x1100, 0x1110, 0x100)]
    symbols = [
        CodeSymbol("main", 0x1000, 0x40, False),
        CodeSymbol("_start", 0x1040, 0x10, False),
        CodeSymbol("nlr_push", 0x1060, 0, False),
        CodeSymbol("helper", 0x1080, 0x20, False),
    ]
    monkeypatch.setattr(native_binary, "code_symbols", lambda binary: CodeMap(symbols, sections))
    units = [SimpleNamespace(ranges=[(0x1000, 0x1040)]), SimpleNamespace(ranges=[(0x1080, 0x10A0)])]
    assert uncovered_code(binary, units) == [
        "nlr_push is code no unit's debug information covers (assembly built without -g?)",
        "the code at 0x10c4 has no symbol, and no unit's debug information covers it",
    ]
    # Startup code, padding and the linker's own sections are no one's.
    units.append(SimpleNamespace(ranges=[(0x1060, 0x1064), (0x10C4, 0x10C8)]))
    assert uncovered_code(binary, units) == []

    monkeypatch.setattr(native_binary, "code_symbols", lambda binary: CodeMap(None, sections))
    assert uncovered_code(binary, units) == [
        "the binary has no symbol table, so Peeky can't tell what code no unit covers"
    ]

    def unreadable(binary):
        raise ValueError("truncated")

    monkeypatch.setattr(native_binary, "code_symbols", unreadable)
    assert uncovered_code(binary, units) == [
        "Peeky can't read the binary's symbol table (truncated), so it can't tell what code no "
        "unit covers"
    ]


def test_padding_longer_than_a_read_is_read_to_its_end(root):
    from peeky.native_binary import CodeSection, _code_between

    binary = root / "prog"
    binary.write_bytes(b"\0" * (3 << 20) + b"\xc0\x03\x5f\xd6")
    section = CodeSection(".text", 0x1000, 0x1000 + (3 << 20) + 4, 0)
    with open(binary, "rb") as stream:
        assert _code_between(stream, section, section.low, section.high) == 0x1000 + (3 << 20)
        assert _code_between(stream, section, section.low, section.high - 4) is None


def _search(root, *angled):
    (root / "search").write_text(
        '#include "..." search starts here:\n#include <...> search starts here:\n'
        + "".join(f" {directory}\n" for directory in angled)
        + "End of search list.\n"
    )


def test_what_asks_whether_a_file_exists_is_found():
    from peeky.native_binary import probes

    text = """#ifndef __has_include
#define __has_include(x) 0
#endif
#if defined(__has_include) && __has_include(<a/b.h>) // __has_include(<no.h>)
#endif
/* __has_include("c.h") */
#if __has_include_next( "d.h" )
#endif
#define USE_TBB __has_include(<tbb/tbb.h>)
#define ASK(x) __has_include(x)
const char *s = "__has_include(<str.h>)";
#if __has_include \\
  (<cont.h>)
#endif
#embed "data.bin"
"""
    found = [
        (probe.line, probe.kind, probe.angled, probe.header, probe.macro) for probe in probes(text)
    ]
    assert found == [
        (4, "__has_include", True, "a/b.h", None),
        (7, "__has_include_next", False, "d.h", None),
        (9, "__has_include", True, "tbb/tbb.h", "USE_TBB"),
        (10, "__has_include", False, None, "ASK"),
        (12, "__has_include", True, "cont.h", None),
        (15, "#embed", False, None, None),
    ]


def test_a_probe_the_replay_answers_no_leaves_the_text_unread(root, tmp_path):
    from peeky.native_binary import UnitText

    (root / "src" / "a.h").write_text("#if __has_include(<extra/config.h>)\n#endif\n")
    (root / "reads").write_text("src/a.h")
    include = tmp_path / "include"
    _search(root, include)
    assert _replay(root, tmp_path) == (
        "Peeky can't read src/main.c as compiled: its preprocessing asks whether "
        "<extra/config.h> exists (__has_include, in src/a.h:1), and finds none: the build may "
        "have found one (its environment can add header paths, and a file may since be gone)"
    )
    # Found now: clang lists what a probe finds among the replay's dependencies, which
    # the line table must name; so the build found it too, or the probe never ran.
    (include / "extra").mkdir(parents=True)
    (include / "extra" / "config.h").write_text("")
    assert isinstance(_replay(root, tmp_path), UnitText)
    # Without the compiler's search path, Peeky can't tell.
    (root / "search").unlink()
    assert "can't read the compiler's search path" in _replay(root, tmp_path)


@pytest.mark.parametrize(
    "header, said",
    [
        ('#embed "table.bin"\n', "reads a file to embed (#embed, in src/a.h:1)"),
        ("#if __has_embed(<t.bin>)\n#endif\n", "reads a file to embed (__has_embed, in "),
        ("#define ASK(x) __has_include(x)\n#if ASK(<q.h>)\n#endif\n", "naming it in a way"),
        ('#define HAVE __has_include("q.h")\n#if HAVE\n#endif\n', "in a macro, whose search"),
    ],
)
def test_what_peeky_cant_follow_leaves_the_text_unread(root, tmp_path, header, said):
    (root / "src" / "a.h").write_text(header)
    (root / "reads").write_text("src/a.h")
    _search(root, tmp_path)
    assert said in _replay(root, tmp_path)


def test_a_probe_in_a_macro_no_input_names_never_runs(root, tmp_path):
    from peeky.native_binary import UnitText

    (root / "src" / "a.h").write_text("#define HAVE_TBB __has_include(<tbb/tbb.h>)\n")
    (root / "reads").write_text("src/a.h")
    _search(root, tmp_path)
    assert isinstance(_replay(root, tmp_path), UnitText)


def test_a_probe_s_file_is_looked_up_as_clang_does(tmp_path):
    from peeky.native_binary import Probe, _found

    first, second, own = tmp_path / "first", tmp_path / "second", tmp_path / "own"
    for directory in (first, second, own):
        directory.mkdir()
    (first / "stdint.h").write_text("")
    (second / "stdint.h").write_text("")
    (own / "local.h").write_text("")
    lists = ([], [(str(first), True), (str(second), True)])
    includer = str(first / "stdint.h")

    def probe(kind, header, angled=True):
        return Probe(1, kind, angled, header, None)

    assert _found(probe("__has_include", "stdint.h"), includer, lists) == str(first / "stdint.h")
    # Next: on after the directory that holds the includer.
    found = _found(probe("__has_include_next", "stdint.h"), includer, lists)
    assert found == str(second / "stdint.h")
    assert _found(probe("__has_include_next", "stdint.h"), str(second / "stdint.h"), lists) is None
    # Not under any directory searched: Peeky can't tell where the search goes on.
    assert _found(probe("__has_include_next", "local.h"), str(own / "x.h"), lists) is None
    # Quoted: the includer's own directory first; never for an angled one.
    assert _found(probe("__has_include", "local.h", False), str(own / "x.h"), lists)
    assert _found(probe("__has_include", "local.h"), str(own / "x.h"), lists) is None
    # A framework directory isn't looked in.
    framework = ([], [(str(own), False)])
    assert _found(probe("__has_include", "local.h"), includer, framework) is None


def test_asm_in_a_header_outside_the_program_counts(root, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-ext" / "spin.h"
    (root / "output").write_text(
        f'# 1 "src/main.c"\n# 1 "{outside}" 1\nstatic inline int spin(int x) {{\n'
        '  __asm__ volatile("" ::: "memory");\n  return x;\n}\n'
        '# 2 "src/main.c" 2\nint main(void) { return spin(0); }\n'
    )
    text = _replay(root, tmp_path)
    assert text.asm == f"src/main.c's text holds asm ({outside}:2)", text


@pytest.mark.parametrize(
    "code, said",
    [
        ('extern int fscanf(void *) __asm__ ("" "__isoc99_fscanf");', None),
        ('int x __asm__("xx"); int a[2] asm("arr");', None),
        ('void g(void) noexcept (true) __asm__ ("g2");', None),
        ('extern int jump(void *) __attribute__((nothrow)) __asm__ ("_setjmp");', "symbol _setjmp"),
        ('extern int spawn(void) __asm__ ("_vfork");', "symbol _vfork"),
        ('void f(int x) { if (x) __asm__("nop"); }', "holds asm"),
        ('void f(void) { __asm__ volatile ("" ::: "memory"); }', "holds asm"),
        ('__asm__(".globl foo");', "holds asm"),
        ('void f(void) { asm goto ("" :::: done); done:; }', "holds asm"),
    ],
)
def test_an_asm_label_is_read_by_the_symbol_it_gives(code, said):
    from peeky.native_binary import asm_uses

    found = asm_uses(code, True)
    assert (found[0][1] if found else None) == said or said in found[0][1], found


def test_units_built_from_one_file_with_other_commands_leave_its_text_unread(root, tmp_path):
    from peeky.native_binary import UnitText, replay_units

    compiler = tmp_path / "fake-cc"
    compiler.write_text(FAKE_CC)
    compiler.chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    one = judge(_unit(root), _tables(root), CLANG, root)
    other = Judged(**{**one.__dict__, "words": (*one.words[:-1], "-DVARIANT=2", one.words[-1])})
    texts = replay_units([one, other], str(compiler), scratch, root, None)
    assert texts == {
        "src/main.c": "Peeky can't read src/main.c as compiled: 2 units are built from it, with "
        "different commands, and Peeky reads a unit's text by its file's name"
    }
    # Built alike, they are one text.
    texts = replay_units([one, one], str(compiler), scratch, root, None)
    assert isinstance(texts["src/main.c"], UnitText)


def test_a_joined_sysroot_holds_the_sdk_settings_too(root, tmp_path):
    from peeky.native_binary import UnitText

    sdk = tmp_path / "SDK"
    sdk.mkdir()
    (sdk / "SDKSettings.json").write_text("{}")
    (root / "reads").write_text(str(sdk / "SDKSettings.json"))
    words = ["/usr/bin/clang", f"--sysroot={sdk}", "-O0", "-c", "src/main.c"]
    assert isinstance(_replay(root, tmp_path, words=words), UnitText)


def _piece(origin, section, data, masked=(), **symbols):
    from peeky.native_binary import StartupPiece

    cleared = bytearray(data)
    for at in masked:
        cleared[at] = 0
    return StartupPiece(origin, section, bytes(cleared), tuple(masked), symbols)


RET = b"\xc0\x03\x5f\xd6"  # ret
BL = b"\x46\x00\x00\x94"  # bl, its target filled in by the linker
MOV = b"\xe9\x03\x09\xaa"  # mov x9, x9


def test_startup_names_count_unless_their_bytes_are_the_toolchain_s(root, monkeypatch):
    # frame_dummy, init_have_lse_atomics and the like aren't reserved names: a program
    # may define its own, even local, and called by .init_array.
    from peeky import native_binary
    from peeky.native_binary import CodeMap, CodeSection, CodeSymbol, uncovered_code

    binary = root / "prog"
    text = bytearray(0x100)
    text[0x40:0x48] = BL + RET  # crtbeginS.o's block, as linked
    binary.write_bytes(bytes(text))
    sections = [CodeSection(".text", 0x1000, 0x1100, 0)]
    crtbegin = _piece(
        "crtbeginS.o",
        ".text",
        b"\0\0\0\x94" + RET,
        (0, 1, 2, 3),
        register_tm_clones=0,
        frame_dummy=4,
    )

    def found(*symbols, pieces=(crtbegin,), why=None):
        monkeypatch.setattr(
            native_binary, "code_symbols", lambda binary: CodeMap(list(symbols), sections)
        )
        return uncovered_code(binary, [], lambda: (list(pieces), why))

    said = "{} is code no unit's debug information covers"
    # crtbeginS.o's bytes, its call's target masked: the toolchain's, the block whole.
    block = (
        CodeSymbol("register_tm_clones", 0x1040, 0, True),
        CodeSymbol("frame_dummy", 0x1044, 0, True),
    )
    assert found(*block) == []
    # Reserved to the implementation: by name.
    assert found(*block, CodeSymbol("_start", 0x1000, 8, False)) == []
    assert found(*block, CodeSymbol("__aarch64_ldadd4_acq_rel", 0x1000, 8, False)) == []
    # Other bytes, though local: the program's.
    own = CodeSymbol("frame_dummy", 0x1080, 0, True)
    assert found(own) == [
        said.format("frame_dummy") + ", and its bytes are none of the toolchain's (crtbeginS.o)"
    ]
    assert found(own, pieces=(), why="clang finds no crtbeginS.o") == [
        said.format("frame_dummy")
        + ", and Peeky found no toolchain startup object holding it (clang finds no crtbeginS.o)"
    ]


def test_a_startup_piece_masks_what_the_linker_fills_in():
    piece = _piece("crti.o", ".init", b"\0\0\0\x94" + RET, (0, 1, 2, 3))
    assert piece.matches(BL + RET) and not piece.matches(BL + MOV) and not piece.matches(BL)
    assert not piece.matches(None)


def test_init_is_the_toolchain_s_only_where_crti_and_crtn_s_bytes_are(root, monkeypatch):
    # .init and .fini hold whatever objects put there, around crti.o's head and crtn.o's
    # tail; their _init and _fini symbols have no size.
    from peeky import native_binary
    from peeky.native_binary import CodeMap, CodeSection, CodeSymbol, uncovered_code

    binary = root / "prog"
    symbols = [CodeSymbol("_init", 0x2000, 0, False)]
    head = _piece("crti.o", ".init", MOV + b"\0\0\0\x94", (4, 5, 6, 7))
    tail = _piece("crtn.o", ".init", RET)

    def found(data, pieces=(head, tail), why=None):
        binary.write_bytes(data)
        sections = [CodeSection(".init", 0x2000, 0x2000 + len(data), 0)]
        monkeypatch.setattr(
            native_binary, "code_symbols", lambda binary: CodeMap(symbols, sections)
        )
        return uncovered_code(binary, [], lambda: (list(pieces), why))

    assert found(MOV + BL + RET) == []
    # Another object's code between them.
    assert found(MOV + BL + MOV + RET) == [
        "the code at 0x2008, in .init, may be the program's: it isn't crti.o's or crtn.o's"
    ]
    # Not crti.o's head at all.
    assert found(RET + BL + RET) == [
        "the code at 0x2000, in .init, may be the program's: it isn't crti.o's or crtn.o's"
    ]
    assert found(MOV + BL + RET, pieces=(), why="clang finds no crti.o") == [
        "the code at 0x2000, in .init, may be the program's: Peeky can't compare it with "
        "crti.o's and crtn.o's (clang finds no crti.o)"
    ]


@pytest.mark.parametrize(
    "defined, said",
    [
        ('-DCHECK=__has_include("x.h")', 'defines CHECK as __has_include("x.h"), which asks'),
        ("-DHAVE=__has_include_next", "defines HAVE as __has_include_next, which asks"),
        ("-DP=__has_##include", "which may paste together a probe"),
        ("-DCAT(a,b)=a##b", "defines CAT as a##b, which may paste together a probe"),
        ("-DLEFT=__has_", "which holds __h, part of a probe's name"),
        ("-DMID=has_inclu", "which holds has, part of a probe's name"),
        ("-DEND=lude_next", "which holds lud, part of a probe's name"),
    ],
)
def test_a_probe_a_recorded_define_may_hold_leaves_the_text_unread(root, tmp_path, defined, said):
    # `-DCHECK=__has_include("x.h")` and `#if CHECK`: no file the replay reads names it.
    words = ["/usr/bin/clang", defined, "-O0", "-c", "src/main.c"]
    assert said in _replay(root, tmp_path, words=words)


def test_a_define_s_literal_is_no_probe(root, tmp_path):
    from peeky.native_binary import UnitText

    words = ["/usr/bin/clang", '-DPREFIX="/usr/include"', '-DS="#x"', "-O0", "-c", "src/main.c"]
    assert isinstance(_replay(root, tmp_path, words=words), UnitText)
