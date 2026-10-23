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


def declaration_dumps(workspace, rows=None):
    """A C unit: `take` (lines 5-12) with a parameter, two automatics, a static and a
    local in a nested block; the static has an address, not a frame location. ``rows``:
    the line table's (address, line), offsets from 0x100000000."""
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
    rows = rows or [(0x400, 5), (0x410, 6), (0x420, 7), (0x430, 8), (0x440, 11), (0x460, 12)]
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


def test_a_declaration_lines_first_row_after_another_lines_code_is_only_preceded(tmp_path):
    from peeky.native import local_declarations

    # #288: at -O0 clang puts an exception landing pad (on a closing `}`'s line, 12 here)
    # among the function's code, just before line 11's. Whether that code runs into line 11
    # (a header over two lines does) or ends in a jump, the stop's disassembly tells.
    (tmp_path / "bucket.c").write_text("\n" * 20)
    rows = [(0x400, 5), (0x410, 6), (0x420, 7), (0x430, 8), (0x438, 12), (0x440, 11), (0x460, 12)]
    info, dump = declaration_dumps(tmp_path, rows)
    [take] = local_declarations(info, dump, tmp_path, ("bucket.c",))
    base = 0x100000000
    assert take["starts"] == {
        ("elapsed", "bucket.c", 6): {base + 0x410},
        ("refill", "bucket.c", 8): {base + 0x430},
    }
    assert take["preceded"] == {("now", "bucket.c", 11): {base + 0x440}}
    # Without the landing pad, line 11's first row is a start.
    info, dump = declaration_dumps(tmp_path)
    [take] = local_declarations(info, dump, tmp_path, ("bucket.c",))
    assert take["starts"][("now", "bucket.c", 11)] == {base + 0x440}
    assert take["preceded"] == {}


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


def test_a_first_row_after_another_lines_code_is_a_start_where_that_code_ends_in_a_jump():
    from peeky.native import declared_on_line

    # #288: line 20's code (a landing pad) lies just before line 16's first row, 0x80.
    main = {
        "source": "q.cpp",
        "rows": rows({16: (0x80, 0x84), 20: (0x70, 0x7C)}, "q.cpp"),
        "jumps": False,
        "ranges": ((0x60, 0xA0),),
        "locals": {"q": {("q.cpp", 16, True)}},
        "visible": {("q", "q.cpp", 16, True): ((0x60, 0xA0),)},
        "starts": {},
        "preceded": {("q", "q.cpp", 16): {0x80}},
    }
    # It ends in a jump: control enters line 16 there, before `q` is set.
    assert declared_on_line([main], "q", "q.cpp", 16, 0x80, {0x80: False}.get) == (16, None)
    labelled = [{**main, "jumps": True}]
    assert declared_on_line(labelled, "q", "q.cpp", 16, 0x80, {0x80: False}.get) == (16, "label")
    # It runs into line 16 (`for (` over two lines), with the local set.
    assert declared_on_line([main], "q", "q.cpp", 16, 0x80, {0x80: True}.get) is None
    # Not read: unknown, said.
    assert declared_on_line([main], "q", "q.cpp", 16, 0x80, {}.get) == (16, "preceded")
    assert declared_on_line([main], "q", "q.cpp", 16, 0x80) == (16, "preceded")
    # Past the line's first row, the local is live.
    assert declared_on_line([main], "q", "q.cpp", 16, 0x84, {0x80: False}.get) is None
    # Two `q` in one block: whichever is shown, withheld where the line starts; with
    # where it starts unknown, said so.
    twice = {**main, "locals": {"q": {("q.cpp", 16, True), ("q.cpp", 12, True)}}}
    twice["visible"] = {**main["visible"], ("q", "q.cpp", 12, True): ((0x60, 0xA0),)}
    assert declared_on_line([twice], "q", "q.cpp", 16, 0x80, {0x80: False}.get) == (16, "unknown")
    assert declared_on_line([twice], "q", "q.cpp", 16, 0x80, {}.get) == (16, "preceded")
    assert declared_on_line([twice], "q", "q.cpp", 16, 0x80, {0x80: True}.get) is None


def test_the_disassembly_says_where_control_comes_from():
    from peeky.native import control_sources

    # #288: clang's landing pad ends in `b`, out to the unwinder's resume; line 16's code
    # follows it, reached by the inner loop's exit (`b.ge`). At -O0 clang ends a block
    # with a jump to the next instruction.
    arm = _instructions(
        (0x620, "b.ge    0x6FC"),
        (0x624, "b       0x628                     ; <+320> at q.cpp:13:9"),
        (0x628, "bl      0x100000964"),
        (0x62C, "b       0x620"),
        (0x6F8, "b       0x10000078c               ; <+676> at q.cpp:10:38"),
        (0x6FC, "str     wzr, [sp, #0x50]"),
        (0x700, "ret"),
        (0x704, "brk     #0x1"),
        (0x708, "nop"),
    )
    assert control_sources(arm, 0x620, 0x70C, "arm64") == {
        0x620: {0x62C},
        0x624: {0x620},  # after a conditional jump
        0x628: {0x624},  # a jump to the next instruction
        0x62C: {None},  # after a call, which may not return
        0x6F8: set(),  # after a jump back
        0x6FC: {0x620},
        0x700: {0x6FC},
        0x704: set(),
        0x708: set(),
    }
    # Only the range's own instructions, and jumps into it.
    assert control_sources(arm, 0x6F8, 0x700, "arm64") == {0x6F8: set(), 0x6FC: set()}
    # Review of #295: a cleanup ending in a call that never returns (`_Unwind_Resume`,
    # `__cxa_throw`), laid just before the row, which a branch reaches: that call's
    # return is no way in Peeky can follow.
    noreturn = _instructions(
        (0x620, "b.ge    0x6FC"),
        (0x624, "b       0x6F0"),
        (0x6F0, "str     x0, [sp, #0x80]"),
        (0x6F4, "ldr     x0, [sp, #0x80]"),
        (0x6F8, "bl      0x100000990"),
        (0x6FC, "str     wzr, [sp, #0x50]"),
    )
    assert control_sources(noreturn, 0x620, 0x700, "arm64")[0x6FC] == {0x620, None}
    x86 = _instructions(
        (0x1130, "jge    0x1146"),
        (0x1136, "jmp    0x1141"),
        (0x113B, "movq   -0x10(%rbp), %rdi"),
        (0x113F, "jne    0x1130"),
        (0x1141, "callq  0x401020"),
        (0x1146, "movl   $0x0, -0x8(%rbp)"),
        (0x114D, "retq"),
        (0x114E, "ud2"),
        (0x1150, "nop"),
    )
    assert control_sources(x86, 0x1130, 0x1150, "x86_64") == {
        0x1130: {0x113F},
        0x1136: {0x1130},
        0x113B: set(),
        0x113F: {0x113B},
        0x1141: {0x1136, 0x113F},
        0x1146: {0x1130, None},  # the jump, and a `call __cxa_throw` that never returns
        0x114D: {0x1146},
        0x114E: set(),
    }
    # A jump through a register may go anywhere: no instruction's ways in are all known.
    for listing, kind in (
        (_instructions((0x10, "br      x8"), (0x14, "nop"), (0x18, "nop")), "arm64"),
        (_instructions((0x10, "jmpq   *%rax"), (0x12, "nop"), (0x13, "nop")), "x86_64"),
        (_instructions((0x10, "jmpq   *0x8(,%rax,8)"), (0x17, "nop")), "x86_64"),
    ):
        found = control_sources(listing, 0x10, 0x20, kind)
        assert found and all(None in ways for ways in found.values()), found
    assert control_sources(arm, 0x620, 0x70C, "riscv64") is None
    assert control_sources([{"address": "nope"}], 0x10, 0x18, "arm64") is None


def test_control_comes_to_a_line_only_past_its_declaration_on_every_way_in():
    from peeky.native import runs_into

    # Line 16's first row is 0x6FC; a landing pad on line 20 lies just before it.
    order = (
        (0x610, "q.cpp", 13),
        (0x628, "q.cpp", 14),
        (0x6E4, "q.cpp", 20),
        (0x6FC, "q.cpp", 16),
        (0x700, "q.cpp", 17),
    )
    lines = {("q.cpp", 16)}
    # Reached from line 13's loop exit only: control enters line 16 there.
    assert runs_into(order, 0x6FC, lines, {0x6FC: {0x620}}) is False
    # Only line 20's code falls or jumps into it (`for (` over two lines).
    assert runs_into(order, 0x6FC, lines, {0x6FC: {0x6F8}}) is True
    # Another file's code too (a member initializer clang runs in its caller).
    other = (*order[:2], (0x6E4, "q.hpp", 3), *order[3:])
    assert runs_into(other, 0x6FC, lines, {0x6FC: {0x6F8}}) is True
    # Review of #295: from line 13 and from line 20 alike, the stop cannot tell which
    # way control came: unknown, never proof that the declaration ran.
    assert runs_into(order, 0x6FC, lines, {0x6FC: {0x620, 0x6F8}}) is None
    assert runs_into(other, 0x6FC, lines, {0x6FC: {0x620, 0x6F8}}) is None
    # A jump back is a loop's, judged apart where control enters the line; past the
    # declaration only, it may still come back before the declaration runs again.
    assert runs_into(order, 0x6FC, lines, {0x6FC: {0x620, 0x704}}) is False
    assert runs_into(order, 0x6FC, lines, {0x6FC: {0x6F8, 0x704}}) is None
    # A way in Peeky cannot follow (a call that may not return, a computed jump), none
    # from below (only the unwinder, or a jump back, enters it), or no disassembly.
    assert runs_into(order, 0x6FC, lines, {0x6FC: {0x620, None}}) is None
    assert runs_into(order, 0x6FC, lines, {0x6FC: {0x6F8, None}}) is None
    assert runs_into(order, 0x6FC, lines, {0x6FC: set()}) is None
    assert runs_into(order, 0x6FC, lines, None) is None
    assert runs_into(order, 0x6FC, lines, {0x700: {0x6FC}}) is None
    # Review round 2 of #295: only a jump back comes to it, a loop's: control enters the
    # line there, and the loop's note says why the value may be the last pass's.
    assert runs_into(order, 0x6FC, lines, {0x6FC: {0x704}}) is False
    # Review round 2 of #295: the unwinder may enter it (a landing pad), which no
    # disassembly shows, though line 20's code falls into it: never proof.
    assert runs_into(order, 0x6FC, lines, {0x6FC: {0x6F8}}, frozenset({0x6FC})) is None
    assert runs_into(order, 0x6FC, lines, {0x6FC: {0x620}}, frozenset({0x6FC})) is None
    assert runs_into(order, 0x6FC, lines, {0x6FC: {0x6F8}}, None) is None
    assert runs_into(order, 0x6FC, lines, {0x6FC: {0x6F8}}, frozenset({0x6E4})) is True


