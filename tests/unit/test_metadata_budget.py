import os
import sys

import pytest

from peeky.errors import classify
from peeky.native import (
    MAX_DEBUG_INFO,
    MAX_METADATA,
    LineTable,
    SymbolLimit,
    _line_rows,
    _metadata,
    _parse_dies,
    _symbol_text,
    has_tail_calls,
    parse_lines,
    read_debug_info,
    read_line_table,
    symbol_limit,
)
from peeky.textlines import split_lines


def test_engineering_capture_does_not_relax_production_parser(tmp_path):
    writer = tmp_path / "writer.py"
    writer.write_text(
        'import pathlib, sys\npathlib.Path(sys.argv[-1]).write_bytes(b"x" * (3 * 1024 * 1024))\n'
    )
    with pytest.raises((RuntimeError, ValueError), match="budget|limit"):
        _metadata(sys.executable, [str(writer)], tmp_path, dict(os.environ))
    dump = _metadata(
        sys.executable, [str(writer)], tmp_path, dict(os.environ), max_bytes=4 * 1024 * 1024
    )
    assert len(dump) == 3 * 1024 * 1024 > MAX_METADATA
    with pytest.raises(SymbolLimit, match="at most 2 MiB of a build's line tables"):
        parse_lines(dump, tmp_path, (), limit=MAX_METADATA)
    assert not list(tmp_path.glob("peeky-symbols-*"))


def tool(tmp_path, body):
    """A stand-in for llvm-dwarfdump: a Python script running ``body``."""
    script = tmp_path / "dwarfdump"
    script.write_text(f"#!{sys.executable}\nimport os, sys, time\nout = sys.stdout.buffer\n{body}")
    script.chmod(0o755)
    return str(script)


def read(tmp_path, script, *, limit=MAX_DEBUG_INFO, timeout=15, commands=([],)):
    with _symbol_text(
        script,
        list(commands),
        tmp_path,
        dict(os.environ),
        limit=limit,
        timeout=timeout,
        what="debug information",
    ) as lines:
        return list(lines)


# Lines as dwarfdump may end them: `\r\n`, a lone `\r` inside a line and at the end of
# the text, a name with a character of several bytes, and no last `\n`.
TEXT = 'a\r\nb\rc\n\n  DW_AT_name\t("caf\u00e9 \u2028")\n\u00e9\u00e9\u00e9\r'


def test_symbol_text_gives_the_lines_split_lines_gives_however_it_is_written(tmp_path):
    # Written a byte at a time, so a character's bytes and a `\r\n` come in two reads.
    data = TEXT.encode()
    script = tool(
        tmp_path,
        f"for byte in {data!r}:\n    out.write(bytes([byte])); out.flush(); time.sleep(0.001)\n",
    )
    assert read(tmp_path, script) == split_lines(TEXT)
    whole = tool(tmp_path, f"out.write({(TEXT * 3000).encode()!r})\n")
    assert read(tmp_path, whole, commands=([], [])) == split_lines(TEXT * 3000) * 2
    assert not list(tmp_path.glob("peeky-symbols-*"))


def test_symbol_text_over_its_limit_is_refused_as_a_budget(tmp_path):
    # Every bound Peeky puts on a build's symbols is BUDGET_EXHAUSTED, with what to do
    # (a dump over 16 MiB was a DEBUGGER_ERROR, "failed or exceeded its budget").
    script = tool(tmp_path, "out.write(b'0x1: DW_TAG_member\\n' * 300000)\n")
    with pytest.raises(SymbolLimit) as refused:
        read(tmp_path, script, limit=4 * 1024 * 1024)
    error = classify(refused.value)
    assert error["code"] == "BUDGET_EXHAUSTED"
    assert error["message"] == (
        "Peeky reads at most 4 MiB of a build's debug information, and this build has more: "
        "probe a smaller "
        "program (fewer source files, standard headers or std collections)."
    )
    assert error["hint"].startswith("Probe a smaller program") and len(error["hint"]) <= 150
    assert refused.value.detail == "over Peeky's limit of 4 MiB; probe a smaller program"
    kept = classify(symbol_limit("300 MiB", "debug information", kept=True))
    assert kept["code"] == "BUDGET_EXHAUSTED" and kept["message"].startswith(
        "Peeky keeps at most 300 MiB of a build's debug information in memory, and this build "
        "needs more: probe a smaller program"
    )
    assert not list(tmp_path.glob("peeky-symbols-*"))


