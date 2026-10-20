import struct
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


@pytest.mark.parametrize("command_index,retryable", [(0, True), (1, True), (2, False)])
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

    class Captured(Exception):
        pass

    def capture(recipe, root, **kwargs):
        assert recipe.language == "cpp"
        assert recipe.inputs == ("main.cpp", "types.hpp")
        assert all(command.argv[0] == "/trusted/clang++-18" for command in recipe.build_commands)
        assert "-std=c++17" in recipe.build_commands[0].argv
        assert "-std=c11" not in recipe.build_commands[0].argv
        assert recipe.build_commands[1].argv[-2:] == ("-o", "program")
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
    "name, line, below",
    [
        ("refill", 7, 8),  # #74: declared on the next line, so leftovers here
        ("refill", 8, None),  # its own line: a for-header counter is live there again
        ("elapsed", 7, None),
        ("calls", 7, None),  # a static has its value before its declaration
        ("now", 7, None),  # the parameter or the block's `now`: either may be shown
        ("now @ bucket.c:11", 7, 11),  # lldb-dap names each shadowed local
        ("now @ bucket.c:5", 7, None),
        ("missing", 7, None),
        ("refill", 13, None),  # outside the function
    ],
)
def test_a_local_is_withheld_only_when_every_candidate_is_declared_below(
    tmp_path, name, line, below
):
    from peeky.native import declared_below, local_declarations

    (tmp_path / "bucket.c").write_text("\n" * 20)
    info, dump = declaration_dumps(tmp_path)
    table = local_declarations(info, dump, tmp_path, ("bucket.c",))
    assert declared_below(table, name, "bucket.c", line) == below
    assert declared_below(table, name, "other.c", line) is None


def rows(spans, source="a.cpp"):
    return {(source, line): span for line, span in spans.items()}


def test_only_functions_owning_the_line_count_and_jumps_disable_withholding():
    from peeky.native import declared_below

    # Review of #81: a local class's constructor owns its member initializer on line 5,
    # above its own declaration; `main`, which defines the class, declares `v` on line 12.
    ctor = {"source": "a.cpp", "rows": rows({5: (50, 50), 7: (70, 70), 8: (80, 80)})}
    ctor.update(locals={"v": {(None, None, False)}}, jumps=False)
    main = {"source": "a.cpp", "rows": rows({3: (3, 3), 11: (11, 11), 12: (12, 12)})}
    main["jumps"] = False
    main["locals"] = {"v": {("a.cpp", 12, True)}}
    assert declared_below([ctor, main], "v", "a.cpp", 5) is None
    assert declared_below([ctor, main], "v", "a.cpp", 11) == 12
    # A backward goto can return above the declaration with `v` live.
    assert declared_below([ctor, {**main, "jumps": True}], "v", "a.cpp", 11) is None


@pytest.mark.parametrize(
    "rows, below",
    [
        ({7: (10, 12), 8: (20, 24)}, 8),  # line 7's code all comes before line 8's
        # Review of #81: clang runs a struct's default member initializer in its caller,
        # on the struct's line, inside the code of a later line.
        ({7: (30, 32), 8: (20, 24)}, None),
        # GCC runs a `cleanup` at its variable's line when the block ends: line 7 then
        # has code after line 8's.
        ({7: (10, 40), 8: (20, 24)}, None),
        ({7: (10, 12)}, None),  # a declaration without code proves nothing
    ],
)
def test_a_local_is_withheld_only_when_the_lines_code_comes_before_its_declaration(rows, below):
    from peeky.native import declared_below

    function = {"source": "a.c", "rows": {("a.c", n): span for n, span in rows.items()}}
    function.update(locals={"later": {("a.c", 8, True)}}, jumps=False)
    assert declared_below([function], "later", "a.c", 7) == below


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
    origin = {"source": "geo.hpp", "jumps": False, "locals": {"x": {("geo.hpp", 5, True)}}}
    origin["rows"] = {("geo.hpp", 2): (40, 42), ("geo.hpp", 5): (50, 52)}
    assert declared_below([main, origin], "x", "geo.hpp", 2) is None
    assert declared_below([origin], "x", "geo.hpp", 2) == 5
    # Declared on the stop line itself: never withheld (a `for` counter is live there
    # at a later pass).
    loop = {"source": "a.c", "jumps": False, "locals": {"i": {("a.c", 3, True)}}}
    loop["rows"] = {("a.c", 3): (10, 12), ("a.c", 4): (20, 30)}
    assert declared_below([loop], "i", "a.c", 3) is None


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
    }
    main["locals"] = {
        "i": {("a.c", 8, True)},
        "total": {("a.c", 7, True)},
        "sq": {("a.c", 8, True)},
    }
    main["starts"] = {("i", "a.c", 8): {0x80}, ("sq", "a.c", 8): {0x90}}
    assert declared_on_line([main], "i", "a.c", 8, 0x80) == 8
    assert declared_on_line([main], "sq", "a.c", 8, 0x90) == 8
    # Mid-line (a return from a call, a loop's later part), the local is live.
    assert declared_on_line([main], "i", "a.c", 8, 0x88) is None
    assert declared_on_line([main], "i", "a.c", 8, 0x90) is None
    assert declared_on_line([main], "total", "a.c", 8, 0x80) is None
    # An address outside the line (a wrong load bias) proves nothing.
    assert declared_on_line([main], "i", "a.c", 8, 0x70) is None
    assert declared_on_line([{**main, "jumps": True}], "i", "a.c", 8, 0x80) is None
    # A same-named static, or another `i` declared elsewhere in the function.
    static = {**main, "locals": {"i": {("a.c", 8, False)}}}
    assert declared_on_line([static], "i", "a.c", 8, 0x80) is None
    twice = {**main, "locals": {"i": {("a.c", 8, True), ("a.c", 3, True)}}}
    assert declared_on_line([twice], "i", "a.c", 8, 0x80) is None
    assert declared_on_line([twice], "i @ a.c:8", "a.c", 8, 0x80) == 8


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