def test_the_stops_disassembly_says_how_control_comes_to_a_preceded_line(monkeypatch, tmp_path):
    # #288: one disassembly of the function says both whether a loop returns to a line's
    # start and, where another line's code lies just before it, whether control comes
    # there only past the declaration.
    monkeypatch.setattr("peeky.native.architecture", lambda binary: "arm64")
    session = NativeSession.__new__(NativeSession)
    session.load_bias, session.binary = 0x1000, None
    session.unwinder_entries = frozenset({0x6E4})
    session.declarations = [
        {
            "ranges": ((0x600, 0x710),),
            "starts": {("k", "q.cpp", 13): {0x610}},
            "preceded": {
                ("q", "q.cpp", 16): {0x6FC},
                ("i2", "q.cpp", 19): {0x704},
                ("m", "q.cpp", 18): {0x70C},
            },
            "order": (
                (0x600, "q.cpp", 12),
                (0x610, "q.cpp", 13),
                (0x6F0, "q.cpp", 20),
                (0x6FC, "q.cpp", 16),
                (0x700, "q.cpp", 20),
                (0x704, "q.cpp", 19),
                (0x708, "q.cpp", 22),
                (0x70C, "q.cpp", 18),
            ),
        }
    ]
    listing = _instructions(
        *((0x1000 + at, "nop") for at in range(0x600, 0x620, 4)),
        (0x1620, "b.ge    0x16FC"),
        (0x1624, "cbz     w0, 0x170C"),
        (0x1628, "bl      0x1990"),
        *((0x1000 + at, "nop") for at in range(0x62C, 0x6F8, 4)),
        (0x16F8, "b       0x1708"),
        (0x16FC, "str     wzr, [sp, #0x50]"),
        (0x1700, "b       0x1704"),
        (0x1704, "b.lt    0x1610"),
        (0x1708, "ldr     w8, [sp, #0x28]"),
        *((0x1000 + at, "nop") for at in range(0x70C, 0x720, 4)),
    )
    original = listing
    asked = []

    class Adapter:
        def request(self, command, arguments, timeout=None):
            asked.append((command, arguments["memoryReference"]))
            return {"instructions": listing}

    for pc in (0x6FC, 0x704, 0x70C, 0x610, 0x62C):
        session._check_loop_target(Adapter(), {"instructionPointerReference": hex(0x1000 + pc)})
    # Line 16 from line 13 only; line 19 from line 20 only; line 18 from both.
    assert session.runs_into == {0x6FC: False, 0x704: True, 0x70C: None}
    assert session.loop_targets == {0x6FC: False, 0x704: False, 0x70C: False, 0x610: True}
    assert asked == [("disassemble", "0x1600")]
    assert not getattr(session, "unread_code", None)
    # A disassembly that cannot be read leaves both unknown, and says so.
    session.__dict__.pop("backward_jumps")
    session.loop_targets, session.runs_into = {}, {}
    listing = []
    session._check_loop_target(Adapter(), {"instructionPointerReference": hex(0x16FC)})
    assert session.runs_into == {0x6FC: None} and session.loop_targets == {0x6FC: None}
    assert session.unread_code == {0x6FC}
    # Review round 2 of #295: line 19's row, which only line 20's code reaches in the
    # disassembly, is also a landing pad; or the binary's exception tables cannot be read.
    for entries in (frozenset({0x704}), "unread"):
        session.__dict__.pop("backward_jumps")
        session.loop_targets, session.runs_into = {}, {}
        if entries == "unread":
            del session.unwinder_entries
            session.binary = tmp_path / "not-a-binary"
            session.binary.write_bytes(b"#!/bin/sh\n")
        else:
            session.unwinder_entries = entries
        listing = original
        session._check_loop_target(Adapter(), {"instructionPointerReference": hex(0x1704)})
        assert session.runs_into == {0x704: None}, entries
    assert session.unwinder_entries is None


# Apple clang's LSDA for q.cpp's `main` (#288): no LPStart, no type table, uleb128 call
# sites; three of its four records have a landing pad, at main + 0x164, 0x1B4 and 0x1FC.
MAIN_LSDA = bytes.fromhex(
    "ff ff 01 17"  # LPStart, type table: none; call sites: uleb128, 23 bytes
    " 30 4c e402 00  9801 08 b403 00  c402 8402 fc03 00  c804 64 00 00"  # start, size, pad, action
    " 00"
)


def test_an_lsdas_call_sites_give_its_landing_pads():
    from peeky.native import _lsda_pads

    table = b"\xaa" * 4 + MAIN_LSDA
    assert _lsda_pads(table, 0x24D0, 0x24D4, 0x4E8) == {0x64C, 0x69C, 0x6E4}
    # udata4 call sites; an LPStart, pc-relative, that the landing pads count from.
    sites = struct.pack("<IIIB", 0x30, 0x4C, 0x164, 0) + struct.pack("<IIIB", 0x98, 8, 0, 0)
    udata = bytes([0xFF, 0xFF, 0x03, len(sites)]) + sites
    assert _lsda_pads(udata, 0x9000, 0x9000, 0x4E8) == {0x64C}
    based = bytes([0x1B]) + struct.pack("<i", 0x100 - 1) + bytes([0xFF, 0x03, len(sites)]) + sites
    assert _lsda_pads(based, 0x9000, 0x9000, 0x4E8) == {0x9100 + 0x164}
    # A type table's offset is skipped.
    typed = bytes([0xFF, 0x9B, 0x05, 0x03, len(sites)]) + sites
    assert _lsda_pads(typed, 0x9000, 0x9000, 0x4E8) == {0x64C}
    # Review 4 of PR #295: an LPStart of 2**64 - 1 plus a landing pad's offset leaves the
    # address space: malformed, never an address.
    huge = bytes([0x00]) + struct.pack("<Q", (1 << 64) - 1) + bytes([0xFF, 0x03, len(sites)])
    with pytest.raises(ValueError, match="address space"):
        _lsda_pads(huge + sites, 0x9000, 0x9000, 0x4E8)
    for broken, where in (
        (MAIN_LSDA[:10], 0x24D4),  # a call-site table past the section's end
        (MAIN_LSDA, 0x2000),  # outside the section
        (bytes([0xFF, 0xFF, 0x13, 0x04]) + bytes(4), 0x24D4),  # pc-relative call sites
        (bytes([0xFF, 0xFF, 0x0F, 0x04]) + bytes(4), 0x24D4),  # an unknown format
    ):
        with pytest.raises(ValueError):
            _lsda_pads(broken, 0x24D4, where, 0x4E8)


def eh_frame(address, functions):
    """An `.eh_frame` section loaded at ``address``: a "zPLR" CIE (an indirect personality,
    pc-relative LSDAs and starts), an FDE per (start, size, LSDA or 0) under it, a "zR"
    CIE with an FDE of its own, and the terminator."""

    def record(data, body):
        body += bytes(-len(body) % 4)
        return data + struct.pack("<I", len(body)) + body

    personal = bytes([1]) + b"zPLR\0" + bytes([1, 0x78, 30, 7, 0x9B]) + bytes(4)
    data = record(b"", bytes(4) + personal + bytes([0x1B, 0x1B]))
    for start, size, lsda in functions:
        at = len(data) + 4
        begin = start - (address + at + 4)
        where = lsda - (address + at + 13) if lsda else 0
        data = record(data, struct.pack("<IiiBi", at, begin, size, 4, where))
    plain = len(data)
    data = record(data, bytes(4) + bytes([1]) + b"zR\0" + bytes([1, 0x78, 30, 1, 0x1B]))
    at = len(data) + 4
    data = record(data, struct.pack("<IiiB", at - plain, 0x500 - (address + at + 4), 16, 0))
    return data + bytes(4)