def test_symbol_text_has_a_time_limit_and_a_failing_tool_says_why(tmp_path):
    slow = tool(tmp_path, "out.write(b'a\\n'); out.flush(); time.sleep(30)\n")
    with pytest.raises(TimeoutError, match="timed out after 0.5 s") as late:
        read(tmp_path, slow, timeout=0.5)
    assert classify(late.value)["code"] == "TIMEOUT"
    assert late.value.detail == "it took over 0.5 s; probe a smaller program"
    failing = tool(tmp_path, "sys.stderr.write('no such file'); sys.exit(3)\n")
    with pytest.raises(RuntimeError, match="exit=3, diagnostics=no such file") as failed:
        read(tmp_path, failing)
    assert classify(failed.value)["code"] == "DEBUGGER_ERROR"
    with pytest.raises(RuntimeError, match="not read to its end"):
        with _symbol_text(failing, [[]], tmp_path, {}, limit=1024, timeout=5, what="x"):
            pass
    for limit in (0, MAX_DEBUG_INFO + 1):
        with pytest.raises(ValueError, match="capture limit"):
            read(tmp_path, failing, limit=limit)
    assert not list(tmp_path.glob("peeky-symbols-*"))


INFO = """program:\tfile format Mach-O arm64

.debug_info contents:
0x0000000b: Compile Unit: length = 0x00000099, format = DWARF32, version = 0x0004
0x0000000b: DW_TAG_compile_unit
              DW_AT_name\t("app.cpp")
0x00000020:   DW_TAG_namespace
                DW_AT_name\t("geo")
0x00000025:     DW_TAG_structure_type
                  DW_AT_name\t("Box")
                  DW_AT_byte_size\t(0x08)
0x0000002a:       DW_TAG_member
                    DW_AT_name\t("side")
                    DW_AT_type\t(0x00000090 "long")
0x00000030:       NULL
0x00000031:     NULL
0x00000060:   DW_TAG_subprogram
                DW_AT_low_pc\t(0x0000000100000400)
                DW_AT_high_pc\t(0x0000000100000420)
                DW_AT_ranges\t(0x00000000
                   [0x0000000100000400, 0x0000000100000410)
                   [0x0000000100000418, 0x0000000100000420))
                DW_AT_name\t("main")
0x00000068:     DW_TAG_variable
                  DW_AT_name\t("box")
                  DW_AT_type\t(0x00000025 "geo::Box")
0x00000070:     NULL
0x00000090:   DW_TAG_base_type
                DW_AT_name\t("long")
0x000000a0:   NULL
"""
TAIL_CALL = "0x00000080:   DW_TAG_call_site\n                DW_AT_call_tail_call\t(true)\n"

LINE = """program:\tfile format Mach-O arm64

.debug_line contents:
debug_line[0x00000000]
version: 4
include_directories[  1] = "/sdk"
file_names[  1]:
name: "app.cpp"
dir_index: 0
file_names[  2]:
name: "vector"
dir_index: 1
Address            Line   Column File   ISA Discriminator Flags
0x0000000100000400      3      1      1   0             0  is_stmt
0x0000000100000408      4      1      1   0             0  is_stmt prologue_end
0x0000000100000410      5      1      1   0             0  is_stmt
0x0000000100000410    900      1      2   0             0  is_stmt
0x0000000100000418      6      1      1   0             0  is_stmt epilogue_begin
0x0000000100000420      6      1      1   0             0  is_stmt end_sequence
"""


def cat(tmp_path, text):
    (tmp_path / "dump.txt").write_text(text)
    return tool(tmp_path, f"out.write(open({str(tmp_path / 'dump.txt')!r}, 'rb').read())\n")


