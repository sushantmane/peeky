import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from peeky.native import NativeSession, NativeTarget, elf_build_id, parse_lines


def line_dump(name="main.c", line=2, version=4):
    return f'''debug_line[0x00000000]
version: {version}
file_names[ 1]:
name: "{name}"
dir_index: 0
Address Line Column File
0x0000000000401000 {line} 1 1 0 0 is_stmt
0x0000000000401010 {line} 1 1 0 0 end_sequence
'''


def test_line_metadata_resolves_declared_relative_source(tmp_path):
    (tmp_path / "main.c").write_text("int main() {\nreturn 0;\n}\n")
    assert parse_lines(line_dump(), tmp_path, ("main.c",)) == {"main.c": [2]}


@pytest.mark.parametrize(
    "text",
    [
        line_dump(line=99),
        line_dump(version=5),
        line_dump("other.c"),
        line_dump().replace("dir_index: 0", "dir_index: 9"),
        "",
        line_dump().replace("2 1 1", "bad 1 1"),
    ],
)
def test_line_metadata_rejects_missing_mismatched_or_invalid_input(tmp_path, text):
    (tmp_path / "main.c").write_text("int main() {\nreturn 0;\n}\n")
    with pytest.raises(ValueError):
        parse_lines(text, tmp_path, ("main.c",))


def elf_fixture(tmp_path, *, length=20, note_length=None):
    identity = b"\x12" * length
    note = struct.pack("<III", 4, length, 3) + b"GNU\0" + identity
    note += b"\0" * ((-len(note)) % 4)
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HH", header, 54, 56, 1)
    program = struct.pack(
        "<IIQQQQQQ", 4, 0, 120, 0, 0, len(note) if note_length is None else note_length, 0, 4
    )
    binary = tmp_path / "program"
    binary.write_bytes(header + program + note)
    return binary, identity.hex().upper()


def test_elf_build_id_matches_recorded_note(tmp_path):
    binary, identity = elf_fixture(tmp_path)
    assert elf_build_id(binary) == identity


@pytest.mark.parametrize("length,note_length", [(4, None), (20, 9999)])
def test_elf_rejects_unsupported_or_truncated_notes(tmp_path, length, note_length):
    binary, _ = elf_fixture(tmp_path, length=length, note_length=note_length)
    with pytest.raises(ValueError):
        elf_build_id(binary)


@pytest.mark.parametrize(
    "name", ["../main.c", "-main.c", "@main.c", "/main.c", "a//main.c", "main.cpp"]
)
def test_fixed_native_target_rejects_unsafe_or_wrong_language_inputs(name):
    with pytest.raises(ValueError):
        NativeTarget(sources=(name,))


def session():
    return NativeSession.__new__(NativeSession)


@pytest.mark.parametrize(
    "variable,value",
    [
        ({"name": "p", "type": "int *", "variablesReference": 0}, "0"),
        ({"name": "a", "type": "int[3]", "variablesReference": 7}, "1"),
        ({"name": "x", "type": "const int"}, "2"),
        ({"name": "x", "type": "int", "presentationHint": {"attributes": ["readOnly"]}}, "2"),
        ({"name": "*(p)", "type": "int"}, "2"),
        ({"name": "x", "type": "int"}, "2147483648"),
        ({"name": "x", "type": "unsigned int"}, "-1"),
        ({"name": "x", "type": "int"}, "system(1)"),
        ({"name": "x", "type": "int"}, "0x100"),
        ({"name": "x", "type": "int"}, "010"),
        ({"name": "x", "type": "int"}, "-010"),
    ],
)
def test_native_mutation_rejects_pointer_aggregate_readonly_or_expression(variable, value):
    with pytest.raises(ValueError):
        session().validate_variable(variable, value)


@pytest.mark.parametrize(
    "kind,value", [("int", "-2147483648"), ("int", "+2147483647"), ("unsigned int", "4294967295")]
)
def test_native_integer_boundaries(kind, value):
    session().validate_variable({"name": "value", "type": kind, "variablesReference": 0}, value)


def test_backend_capabilities_cannot_enable_unsupported_operations():
    caps = session().normalize_capabilities(
        {"supportsSetVariable": True, "supportsEvaluateForHovers": True, "supportsLogPoints": True}
    )
    assert caps["supports_set_variable"]
    assert not caps["supports_evaluate"] and not caps["supports_logpoints"]
    init = session().initialize_args()
    assert init["sourceInitFile"] is False and init["$__lldb_sourceInitFile"] is False


def test_module_identity_requires_path_uuid_and_symbols(tmp_path, monkeypatch):
    monkeypatch.setattr("peeky.native.sys.platform", "linux")
    runtime = session()
    runtime.binary = tmp_path / "program"
    runtime.binary.write_bytes(b"test")
    runtime.module_identity = "AABB"
    runtime.symbols = runtime.binary

    class Adapter:
        def __init__(self, module):
            self.module = module

        def request(self, *_):
            return {"modules": [self.module]}

    module = {
        "id": "AA-BB",
        "path": str(runtime.binary),
        "symbolStatus": "Symbols loaded.",
        "symbolFilePath": str(runtime.binary),
    }
    runtime.validate_debugger(Adapter(module), {"moduleId": "AA-BB"})
    for invalid in [
        dict(module, path=str(tmp_path / "missing")),
        dict(module, symbolStatus="Symbols not found."),
        dict(module, symbolFilePath=str(tmp_path / "foreign")),
        {key: value for key, value in module.items() if key != "symbolFilePath"},
        dict(module, id="CC-DD"),
    ]:
        with pytest.raises(RuntimeError):
            runtime.validate_debugger(Adapter(invalid), {"moduleId": invalid["id"]})


def test_stopped_source_must_match_prepared_line(tmp_path):
    runtime = session()
    runtime.prepared = SimpleNamespace(workspace=tmp_path)
    runtime._lines_data = '{"main.c": [2]}'
    source = tmp_path / "main.c"
    source.write_text("\n\n")
    runtime.validate_stop({"source": {"path": str(source)}, "line": 2})
    for value in (True, 1, "2"):
        with pytest.raises(RuntimeError):
            runtime.validate_stop({"source": {"path": str(source)}, "line": value})


def test_line_metadata_returns_defensive_copy():
    runtime = session()
    runtime._lines_data = '{"main.c": [2]}'
    runtime.lines["main.c"].append(3)
    assert runtime.lines == {"main.c": [2]}


def elf_load_fixture(tmp_path, *, address=0x400000, memory=256, files=256):
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HH", header, 54, 56, 2)
    table = struct.pack("<IIQQQQQQ", 1, 4, 0, address, 0, 256, 256, 4096)
    table += struct.pack("<IIQQQQQQ", 1, 5, 256, address + 4096, 0, files, memory, 4096)
    binary = tmp_path / "program"
    binary.write_bytes((header + table).ljust(512, b"\0"))
    return binary


@pytest.mark.parametrize("address", [0, 0x400000])
def test_llvm18_frame_pc_binds_to_loaded_executable(tmp_path, monkeypatch, address):
    monkeypatch.setattr("peeky.native.sys.platform", "linux")
    runtime = session()
    runtime.binary = runtime.symbols = elf_load_fixture(tmp_path, address=address)
    runtime.module_identity = "AABB"
    module = {
        "id": "AA-BB",
        "path": str(runtime.binary),
        "symbolStatus": "Symbols loaded.",
        "symbolFilePath": str(runtime.binary),
        "addressRange": str(0x700000),
    }
    adapter = SimpleNamespace(request=lambda *_: {"modules": [module]})
    frame = {"instructionPointerReference": "0x701010"}
    runtime.validate_debugger(adapter, frame)
    for invalid in [
        {},
        {"instructionPointerReference": "0x700010"},
        {"instructionPointerReference": "0x701100"},
        {"instructionPointerReference": "0x10000000000000000"},
        {"instructionPointerReference": "-1"},
        dict(frame, moduleId=None),
        dict(frame, moduleId="wrong"),
    ]:
        with pytest.raises(RuntimeError):
            runtime.validate_debugger(adapter, invalid)
    for loaded in [None, "0x700000", "-1", str(1 << 64), str((1 << 64) - 1)]:
        module["addressRange"] = loaded
        with pytest.raises(RuntimeError):
            runtime.validate_debugger(adapter, frame)
    module["addressRange"] = str(0x700000)
    adapter.request = lambda *_: {"modules": [module, dict(module)]}
    with pytest.raises(RuntimeError, match="ambiguous"):
        runtime.validate_debugger(adapter, frame)


@pytest.mark.parametrize("memory,files", [(10, 256), (300, 300), ((1 << 64) - 1, 256)])
def test_elf_load_ranges_reject_invalid_bounds(tmp_path, memory, files):
    from peeky.native import elf_executable_ranges

    binary = elf_load_fixture(tmp_path, memory=memory, files=files)
    with pytest.raises(ValueError):
        elf_executable_ranges(binary)


def test_elf_pc_cannot_bind_to_unbacked_executable_memory(tmp_path):
    from peeky.native import _elf_frame_matches

    binary = elf_load_fixture(tmp_path, memory=512, files=256)
    assert not _elf_frame_matches(
        binary, {"addressRange": str(0x700000)}, {"instructionPointerReference": "0x701100"}
    )


@pytest.mark.parametrize("key", ["value", "result"])
def test_native_mutation_value_normalizes_llvm_versions(key):
    assert session().mutation_value({key: "5000"}) == "5000"


def test_native_mutation_missing_value_is_uncertain_failure():
    with pytest.raises(RuntimeError, match="observed value"):
        session().mutation_value({})


# The unit's preprocessing (#180), its compile, the link, then dsymutil.
@pytest.mark.parametrize("command_index,retryable", [(0, True), (1, True), (2, True), (3, False)])
def test_native_prepare_retries_only_fixed_compile_and_link_commands(
    tmp_path, monkeypatch, command_index, retryable
):
    from peeky.build import BuildCommandError

    source = tmp_path / "main.c"
    source.write_text("int main(void) { return 0; }\n")
    monkeypatch.setattr("peeky.native.sys.platform", "darwin")
    monkeypatch.setattr("peeky.native._tool", lambda tool: f"/trusted/{tool}")

    def failing_prepare(recipe, root, **kwargs):
        raise BuildCommandError(
            recipe.build_commands[command_index].argv,
            {"exit_code": 7, "stdout": "", "stderr": "failed"},
        )

    monkeypatch.setattr("peeky.native.prepare", failing_prepare)
    with pytest.raises(RuntimeError) as caught:
        NativeTarget(sources=("main.c",)).prepare(source, tmp_path)
    assert isinstance(caught.value, BuildCommandError) is retryable
    if not retryable:
        assert "symbol preparation" in str(caught.value)
        assert isinstance(caught.value.__cause__, BuildCommandError)


def test_native_prepare_rejects_header_edits_before_running_tools(tmp_path, monkeypatch):
    from peeky.build import SourceEdit

    source = tmp_path / "main.c"
    source.write_text("int main(void) { return 0; }\n")
    (tmp_path / "config.h").write_text("#define SIZE 3\n")
    monkeypatch.setattr("peeky.native._tool", lambda _: pytest.fail("Tool must not run"))
    with pytest.raises(ValueError, match="declared C source"):
        NativeTarget(sources=("main.c",), headers=("config.h",)).prepare(
            source, tmp_path, edit=SourceEdit("config.h", "3", "4")
        )


@pytest.mark.parametrize("suffix", [".cpp", ".cc", ".cxx"])
def test_cpp_target_has_explicit_language_and_fixed_driver_flags(suffix):
    target = NativeTarget(sources=("main" + suffix,), headers=("types.hpp",), language="cpp")
    assert target.compiler == "clang++"
    assert target.compiler_options == (
        "-std=c++17",
        "-g",
        "-gdwarf-4",
        "-O0",
        "-fno-omit-frame-pointer",
    )
    assert target.target_identity["language"] == "cpp"
    assert target.verification_inputs == ("main" + suffix, "types.hpp")
    assert target.verification_sources == ("main" + suffix,)
    assert NativeTarget(sources=("main.c",)).compiler == "clang"


@pytest.mark.parametrize(
    "language,source",
    [
        ("rust", "main.c"),
        ("C++", "main.cpp"),
        (None, "main.c"),
        ("cpp", "main.c"),
        ("cpp", "-main.cpp"),
        ("cpp", "@main.cpp"),
    ],
)
def test_native_language_selection_is_explicit_and_validated(language, source):
    with pytest.raises(ValueError):
        NativeTarget(sources=(source,), language=language)


