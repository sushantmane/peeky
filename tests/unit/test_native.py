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