@pytest.mark.parametrize("text", [INFO, INFO.replace("0x000000a0:", TAIL_CALL + "0x000000a0:")])
def test_debug_information_read_as_it_is_written_is_the_texts(tmp_path, text):
    script = cat(tmp_path, text)
    dies = read_debug_info(script, [[]], tmp_path, dict(os.environ), timeout=15)
    assert dies == _parse_dies(text) and len(dies) == 7 + (TAIL_CALL in text)
    assert dies[0x60]["ranges"] == [(0x100000400, 0x100000410), (0x100000418, 0x100000420)]
    assert has_tail_calls(dies) is has_tail_calls(text) is (TAIL_CALL in text)
    # Several units in turn, as the text of all of them.
    both = read_debug_info(script, [[], []], tmp_path, dict(os.environ), timeout=15)
    assert both == _parse_dies(text + text) == dies


def test_line_tables_read_as_they_are_written_are_the_texts(tmp_path):
    (tmp_path / "app.cpp").write_text("\n" * 8)
    script = cat(tmp_path, LINE)
    table = read_line_table(script, [[]], tmp_path, dict(os.environ), ("app.cpp",), timeout=15)
    assert isinstance(table, LineTable)
    marks = [set(), set(), set()], [set(), set(), set()]
    for found, source in zip(marks, (table, LINE)):
        rows = _line_rows(
            source,
            tmp_path,
            ("app.cpp",),
            replaced=found[0],
            epilogues=found[1],
            prologues=found[2],
        )
        assert rows == _line_rows(LINE, tmp_path, ("app.cpp",))
        assert [number for _, _, number in rows] == [3, 4, 5, 6]
    assert marks[0] == marks[1] == [{(0x100000410, "app.cpp", 5)}, {0x100000418}, {0x100000408}]
    assert parse_lines(table, tmp_path, ["app.cpp"]) == {"app.cpp": [3, 4, 5, 6]}
    # Read for other sources, or as Rust counts lines, it is not this table.
    with pytest.raises(ValueError, match="read for other sources"):
        _line_rows(table, tmp_path, ("app.cpp",), rust=True)


@pytest.mark.parametrize("limit", [True, 0, -1, 16 * 1024 * 1024 + 1])
def test_capture_limit_cannot_be_unbounded(tmp_path, limit):
    with pytest.raises(ValueError, match="capture limit"):
        _metadata("unused", [], tmp_path, {}, max_bytes=limit)


@pytest.mark.parametrize(
    "second, survives",
    [(ProcessLookupError, False), (None, False), (PermissionError, True)],
)
def test_process_group_eperm_is_tolerated_only_after_confirmed_absence(
    tmp_path, monkeypatch, second, survives
):
    from peeky.agent import execute

    writer = tmp_path / "writer.py"
    writer.write_text('import pathlib, sys\npathlib.Path(sys.argv[-1]).write_text("ok")\n')
    for module in ("peeky.agent",):
        calls = []

        def killpg(pid, sig):
            calls.append(pid)
            if len(calls) % 2 == 1:
                raise PermissionError("Operation not permitted")
            if second is not None:
                raise second("second attempt")

        monkeypatch.setattr(f"{module}.os.killpg", killpg)
        if survives:
            with pytest.raises(PermissionError):
                _metadata(sys.executable, [str(writer)], tmp_path, dict(os.environ))
            with pytest.raises(PermissionError):
                execute([sys.executable, "-c", "print('done')"], tmp_path)
        else:
            assert _metadata(sys.executable, [str(writer)], tmp_path, dict(os.environ)) == "ok"
            assert execute([sys.executable, "-c", "print('done')"], tmp_path)["stdout"] == "done\n"
        assert len(calls) == 4


# A unit's entry, then a function's: room for both and a few ranges, never for more.
HEAD = [
    "0x0000000b: DW_TAG_compile_unit",
    '              DW_AT_name\t("app.cpp")',
    "0x00000020:   DW_TAG_subprogram",
]


