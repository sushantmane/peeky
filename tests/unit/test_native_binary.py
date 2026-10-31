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
    for written, linked in ((second, second + 500_000_000), (second, second)):
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