def test_cpp_build_recipe_uses_selected_compiler_for_compile_and_link(tmp_path, monkeypatch):
    source = tmp_path / "main.cpp"
    source.write_text("int main() { return 0; }\n")
    (tmp_path / "types.hpp").write_text("#pragma once\n")
    monkeypatch.setattr("peeky.native.sys.platform", "linux")
    monkeypatch.setattr("peeky.native._tool", lambda tool: tool)
    # What `/trusted/clang++-18 --version` would say (#167).
    monkeypatch.setattr("peeky.native.compiler_family", lambda path: "clang")

    class Captured(Exception):
        pass

    def capture(recipe, root, **kwargs):
        assert recipe.language == "cpp"
        assert recipe.inputs == ("main.cpp", "types.hpp")
        assert all(command.argv[0] == "/trusted/clang++-18" for command in recipe.build_commands)
        # The unit is preprocessed first, as it is compiled (#180).
        assert recipe.build_commands[0].argv[-4:] == ("-E", "main.cpp", "-o", "peeky-unit-0.i")
        assert recipe.build_commands[0].argv[:-4] == recipe.build_commands[1].argv[:-7]
        assert "-std=c++17" in recipe.build_commands[1].argv
        assert "-std=c11" not in recipe.build_commands[1].argv
        assert recipe.build_commands[2].argv[-2:] == ("-o", "program")
        raise Captured

    monkeypatch.setattr("peeky.native.prepare", capture)
    target = NativeTarget(
        sources=("main.cpp",),
        headers=("types.hpp",),
        language="cpp",
        compiler="/trusted/clang++-18",
    )
    with pytest.raises(Captured):
        target.prepare(source, tmp_path)


@pytest.mark.parametrize("language,suffix", [("c", ".c"), ("cpp", ".cpp")])
def test_native_verification_keeps_explicit_target_language(tmp_path, language, suffix):
    from peeky.native_verification import NativeVerification

    name = "main" + suffix
    (tmp_path / name).write_text("int main(void) { return 0; }\n")
    target = NativeTarget(sources=(name,), language=language)
    verification = NativeVerification(target, tmp_path)
    assert verification.language == language


def scope_dumps(workspace):
    """A C++ unit: a namespaced out-of-line method, a lambda, and an SDK function."""
    source = workspace / "app.cpp"
    info = f"""{workspace}/program: file format Mach-O arm64

.debug_info contents:
0x0000000b: DW_TAG_compile_unit
              DW_AT_name\t("app.cpp")
0x00000020:   DW_TAG_namespace
                DW_AT_name\t("geo")
0x00000025:     DW_TAG_structure_type
                  DW_AT_name\t("Box")
0x0000002a:       DW_TAG_subprogram
                    DW_AT_name\t("area")
                    DW_AT_decl_file\t("{source}")
                    DW_AT_decl_line\t(7)
0x00000030:       NULL
0x00000031:     NULL
0x00000032:   DW_TAG_subprogram
                DW_AT_low_pc\t(0x0000000100000400)
                DW_AT_high_pc\t(0x0000000100000440)
                DW_AT_specification\t(0x0000002a "area")
0x00000040:     DW_TAG_formal_parameter
                  DW_AT_name\t("this")
0x00000048:     DW_TAG_lexical_block
                  DW_AT_low_pc\t(0x0000000100000410)
                  DW_AT_high_pc\t(0x0000000100000430)
0x00000050:       DW_TAG_variable
                    DW_AT_name\t("product")
0x00000058:       NULL
0x00000059:     DW_TAG_variable
                  DW_AT_name\t("$discr$")
0x0000005a:     NULL
0x00000060:   DW_TAG_subprogram
                DW_AT_low_pc\t(0x0000000100000500)
                DW_AT_high_pc\t(0x0000000100000540)
                DW_AT_name\t("main")
                DW_AT_decl_file\t("{source}")
0x00000068:     DW_TAG_variable
                  DW_AT_name\t("total")
0x00000070:     DW_TAG_subprogram
                  DW_AT_low_pc\t(0x0000000100000520)
                  DW_AT_high_pc\t(0x0000000100000530)
                  DW_AT_name\t("{{closure#0}}")
                  DW_AT_decl_file\t("{source}")
0x00000078:       DW_TAG_formal_parameter
                    DW_AT_name\t("x")
0x00000080:       NULL
0x00000081:     NULL
0x00000090:   DW_TAG_subprogram
                DW_AT_low_pc\t(0x0000000100000600)
                DW_AT_high_pc\t(0x0000000100000640)
                DW_AT_name\t("printf")
                DW_AT_decl_file\t("/sdk/usr/include/stdio.h")
0x000000a0:   NULL
"""
    rows = [(0x400, 7), (0x410, 8), (0x430, 9), (0x500, 12), (0x520, 13), (0x530, 15), (0x600, 9)]
    line = 'debug_line[0x00000000]\nversion: 4\nfile_names[ 1]:\nname: "app.cpp"\ndir_index: 0\n'
    line += "Address Line Column File\n"
    base = 0x100000000
    line += "".join(
        f"0x{base + address:016x} {number} 1 1 0 0 is_stmt\n" for address, number in rows
    )
    line += f"0x{base + 0x700:016x} 15 1 1 0 0 end_sequence\n"
    return info, line


def test_function_scopes_list_qualified_names_spans_and_locals(tmp_path):
    from peeky.native import function_scopes

    (tmp_path / "app.cpp").write_text("\n" * 20)
    info, line = scope_dumps(tmp_path)
    scopes = function_scopes(info, line, tmp_path, ("app.cpp",))
    assert scopes == [
        # The out-of-line definition takes its name and file from its declaration;
        # nested blocks contribute locals, with their blocks' lines (#107);
        # `$`-synthesized storage does not.
        {
            "scope": "geo::Box::area",
            "source": "app.cpp",
            "lines": [7, 9],
            "names": ["product", "this"],
            "spans": {"product": [[8, 8]]},
        },
        {"scope": "main", "source": "app.cpp", "lines": [12, 15], "names": ["total"]},
        # A closure is qualified by its function; SDK functions are not listed.
        {"scope": "main::{closure#0}", "source": "app.cpp", "lines": [13, 13], "names": ["x"]},
    ]


def test_function_scopes_are_bounded(tmp_path, monkeypatch):
    import peeky.native as native

    (tmp_path / "app.cpp").write_text("\n" * 20)
    info, line = scope_dumps(tmp_path)
    monkeypatch.setattr(native, "MAX_SCOPES", 1)
    scopes = native.function_scopes(info, line, tmp_path, ("app.cpp",))
    assert scopes[-1] == {"omitted": 2} and len(scopes) == 2
    monkeypatch.setattr(native, "MAX_DIES", 3)
    with pytest.raises(ValueError, match="Too many debug-info entries"):
        native.function_scopes(info, line, tmp_path, ("app.cpp",))


def ownership_dumps(workspace):
    """main (lines 10-20) inlines helper (lines 2-4, same file) and a std function whose
    range also covers one of main's own rows; plus an O2-style out-of-line copy whose
    decl_file sits on its abstract origin, a tombstoned COMDAT copy, a range list, a
    non-ASCII name and a legacy-mangled Rust symbol."""
    source = workspace / "app.cpp"
    info = f""".debug_info contents:
0x0000000b: DW_TAG_compile_unit
              DW_AT_name\t("app.cpp")
0x00000010:   DW_TAG_subprogram
                DW_AT_name\t("helper")
                DW_AT_decl_file\t("{source}")
                DW_AT_decl_line\t(2)
                DW_AT_inline\t(DW_INL_inlined)
0x00000018:     DW_TAG_formal_parameter
                  DW_AT_name\t("x")
0x00000020:     NULL
0x00000030:   DW_TAG_subprogram
                DW_AT_low_pc\t(0x0000000100001000)
                DW_AT_high_pc\t(0x0000000100001100)
                DW_AT_name\t("main")
                DW_AT_decl_file\t("{source}")
                DW_AT_decl_line\t(10)
0x00000040:     DW_TAG_inlined_subroutine
                  DW_AT_abstract_origin\t(0x00000010 "helper")
                  DW_AT_ranges\t(0x00000000
                     [0x0000000100001010, 0x0000000100001020)
                     [0x0000000100001030, 0x0000000100001040))
0x00000050:       DW_TAG_formal_parameter
                    DW_AT_abstract_origin\t(0x00000018 "x")
0x00000058:       NULL
0x00000060:     DW_TAG_inlined_subroutine
                  DW_AT_abstract_origin\t(0x000000f0 "write_fmt")
                  DW_AT_low_pc\t(0x0000000100001050)
                  DW_AT_high_pc\t(0x0000000100001070)
0x00000068:     DW_TAG_variable
                  DW_AT_name\t("total")
0x00000070:     NULL
0x00000080:   DW_TAG_subprogram
                DW_AT_name\t("gr\\303\\266\\303\\237e")
                DW_AT_decl_file\t("{source}")
                DW_AT_decl_line\t(30)
0x00000088:   DW_TAG_subprogram
                DW_AT_abstract_origin\t(0x00000080 "gr\\303\\266\\303\\237e")
                DW_AT_low_pc\t(0x0000000100002000)
                DW_AT_high_pc\t(0x0000000100002010)
0x00000090:   DW_TAG_subprogram
                DW_AT_low_pc\t(0x0000000000000000)
                DW_AT_high_pc\t(0x0000000000000010)
                DW_AT_name\t("discarded")
                DW_AT_decl_file\t("{source}")
0x000000a0:   DW_TAG_subprogram
                DW_AT_low_pc\t(0x0000000100003000)
                DW_AT_high_pc\t(0x0000000100003010)
                DW_AT_linkage_name\t("_ZN5crate5Pager5pages17h0123456789abcdefE")
                DW_AT_name\t("pages")
                DW_AT_decl_file\t("{source}")
0x000000f0:   DW_TAG_subprogram
                DW_AT_name\t("write_fmt")
                DW_AT_decl_file\t("/sdk/core/fmt.rs")
0x000000f8:   NULL
"""
    rows = [
        (0x1000, 10),
        (0x1010, 3),
        (0x1020, 11),
        (0x1030, 4),
        (0x1040, 12),
        (0x1058, 13),
        (0x1070, 20),
        (0x2000, 31),
        (0x2008, 32),
        (0x3000, 40),
        (0x3008, 41),
        (0x0, 99),
    ]
    line = 'debug_line[0x00000000]\nversion: 4\nfile_names[ 1]:\nname: "app.cpp"\ndir_index: 0\n'
    line += "Address Line Column File\n"
    base = 0x100000000
    line += "".join(
        f"0x{(base + address) if address else 0:016x} {number} 1 1 0 0 is_stmt\n"
        for address, number in rows
    )
    line += f"0x{base + 0x4000:016x} 41 1 1 0 0 end_sequence\n"
    return info, line


def test_rows_belong_to_the_innermost_function_from_their_own_file(tmp_path):
    from peeky.native import function_scopes

    (tmp_path / "app.cpp").write_text("\n" * 100)
    info, line = ownership_dumps(tmp_path)
    scopes = {s["scope"]: s for s in function_scopes(info, line, tmp_path, ("app.cpp",))}
    # The inlined same-file callee takes its own lines; main keeps line 13 even though
    # an inlined std function's range covers it.
    assert scopes["helper"]["lines"] == [3, 4] and scopes["helper"]["names"] == ["x"]
    assert scopes["main"]["lines"] == [10, 20] and scopes["main"]["names"] == ["total"]
    # decl_file found on the abstract origin; octal-escaped UTF-8 names decoded.
    assert scopes["größe"]["lines"] == [31, 32]
    # Rust legacy symbols are named as LLDB names their frames.
    assert scopes["crate::Pager::pages"]["lines"] == [40, 41]
    # Tombstoned (address 0) copies and SDK functions are never listed.
    assert set(scopes) == {"helper", "main", "größe", "crate::Pager::pages"}


@pytest.mark.parametrize(
    "linkage, name",
    [
        ("_ZN16peeky_pagination4main17h0123456789abcdefE", "peeky_pagination::main"),
        (
            "_ZN16peeky_pagination5Pager5pages28_$u7b$$u7b$closure$u7d$$u7d$17h0123456789abcdefE",
            "peeky_pagination::Pager::pages::{{closure}}",
        ),
        (
            "_ZN62_$LT$peeky_pagination..Pager$u20$as$u20$core..fmt..Display$GT$3fmt"
            "17h0123456789abcdefE",
            "<peeky_pagination::Pager as core::fmt::Display>::fmt",
        ),
        ("_ZN3geo3Box4areaEv", None),
        ("_ZN5crate5Pager17h0123E", None),
        ("_ZN99short17h0123456789abcdefE", None),
        (None, None),
    ],
)
def test_rust_symbols_are_demangled_like_lldb_frames(linkage, name):
    from peeky.native import _rust_name

    assert _rust_name(linkage) == name