def refused_before_kept(monkeypatch, lines, dies):
    """Parse ``lines`` (made before measuring) with MAX_KEPT at 1 MiB: refused, and the
    most Python held meanwhile."""
    import tracemalloc

    monkeypatch.setattr("peeky.native.MAX_KEPT", 1 << 20)
    tracemalloc.start()
    try:
        with pytest.raises(SymbolLimit, match="keeps at most 1 MiB of a build's debug info"):
            _parse_dies(iter(lines), dies)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_a_value_over_what_is_left_is_refused_before_it_is_kept(monkeypatch):
    # Each line's cost is reckoned before anything of it is kept or copied (review): a 10
    # MiB name was kept, then refused, holding 21 MB against a 1 MiB bound.
    from peeky.native import DieMap

    lines = [*HEAD, '                DW_AT_name\t("' + "n" * (10 << 20) + '")']
    dies = DieMap()
    peak = refused_before_kept(monkeypatch, lines, dies)
    assert peak < 1 << 20, peak
    assert sorted(dies) == [0x0B, 0x20] and dies[0x20]["attrs"] == {}


@pytest.mark.parametrize("one_line", [False, True])
def test_ranges_over_what_is_left_are_refused_before_they_are_kept(monkeypatch, one_line):
    from peeky.native import _RANGE_BYTES, DieMap

    ranges = [f"[0x{n:016x}, 0x{n + 1:016x})" for n in range(30_000)]
    if one_line:
        lines = [*HEAD, f"                DW_AT_ranges\t(0x00000000 {' '.join(ranges)})"]
    else:
        rows = [f"                   {each}" for each in ranges]
        lines = [*HEAD, "                DW_AT_ranges\t(0x00000000", *rows[:-1], rows[-1] + ")"]
    # What the entries and the list's start keep: the rows after it fill the rest.
    before = _parse_dies(iter(lines[:4])).kept
    dies = DieMap()
    peak = refused_before_kept(monkeypatch, lines, dies)
    # Each range is reckoned at _RANGE_BYTES: all of them would take 6 MB.
    assert 30_000 * _RANGE_BYTES > 4 << 20 and peak < 1 << 20, peak
    if one_line:
        assert dies[0x20]["ranges"] == [] and dies[0x20]["attrs"] == {}
    else:
        # Every row that fits, and not the one that doesn't.
        assert len(dies[0x20]["ranges"]) == ((1 << 20) - before) // _RANGE_BYTES


def test_a_line_over_max_line_is_refused_as_it_is_read(tmp_path, monkeypatch):
    # Reading holds a line at most MAX_LINE long, and one read: never the whole line.
    import tracemalloc

    # What reading imports the first time is no part of what it holds.
    import peeky.agent  # noqa: F401

    monkeypatch.setattr("peeky.native.MAX_LINE", 1 << 20)
    script = tool(tmp_path, "out.write(b'a' * (10 << 20) + b'\\n')\n")
    tracemalloc.start()
    try:
        with pytest.raises(SymbolLimit, match="at most 1 MiB of one line of a build's debug info"):
            read(tmp_path, script)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 3 << 20, peak
    refused = SymbolLimit(
        "Peeky reads at most 1 MiB of one line of a build's debug information", ""
    )
    assert classify(refused)["code"] == "BUDGET_EXHAUSTED"


@pytest.mark.parametrize("one_line", [True, False])
def test_ranges_near_the_bound_are_read_within_it(monkeypatch, one_line):
    # Accepted, a line's ranges are read one at a time (review): all 30,000 of a line's
    # matches at once took a parse with an 8 MiB bound to 10.7 MB.
    import tracemalloc

    from peeky.native import DieMap

    ranges = [f"[0x{n:016x}, 0x{n + 1:016x})" for n in range(30_000)]
    if one_line:
        lines = [*HEAD, f"                DW_AT_ranges\t(0x00000000 {' '.join(ranges)})"]
    else:
        rows = [f"                   {each}" for each in ranges]
        lines = [*HEAD, "                DW_AT_ranges\t(0x00000000", *rows[:-1], rows[-1] + ")"]
    monkeypatch.setattr("peeky.native.MAX_KEPT", 8 << 20)
    tracemalloc.start()
    try:
        dies = _parse_dies(iter(lines), DieMap())
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(dies[0x20]["ranges"]) == 30_000 and 5 << 20 < dies.kept <= 8 << 20
    assert peak < 8 << 20, peak