def test_eh_frame_fdes_name_their_lsdas():
    from peeky.native import _eh_frame_lsdas

    data = eh_frame(0x2000, [(0x400, 0x80, 0x3010), (0x480, 0x20, 0)])
    assert _eh_frame_lsdas(data, 0x2000) == [(0x400, 0x3010)]
    # An FDE whose CIE is not before it, a truncated record, an unknown augmentation.
    with pytest.raises(ValueError, match="names no CIE"):
        _eh_frame_lsdas(data[28:], 0x2000 + 28)
    with pytest.raises(ValueError, match="Truncated"):
        _eh_frame_lsdas(data[:30], 0x2000)
    with pytest.raises(ValueError, match="Truncated"):
        _eh_frame_lsdas(data[:40], 0x2000)
    with pytest.raises(ValueError):
        _eh_frame_lsdas(data.replace(b"zPLR", b"zPXR"), 0x2000)


def unwind_info(lsdas, base=0):
    """A Mach-O `__unwind_info` with no encodings: two index entries (the second the
    sentinel), and the LSDA array (function, LSDA), offsets from ``base``."""
    array = 28 + 24
    index = struct.pack("<III", 0x4E8, 0, array) + struct.pack(
        "<III", 0x9000, 0, array + 8 * len(lsdas)
    )
    body = b"".join(struct.pack("<II", start - base, lsda - base) for start, lsda in lsdas)
    return struct.pack("<7I", 1, 28, 0, 28, 0, 28, 2) + index + body


def test_unwind_info_names_each_functions_lsda():
    from peeky.native import _unwind_info_lsdas

    data = unwind_info([(0x1000004E8, 0x1000024D4), (0x100000B18, 0x1000024F0)], 0x100000000)
    assert _unwind_info_lsdas(data, 0x100000000) == [
        (0x1000004E8, 0x1000024D4),
        (0x100000B18, 0x1000024F0),
    ]
    with pytest.raises(ValueError):
        _unwind_info_lsdas(struct.pack("<7I", 2, 28, 0, 28, 0, 28, 0), 0)
    with pytest.raises(ValueError):
        _unwind_info_lsdas(data[:60], 0x100000000)
    # Review 4 of PR #295: offsets from a base past the address space's end are malformed,
    # never an address.
    with pytest.raises(ValueError):
        _unwind_info_lsdas(data, (1 << 64) - 0x100)
    # Non-empty common and personality arrays at one offset share bytes.
    entries = struct.pack("<3I", 0x4E8, 0, 56) + struct.pack("<3I", 0x4E9, 0, 64)
    shared = (
        struct.pack("<7I", 1, 28, 1, 28, 1, 32, 2)
        + bytes(4)
        + entries
        + struct.pack("<II", 0x4E8, 0x24D4)
    )
    with pytest.raises(ValueError, match="Overlapping"):
        _unwind_info_lsdas(shared, 0x100000000)
    apart = struct.pack("<7I", 1, 28, 1, 32, 1, 36, 2) + bytes(8)
    apart += struct.pack("<3I", 0x4E8, 0, 60) + struct.pack("<3I", 0x4E9, 0, 68)
    apart += struct.pack("<II", 0x4E8, 0x24D4)
    assert _unwind_info_lsdas(apart, 0x100000000) == [(0x1000004E8, 0x1000024D4)]


def macho_image(sections):
    """A 64-bit Mach-O whose one segment, __TEXT at 0x100000000, holds ``sections``:
    (name, address, bytes) each."""
    size = 72 + 80 * len(sections)
    commands = struct.pack(
        "<II16sQQQQIIII", 0x19, size, b"__TEXT", 0x100000000, 0x4000, 0, 0x4000, 5, 5, 0, 0
    )
    commands = commands[:64] + struct.pack("<I", len(sections)) + commands[68:]
    blob, offset = b"", 32 + size
    for name, address, data in sections:
        place = struct.pack("<16s16sQQI", name, b"__TEXT", address, len(data), offset + len(blob))
        commands += place + bytes(28)
        blob += data
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 1, len(commands), 0, 0)
    return header + commands + blob


def elf_image(sections):
    """An ELF64 with section headers alone: `.shstrtab`, then ``sections``, (name, type,
    address, bytes) each."""
    names = b"\0.shstrtab\0" + b"".join(name + b"\0" for name, *_ in sections)
    parts, label = [(1, 3, 0, names)], 11
    for name, kind, address, data in sections:
        parts.append((label, kind, address, data))
        label += len(name) + 1
    blob, headers = b"", [bytes(64)]
    for label, kind, address, data in parts:
        place = (label, kind, 0, address, 64 + len(blob), len(data), 0, 0, 1, 0)
        headers.append(struct.pack("<IIQQQQIIQQ", *place))
        blob += data
    fields = (3, 62, 1, 0, 0, 64 + len(blob), 0, 64, 56, 0, 64, len(headers), 1)
    header = b"\x7fELF\x02\x01\x01" + bytes(9) + struct.pack("<HHIQQQIHHHHHH", *fields)
    return header + blob + b"".join(headers)


def test_landing_pads_come_from_a_mach_o_or_elf_executables_tables(tmp_path):
    from peeky.native import landing_pads

    # Mach-O: `__unwind_info`'s LSDA array, and `__gcc_except_tab`.
    program = tmp_path / "program"
    info = unwind_info([(0x1000004E8, 0x100003000)], 0x100000000)
    tables = [(b"__unwind_info", 0x100002000, info), (b"__gcc_except_tab", 0x100003000, MAIN_LSDA)]
    program.write_bytes(macho_image(tables))
    assert landing_pads(program) == {0x10000064C, 0x10000069C, 0x1000006E4}
    # ELF: `.eh_frame` FDEs, and `.gcc_except_table`; a NOBITS section has no bytes.
    frame = eh_frame(0x2000, [(0x14E8, 0x400, 0x3000)])
    tables = [
        (b".eh_frame", 1, 0x2000, frame),
        (b".gcc_except_table", 1, 0x3000, MAIN_LSDA),
        (b".bss", 8, 0x4000, b""),
    ]
    program.write_bytes(elf_image(tables))
    assert landing_pads(program) == {0x164C, 0x169C, 0x16E4}
    # A C program's tables name no LSDA: no landing pads.
    program.write_bytes(elf_image([(b".eh_frame", 1, 0x2000, eh_frame(0x2000, []))]))
    assert landing_pads(program) == frozenset()
    # LSDAs without the table they point into, or not a binary at all: unreadable.
    program.write_bytes(macho_image([(b"__unwind_info", 0x100002000, info)]))
    with pytest.raises(ValueError, match="LSDAs without"):
        landing_pads(program)
    program.write_bytes(b"#!/bin/sh\n")
    with pytest.raises(ValueError):
        landing_pads(program)


def record(body):
    """An `.eh_frame` record: its 4-byte length, then ``body``, padded to 4 bytes."""
    body += bytes(-len(body) % 4)
    return struct.pack("<I", len(body)) + body


def zplr_cie(augmentation=7):
    """A "zPLR" CIE's body: an indirect personality, pc-relative LSDAs and starts, and
    ``augmentation`` as its augmentation data's length."""
    fields = bytes([1, 0x78, 30, augmentation, 0x9B]) + bytes(4) + bytes([0x1B, 0x1B])
    return bytes(4) + b"\x01zPLR\0" + fields


def fde(at, start=0x14E8, lsda=0x3000, augmentation=4, address=0x2000):
    """The body of an FDE whose record starts at ``at`` of `.eh_frame`, under the CIE at
    0, naming ``lsda``."""
    begin = start - (address + at + 8)
    return struct.pack("<IiiBi", at + 4, begin, 0x400, augmentation, lsda - (address + at + 17))


def elf_tables(frame, lsda=MAIN_LSDA):
    return elf_image([(b".eh_frame", 1, 0x2000, frame), (b".gcc_except_table", 1, 0x3000, lsda)])


def macho_tables(info):
    tables = [(b"__unwind_info", 0x100002000, info), (b"__gcc_except_tab", 0x100003000, MAIN_LSDA)]
    return macho_image(tables)


def index_info(index, first, last=None, count=2):
    """An `__unwind_info` (92 bytes) with ``count`` index entries at ``index`` whose LSDA
    offsets are ``first``, then ``last``, and one LSDA (main's) at ``first``. What would
    lie over the header is left out: the header's own bytes are read there."""
    last = first + 8 if last is None else last
    entries = [struct.pack("<III", 0x4E8, 0, first), struct.pack("<III", 0x9000, 0, last)]
    data = bytearray(struct.pack("<7I", 1, 28, 0, 28, 0, index, count) + bytes(64))
    for n, entry in enumerate(entries[:count]):
        if 28 <= index + 12 * n <= len(data) - 12:
            data[index + 12 * n : index + 12 * n + 12] = entry
    if 28 <= first <= len(data) - 8:
        data[first : first + 8] = struct.pack("<II", 0x4E8, 0x3000)
    return bytes(data)


def test_a_wellformed_table_reads_and_each_malformed_case_below_is_its_variant(tmp_path):
    from peeky.native import landing_pads

    program = tmp_path / "program"
    program.write_bytes(elf_tables(record(zplr_cie()) + record(fde(28)) + bytes(4)))
    assert landing_pads(program) == {0x164C, 0x169C, 0x16E4}
    program.write_bytes(macho_tables(index_info(28, 52)))
    assert landing_pads(program) == {0x10000064C, 0x10000069C, 0x1000006E4}