def test_identical_scopes_from_several_units_are_listed_once(tmp_path):
    from peeky.native import function_scopes

    (tmp_path / "app.cpp").write_text("\n" * 100)
    info, line = ownership_dumps(tmp_path)
    second = info.replace("0x000000", "0x000010").replace("(0x00000010 ", "(0x00001010 ")
    second = second.replace("(0x00000018 ", "(0x00001018 ").replace("(0x000000f0 ", "(0x000010f0 ")
    second = second.replace("(0x00000080 ", "(0x00001080 ")
    scopes = function_scopes(info + second, line, tmp_path, ("app.cpp",))
    assert len(scopes) == 4


@pytest.mark.parametrize(
    "linkage, scope",
    [
        ("_ZZ11apply_twiceiENKUliE_clEi", "apply_twice"),
        ("_ZZN3geo3Box4areaEvENKUlvE_clEv", "geo::Box::area"),
        ("_ZZNK3geo3Box4areaEvE1x", "geo::Box::area"),
        ("_ZZ4mainENK3$_0clEii", "main"),
        ("_ZN3geo3Box4areaEv", None),
        ("_ZZN3geoIiE", None),
        (None, None),
    ],
)
def test_local_entities_name_their_enclosing_function(linkage, scope):
    from peeky.native import _local_scope

    assert _local_scope(linkage) == scope


def test_lambdas_under_an_inlined_only_function_keep_its_name(tmp_path):
    from peeky.native import _dies, _qualified

    # clang parents the lambda class under an empty subprogram when the enclosing
    # function exists only inlined.
    info = """0x0000000b: DW_TAG_compile_unit
0x00000020:   DW_TAG_subprogram
0x00000028:     DW_TAG_class_type
0x00000030:       DW_TAG_subprogram
                    DW_AT_linkage_name\t("_ZZ11apply_twiceiENKUliE_clEi")
                    DW_AT_name\t("operator()")
0x00000038:       NULL
0x00000039:     NULL
0x0000003a:   NULL
"""
    assert _qualified(_dies(info), 0x30) == "apply_twice::operator()"


def declaration_dumps(workspace):
    """A C unit: `take` (lines 5-12) with a parameter, two automatics, a static and a
    local in a nested block; the static has an address, not a frame location."""
    source = workspace / "bucket.c"
    info = f"""{workspace}/program: file format Mach-O arm64

.debug_info contents:
0x0000000b: DW_TAG_compile_unit
              DW_AT_name\t("bucket.c")
0x00000020:   DW_TAG_subprogram
                DW_AT_low_pc\t(0x0000000100000400)
                DW_AT_high_pc\t(0x0000000100000480)
                DW_AT_name\t("take")
                DW_AT_decl_file\t("{source}")
                DW_AT_decl_line\t(5)
0x00000030:     DW_TAG_formal_parameter
                  DW_AT_location\t(DW_OP_fbreg -8)
                  DW_AT_name\t("now")
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(5)
0x00000038:     DW_TAG_variable
                  DW_AT_location\t(DW_OP_fbreg -16)
                  DW_AT_name\t("elapsed")
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(6)
0x00000040:     DW_TAG_variable
                  DW_AT_location\t(DW_OP_breg31 WSP+40)
                  DW_AT_name\t("refill")
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(8)
0x00000048:     DW_TAG_variable
                  DW_AT_location\t(DW_OP_addr 0x100008000)
                  DW_AT_name\t("calls")
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(9)
0x00000050:     DW_TAG_lexical_block
                  DW_AT_low_pc\t(0x0000000100000440)
                  DW_AT_high_pc\t(0x0000000100000460)
0x00000058:       DW_TAG_variable
                    DW_AT_location\t(DW_OP_fbreg -20)
                    DW_AT_name\t("now")
                    DW_AT_decl_file\t("{source}")
                    DW_AT_decl_line\t(11)
0x00000060:       NULL
0x00000061:     NULL
0x00000062:   NULL
"""
    rows = [(0x400, 5), (0x410, 6), (0x420, 7), (0x430, 8), (0x440, 11), (0x460, 12)]
    line = 'debug_line[0x00000000]\nversion: 4\nfile_names[ 1]:\nname: "bucket.c"\ndir_index: 0\n'
    line += "Address Line Column File\n"
    base = 0x100000000
    line += "".join(
        f"0x{base + address:016x} {number} 1 1 0 0 is_stmt\n" for address, number in rows
    )
    line += f"0x{base + 0x480:016x} 12 1 1 0 0 end_sequence\n"
    return info, line


def test_local_declarations_record_where_and_how_each_local_is_declared(tmp_path):
    from peeky.native import local_declarations

    (tmp_path / "bucket.c").write_text("\n" * 20)
    info, line = declaration_dumps(tmp_path)
    [take] = local_declarations(info, line, tmp_path, ("bucket.c",))
    base = 0x100000000
    assert take["source"] == "bucket.c" and take["jumps"] is False
    assert take["rows"] == {
        ("bucket.c", number): (base + address, base + address)
        for address, number in [
            (0x400, 5),
            (0x410, 6),
            (0x420, 7),
            (0x430, 8),
            (0x440, 11),
            (0x460, 12),
        ]
    }
    assert take["locals"] == {
        "now": {(None, None, False), ("bucket.c", 11, True)},
        "elapsed": {("bucket.c", 6, True)},
        "refill": {("bucket.c", 8, True)},
        "calls": {("bucket.c", 9, False)},
    }


@pytest.mark.parametrize(
    "name, line, pc, below",
    [
        ("refill", 7, 0x420, (8, None)),  # #74: declared on the next line: leftovers here
        ("refill", 8, 0x430, None),  # its own line: a for-header counter is live there
        ("elapsed", 7, 0x420, None),
        ("calls", 7, 0x420, None),  # a static (listed among globals) has its value
        # Outside the block declaring the other `now`: the parameter is the one shown.
        ("now", 7, 0x420, None),
        # lldb-dap names each shadowed local; out of its block, which one is shown
        # cannot be told: withheld.
        ("now @ bucket.c:11", 7, 0x420, (11, "unknown")),
        ("now @ bucket.c:5", 7, 0x420, None),
        ("missing", 7, 0x420, None),
        ("refill", 13, 0x500, None),  # outside the function
        ("refill", 7, None, (8, "unknown")),  # the stop's address unknown
    ],
)
def test_a_local_is_withheld_when_the_declaration_shown_is_below(tmp_path, name, line, pc, below):
    from peeky.native import declared_below, local_declarations

    (tmp_path / "bucket.c").write_text("\n" * 20)
    info, dump = declaration_dumps(tmp_path)
    table = local_declarations(info, dump, tmp_path, ("bucket.c",))
    pc = None if pc is None else 0x100000000 + pc
    assert declared_below(table, name, "bucket.c", line, pc=pc) == below
    assert declared_below(table, name, "other.c", line, pc=pc) is None


def rows(spans, source="a.cpp"):
    return {(source, line): span for line, span in spans.items()}


def test_only_functions_owning_the_line_count_and_jumps_disable_withholding():
    from peeky.native import declared_below

    # Review of #81: a local class's constructor owns its member initializer on line 5,
    # above its own declaration; `main`, which defines the class, declares `v` on line 12.
    ctor = {"source": "a.cpp", "rows": rows({5: (50, 50), 7: (70, 70), 8: (80, 80)})}
    ctor.update(locals={"v": {(None, None, False)}}, jumps=False, ranges=((50, 90),))
    ctor["visible"] = {("v", None, None, False): ((50, 90),)}
    main = {"source": "a.cpp", "rows": rows({3: (3, 3), 11: (11, 11), 12: (12, 12)})}
    main.update(jumps=False, ranges=((1, 20),))
    main["locals"] = {"v": {("a.cpp", 12, True)}}
    main["visible"] = {("v", "a.cpp", 12, True): ((1, 20),)}
    # Stopped in the constructor, its parameter is the `v` shown.
    assert declared_below([ctor, main], "v", "a.cpp", 5, pc=50) is None
    assert declared_below([ctor, main], "v", "a.cpp", 11, pc=11) == (12, None)
    # A backward goto can return above the declaration with `v` live, or this is the
    # first pass: unproven, and withheld all the same (sixth agent evaluation).
    jumps = {**main, "jumps": True}
    assert declared_below([ctor, jumps], "v", "a.cpp", 11, pc=11) == (12, "label")


def test_only_the_declaration_the_debugger_shows_is_judged():
    from peeky.native import declared_below, declared_on_line

    # Review of PR #140: an earlier loop's `qty` (lines 11-14), a static `qty` clang
    # declares in the function's scope, and a lambda's parameter `b` each kept the
    # local declared below the stop shown with leftovers, and settable.
    main = {"source": "a.c", "jumps": False, "ranges": ((100, 200),)}
    main["rows"] = rows({12: (110, 112), 16: (130, 134), 17: (140, 144)}, "a.c")
    main["locals"] = {
        "qty": {("a.c", 12, True), ("a.c", 17, True), ("a.c", 5, False)},
        "b": {("a.c", 17, True)},
    }
    main["visible"] = {
        ("qty", "a.c", 12, True): ((108, 120),),  # the loop's body
        ("qty", "a.c", 17, True): ((100, 200),),
        ("qty", "a.c", 5, False): ((100, 200),),  # a static: listed among globals
        ("b", "a.c", 17, True): ((100, 200),),
    }
    main["starts"] = {("qty", "a.c", 17): {140}}
    sort = {"source": "a.c", "jumps": False, "ranges": ((300, 320),)}
    sort.update(rows=rows({16: (300, 310)}, "a.c"), locals={"b": {(None, None, False)}})
    sort["visible"] = {("b", None, None, False): ((300, 320),)}
    table = [main, sort]
    assert declared_below(table, "qty", "a.c", 16, pc=130) == (17, None)
    assert declared_below(table, "b", "a.c", 16, pc=130) == (17, None)
    assert declared_on_line(table, "qty", "a.c", 17, 140) == (17, None)
    # In the loop's body, its own `qty` is the one shown; in the lambda, its parameter.
    assert declared_below(table, "qty", "a.c", 12, pc=110) is None
    assert declared_below(table, "b", "a.c", 16, pc=305) is None
    # Two of the name in one block, or a block not known: whichever is shown, withheld.
    twice = {**main, "visible": {**main["visible"], ("qty", "a.c", 12, True): ((100, 200),)}}
    assert declared_below([twice], "qty", "a.c", 16, pc=130) == (17, "unknown")
    assert declared_on_line([twice], "qty", "a.c", 17, 140) == (17, "unknown")
    unknown = {**main, "visible": {}}
    assert declared_below([unknown], "qty", "a.c", 16, pc=130) == (17, "unknown")


@pytest.mark.parametrize(
    "rows, address, below",
    [
        ({7: (10, 12), 8: (20, 24)}, None, (8, None)),  # line 7's code all before line 8's
        # Review of #81: clang runs a struct's default member initializer in its caller,
        # on the struct's line, inside the code of a later line: a stop there may be
        # inside the declaration, so unproven (and withheld).
        ({7: (30, 32), 8: (20, 24)}, None, (8, "after")),
        ({7: (30, 32), 8: (20, 24)}, 30, (8, "after")),
        # GCC runs a `cleanup` at its variable's line when the block ends, and clang an
        # exception handler destroying a temporary (sixth agent evaluation: `std::string
        # name = trim_left(entry.substr(0, colon));`): line 7 then has code after line
        # 8's. A stop before line 8's code is proven above it; one after is not.
        ({7: (10, 40), 8: (20, 24)}, None, (8, "after")),
        ({7: (10, 40), 8: (20, 24)}, 10, (8, None)),
        ({7: (10, 40), 8: (20, 24)}, 40, (8, "after")),
        ({7: (10, 40), 8: (20, 24)}, 99, (8, "unknown")),  # not in the function's code
        ({7: (10, 12)}, None, (8, "no code")),  # a declaration without code proves nothing
    ],
)
def test_a_local_declared_below_is_proven_so_only_before_its_declarations_code(
    rows, address, below
):
    from peeky.native import declared_below

    function = {"source": "a.c", "rows": {("a.c", n): span for n, span in rows.items()}}
    function.update(locals={"later": {("a.c", 8, True)}}, jumps=False, ranges=((0, 50),))
    function["visible"] = {("later", "a.c", 8, True): ((0, 50),)}
    pc = rows[7][0] if address is None else address
    assert declared_below([function], "later", "a.c", 7, address=address, pc=pc) == below


def test_a_function_with_a_label_can_jump_back(tmp_path):
    from peeky.native import local_declarations

    info, line = declaration_dumps(tmp_path)
    info = info.replace(
        "0x00000050:     DW_TAG_lexical_block",
        '0x0000004c:     DW_TAG_label\n                  DW_AT_name\t("again")\n'
        "0x00000050:     DW_TAG_lexical_block",
    )
    (tmp_path / "bucket.c").write_text("\n" * 20)
    [take] = local_declarations(info, line, tmp_path, ("bucket.c",))
    assert take["jumps"] is True


@pytest.mark.parametrize(
    "contents, found",
    [
        (b"\x00_setjmp\x00", True),
        (b"\x00___sigsetjmp\x00", True),
        (b"x" * (1 << 20) + b"swapcontext", True),  # across a read boundary
        (b"x" * ((1 << 20) - 3) + b"getcontext", True),
        (b"\x00_printf\x00_malloc\x00", False),
    ],
)
def test_nonlocal_jumps_are_found_in_the_executable(tmp_path, contents, found):
    from peeky.native import has_nonlocal_jumps

    # Review of #81: `setjmp` behind a TRY macro from a header never appears in the
    # function's lines, but the executable imports it.
    binary = tmp_path / "program"
    binary.write_bytes(contents)
    assert has_nonlocal_jumps(binary) is found


def test_a_function_running_code_on_another_files_line_is_among_its_owners():
    from peeky.native import declared_below

    # Review of #81: clang runs a header struct's member initializer (geo.hpp:2) inside
    # main, which declares `x` on main.cpp:5; origin_sum in geo.hpp declares its own `x`
    # on geo.hpp:5.
    main = {"source": "main.cpp", "jumps": False, "locals": {"x": {("main.cpp", 5, True)}}}
    main["rows"] = {("main.cpp", 5): (10, 12), ("geo.hpp", 2): (20, 22), ("main.cpp", 6): (14, 24)}
    main.update(ranges=((10, 30),), visible={("x", "main.cpp", 5, True): ((10, 30),)})
    origin = {"source": "geo.hpp", "jumps": False, "locals": {"x": {("geo.hpp", 5, True)}}}
    origin["rows"] = {("geo.hpp", 2): (40, 42), ("geo.hpp", 5): (50, 52)}
    origin.update(ranges=((40, 60),), visible={("x", "geo.hpp", 5, True): ((40, 60),)})
    # Stopped in main, main's `x` is the one shown; in origin_sum, its own.
    assert declared_below([main, origin], "x", "geo.hpp", 2, pc=20) is None
    assert declared_below([main, origin], "x", "geo.hpp", 2, pc=40) == (5, None)
    # Declared on the stop line itself: never withheld (a `for` counter is live there
    # at a later pass).
    loop = {"source": "a.c", "jumps": False, "locals": {"i": {("a.c", 3, True)}}}
    loop["rows"] = {("a.c", 3): (10, 12), ("a.c", 4): (20, 30)}
    loop.update(ranges=((10, 30),), visible={("i", "a.c", 3, True): ((10, 30),)})
    assert declared_below([loop], "i", "a.c", 3, pc=10) is None
    assert declared_below([loop], "i", "a.c", 3) is None


def test_a_caller_frame_inside_a_locals_declaration_statement_withholds_it():
    from peeky.c_source import Statements
    from peeky.native import declared_unfinished

    # Review of #104 (round 3): line 21's own code (`t.x = seed`) all runs before the call
    # on line 22, and `t.p` is stored after the calls on lines 22-23 return.
    statements = Statements(
        "\n" * 20 + "struct trio t = { seed,\n    { twice(3),\n      twice(4) } };\nlong k = 1;\n"
    )
    run = {"source": "a.c", "jumps": False, "ranges": [(0, 1000)]}
    run["rows"] = rows({21: (100, 108), 22: (110, 130), 23: (134, 150), 24: (150, 160)}, "a.c")
    run["locals"] = {"t": {("a.c", 21, True)}}
    for line, address in ((22, 116), (23, 140)):
        assert declared_unfinished([run], "t", "a.c", line, address) is None
        found = declared_unfinished([run], "t", "a.c", line, address, ends=statements.end)
        assert found == (21, "statement", 23)
    assert declared_unfinished([run], "t", "a.c", 24, 156, ends=statements.end) is None
    # A statement end the source does not give: unknown means withheld.
    unknown = declared_unfinished([run], "t", "a.c", 24, 156, ends=lambda at, name: None)
    assert unknown == (21, "statement", None)


def test_a_caller_frame_in_another_file_than_a_locals_declaration_withholds_it():
    from peeky.native import declared_unfinished

    # Review of #104 (round 4): `struct pair ps[2] = {` on inc.c:18 takes its rows from
    # `#include "table.h"`; the frame is at table.h:2. A declaration in an included file
    # with the frame in the including file is the same.
    run = {"source": "inc.c", "jumps": False, "ranges": [(0, 1000)]}
    run["rows"] = {("inc.c", 18): (100, 104), ("table.h", 2): (110, 140), ("inc.c", 20): (140, 150)}
    run["locals"] = {
        "ps": {("inc.c", 18, True)},
        "q": {("decl.h", 1, True)},
        "r": {(None, 3, True)},
    }
    assert declared_unfinished([run], "ps", "table.h", 2, 120) == (18, "file", "inc.c")
    assert declared_unfinished([run], "q", "inc.c", 20, 144) == (1, "file", "decl.h")
    # Declared outside the program's sources.
    assert declared_unfinished([run], "r", "inc.c", 20, 144) == (3, "file", None)
    assert declared_unfinished([run], "ps", "inc.c", 20, 144) is None


def test_sources_naming_a_nonlocal_jump_are_found(tmp_path):
    from peeky.native import mentions_nonlocal_jumps

    plain, builtin = tmp_path / "a.c", tmp_path / "b.h"
    plain.write_text("int main(void) { return 0; }\n")
    builtin.write_text("#define TRY if (__builtin_setjmp(buf) == 0)\n")
    assert not mentions_nonlocal_jumps([plain])
    assert mentions_nonlocal_jumps([plain, builtin])


def test_an_external_header_hiding_the_builtin_is_found(tmp_path):
    from peeky.native import mentions_nonlocal_jumps

    # Review of #81: `/usr/local/include/sjlj_try.h` defines TRY with __builtin_setjmp;
    # the SDK's setjmp.h only names `setjmp`, which the executable check covers.
    header = tmp_path / "sjlj_try.h"
    header.write_text("#define TRY if (__builtin_setjmp(sjlj_env) == 0)\n")
    sdk = tmp_path / "setjmp.h"
    sdk.write_text("int setjmp(jmp_buf);\n")
    assert mentions_nonlocal_jumps({str(header): "sha"}, (b"__builtin_setjmp",))
    assert not mentions_nonlocal_jumps({str(sdk): "sha"}, (b"__builtin_setjmp",))


def test_a_local_declared_on_the_stop_line_is_withheld_only_where_its_line_starts():
    from peeky.native import declared_on_line

    # #92: line 8 runs from 0x80 to 0x9f; `i` is declared there, `total` on line 7.
    # `starts`: where control enters line 8 in the block declaring each local (the
    # function's block for `i`, a nested `{ int sq = ...; }` block from 0x90 for `sq`).
    main = {
        "source": "a.c",
        "rows": rows({7: (0x70, 0x7F), 8: (0x80, 0x9F)}, "a.c"),
        "jumps": False,
        "ranges": ((0x70, 0xA0),),
    }
    main["locals"] = {
        "i": {("a.c", 8, True)},
        "total": {("a.c", 7, True)},
        "sq": {("a.c", 8, True)},
    }
    main["visible"] = {
        ("i", "a.c", 8, True): ((0x70, 0xA0),),
        ("total", "a.c", 7, True): ((0x70, 0xA0),),
        ("sq", "a.c", 8, True): ((0x90, 0xA0),),
        ("i", "a.c", 8, False): ((0x70, 0xA0),),
        ("i", "a.c", 3, True): ((0x60, 0x70),),
    }
    main["starts"] = {("i", "a.c", 8): {0x80}, ("sq", "a.c", 8): {0x90}}
    assert declared_on_line([main], "i", "a.c", 8, 0x80) == (8, None)
    assert declared_on_line([main], "sq", "a.c", 8, 0x90) == (8, None)
    # Mid-line (a return from a call, a loop's later part), the local is live.
    assert declared_on_line([main], "i", "a.c", 8, 0x88) is None
    assert declared_on_line([main], "i", "a.c", 8, 0x90) is None
    assert declared_on_line([main], "total", "a.c", 8, 0x80) is None
    # An address outside the line (a wrong load bias) proves nothing.
    assert declared_on_line([main], "i", "a.c", 8, 0x70) is None
    # With a label, a `goto` may have returned here with `i` live, or this is its first
    # pass: withheld all the same (sixth agent evaluation).
    assert declared_on_line([{**main, "jumps": True}], "i", "a.c", 8, 0x80) == (8, "label")
    # A same-named static, or another `i` declared elsewhere in the function.
    static = {**main, "locals": {"i": {("a.c", 8, False)}}}
    assert declared_on_line([static], "i", "a.c", 8, 0x80) is None
    # Another `i`, in a block out of scope here: the one shown is this line's.
    twice = {**main, "locals": {"i": {("a.c", 8, True), ("a.c", 3, True)}}}
    assert declared_on_line([twice], "i", "a.c", 8, 0x80) == (8, None)
    assert declared_on_line([twice], "i @ a.c:8", "a.c", 8, 0x80) == (8, None)


def _instructions(*pairs):
    return [{"address": f"0x{address:X}", "instruction": text} for address, text in pairs]


def test_backward_jump_targets_come_from_the_disassembly():
    from peeky.native import backward_targets

    # Review of #99: x86-64 at -O0 tests `while ((x = next()) > 0)` first, at the line's
    # lowest address, and jumps back there: control re-enters it with `x` live.
    x86 = _instructions(
        (0x1130, "pushq  %rbp"),
        (0x1136, "callq  0x1120                     ; next at t.c:3"),
        (0x113B, "movl   %eax, -0x8(%rbp)"),
        (0x113E, "cmpl   $0x0, %eax"),
        (0x1141, "jle    0x1152"),
        (0x1147, "addl   -0x8(%rbp), %ecx"),
        (0x114D, "jmp    0x1136                     ; <+6> at t.c:5"),
        (0x1152, "popq   %rbp"),
        (0x1153, "retq"),
        (0x1154, "nop"),
    )
    assert backward_targets(x86, 0x1130, 0x1154, "x86_64") == {0x1136}
    intel = _instructions((0x1130, "jmp 0x1130"), (0x1132, "jne 0x1140"), (0x1140, "ret"))
    assert backward_targets(intel, 0x1130, 0x1141, "x86_64") == {0x1130}
    arm = _instructions(
        (0x10000050C, "b       0x100000510               ; <+24> at a.c:6:21"),
        (0x100000510, "bl      0x1000005b0               ; next at a.c:3"),
        (0x100000514, "tbz     w8, #0x1f, 0x100000520"),
        (0x100000518, "b.gt    0x100000510"),
        (0x10000051C, "cbnz    w9, 0x100000528"),
        (0x100000520, "add     w8, w8, #0x1"),
        (0x100000524, "ret"),
    )
    assert backward_targets(arm, 0x10000050C, 0x10000052C, "arm64") == {0x100000510}
    # No jumps: no loop (calls and returns are not jumps).
    straight = _instructions((0x10, "bl      0x100"), (0x14, "ret"))
    assert backward_targets(straight, 0x10, 0x18, "arm64") == set()
    # A jump through a register (a jump table) or out of the function, or another
    # architecture: unknown, so nothing is withheld.
    assert backward_targets(_instructions((0x10, "br      x8")), 0x10, 0x14, "arm64") is None
    assert backward_targets(_instructions((0x10, "jmpq   *%rax")), 0x10, 0x14, "x86_64") is None
    assert backward_targets(_instructions((0x10, "b       0x900")), 0x10, 0x14, "arm64") is None
    assert backward_targets(straight, 0x10, 0x18, "riscv64") is None
    assert backward_targets([{"address": "nope"}], 0x10, 0x18, "arm64") is None