# Review round 3 of #295: each must be unreadable (ValueError, for the reason given: no
# landing pad is known, so every preceded row is None), never read as having none.
MALFORMED = {
    "a CIE whose fields run past its end, into the terminator": (
        lambda: elf_tables(struct.pack("<I", 6) + bytes(4) + b"\x01\0" + bytes(4)),
        "LEB128",
    ),
    "a zPLR CIE cut after its augmentation string": (
        lambda: elf_tables(struct.pack("<I", 10) + zplr_cie()[:10] + bytes(4)),
        "LEB128",
    ),
    "a CIE whose augmentation data is shorter than its letters": (
        lambda: elf_tables(record(zplr_cie(augmentation=2)) + record(fde(28)) + bytes(4)),
        "Truncated pointer",
    ),
    "an FDE whose augmentation runs past its end": (
        lambda: elf_tables(record(zplr_cie()) + record(fde(28, augmentation=0x7F)) + bytes(4)),
        "augmentation passes its end",
    ),
    "an FDE whose LSDA pointer runs past its augmentation data": (
        lambda: elf_tables(record(zplr_cie()) + record(fde(28, augmentation=2)) + bytes(4)),
        "Truncated pointer",
    ),
    "a DWARF64 record": (
        lambda: elf_tables(struct.pack("<IQ", 0xFFFFFFFF, 12) + bytes(8) + b"\x01zR\0"),
        "DWARF64",
    ),
    "a call-site record running past its table's end": (
        lambda: elf_tables(
            record(zplr_cie()) + record(fde(28)) + bytes(4),
            MAIN_LSDA[:3] + b"\x10" + MAIN_LSDA[4:],
        ),
        "LEB128",
    ),
    "a call site's landing pad over 64 bits (2**64, read as 0)": (
        lambda: elf_tables(
            record(zplr_cie()) + record(fde(28)) + bytes(4),
            bytes.fromhex("ff ff 01 0d 30 4c") + b"\x80" * 9 + b"\x02" + b"\x00",
        ),
        "exceeds 64 bits",
    ),
    "an `__unwind_info` index over its header": (
        lambda: macho_tables(index_info(0, 52)),
        "arrays",
    ),
    "an `__unwind_info` index past its section": (
        lambda: macho_tables(index_info(80, 52)),
        "arrays",
    ),
    "an `__unwind_info` with no index": (
        lambda: macho_tables(index_info(28, 52, count=0)),
        "Unsupported",
    ),
    "an LSDA array over the index": (lambda: macho_tables(index_info(28, 40)), "LSDA array"),
    "an LSDA array over the header": (lambda: macho_tables(index_info(52, 0)), "LSDA array"),
    "a misaligned LSDA array": (lambda: macho_tables(index_info(28, 54)), "LSDA array"),
    "an LSDA array past its section": (
        lambda: macho_tables(index_info(28, 52, last=4096)),
        "LSDA array",
    ),
    "an LSDA array running backward": (
        lambda: macho_tables(index_info(28, 60, last=52)),
        "LSDA array",
    ),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_a_malformed_exception_table_is_unreadable_never_empty(tmp_path, case):
    from peeky.native import landing_pads

    build, reason = MALFORMED[case]
    program = tmp_path / "program"
    program.write_bytes(build())
    with pytest.raises(ValueError, match=reason):
        landing_pads(program)


def test_a_leb128_number_is_read_within_its_limit_and_64_bits():
    from peeky.native import _leb128

    assert _leb128(b"\xe4\x02", 0, 2) == (0x164, 2)
    assert _leb128(b"\x78", 0, 1, signed=True) == (-8, 1)
    assert _leb128(b"\xff" * 9 + b"\x01", 0, 10) == ((1 << 64) - 1, 10)
    assert _leb128(b"\x80" * 9 + b"\x7f", 0, 10, signed=True) == (-(1 << 63), 10)
    for data, limit, signed in (
        (b"\xe4\x02", 1, False),  # past its limit
        (b"\x80" * 9 + b"\x02", 10, False),  # 2**64
        (b"\x80" * 10 + b"\x01", 11, False),  # eleven bytes
        (b"\x80" * 9 + b"\x40", 10, True),  # -2**69
        (b"\x80" * 9 + b"\x01", 10, True),  # 2**63
    ):
        with pytest.raises(ValueError):
            _leb128(data, 0, limit, signed=signed)


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


RANGE_FOR_UNIT = b"""# 1 "a.c"
long f(void) {
    long q = 1;
    for (const auto &[q, u] : make(q)) { use(q * u); }
    for (auto [q, u] : v) { use(q); }
    for (const auto &[q, u] :
         make(q)) { use(q - u); }
    for (auto [q, u] : make(p.template q<0>())) { use(q); }
    for (auto [q, u] : make(sizeof(q))) { use(q); }
    for (auto [q, u] : v) {
        use(q);
    }
    return q;
}
"""


def stopped(unit, line, where):
    """A stop's frame on ``line`` of ``unit`` (its text as written, past the marker), at
    the line table's column ``where``: "past" the line's last `)` (its last character),
    "inside" a header (just before its `)`, where a body's `{` follows), at a range-`for`'s
    `:` ("pass": its implicit code, after the range), or None (no column)."""
    text = unit.decode().split("\n", 1)[1].splitlines()[line - 1]
    frame = {"instructionPointerReference": "0x150", "line": line}
    if where == "past":
        frame["column"] = len(text)
    elif where == "inside" and ") {" in text:
        frame["column"] = text.index(") {")
    elif where == "pass" and " : " in text:
        frame["column"] = text.index(" : ") + 2
    return frame


@pytest.mark.parametrize("entered", ["caller", None, True, False, "inside", "pass"])
def test_a_range_for_header_naming_the_outer_local_hedges_its_note(tmp_path, entered):
    # #283: `for (const auto &[q, u] : make(q)) { use(q * u); }` gave the outer `q` the
    # unhedged "not what the code reads", though the header runs before the binding
    # exists. entered: "caller" (a caller frame: no stop is told), None (an optimized
    # build), True or False (unoptimized: at the line's first instruction, or past it,
    # where the line table's column is after the header's `)`). "inside": past the first
    # instruction, but at a column inside the header (back from a call there, #287 review);
    # "pass": at the loop's `:`, whose implicit code (`begin`/`end` setup, each pass's test
    # and binding, the range's cleanup) is all emitted after the range expression.
    session = preprocessing_session(tmp_path, {"a.c": RANGE_FOR_UNIT})
    session.target.language = "cpp"
    (tmp_path / "a.c").write_bytes(RANGE_FOR_UNIT.split(b"\n", 1)[1])
    where = {False: "past"}.get(entered, entered if entered in ("inside", "pass") else None)
    ran = entered in (False, "pass")

    def note(line):
        frame = stopped(RANGE_FOR_UNIT, line, where)
        later = entered in ("inside", "pass")
        entering = None if entered == "caller" else (lambda: False if later else entered)
        return session._block_storage("q", frame, "a.c", line, entering=entering)

    hedged = (
        "may not be what the code reads",
        "the code here may name the structured binding `q` (line {}), but the loop's header "
        "names `q` before that binding exists, and Peeky cannot tell whether running the "
        "header reads this local: this row is the outer local of that name",
    )
    plain = (
        "not what the code reads",
        "the code here names the structured binding `q` (line {}): this row is the outer "
        "local of that name",
    )
    unsure = (
        "may not be what the code reads",
        "the code here may name the structured binding `q` (line 9), unless the stop starts "
        "the line, before that binding exists (in an optimized build Peeky cannot tell "
        "which): this row is the outer local of that name",
    )
    expected = {
        # On the line, a header that has run past the first instruction: the binding.
        3: plain if ran else hedged,
        4: plain,
        # A header over two lines: no stop on it can be told from its range's.
        5: hedged,
        6: hedged,
        # A member template's name is no mention of the local.
        7: plain,
        # `sizeof(q)` reads nothing, but a hedge says only that the header names it.
        8: plain if ran else hedged,
        9: {"caller": plain, None: unsure, True: None}.get(entered, plain),
    }
    for line, want in expected.items():
        found = note(line)
        if want is None:
            assert found is None, (line, found)
            continue
        binding = 5 if line in (5, 6) else line
        assert (found.label, str(found)) == (want[0], want[1].format(binding)), (line, found)
        assert "reads this local first" not in found and "reads this local first" not in (
            found.refusal
        )
        would = "may not" if want[0].startswith("may") else "would not"
        assert found.refusal.endswith(f"so a change to it {would} reach that code"), found.refusal
        if want is hedged:
            assert "Peeky cannot tell whether running the header reads" in found.refusal


STATEMENT_HEADER_UNIT = b"""# 1 "a.c"
long f(void) {
    long q = 1;
    if (q) { static long q = 5; use(q); }
    while (q-- > 0) { static long q = 6; use(q); }
    for (long i = 0; i < q; i++) { static long q = 7; use(q + i); }
    for (long i = q; i < 3; i++) { static long q = 8; use(q + i); }
    switch (q) { default: { static long q = 9; use(q); } }
    if (q > 0 &&
        q < 9) { static long q = 10; use(q); }
    if (q) { typedef int q; q k = 4; use(k); }
    if (p.q) { static long q = 11; use(q); }
    if (u) { static long q = 12; use(q); }
    { static long q = 13; if (q) { use(q); } }
    if (q) { static long q = 14; use(q); }
    return q;
}
"""
STATEMENT_HEADER_CPP_UNIT = b"""# 1 "a.c"
long f(void) {
    long q = 1;
    if (long k = q; k) { static long q = 5; use(q + k); }
    switch (long k = q; k) { default: { static long q = 6; use(q + k); } }
    for (long i = 0; i < q; i++) { auto [q, u] = make(i)[0]; use(q + u); }
    for (const auto &[a, b] : make2(q, [] { long r; { static long q = 7; r = q; } return r; })) { use(a); }
    for (const auto &[q, u] : make2(q, [] { long r; { static long q = 8; r = q; } return r; })) {
        use(q * u);
    }
    for (auto [a, b] : make(p.template q<0>())) { static long q = 9; use(q + a); }
    return q;
}
"""


@pytest.mark.parametrize("entered", ["caller", None, True, False, "inside"])
@pytest.mark.parametrize("language", ["c", "cpp"])
def test_a_statement_header_naming_the_outer_local_hedges_its_note(tmp_path, language, entered):
    # #286: `if (q) { static long q = 5; use(q); }` gave the outer `q` the unhedged "not
    # what the code reads: the code here names the block's `static`", though the `if`'s
    # header runs first and names the outer `q`. entered: as in the range-`for` test
    # above. A `while`'s condition, a `for`'s condition or increment, and a lambda's
    # `static` in the header (another function's) are hedged at every stop.
    unit = STATEMENT_HEADER_UNIT if language == "c" else STATEMENT_HEADER_CPP_UNIT
    session = preprocessing_session(tmp_path, {"a.c": unit})
    session.target.language = language
    # As written, line 14 names a macro the compiler expanded: its columns prove nothing.
    (tmp_path / "a.c").write_bytes(unit.split(b"\n", 1)[1].replace(b"q = 14;", b"q = FOURTEEN;"))
    where = {False: "past", "inside": "inside"}.get(entered)

    def note(line):
        frame = stopped(unit, line, where)
        later = entered == "inside"
        entering = None if entered == "caller" else (lambda: False if later else entered)
        return session._block_storage("q", frame, "a.c", line, entering=entering)

    def static(line):
        return f"the block's `static` `q` (line {line}), which the debugger does not list"

    def hedged(keyword, what):
        return (
            "may not be what the code reads",
            f"the code here may name {what}, but the `{keyword}` statement's header names `q` "
            "before that declaration, and Peeky cannot tell whether running the header reads "
            "this local: this row is the outer local of that name",
        )

    def plain(what):
        return (
            "not what the code reads",
            f"the code here names {what}: this row is the outer local of that name",
        )

    def once(keyword, what):
        # Past the first instruction (unoptimized), at a column after the header's `)`, a
        # header that runs once has run; not back inside it ("inside").
        return plain(what) if entered is False else hedged(keyword, what)

    if language == "c":
        expected = {
            3: once("if", static(3)),
            4: hedged("while", static(4)),
            5: hedged("for", static(5)),
            # Only the init-statement names it.
            6: once("for", static(6)),
            7: once("switch", static(7)),
            # A header over two lines: no stop on it can be told apart.
            8: None,
            9: hedged("if", static(9)),
            10: once("if", "the type `q` the block's `typedef` names (line 10)"),
            # A member; a header not naming it; a `static` before the header.
            11: plain(static(11)),
            12: plain(static(12)),
            13: plain(static(13)),
            # A stop past the `)` is not proven on a line a macro changes.
            14: hedged("if", static(14)),
        }
    else:
        binding = "the structured binding `q` (line {})".format
        expected = {
            3: once("if", static(3)),
            4: once("switch", static(4)),
            5: hedged("for", binding(5)),
            6: hedged("for", static(6)),
            # The loop's own binding (the range-`for` test above); at the line's first
            # instruction, before it exists, the lambda's `static` is next.
            7: {
                True: hedged("for", static(7)),
                False: plain(binding(7)),
            }.get(
                entered,
                (
                    "may not be what the code reads",
                    f"the code here may name {binding(7)}, but the loop's header names `q` "
                    "before that binding exists, and Peeky cannot tell whether running the "
                    "header reads this local: this row is the outer local of that name",
                ),
            ),
            10: plain(static(10)),
        }
    for line, want in expected.items():
        found = note(line)
        if want is None:
            assert found is None, (line, found)
            continue
        assert (found.label, str(found)) == want, (line, found)
        would = "may not" if want[0].startswith("may") else "would not"
        assert found.refusal.endswith(f"so a change to it {would} reach that code"), found.refusal
        assert want[1].split(": this row")[0][len("the code here ") :] in found.refusal, (
            found.refusal
        )


IF_CONSTEXPR_UNIT = b"""# 1 "a.c"
long f(void) {
    long q = 1;
    if constexpr (sizeof(long) > 1) { static long q = 5; use(q); }
    if consteval { static long q = 6; use(q); }
    if !consteval { static long q = 7; use(q); }
    return q;
}
"""


def test_an_if_constexpr_or_consteval_block_withholds_the_outer_local(tmp_path):
    # #289: `if constexpr (sizeof(long) > 1) { static long q = 5; use(q); }` showed the
    # outer `q` with no note, though the code there names the block's `static`. Peeky
    # builds C++17, where `if consteval` (C++23) is refused, but the scanner reads it so.
    frame = {"instructionPointerReference": "0x150"}
    session = preprocessing_session(tmp_path, {"a.c": IF_CONSTEXPR_UNIT})
    session.target.language = "cpp"
    for line in (3, 4, 5):
        found = session._block_storage("q", frame, "a.c", line)
        assert found is not None, line
        assert (found.label, str(found)) == (
            "not what the code reads",
            f"the code here names the block's `static` `q` (line {line}), which the debugger "
            "does not list: this row is the outer local of that name",
        ), line
        assert found.refusal.endswith("so a change to it would not reach that code")


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


def test_a_frame_whose_address_is_unknown_is_withheld_on_a_prologues_line(tmp_path, monkeypatch):
    # Review of #279: where the debugger gives no load address (`addressRange` missing or
    # unreadable, which a frame with a `moduleId` allows), no pc can be placed. On a line
    # where a prologue runs, the stop may be before it stored the parameters: at a GCC
    # function's start, or in any prologue on a fault or a signal.
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
        "Peeky cannot tell whether the stop is before or after its function's prologue stores "
        "the parameters: the debugger did not report where the program is loaded, so it "
        "cannot place the stop's instruction."
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
        with pytest.raises(ValueError, match="cannot tell whether the stop is before or after"):
            session.validate_mutation_target({"name": "a"}, frame(3))
        # A function on several lines says to stop past its first line.
        assert session.not_yet_declared({"name": "k"}, frame(4)).endswith(
            "Read the arguments at the call, or stop past the function's first line"
        )
        # A prologue whose end is marked (clang's) too (review round 2 of #280): a fault or
        # a signal can stop in it, on its line.
        marked = session.not_yet_declared({"name": "v"}, frame(5))
        assert marked == reason and marked.refusal == reason.refusal.replace("'a'", "'v'")
        # What the prologue leaves alone, and a line no prologue runs on, are not withheld.
        assert session.not_yet_declared({"name": "calls"}, frame(3)) is None
        assert session.not_yet_declared({"name": "a"}, frame(6)) is None
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


def code_die(offset, low, high, root, inline=False, depth=1, parent=None, call=None, first=None):
    from peeky.native import CodeDie

    return CodeDie(offset, ((low, high),), inline, depth, root, parent, call, {low: first})


def unplaced_session(tmp_path, language="cpp"):
    """A NativeSession whose table holds `main` (0x100-0x200, rows on lines 2 and 5 of
    a.cpp) and nothing else: GCC's lambdas and implicit constructors had no entry (#167).
    Its code: `main` and a function Peeky cannot place (0x300-0x340, line 8); line 9 runs
    both (review of #282)."""
    session = NativeSession.__new__(NativeSession)
    session.load_bias = 0x1000
    session.declarations = [
        {
            "root": 1,
            "source": "a.cpp",
            "ranges": ((0x100, 0x200),),
            "order": ((0x100, "a.cpp", 2), (0x180, "a.cpp", 5)),
            "locals": {"x": {("a.cpp", 3, True)}},
        }
    ]
    session.code = {
        "dies": (code_die(1, 0x100, 0x200, 1), code_die(2, 0x300, 0x340, None)),
        "lines": {
            ("a.cpp", 2): frozenset({1}),
            ("a.cpp", 5): frozenset({1}),
            ("a.cpp", 8): frozenset({None}),
            ("a.cpp", 9): frozenset({1, None}),
        },
    }
    session.target = SimpleNamespace(language=language, verification_inputs=("a.cpp",))
    session.prepared = SimpleNamespace(workspace=tmp_path)
    (tmp_path / "a.cpp").write_text("\n" * 12)
    return session


def test_a_cpp_frame_peeky_cannot_place_withholds_its_locals(tmp_path):
    # #167: GCC gives a lambda's `operator()` and an implicit constructor no source, so the
    # table had no entry for them, no rule ran, and their locals showed leftovers
    # (`x = 281474976709584` before `long x = a * 2;` ran) with no note.
    session = unplaced_session(tmp_path)

    def frame(address, path=None, line=8):
        return {
            "id": 1,
            "name": "operator()",
            "instructionPointerReference": None if address is None else hex(0x1000 + address),
            "source": {"path": str(path or tmp_path / "a.cpp")},
            "line": line,
        }

    reason = session.not_yet_declared({"name": "x"}, frame(0x300))
    assert reason == (
        "Peeky cannot place this function's declarations in the program's sources (its debug "
        "information ties it to none), so it cannot tell whether this is set yet: it may hold "
        "leftovers. Read what the function returns (`step --out`), or stop in a function "
        "`peeky describe` lists"
    )
    assert reason.label == "not shown" and len(reason) <= 300
    # Its parameters too, and a change is refused for the same reason.
    assert session.not_yet_declared({"name": "a"}, frame(0x300)) == reason
    assert reason.refusal == (
        "'x' cannot be changed here: Peeky cannot place this function's declarations in the "
        "program's sources, so it cannot tell whether the function's code overwrites a "
        "change. Change it in a function `peeky describe` lists"
    )
    with pytest.raises(ValueError, match="cannot place this function's declarations"):
        session.validate_mutation_target({"name": "a"}, frame(0x300))
    # A step's returned value is the callee's, not this frame's.
    assert session._unplaced({"name": "(Return Value)"}, frame(0x300)) is None
    # A function the table holds is judged by its rules, as before.
    assert session._unplaced({"name": "x"}, frame(0x150)) is None
    # The standard library's code, a frame with no source, and C are not judged so.
    header = tmp_path.parent / "usr" / "include" / "stl_algo.h"
    assert session._unplaced({"name": "x"}, frame(0x300, header)) is None
    assert session._unplaced({"name": "x"}, {**frame(0x300), "source": {}}) is None
    assert unplaced_session(tmp_path, "c")._unplaced({"name": "x"}, frame(0x300)) is None
    # Without the build's code (an older table), a frame no entry holds, as before.
    session.code = None
    assert session._unplaced({"name": "x"}, frame(0x300)) == reason
    assert session._unplaced({"name": "x"}, frame(0x150)) is None
    # Nothing read of the build: unjudged, as before.
    session.declarations = None
    assert session.not_yet_declared({"name": "x"}, frame(0x300)) is None


def test_a_frame_whose_address_is_unknown_is_placed_only_by_one_functions_line(tmp_path):
    # Review of #282: without the load address, a line some placed function runs proved
    # nothing: an inlined call Peeky cannot place runs on its own lines inside it. Only a
    # line whose every row is one placed function's own code places the frame.
    session = unplaced_session(tmp_path)
    session.load_bias, session.load_bias_unknown = None, True

    def frame(line):
        return {
            "id": 1,
            "instructionPointerReference": "0x1300",
            "source": {"path": str(tmp_path / "a.cpp")},
            "line": line,
        }

    assert session._unplaced({"name": "x"}, frame(5)) is None
    # All of the line's code is what Peeky cannot place.
    assert session._unplaced({"name": "x"}, frame(8)).startswith(
        "Peeky cannot place this function's declarations"
    )
    # Both (an inlined call Peeky cannot place, on its caller's line), or no code at all.
    for line in (9, 11):
        reason = session._unplaced({"name": "x"}, frame(line))
        assert reason == (
            "Peeky cannot tell which function this frame runs (the debugger did not report "
            "where the program is loaded): no one function it places runs all the code on "
            "this line, so it cannot tell whether this is set yet: it may hold leftovers. "
            "Read it on a line only one function `peeky describe` lists runs"
        )
        assert reason.label == "not shown" and len(reason) <= 300
        assert reason.refusal == (
            "'x' cannot be changed here: Peeky cannot tell which function this frame runs, so "
            "it cannot tell whether that function's code overwrites a change. Change it in a "
            "function `peeky describe` lists"
        )
    # Without the build's code, no line places it.
    session.code = None
    assert "cannot tell which function" in session._unplaced({"name": "x"}, frame(5))


def test_an_inlined_call_peeky_cannot_place_is_not_its_callers(tmp_path):
    # Review of #282: an inlined call shares its caller's code, so `_function_at` found
    # the caller's entry for its frame, and the net never ran: a forced-inline helper a
    # `#line` declares elsewhere showed `x` as leftovers inside `main`.
    from peeky.native import frame_level

    main = code_die(1, 0x100, 0x200, 1, first=("a.cpp", 14))
    # `helper`, inlined into `main` (called on line 16) at 0x140-0x160: Peeky cannot place
    # it; `twice`, which it places, inlined into `helper` (called on line 11) at 0x150.
    helper = code_die(2, 0x140, 0x160, None, True, 2, 1, ("a.cpp", 16), ("a.cpp", 10))
    twice = code_die(3, 0x150, 0x158, 4, True, 3, 2, ("a.cpp", 11), ("a.cpp", 3))
    code = (main, helper, twice)
    lines = {
        ("a.cpp", 16): frozenset({1}),
        ("a.cpp", 10): frozenset({None}),
        ("a.cpp", 11): frozenset({None}),
    }
    # The innermost frame in `helper`'s code is `helper`'s; in `twice`'s, `twice`'s.
    assert frame_level(code, 0x144, False, ("a.cpp", 10), lines) == {None}
    assert frame_level(code, 0x154, False, ("a.cpp", 3), lines) == {4}
    # At `helper`'s start, shown on its call's line: LLDB shows the call, in `main`.
    assert frame_level(code, 0x140, False, ("a.cpp", 16), lines) == {1}
    assert frame_level(code, 0x140, False, ("a.cpp", 10), lines) == {None}
    # A caller frame: LLDB gives the caller of an inlined call the address its code starts
    # at (`main` at 0x140, `helper` at 0x150), and a call's caller its return address.
    # The code changes there (review round 2): only the frame's line tells which it is.
    assert frame_level(code, 0x140, True, ("a.cpp", 16), lines) == {1}
    assert frame_level(code, 0x140, True, ("a.cpp", 16)) == {1, None}
    assert frame_level(code, 0x150, True, ("a.cpp", 11), lines) == {None}
    assert frame_level(code, 0x15C, True, ("a.cpp", 12), lines) == {None}
    assert frame_level(code, 0x184, True, ("a.cpp", 17), lines) == {1}
    # The call's own first row on its call's line: either may be meant.
    same = helper._replace(starts={0x140: ("a.cpp", 16)})
    assert frame_level((main, same, twice), 0x140, False, ("a.cpp", 16), lines) == {1, None}
    # A function Peeky places inlined into one it cannot (a lambda into `std::sort`).
    library = code_die(5, 0x400, 0x500, None)
    lambda_ = code_die(6, 0x420, 0x430, 7, True, 2, 5, None, ("a.cpp", 20))
    assert frame_level((library, lambda_), 0x424, False, ("a.cpp", 20)) == {7}
    assert frame_level((library, lambda_), 0x420, True, None) == {None, 7}
    # The session: frame 0 in `helper` is withheld, and its caller `main` is judged as
    # before, by its own entry.
    session = unplaced_session(tmp_path)
    session.code = {"dies": code, "lines": lines}

    def frame(identity, address, line):
        return {
            "id": identity,
            "instructionPointerReference": hex(0x1000 + address),
            "source": {"path": str(tmp_path / "a.cpp")},
            "line": line,
        }

    stack = [frame(1, 0x144, 10), frame(2, 0x140, 16)]
    session.frames_listed(stack)
    assert session._function_at(stack[0]) is None
    assert session._unplaced({"name": "x"}, stack[0]).startswith("Peeky cannot place")
    assert session._function_at(stack[1]) is session.declarations[0]
    assert session._unplaced({"name": "seed"}, stack[1]) is None
    shared = {**stack[0], "line": 16}
    session.code = {"dies": (main, same, twice), "lines": lines}
    session.frames_listed([{**shared, "instructionPointerReference": hex(0x1140)}])
    reason = session._unplaced({"name": "x"}, {**shared, "instructionPointerReference": "0x1140"})
    assert reason == (
        "Peeky cannot tell which function this frame runs: more than one runs the code at its "
        "address (an inlined call and its caller, or copies of one function), so it cannot "
        "tell whether this is set yet: it may hold leftovers. Read it at a stop on a line "
        "only one of them runs"
    )
    assert reason.label == "not shown" and "cannot tell which function" in reason.refusal


def test_a_frame_two_placed_functions_may_run_is_placed_by_neither(tmp_path):
    # Review round 2 of #282: of several roots that may run a frame's code, `_function_at`
    # took the narrowest whenever all were placed: an inlined call's start on its own
    # call's line, or a caller frame at the end of an inlined call, was taken for the
    # call's, and judged by its declarations.
    from peeky.native import frame_level

    parent = code_die(1, 0x100, 0x200, 1, first=("a.cpp", 14))
    child = code_die(2, 0x140, 0x160, 2, True, 2, 1, ("a.cpp", 15), ("a.cpp", 15))
    both = {("a.cpp", 15): frozenset({1, 2}), ("a.cpp", 17): frozenset({1})}
    one = {("a.cpp", 15): frozenset({2})}
    # At the child's start, on its call's line, where its own first row is too: either,
    # whatever the line's rows say.
    assert frame_level((parent, child), 0x140, False, ("a.cpp", 15), both) == {1, 2}
    assert frame_level((parent, child), 0x140, False, ("a.cpp", 15), one) == {1, 2}
    # Inside the child, the child's; outside it, the parent's.
    assert frame_level((parent, child), 0x148, False, ("a.cpp", 15), both) == {2}
    assert frame_level((parent, child), 0x180, False, ("a.cpp", 17), both) == {1}
    # A caller frame at the child's end (its last instruction calls): the code before its
    # address is the child's, at it the parent's. Its line, or its name, may tell which.
    short = (parent, child._replace(ranges=((0x140, 0x150),)))
    assert frame_level(short, 0x150, True, ("a.cpp", 15), both) == {1, 2}
    assert frame_level(short, 0x150, True, ("a.cpp", 15), one) == {2}
    names = {1: "main", 2: "helper"}
    assert frame_level(short, 0x150, True, ("a.cpp", 15), both, names, "helper(long)") == {2}
    assert frame_level(short, 0x150, True, ("a.cpp", 15), both, names, "main") == {1}
    assert frame_level(short, 0x150, True, ("a.cpp", 15), both, names, "other(int)") == {1, 2}
    # Review 3 of PR #282: a line only `helper` owns, on a frame named `main`, contradict
    # each other: both stay, and the frame is withheld. Where they agree, that one.
    assert frame_level(short, 0x150, True, ("a.cpp", 15), one, names, "main") == {1, 2}
    assert frame_level(short, 0x150, True, ("a.cpp", 15), one, names, "helper(long)") == {2}
    assert frame_level(short, 0x150, True, ("a.cpp", 15), one, names, "other(int)") == {2}
    # Copies of a function at one address (folded), placed or not: either.
    folded = (code_die(1, 0x100, 0x200, 1), code_die(3, 0x100, 0x200, 3))
    assert frame_level(folded, 0x120, False, ("a.cpp", 3)) == {1, 3}
    copied = (code_die(1, 0x100, 0x200, 1), code_die(3, 0x100, 0x200, None))
    assert frame_level(copied, 0x120, False, ("a.cpp", 3)) == {1, None}
    # The session takes the one root's entry, never the narrowest holding the address
    # before a caller's (the child's, here); where there is none, the frame is withheld.
    session = unplaced_session(tmp_path)
    main_entry = {**session.declarations[0], "name": "main"}
    helper_entry = {"root": 2, "name": "helper", "source": "a.cpp", "ranges": ((0x140, 0x150),)}
    session.declarations = [main_entry, helper_entry]
    session.code = {"dies": short, "lines": both}

    def frame(identity, address, line, name):
        return {
            "id": identity,
            "name": name,
            "instructionPointerReference": hex(0x1000 + address),
            "source": {"path": str(tmp_path / "a.cpp")},
            "line": line,
        }

    innermost, caller = frame(1, 0x400, 3, "twice(long)"), frame(2, 0x150, 15, "main")
    session.frames_listed([innermost, caller])
    assert session._narrowest(0x14F) is helper_entry
    assert session._function_at(caller) is main_entry
    assert session._unplaced({"name": "x"}, caller) is None
    unnamed = {**caller, "name": "run"}
    assert session._function_at(unnamed) is None
    assert session._unplaced({"name": "x"}, unnamed).startswith(
        "Peeky cannot tell which function this frame runs: more than one"
    )
    # At the child's start on the call's line, where its first row is too: withheld.
    session.code = {"dies": (parent, child), "lines": one}
    tie = {**frame(1, 0x140, 15, "helper(long)")}
    session.frames_listed([tie])
    assert session._function_at(tie) is None
    assert "more than one runs the code" in session._unplaced({"name": "x"}, tie)
    # A C target keeps the narrowest, and is not judged so.
    session.target = SimpleNamespace(language="c", verification_inputs=("a.cpp",))
    session.frames_listed([innermost, caller])
    assert session._function_at(unnamed) is helper_entry
    assert session._unplaced({"name": "x"}, unnamed) is None


def gcc_dumps(workspace, class_declared=True):
    """A C++ unit as GCC 13 builds it (#167): `main` (lines 8-17) defines a lambda (line
    10, lines 10-14) whose `operator()` and closure are artificial and name no file or
    line, but the closure does; `nested` (line 15) builds an unnamed struct (line 16)
    whose `<constructor>` is artificial too, its code a copy inside the struct. Without
    ``class_declared``, the closure names no file or line either, and the lambda's first
    row (0x8c0) is the code of `twice` (line 3), inlined into it."""
    source = workspace / "gcc.cpp"
    declared = (
        f'                  DW_AT_decl_file\t("{source}")\n                  DW_AT_decl_line\t(10)\n'
        if class_declared
        else ""
    )
    inlined = (
        ""
        if class_declared
        else f"""0x0000006c:         DW_TAG_inlined_subroutine
                      DW_AT_abstract_origin\t(0x000000e8 "twice")
                      DW_AT_low_pc\t(0x00000000000008c0)
                      DW_AT_high_pc\t(0x00000000000008cc)
                      DW_AT_call_file\t("{source}")
                      DW_AT_call_line\t(10)
"""
    )
    twice = (
        ""
        if class_declared
        else f"""0x000000e8:   DW_TAG_subprogram
                DW_AT_name\t("twice")
                DW_AT_decl_file\t("{source}")
                DW_AT_decl_line\t(3)
                DW_AT_inline\t(DW_INL_inlined)
"""
    )
    info = f"""{workspace}/program: file format elf64-littleaarch64

.debug_info contents:
0x0000000b: DW_TAG_compile_unit
              DW_AT_name\t("gcc.cpp")
0x00000020:   DW_TAG_subprogram
                DW_AT_name\t("main")
                DW_AT_decl_file\t("{source}")
                DW_AT_decl_line\t(8)
                DW_AT_low_pc\t(0x0000000000000900)
                DW_AT_high_pc\t(0x0000000000000980)
                DW_AT_frame_base\t(DW_OP_call_frame_cfa)
0x0000002c:     DW_TAG_variable
                  DW_AT_name\t("base")
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(9)
                  DW_AT_location\t(DW_OP_fbreg -48)
0x00000038:     DW_TAG_structure_type
                  DW_AT_byte_size\t(0x08)
{declared}0x00000040:       DW_TAG_subprogram
                    DW_AT_name\t("<lambda>")
                    DW_AT_artificial\t(true)
                    DW_AT_declaration\t(true)
0x00000048:         DW_TAG_formal_parameter
                      DW_AT_artificial\t(true)
0x0000004c:         NULL
0x00000050:       DW_TAG_subprogram
                    DW_AT_name\t("operator()")
                    DW_AT_artificial\t(true)
                    DW_AT_object_pointer\t(0x0000005c)
                    DW_AT_low_pc\t(0x00000000000008c0)
                    DW_AT_high_pc\t(0x0000000000000900)
                    DW_AT_frame_base\t(DW_OP_call_frame_cfa)
0x0000005c:         DW_TAG_formal_parameter
                      DW_AT_name\t("__closure")
                      DW_AT_artificial\t(true)
                      DW_AT_location\t(DW_OP_fbreg -24)
0x00000060:         DW_TAG_formal_parameter
                      DW_AT_name\t("a")
                      DW_AT_decl_file\t("{source}")
                      DW_AT_decl_line\t(10)
                      DW_AT_location\t(DW_OP_fbreg -32)
0x00000068:         DW_TAG_variable
                      DW_AT_name\t("base")
                      DW_AT_artificial\t(true)
                      DW_AT_location\t(DW_OP_fbreg -24, DW_OP_deref)
{inlined}0x00000070:         DW_TAG_lexical_block
                      DW_AT_low_pc\t(0x00000000000008cc)
                      DW_AT_high_pc\t(0x00000000000008f4)
0x00000078:           DW_TAG_variable
                        DW_AT_name\t("x")
                        DW_AT_decl_file\t("{source}")
                        DW_AT_decl_line\t(11)
                        DW_AT_location\t(DW_OP_fbreg -16)
0x00000080:           DW_TAG_variable
                        DW_AT_name\t("y")
                        DW_AT_decl_file\t("{source}")
                        DW_AT_decl_line\t(12)
                        DW_AT_location\t(DW_OP_fbreg -8)
0x00000088:           NULL
0x00000089:         NULL
0x0000008a:       NULL
0x00000090:     DW_TAG_variable
                  DW_AT_name\t("f")
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(10)
                  DW_AT_location\t(DW_OP_fbreg -40)
0x00000098:     NULL
0x000000a0:   DW_TAG_subprogram
                DW_AT_name\t("nested")
                DW_AT_decl_file\t("{source}")
                DW_AT_decl_line\t(15)
                DW_AT_low_pc\t(0x0000000000000a00)
                DW_AT_high_pc\t(0x0000000000000a40)
                DW_AT_frame_base\t(DW_OP_call_frame_cfa)
0x000000a8:     DW_TAG_structure_type
                  DW_AT_byte_size\t(0x10)
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(16)
0x000000b0:       DW_TAG_subprogram
                    DW_AT_name\t("<constructor>")
                    DW_AT_artificial\t(true)
                    DW_AT_object_pointer\t(0x000000b8)
                    DW_AT_inline\t(DW_INL_declared_not_inlined)
0x000000b8:         DW_TAG_formal_parameter
                      DW_AT_name\t("this")
                      DW_AT_artificial\t(true)
0x000000bc:         NULL
0x000000c0:       DW_TAG_subprogram
                    DW_AT_abstract_origin\t(0x000000b0 "<constructor>")
                    DW_AT_object_pointer\t(0x000000c8)
                    DW_AT_low_pc\t(0x00000000000009c0)
                    DW_AT_high_pc\t(0x0000000000000a00)
                    DW_AT_frame_base\t(DW_OP_call_frame_cfa)
0x000000c8:         DW_TAG_formal_parameter
                      DW_AT_abstract_origin\t(0x000000b8 "this")
                      DW_AT_location\t(DW_OP_fbreg -8)
0x000000cc:         NULL
0x000000cd:       NULL
0x000000d0:     DW_TAG_variable
                  DW_AT_name\t("anon")
                  DW_AT_decl_file\t("{source}")
                  DW_AT_decl_line\t(16)
                  DW_AT_location\t(DW_OP_fbreg -24)
0x000000d8:     NULL
{twice}0x000000f0:   NULL
"""
    rows = [
        (0x8C0, 10 if class_declared else 3),
        (0x8CC, 11),
        (0x8DC, 12),
        (0x8EC, 13),
        (0x8F4, 14),
    ]
    rows += [(0x900, 8), (0x90C, 9), (0x918, 10), (0x930, 15), (0x940, 16), (0x970, 17)]
    rows += [(0x9C0, 16), (0x9D0, 16), (0xA00, 15), (0xA10, 16), (0xA20, 17)]
    line = 'debug_line[0x00000000]\nversion: 4\nfile_names[ 1]:\nname: "gcc.cpp"\ndir_index: 0\n'
    line += "Address Line Column File\n"
    line += "".join(f"0x{address:016x} {number} 1 1 0 0 is_stmt\n" for address, number in rows)
    line += f"0x{0xA40:016x} 17 1 1 0 0 end_sequence\n"
    return info, line


def test_gccs_lambdas_and_implicit_constructors_are_placed_by_their_class(tmp_path):
    # #167: GCC gives a lambda's `operator()` and an implicit constructor no file or line,
    # so they had no entry: no rule ran in them, and their locals showed leftovers. They
    # take their class's, as clang gives them.
    from peeky.native import frame_level, function_scopes, local_declarations

    (tmp_path / "gcc.cpp").write_text("\n" * 20)
    info, line = gcc_dumps(tmp_path)
    table = {
        entry["name"]: entry for entry in local_declarations(info, line, tmp_path, ("gcc.cpp",))
    }
    assert sorted(table) == ["main", "main::operator()", "nested", "nested::<constructor>"]
    lambda_ = table["main::operator()"]
    assert (lambda_["source"], lambda_["span"], lambda_["ranges"]) == (
        "gcc.cpp",
        (10, 14),
        ((0x8C0, 0x900),),
    )
    # A member of a class `main` defines, at the closure's line; no constructor, and not a
    # default member initializer's.
    assert (lambda_["constructor"], lambda_["enclosing"], lambda_["defined"]) == (False, 0x20, 10)
    assert lambda_["initializer"] is False
    assert {"a", "x", "y"} <= set(lambda_["locals"])
    assert lambda_["locals"]["x"] == {("gcc.cpp", 11, True)}
    built = table["nested::<constructor>"]
    assert (built["source"], built["span"], built["constructor"]) == ("gcc.cpp", (16, 16), True)
    assert (built["enclosing"], built["defined"]) == (0xA0, 16)
    # `main`'s closure is no class whose lines `main` runs.
    assert table["main"]["classes"] == ((10, True),)
    # `describe` lists the lambda as clang's: its parameter and locals, never GCC's
    # `__closure` or its captures (lldb-dap lists neither).
    scopes = {
        scope["scope"]: scope for scope in function_scopes(info, line, tmp_path, ("gcc.cpp",))
    }
    assert scopes["main::operator()"]["lines"] == [10, 14]
    assert scopes["main::operator()"]["names"] == ["a", "x", "y"]
    # A closure that names no file or line either (review of #282): no row places it, as
    # its first may be another function's code inlined there (`twice`'s, line 3). Its
    # frames withhold their locals (the code says Peeky cannot place it); in `twice`'s
    # code, the frame is `twice`'s.
    info, line = gcc_dumps(tmp_path, class_declared=False)
    code = {}
    table = {
        entry["name"]: entry
        for entry in local_declarations(info, line, tmp_path, ("gcc.cpp",), code=code)
    }
    assert sorted(table) == ["main", "nested", "nested::<constructor>", "twice"]
    dies = {each.offset: each for each in code["dies"]}
    assert dies[0x50].root is None and dies[0x6C].root == 0xE8
    assert (dies[0x6C].parent, dies[0x6C].call, dies[0x6C].starts) == (
        0x50,
        ("gcc.cpp", 10),
        {0x8C0: ("gcc.cpp", 3)},
    )
    assert code["lines"][("gcc.cpp", 3)] == {0xE8} and code["lines"][("gcc.cpp", 11)] == {None}
    lines = code["lines"]
    assert frame_level(code["dies"], 0x8D0, False, ("gcc.cpp", 11), lines) == {None}
    assert frame_level(code["dies"], 0x8C4, False, ("gcc.cpp", 3), lines) == {0xE8}
    # The lambda's frame above `twice`'s, at its start: the lambda's or `twice`'s.
    assert frame_level(code["dies"], 0x8C0, True, ("gcc.cpp", 10), lines) == {None, 0xE8}


def test_gccs_closures_and_constructors_are_told_from_their_names():
    from peeky.native import _CONSTRUCTOR_NAME, _LIFETIME_NAME, _class_role, _local_classes

    def die(tag, parent, children=(), **attrs):
        return {"tag": tag, "parent": parent, "attrs": attrs, "children": list(children)}

    dies = {
        1: die("DW_TAG_compile_unit", None),
        2: die("DW_TAG_subprogram", 1, DW_AT_name='"run"'),
        # GCC's closure, named or not; a lambda returning nothing has no return type.
        3: die("DW_TAG_structure_type", 2, [4, 6, 7], DW_AT_decl_line="12"),
        4: die(
            "DW_TAG_subprogram",
            3,
            [5],
            DW_AT_name='"operator()"',
            DW_AT_artificial="true",
            DW_AT_object_pointer="0x00000005",
        ),
        5: die("DW_TAG_formal_parameter", 4, DW_AT_name='"__closure"'),
        6: die("DW_TAG_subprogram", 3, DW_AT_name='"_FUN"', DW_AT_artificial="true"),
        7: die("DW_TAG_subprogram", 3, DW_AT_name='"<lambda>"', DW_AT_artificial="true"),
        8: die(
            "DW_TAG_structure_type", 2, [9], DW_AT_name='"<lambda(long int)>"', DW_AT_decl_line="20"
        ),
        9: die("DW_TAG_subprogram", 8, DW_AT_name='"operator()<int>"', DW_AT_artificial="true"),
        10: die("DW_TAG_structure_type", 2, [11, 12], DW_AT_decl_line="30"),
        11: die("DW_TAG_subprogram", 10, DW_AT_name='"<constructor>"', DW_AT_artificial="true"),
        12: die("DW_TAG_subprogram", 10, DW_AT_name='"~<constructor>"', DW_AT_artificial="true"),
        # A closure known only by its object pointer's name.
        13: die("DW_TAG_structure_type", 2, [14], DW_AT_decl_line="40"),
        # GCC's closure of a lambda whose every call it folded: no `operator()` at all.
        17: die("DW_TAG_structure_type", 2, [18], DW_AT_decl_line="50"),
        18: die("DW_TAG_subprogram", 17, DW_AT_name='"<lambda>"', DW_AT_artificial="true"),
        14: die("DW_TAG_subprogram", 13, [15], DW_AT_object_pointer="0x0000000f"),
        15: die("DW_TAG_formal_parameter", 14, DW_AT_name='"__closure"'),
    }
    # A lambda's `operator()` and `_FUN` are no constructors, though artificial with no
    # return type; the closure's own `<lambda>` and an unnamed struct's `<constructor>` are.
    assert _class_role(dies, [4]) == (False, 2, 12, False)
    assert _class_role(dies, [6]) == (False, 2, 12, False)
    assert _class_role(dies, [7])[0] is True
    assert _class_role(dies, [9]) == (False, 2, 20, False)
    assert _class_role(dies, [11]) == (True, 2, 30, False)
    assert _class_role(dies, [12])[0] is False
    # A closure inside a class's scope is a default member initializer's: by its name, a
    # generic lambda's, or its object pointer, `__closure`.
    for closure, call in ((3, 4), (8, 9), (13, 14)):
        dies[closure]["parent"] = 16
        dies[16] = die("DW_TAG_structure_type", 1, DW_AT_name='"Agg"')
        assert _class_role(dies, [call])[3] is True, call
        dies[closure]["parent"] = 2
    # Closures are not the function's classes: a generic lambda's too.
    assert _local_classes(dies) == {
        2: ((12, True), (20, True), (30, False), (40, True), (50, True))
    }
    for name in ("<constructor>", "nested(long)::<constructor>()", "<lambda>"):
        assert _CONSTRUCTOR_NAME.search(name), name
    for name in ("~<constructor>", "operator()", "main::<lambda(int)>::operator()(int) const"):
        assert not _CONSTRUCTOR_NAME.search(name), name
    assert _LIFETIME_NAME.search("~<constructor>") and not _LIFETIME_NAME.search("<constructor>")


def test_an_inlined_call_with_no_code_left_holds_no_address():
    # Review of #282: GCC -O1 inlines `std::allocator<int>`'s destructor into `main` with no
    # code left: low_pc and high_pc 0x878, which dwarfdump writes as addresses. Read as an
    # offset, it held 0x878-0x10f0, all of `main` after it, and `main`'s frame was taken for
    # that call's.
    from peeky.native import _code_ranges

    def die(high):
        return {"attrs": {"DW_AT_low_pc": "0x0000000000000878", "DW_AT_high_pc": high}}

    assert _code_ranges(die("0x0000000000000878")) == []
    assert _code_ranges(die("0x00000000000008a0")) == [(0x878, 0x8A0)]
    # An offset (dwarfdump writes the attribute's data, at most 8 digits).
    assert _code_ranges(die("0x00000020")) == [(0x878, 0x898)]
    assert _code_ranges(die("0x00000000")) == []