def test_architecture_reads_mach_o_and_elf_headers(tmp_path):
    import struct

    from peeky.native import architecture

    cases = {
        "arm": struct.pack("<II", 0xFEEDFACF, 0x0100000C) + bytes(12),
        "intel": struct.pack("<II", 0xFEEDFACF, 0x01000007) + bytes(12),
        "elf-x86": b"\x7fELF\x02\x01" + bytes(12) + struct.pack("<H", 62),
        "elf-arm": b"\x7fELF\x02\x01" + bytes(12) + struct.pack("<H", 183),
        "riscv": b"\x7fELF\x02\x01" + bytes(12) + struct.pack("<H", 243),
        "text": b"#!/bin/sh\n",
    }
    for name, header in cases.items():
        (tmp_path / name).write_bytes(header)
    found = {name: architecture(tmp_path / name) for name in cases}
    assert found == {
        "arm": "arm64",
        "intel": "x86_64",
        "elf-x86": "x86_64",
        "elf-arm": "arm64",
        "riscv": None,
        "text": None,
    }


def test_the_load_bias_reads_each_debuggers_module_address(tmp_path, monkeypatch):
    import peeky.native as native

    monkeypatch.setattr(native, "macho_text_address", lambda binary: 0x100000000)
    monkeypatch.setattr(native, "elf_executable_ranges", lambda binary: (0, ()))
    monkeypatch.setattr(native.sys, "platform", "darwin")
    # Apple's lldb-dap: hex.
    assert native.load_bias("p", {"addressRange": "0x10457c000"}) == 0x457C000
    monkeypatch.setattr(native.sys, "platform", "linux")
    # LLVM 18: decimal; LLVM 20: hex.
    assert native.load_bias("p", {"addressRange": "93824992231424"}) == 93824992231424
    assert native.load_bias("p", {"addressRange": "0x555555554000"}) == 0x555555554000
    for bad in (None, "", "0x", "12ab", "-5"):
        with pytest.raises(ValueError):
            native.load_bias("p", {"addressRange": bad})


def test_macho_text_address_reads_the_header_segment(tmp_path):
    import struct

    from peeky.native import macho_text_address

    def segment(name, address, offset):
        body = struct.pack("<16sQQQQiiII", name, address, 0x4000, offset, 0x4000, 5, 5, 0, 0)
        return struct.pack("<II", 0x19, 8 + len(body)) + body

    commands = segment(b"__PAGEZERO", 0, 0) + segment(b"__TEXT", 0x100000000, 0)
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, 0, 0, 2, 2, len(commands), 0, 0)
    (tmp_path / "p").write_bytes(header + commands)
    assert macho_text_address(tmp_path / "p") == 0x100000000
    (tmp_path / "q").write_bytes(header[:4] + b"\0" * 28)
    with pytest.raises(ValueError):
        macho_text_address(tmp_path / "q")


def test_a_non_statement_row_another_row_replaces_runs_no_code(tmp_path):
    # Review 3 of #107: GCC -O2 writes a row that is not a statement at the address of
    # the next row (line 2 under line 3); LLDB keeps the later row there. A statement
    # row, or one with code of its own, stays.
    (tmp_path / "main.c").write_text("a\nb\nc\nd\ne\nf\n")
    rows = (
        (0x1000, 1, " is_stmt"),
        (0x1004, 2, ""),
        (0x1004, 3, " is_stmt"),
        (0x1008, 4, " is_stmt"),
        (0x1008, 3, " is_stmt"),
        (0x100C, 6, ""),
        (0x1010, 5, ""),
        (0x1010, 5, " end_sequence"),
    )
    text = line_dump().split("0x0000000000401000")[0] + "".join(
        f"0x{0x401000 + address:016x} {number} 1 1 0 0{flags}\n" for address, number, flags in rows
    )
    assert parse_lines(text, tmp_path, ("main.c",)) == {"main.c": [1, 3, 4, 6]}


@pytest.mark.parametrize(
    "text, lines",
    [
        ("fn f() {}\n", set()),
        ('fn f() {}\nmacro_rules! m {\n    () => { "}" };\n}\nfn g() {}\n', {2, 3, 4}),
        ("macro_rules! m { ('{') => {} }\n// macro_rules! n {\nfn g<'a>(x: &'a u8) {}\n", {1}),
        ("macro_rules! m (\n /* ) */ () => (1)\n);\n", {1, 2, 3}),
        ('macro_rules! m { () => { r#"}"# } }\nfn g() {}\n', {1}),
        ('let s = "macro_rules! m {";\n', set()),
        # A C raw string (`cr"..."`) ends at its quote: a backslash escapes nothing.
        ('macro_rules! m { () => { cr"C:\\" } }\nfn g() {}\n', {1}),
        ("macro_rules! m {\n () => {}\n", None),
        ("macro_rules! m { () => {} }\n/* unclosed\n", None),
    ],
)
def test_a_rust_sources_macro_definitions_are_told_by_their_brackets(text, lines):
    from peeky.native import _macro_rules_lines

    assert _macro_rules_lines(text) == lines


def test_a_statement_row_another_row_replaces_is_kept_for_breakpoints_but_noted(tmp_path):
    # Review 4 of #107: LLDB keeps the later row at an address, so a statement row the
    # next row replaces runs no code; its line stays a breakpoint line.
    from peeky.native import _line_rows

    (tmp_path / "main.c").write_text("a\nb\nc\nd\n")
    rows = (
        (0x1000, 1, 1),
        (0x1000, 2, 1),  # replaces line 1
        (0x1004, 3, 1),
        (0x1004, 3, 1),  # the same line again: nothing replaced
        (0x100C, 4, 1),
        (0x100C, 7, 9),  # an undeclared header's row replaces line 4
    )
    header = 'dir_index: 0\nfile_names[ 9]:\nname: "/usr/include/stdio.h"\ndir_index: 0\n'
    dump = line_dump().split("0x0000000000401000")[0].replace("dir_index: 0\n", header, 1)
    text = dump + "".join(
        f"0x{0x400000 + address:016x} {number} 1 {file} 0 0 is_stmt\n"
        for address, number, file in rows
    )
    replaced = set()
    found = _line_rows(text, tmp_path, ("main.c",), replaced=replaced)
    assert [row[2] for row in found] == [1, 2, 3, 3, 4]
    assert replaced == {(0x401000, "main.c", 1), (0x40100C, "main.c", 4)}


def test_of_the_rows_at_one_address_the_last_is_the_one_that_runs(tmp_path):
    # Review 5 of #107: GCC gives one address runs of rows; only the last runs. A row
    # of the last row's line is not replaced, whatever comes between.
    from peeky.native import _line_rows

    (tmp_path / "main.c").write_text("x\n" * 40)
    rows = (
        (0x1000, 25, True),
        (0x1000, 25, False),
        (0x1000, 28, True),
        (0x1000, 29, True),
        (0x1000, 29, False),
        (0x1000, 30, False),  # runs
        (0x1010, 38, True),
        (0x1010, 39, True),
        (0x1010, 38, False),  # runs: line 38 stays
        (0x1020, 40, True),
        (0x1020, 40, False, "end_sequence"),
    )
    text = line_dump().split("0x0000000000401000")[0] + "".join(
        f"0x{0x400000 + row[0]:016x} {row[1]} 1 1 0 0"
        + (" is_stmt" if row[2] else "")
        + "".join(f" {flag}" for flag in row[3:])
        + "\n"
        for row in rows
    )
    replaced = set()
    found = _line_rows(text, tmp_path, ("main.c",), replaced=replaced)
    assert sorted({row[2] for row in found}) == [25, 28, 29, 30, 38, 39, 40]
    assert {line for _, _, line in replaced} == {25, 28, 29, 39, 40}
    assert (0x401010, "main.c", 38) not in replaced
    # Review 6: a line-0 row (no source line) is never a line, wherever it is.
    rows = ((0x1000, 5, True), (0x1010, 0, True), (0x1010, 7, True), (0x1020, 8, True))
    text = line_dump().split("0x0000000000401000")[0] + "".join(
        f"0x{0x400000 + address:016x} {number} 1 1 0 0 is_stmt\n" for address, number, _ in rows
    )
    assert sorted({row[2] for row in _line_rows(text, tmp_path, ("main.c",))}) == [5, 7, 8]


# #121: the frame stopped in.


def _aggregate_run():
    """`long head` (16); `struct outer o = {` (17, no code) to `};` (22); a local `q` of
    a block (lines 3-4) the stop is not in; code of lines 16, 19, 20 and 23."""
    run = {"source": "a.c", "jumps": False, "ranges": [(0, 1000)]}
    run["rows"] = rows({16: (90, 96), 19: (100, 110), 20: (110, 120), 23: (130, 140)}, "a.c")
    run["locals"] = {
        "o": {("a.c", 17, True)},
        "head": {("a.c", 16, True)},
        "q": {("a.c", 3, True)},
        "seed": {(None, None, False)},
    }
    run["scopes"] = {("q", "a.c", 3): {(500, 600)}, ("o", "a.c", 17): {(0, 1000)}}
    return run


def _through(ends):
    def through(source, at, name):
        return ends.get((source, at))

    return through


def test_a_stop_inside_a_declaration_statement_withholds_its_local():
    from peeky.native import declared_running

    run = _aggregate_run()
    through = _through({("a.c", 17): 22, ("a.c", 16): 16})
    assert declared_running([run], "o", "a.c", 20, 112, through) == (17, 22, "a.c", None)
    assert declared_running([run], "o", "a.c", 23, 132, through) is None
    assert declared_running([run], "head", "a.c", 20, 112, through) is None
    assert declared_running([run], "seed", "a.c", 20, 112, through) is None
    # An end the source does not give withholds it, but only for a block holding the stop.
    unknown = _through({("a.c", 16): 16})
    assert declared_running([run], "o", "a.c", 23, 132, unknown) == (17, None, "a.c", None)
    assert declared_running([run], "q", "a.c", 23, 132, unknown) is None
    assert declared_running([run], "q", "a.c", 23, None, unknown) == (3, None, "a.c", None)
    # Declared in another file: its `#include` in the stop's file stands for it
    # (review of #126, round 2); none found, or an end the source does not give, withholds.
    run["rows"][("t.c", 9)] = (114, 116)

    def site(source, declared_in):
        return 5

    assert declared_running([run], "o", "t.c", 9, 115, through) == (17, None, "a.c", None)
    assert declared_running([run], "o", "t.c", 9, 115, through, site=site) is None
    assert declared_running([run], "o", "t.c", 9, 115, unknown, site=site) == (17, None, "a.c", 5)
    # The `#include` at or below the stop: declared below it.
    assert declared_running([run], "o", "t.c", 5, 115, through, site=site) == (17, None, "a.c", 5)


def test_a_local_a_jump_can_skip_is_withheld_below_the_label():
    from peeky.native import declared_skipped

    run = _aggregate_run()
    labels = {17: 21, 16: False}

    def skipped(source, at, name, line, span):
        return labels.get(at)

    assert declared_skipped([run], "o", "a.c", 23, 132, skipped) == (17, 21)
    assert declared_skipped([run], "head", "a.c", 23, 132, skipped) is None
    # Unknown: withheld.
    assert declared_skipped([run], "o", "a.c", 23, 132, lambda *_: None) == (17, None)
    # Declared below the stop, or on its line: other rules.
    assert declared_skipped([run], "o", "a.c", 17, 99, skipped) is None


def test_a_declaration_line_without_code_is_placed_by_its_statements_code():
    from peeky.native import declared_below

    # #121: `struct pair ps[2] = {` (25) has no code; its elements' lines (26, 27) do.
    run = {"source": "a.c", "jumps": False, "ranges": ((100, 160),)}
    run["rows"] = rows({20: (100, 110), 26: (120, 130), 27: (130, 140), 15: (140, 150)}, "a.c")
    run["locals"] = {"ps": {("a.c", 25, True)}}
    run["visible"] = {("ps", "a.c", 25, True): ((100, 160),)}
    through = _through({("a.c", 25): 28})
    # Without its statement's end (a caller frame), unproven: withheld all the same.
    assert declared_below([run], "ps", "a.c", 20, pc=100) == (25, "no code")
    assert declared_below([run], "ps", "a.c", 20, through=through, pc=100) == (25, None)
    # Code of the stop's line after the statement's (a member initializer run there).
    assert declared_below([run], "ps", "a.c", 15, through=through, pc=140) == (25, "after")
    # Its end unknown: unproven (review of PR #140), withheld all the same.
    assert declared_below([run], "ps", "a.c", 20, through=_through({}), pc=100) == (25, "no code")


def test_constructors_and_lambdas_are_told_from_their_debug_information():
    from peeky.native import _CONSTRUCTOR_NAME, _class_role

    def die(tag, parent, **attrs):
        return {"tag": tag, "parent": parent, "attrs": attrs, "children": []}

    dies = {
        1: die("DW_TAG_compile_unit", None),
        2: die("DW_TAG_structure_type", 1, DW_AT_name='"Box<int>"'),
        3: die("DW_TAG_subprogram", 2, DW_AT_name='"Box"'),
        4: die("DW_TAG_subprogram", 1, DW_AT_specification='0x00000003 "Box"'),
        5: die("DW_TAG_subprogram", 2, DW_AT_name='"size"'),
        6: die("DW_TAG_subprogram", 1, DW_AT_name='"run"'),
        7: die("DW_TAG_class_type", 6, DW_AT_decl_line="22"),
        8: die("DW_TAG_subprogram", 7, DW_AT_name='"operator()"'),
        9: die("DW_TAG_subprogram", 1, DW_AT_specification='0x00000008 "operator()"'),
    }
    assert _class_role(dies, [4, 3]) == (True, None, None, False)
    assert _class_role(dies, [5]) == (False, None, None, False)
    assert _class_role(dies, [6]) == (False, None, None, False)
    assert _class_role(dies, [9, 8]) == (False, 6, 22, False)
    for name in ("Widget::Widget(long)", "ns::Box<int>::Box(int)", "A::B::B<int>(int)"):
        assert _CONSTRUCTOR_NAME.search(name), name
    for name in ("Widget::size() const", "run(long)::$_0::operator()() const", "Box(int)"):
        assert not _CONSTRUCTOR_NAME.search(name), name


# Review of #126 (round 1).


def test_a_local_declared_on_the_line_a_call_returns_to_is_withheld_part_way():
    from peeky.native import declared_mid_line

    # `long r = twice(3) + twice(4);` on line 17: rows 100-130, line 18 from 130.
    run = {"source": "a.c", "jumps": False, "ranges": [(0, 1000)]}
    run["rows"] = rows({17: (100, 124), 18: (130, 140)}, "a.c")
    run["locals"] = {"r": {("a.c", 17, True)}, "i": {("a.c", 17, True)}}

    def header(source, line, name, span):
        return name == "i"

    assert declared_mid_line([run], "r", "a.c", 17, 112, header) == 17
    # Its last row runs to line 18's code.
    assert declared_mid_line([run], "r", "a.c", 17, 126, header) == 17
    # At the line's start (other rules), or once its code has run.
    assert declared_mid_line([run], "r", "a.c", 17, 100, header) is None
    assert declared_mid_line([run], "r", "a.c", 18, 132, header) is None
    # A header's variable (a one-line loop's counter) is live mid-line.
    assert declared_mid_line([run], "i", "a.c", 17, 112, header) is None
    assert declared_mid_line([run], "r", "a.c", 17, None, header) is None


def test_a_local_whose_file_may_include_code_after_it_is_withheld_there():
    from peeky.native import declared_running

    run = _aggregate_run()
    run["rows"][("cases.h", 3)] = (114, 116)
    through = _through({("a.c", 16): 16})

    def included(source, at, name):
        return True

    def site(source, declared_in):
        return 2

    assert declared_running([run], "head", "cases.h", 3, 115, through, site=site) is None
    assert declared_running([run], "head", "cases.h", 3, 115, through, included, site) == (
        16,
        None,
        "a.c",
        2,
    )
    assert (
        declared_running([run], "head", "cases.h", 3, 115, through, lambda *_: False, site) is None
    )


def test_local_classes_and_closures_are_told_from_the_debug_information():
    from peeky.native import _local_classes

    def die(tag, parent, children=(), **attrs):
        return {"tag": tag, "parent": parent, "attrs": attrs, "children": list(children)}

    dies = {
        1: die("DW_TAG_compile_unit", None),
        2: die("DW_TAG_subprogram", 1, DW_AT_name='"run"'),
        3: die("DW_TAG_lexical_block", 2),
        4: die("DW_TAG_structure_type", 3, DW_AT_name='"Local"', DW_AT_decl_line="19"),
        5: die("DW_TAG_class_type", 2, [6], DW_AT_decl_line="30"),
        6: die("DW_TAG_subprogram", 5, DW_AT_name='"operator()"'),
        7: die("DW_TAG_class_type", 2, [8], DW_AT_name='"<lambda()>"', DW_AT_decl_line="31"),
        8: die("DW_TAG_subprogram", 7, DW_AT_name='"operator()"'),
        9: die("DW_TAG_structure_type", 2, DW_AT_name='"Fwd"', DW_AT_declaration="(true)"),
        10: die("DW_TAG_structure_type", 1, DW_AT_name='"Cfg"', DW_AT_decl_line="13"),
    }
    assert _local_classes(dies) == {2: ((19, False), (30, True), (31, True))}


TYPEDEFS = """
0x0000000b: DW_TAG_compile_unit
              DW_AT_name\t("a.cpp")
0x00000010:   DW_TAG_namespace
                DW_AT_name\t("std")
0x00000014:     DW_TAG_namespace
                  DW_AT_name\t("__1")
                  DW_AT_export_symbols\t(true)
0x00000020:       DW_TAG_class_type
                    DW_AT_name\t("map<int, int, std::__1::less<int>, std::__1::allocator<std::__1::pair<const int, int> > >")
0x00000028:       DW_TAG_class_type
                    DW_AT_name\t("vector<int, std::__1::allocator<int> >")
0x00000030:       NULL
0x00000031:     NULL
0x00000040:   DW_TAG_typedef
                DW_AT_type\t(0x00000020 "std::__1::map<int, int>")
                DW_AT_name\t("Index")
0x00000048:   DW_TAG_typedef
                DW_AT_type\t(0x00000050 "const Index &")
                DW_AT_name\t("IndexRef")
0x00000050:   DW_TAG_reference_type
                DW_AT_type\t(0x00000058 "const Index")
0x00000058:   DW_TAG_const_type
                DW_AT_type\t(0x00000040 "Index")
0x00000060:   DW_TAG_typedef
                DW_AT_type\t(0x00000020 "std::__1::map<int, int>")
                DW_AT_name\t("Twice")
0x00000068:   DW_TAG_typedef
                DW_AT_type\t(0x00000028 "std::__1::vector<int>")
                DW_AT_name\t("Twice")
0x00000070:   DW_TAG_typedef
                DW_AT_type\t(0x00000078 "int")
                DW_AT_name\t("Id")
0x00000078:   DW_TAG_base_type
                DW_AT_name\t("int")
0x00000080:   NULL
"""


def test_a_typedef_names_the_class_behind_it():
    # #119: `Index m` (`using Index = std::map<int, int>`) shows only as `Index`; its
    # class says `m[1]` is key 1, not the second entry.
    from peeky.native import typedef_kinds

    kinds = typedef_kinds(TYPEDEFS)
    shown = "std::map<int, int, std::__1::less<int>, std::__1::allocator<std::__1::pair<const int, int> > >"
    assert kinds["Index"] == ("other", shown)
    assert kinds["IndexRef"] == ("reference", shown)
    # Two typedefs of one name for two classes, a map among them: it may be a map ("?").
    assert kinds["Twice"] == ("other", "?")
    assert kinds["Id"] == ("other", "")
    native = session()
    native.typedefs = kinds
    for written in ("Index", "const Index &", "Index const&", "IndexRef"):
        assert native.aliased_type(written) == shown, written
    assert native.aliased_type("Index *") is None
    assert native.aliased_type("Twice") == "?"
    # What a pointer points at: `Index *`, or a typedef'd pointer's pointee.
    for written in ("Index *", "const Index *", "Index *const"):
        assert native.pointee_aliased_type(written) == shown, written
    assert native.pointee_aliased_type("IndexPtr", "Index") == shown
    assert native.pointee_aliased_type("Index **") is None
    assert native.pointee_aliased_type("Index") is None
    assert native.typedef_kind("const Index") == ("other", shown)


MEMBERS = """
0x0000000b: DW_TAG_compile_unit
              DW_AT_name\t("a.cpp")
0x00000010:   DW_TAG_namespace
                DW_AT_name\t("std")
0x00000014:     DW_TAG_namespace
                  DW_AT_name\t("__1")
                  DW_AT_export_symbols\t(true)
0x00000020:       DW_TAG_class_type
                    DW_AT_name\t("set<int, std::__1::less<int>, std::__1::allocator<int> >")
0x00000024:       DW_TAG_class_type
                    DW_AT_name\t("map<int, int, std::__1::less<int> >")
0x00000028:       DW_TAG_class_type
                    DW_AT_name\t("map<int, long, std::__1::less<int> >")
0x0000002c:       DW_TAG_class_type
                    DW_AT_name\t("vector<int, std::__1::allocator<int> >")
0x00000030:       NULL
0x00000031:     NULL
0x00000040:   DW_TAG_structure_type
                DW_AT_name\t("Holder<int>")
0x00000044:     DW_TAG_typedef
                  DW_AT_type\t(0x00000020 "std::__1::set<int>")
                  DW_AT_name\t("S")
0x00000048:     DW_TAG_typedef
                  DW_AT_type\t(0x00000024 "std::__1::map<int, int>")
                  DW_AT_name\t("M")
0x0000004c:     DW_TAG_typedef
                  DW_AT_type\t(0x0000002c "std::__1::vector<int>")
                  DW_AT_name\t("V")
0x00000050:     DW_TAG_typedef
                  DW_AT_type\t(0x00000020 "std::__1::set<int>")
                  DW_AT_name\t("X")
0x00000054:     NULL
0x00000060:   DW_TAG_structure_type
                DW_AT_name\t("Holder<long>")
0x00000064:     DW_TAG_typedef
                  DW_AT_type\t(0x00000020 "std::__1::set<int>")
                  DW_AT_name\t("S")
0x00000068:     DW_TAG_typedef
                  DW_AT_type\t(0x00000028 "std::__1::map<int, long>")
                  DW_AT_name\t("M")
0x0000006c:     DW_TAG_typedef
                  DW_AT_type\t(0x00000078 "int")
                  DW_AT_name\t("V")
0x00000070:     DW_TAG_typedef
                  DW_AT_type\t(0x00000078 "int")
                  DW_AT_name\t("X")
0x00000074:     NULL
0x00000078:   DW_TAG_base_type
                DW_AT_name\t("int")
0x00000080:   NULL
"""


def test_a_class_member_typedef_keeps_the_class_every_instance_names():
    # Review of #119 (PR #127): `Holder<std::string>::S hs2` (a std::set<int>) indexed by
    # position, since a member typedef's class was dropped.
    from peeky.native import _merged_class, typedef_kinds

    kinds = typedef_kinds(MEMBERS)
    assert kinds["::Holder::S"] == (
        "other",
        "std::set<int, std::__1::less<int>, std::__1::allocator<int> >",
    )
    # Maps of other mapped types: still a std::map, which one depends on the instance.
    assert kinds["::Holder::M"] == ("other", "std::map")
    # A vector in one instance, an int in another: no map or set either way.
    assert kinds["::Holder::V"] == ("other", "")
    # A set in one, an int in the other: it may be a set.
    assert kinds["::Holder::X"] == ("other", "?")
    native = session()
    native.typedefs = kinds
    assert native.aliased_type("Holder<std::string>::S").startswith("std::set<int")
    # By its own instance's name where the debug information spells it as lldb-dap does,
    # else by the class's.
    assert native.aliased_type("const Holder<long>::M &").startswith("std::map<int, long")
    assert native.aliased_type("const Holder<std::string>::M &") == "std::map"
    assert native.aliased_type("Holder<std::string>::V") is None
    assert native.aliased_type("Holder<std::string>::X") == "?"
    assert _merged_class("std::map<int, int>", "std::unordered_map<int, int>") == "?"
    assert _merged_class("Point", "Line") == ""
    assert _merged_class("?", "") == "?"


def test_unnamed_constructors_and_member_initializer_lambdas_are_told_apart():
    from peeky.native import _CONSTRUCTOR_NAME, _class_role

    # Review of #126 (round 4).
    def die(tag, parent, **attrs):
        return {"tag": tag, "parent": parent, "attrs": attrs, "children": []}

    dies = {
        1: die("DW_TAG_compile_unit", None),
        2: die("DW_TAG_subprogram", 1, DW_AT_name='"nested"'),
        # `struct { long s = twice(13); } anon;`: Clang's implicit constructor.
        3: die("DW_TAG_structure_type", 2, DW_AT_decl_line="55"),
        4: die(
            "DW_TAG_subprogram",
            3,
            DW_AT_name='"(unnamed struct at f.cpp:55:5)"',
            DW_AT_artificial="(true)",
        ),
        5: die("DW_TAG_subprogram", 1, DW_AT_specification='0x00000004 "(unnamed struct)"'),
        6: die("DW_TAG_subprogram", 3, DW_AT_name='"get"', DW_AT_type='0x00000009 "long"'),
        # `struct Agg { long b = [this] { ... }(); };`: its closure is in `Agg`.
        7: die("DW_TAG_structure_type", 1, DW_AT_name='"Agg"', DW_AT_decl_line="13"),
        8: die("DW_TAG_class_type", 7, DW_AT_decl_line="15"),
        9: die("DW_TAG_subprogram", 8, DW_AT_name='"operator()"', DW_AT_type='0x0 "long"'),
        10: die("DW_TAG_subprogram", 1, DW_AT_specification='0x00000009 "operator()"'),
    }
    assert _class_role(dies, [5, 4]) == (True, 2, 55, False)
    assert _class_role(dies, [6]) == (False, 2, 55, False)
    assert _class_role(dies, [10, 9]) == (False, None, 15, True)
    for name in (
        "nested(long)::$_0::$_0()",
        "nested(long)::(anonymous struct)::(anonymous struct)()",
        "f()::(unnamed struct at a.cpp:5:3)::(unnamed struct at a.cpp:5:3)()",
        "nested(long)::{unnamed type#1}::{unnamed type#1}()",
    ):
        assert _CONSTRUCTOR_NAME.search(name), name
    assert not _CONSTRUCTOR_NAME.search("nested(long)::$_0::get() const")


def test_a_blocks_lines_split_only_where_code_outside_it_runs_between():
    # Eval 6, finding 17: C++ runs a block's cleanup on the function's closing line (35),
    # so `name` (lines 22-25) read `lines 22-35`, over code outside its block.
    from peeky.native import _runs

    running = [12, 13, 14, 15, 16, 17, 18, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 33, 35]
    # Line 34 runs no code here: a stop there is made at 35.
    assert _runs([22, 23, 24, 25, 35, 22], running) == ((22, 25), (34, 35))
    # A later span starts right after the last line of code outside the block: a line
    # with no code stops at the next line with code (Rust's `let s = twice!(i);`).
    assert _runs([66, 71], [63, 66, 69, 71, 73]) == ((66, 66), (70, 71))
    # Lines with no code between two of the block's (a comment, a blank) stay inside.
    assert _runs([17, 18, 22], running) == ((17, 22),)
    assert _runs([35], running) == ((35, 35),)


def preprocessing_session(tmp_path, outputs, sources=("a.c",), headers=()):
    """A NativeSession over ``a.c``'s local `q` (declared on line 2, shown at pc 0x150)
    whose "compiler" prints ``outputs[unit]`` as its preprocessed text (None: fails)."""
    import sys

    session = NativeSession.__new__(NativeSession)
    session.load_bias = 0
    where = ("a.c", 2, True)
    session.declarations = [
        {
            "ranges": [(0x100, 0x200)],
            "locals": {"q": {where}},
            "visible": {("q", *where): [(0x100, 0x200)]},
        }
    ]

    def preprocess_argv(unit):
        if outputs[unit] is None:
            return str(tmp_path / "missing-compiler"), [unit]
        code = f"import sys; open(sys.argv[2], 'wb').write({outputs[unit]!r})"
        return sys.executable, ["-c", code]

    session.target = SimpleNamespace(
        sources=sources,
        verification_inputs=sources + headers,
        language="c",
        preprocess_argv=preprocess_argv,
    )
    session.prepared = SimpleNamespace(
        workspace=tmp_path, manifest=SimpleNamespace(effective_environment=())
    )
    return session


UNIT = b"""# 1 "a.c"
# 1 "/usr/include/stdio.h" 1 3 4
int printf(const char *, ...);
# 1 "a.c" 2
long f(void) {
    long q = 1;
    {
        enum { q = 7 };
        return q;
    }
}
"""


def test_a_hidden_local_is_read_from_the_preprocessed_unit_or_withheld(tmp_path):
    # Review of PR #144 (round 2): the translation unit is read as the build's compiler
    # preprocesses it; where it cannot be, the row is withheld, never shown.
    frame = {"instructionPointerReference": "0x150"}
    found = preprocessing_session(tmp_path, {"a.c": UNIT})._block_storage("q", frame, "a.c", 5)
    assert found.label == "not what the code reads" and "enumerator `q` (line 4)" in found
    plain = UNIT.replace(b"enum { q = 7 };", b"long r = 7;")
    assert (
        preprocessing_session(tmp_path, {"a.c": plain})._block_storage("q", frame, "a.c", 5) is None
    )
    failed = preprocessing_session(tmp_path, {"a.c": None})._block_storage("q", frame, "a.c", 5)
    assert failed.label == "may not be what the code reads" and "could not preprocess" in failed
    assert "cannot be changed" in failed.refusal
    # Not in the unit's text at all (the markers name another file): not singled out.
    other = UNIT.replace(b'"a.c"', b'"b.c"')
    lost = preprocessing_session(tmp_path, {"a.c": other})._block_storage("q", frame, "a.c", 5)
    assert lost.label == "may not be what the code reads"
    # A header's function in two units that read it differently: Peeky cannot tell.
    header = b'# 1 "b.c"\n# 1 "a.c" 1\n' + UNIT.split(b"\n", 1)[1]
    plain_header = b'# 1 "c.c"\n# 1 "a.c" 1\n' + plain.split(b"\n", 1)[1]
    (tmp_path / "peeky-object-0.o.d").write_text("b.o: b.c a.c\n")
    (tmp_path / "peeky-object-1.o.d").write_text("c.o: c.c a.c\n")
    session = preprocessing_session(
        tmp_path, {"b.c": header, "c.c": plain_header}, sources=("b.c", "c.c"), headers=("a.c",)
    )
    assert session._block_storage("q", frame, "a.c", 5).label == "may not be what the code reads"
    session = preprocessing_session(
        tmp_path, {"b.c": header, "c.c": header}, sources=("b.c", "c.c"), headers=("a.c",)
    )
    assert session._block_storage("q", frame, "a.c", 5).label == "not what the code reads"
    # A unit whose dependencies do not name the file is not read.
    (tmp_path / "peeky-object-1.o.d").write_text("c.o: c.c\n")
    session = preprocessing_session(
        tmp_path, {"b.c": header, "c.c": None}, sources=("b.c", "c.c"), headers=("a.c",)
    )
    assert session._block_storage("q", frame, "a.c", 5).label == "not what the code reads"


OWN_STATIC_UNIT = b"""# 1 "a.c"
long f(void) {
    long q = 1;
    {
        static long q = 7;
        return q;
    }
}
"""


def test_a_static_kept_as_a_constant_is_shown_where_the_code_names_it(tmp_path):
    # Review of PR #175: at -O1 lldb-dap lists a block's `static` that clang keeps as a
    # constant (DW_AT_const_value, no address) as a local, `q @ a.c:4`. Its row is what
    # the code names; the outer local's says the static is listed apart.
    frame = {"instructionPointerReference": "0x150"}
    session = preprocessing_session(tmp_path, {"a.c": OWN_STATIC_UNIT})
    own = ("a.c", 4, False)
    function = session.declarations[0]
    function["locals"]["q"].add(own)
    function["visible"][("q", *own)] = [(0x100, 0x200)]
    function["unstored"] = frozenset({("q", "a.c", 4)})
    assert session._block_storage("q @ a.c:4", frame, "a.c", 5) is None
    listed = [{"name": "q @ a.c:2"}, {"name": "q @ a.c:4"}]
    for siblings in (listed, None):
        found = session._block_storage("q @ a.c:2", frame, "a.c", 5, siblings=siblings)
        assert found.label == "not what the code reads", found
        assert "the block's `static` `q` (line 4), listed separately as `q @ a.c:4`" in found
        assert "does not list" not in found and "does not list" not in found.refusal
    # Where no row of it is listed (at -O0, it has an address): said so.
    found = session._block_storage("q @ a.c:2", frame, "a.c", 5, siblings=listed[:1])
    assert "the block's `static` `q` (line 4), which the debugger does not list" in found
    function["unstored"] = frozenset()
    found = session._block_storage("q", frame, "a.c", 5)
    assert "which the debugger does not list" in found


def test_members_of_a_root_outside_the_frames_memory_are_never_settable():
    # #138 with #146: `set` refuses a field or element of a root the debug information
    # keeps in a register (or a location list) or as a static, at every stop; its rows
    # say why. Roots, frame locals and optimized builds are left to the stop.
    native = session()
    native.fields, native.target = {}, SimpleNamespace(optimization="O0")
    types = {
        "r": {(1, "register", ("a.c", 3))},
        "l": {(1, "other", ("a.c", 4))},
        "k": {(1, "static", ("a.c", 5))},
        "s": {(1, "automatic", ("a.c", 6))},
        "p": {(1, "parameter", ("a.c", 2))},
        "z": {(1, "register", ("a.c", 7)), (1, "automatic", ("a.c", 9))},
    }
    native._function_at = lambda frame: {"types": types}
    row = native.never_settable_row
    assert row({"name": "r"}, {}, member=True) == "register"
    assert row({"name": "l"}, {}, member=True) == "register"
    assert row({"name": "k"}, {}, member=True) == "static"
    assert row({"name": "s"}, {}, member=True) is None
    assert row({"name": "p"}, {}, member=True) is None
    # LLDB's name for one of several: only that one's storage.
    assert row({"name": "z @ a.c:7"}, {}, member=True) == "register"
    assert row({"name": "z @ a.c:9"}, {}, member=True) is None
    assert row({"name": "z"}, {}, member=True) is None
    assert row({"name": "r"}, {}) is None
    native.target = SimpleNamespace(optimization="O1")
    assert row({"name": "r"}, {}, member=True) is None


def test_a_caller_is_judged_before_its_return_address_unless_a_signal_interrupted_it():
    # #158, review of PR #159: a caller frame's address is where its call returns, so
    # blocks are matched one before it, as LLDB reads it; the frame above a signal
    # trampoline was interrupted at its instruction, which counts as it is.
    from peeky.native import _trampoline

    session = NativeSession.__new__(NativeSession)
    session.load_bias = 0
    frames = [
        {"id": 1, "name": "on_alarm", "instructionPointerReference": "0x100"},
        {
            "id": 2,
            "name": "libsystem_platform.dylib`_sigtramp",
            "instructionPointerReference": "0x200",
        },
        {"id": 3, "name": "main", "instructionPointerReference": "0x300"},
        {"id": 4, "name": "start", "instructionPointerReference": "0x400"},
    ]
    session.frames_listed(frames)
    assert [session._block_pc(frame) for frame in frames] == [0x100, 0x1FF, 0x300, 0x3FF]
    for name in ("_sigtramp", "__restore_rt", "__kernel_rt_sigreturn", "<signal handler called>"):
        assert _trampoline(name), name
    assert not _trampoline("sigtramp_helper") and not _trampoline(None)
    # A function of the program named so is no trampoline (review of PR #159, round 3).
    session.prepared = SimpleNamespace(workspace=Path("/w"))
    session.target = SimpleNamespace(verification_inputs=("app.c",))
    own = {**frames[1], "name": "__restore", "source": {"path": "/w/app.c"}}
    session.frames_listed([frames[0], own, frames[2]])
    assert session._block_pc(frames[2]) == 0x2FF
    # Before the worker lists a stop's frames, a frame counts as it is.
    fresh = NativeSession.__new__(NativeSession)
    fresh.load_bias = 0
    assert fresh._block_pc(frames[2]) == 0x300


def prologue_dumps(workspace, marked=False):
    """A C unit as GCC builds it (``marked``: as clang does): `one` (line 3), whose code is
    all on its first line, with a parameter, one the caller passed on the stack, a local,
    a static, one a location list places, one in a register and a thread-local; `none`
    (line 4), whose prologue is empty; and a lambda's body (lines 5-6), which names no
    file and no frame base."""
    source = workspace / "entry.c"
    info = f"""{workspace}/program: file format elf64-littleaarch64

.debug_info contents:
0x0000000b: DW_TAG_compile_unit
              DW_AT_name\t("entry.c")
0x00000020:   DW_TAG_subprogram
                DW_AT_low_pc\t(0x0000000000000400)
                DW_AT_high_pc\t(0x0000000000000420)
                DW_AT_frame_base\t(DW_OP_call_frame_cfa)
                DW_AT_name\t("one")
                DW_AT_decl_file\t("{source}")
                DW_AT_decl_line\t(3)
0x0000002c:     DW_TAG_formal_parameter
                  DW_AT_location\t(DW_OP_fbreg +0)
                  DW_AT_name\t("ninth")
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(3)
0x00000030:     DW_TAG_formal_parameter
                  DW_AT_location\t(DW_OP_fbreg -20)
                  DW_AT_name\t("a")
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(3)
0x00000038:     DW_TAG_variable
                  DW_AT_location\t(DW_OP_fbreg -4)
                  DW_AT_name\t("b")
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(3)
0x00000040:     DW_TAG_variable
                  DW_AT_location\t(DW_OP_addr 0x11000)
                  DW_AT_name\t("calls")
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(3)
0x00000048:     DW_TAG_formal_parameter
                  DW_AT_location\t(0x00000000:
                     [0x0000000000000400, 0x0000000000000410): DW_OP_reg1 W1)
                  DW_AT_name\t("listed")
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(3)
0x0000004c:     DW_TAG_variable
                  DW_AT_location\t(DW_OP_reg19 W19)
                  DW_AT_name\t("held")
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(3)
0x0000004e:     DW_TAG_variable
                  DW_AT_location\t(DW_OP_const8u 0x10, DW_OP_GNU_push_tls_address)
                  DW_AT_name\t("each")
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(3)
0x00000050:     NULL
0x00000058:   DW_TAG_subprogram
                DW_AT_low_pc\t(0x0000000000000420)
                DW_AT_high_pc\t(0x0000000000000430)
                DW_AT_name\t("none")
                DW_AT_decl_file\t("{source}")
                DW_AT_decl_line\t(4)
0x00000068:   DW_TAG_subprogram
                DW_AT_low_pc\t(0x0000000000000430)
                DW_AT_high_pc\t(0x0000000000000450)
                DW_AT_name\t("operator()")
                DW_AT_artificial\t(true)
0x00000078:     DW_TAG_formal_parameter
                  DW_AT_location\t(DW_OP_fbreg -24)
                  DW_AT_name\t("v")
0x0000007c:     DW_TAG_formal_parameter
                  DW_AT_location\t(DW_OP_fbreg +8)
                  DW_AT_name\t("w")
0x00000080:     NULL
0x00000088:   NULL
"""
    rows = [(0x400, 3), (0x408, 3), (0x410, 3), (0x420, 4), (0x420, 4), (0x428, 4)]
    rows += [(0x430, 5), (0x43C, 6), (0x444, 6)]
    line = 'debug_line[0x00000000]\nversion: 4\nfile_names[ 1]:\nname: "entry.c"\ndir_index: 0\n'
    line += "Address Line Column File\n"
    for address, number in rows:
        flags = " prologue_end" if marked and address in (0x40C, 0x43C) else ""
        line += f"0x{address:016x} {number} 1 1 0 0 is_stmt{flags}\n"
    if marked:
        # Clang's mark, past the parameters' stores, on a row of its own.
        line += f"0x{0x40C:016x} 3 30 1 0 0 is_stmt prologue_end\n"
    line += f"0x{0x450:016x} 5 1 1 0 0 end_sequence\n"
    return info, "\n".join(sorted(line.splitlines(), key=_row_order)) + "\n"


def _row_order(text):
    """Header lines first, then the rows by address, the end of the sequence last."""
    if not text.startswith("0x"):
        return (0, 0, 0)
    return (1, int(text.split()[0], 16), "end_sequence" in text)


def test_a_functions_prologue_runs_to_its_mark_or_else_to_its_second_row(tmp_path):
    # #279: GCC marks no prologue end, so its second row (the body's first) ends the
    # prologue, where the parameters are stored; clang marks it. An empty prologue (two
    # rows at the entry) has no window; a lambda that names no file has one all the same.
    from peeky.native import local_declarations

    (tmp_path / "entry.c").write_text("\n" * 10)
    # A static, a thread-local, one a location list places and one on the caller's stack
    # (at or above the call's frame address); not one in a register. The lambda's frame
    # base is unknown, so its `w` is not kept.
    keep = frozenset({"calls", "each", "listed", "ninth"})
    three, five = frozenset({("entry.c", 3)}), frozenset({("entry.c", 5)})
    for marked, windows in (
        (
            False,
            [
                (0x400, 0x408, False, keep, True, three),
                (0x430, 0x43C, False, frozenset(), False, five),
            ],
        ),
        (
            True,
            [
                (0x400, 0x40C, True, keep, True, three),
                (0x430, 0x43C, True, frozenset(), False, five),
            ],
        ),
    ):
        info, line = prologue_dumps(tmp_path, marked)
        found = []
        local_declarations(info, line, tmp_path, ("entry.c",), prologues=found)
        assert found == windows, (marked, found)


def prologue_session(windows):
    from peeky.native import PrologueWindow

    session = NativeSession.__new__(NativeSession)
    session.load_bias = 0x1000
    session.prologues = tuple(PrologueWindow(*window) for window in windows)
    # Nothing else of the build is known: only the prologue rule judges.
    session.declarations, session.fields = None, None
    return session


def test_a_stop_in_a_prologue_withholds_what_it_has_not_stored(tmp_path):
    from peeky.native import _prologue_windows

    lines = frozenset()
    session = prologue_session(
        [
            (0x400, 0x408, False, frozenset({"calls"}), True, lines),
            (0x430, 0x43C, True, frozenset(), True, lines),
            (0x460, 0x468, False, frozenset(), False, lines),
        ]
    )

    def frame(address):
        return {"id": 1, "instructionPointerReference": hex(0x1000 + address)}

    reason = session.not_yet_declared({"name": "a"}, frame(0x400))
    assert reason == (
        "the stop is at its function's start, before the prologue stores the parameters: "
        "its storage holds leftovers (the build marks no prologue end, as GCC's never do, so "
        "LLDB stops a one-line function here). Read the arguments at the call, or put the "
        "body on a line of its own"
    )
    # A row keeps `no value yet`, and a change says why it is refused.
    assert reason.label is None and len(reason) <= 300
    assert reason.refusal == (
        "'a' cannot be changed here: the stop is at its function's start, before the "
        "prologue stores the parameters, and the function's code may overwrite a change. "
        "Change the argument at the call, or put the body on a line of its own and stop there"
    )
    with pytest.raises(ValueError, match="before the prologue stores the parameters"):
        session.validate_mutation_target({"name": "a"}, frame(0x404))
    # A located name (`b @ entry.c:3`) too; a static is not the prologue's to store.
    assert session.not_yet_declared({"name": "b @ entry.c:3"}, frame(0x404)) == reason
    assert session.not_yet_declared({"name": "calls"}, frame(0x400)) is None
    # From the body's first row on, and outside the function, the rule says nothing.
    assert session.not_yet_declared({"name": "a"}, frame(0x408)) is None
    assert session.not_yet_declared({"name": "a"}, frame(0x3FC)) is None
    # A build that marks the prologue's end: only a stop no breakpoint makes is there.
    marked = session.not_yet_declared({"name": "v"}, frame(0x434))
    assert marked == (
        "the stop is in its function's prologue, before it has stored the parameters: its "
        "storage holds leftovers. Read it at a later stop in the function"
    )
    # A function on several lines whose start LLDB stops at all the same (review of #279:
    # the next function's merged rows taken for the prologue's end): no one-line advice.
    later = session.not_yet_declared({"name": "k"}, frame(0x460))
    assert later == (
        "the stop is at its function's start, before the prologue stores the parameters: its "
        "storage holds leftovers (the build marks no prologue end, as GCC's never do, and LLDB "
        "did not find it). Read the arguments at the call, or stop on a later line of the "
        "function"
    )
    assert len(later) <= 300 and later.refusal.endswith(
        "Change the argument at the call, or change it on a later line of the function"
    )
    # A change: refused as before, but for what the stop is, not "part-way through".
    for address, text, advice in (
        (
            0x400,
            "the stop is at its function's start, before the prologue stores the parameters, "
            "and the function's code may overwrite a change",
            "Change the argument at the call, or put the body on a line of its own and stop there",
        ),
        (
            0x430,
            "the stop is in its function's prologue, before it has stored the parameters, "
            "and the function's code may overwrite a change",
            "Change it at a later stop in the function",
        ),
    ):
        why = session.mid_statement({"name": "calls"}, frame(address))
        assert (why, why.advice) == (text, advice)
    # A caller frame is judged one before its return address.
    session.frames_listed([frame(0x500), {**frame(0x408), "id": 2}])
    assert session.not_yet_declared({"name": "a"}, {**frame(0x408), "id": 2}, innermost=False)
    assert _prologue_windows({}, [], set()) == []


def test_a_frame_whose_address_is_unknown_is_withheld_on_a_gcc_prologues_line(
    tmp_path, monkeypatch
):
    # Review of #279: where the debugger gives no load address (`addressRange` missing or
    # unreadable, which a frame with a `moduleId` allows), no pc can be placed. On a line
    # where a GCC prologue runs, the stop may be before it stored the parameters.
    monkeypatch.setattr("peeky.native.sys.platform", "linux")
    source = tmp_path / "entry.c"
    source.write_text("\n" * 10)
    session = prologue_session(
        [
            (0x400, 0x408, False, frozenset({"calls"}), True, frozenset({("entry.c", 3)})),
            (0x420, 0x428, False, frozenset(), False, frozenset({("entry.c", 4)})),
            (0x430, 0x43C, True, frozenset(), True, frozenset({("entry.c", 5)})),
        ]
    )
    session.prepared = SimpleNamespace(workspace=tmp_path)
    session.binary = session.symbols = elf_load_fixture(tmp_path, address=0)
    session.module_identity = "AABB"
    module = {
        "id": "AA-BB",
        "path": str(session.binary),
        "symbolStatus": "Symbols loaded.",
        "symbolFilePath": str(session.binary),
    }
    adapter = SimpleNamespace(request=lambda *_: {"modules": [module]})

    def frame(line, address=0x1400):
        return {
            "id": 1,
            "moduleId": "AA-BB",
            "instructionPointerReference": hex(address),
            "source": {"path": str(source)},
            "line": line,
        }

    note = (
        "Peeky cannot tell whether the stop is past its function's prologue, which stores "
        "the parameters: the debugger did not report where the program is loaded, and the "
        "build marks no prologue end (GCC's never do)."
    )
    for loaded in (None, "elsewhere"):
        if loaded is None:
            module.pop("addressRange", None)
        else:
            module["addressRange"] = loaded
        # The module's identity is verified all the same.
        session.validate_debugger(adapter, frame(3))
        assert session.load_bias is None and session.load_bias_unknown is True
        reason = session.not_yet_declared({"name": "a"}, frame(3))
        assert reason == note + (
            " Read the arguments at the call, or put the body on a line of its own"
        )
        assert len(reason) <= 300 and reason.label is None
        assert reason.refusal == (
            "'a' cannot be changed here: " + note[:-1] + ". Change the argument at the call, "
            "or put the body on a line of its own and change it there"
        )
        with pytest.raises(ValueError, match="cannot tell whether the stop is past"):
            session.validate_mutation_target({"name": "a"}, frame(3))
        # A function on several lines says to stop past its first line.
        assert session.not_yet_declared({"name": "k"}, frame(4)).endswith(
            "Read the arguments at the call, or stop past the function's first line"
        )
        # What the prologue leaves alone, a line no GCC prologue runs on, and a prologue
        # whose end is marked (clang's, where no breakpoint stops) are not withheld.
        assert session.not_yet_declared({"name": "calls"}, frame(3)) is None
        assert session.not_yet_declared({"name": "a"}, frame(6)) is None
        assert session.not_yet_declared({"name": "v"}, frame(5)) is None
    # With the load address, the address decides: past the prologue, the value reads.
    module["addressRange"] = str(0x1000)
    session.validate_debugger(adapter, frame(3))
    assert session.load_bias == 0x1000 and session.load_bias_unknown is False
    assert session.not_yet_declared({"name": "a"}, frame(3, 0x1408)) is None
    assert session.not_yet_declared({"name": "a"}, frame(3)).startswith(
        "the stop is at its function's start"
    )
    # A frame the debugger gives no address for says so.
    pointless = {
        key: value for key, value in frame(3).items() if key != "instructionPointerReference"
    }
    assert "the debugger gave no instruction address for this frame" in (
        session.not_yet_declared({"name": "a"}, pointless)
    )
